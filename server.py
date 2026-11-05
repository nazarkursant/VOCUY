#!/usr/bin/env python3
"""VO Studio: private localhost browser interface for ElevenLabs speech + forced alignment.
Python 3.9+, standard library only. API keys are kept only in RAM for the request.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / 'static'
OUTPUT_DIR = APP_DIR / 'outputs'
API_ROOT = 'https://api.elevenlabs.io'
PREFERRED_PORT = 8765
MAX_REQUEST_BYTES = 120_000
OUTPUT_DIR.mkdir(exist_ok=True)
JOBS: dict[str, dict] = {}
JOB_LOCK = threading.RLock()
GENERATE_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def redact_error(message: str, key: str = '') -> str:
    """Keep credentials and local filesystem locations out of stored errors."""
    for value in sorted({key, str(APP_DIR), str(OUTPUT_DIR), str(Path.home())},
                        key=len, reverse=True):
        if value:
            message = message.replace(value, '[redacted]')
    return message


def api_request(path: str, key: str, data: bytes | None = None,
                content_type: str | None = None, timeout: int = 300) -> bytes:
    headers = {'xi-api-key': key, 'User-Agent': 'VO-Studio-Local/1.0'}
    if content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(API_ROOT + path, data=data, headers=headers,
                                 method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode('utf-8', errors='replace')
        try:
            obj = json.loads(body)
            detail = obj.get('detail', obj)
            if isinstance(detail, dict):
                detail = detail.get('message', detail.get('detail', detail))
            if isinstance(detail, (dict, list)):
                detail = json.dumps(detail, ensure_ascii=False)
            message = str(detail)
        except (ValueError, TypeError):
            message = body or exc.reason
        raise ApiError(exc.code, redact_error(message, key)[:1100]) from None
    except urllib.error.URLError as exc:
        raise ApiError(503, 'Cannot reach ElevenLabs. Check your internet connection.') from exc
    except TimeoutError as exc:
        raise ApiError(504, 'ElevenLabs request timed out. Try again.') from exc


def slugify(value: str, default: str = 'voiceover') -> str:
    value = re.sub(r'[^a-z0-9]+', '-', value.lower().strip()).strip('-')
    return value[:48] or default


def validate_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ApiError(400, 'Invalid request.')
    key = str(data.get('api_key') or '').strip()
    voice_id = str(data.get('voice_id') or '').strip()
    voice_name = str(data.get('voice_name') or '').strip()[:100]
    name = str(data.get('name') or 'Untitled voiceover').strip()[:100]
    episode_id = str(data.get('episode_id') or '').strip()[:60]
    script = str(data.get('text') or '').strip()
    model_id = str(data.get('model_id') or 'eleven_multilingual_v2').strip()
    sample_rate = data.get('sample_rate', 44100)
    if not key or len(key) > 300:
        raise ApiError(400, 'Enter a valid ElevenLabs API key.')
    if not re.fullmatch(r'[\w-]{8,100}', voice_id):
        raise ApiError(400, 'Voice ID is required (8–100 letters, digits, underscores or hyphens).')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{4,100}', model_id):
        raise ApiError(400, 'Enter a valid model ID.')
    if not script:
        raise ApiError(400, 'Paste your voiceover script first.')
    if len(script) > 10000:
        raise ApiError(400, 'Maximum 10,000 characters per generation. Split longer scripts.')
    if type(sample_rate) is not int or sample_rate not in (44100, 24000):
        raise ApiError(400, 'Choose 44.1 kHz or 24 kHz audio.')
    raw = data.get('settings') or {}
    if not isinstance(raw, dict):
        raise ApiError(400, 'Invalid voice settings.')
    settings = {}
    for field, default, low, high in [
        ('stability', .5, 0, 1),
        ('similarity_boost', .75, 0, 1),
        ('style', 0, 0, 1),
        ('speed', 1, .7, 1.2),
    ]:
        try:
            v = float(raw.get(field, default))
        except (TypeError, ValueError):
            raise ApiError(400, f'{field} must be a number.')
        if not (low <= v <= high):
            raise ApiError(400, f'{field} must be between {low} and {high}.')
        settings[field] = round(v, 3)
    settings['use_speaker_boost'] = bool(raw.get('use_speaker_boost', True))
    return {
        'api_key': key, 'name': name, 'episode_id': episode_id, 'voice_name': voice_name,
        'voice_id': voice_id, 'text': script, 'model_id': model_id,
        'sample_rate': sample_rate, 'settings': settings,
    }


def create_wav(raw_pcm: bytes, target: Path, sample_rate: int) -> float:
    if not raw_pcm or len(raw_pcm) % 2:
        raise ApiError(502, 'ElevenLabs returned empty or invalid 16-bit PCM audio.')
    with wave.open(str(target), 'wb') as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(raw_pcm)
    return len(raw_pcm) / (2 * sample_rate)


def forced_alignment(key: str, wav_path: Path, script: str) -> dict:
    boundary = '----VOStudio' + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="text"\r\n\r\n'.encode(),
        script.encode('utf-8'),
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
        wav_path.read_bytes(),
        f'\r\n--{boundary}--\r\n'.encode(),
    ]
    result = api_request('/v1/forced-alignment', key, b''.join(parts),
                         f'multipart/form-data; boundary={boundary}', timeout=600)
    try:
        parsed = json.loads(result)
    except ValueError:
        raise ApiError(502, 'ElevenLabs returned an invalid alignment response.')
    if not isinstance(parsed.get('words'), list):
        raise ApiError(502, 'Alignment response did not contain word timestamps.')
    return parsed


def update_job(job_id: str, **updates):
    with JOB_LOCK:
        JOBS[job_id].update(updates)


def public_job(job: dict) -> dict:
    """Intentionally exclude the API key, transcript and filesystem paths."""
    return {key: job.get(key) for key in (
        'id', 'name', 'episode_id', 'voice_name', 'voice_id', 'model_id',
        'sample_rate', 'created_at', 'step', 'status', 'error', 'duration',
        'word_count', 'has_wav', 'has_alignment',
    )}


def persist_job(job_id: str):
    with JOB_LOCK:
        item = dict(JOBS[job_id])
    directory = OUTPUT_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)
    metadata = public_job(item)
    (directory / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def run_generation(job_id: str, config: dict):
    try:
        update_job(job_id, step='Generating audio', status='working')
        slug = slugify(config['name'])
        episode = slugify(config['episode_id'], default='') if config['episode_id'] else ''
        stem = '_'.join(x for x in [slug, episode] if x)
        target_dir = OUTPUT_DIR / job_id
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / 'transcript.txt').write_text(config['text'] + '\n', encoding='utf8')
        (target_dir / 'metadata_internal.json').write_text(json.dumps({
            'filename_stem': stem, 'name': config['name'], 'episode_id': config['episode_id'],
            'voice_name': config['voice_name'], 'voice_id': config['voice_id'],
            'model_id': config['model_id'], 'settings': config['settings'],
            'sample_rate': config['sample_rate']
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        request_body = json.dumps({
            'text': config['text'], 'model_id': config['model_id'],
            'voice_settings': config['settings']
        }, ensure_ascii=False).encode('utf8')
        pcm = api_request(
            '/v1/text-to-speech/' + urllib.parse.quote(config['voice_id'], safe='') +
            '?output_format=pcm_' + str(config['sample_rate']),
            config['api_key'], request_body, 'application/json', timeout=360)
        duration = create_wav(pcm, target_dir / (stem + '.wav'), config['sample_rate'])
        update_job(job_id, has_wav=True, duration=round(duration, 2), step='Aligning words')
        persist_job(job_id)
        aligned = forced_alignment(config['api_key'], target_dir / (stem + '.wav'), config['text'])
        (target_dir / (stem + '_alignment.json')).write_text(
            json.dumps(aligned, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        update_job(job_id, has_alignment=True, word_count=len(aligned['words']),
                   step='Finished', status='complete', error='')
    except ApiError as exc:
        update_job(job_id, status='partial' if JOBS[job_id]['has_wav'] else 'failed',
                   step='Alignment failed' if JOBS[job_id]['has_wav'] else 'Generation failed',
                   error=f'ElevenLabs error {exc.status}: {redact_error(exc.message, config["api_key"])}')
    except Exception as exc:
        update_job(job_id, status='partial' if JOBS[job_id]['has_wav'] else 'failed',
                   step='Alignment failed' if JOBS[job_id]['has_wav'] else 'Generation failed',
                   error=f'Unexpected error: {type(exc).__name__}: {redact_error(str(exc), config["api_key"])[:250]}')
    finally:
        persist_job(job_id)
        GENERATE_LOCK.release()


def run_retry(job_id: str, key: str):
    try:
        target_dir = OUTPUT_DIR / job_id
        meta = json.loads((target_dir / 'metadata_internal.json').read_text('utf8'))
        wav_path = target_dir / (meta['filename_stem'] + '.wav')
        script = (target_dir / 'transcript.txt').read_text('utf8').rstrip('\n')
        update_job(job_id, status='working', step='Aligning words', error='')
        aligned = forced_alignment(key, wav_path, script)
        (target_dir / (meta['filename_stem'] + '_alignment.json')).write_text(
            json.dumps(aligned, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        update_job(job_id, status='complete', step='Finished', has_alignment=True,
                   word_count=len(aligned['words']), error='')
    except ApiError as exc:
        update_job(job_id, status='partial', step='Alignment failed',
                   error=f'ElevenLabs error {exc.status}: {redact_error(exc.message, key)}')
    except Exception as exc:
        update_job(job_id, status='partial', step='Alignment failed',
                   error=f'Unexpected error: {type(exc).__name__}: {redact_error(str(exc), key)[:250]}')
    finally:
        persist_job(job_id)
        GENERATE_LOCK.release()


def load_history() -> list[dict]:
    items = []
    for file in OUTPUT_DIR.glob('*/metadata.json'):
        try:
            data = json.loads(file.read_text('utf8'))
            if re.fullmatch(r'[a-f0-9]{32}', data.get('id', '')):
                items.append(data)
        except (ValueError, OSError):
            continue
    return sorted(items, key=lambda x: x.get('created_at', ''), reverse=True)[:35]


class Handler(BaseHTTPRequestHandler):
    server_version = 'VOStudio/1.0'

    def log_message(self, fmt, *args):
        # Never log POST payloads, API keys or scripts.
        if not self.path.startswith('/api/jobs'):
            super().log_message(fmt, *args)

    def _safe_request(self):
        host = self.headers.get('Host', '')
        if not re.fullmatch(r'(127\.0\.0\.1|localhost)(:\d+)?', host, re.I):
            return False
        if self.command == 'POST':
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://' + host, 'http://127.0.0.1:' + str(self.server.server_port),
                                         'http://localhost:' + str(self.server.server_port)):
                return False
        return True

    def _send(self, status, data: bytes, mime='application/json', filename=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'")
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False).encode('utf8'))

    def _body(self):
        if self.headers.get('Content-Type', '').split(';')[0].lower() != 'application/json':
            raise ApiError(415, 'Expected JSON request body.')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise ApiError(413, 'Request too large.')
            payload = json.loads(self.rfile.read(size).decode('utf8'))
            if not isinstance(payload, dict):
                raise ApiError(400, 'Invalid JSON object.')
            return payload
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'Invalid JSON request.')

    def do_GET(self):
        if not self._safe_request():
            return self._json({'error':'Forbidden'}, 403)
        path = urllib.parse.urlsplit(self.path).path
        if path in ('/', '/index.html'):
            return self._static('index.html', 'text/html; charset=utf-8')
        if path == '/style.css':
            return self._static('style.css', 'text/css; charset=utf-8')
        if path == '/app.js':
            return self._static('app.js', 'text/javascript; charset=utf-8')
        if path == '/api/health':
            return self._json({'ok':True})
        if path == '/api/history':
            return self._json({'items':load_history()})
        match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})', path)
        if match:
            job_id = match.group(1)
            with JOB_LOCK:
                obj = JOBS.get(job_id)
            if obj is None:
                f = OUTPUT_DIR / job_id / 'metadata.json'
                if f.is_file():
                    try:
                        obj = json.loads(f.read_text('utf8'))
                    except ValueError:
                        obj = None
            return self._json({'job': public_job(obj)}, 200) if obj else self._json({'error':'Not found'}, 404)
        match = re.fullmatch(r'/api/media/([a-f0-9]{32})/wav', path)
        if match:
            job_id = match.group(1)
            directory = OUTPUT_DIR / job_id
            meta_file = directory / 'metadata_internal.json'
            if not meta_file.is_file():
                return self._json({'error':'Not found'}, 404)
            try:
                meta = json.loads(meta_file.read_text('utf8'))
                wav = directory / (meta['filename_stem'] + '.wav')
                if not wav.is_file():
                    return self._json({'error':'Not found'}, 404)
                return self._send(200, wav.read_bytes(), 'audio/wav')
            except (ValueError, KeyError, OSError):
                return self._json({'error':'Not found'}, 404)
        match = re.fullmatch(r'/api/download/([a-f0-9]{32})/(wav|alignment|transcript)', path)
        if match:
            job_id, which = match.groups()
            directory = OUTPUT_DIR / job_id
            meta_file = directory / 'metadata_internal.json'
            if not meta_file.is_file():
                return self._json({'error':'Not found'}, 404)
            try:
                meta = json.loads(meta_file.read_text('utf8'))
            except ValueError:
                return self._json({'error':'Not found'}, 404)
            stem = meta['filename_stem']
            fname = ({'wav':stem+'.wav', 'alignment':stem+'_alignment.json',
                      'transcript':'transcript.txt'})[which]
            file = directory / fname
            if not file.is_file():
                return self._json({'error':'Not ready'}, 404)
            mime = {'wav':'audio/wav', 'alignment':'application/json; charset=utf-8',
                    'transcript':'text/plain; charset=utf-8'}[which]
            return self._send(200, file.read_bytes(), mime, fname)
        return self._json({'error':'Not found'}, 404)

    def _static(self, name, mime):
        target = STATIC_DIR / name
        try:
            self._send(200, target.read_bytes(), mime)
        except FileNotFoundError:
            self._json({'error':'Missing application file'}, 500)

    def do_POST(self):
        if not self._safe_request():
            return self._json({'error':'Forbidden'}, 403)
        try:
            request = self._body()
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/voices':
                key = str(request.get('api_key') or '').strip()
                if not key:
                    raise ApiError(400, 'Enter your API key first.')
                all_voices = []
                next_page = None
                for _ in range(4):
                    params = {'page_size': '100'}
                    if next_page:
                        params['next_page_token'] = next_page
                    result = json.loads(api_request('/v2/voices?' + urllib.parse.urlencode(params), key))
                    for v in result.get('voices', []):
                        if v.get('voice_id'):
                            all_voices.append({
                                'voice_id':v['voice_id'], 'name':v.get('name') or 'Unnamed',
                                'category':v.get('category') or '',
                            })
                    next_page = result.get('next_page_token') if result.get('has_more') else None
                    if not next_page:
                        break
                all_voices.sort(key=lambda v: v['name'].lower())
                return self._json({'voices':all_voices})
            if path == '/api/generate':
                config = validate_payload(request)
                if not GENERATE_LOCK.acquire(blocking=False):
                    raise ApiError(409, 'Another job is running. Finish that one before starting another.')
                job_id = uuid.uuid4().hex
                with JOB_LOCK:
                    JOBS[job_id] = {
                        'id':job_id, 'name':config['name'], 'episode_id':config['episode_id'],
                        'voice_name':config['voice_name'], 'voice_id':config['voice_id'],
                        'model_id':config['model_id'], 'sample_rate':config['sample_rate'],
                        'created_at':dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                        'step':'Queued', 'status':'working', 'error':'', 'duration':None,
                        'word_count':None, 'has_wav':False, 'has_alignment':False,
                    }
                threading.Thread(target=run_generation, args=(job_id, config), daemon=True).start()
                return self._json({'job_id':job_id}, 202)
            match = re.fullmatch(r'/api/retry/([a-f0-9]{32})', path)
            if match:
                key = str(request.get('api_key') or '').strip()
                if not key:
                    raise ApiError(400, 'Enter your API key first.')
                job_id = match.group(1)
                directory = OUTPUT_DIR / job_id
                if not ((directory / 'metadata_internal.json').is_file() and
                        (directory / 'transcript.txt').is_file()):
                    raise ApiError(404, 'Original voiceover files not found.')
                with JOB_LOCK:
                    item = JOBS.get(job_id)
                    if not item:
                        file = directory / 'metadata.json'
                        if file.is_file():
                            JOBS[job_id] = json.loads(file.read_text('utf8'))
                            item = JOBS[job_id]
                if not item or not item.get('has_wav'):
                    raise ApiError(400, 'Generate the WAV before retrying alignment.')
                if item.get('has_alignment'):
                    raise ApiError(400, 'Alignment already exists for this voiceover.')
                if not GENERATE_LOCK.acquire(blocking=False):
                    raise ApiError(409, 'Another job is running. Try again when it finishes.')
                threading.Thread(target=run_retry, args=(job_id,key), daemon=True).start()
                return self._json({'job_id':job_id},202)
            if path == '/api/open-folder':
                try:
                    if sys.platform == 'darwin':
                        subprocess.Popen(['open', str(OUTPUT_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    elif os.name == 'nt':
                        os.startfile(str(OUTPUT_DIR))
                    else:
                        subprocess.Popen(['xdg-open', str(OUTPUT_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    raise ApiError(500, f'Could not open folder. Find it at: {OUTPUT_DIR}')
                return self._json({'ok':True})
            raise ApiError(404, 'Not found.')
        except ApiError as exc:
            return self._json({'error':exc.message}, exc.status if 400 <= exc.status <= 599 else 500)
        except Exception as exc:
            return self._json({'error':f'Unexpected error: {type(exc).__name__}'}, 500)


def main():
    if sys.version_info < (3, 9):
        sys.exit('Python 3.9 or newer is required.')
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PREFERRED_PORT), Handler)
    except OSError:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    address = f'http://127.0.0.1:{server.server_port}'
    print('\n  VO STUDIO  •  ELEVENLABS')
    print('  ' + '─' * 40)
    print('  Browser:  ' + address)
    print('  Outputs:  ' + str(OUTPUT_DIR))
    print('  Private local server, listening only on this laptop.')
    print('  Close this window or press Ctrl+C to stop.\n')
    threading.Timer(0.65, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print('\nStopping VO Studio…')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
