import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

import conversation
import lipsync
import server as app

SAMPLE = Path(__file__).resolve().parent.parent / 'Clay_Mouth_Poses_12_Transparent_512'
NAMES = ['01_closed_neutral', '02_slightly_open_relaxed', '03_open_ah_medium', '04_open_ah_wide',
         '05_smile_upper_teeth', '06_wide_grin_teeth_together', '07_rounded_o_open',
         '08_puckered_u_small_opening', '09_rounded_u_narrow_opening', '10_narrow_teeth_showing',
         '11_open_tongue_low', '12_open_tongue_raised', '13_open_tongue_forward',
         '14_lower_lip_tucked_upper_teeth', '15_open_teeth_separated', '16_closed_soft_press',
         '17_closed_pout_lower_lip_forward', '18_asymmetric_teeth_grimace', '19_open_ah_shallow',
         '20_closed_gentle_smile']


def characters(text, start=0.0, step=.08, pause=.3):
    """Fake ElevenLabs character timings; punctuation is followed by a pause."""
    out, t = [], start
    for char in text:
        length = pause if char in ',.!?' else .04 if char == ' ' else step
        out.append({'text': char, 'start': round(t, 4), 'end': round(t + length, 4)})
        t += length
    return out, t


class TestSounds(unittest.TestCase):
    def shapes(self, word):
        return [s for _, _, s in lipsync.letter_shapes([(c, i, i + 1) for i, c in enumerate(word)])]

    def test_unknown_words_use_digraphs_soft_letters_and_continental_vowels(self):
        self.assertEqual(self.shapes('thik'), ['th', 'ee', 'tdn'])
        self.assertEqual(self.shapes('Zeleboba'), ['sz', 'eh', 'l', 'eh', 'mbp', 'oh', 'mbp', 'ah'])
        self.assertEqual(self.shapes('cira'), ['sz', 'ee', 'r', 'ah'])
        self.assertEqual(self.shapes('Café!'), ['tdn', 'ah', 'fv', 'eh', None])
        self.assertEqual(self.shapes('мама'), ['mbp', 'ah', 'mbp', 'ah'])

    def test_dictionary_words_use_pronunciation_and_split_diphthongs(self):
        def say(word, start=1.0, step=.1):
            shaped = lipsync.phoneme_shapes([(c, start + i * step, start + (i + 1) * step) for i, c in enumerate(word)])
            return shaped and [s for _, _, s in shaped]
        self.assertEqual(say('Friday!'), ['fv', 'r', 'ah', 'ee', 'tdn', 'ee'])
        self.assertEqual(say('Valentina'), ['fv', 'ah', 'l', 'eh', 'tdn', 'tdn', 'ee', 'tdn', 'uh'])
        self.assertEqual(say("don’t"), ['tdn', 'oh', 'oo', 'tdn', 'tdn'])
        self.assertIsNone(say('zeleboba'))
        shaped = lipsync.phoneme_shapes([(c, 1 + i * .1, 1.1 + i * .1) for i, c in enumerate('time,')])
        self.assertEqual((shaped[0][0], round(shaped[-1][1], 6)), (1, 1.4))  # The comma is a pause.
        vowel, stop = shaped[1][1] - shaped[1][0] + shaped[2][1] - shaped[2][0], shaped[0][1] - shaped[0][0]
        self.assertGreater(vowel, stop * 2)

    def test_word_timings_spread_when_character_timings_are_missing(self):
        words = lipsync.timed_words({'words': [{'text': 'Hi', 'start': 1, 'end': 1.2}]}, 2)
        self.assertEqual([[(c, round(s, 3), round(e, 3)) for c, s, e in w] for w in words],
                         [[('H', 1, 1.1), ('i', 1.1, 1.2)]])

    def test_pause_closes_mouth_and_long_a_opens_wide(self):
        chars, end = characters('Ma, aaaa')
        segments = lipsync.segments(lipsync.timed_words({'characters': chars}, end))
        self.assertEqual([s for _, _, s in segments], ['mbp', 'ah', 'wide'])
        events = lipsync.speech_events(segments, 0)
        self.assertEqual([s for _, s in events], ['mbp', 'ah', 'rest', 'wide', 'rest'])


class TestKeyframes(unittest.TestCase):
    def setUp(self):
        self.mapping = {shape: [n] for n, shape in enumerate(lipsync.SHAPES, 1)}
        self.mapping['ah'] = [7, 8]

    def test_starts_and_ends_at_rest_with_minimum_hold_and_one_frame_lead(self):
        events = [(.5, 'ah'), (.52, 'tdn'), (.6, 'mbp'), (.7, 'ah'), (1.0, 'rest')]
        keys = lipsync.keyframes(events, 30, self.mapping)
        frames = [round(t * 30) for t, _ in keys]
        self.assertEqual(keys[0], (0, self.mapping['rest'][0]))
        self.assertEqual(keys[-1][1], self.mapping['rest'][0])
        self.assertEqual(frames[1], 14)  # 0.5 s is frame 15; shown one frame early.
        self.assertTrue(all(b - a >= 2 for a, b in zip(frames, frames[1:])))
        self.assertIn(self.mapping['mbp'][0], [p for _, p in keys])  # Lip closures survive.

    def test_variety_rotates_poses_and_never_repeats_a_pose(self):
        events = [(.2, 'ah'), (.4, 'tdn'), (.6, 'ah'), (.8, 'tdn'), (1.0, 'ah')]
        poses = [p for _, p in lipsync.keyframes(events, 30, self.mapping)]
        self.assertEqual([p for p in poses if p in (7, 8)], [7, 8, 7])
        self.assertTrue(all(a != b for a, b in zip(poses, poses[1:])))


class TestMouthFolders(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / 'mouths'
        self.folder.mkdir()

    def tearDown(self):
        lipsync.MOUTH_FOLDERS.clear()
        self.temp.cleanup()

    def images(self, names):
        for name in names:
            (self.folder / name).write_bytes(b'\x89PNG')

    def test_sample_naming_uses_built_in_table_and_ignores_reference_sheet(self):
        self.images([f'mouth_{n}.png' for n in NAMES] + ['Reff.png', 'notes.md'])
        folder, files, mapping, source = lipsync.load_folder(self.folder)
        self.assertEqual((source, len(files)), ('built-in', 20))
        self.assertEqual(files[13].name, 'mouth_14_lower_lip_tucked_upper_teeth.png')
        self.assertEqual(mapping['fv'], [14])
        self.assertEqual(sorted({p for poses in mapping.values() for p in poses}), list(range(1, 21)))

    def test_custom_table_by_file_name_with_missing_shapes_borrowing(self):
        self.images(['closed.png', 'open.png', 'O.png'])
        (self.folder / 'mouth_map.csv').write_text(
            '﻿Shape,Poses\nrest,closed\nah,open.png\noh,O\n', encoding='utf8')
        _, files, mapping, source = lipsync.load_folder(self.folder)
        self.assertEqual(source, 'mouth_map.csv')
        self.assertEqual([f.name for f in files], ['closed.png', 'O.png', 'open.png'])
        self.assertEqual((mapping['rest'], mapping['ah'], mapping['oh']), ([1], [3], [2]))
        self.assertEqual(mapping['oo'], [2])     # oo -> oh
        self.assertEqual(mapping['mbp'], [1])    # mbp -> rest
        self.assertEqual(mapping['ee'], [3])     # ee -> eh -> ah

    def test_table_errors_are_explained(self):
        self.images(['closed.png', 'open.png'])
        with self.assertRaisesRegex(ValueError, 'no mouth_map.csv'):
            lipsync.load_folder(self.folder)
        table = self.folder / 'mouth_map.csv'
        for text, message in [('shape,poses\nah,open\n', 'needs a "rest" row'),
                              ('shape,poses\nrest,closed\nyell,open\n', 'unknown shape "yell"'),
                              ('shape,poses\nrest,shut\n', 'No mouth image matches "shut"'),
                              ('name,files\n', '"shape" and "poses" columns')]:
            table.write_text(text, encoding='utf8')
            with self.assertRaisesRegex(ValueError, message):
                lipsync.load_folder(self.folder)
        with self.assertRaisesRegex(ValueError, 'no mouth images'):
            lipsync.load_folder(Path(self.temp.name))

    @unittest.skipUnless(SAMPLE.is_dir(), 'Clay mouth folder is not present')
    def test_clay_pack_table_uses_all_twelve_poses_and_ignores_reference_sheet(self):
        _, files, mapping, source = lipsync.load_folder(SAMPLE)
        self.assertEqual((source, len(files)), ('mouth_map.csv', 12))
        self.assertEqual([f.name[:13] for f in files], [f'clay_mouth_{n:02d}' for n in range(1, 13)])
        self.assertEqual(set(mapping), set(lipsync.SHAPES))
        self.assertEqual(sorted({p for poses in mapping.values() for p in poses}), list(range(1, 13)))


class TestLipSyncExport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = self.root / 'history'
        self.history.mkdir()
        self.destination = self.root / 'output'
        self.destination.mkdir()
        self.patches = [mock.patch.object(app, 'OUTPUT_DIR', self.history),
                        mock.patch.object(app, 'api_request', side_effect=AssertionError('Export must be offline'))]
        for patch in self.patches:
            patch.start()
        app.JOBS.clear()
        app.BATCHES.clear()
        self.token = conversation.register_destination(str(self.destination))['token']
        self.mouths = {}
        for name, poses in (('Hero', NAMES), ('Sidekick', NAMES[:3])):
            folder = self.destination / name
            folder.mkdir()
            for pose in poses:
                (folder / f'mouth_{pose}.png').write_bytes(b'\x89PNG')
            if name == 'Sidekick':
                (folder / 'mouth_map.csv').write_text('shape,poses\nrest,1\nah,3\nee,2\n', encoding='utf8')
            self.mouths[name] = lipsync.register_folder(str(folder))

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        app.JOBS.clear()
        app.BATCHES.clear()
        conversation.DESTINATIONS.clear()
        lipsync.MOUTH_FOLDERS.clear()
        self.temp.cleanup()

    def recording(self, text, speaker=1):
        job_id = uuid.uuid4().hex
        directory = self.history / job_id
        directory.mkdir()
        stem = 'voice_' + job_id
        chars, end = characters(text, start=.1)
        wav = directory / (stem + '.wav')
        app.create_wav(b'\0\0' * int((end + .2) * 24000), wav, 24000)
        shutil.copyfile(wav, self.destination / wav.name)
        (directory / 'metadata_internal.json').write_text(json.dumps(
            {'filename_stem': stem, 'external_wav': str(self.destination / wav.name)}), encoding='utf8')
        words = [{'text': w, 'start': .1, 'end': end} for w in text.split()][:1]
        (directory / (stem + '_alignment.json')).write_text(
            json.dumps({'characters': chars, 'words': words}), encoding='utf8')
        job = {'id': job_id, 'name': 'Test project', 'speaker': speaker, 'voice_name': f'Speaker {speaker}',
               'has_wav': True, 'has_alignment': True, 'status': 'complete', 'export_filename': wav.name}
        app.JOBS[job_id] = job
        app.persist_job(job_id)
        return job

    def run_script(self, filename):
        driver = Path(__file__).with_name('lipsync_driver.js')
        output = subprocess.run(['node', str(driver), str(self.destination / filename)],
                                check=True, capture_output=True, text=True)
        return json.loads(output.stdout)

    def test_requires_mouth_folder_and_keeps_regular_export_separate(self):
        job = self.recording('Hello')
        request = {'job_id': job['id'], 'destination_token': self.token}
        with self.assertRaisesRegex(ValueError, 'mouth folder for Voice 1'):
            app.export_timeline(request, 'lip_sync')
        self.assertEqual(list(self.destination.glob('*.jsx')), [])
        lipsync.MOUTH_FOLDERS.clear()
        with self.assertRaisesRegex(ValueError, 'mouth folder again'):
            app.export_timeline({**request, 'mouths': {'1': self.mouths['Hero']['token']}}, 'lip_sync')

    @unittest.skipUnless(shutil.which('node'), 'Optional Node.js checks generated JSX behavior')
    def test_single_voice_script_builds_null_parent_mouth_and_audio(self):
        job = self.recording('Hello, my friend.')
        result = app.export_timeline({'job_id': job['id'], 'destination_token': self.token,
                                      'settings': {'width': 1920, 'height': 1080, 'fps': 24},
                                      'mouths': {'1': self.mouths['Hero']['token']}}, 'lip_sync')
        self.assertTrue(result['filename'].endswith('_lipsync.jsx'))
        run = self.run_script(result['filename'])
        self.assertEqual(len(run['imports']), 21)
        self.assertTrue(all(i['sequence'] is False for i in run['imports']))
        poses, comp = run['comps']
        self.assertEqual((poses['width'], poses['height'], len(poses['layers'])), (512, 512, 20))
        self.assertEqual(poses['layers'][0]['name'], '1 - mouth_01_closed_neutral.png')
        self.assertAlmostEqual(poses['layers'][13]['in'], 13 / 24)
        self.assertEqual(comp['name'], 'Test project - Lip sync')
        self.assertEqual([l['kind'] for l in comp['layers']], ['null', 'precomp', 'audio'])
        control, mouth, audio = comp['layers']
        self.assertEqual(control['name'], 'Voice 1 - Speaker 1 - Mouth control')
        self.assertEqual((mouth['parent'], audio['parent']), (control['name'], control['name']))
        self.assertEqual(control['position'], [960, 540])
        self.assertEqual(mouth['out'], comp['duration'])
        # Slider value N shows image N; values outside the folder clamp to the last image.
        self.assertEqual([round(t * 24, 3) for t in mouth['remap']], [.5, 1.5, 2.5, 19.5])
        effect = control['effects'][0]
        self.assertEqual((effect['match'], effect['name']), ('ADBE Slider Control', 'Mouth pose'))
        keys = effect['keys']
        self.assertEqual(effect['hold'], len(keys))
        self.assertEqual(keys[0], [0, 1])
        self.assertIn(keys[-1][1], (1, 20))
        self.assertIn(16, [p for _, p in keys])           # "my" closes the lips.
        self.assertTrue(set(p for _, p in keys) <= set(range(1, 21)))

    @unittest.skipUnless(shutil.which('node'), 'Optional Node.js checks generated JSX behavior')
    def test_multi_voice_gets_one_null_and_mouth_per_voice(self):
        jobs = [self.recording('Hi there.', 1), self.recording('Wow!', 2), self.recording('Yes.', 1)]
        turns = [{'number': i + 1, 'speaker': j['speaker'], 'job': j} for i, j in enumerate(jobs)]
        batch = {'id': uuid.uuid4().hex, 'name': 'Chat', 'mode': 'multi',
                 'turns': [{**t, 'job_id': t['job']['id']} for t in turns]}
        for t in batch['turns']:
            del t['job']
        app.BATCHES[batch['id']] = batch
        app.persist_batch(batch)
        result = app.export_timeline({'batch_id': batch['id'], 'destination_token': self.token,
                                      'mouths': {'1': self.mouths['Hero']['token'],
                                                 '2': self.mouths['Sidekick']['token']}}, 'lip_sync')
        run = self.run_script(result['filename'])
        hero, sidekick, comp = run['comps']
        self.assertEqual((len(hero['layers']), len(sidekick['layers'])), (20, 3))
        self.assertEqual([l['kind'] for l in comp['layers']],
                         ['null', 'precomp', 'audio', 'audio', 'null', 'precomp', 'audio'])
        first, second = comp['layers'][0], comp['layers'][4]
        self.assertTrue(first['name'].startswith('Voice 1') and second['name'].startswith('Voice 2'))
        self.assertEqual(comp['layers'][1]['source'], 'Mouth poses - Hero')
        self.assertEqual(comp['layers'][5]['source'], 'Mouth poses - Sidekick')
        self.assertEqual([l['parent'] for l in comp['layers'][1:4]], [first['name']] * 3)
        self.assertEqual(first['position'], [360, 960])
        self.assertEqual(second['position'], [720, 960])
        # Voice 2 rests while Voice 1 speaks first, and only uses its three images.
        keys = second['effects'][0]['keys']
        self.assertEqual(keys[0], [0, 1])
        self.assertGreater(keys[1][0], .3)
        self.assertTrue({p for _, p in keys} <= {1, 2, 3})


if __name__ == '__main__':
    unittest.main()
