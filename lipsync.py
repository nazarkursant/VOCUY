"""Lip sync: turn saved character timings into mouth-pose keyframes for After Effects.

A mouth folder holds still images plus an optional mouth_map.csv table that says
which images show each mouth shape. Words are looked up in the CMU Pronouncing
Dictionary (data/cmudict.txt.gz); other words, usually names and non-English
words, are read letter by letter with continental vowels. This is a visual
approximation, not phoneme recognition. Python standard library only.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
from pathlib import Path
import re
import unicodedata
import uuid

import after_effects
import recordings


TEMPLATE = Path(__file__).parent / 'templates' / 'lip_sync.jsx'
DICTIONARY_FILE = Path(__file__).parent / 'data' / 'cmudict.txt.gz'
MAP_NAME = 'mouth_map.csv'
IMAGE_TYPES = ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.psd')
MAX_POSES = 200
REST_GAP = .15      # Seconds of silence before the mouth closes between words.
WIDE_VOWEL = .2     # An A held this long uses the "wide" shape.
LEAD_FRAMES = 1     # Animators show a mouth shape slightly before its sound.

# shape: (sounds, default poses for the 20-pose sample set)
SHAPES = {
    'rest': ('Silence and pauses between words', '01 20'),
    'mbp': ('M, B, P', '16 01 17'),
    'fv': ('F, V, PH', '14'),
    'th': ('TH', '13'),
    'l': ('L', '12 13'),
    'ee': ('EE, EA, IE, Y', '05 10 06'),
    'eh': ('E, I (short vowels), AI, AY', '15 02'),
    'ah': ('A', '03 19 11'),
    'wide': ('A held 0.2 s or longer', '04 03'),
    'uh': ('U as in but', '19 02'),
    'oh': ('O, OW, OA, AW, AU, OI', '07'),
    'oo': ('OO, OU, EW, U as in you, W, WH, QU', '09 08'),
    'r': ('R', '09 02'),
    'sz': ('S, Z, X, soft C', '06 10'),
    'ch': ('CH, SH, J, soft G', '10 18'),
    'tdn': ('T, D, N, K, G, H, NG and other letters', '02 15 11'),
}
# A table may leave shapes out; each borrows from a similar shape, ending at rest.
FALLBACK = {'wide': 'ah', 'uh': 'ah', 'eh': 'ah', 'ee': 'eh', 'oh': 'ah', 'oo': 'oh', 'r': 'oo',
            'th': 'l', 'l': 'tdn', 'tdn': 'eh', 'sz': 'tdn', 'ch': 'sz', 'fv': 'mbp', 'mbp': 'rest',
            'ah': 'rest'}
# When two shapes land too close together, keep the one viewers notice missing.
PRIORITY = {'rest': 0, 'mbp': 3, 'fv': 2, 'wide': 2, 'ah': 2, 'oh': 2, 'oo': 2, 'ee': 2}

# ARPAbet phonemes (stress digits removed). Diphthongs move through two shapes.
PHONEMES = {
    'AA': 'ah', 'AE': 'ah', 'AH': 'uh', 'AO': 'oh', 'EH': 'eh', 'ER': 'r', 'IH': 'eh',
    'IY': 'ee', 'UH': 'oo', 'UW': 'oo',
    'AY': ('ah', 'ee'), 'AW': ('ah', 'oo'), 'EY': ('eh', 'ee'), 'OW': ('oh', 'oo'), 'OY': ('oh', 'ee'),
    'B': 'mbp', 'M': 'mbp', 'P': 'mbp', 'F': 'fv', 'V': 'fv', 'TH': 'th', 'DH': 'th', 'L': 'l',
    'R': 'r', 'W': 'oo', 'Y': 'ee', 'S': 'sz', 'Z': 'sz', 'SH': 'ch', 'ZH': 'ch', 'CH': 'ch',
    'JH': 'ch', 'T': 'tdn', 'D': 'tdn', 'N': 'tdn', 'K': 'tdn', 'G': 'tdn', 'NG': 'tdn', 'HH': 'tdn',
}
# Relative phoneme lengths used to spread a word's time across its sounds.
WEIGHTS = {'vowel': 1.6, 'P': .7, 'B': .7, 'T': .7, 'D': .7, 'K': .7, 'G': .7, 'HH': .5}
_dictionary = None

SILENT = 'silent'  # Letters that are written but not voiced extend their neighbour.
DIGRAPHS = {
    'th': 'th', 'sh': 'ch', 'ch': 'ch', 'ph': 'fv', 'wh': 'oo', 'qu': 'oo', 'ck': 'tdn',
    'ng': 'tdn', 'gh': SILENT, 'ee': 'ee', 'ea': 'ee', 'ie': 'ee', 'ey': 'ee', 'oo': 'oo',
    'ou': 'oo', 'ew': 'oo', 'ue': 'oo', 'ow': 'oh', 'oa': 'oh', 'oi': 'oh', 'oy': 'oh',
    'aw': 'oh', 'au': 'oh', 'ai': 'eh', 'ay': 'eh', 'ei': 'eh', 'zh': 'ch', 'kh': 'tdn', 'ts': 'sz',
}
LETTERS = {
    'a': 'ah', 'e': 'eh', 'i': 'ee', 'o': 'oh', 'u': 'oo', 'y': 'ee', 'w': 'oo',
    'b': 'mbp', 'm': 'mbp', 'p': 'mbp', 'f': 'fv', 'v': 'fv', 'l': 'l', 'r': 'r',
    's': 'sz', 'z': 'sz', 'x': 'sz', 'j': 'ch',
    # Cyrillic
    'а': 'ah', 'я': 'ah', 'е': 'eh', 'э': 'eh', 'є': 'eh', 'ы': 'eh', 'ё': 'oh', 'о': 'oh',
    'и': 'ee', 'і': 'ee', 'ї': 'ee', 'й': 'ee', 'у': 'oo', 'ю': 'oo',
    'м': 'mbp', 'б': 'mbp', 'п': 'mbp', 'ф': 'fv', 'в': 'fv', 'л': 'l', 'р': 'r',
    'с': 'sz', 'з': 'sz', 'ц': 'sz', 'ш': 'ch', 'щ': 'ch', 'ж': 'ch', 'ч': 'ch',
    'ь': SILENT, 'ъ': SILENT,
}
MOUTH_FOLDERS = {}


def normalize(char):
    """Lowercase and drop accents, keeping Cyrillic й/ё/ї distinct."""
    char = char.lower()
    if char in LETTERS:
        return char
    stripped = ''.join(c for c in unicodedata.normalize('NFD', char) if not unicodedata.combining(c))
    return stripped[:1] or char


def dictionary():
    """{word: [phonemes]} from the bundled CMU dictionary, loaded once; {} if missing."""
    global _dictionary
    if _dictionary is None:
        _dictionary = {}
        try:
            with gzip.open(DICTIONARY_FILE, 'rt', encoding='utf8') as lines:
                for line in lines:
                    if not line.startswith('#'):
                        word, _, phones = line.rstrip('\n').partition(' ')
                        _dictionary[word] = phones
        except (OSError, EOFError, UnicodeDecodeError):
            _dictionary = {}
    return _dictionary


def phoneme_shapes(word):
    """Dictionary pronunciation spread over the word's spoken time, or None if unknown."""
    letters = [(c, s, e) for c, s, e in word if c.isalnum() or c in "'’"]
    key = ''.join(c for c, _, _ in letters).lower().replace('’', "'").strip("'")
    phones = dictionary().get(key) if key else None
    if not phones:
        return None
    sounds = []
    for phone in phones.split():
        base = phone.rstrip('012')
        shape = PHONEMES.get(base)
        if shape is None:
            return None
        weight = WEIGHTS['vowel'] if phone[-1].isdigit() else WEIGHTS.get(base, 1)
        if isinstance(shape, tuple):
            sounds += [(shape[0], weight * .6), (shape[1], weight * .4)]
        else:
            sounds.append((shape, weight))
    start, end = letters[0][1], max(e for _, _, e in letters)
    total, elapsed, out = sum(w for _, w in sounds), 0, []
    for shape, weight in sounds:
        begin = start + (end - start) * elapsed / total
        elapsed += weight
        out.append([begin, start + (end - start) * elapsed / total, shape])
    return out


def letter_shapes(word):
    """[(char, start, end)] for one spoken word -> [[start, end, shape]].

    The shape is SILENT for unvoiced letters and None for punctuation, whose time
    is a pause rather than part of a sound.
    """
    letters = [(normalize(c), s, e) for c, s, e in word]
    text = ''.join(c for c, _, _ in letters)
    out, i = [], 0
    while i < len(letters):
        char, start, end = letters[i]
        pair = text[i:i + 2]
        if len(pair) == 2 and pair in DIGRAPHS and not (pair == 'gh' and i == 0):
            out.append([start, letters[i + 1][2], DIGRAPHS[pair]])
            i += 2
            continue
        if char in 'cg':
            soft = i + 1 < len(text) and text[i + 1] in 'eiy'
            shape = ('sz' if char == 'c' else 'ch') if soft else 'tdn'
        elif char in LETTERS:
            shape = LETTERS[char]
        elif unicodedata.category(char).startswith('L') or char.isdigit():
            shape = 'tdn' if char.isascii() else 'eh'
        else:
            shape = None
        out.append([start, end, shape])
        i += 1
    return out


def timed_words(alignment, duration):
    """Words as [(char, start, end)] from character timings, or spread over word timings."""
    characters = alignment.get('characters') if isinstance(alignment, dict) else None
    words, current = [], []
    if isinstance(characters, list) and characters:
        for item in characters:
            if not isinstance(item, dict) or not isinstance(item.get('text'), str):
                continue
            start, end = item.get('start'), item.get('end')
            if (type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end) or end < start):
                continue
            start, end = max(0, min(start, duration)), max(0, min(end, duration))
            for char in item['text']:
                if char.isspace():
                    if current:
                        words.append(current)
                    current = []
                else:
                    current.append((char, start, end))
        if current:
            words.append(current)
        if words:
            return words
    for word in after_effects.read_words(alignment, duration):
        text = word['text']
        step = (word['end'] - word['start']) / len(text)
        words.append([(c, word['start'] + n * step, word['start'] + (n + 1) * step)
                      for n, c in enumerate(text)])
    return words


def segments(words):
    """Merge letters into timed mouth shapes; silent letters extend their neighbours."""
    result = []
    for word in words:
        shaped = phoneme_shapes(word) or letter_shapes(word)
        pending = None
        for start, end, shape in shaped:
            if shape is None:
                continue
            if shape == SILENT:
                if result and result[-1][3] is word:
                    result[-1][1] = max(result[-1][1], end)
                elif pending is None:
                    pending = start
                continue
            if result and result[-1][3] is word and result[-1][2] == shape:
                result[-1][1] = max(result[-1][1], end)
                continue
            result.append([start if pending is None else pending, end, shape, word])
            pending = None
    for item in result:
        if item[2] == 'ah' and item[1] - item[0] >= WIDE_VOWEL:
            item[2] = 'wide'
    return [(s, e, shape) for s, e, shape, _ in result]


def speech_events(segment_list, offset):
    """Shapes with rest closures for pauses, as (seconds, shape) from the clip start."""
    events, previous_end = [], None
    for start, end, shape in segment_list:
        if previous_end is not None and start - previous_end >= REST_GAP:
            events.append((offset + previous_end, 'rest'))
        events.append((offset + start, shape))
        previous_end = end if previous_end is None else max(previous_end, end)
    if previous_end is not None:
        events.append((offset + previous_end, 'rest'))
    return events


def keyframes(events, fps, mapping):
    """Hold keys [(seconds, pose number)], at most one change per min_hold frames."""
    min_hold = max(1, round(fps / 12))
    keys = [[0, 'rest']]
    for seconds, shape in sorted(events, key=lambda e: e[0]):
        frame = max(0, math.floor(seconds * fps + 1e-6) - (LEAD_FRAMES if shape != 'rest' else 0))
        frame = max(frame, keys[-1][0])
        if shape == keys[-1][1]:
            continue
        if frame - keys[-1][0] < min_hold:
            if keys[-1][1] == 'rest' and frame > keys[-1][0]:
                keys.append([frame, shape])  # Speech starts on time, even after a brief rest.
            # Too soon to show both; keep the more visible shape at the earlier frame.
            elif PRIORITY.get(shape, 1) > PRIORITY.get(keys[-1][1], 1):
                keys[-1][1] = shape
                if len(keys) > 1 and keys[-2][1] == shape:
                    keys.pop()
            elif shape == 'rest' and keys[-1][1] != 'rest':
                keys.append([keys[-1][0] + min_hold, 'rest'])
            continue
        keys.append([frame, shape])
    poses, used, previous = [], {}, None
    for frame, shape in keys:
        candidates = mapping[shape]
        count = used.get(shape, 0)
        choice = None
        for step in range(len(candidates)):
            pose = candidates[(count + step) % len(candidates)]
            if pose != previous:
                choice = pose
                used[shape] = count + step + 1
                break
        if choice is None:
            continue  # Every option looks like the current pose.
        poses.append((frame / fps, choice))
        previous = choice
    return poses


def pose_files(folder):
    files = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_TYPES
                    and not p.name.startswith('.')), key=lambda p: p.name.lower())
    return files


def resolve_pose(token, files):
    """A table cell names an image by file name, name without extension, or its number."""
    lowered = token.lower()
    for index, path in enumerate(files):
        if lowered in (path.name.lower(), path.stem.lower()):
            return index + 1
    if token.isdigit():
        matches = [i for i, p in enumerate(files)
                   if (m := re.search(r'\d+', p.stem)) and int(m.group()) == int(token)]
        if len(matches) == 1:
            return matches[0] + 1
        if len(matches) > 1:
            raise ValueError(f'More than one mouth image is numbered {token}. Use file names in {MAP_NAME}.')
    raise ValueError(f'No mouth image matches "{token}".')


def read_map(folder, files):
    """{shape: [pose numbers]} from mouth_map.csv, or the sample set's defaults."""
    table = folder / MAP_NAME
    rows = {}
    if table.is_file():
        try:
            text = table.read_text('utf-8-sig')
        except (OSError, UnicodeDecodeError):
            raise ValueError(f'Could not read {MAP_NAME}. Save it as UTF-8 CSV.') from None
        reader = csv.DictReader(text.splitlines())
        fields = {(f or '').strip().lower(): f for f in reader.fieldnames or []}
        if 'shape' not in fields or 'poses' not in fields:
            raise ValueError(f'{MAP_NAME} needs "shape" and "poses" columns.')
        for row in reader:
            shape = (row.get(fields['shape']) or '').strip().lower()
            poses = (row.get(fields['poses']) or '').strip()
            if not shape and not poses:
                continue
            if shape not in SHAPES:
                raise ValueError(f'{MAP_NAME}: unknown shape "{shape}". Use: {", ".join(SHAPES)}.')
            if poses:
                rows[shape] = poses
        source = MAP_NAME
    else:
        rows = {shape: poses for shape, (_, poses) in SHAPES.items()}
        source = 'built-in'
    mapping = {}
    for shape, cell in rows.items():
        try:
            mapping[shape] = [resolve_pose(t, files) for t in re.split(r'[\s,;]+', cell) if t]
        except ValueError as exc:
            if source == 'built-in':
                raise ValueError(f'This folder has no {MAP_NAME}, and its images do not follow the '
                                 f'20-pose sample naming. Add a {MAP_NAME} table (copy the sample one).') from None
            raise ValueError(f'{MAP_NAME}, shape "{shape}": {exc}') from None
    if 'rest' not in mapping:
        raise ValueError(f'{MAP_NAME} needs a "rest" row: the mouth shown during silence.')
    for shape in SHAPES:
        fallback = shape
        while fallback not in mapping:
            fallback = FALLBACK[fallback]
        mapping[shape] = mapping[fallback]
    return mapping, source


def load_folder(path):
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError('Choose an existing mouth folder on this computer.')
    try:
        files = pose_files(folder)
    except OSError:
        raise ValueError('Could not read that mouth folder.') from None
    if not files:
        raise ValueError('That folder has no mouth images (PNG, JPG, TIFF or PSD).')
    if len(files) > MAX_POSES:
        raise ValueError(f'Use a mouth folder with at most {MAX_POSES} images.')
    mapping, source = read_map(folder, files)
    # Only images the table uses become poses; reference sheets in the folder are ignored.
    used = sorted({n for poses in mapping.values() for n in poses})
    renumber = {old: new for new, old in enumerate(used, 1)}
    mapping = {shape: [renumber[n] for n in poses] for shape, poses in mapping.items()}
    return folder, [files[n - 1] for n in used], mapping, source


def register_folder(path):
    if not isinstance(path, str) or not path.strip():
        raise ValueError('Choose a mouth folder first.')
    folder, files, _, source = load_folder(path)
    token = uuid.uuid4().hex
    MOUTH_FOLDERS[token] = folder
    return {'token': token, 'path': str(folder), 'name': folder.name, 'poses': len(files), 'table': source}


def folder_for(token):
    folder = MOUTH_FOLDERS.get(str(token))
    if folder is None:
        raise ValueError('Choose the mouth folder again before exporting lip sync.')
    return load_folder(folder)


def read_alignment(output_dir, job_id):
    directory = recordings.directory_for(output_dir, job_id)
    try:
        meta = json.loads((directory / 'metadata_internal.json').read_text('utf8'))
        return json.loads((directory / (meta['filename_stem'] + '_alignment.json')).read_text('utf8'))
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('Saved word timings are missing or unreadable. Retry alignment before exporting.') from None


def relative(path, destination):
    try:
        return path.resolve().relative_to(destination.resolve()).as_posix()
    except ValueError:
        return None


def build_manifest(manifest, output_dir, destination, mouths):
    """Add per-voice mouth keys to an export manifest built by after_effects."""
    if not isinstance(mouths, dict) or not mouths.get('1'):
        raise ValueError('Choose a mouth folder for Voice 1.')
    fps = manifest['settings']['fps']
    sets, set_index, speaker_set = [], {}, {}
    for speaker in ('1', '2', '3'):
        token = mouths.get(speaker) or mouths['1']
        folder, files, mapping, _ = folder_for(token)
        if folder not in set_index:
            set_index[folder] = len(sets)
            sets.append({'name': folder.name, 'mapping': mapping,
                         'poses': [{'name': p.name, 'path': str(p), 'relative_path': relative(p, destination)}
                                   for p in files]})
        speaker_set[int(speaker)] = set_index[folder]
    shapes = {}
    for asset in manifest['assets']:
        alignment = read_alignment(output_dir, asset['id'])
        shapes[asset['id']] = segments(timed_words(alignment, asset['duration']))
    compositions = []
    width, height = manifest['settings']['width'], manifest['settings']['height']
    for comp in manifest['compositions']:
        speakers = sorted({max(1, min(3, c['speaker'])) for c in comp['clips']})
        voices = []
        for n, speaker in enumerate(speakers):
            clips = [c for c in comp['clips'] if max(1, min(3, c['speaker'])) == speaker]
            events = []
            for clip in clips:
                events += speech_events(shapes[manifest['assets'][clip['asset']]['id']], clip['start'])
            mouth = speaker_set[speaker]
            label = clips[0]['name'].split(' · ')[1] if ' · ' in clips[0]['name'] else f'Voice {speaker}'
            voices.append({'speaker': speaker, 'label': label, 'mouth': mouth,
                           'position': [round(width * (n + 1) / (len(speakers) + 1), 2), height / 2],
                           'keys': keyframes(events, fps, sets[mouth]['mapping'])})
        compositions.append({'name': comp['name'], 'duration': comp['duration'], 'voices': voices,
                             'clips': [{'asset': c['asset'], 'start': c['start'], 'speaker': max(1, min(3, c['speaker'])),
                                        'name': c['name']} for c in comp['clips']]})
    return {'name': manifest['name'], 'settings': {'width': width, 'height': height, 'fps': fps},
            'mouths': [{'name': s['name'], 'poses': s['poses']} for s in sets],
            'assets': [{'name': a['name'], 'path': a['path'], 'relative_path': a['relative_path'],
                        'duration': a['duration']} for a in manifest['assets']],
            'compositions': compositions}


def render_script(manifest):
    payload = json.dumps(manifest, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    return TEMPLATE.read_text('utf8').replace('/*__VOCUY_LIPSYNC__*/', payload)
