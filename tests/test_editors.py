import io
import contextlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

import after_effects as ae
import conversation
import editors
import server as app


class FakeItem:
    def __init__(self, path):
        self.path = path

    def GetClipProperty(self, name):
        return self.path if name == 'File Path' else ''


class FakeTimelineItem:
    def __init__(self, info):
        self.info, self.markers, self.color, self.name = info, [], None, None

    def SetName(self, name):
        self.name = name

    def SetClipColor(self, color):
        self.color = color

    def GetStart(self):
        return self.info.get('recordFrame', 0)

    def AddMarker(self, frame, color, name, note, duration, custom):
        if any(m[0] == frame for m in self.markers):
            return False
        self.markers.append((frame, color, name, duration))
        return True


class FakeTimeline:
    def __init__(self, name):
        self.name, self.settings, self.audio_tracks = name, {}, 1
        self.items, self.subtitles = [], []

    def GetName(self):
        return self.name

    def SetSetting(self, key, value):
        self.settings[key] = value
        return True

    def GetStartFrame(self):
        return 108000

    def GetTrackCount(self, kind):
        return self.audio_tracks if kind == 'audio' else (1 if self.subtitles else 0)

    def AddTrack(self, kind, subtype=None):
        self.audio_tracks += 1
        return True

    def GetItemListInTrack(self, kind, index):
        return list(self.subtitles) if kind == 'subtitle' else []

    def DeleteClips(self, items):
        self.subtitles = []
        return True


class FakePool:
    def __init__(self, project, subtitle_offset):
        self.project, self.subtitle_offset = project, subtitle_offset
        self.folders, self.imports, self.deleted = [], [], []

    def GetRootFolder(self):
        return 'root'

    def AddSubFolder(self, parent, name):
        self.folders.append((parent, name))
        return name

    def SetCurrentFolder(self, folder):
        return True

    def ImportMedia(self, paths):
        self.imports.extend(paths)
        return [FakeItem(p) for p in reversed(paths)]  # Resolve may not keep the order.

    def CreateEmptyTimeline(self, name):
        timeline = FakeTimeline(name)
        self.project.timelines.append(timeline)
        return timeline

    def AppendToTimeline(self, infos):
        timeline = self.project.current
        if isinstance(infos[0], dict):
            item = FakeTimelineItem(infos[0])
            timeline.items.append(item)
            return [item]
        if self.subtitle_offset is not None:
            timeline.subtitles.append(FakeTimelineItem({'recordFrame': 108000 + self.subtitle_offset}))
        return []

    def DeleteTimelines(self, timelines):
        self.deleted.append('timelines')

    def DeleteClips(self, clips):
        self.deleted.append('clips')

    def DeleteFolders(self, folders):
        self.deleted.append('folders')


class FakeProject:
    def __init__(self, fps='30', existing=0, subtitle_offset=None):
        self.settings = {'timelineFrameRate': fps}
        self.timelines = [FakeTimeline(f'Existing {i}') for i in range(existing)]
        self.current = None
        self.pool = FakePool(self, subtitle_offset)

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, index):
        return self.timelines[index - 1]

    def SetSetting(self, key, value):
        self.settings[key] = value
        return True

    def GetSetting(self, key):
        return self.settings.get(key, '')

    def GetMediaPool(self):
        return self.pool

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True


class FakeResolve:
    def __init__(self, project):
        self.project = project

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project


class TestEditors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = self.root / 'history'
        self.history.mkdir()
        self.destination = self.root / 'output with spaces'
        self.destination.mkdir()
        self.patches = [mock.patch.object(app, 'OUTPUT_DIR', self.history),
                        mock.patch.object(app, 'api_request', side_effect=AssertionError('Export must be offline'))]
        for patch in self.patches:
            patch.start()
        app.JOBS.clear()
        app.BATCHES.clear()
        self.token = conversation.register_destination(str(self.destination))['token']

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        app.JOBS.clear()
        app.BATCHES.clear()
        conversation.DESTINATIONS.clear()
        self.temp.cleanup()

    def recording(self, speaker=1, frames=24000, words=None):
        job_id = uuid.uuid4().hex
        directory = self.history / job_id
        directory.mkdir()
        stem = 'voice_' + job_id
        wav = directory / (stem + '.wav')
        app.create_wav(b'\0\0' * frames, wav, 24000)
        target = self.destination / wav.name
        shutil.copyfile(wav, target)
        (directory / 'metadata_internal.json').write_text(
            json.dumps({'filename_stem': stem, 'external_wav': str(target)}), encoding='utf8')
        words = words if words is not None else [
            {'text': 'Hello', 'start': .1, 'end': .4}, {'text': 'world!', 'start': .5, 'end': .9}]
        (directory / (stem + '_alignment.json')).write_text(json.dumps({'words': words}), encoding='utf8')
        job = {'id': job_id, 'name': 'Test project', 'episode_id': '01', 'speaker': speaker,
               'voice_name': f'Speaker {speaker}', 'has_wav': True, 'has_alignment': True,
               'status': 'complete', 'export_filename': target.name}
        app.JOBS[job_id] = job
        app.persist_job(job_id)
        return job

    def manifest(self, turns, mode='multi', **settings):
        return ae.build_manifest('Test project', mode, turns, self.history, self.destination,
                                 {'subtitles': 'words', **settings})

    def conversation(self):
        jobs = [self.recording(speaker=n) for n in (1, 2, 1)]
        return [{'number': i + 1, 'speaker': j['speaker'], 'job': j} for i, j in enumerate(jobs)]

    def test_layout_uses_speaker_tracks_whole_frames_and_merges_same_frame_words(self):
        job = self.recording(words=[{'text': 'a', 'start': 0, 'end': .01}, {'text': 'b', 'start': .01, 'end': .02},
                                    {'text': 'c', 'start': .05, 'end': .06}], frames=2401)
        turns = [{'number': 1, 'speaker': 1, 'job': job}, {'number': 2, 'speaker': 1, 'job': job},
                 {'number': 3, 'speaker': 2, 'job': self.recording(speaker=2, frames=2401, words=[{'text': 'd', 'start': 0, 'end': .1}])}]
        manifest = self.manifest(turns)
        timeline = editors.layout(manifest, manifest['compositions'][0])
        clips = timeline['clips']
        self.assertEqual([c['track'] for c in clips], [1, 1, 2])
        self.assertEqual(timeline['tracks'], 2)
        # 2401 samples at 24 kHz = 3.001 frames; the back-to-back clip trims silence.
        self.assertEqual([(c['start'], c['length']) for c in clips], [(0, 3), (3, 4), (6, 4)])
        self.assertEqual(clips[0]['markers'][0], [0, 1, 'a b'])
        self.assertEqual(clips[0]['markers'][1][2], 'c')
        self.assertTrue(all(m[0] < c['length'] for c in clips for m in c['markers']))

    def test_premiere_xml_defines_each_file_once_with_markers_rates_and_size(self):
        turns = self.conversation()
        manifest = self.manifest(turns, fps=29.97, width=1920, height=1080)
        root = ET.fromstring(editors.premiere_xml(manifest).split('\n', 2)[2])
        sequence = root.find('project/children/sequence')
        self.assertEqual(sequence.findtext('rate/timebase'), '30')
        self.assertEqual(sequence.findtext('rate/ntsc'), 'TRUE')
        self.assertEqual(sequence.findtext('media/video/format/samplecharacteristics/width'), '1920')
        tracks = sequence.findall('media/audio/track')
        self.assertEqual([len(t.findall('clipitem')) for t in tracks], [2, 1])
        files = [f for f in root.iter('file') if f.find('pathurl') is not None]
        self.assertEqual(len(files), 3)
        url = files[0].findtext('pathurl')
        self.assertTrue(url.startswith('file://localhost/'))
        self.assertIn('output%20with%20spaces', url)
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        self.assertTrue(Path(path.lstrip('/') if os.name == 'nt' else path).is_file())
        self.assertEqual(files[0].findtext('media/audio/samplecharacteristics/samplerate'), '24000')
        first = tracks[0].find('clipitem')
        self.assertEqual([m.findtext('name') for m in first.findall('marker')], ['Hello', 'world!'])
        self.assertEqual(first.findtext('marker/in'), '2')  # 0.1 s at 29.97 fps, floored like captions
        self.assertEqual(first.findtext('labels/label2'), 'Forest')
        self.assertEqual(tracks[1].find('clipitem/labels/label2').text, 'Cerulean')

    def test_premiere_batch_reuses_shared_file_and_escapes_text(self):
        text = 'Привіт <b>&"\x01'
        shared = self.recording(words=[{'text': text, 'start': 0, 'end': .5}])
        turns = [{'number': i + 1, 'video_id': f'VIDEO_{i}', 'job': shared} for i in range(5)]
        xml = editors.premiere_xml(self.manifest(turns, mode='batch'))
        root = ET.fromstring(xml.split('\n', 2)[2])
        self.assertEqual(len(root.findall('project/children/sequence')), 5)
        self.assertEqual(len([f for f in root.iter('file') if f.find('pathurl') is not None]), 1)
        self.assertEqual(len([f for f in root.iter('file') if f.get('id') == 'file-1']), 5)
        self.assertEqual(root.find('.//marker/name').text, 'Привіт <b>&"')

    def test_srt_timestamps_and_off(self):
        manifest = self.manifest(self.conversation(), subtitles='phrases')
        text = editors.srt(manifest['compositions'][0])
        blocks = text.strip().split('\n\n')
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0].split('\n'), ['1', '00:00:00,100 --> 00:00:00,900', 'Hello world!'])
        self.assertTrue(blocks[1].startswith('2\n00:00:01,100 --> '))
        self.assertEqual(editors.srt(self.manifest(self.conversation(), subtitles='off')['compositions'][0]), '')

    def test_export_saves_xml_and_subtitles_without_replacing_earlier_exports(self):
        turns = self.conversation()
        batch = {'id': uuid.uuid4().hex, 'name': 'Test project', 'episode_id': '01', 'mode': 'multi',
                 'turns': [{**{k: v for k, v in t.items() if k != 'job'}, 'job_id': t['job']['id']} for t in turns]}
        app.BATCHES[batch['id']] = batch
        app.persist_batch(batch)
        request = {'batch_id': batch['id'], 'destination_token': self.token, 'target': 'premiere'}
        first = app.export_timeline(request)
        second = app.export_timeline(request)
        self.assertEqual(first['filename'], 'test-project_01_premiere.xml')
        self.assertEqual(second['filename'], 'test-project_01_premiere_02.xml')
        self.assertEqual((first['subtitles'], first['subtitles_folder']), (1, 'test-project_01_premiere_subtitles'))
        srt = self.destination / first['subtitles_folder'] / 'test-project-01.srt'
        self.assertIn('Hello', srt.read_text('utf8'))
        self.assertTrue((self.destination / second['subtitles_folder']).is_dir())
        off = app.export_timeline({**request, 'settings': {'subtitles': 'off'}})
        self.assertEqual((off['subtitles'], off['subtitles_folder']), (0, ''))
        resolve = app.export_timeline({**request, 'target': 'resolve'})
        self.assertTrue(resolve['filename'].endswith('_resolve.py'))
        self.assertEqual(Path(resolve['path']), (self.destination / resolve['filename']).resolve())
        with self.assertRaises(app.ApiError):
            app.export_timeline({**request, 'target': 'final_cut'})
        cached = list((self.history / 'exports').iterdir())
        self.assertEqual(sorted(p.suffix for p in cached), ['.py', '.xml', '.xml', '.xml'])

    def run_resolve(self, script_path, project, file_defined=True):
        scope = {'resolve': FakeResolve(project), '__name__': '__main__'}
        if file_defined:
            scope['__file__'] = str(script_path)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(compile(script_path.read_text('utf8'), str(script_path), 'exec'), scope)
        return output.getvalue()

    def resolve_export(self, turns, mode='multi', **settings):
        jobs = {t['job']['id'] for t in turns}
        batch = {'id': uuid.uuid4().hex, 'name': 'Test project', 'episode_id': '01', 'mode': mode,
                 'turns': [{**{k: v for k, v in t.items() if k != 'job'}, 'job_id': t['job']['id']} for t in turns]}
        app.BATCHES[batch['id']] = batch
        app.persist_batch(batch)
        result = app.export_timeline({'batch_id': batch['id'], 'destination_token': self.token,
                                      'target': 'resolve', 'settings': settings})
        self.assertEqual(len(jobs), result['recordings'])
        return self.destination / result['filename']

    def test_resolve_script_builds_timeline_tracks_markers_and_subtitles(self):
        script = self.resolve_export(self.conversation(), fps=25)
        project = FakeProject(fps='24', subtitle_offset=2)  # 0.1 s at 25 fps
        output = self.run_resolve(script, project)
        self.assertIn('created 1 timeline(s), using 3 recording(s)', output)
        self.assertNotIn('drag each SRT', output)
        self.assertEqual(project.settings['timelineFrameRate'], '25')
        timeline = project.timelines[0]
        self.assertEqual(timeline.name, 'Test project · 01')
        self.assertEqual(timeline.audio_tracks, 2)
        self.assertEqual(timeline.settings['timelineResolutionHeight'], '1920')
        infos = [i.info for i in timeline.items]
        self.assertEqual([i['trackIndex'] for i in infos], [1, 2, 1])
        self.assertEqual([i['recordFrame'] for i in infos], [108000, 108025, 108050])
        self.assertEqual({i['mediaType'] for i in infos}, {2})
        self.assertEqual(infos[0]['endFrame'], 24)
        self.assertEqual(timeline.items[0].markers, [(2, 'Green', 'Hello', 8), (12, 'Green', 'world!', 11)])
        self.assertEqual(timeline.items[1].color, 'Blue')
        self.assertEqual(timeline.items[0].name, '001 · Speaker 1')
        self.assertEqual(project.pool.imports[-1][-4:], '.srt')

    def test_resolve_script_matches_imported_paths_and_relative_files(self):
        turns = self.conversation()
        script = self.resolve_export(turns)
        moved = self.root / 'moved'
        shutil.copytree(self.destination, moved)
        shutil.rmtree(self.destination)
        project = FakeProject()
        output = self.run_resolve(moved / script.name, project)
        self.assertIn('created 1 timeline(s)', output)
        names = [Path(t['job']['export_filename']).name for t in turns]
        placed = [Path(i.info['mediaPoolItem'].path) for i in project.timelines[0].items]
        self.assertEqual([p.name for p in placed], names)
        self.assertTrue(all(moved in p.parents for p in placed))

    def test_resolve_script_reports_misplaced_subtitles_missing_audio_and_fps_mismatch(self):
        script = self.resolve_export(self.conversation())
        project = FakeProject(subtitle_offset=500)
        output = self.run_resolve(script, project)
        self.assertIn('drag each SRT from the Subtitles bin', output)
        self.assertEqual(project.timelines[0].subtitles, [])
        project = FakeProject(fps='25', existing=1)
        output = self.run_resolve(script, project)
        self.assertIn('uses 25 fps but the export uses 30 fps', output)
        self.assertEqual(len(project.timelines), 1)
        self.assertEqual(project.pool.folders, [])
        # Without __file__ (Console exec), the absolute paths are used.
        project = FakeProject()
        self.assertIn('created', self.run_resolve(script, project, file_defined=False))
        for wav in self.destination.glob('*.wav'):
            wav.unlink()
        project = FakeProject()
        self.assertIn('audio file missing', self.run_resolve(script, project))
        self.assertEqual(project.pool.folders, [])

    def test_resolve_batch_creates_unique_timeline_names_and_reuses_imports(self):
        shared = self.recording()
        turns = [{'number': i + 1, 'video_id': 'VIDEO', 'job': shared} for i in range(2)]
        turns += [{'number': 3, 'video_id': 'OTHER', 'job': shared}]
        script = self.resolve_export(turns, mode='batch', subtitles='off')
        project = FakeProject()
        project.timelines.append(FakeTimeline('VIDEO'))
        self.run_resolve(script, project)
        self.assertEqual([t.name for t in project.timelines], ['VIDEO', 'VIDEO (2)', 'OTHER'])
        self.assertEqual(len(project.pool.imports), 1)
        self.assertEqual([i.info['recordFrame'] for i in project.timelines[1].items], [108000, 108030])


if __name__ == '__main__':
    unittest.main()
