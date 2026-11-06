"""Premiere Pro XML, DaVinci Resolve scripts and SRT subtitles from export manifests.

Both editors place audio on whole frames, so this module turns the sample-precise
After Effects manifest into frame layouts. Python standard library only.
"""
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET


RESOLVE_TEMPLATE = Path(__file__).parent / 'templates' / 'davinci_resolve.py'
PREMIERE_LABELS = ('Forest', 'Cerulean', 'Rose')  # Green, blue, red, as in After Effects.
_INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')


def frame_rate(fps):
    """Return (timebase, ntsc, exact frames per second) for a supported rate."""
    timebase = round(fps)
    ntsc = abs(fps - timebase) > 1e-6
    return timebase, ntsc, (timebase * 1000 / 1001 if ntsc else float(timebase))


def track_for(manifest, clip):
    return max(1, min(3, clip['speaker'])) if manifest['mode'] == 'multi' else 1


def layout(manifest, composition):
    """Frame-based clips: one audio track per speaker, word markers in source frames."""
    rate = frame_rate(manifest['settings']['fps'])[2]
    clips, previous = [], {}
    for clip in composition['clips']:
        asset = manifest['assets'][clip['asset']]
        length = max(1, math.ceil(asset['duration'] * rate - 1e-6))
        start = math.floor(clip['start'] * rate + .5)
        track = track_for(manifest, clip)
        markers = []
        for word in asset['words']:
            # Floor matches the subtitle frames, so a marker never precedes its caption.
            frame = min(length - 1, math.floor(word['start'] * rate + 1e-6))
            end = max(frame + 1, math.floor(word['end'] * rate + .5))
            # Editors allow one marker per frame; keep fast words together.
            if markers and markers[-1][0] == frame:
                markers[-1][1] = max(markers[-1][1], end - frame)
                markers[-1][2] += ' ' + word['text']
            else:
                markers.append([frame, end - frame, word['text']])
        item = {'asset': clip['asset'], 'name': clip['name'], 'speaker': clip['speaker'],
                'track': track, 'start': start, 'length': length, 'markers': markers}
        # Rounded back-to-back clips can overlap by one frame of trailing silence.
        before = previous.get(track)
        if before and before['start'] + before['length'] > start:
            before['length'] = max(1, start - before['start'])
        previous[track] = item
        clips.append(item)
    end = max([c['start'] + c['length'] for c in clips] + [math.floor(composition['duration'] * rate + .5)])
    return {'name': composition['name'], 'duration': end, 'clips': clips,
            'tracks': max([c['track'] for c in clips] + [1])}


def srt(composition):
    """SubRip captions for one sequence, or an empty string when subtitles are off."""
    cues = sorted((cue for clip in composition['clips'] for cue in clip['cues']), key=lambda c: c['start'])
    blocks = []
    for cue in cues:
        text = _INVALID_XML.sub('', cue['text']).replace('\r', ' ').replace('\n', ' ').strip()
        if text:
            blocks.append(f'{len(blocks) + 1}\n{_timestamp(cue["start"])} --> {_timestamp(cue["end"])}\n{text}\n')
    return '\n'.join(blocks)


def _timestamp(seconds):
    ms = max(0, round(seconds * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def _text(value):
    return _INVALID_XML.sub('', str(value))


def _sub(parent, tag, text=None, **attrs):
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = _text(text)
    return element


def _rate(parent, fps):
    timebase, ntsc, _ = frame_rate(fps)
    rate = _sub(parent, 'rate')
    _sub(rate, 'timebase', timebase)
    _sub(rate, 'ntsc', 'TRUE' if ntsc else 'FALSE')
    return rate


def _pathurl(path):
    # Premiere expects file://localhost/ URLs; as_uri() percent-encodes spaces and Unicode.
    return 'file://localhost' + Path(path).resolve().as_uri()[len('file://'):]


def premiere_xml(manifest):
    """Final Cut Pro 7 XML (xmeml v4), which Premiere Pro imports as sequences."""
    settings, fps = manifest['settings'], manifest['settings']['fps']
    root = ET.Element('xmeml', version='4')
    project = _sub(root, 'project')
    _sub(project, 'name', 'VOCUY - ' + manifest['name'])
    children = _sub(project, 'children')
    defined, clip_number = set(), 0
    for number, composition in enumerate(manifest['compositions'], 1):
        timeline = layout(manifest, composition)
        sequence = _sub(children, 'sequence', id=f'sequence-{number}')
        _sub(sequence, 'name', timeline['name'])
        _sub(sequence, 'duration', timeline['duration'])
        _rate(sequence, fps)
        timecode = _sub(sequence, 'timecode')
        _rate(timecode, fps)
        _sub(timecode, 'string', '00:00:00:00')
        _sub(timecode, 'frame', 0)
        _sub(timecode, 'displayformat', 'NDF')
        media = _sub(sequence, 'media')
        video = _sub(media, 'video')
        characteristics = _sub(_sub(video, 'format'), 'samplecharacteristics')
        _rate(characteristics, fps)
        _sub(characteristics, 'width', settings['width'])
        _sub(characteristics, 'height', settings['height'])
        _sub(characteristics, 'anamorphic', 'FALSE')
        _sub(characteristics, 'pixelaspectratio', 'square')
        _sub(characteristics, 'fielddominance', 'none')
        _sub(video, 'track')
        audio = _sub(media, 'audio')
        _sub(audio, 'numOutputChannels', 2)
        output = _sub(_sub(audio, 'format'), 'samplecharacteristics')
        _sub(output, 'depth', 16)
        _sub(output, 'samplerate', 48000)
        tracks = [_sub(audio, 'track') for _ in range(timeline['tracks'])]
        for clip in timeline['clips']:
            clip_number += 1
            asset = manifest['assets'][clip['asset']]
            file_frames = max(1, math.ceil(asset['duration'] * frame_rate(fps)[2] - 1e-6))
            item = _sub(tracks[clip['track'] - 1], 'clipitem', id=f'clipitem-{clip_number}')
            _sub(item, 'name', clip['name'])
            _sub(item, 'enabled', 'TRUE')
            _sub(item, 'duration', file_frames)
            _rate(item, fps)
            _sub(item, 'start', clip['start'])
            _sub(item, 'end', clip['start'] + clip['length'])
            _sub(item, 'in', 0)
            _sub(item, 'out', clip['length'])
            file_id = f'file-{clip["asset"] + 1}'
            file = _sub(item, 'file', id=file_id)
            # The first reference defines the file; later clips reuse it by ID.
            if file_id not in defined:
                defined.add(file_id)
                _sub(file, 'name', asset['name'])
                _sub(file, 'pathurl', _pathurl(asset['path']))
                _rate(file, fps)
                _sub(file, 'duration', file_frames)
                source = _sub(_sub(file, 'media'), 'audio')
                characteristics = _sub(source, 'samplecharacteristics')
                _sub(characteristics, 'depth', asset.get('sample_width', 2) * 8)
                _sub(characteristics, 'samplerate', asset.get('sample_rate', 48000))
                _sub(source, 'channelcount', asset.get('channels', 1))
            sourcetrack = _sub(item, 'sourcetrack')
            _sub(sourcetrack, 'mediatype', 'audio')
            _sub(sourcetrack, 'trackindex', 1)
            _sub(_sub(item, 'labels'), 'label2', PREMIERE_LABELS[max(0, min(2, clip['speaker'] - 1))])
            for frame, duration, text in clip['markers']:
                marker = _sub(item, 'marker')
                _sub(marker, 'name', text)
                _sub(marker, 'comment', '')
                _sub(marker, 'in', frame)
                _sub(marker, 'out', frame + duration)
    ET.indent(root, '  ')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
            + ET.tostring(root, encoding='unicode') + '\n')


def resolve_script(manifest, subtitles=None):
    """Python script for DaVinci Resolve. subtitles maps composition index to SRT paths."""
    subtitles = subtitles or {}
    timelines = []
    for index, composition in enumerate(manifest['compositions']):
        timeline = layout(manifest, composition)
        entry = subtitles.get(index)
        cues = [cue for clip in composition['clips'] for cue in clip['cues']]
        if entry and cues:
            rate = frame_rate(manifest['settings']['fps'])[2]
            entry = {**entry, 'first_frame': math.floor(min(c['start'] for c in cues) * rate + .5)}
        timeline['subtitles'] = entry if entry and cues else None
        timelines.append(timeline)
    data = {'name': manifest['name'], 'mode': manifest['mode'], 'settings': manifest['settings'],
            'assets': [{'name': a['name'], 'path': a['path'], 'relative_path': a['relative_path']}
                       for a in manifest['assets']],
            'timelines': timelines}
    # The manifest is a JSON string literal parsed at runtime, never evaluated as code.
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    return RESOLVE_TEMPLATE.read_text('utf8').replace('__VOCUY_MANIFEST__', repr(payload))
