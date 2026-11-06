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
import conversation
import production
import after_effects
import recordings

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / 'static'
OUTPUT_DIR = APP_DIR / 'outputs'
API_ROOT = 'https://api.elevenlabs.io'
PREFERRED_PORT = 8765
MAX_REQUEST_BYTES = 6_000_000
OUTPUT_DIR.mkdir(exist_ok=True)
JOBS: dict[str, dict] = {}
JOB_LOCK = threading.RLock()
GENERATE_LOCK = threading.Lock()
BATCHES: dict[str, dict] = {}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def redact_error(message: str, key: str = '') -> str:
    """Keep credentials and local filesystem locations out of stored errors."""
    for value in sorted({key, str(APP_DIR), str(OUTPUT_DIR), str(Path.home()),
                         *(str(p) for p in conversation.DESTINATIONS.values())},
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
        'word_count', 'has_wav', 'has_alignment', 'export_filename', 'export_error',
        'turn_number', 'speaker', 'video_id', 'segment_id',
    )}


def job_directory(job_id: str, create=False):
    with JOB_LOCK:
        directory = recordings.find_directory(OUTPUT_DIR, job_id)
        if directory is None and create:
            directory = recordings.create_directory(OUTPUT_DIR, JOBS[job_id])
            # Write the ID before returning so subsequent lookups find this take.
            metadata = public_job(JOBS[job_id])
            recordings.write_metadata(directory, metadata)
        return directory or OUTPUT_DIR / job_id


def persist_job(job_id: str):
    with JOB_LOCK:
        directory = job_directory(job_id, create=True)
        recordings.write_metadata(directory, public_job(JOBS[job_id]))


def export_alignment(job_id, aligned):
    meta = json.loads((job_directory(job_id) / 'metadata_internal.json').read_text('utf8'))
    if meta.get('external_wav') and meta.get('export_alignment', True):
        try:
            production.write_alignment(Path(meta['external_wav']), aligned, meta.get('csv_mode', False))
        except OSError:
            update_job(job_id, export_error='Audio is saved. Timing files could not be saved to the chosen folder; they remain available in local history.')


def run_generation(job_id: str, config: dict, release_lock=True):
    try:
        update_job(job_id, step='Generating audio', status='working')
        slug = slugify(config['name'])
        episode = slugify(config['episode_id'], default='') if config['episode_id'] else ''
        stem = '_'.join(x for x in [slug, episode] if x)
        if config.get('turn_number'):
            stem += f'_{config["turn_number"]:03d}_voice_{config["speaker"]}'
        if config.get('video_id'):
            stem = f'{config["video_id"]}_{config["segment_id"]}'
        if config.get('static_key'):
            stem = 'shared_' + config['static_key']
        target_dir = job_directory(job_id, create=True)
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
        if config.get('destination'):
            try:
                csv_mode = bool(config.get('video_id'))
                if csv_mode:
                    exported, attempt = production.export_candidate(target_dir / (stem + '.wav'), config['destination'], config)
                else:
                    exported = conversation.export_wav(target_dir / (stem + '.wav'), config['destination'], stem)
                update_job(job_id, export_filename=str(exported.relative_to(config['destination'])))
                meta_path = target_dir / 'metadata_internal.json'
                meta = json.loads(meta_path.read_text('utf8'))
                meta.update(external_wav=str(exported), csv_mode=csv_mode,
                            export_alignment=bool(csv_mode or config.get('export_alignment')))
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
                if csv_mode:
                    production.write_candidate_details(exported, config['destination'], config, job_id, attempt)
            except OSError:
                update_job(job_id, export_error='Audio is saved in local history, but the destination folder could not be written. Download the WAV or choose another folder.')
        persist_job(job_id)
        aligned = forced_alignment(config['api_key'], target_dir / (stem + '.wav'), config['text'])
        (target_dir / (stem + '_alignment.json')).write_text(
            json.dumps(aligned, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        update_job(job_id, has_alignment=True, word_count=len(aligned['words']),
                   step='Finished', status='complete', error='')
        export_alignment(job_id, aligned)
    except ApiError as exc:
        update_job(job_id, status='partial' if JOBS[job_id]['has_wav'] else 'failed',
                   step='Alignment failed' if JOBS[job_id]['has_wav'] else 'Generation failed',
                   error=f'ElevenLabs error {exc.status}: {redact_error(exc.message, config["api_key"])}')
    except Exception as exc:
        update_job(job_id, status='partial' if JOBS[job_id]['has_wav'] else 'failed',
                   step='Alignment failed' if JOBS[job_id]['has_wav'] else 'Generation failed',
                   error=f'Unexpected error: {type(exc).__name__}: {redact_error(str(exc), config["api_key"])[:250]}')
    finally:
        try:
            persist_job(job_id)
        finally:
            if release_lock:
                GENERATE_LOCK.release()


def run_retry(job_id: str, key: str):
    try:
        target_dir = job_directory(job_id)
        meta = json.loads((target_dir / 'metadata_internal.json').read_text('utf8'))
        wav_path = target_dir / (meta['filename_stem'] + '.wav')
        script = (target_dir / 'transcript.txt').read_text('utf8').rstrip('\n')
        update_job(job_id, status='working', step='Aligning words', error='')
        aligned = forced_alignment(key, wav_path, script)
        (target_dir / (meta['filename_stem'] + '_alignment.json')).write_text(
            json.dumps(aligned, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        update_job(job_id, status='complete', step='Finished', has_alignment=True,
                   word_count=len(aligned['words']), error='')
        export_alignment(job_id, aligned)
    except ApiError as exc:
        update_job(job_id, status='partial', step='Alignment failed',
                   error=f'ElevenLabs error {exc.status}: {redact_error(exc.message, key)}')
    except Exception as exc:
        update_job(job_id, status='partial', step='Alignment failed',
                   error=f'Unexpected error: {type(exc).__name__}: {redact_error(str(exc), key)[:250]}')
    finally:
        try:
            persist_job(job_id)
        finally:
            GENERATE_LOCK.release()


def load_history() -> list[dict]:
    items = []
    for file in recordings.metadata_files(OUTPUT_DIR):
        try:
            data = json.loads(file.read_text('utf8'))
            if isinstance(data, dict) and isinstance(data.get('id'), str) and re.fullmatch(r'[a-f0-9]{32}', data['id']):
                items.append(data)
        except (ValueError, OSError):
            continue
    return sorted(items, key=lambda x: x.get('created_at', ''), reverse=True)[:35]


def create_job(config):
    job_id = uuid.uuid4().hex
    with JOB_LOCK:
        JOBS[job_id] = {
            'id': job_id, **{k: config[k] for k in (
                'name', 'episode_id', 'voice_name', 'voice_id', 'model_id', 'sample_rate')},
            'created_at': dt.datetime.now().astimezone().isoformat(timespec='microseconds'),
            'step': 'Queued', 'status': 'working', 'error': '', 'duration': None,
            'word_count': None, 'has_wav': False, 'has_alignment': False,
            'turn_number': config.get('turn_number'), 'speaker': config.get('speaker'),
            'video_id': config.get('video_id'), 'segment_id': config.get('segment_id'),
            'static_key': config.get('static_key'),
            'export_filename': '', 'export_error': '',
        }
    persist_job(job_id)
    return job_id


def read_job(job_id):
    with JOB_LOCK:
        if job_id in JOBS:
            return dict(JOBS[job_id])
    try:
        job = json.loads((job_directory(job_id) / 'metadata.json').read_text('utf8'))
    except (OSError, ValueError):
        return None
    if job.get('status') == 'working':
        job.update(status='partial' if job.get('has_wav') else 'failed',
                   step='Interrupted', error='The app stopped before this message finished. Retry this message.')
    return job


def persist_batch(batch):
    directory = OUTPUT_DIR / 'conversations'
    directory.mkdir(exist_ok=True)
    target = directory / (batch['id'] + '.json')
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    temporary.replace(target)


def read_batch(batch_id):
    with JOB_LOCK:
        batch = BATCHES.get(batch_id)
        if batch is None:
            try:
                batch = json.loads((OUTPUT_DIR / 'conversations' / (batch_id + '.json')).read_text('utf8'))
            except (OSError, ValueError):
                raise ApiError(404, 'Conversation not found.') from None
            BATCHES[batch_id] = batch
        return {**batch, 'turns': [dict(turn) for turn in batch['turns']]}


def public_batch(batch):
    turns = []
    for turn in batch['turns']:
        job = read_job(turn['job_id'])
        previous = read_job(turn['previous_job_id']) if turn.get('previous_job_id') else None
        turns.append({**turn, 'job': public_job(job) if job else None,
                      'previous_job': public_job(previous) if previous else None})
    return {**batch, 'turns': turns,
            'working': any(t['job'] and t['job']['status'] == 'working' for t in turns)}


def run_batch(jobs):
    try:
        for job_id, config in jobs:
            run_generation(job_id, config, release_lock=False)
    finally:
        # A filesystem error must not leave the remaining messages stuck in Queued.
        for job_id, _ in jobs:
            if JOBS[job_id]['status'] == 'working':
                update_job(job_id, status='failed', step='Interrupted',
                           error='The batch stopped. Retry this message.')
                try:
                    persist_job(job_id)
                except OSError:
                    pass
        GENERATE_LOCK.release()


def start_batch(request, batch_id=None):
    try:
        destination = conversation.destination_for(request.get('destination_token'))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    profiles = request.get('profiles')
    if not isinstance(profiles, list) or len(profiles) != 3 or any(not isinstance(p, dict) for p in profiles):
        raise ApiError(400, 'Provide the three voice profiles.')
    batch = read_batch(batch_id) if batch_id else None
    mode = batch.get('mode', 'multi') if batch else request.get('mode', 'multi')
    if mode not in ('multi', 'batch'):
        raise ApiError(400, 'Choose Multi voice or Batch mode.')
    if batch:
        number = request.get('number')
        if type(number) is not int or not 1 <= number <= len(batch['turns']):
            raise ApiError(400, 'Choose a valid message to regenerate.')
        old = batch['turns'][number - 1]
        turns = [{'number': number, 'speaker': request.get('speaker'), 'text': request.get('text')}]
        if mode == 'batch':
            affected = ([t for t in batch['turns'] if t.get('static_key') == old['static_key']]
                        if old.get('static_key') else [old])
            turns = [{**{key: t[key] for key in ('number', 'video_id', 'segment_id', 'script_revision')},
                      'speaker': 1, 'text': request.get('text'),
                      **({'static_key': t['static_key']} if t.get('static_key') else {})} for t in affected]
    else:
        turns = request.get('turns')
        if mode == 'batch':
            try:
                turns = production.validate_clips(turns)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        else:
            if not isinstance(turns, list) or not 1 <= len(turns) <= 200:
                raise ApiError(400, 'Prepare between 1 and 200 messages first.')
            if any(not isinstance(t, dict) for t in turns):
                raise ApiError(400, 'Invalid conversation messages.')
            turns = [{'text': t.get('text'), 'speaker': t.get('speaker'), 'number': i}
                     for i, t in enumerate(turns, 1)]
    limit = production.MAX_CHARACTERS if mode == 'batch' else 10000
    if any(not isinstance(t.get('text'), str) for t in turns):
        raise ApiError(400, 'Every message must contain speech text.')
    total = sum(len(t['text']) for t in turns)
    if batch:
        replaced = {t['number'] for t in turns}
        total += sum(len(t['text']) for t in batch['turns'] if t['number'] not in replaced)
    if total > limit:
        raise ApiError(400, f'Keep this {mode} within {limit:,} spoken characters.')
    configs = []
    for turn in turns:
        speaker = turn.get('speaker')
        if type(speaker) is not int or speaker not in (1, 2, 3):
            raise ApiError(400, 'Each message must use Voice 1, Voice 2, or Voice 3.')
        try:
            config = validate_payload({**profiles[speaker - 1], 'api_key': request.get('api_key'),
                                       'name': batch['name'] if batch else request.get('name'),
                                       'episode_id': batch['episode_id'] if batch else request.get('episode_id'),
                                       'text': turn['text']})
        except ApiError as exc:
            raise ApiError(exc.status, f'Voice {speaker}, message {turn["number"]}: {exc.message}') from None
        if mode == 'batch':
            config.update({key: turn[key] for key in ('video_id', 'segment_id', 'script_revision')})
            if turn.get('static_key'):
                config.update(static_key=turn['static_key'],
                              used_by=[t['video_id'] for t in turns if t.get('static_key') == turn['static_key']])
        configs.append({**config, 'speaker': speaker, 'turn_number': turn['number'], 'destination': destination})
    if not GENERATE_LOCK.acquire(blocking=False):
        raise ApiError(409, 'Another job is running. Wait for it to finish.')
    try:
        if batch is None:
            batch = {'id': uuid.uuid4().hex, 'name': configs[0]['name'], 'mode': mode,
                     'episode_id': configs[0]['episode_id'], 'turns': []}
        jobs, shared_jobs = [], {}
        for turn, config in zip(turns, configs):
            static_key = turn.get('static_key')
            job_id = shared_jobs.get(static_key) if static_key else None
            if job_id is None:
                job_id = create_job(config)
                jobs.append((job_id, config))
                if static_key:
                    shared_jobs[static_key] = job_id
            item = {**turn, 'text': config['text'], 'job_id': job_id}
            if batch_id:
                old = batch['turns'][turn['number'] - 1]
                old_job = read_job(old['job_id'])
                item['previous_job_id'] = old['job_id'] if old_job and old_job.get('has_wav') else old.get('previous_job_id')
                batch['turns'][turn['number'] - 1] = item
            else:
                batch['turns'].append(item)
        persist_batch(batch)
        with JOB_LOCK:
            BATCHES[batch['id']] = batch
        threading.Thread(target=run_batch, args=(jobs,), daemon=True).start()
    except Exception:
        GENERATE_LOCK.release()
        raise
    return batch['id']


def export_after_effects(request):
    destination = conversation.destination_for(request.get('destination_token'))
    settings = after_effects.validate_settings(request.get('settings', {}))
    job_id, batch_id = request.get('job_id'), request.get('batch_id')
    identifier = job_id or batch_id
    if bool(job_id) == bool(batch_id) or not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ApiError(400, 'Choose one generated voiceover, conversation, or batch.')
    # Freeze the active takes while reading files and saving the export.
    if not GENERATE_LOCK.acquire(blocking=False):
        raise ApiError(409, 'Wait for generation and alignment to finish before exporting.')
    try:
        if job_id:
            job = read_job(job_id)
            if not job:
                raise ApiError(404, 'Voiceover not found.')
            name = job.get('name') or 'Voiceover'
            episode = job.get('episode_id') or ''
            mode = 'single'
            turns = [{'number': 1, 'speaker': 1, 'job': job}]
        else:
            batch = public_batch(read_batch(batch_id))
            name, episode = batch['name'], batch.get('episode_id') or ''
            mode, turns = batch.get('mode', 'multi'), batch['turns']
        title = name + (' · ' + episode if episode else '')
        manifest = after_effects.build_manifest(title, mode, turns, OUTPUT_DIR, destination, settings)
        script = after_effects.render_script(manifest)
        export_id = uuid.uuid4().hex
        cache = production.safe_directory(OUTPUT_DIR, 'after_effects') / (export_id + '.jsx')
        cache.write_text(script, encoding='utf8')
        stem = slugify(name) + ('_' + slugify(episode) if episode else '') + '_after_effects'
        try:
            for take in range(1, 10001):
                filename = stem + (f'_{take:02d}' if take > 1 else '') + '.jsx'
                try:
                    with (destination / filename).open('x', encoding='utf8') as out:
                        out.write(script)
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError('Too many exports.')
        except OSError:
            cache.unlink(missing_ok=True)
            raise ApiError(400, 'Could not save the script. Choose a writable destination folder and try again.') from None
        return {'filename': filename, 'compositions': len(manifest['compositions']),
                'recordings': len(manifest['assets']),
                'download_url': f'/api/after-effects/{export_id}/{filename}'}
    finally:
        GENERATE_LOCK.release()


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
        if path == '/playback.js':
            return self._static('playback.js', 'text/javascript; charset=utf-8')
        if path == '/api/health':
            return self._json({'ok':True})
        if path == '/api/history':
            return self._json({'items':load_history()})
        match = re.fullmatch(r'/api/after-effects/([a-f0-9]{32})/([a-z0-9_-]+\.jsx)', path)
        if match:
            try:
                script = (OUTPUT_DIR / 'after_effects' / (match.group(1) + '.jsx')).read_bytes()
            except OSError:
                return self._json({'error': 'Export not found.'}, 404)
            return self._send(200, script, 'application/octet-stream', match.group(2))
        match = re.fullmatch(r'/api/batches/([a-f0-9]{32})', path)
        if match:
            try:
                return self._json({'batch': public_batch(read_batch(match.group(1)))})
            except ApiError as exc:
                return self._json({'error': exc.message}, exc.status)
        match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})', path)
        if match:
            job_id = match.group(1)
            with JOB_LOCK:
                obj = JOBS.get(job_id)
            if obj is None:
                f = job_directory(job_id) / 'metadata.json'
                if f.is_file():
                    try:
                        obj = json.loads(f.read_text('utf8'))
                    except ValueError:
                        obj = None
            return self._json({'job': public_job(obj)}, 200) if obj else self._json({'error':'Not found'}, 404)
        match = re.fullmatch(r'/api/media/([a-f0-9]{32})/wav', path)
        if match:
            job_id = match.group(1)
            directory = job_directory(job_id)
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
            directory = job_directory(job_id)
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
            if path == '/api/after-effects':
                return self._json(export_after_effects(request))
            if path == '/api/inspect-csv':
                return self._json(production.inspect_csv(request.get('text')))
            if path == '/api/prepare-csv':
                turns = production.parse_csv(request.get('text'), request.get('strip_cake_hook') is True,
                                             request.get('static_columns'))
                return self._json({'turns': turns, 'videos': len({t['video_id'] for t in turns}),
                                   'recordings': production.recording_count(turns),
                                   'characters': sum(len(t['text']) for t in turns)})
            if path in ('/api/prepare', '/api/destination', '/api/choose-folder'):
                try:
                    if path == '/api/prepare':
                        return self._json({'turns': conversation.parse_script(request.get('text'))})
                    if path == '/api/destination':
                        return self._json({'destination': conversation.register_destination(request.get('path'))})
                    return self._json({'destination': conversation.choose_folder()})
                except (ValueError, OSError):
                    if path == '/api/prepare':
                        raise
                    raise ApiError(400, 'Could not select that folder. Choose an existing writable folder, or enter its path below.') from None
            if path == '/api/batches':
                return self._json({'batch_id': start_batch(request)}, 202)
            match = re.fullmatch(r'/api/batches/([a-f0-9]{32})/regenerate', path)
            if match:
                return self._json({'batch_id': start_batch(request, match.group(1))}, 202)
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
                config.update(destination=conversation.destination_for(request.get('destination_token')),
                              export_alignment=True)
                if not GENERATE_LOCK.acquire(blocking=False):
                    raise ApiError(409, 'Another job is running. Finish that one before starting another.')
                try:
                    job_id = create_job(config)
                    threading.Thread(target=run_generation, args=(job_id, config), daemon=True).start()
                except Exception:
                    GENERATE_LOCK.release()
                    raise
                return self._json({'job_id':job_id}, 202)
            match = re.fullmatch(r'/api/retry/([a-f0-9]{32})', path)
            if match:
                key = str(request.get('api_key') or '').strip()
                if not key:
                    raise ApiError(400, 'Enter your API key first.')
                job_id = match.group(1)
                directory = job_directory(job_id)
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
                folder = conversation.destination_for(request['destination_token']) if request.get('destination_token') else OUTPUT_DIR
                try:
                    if sys.platform == 'darwin':
                        subprocess.Popen(['open', str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    elif os.name == 'nt':
                        os.startfile(str(folder))
                    else:
                        subprocess.Popen(['xdg-open', str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    raise ApiError(500, f'Could not open folder. Find it at: {OUTPUT_DIR}')
                return self._json({'ok':True})
            raise ApiError(404, 'Not found.')
        except ApiError as exc:
            return self._json({'error':exc.message}, exc.status if 400 <= exc.status <= 599 else 500)
        except ValueError as exc:
            return self._json({'error': redact_error(str(exc))}, 400)
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
