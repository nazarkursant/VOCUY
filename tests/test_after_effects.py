import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
import uuid

import after_effects as ae
import conversation
import server as app


class TestAfterEffects(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = self.root / 'history'
        self.history.mkdir()
        self.destination = self.root / 'output with spaces'
        self.destination.mkdir()
        self.output_patch = mock.patch.object(app, 'OUTPUT_DIR', self.history)
        self.output_patch.start()
        self.api_patch = mock.patch.object(app, 'api_request', side_effect=AssertionError('Export must be offline'))
        self.api_patch.start()
        app.JOBS.clear()
        app.BATCHES.clear()
        self.token = conversation.register_destination(str(self.destination))['token']

    def tearDown(self):
        self.output_patch.stop()
        self.api_patch.stop()
        app.JOBS.clear()
        app.BATCHES.clear()
        conversation.DESTINATIONS.clear()
        self.temp.cleanup()

    def recording(self, speaker=1, duration_frames=2401, words=None, external=True):
        job_id = uuid.uuid4().hex
        directory = self.history / job_id
        directory.mkdir()
        stem = 'voice_' + job_id
        wav = directory / (stem + '.wav')
        app.create_wav(b'\0\0' * duration_frames, wav, 24000)
        target = self.destination / wav.name
        if external:
            shutil.copyfile(wav, target)
        meta = {'filename_stem': stem, 'external_wav': str(target)}
        (directory / 'metadata_internal.json').write_text(json.dumps(meta), encoding='utf8')
        words = words if words is not None else [
            {'text': 'Hello', 'start': .01, 'end': .045}, {'text': 'world!', 'start': .06, 'end': .09}]
        (directory / (stem + '_alignment.json')).write_text(json.dumps({'words': words}), encoding='utf8')
        job = {'id': job_id, 'name': 'Test project', 'episode_id': '01', 'speaker': speaker,
               'voice_name': f'Speaker {speaker}', 'has_wav': True, 'has_alignment': True,
               'duration': .1, 'status': 'complete', 'export_filename': target.name}
        app.JOBS[job_id] = job
        app.persist_job(job_id)
        return job

    def batch(self, turns, mode='batch'):
        batch = {'id': uuid.uuid4().hex, 'name': 'Test project', 'episode_id': '01', 'mode': mode,
                 'turns': [{**t, 'job_id': t['job']['id']} for t in turns]}
        for t in batch['turns']:
            del t['job']
        app.BATCHES[batch['id']] = batch
        app.persist_batch(batch)
        return batch['id']

    def manifest(self, turns, mode='single', subtitles='words', **settings):
        return ae.build_manifest('Test project', mode, turns, self.history, self.destination,
                                 {'subtitles': subtitles, **settings})

    def test_single_defaults_word_markers_and_exact_wav_duration(self):
        job = self.recording()
        manifest = self.manifest([{'job': job, 'number': 1}])
        self.assertEqual(manifest['settings'], {'width': 1080, 'height': 1920, 'fps': 30, 'subtitles': 'words'})
        asset = manifest['assets'][0]
        self.assertAlmostEqual(asset['duration'], 2401 / 24000)
        self.assertEqual(asset['relative_path'], job['export_filename'])
        self.assertEqual([c['text'] for c in manifest['compositions'][0]['clips'][0]['cues']], ['Hello', 'world!'])
        self.assertEqual(len(asset['words']), 2)
        self.assertGreaterEqual(manifest['compositions'][0]['duration'], asset['duration'])

    def test_hundred_compositions_share_one_import_and_reset_timeline(self):
        shared = self.recording()
        turns = []
        for index in range(100):
            video = f'VIDEO_{index:03d}'
            turns.extend([{'number': index * 2 + 1, 'video_id': video, 'segment_id': 'intro',
                           'static_key': 'intro', 'job': shared},
                          {'number': index * 2 + 2, 'video_id': video, 'segment_id': 'body', 'job': self.recording()}])
        manifest = self.manifest(turns, mode='batch')
        self.assertEqual(len(manifest['compositions']), 100)
        self.assertEqual(len(manifest['assets']), 101)
        for comp in manifest['compositions']:
            self.assertEqual(comp['clips'][0]['asset'], 0)
            self.assertEqual(comp['clips'][0]['start'], 0)
            self.assertAlmostEqual(comp['clips'][1]['start'], 2401 / 24000)
        # Reloaded batch records preserve deduplication and composition grouping.
        batch_id = self.batch(turns)
        app.BATCHES.clear()
        result = app.export_after_effects({'batch_id': batch_id, 'destination_token': self.token})
        self.assertEqual((result['compositions'], result['recordings']), (100, 101))

    def test_multi_offsets_do_not_accumulate_rounded_job_durations(self):
        jobs = [self.recording(speaker=n) for n in (1, 2, 3)]
        turns = [{'number': i + 1, 'speaker': i + 1, 'job': job} for i, job in enumerate(jobs)]
        clips = self.manifest(turns, mode='multi')['compositions'][0]['clips']
        self.assertEqual([c['speaker'] for c in clips], [1, 2, 3])
        self.assertAlmostEqual(clips[2]['start'], 2 * 2401 / 24000)
        self.assertNotEqual(clips[2]['start'], .2)

    def test_failed_new_take_uses_previous_audio_timings_and_speaker(self):
        old = self.recording(speaker=2)
        new = {'id': uuid.uuid4().hex, 'has_wav': False, 'status': 'failed'}
        turn = {'job': new, 'previous_job': old, 'speaker': 3, 'number': 1}
        manifest = self.manifest([turn], mode='multi')
        self.assertEqual(manifest['assets'][0]['id'], old['id'])
        self.assertEqual(manifest['compositions'][0]['clips'][0]['speaker'], 2)
        # A new audible take with missing timing must not silently export the old one.
        new.update(has_wav=True, has_alignment=False)
        with self.assertRaisesRegex(ValueError, 'word timings'):
            self.manifest([turn])

    def test_phrases_split_on_pause_punctuation_and_length_off_keeps_markers(self):
        words = [{'text': word, 'start': i * .2, 'end': i * .2 + .1}
                 for i, word in enumerate('One two. Three four five six seven eight nine'.split())]
        words.append({'text': 'Later', 'start': 5, 'end': 5.2})
        cues = ae.subtitle_cues(words, 'phrases')
        self.assertEqual([c['text'] for c in cues], ['One two.', 'Three four five six seven eight', 'nine', 'Later'])
        job = self.recording()
        off = self.manifest([{'job': job}], subtitles='off')
        self.assertEqual(off['compositions'][0]['clips'][0]['cues'], [])
        self.assertEqual(len(off['assets'][0]['words']), 2)
        phrases = self.manifest([{'job': job}], subtitles='phrases')
        self.assertEqual(phrases['compositions'][0]['clips'][0]['cues'][0]['text'], 'Hello world!')

    def test_fast_words_are_preserved_when_they_share_a_frame(self):
        cues = ae.frame_cues([{'text': 'a', 'start': 0, 'end': .01},
                              {'text': 'b', 'start': .01, 'end': .02},
                              {'text': 'c', 'start': .04, 'end': .07}], 0, .1, 30)
        self.assertEqual([c['text'] for c in cues], ['a b', 'c'])
        self.assertEqual(cues[0]['end'], cues[1]['start'])
        self.assertTrue(all(c['start'] < c['end'] for c in cues))

    def test_timed_whitespace_is_omitted_without_changing_saved_alignment_or_word_times(self):
        words = [{'text': ' \n', 'start': 0, 'end': .01},
                 {'text': ' Hello ', 'start': .01, 'end': .045, 'loss': .1},
                 {'text': ' ', 'start': .045, 'end': .06},
                 {'text': 'world!', 'start': .06, 'end': .09, 'loss': .2},
                 {'text': '\t\r\n\u00a0', 'start': .09, 'end': .1},
                 {'text': '', 'start': .1, 'end': .1}]
        job = self.recording(words=words)
        alignment = next((self.history / job['id']).glob('*_alignment.json'))
        original = alignment.read_bytes()
        expected = [{'text': 'Hello', 'start': .01, 'end': .045},
                    {'text': 'world!', 'start': .06, 'end': .09}]
        for mode in ('single', 'multi', 'batch'):
            for style, texts in (('words', ['Hello', 'world!']),
                                 ('phrases', ['Hello world!']), ('off', [])):
                with self.subTest(mode=mode, style=style):
                    manifest = self.manifest([{'job': job, 'video_id': 'VIDEO_001'}],
                                             mode=mode, subtitles=style)
                    self.assertEqual(manifest['assets'][0]['words'], expected)
                    cues = manifest['compositions'][0]['clips'][0]['cues']
                    self.assertEqual([cue['text'] for cue in cues], texts)
        self.assertEqual(alignment.read_bytes(), original)
        result = app.export_after_effects({'job_id': job['id'], 'destination_token': self.token})
        self.assertTrue((self.destination / result['filename']).is_file())

    def test_subtitles_at_clip_boundary_do_not_overlap(self):
        duration = 2401 / 24000
        words = [{'text': 'Hello', 'start': 0, 'end': duration}]
        first = ae.frame_cues(words, 0, duration, 30)
        second = ae.frame_cues(words, duration, duration, 30)
        self.assertEqual(first[-1]['end'], second[0]['start'])

    def test_missing_or_invalid_timings_block_export_even_with_subtitles_off(self):
        job = self.recording()
        directory = self.history / job['id']
        alignment = next(directory.glob('*_alignment.json'))
        invalid = [[], [{'text': ' \t\n', 'start': 0, 'end': .1}],
                   [{'text': '', 'start': 0, 'end': .1}],
                   [{'text': 'Bad', 'start': float('nan'), 'end': .1}],
                   [{'text': 'Bad', 'start': .05, 'end': .01}],
                   [{'text': 'Bad', 'start': 10, 'end': 11}]]
        for words in invalid:
            alignment.write_text(json.dumps({'words': words}), encoding='utf8')
            with self.subTest(words=words), self.assertRaises(ValueError):
                self.manifest([{'job': job}], subtitles='off')
        alignment.unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unreadable'):
            self.manifest([{'job': job}])

    def test_malformed_word_text_still_blocks_export(self):
        valid = {'text': 'Hello', 'start': 0, 'end': .01}
        for malformed in (None, 'word', {}, {'text': None}, {'text': 42}, {'text': ['word']}):
            with self.subTest(word=malformed), self.assertRaisesRegex(ValueError, 'invalid text'):
                ae.read_words({'words': [valid, malformed]}, .1)

    def test_wrong_destination_copy_is_not_used_and_history_fallback_works(self):
        job = self.recording()
        (self.destination / job['export_filename']).write_bytes(b'wrong recording')
        asset = self.manifest([{'job': job}])['assets'][0]
        self.assertIsNone(asset['relative_path'])
        self.assertIn(self.history.resolve(), Path(asset['path']).parents)
        # An old multi-voice take can find an exact output copy without stored path.
        directory = self.history / job['id']
        meta_path = directory / 'metadata_internal.json'
        meta = json.loads(meta_path.read_text())
        del meta['external_wav']
        meta_path.write_text(json.dumps(meta))
        shutil.copyfile(directory / (meta['filename_stem'] + '.wav'), self.destination / job['export_filename'])
        self.assertEqual(self.manifest([{'job': job}])['assets'][0]['relative_path'], job['export_filename'])

    def test_saved_script_escapes_text_and_preserves_previous_exports(self):
        text = 'Привіт "quote" \\ path\u2028); alert("unsafe"); //'
        job = self.recording(words=[{'text': text, 'start': 0, 'end': .08}])
        request = {'job_id': job['id'], 'destination_token': self.token}
        first = app.export_after_effects(request)
        contents = (self.destination / first['filename']).read_text('utf8')
        second = app.export_after_effects(request)
        self.assertNotEqual(first['filename'], second['filename'])
        self.assertEqual((self.destination / first['filename']).read_text('utf8'), contents)
        self.assertIn(json.dumps(text, ensure_ascii=True), contents)
        self.assertNotIn('/*__VOCUY_MANIFEST__*/', contents)
        self.assertNotIn('api_key', contents)

    def test_invalid_settings_ids_busy_and_missing_destination(self):
        job = self.recording()
        request = {'job_id': job['id'], 'destination_token': self.token}
        for settings in ({'width': True}, {'height': -5}, {'fps': float('inf')}, {'subtitles': 'unknown'}, []):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                app.export_after_effects({**request, 'settings': settings})
        for bad in ({'job_id': '../escape'}, {'batch_id': job['id']}, {'job_id': None}):
            with self.assertRaises(app.ApiError):
                app.export_after_effects({**request, **bad})
        with self.assertRaises(ValueError):
            app.export_after_effects({**request, 'destination_token': 'missing'})
        self.assertTrue(app.GENERATE_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(app.ApiError) as error:
                app.export_after_effects(request)
            self.assertEqual(error.exception.status, 409)
        finally:
            app.GENERATE_LOCK.release()
        self.assertEqual(list(self.destination.glob('*.jsx')), [])

    def test_export_http_download_and_origin_protection(self):
        job = self.recording()
        http = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        root = f'http://127.0.0.1:{http.server_port}'
        body = json.dumps({'job_id': job['id'], 'destination_token': self.token}).encode()
        try:
            request = urllib.request.Request(root + '/api/after-effects', data=body,
                                             headers={'Content-Type': 'application/json', 'Origin': 'https://untrusted.example'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request.remove_header('Origin')
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            with urllib.request.urlopen(root + result['download_url']) as response:
                self.assertIn(result['filename'], response.headers['Content-Disposition'])
                self.assertEqual(response.read(), (self.destination / result['filename']).read_bytes())
        finally:
            http.shutdown()
            http.server_close()

    @unittest.skipUnless(shutil.which('node'), 'Optional Node.js checks generated JSX behavior')
    def test_generated_script_executes_with_ae_api_test_double(self):
        jobs = [self.recording(speaker=n) for n in (1, 2, 3)]
        turns = [{'number': i + 1, 'speaker': i + 1, 'job': j} for i, j in enumerate(jobs)]
        script = self.root / 'export.jsx'
        driver = Path(__file__).with_name('after_effects_driver.js')
        for style in ('words', 'phrases', 'off'):
            manifest = self.manifest(turns, mode='multi', subtitles=style)
            script.write_text(ae.render_script(manifest), encoding='utf8')
            output = subprocess.run(['node', str(driver), str(script)], check=True, capture_output=True, text=True)
            result = json.loads(output.stdout)
            self.assertEqual(result['imports'], 3)
            comp = result['comps'][0]
            self.assertEqual((comp['width'], comp['height']), (1080, 1920))
            audio = sorted(comp['audio'], key=lambda c: c['start'])
            self.assertEqual(len(audio), 3)
            self.assertEqual(len({c['label'] for c in audio}), 3)
            self.assertAlmostEqual(audio[2]['markers'][0][0], 2 * 2401 / 24000 + .01)
            self.assertEqual(len(comp['text']), 0 if style == 'off' else 3)
            if style != 'off':
                self.assertEqual(len({tuple(t['keys'][0][1]['fillColor']) for t in comp['text']}), 3)
        # A second sequence references the same imported recording.
        shared = [{'number': i + 1, 'video_id': f'VIDEO_{i}', 'job': jobs[0]} for i in range(100)]
        script.write_text(ae.render_script(self.manifest(shared, mode='batch')), encoding='utf8')
        output = subprocess.run(['node', str(driver), str(script)], check=True, capture_output=True, text=True)
        result = json.loads(output.stdout)
        self.assertEqual((result['imports'], len(result['comps'])), (1, 100))


if __name__ == '__main__':
    unittest.main()
