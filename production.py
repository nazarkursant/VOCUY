"""CSV batch input and per-video candidate folders, inspired by the SNAGA pipeline."""
import csv
import io
import json
from pathlib import Path
import re
import shutil

SEGMENTS = [('topic', 'topic'), ('what', 'what_is_it'),
            ('usage', 'how_to_use_it'), ('cake', 'cake_explanation')]
MAX_CLIPS = 1000
MAX_CHARACTERS = 500_000


def safe_id(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError(f'{label} must use 1–80 letters, numbers, underscores or hyphens.')
    if value.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        raise ValueError(f'{label} is a reserved filename. Choose another ID.')
    return value


def validate_clips(clips):
    if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
        raise ValueError(f'Prepare between 1 and {MAX_CLIPS:,} clips.')
    seen, cleaned, shared = set(), [], {}
    for i, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            raise ValueError(f'Invalid clip {i}.')
        video = safe_id(clip.get('video_id'), 'Video ID')
        segment = safe_id(clip.get('segment_id'), 'Segment ID')
        identity = (video.casefold(), segment.casefold())
        if identity in seen:
            raise ValueError(f'Duplicate clip: {video}/{segment}. Each video and segment pair must be unique.')
        seen.add(identity)
        text = clip.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError(f'{video}/{segment}: speech must contain 1–10,000 characters.')
        item = {'number': i, 'speaker': 1, 'video_id': video, 'segment_id': segment,
                'text': text.strip(), 'script_revision': str(clip.get('script_revision') or 'r1')[:80]}
        static_key = clip.get('static_key')
        if static_key is not None:
            if static_key != segment:
                raise ValueError('A static column must use its segment ID as the shared recording key.')
            item['static_key'] = static_key
        signature = (static_key, item['text'] if static_key else None)
        if segment.casefold() in shared and shared[segment.casefold()] != signature:
            raise ValueError(f'Static column {segment} must have the same text and selection in every sequence.')
        shared[segment.casefold()] = signature
        cleaned.append(item)
    if sum(len(c['text']) for c in cleaned) > MAX_CHARACTERS:
        raise ValueError(f'Keep each batch within {MAX_CHARACTERS:,} spoken characters.')
    return cleaned


def read_csv(source, strip_cake_hook=False):
    """Read column choices before requiring speech in every dynamic cell."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError('Choose a CSV file or paste its contents first.')
    if len(source) > 1_000_000:
        raise ValueError('Use a CSV smaller than 1,000,000 characters.')
    try:
        reader = csv.DictReader(io.StringIO(source.lstrip('\ufeff'), newline=''), strict=True)
        fields = [f.strip() for f in (reader.fieldnames or [])]
        if len({f.casefold() for f in fields}) != len(fields) or any(not f for f in fields):
            raise ValueError('CSV column names must be unique.')
        reader.fieldnames = fields
        wide = {'video_id', *(col for _, col in SEGMENTS)}.issubset(fields)
        narrow = {'video_id', 'segment_id', 'text'}.issubset(fields)
        if 'video_id' not in fields:
            raise ValueError('CSV needs video_id plus speech columns, or video_id, segment_id, text.')
        columns = SEGMENTS if wide else [(f, f) for f in fields if f not in ('video_id', 'script_revision')]
        if not narrow:
            if not columns:
                raise ValueError('Add at least one speech column after video_id.')
            for segment, _ in columns:
                safe_id(segment, 'Speech column name')
        choices = {} if narrow else dict(columns)
        clips = []
        for row_index, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f'CSV row {row_index} has a different number of columns. Put commas and multiline text inside quotes.')
            row = {k: v.strip() for k, v in row.items()}
            if not any(row.values()):
                continue
            safe_id(row['video_id'], 'Video ID')
            segments = [(row['segment_id'], 'text')] if narrow else columns
            for segment, column in segments:
                safe_id(segment, 'Segment ID')
                choices.setdefault(segment, segment)
                text = row[column]
                if strip_cake_hook and segment == 'cake':
                    text = re.sub(r"^let[’']s explain it with cake\.\s*", '', text, count=1, flags=re.I)
                clips.append({'video_id': row['video_id'], 'segment_id': segment,
                              'text': text, 'script_revision': row.get('script_revision', 'r1')})
            if len(clips) > MAX_CLIPS:
                raise ValueError(f'Use at most {MAX_CLIPS:,} clips per batch.')
    except csv.Error:
        raise ValueError('Could not read the CSV. Check its quotes and column separators.') from None
    if not clips:
        raise ValueError('Add at least one data row to the CSV.')
    return clips, [{'id': key, 'label': label} for key, label in choices.items()]


def inspect_csv(source):
    clips, columns = read_csv(source)
    return {'columns': columns, 'videos': len({c['video_id'] for c in clips})}


def parse_csv(source, strip_cake_hook=False, static_columns=None):
    clips, columns = read_csv(source, strip_cake_hook)
    static_columns = [] if static_columns is None else static_columns
    choices = {c['id'] for c in columns}
    if not isinstance(static_columns, list) or any(not isinstance(c, str) or c not in choices for c in static_columns):
        raise ValueError('Choose static columns from this CSV.')
    for key in set(static_columns):
        copies = [c for c in clips if c['segment_id'] == key]
        texts = {c['text'] for c in copies if c['text']}
        if len(texts) != 1:
            raise ValueError(f'Static column {key} needs one shared text. Repeat the same text in every row, or leave the other cells blank.')
        text = texts.pop()
        for clip in copies:
            clip.update(text=text, static_key=key)
    return validate_clips(clips)


def recording_count(clips):
    return sum(not c.get('static_key') for c in clips) + len({c['static_key'] for c in clips if c.get('static_key')})


def safe_directory(root, *parts):
    root = Path(root).resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise OSError('The output subfolder points outside the selected destination.')
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_new(path, contents):
    try:
        with path.open('x', encoding='utf8', newline='') as f:
            f.write(contents)
    except FileExistsError:
        pass


def export_candidate(source, root, clip):
    video, segment = clip['video_id'], clip['segment_id']
    shared = clip.get('static_key')
    stem = shared or f'{video}_{segment}'
    for attempt in range(1, 10000):
        pass_name = f'pass_{attempt:02d}_' + ('initial' if attempt == 1 else 'retry')
        folder = (safe_directory(root, 'audio', 'shared', pass_name) if shared else
                  safe_directory(root, 'audio', 'candidates', pass_name, video))
        target = folder / (stem + '.wav')
        try:
            output = target.open('xb')
        except FileExistsError:
            continue
        try:
            with output, source.open('rb') as input_file:
                shutil.copyfileobj(input_file, output)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        return target, attempt
    raise OSError('Too many existing passes. Select a new destination.')


def write_candidate_details(target, root, config, job_id, attempt):
    script_dir = safe_directory(root, 'scripts', 'shared' if config.get('static_key') else config['video_id'])
    write_new(script_dir / (target.stem + '.txt'), config['text'] + '\n')
    # This copy follows the actual take if the script was edited for a retry.
    write_new(target.with_suffix('.txt'), config['text'] + '\n')
    record = {key: config[key] for key in ('video_id', 'segment_id', 'text', 'voice_id',
                                          'voice_name', 'model_id', 'sample_rate', 'settings', 'script_revision')}
    record.update(job_id=job_id, attempt=attempt, status='REVIEW_PENDING')
    if config.get('static_key'):
        record.update(static_key=config['static_key'], used_by=config['used_by'])
    write_new(target.with_suffix('.candidate.json'), json.dumps(record, ensure_ascii=False, indent=2) + '\n')


def write_alignment(target, aligned, csv_mode=False):
    suffix = '.alignment.json' if csv_mode else '_alignment.json'
    path = target.with_name(target.stem + suffix)
    write_new(path, json.dumps(aligned, ensure_ascii=False, indent=2) + '\n')
    if csv_mode:
        output = io.StringIO(newline='')
        writer = csv.writer(output, delimiter='\t', lineterminator='\n')
        writer.writerow(['word', 'start_sec', 'end_sec'])
        for word in aligned['words']:
            writer.writerow([word.get('text', word.get('word', '')), word.get('start', ''), word.get('end', '')])
        write_new(target.with_suffix('.words.tsv'), output.getvalue())
