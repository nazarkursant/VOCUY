"""Build After Effects scripts from saved recordings; Python standard library only."""
import filecmp
import json
import math
from pathlib import Path
import re
import wave
import recordings


FRAME_RATES = (23.976, 24, 25, 29.97, 30, 50, 59.94, 60)
TEMPLATE = Path(__file__).parent / 'templates' / 'after_effects.jsx'


def validate_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError('Invalid composition settings.')
    settings = {'width': raw.get('width', 1080), 'height': raw.get('height', 1920),
                'fps': raw.get('fps', 30), 'subtitles': raw.get('subtitles', 'words')}
    for dimension in ('width', 'height'):
        if type(settings[dimension]) is not int or not 16 <= settings[dimension] <= 8192:
            raise ValueError('Composition dimensions must be whole numbers from 16 to 8192.')
    if type(settings['fps']) not in (int, float) or settings['fps'] not in FRAME_RATES:
        raise ValueError('Choose a supported frame rate.')
    if settings['subtitles'] not in ('words', 'phrases', 'off'):
        raise ValueError('Choose Words, Phrases, or Off for subtitles.')
    return settings


def read_words(alignment, duration):
    words = alignment.get('words') if isinstance(alignment, dict) else None
    if not isinstance(words, list) or not words:
        raise ValueError('Word timings are missing. Retry alignment before exporting.')
    cleaned = []
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get('text'), str):
            raise ValueError('Word timings contain invalid text. Retry alignment before exporting.')
        text = word['text'].strip()
        # Forced alignment also returns timed whitespace between spoken words.
        # It has no subtitle/marker text; keep the surrounding word times intact.
        if not text:
            continue
        start, end = word.get('start'), word.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or start >= duration or end > duration + .1
                or (cleaned and start < cleaned[-1]['start'])):
            raise ValueError('Word timings are invalid for this audio. Retry alignment before exporting.')
        cleaned.append({'text': text, 'start': start, 'end': min(end, duration)})
    if not cleaned:
        raise ValueError('Word timings are missing. Retry alignment before exporting.')
    return cleaned


def subtitle_cues(words, style):
    """Group phrases at punctuation, pauses, or a readable length; never across clips."""
    if style == 'off':
        return []
    if style == 'words':
        return [dict(w) for w in words]
    cues, group = [], []
    for word in words:
        if group and (len(group) >= 6 or word['start'] - group[-1]['end'] >= .5
                      or len(' '.join(w['text'] for w in group)) + len(word['text']) + 1 > 42):
            cues.append({'text': ' '.join(w['text'] for w in group),
                         'start': group[0]['start'], 'end': group[-1]['end']})
            group = []
        group.append(word)
        if re.search(r'[.!?…。！？]["\'”’)]*$', word['text']):
            cues.append({'text': ' '.join(w['text'] for w in group),
                         'start': group[0]['start'], 'end': group[-1]['end']})
            group = []
    if group:
        cues.append({'text': ' '.join(w['text'] for w in group),
                     'start': group[0]['start'], 'end': group[-1]['end']})
    return cues


def frame_cues(cues, offset, duration, fps):
    """Quantize display timing only. Audio and markers retain sample/word precision."""
    framed = []
    first_frame = math.floor(offset * fps + 1e-8)
    last_frame = max(first_frame + 1, math.floor((offset + duration) * fps + 1e-8))
    for cue in cues:
        start = max(first_frame, min(last_frame - 1, math.floor((offset + cue['start']) * fps + 1e-8)))
        end = min(last_frame, max(start + 1, math.ceil((offset + cue['end']) * fps - 1e-8)))
        if framed and framed[-1]['start'] == start:
            framed[-1]['text'] += ' ' + cue['text']
            framed[-1]['end'] = max(framed[-1]['end'], end)
        else:
            framed.append({'start': start, 'end': end, 'text': cue['text']})
    for index, cue in enumerate(framed):
        if index + 1 < len(framed):
            cue['end'] = min(cue['end'], framed[index + 1]['start'])
    return [{'start': c['start'] / fps, 'end': c['end'] / fps, 'text': c['text']} for c in framed]


def load_recording(output_dir, job, destination):
    directory = recordings.directory_for(output_dir, job['id'])
    try:
        meta = json.loads((directory / 'metadata_internal.json').read_text('utf8'))
        stem = meta['filename_stem']
        if not isinstance(stem, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', stem):
            raise ValueError('Invalid saved filename.')
        source = directory / (stem + '.wav')
        with wave.open(str(source), 'rb') as audio:
            duration = audio.getnframes() / audio.getframerate()
        if duration <= 0:
            raise ValueError('Empty recording.')
        alignment = json.loads((directory / (stem + '_alignment.json')).read_text('utf8'))
    except (OSError, ValueError, KeyError, TypeError, wave.Error, EOFError):
        raise ValueError('Saved audio or word timings are missing or unreadable. Restore the files or retry the message.') from None
    words = read_words(alignment, duration)
    # Prefer the chosen output copy only if it is the exact saved take. Older multi-
    # voice sessions did not record external_wav, so also check their export filename.
    candidates = []
    if meta.get('external_wav'):
        candidates.append(Path(meta['external_wav']))
    if job.get('export_filename'):
        candidate = (destination / job['export_filename']).resolve()
        if destination.resolve() in candidate.parents:
            candidates.append(candidate)
    selected = source.resolve()
    for candidate in candidates:
        try:
            if candidate.is_file() and filecmp.cmp(source, candidate, shallow=False):
                selected = candidate.resolve()
                break
        except OSError:
            continue
    try:
        relative = selected.relative_to(destination.resolve()).as_posix()
    except ValueError:
        relative = None
    return {'id': job['id'], 'name': selected.name, 'path': str(selected),
            'relative_path': relative, 'duration': duration, 'words': words}


def build_manifest(name, mode, turns, output_dir, destination, settings):
    settings = validate_settings(settings)
    if mode not in ('single', 'multi', 'batch') or not turns:
        raise ValueError('Choose a generated voiceover, conversation, or batch.')
    assets, asset_indices, compositions = [], {}, {}
    for turn in turns:
        current = turn.get('job')
        previous = turn.get('previous_job')
        if current and current.get('status') == 'working':
            raise ValueError('Wait for generation and alignment to finish before exporting.')
        job = current if current and current.get('has_wav') else previous
        if not job or not job.get('has_wav'):
            raise ValueError(f'Message {turn.get("number", 1)} has no audio. Generate it before exporting.')
        if not job.get('has_alignment'):
            raise ValueError(f'Message {turn.get("number", 1)} needs word timings. Retry alignment before exporting.')
        if not isinstance(job.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', job['id']):
            raise ValueError('Invalid saved recording ID.')
        if job['id'] not in asset_indices:
            asset_indices[job['id']] = len(assets)
            assets.append(load_recording(output_dir, job, destination))
        index = asset_indices[job['id']]
        asset = assets[index]
        key = turn['video_id'] if mode == 'batch' else name
        comp = compositions.setdefault(key, {'name': key, 'duration': 0, 'clips': []})
        start = comp['duration']
        speaker = job.get('speaker') or turn.get('speaker', 1)
        voice = job.get('voice_name') or f'Voice {speaker}'
        label = f'{turn.get("number", 1):03d} · {voice}'
        if turn.get('segment_id'):
            label += ' · ' + turn['segment_id']
        comp['clips'].append({'asset': index, 'start': start, 'speaker': speaker, 'name': label,
                              'cues': frame_cues(subtitle_cues(asset['words'], settings['subtitles']),
                                                 start, asset['duration'], settings['fps'])})
        comp['duration'] += asset['duration']
        if comp['duration'] > 10800:
            raise ValueError('An After Effects composition cannot exceed three hours. Split this sequence.')
    for comp in compositions.values():
        comp['duration'] = math.ceil(comp['duration'] * settings['fps'] - 1e-8) / settings['fps']
        if comp['duration'] > 10800:
            raise ValueError('An After Effects composition cannot exceed three hours. Split this sequence.')
    return {'name': name, 'mode': mode, 'settings': settings,
            'assets': assets, 'compositions': list(compositions.values())}


def render_script(manifest):
    # JSON is a literal, never evaluated as code. ASCII escaping also handles the
    # Unicode line separators rejected by the older ExtendScript parser.
    payload = json.dumps(manifest, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    return TEMPLATE.read_text('utf8').replace('/*__VOCUY_MANIFEST__*/', payload)
