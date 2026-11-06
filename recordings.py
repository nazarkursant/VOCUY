"""Readable local recording folders with stable job IDs kept in metadata."""
import json
from pathlib import Path
import re

import production


def folder_name(value, default):
    """Keep human names (including Unicode) portable across macOS and Windows."""
    name = re.sub(r'[^\w -]+', '-', str(value or ''), flags=re.UNICODE)
    name = re.sub(r'\s+', ' ', name).strip(' -_')[:48].rstrip(' -_') or default
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', name):
        name = '_' + name
    return name


def metadata_files(root):
    """Read both the old ID folders and the new project/episode/take layout."""
    root = Path(root)
    for pattern in ('*/metadata.json', 'recordings/*/*/*/Take */metadata.json'):
        for path in root.glob(pattern):
            if path.resolve().is_relative_to(root.resolve()):
                yield path


def find_directory(root, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('Invalid recording ID.')
    root = Path(root).resolve()
    legacy = root / job_id
    if legacy.exists():
        if not legacy.resolve().is_relative_to(root):
            raise OSError('The recording folder points outside local history.')
        if legacy.is_dir():
            return legacy
    for path in metadata_files(root):
        try:
            metadata = json.loads(path.read_text('utf8'))
            if isinstance(metadata, dict) and metadata.get('id') == job_id:
                return path.parent
        except (OSError, ValueError):
            continue
    return None


def directory_for(root, job_id):
    # A missing legacy path lets read callers report their existing not-found errors.
    return find_directory(root, job_id) or Path(root) / job_id


def write_metadata(directory, metadata):
    temporary = directory / '.metadata.tmp'
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    temporary.replace(directory / 'metadata.json')


def create_directory(root, job):
    project = folder_name(job.get('name'), 'Untitled voiceover')
    episode = 'Episode ' + folder_name(job['episode_id'], 'untitled') if job.get('episode_id') else 'No episode'
    voice = folder_name(job.get('voice_name'), 'Voice ' + str(job.get('speaker') or 1))
    if job.get('static_key'):
        clip = 'Shared - ' + folder_name(job['static_key'], 'clip')
    elif job.get('video_id'):
        clip = folder_name(job['video_id'], 'Video') + ' - ' + folder_name(job.get('segment_id'), 'clip')
    elif job.get('turn_number'):
        clip = f'Message {job["turn_number"]:03d} - Voice {job.get("speaker") or 1}'
    else:
        clip = 'Voiceover'
    parent = production.safe_directory(root, 'recordings', project, episode, clip + ' - ' + voice)
    for take in range(1, 10001):
        directory = parent / f'Take {take:02d}'
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            continue
    raise OSError('Too many takes for this recording. Choose a new project or episode name.')
