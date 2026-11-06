import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

import server as app
import conversation
import production


class TestParsing(unittest.TestCase):
    def test_blank_paragraphs_alternate_not_single_newlines(self):
        turns = conversation.parse_script('Hello\nStill me\n \nHi!\n\n\nBack to me')
        self.assertEqual([t['speaker'] for t in turns], [1, 2, 1])
        self.assertEqual(turns[0]['text'], 'Hello\nStill me')
        self.assertEqual([t['number'] for t in turns], [1, 2, 3])

    def test_numbered_turns_preserve_order_without_blank_lines(self):
        turns = conversation.parse_script('1.Hello\n2. Привіт\n1. Welcome\n3. Thanks')
        self.assertEqual([t['speaker'] for t in turns], [1, 2, 1, 3])
        self.assertEqual([t['text'] for t in turns], ['Hello', 'Привіт', 'Welcome', 'Thanks'])

    def test_numbered_multiline_and_windows_line_endings(self):
        turns = conversation.parse_script(' 1. First\r\ncontinued\r\n\r\n3. Last\r\n')
        self.assertEqual([t['text'] for t in turns], ['First\ncontinued', 'Last'])
        self.assertEqual(turns[1]['speaker'], 3)

    def test_invalid_and_mixed_labels_are_not_silently_spoken(self):
        for text in ('1. Hi\n4. No', 'Intro\n1. Hi', '1. Hi\n\nUnlabeled', '1.\n2. Hello', '', 'x' * 10001):
            with self.subTest(text=text[:50]), self.assertRaises(ValueError):
                conversation.parse_script(text)


class TestConversation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_output = app.OUTPUT_DIR
        app.OUTPUT_DIR = Path(self.temp.name) / 'history'
        app.OUTPUT_DIR.mkdir()
        self.destination = Path(self.temp.name) / 'chosen'
        self.destination.mkdir()
        self.token = conversation.register_destination(str(self.destination))['token']
        app.JOBS.clear()
        app.BATCHES.clear()
        self.calls = []
        self.api_patch = mock.patch.object(app, 'api_request', side_effect=self.fake_api)
        self.api_patch.start()

    def tearDown(self):
        self.api_patch.stop()
        app.JOBS.clear()
        app.BATCHES.clear()
        conversation.DESTINATIONS.clear()
        app.OUTPUT_DIR = self.original_output
        self.temp.cleanup()

    def fake_api(self, path, key, data=None, content_type=None, timeout=0):
        self.calls.append((path, data))
        if 'text-to-speech' in path:
            return b'\0\0' * 2400
        return json.dumps({'words': [], 'characters': []}).encode()

    def request(self):
        return {'api_key': 'test-secret', 'name': 'Dialogue', 'episode_id': 'ep1',
                'destination_token': self.token,
                'profiles': [{'voice_id': f'voice_test_{n}', 'model_id': 'eleven_multilingual_v2',
                              'sample_rate': 24000, 'settings': {'stability': n / 10}} for n in (1, 2, 3)],
                'turns': conversation.parse_script('1. Hello\n2. Hi\n1. Welcome\n3. Thanks')}

    def run_batch(self, request, batch_id=None):
        # Run worker synchronously while preserving the endpoint's lock lifecycle.
        with mock.patch.object(app.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = lambda: app.run_batch(thread.call_args.kwargs['args'][0])
            return app.start_batch(request, batch_id)

    def test_batch_uses_each_voice_settings_and_exports_in_order(self):
        request = self.request()
        request['profiles'][1]['model_id'] = 'eleven_flash_v2_5'
        request['profiles'][2]['sample_rate'] = 44100
        batch_id = self.run_batch(request)
        batch = app.public_batch(app.read_batch(batch_id))
        self.assertFalse(batch['working'])
        tts = [(path, json.loads(data)) for path, data in self.calls if 'text-to-speech' in path]
        self.assertEqual([p['text'] for _, p in tts], ['Hello', 'Hi', 'Welcome', 'Thanks'])
        self.assertEqual([p['voice_settings']['stability'] for _, p in tts], [.1, .2, .1, .3])
        self.assertIn('voice_test_3', tts[3][0])
        self.assertIn('pcm_44100', tts[3][0])
        self.assertEqual(tts[1][1]['model_id'], 'eleven_flash_v2_5')
        self.assertEqual(sorted(p.name for p in self.destination.iterdir()), [
            'dialogue_ep1_001_voice_1.wav', 'dialogue_ep1_002_voice_2.wav',
            'dialogue_ep1_003_voice_1.wav', 'dialogue_ep1_004_voice_3.wav'])
        for file in app.OUTPUT_DIR.rglob('*.json'):
            self.assertNotIn('test-secret', file.read_text())
        self.assertNotIn(str(self.destination), json.dumps(batch))

    def test_regeneration_only_changes_one_message_and_keeps_previous_take(self):
        request = self.request()
        batch_id = self.run_batch(request)
        before = app.read_batch(batch_id)
        old_audio = (self.destination / 'dialogue_ep1_002_voice_2.wav').read_bytes()
        self.calls.clear()
        self.run_batch({**request, 'number': 2, 'speaker': 2, 'text': 'New line'}, batch_id)
        after = app.read_batch(batch_id)
        self.assertEqual(len(self.calls), 2)
        for index in (0, 2, 3):
            self.assertEqual(before['turns'][index]['job_id'], after['turns'][index]['job_id'])
        self.assertEqual(after['turns'][1]['previous_job_id'], before['turns'][1]['job_id'])
        self.assertEqual((self.destination / 'dialogue_ep1_002_voice_2.wav').read_bytes(), old_audio)
        self.assertTrue((self.destination / 'dialogue_ep1_002_voice_2_take_02.wav').is_file())

    def test_failed_regeneration_retains_playable_previous_message(self):
        request = self.request()
        batch_id = self.run_batch(request)
        with mock.patch.object(app, 'api_request', side_effect=app.ApiError(503, 'Provider unavailable')):
            self.run_batch({**request, 'number': 1, 'speaker': 1, 'text': 'Try again'}, batch_id)
        turn = app.public_batch(app.read_batch(batch_id))['turns'][0]
        self.assertEqual(turn['job']['status'], 'failed')
        self.assertTrue(turn['previous_job']['has_wav'])
        self.assertEqual(len(list(self.destination.glob('*.wav'))), 4)

    def test_all_used_profiles_validated_before_any_paid_requests(self):
        request = self.request()
        request['profiles'][2]['voice_id'] = ''
        with self.assertRaises(app.ApiError):
            self.run_batch(request)
        self.assertEqual(self.calls, [])
        self.assertEqual(app.JOBS, {})
        request['turns'] = request['turns'][:3]
        self.run_batch(request)  # Unused Voice 3 does not need an ID.
        self.assertEqual(len(self.calls), 6)

    def test_folder_required_and_rechecked_before_generation(self):
        request = self.request()
        for value in ('', 'missing', '../escape'):
            with self.subTest(value=value), self.assertRaises(app.ApiError):
                self.run_batch({**request, 'destination_token': value})
        self.assertEqual(self.calls, [])
        self.destination.rmdir()
        with self.assertRaises(app.ApiError):
            self.run_batch(request)

    def test_folder_picker_selection_and_cancel(self):
        with mock.patch.object(conversation.sys, 'platform', 'darwin'):
            result = mock.Mock(returncode=0, stdout=str(self.destination) + '\n')
            with mock.patch.object(conversation.subprocess, 'run', return_value=result):
                selected = conversation.choose_folder()
            self.assertEqual(conversation.destination_for(selected['token']), self.destination.resolve())
            result = mock.Mock(returncode=1, stderr='User canceled. (-128)')
            with mock.patch.object(conversation.subprocess, 'run', return_value=result):
                self.assertIsNone(conversation.choose_folder())

    def test_regeneration_keeps_total_conversation_length_limit(self):
        request = self.request()
        batch_id = self.run_batch(request)
        self.calls.clear()
        with self.assertRaises(app.ApiError):
            self.run_batch({**request, 'number': 1, 'speaker': 1, 'text': 'x' * 10000}, batch_id)
        self.assertEqual(self.calls, [])

    def test_csv_batch_folders_sidecars_and_retry_pass(self):
        request = self.request()
        request['mode'] = 'batch'
        request['turns'] = production.parse_csv('video_id,topic,what_is_it,how_to_use_it,cake_explanation\nVIDEO_001,Title,Definition,Usage,Cake\nVIDEO_002,Title 2,Definition 2,Usage 2,Cake 2')
        batch_id = self.run_batch(request)
        root = self.destination / 'audio' / 'candidates' / 'pass_01_initial'
        self.assertEqual(len(list(root.glob('*/*.wav'))), 8)
        self.assertTrue((root / 'VIDEO_001' / 'VIDEO_001_topic.candidate.json').is_file())
        self.assertTrue((root / 'VIDEO_001' / 'VIDEO_001_topic.alignment.json').is_file())
        self.assertTrue((root / 'VIDEO_001' / 'VIDEO_001_topic.words.tsv').is_file())
        self.assertEqual((self.destination / 'scripts' / 'VIDEO_001' / 'VIDEO_001_topic.txt').read_text(), 'Title\n')
        self.calls.clear()
        self.run_batch({**request, 'number': 2, 'speaker': 1, 'text': 'Revised definition'}, batch_id)
        retry = self.destination / 'audio' / 'candidates' / 'pass_02_retry' / 'VIDEO_001'
        self.assertEqual([p.name for p in retry.glob('*.wav')], ['VIDEO_001_what.wav'])
        self.assertEqual((retry / 'VIDEO_001_what.txt').read_text(), 'Revised definition\n')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(list(root.glob('*/*.wav'))), 8)
        for file in self.destination.rglob('*.json'):
            self.assertNotIn('test-secret', file.read_text())

    def test_single_mode_saves_audio_and_alignment_to_destination(self):
        request = self.request()
        config = app.validate_payload({**request['profiles'][0], 'api_key': request['api_key'], 'text': 'A single voice.', 'name': 'Solo'})
        config.update(destination=self.destination, export_alignment=True)
        self.assertTrue(app.GENERATE_LOCK.acquire(blocking=False))
        job_id = app.create_job(config)
        app.run_generation(job_id, config)
        self.assertTrue((self.destination / 'solo.wav').is_file())
        self.assertTrue((self.destination / 'solo_alignment.json').is_file())

    def test_static_recording_is_generated_once_exported_once_and_reused_after_reload(self):
        request = self.request()
        request.update(mode='batch', turns=production.parse_csv(
            'video_id,intro,narration,outro\nONE,Hello,First,Bye\nTWO,,Second,', static_columns=['intro', 'outro']))
        batch_id = self.run_batch(request)
        batch = app.read_batch(batch_id)
        self.assertEqual(len(self.calls), 8)  # Four TTS calls and four alignments.
        self.assertEqual(batch['turns'][0]['job_id'], batch['turns'][3]['job_id'])
        self.assertEqual(batch['turns'][2]['job_id'], batch['turns'][5]['job_id'])
        self.assertEqual(len(list(self.destination.rglob('*.wav'))), 4)
        shared = self.destination / 'audio' / 'shared' / 'pass_01_initial'
        self.assertEqual(sorted(p.name for p in shared.glob('*.wav')), ['intro.wav', 'outro.wav'])
        details = json.loads((shared / 'intro.candidate.json').read_text())
        self.assertEqual(details['used_by'], ['ONE', 'TWO'])
        self.assertEqual(details['static_key'], 'intro')
        self.assertTrue((shared / 'intro.alignment.json').is_file())
        self.assertTrue((shared / 'intro.words.tsv').is_file())
        app.JOBS.clear(); app.BATCHES.clear()
        restored = app.public_batch(app.read_batch(batch_id))
        self.assertEqual(restored['turns'][3]['static_key'], 'intro')
        self.assertTrue(restored['turns'][3]['job']['has_wav'])
        self.calls.clear()
        self.run_batch({**request, 'number': 4, 'speaker': 1, 'text': 'New shared intro'}, batch_id)
        after = app.public_batch(app.read_batch(batch_id))
        self.assertEqual(len(self.calls), 2)
        for i in (0, 3):
            self.assertEqual(after['turns'][i]['text'], 'New shared intro')
            self.assertEqual(after['turns'][i]['previous_job']['id'], batch['turns'][0]['job_id'])
        self.assertEqual(after['turns'][0]['job_id'], after['turns'][3]['job_id'])
        for i in (1, 2, 4, 5):
            self.assertEqual(after['turns'][i]['job_id'], batch['turns'][i]['job_id'])
        self.assertEqual(len(list(self.destination.rglob('*.wav'))), 5)
        self.assertTrue((self.destination / 'audio' / 'shared' / 'pass_02_retry' / 'intro.wav').is_file())
        with mock.patch.object(app, 'api_request', side_effect=app.ApiError(503, 'Provider unavailable')):
            self.run_batch({**request, 'number': 1, 'speaker': 1, 'text': 'Try again'}, batch_id)
        failed = app.public_batch(app.read_batch(batch_id))
        for i in (0, 3):
            self.assertEqual(failed['turns'][i]['job']['status'], 'failed')
            self.assertEqual(failed['turns'][i]['previous_job']['id'], after['turns'][i]['job_id'])
            self.assertTrue(failed['turns'][i]['previous_job']['has_wav'])

    def test_batch_alignment_retry_exports_timing_without_new_speech(self):
        request = self.request()
        request.update(mode='batch', turns=production.parse_csv('video_id,segment_id,text\nVIDEO_001,topic,Title'))
        def failing_alignment(path, *args, **kwargs):
            if 'text-to-speech' in path:
                return b'\0\0' * 2400
            raise app.ApiError(503, 'Temporary timing outage')
        with mock.patch.object(app, 'api_request', side_effect=failing_alignment):
            batch_id = self.run_batch(request)
        job_id = app.read_batch(batch_id)['turns'][0]['job_id']
        self.assertTrue(app.GENERATE_LOCK.acquire(blocking=False))
        app.run_retry(job_id, request['api_key'])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.destination / 'audio' / 'candidates' / 'pass_01_initial' / 'VIDEO_001' / 'VIDEO_001_topic.alignment.json').is_file())

    def test_batch_rejects_output_symlink_that_escapes_destination(self):
        if not hasattr(__import__('os'), 'symlink'):
            self.skipTest('Symlinks unavailable')
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        try:
            (self.destination / 'audio').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks requires additional permissions on this platform')
        request = self.request()
        request.update(mode='batch', turns=production.parse_csv('video_id,segment_id,text\nVIDEO_001,topic,Title'))
        batch_id = self.run_batch(request)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(app.public_batch(app.read_batch(batch_id))['turns'][0]['job']['export_error'])

    def test_destination_failure_keeps_local_audio_and_reports_it(self):
        with mock.patch.object(conversation, 'export_wav', side_effect=PermissionError('no access')):
            batch_id = self.run_batch(self.request())
        batch = app.public_batch(app.read_batch(batch_id))
        self.assertTrue(all(t['job']['has_wav'] and t['job']['export_error'] for t in batch['turns']))
        self.assertFalse(batch['working'])

    def test_alignment_failure_does_not_lose_export(self):
        def fake(path, *args, **kwargs):
            if 'text-to-speech' in path:
                return b'\0\0' * 2400
            raise app.ApiError(503, 'Timing unavailable')
        with mock.patch.object(app, 'api_request', side_effect=fake):
            batch_id = self.run_batch(self.request())
        batch = app.public_batch(app.read_batch(batch_id))
        self.assertTrue(all(t['job']['status'] == 'partial' and t['job']['export_filename'] for t in batch['turns']))
        self.assertEqual(len(list(self.destination.glob('*.wav'))), 4)

    def test_reload_restores_conversation_and_interrupted_jobs_are_retryable(self):
        batch_id = self.run_batch(self.request())
        job_id = app.read_batch(batch_id)['turns'][0]['job_id']
        app.update_job(job_id, status='working')
        app.persist_job(job_id)
        app.JOBS.clear()
        app.BATCHES.clear()
        batch = app.public_batch(app.read_batch(batch_id))
        self.assertEqual(batch['turns'][0]['text'], 'Hello')
        self.assertEqual(batch['turns'][0]['job']['status'], 'partial')
        self.assertFalse(batch['working'])

    def test_conversation_http_validation_and_origin_protection(self):
        http = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{http.server_port}'
        def post(path, body, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            return urllib.request.urlopen(urllib.request.Request(root + path, data=json.dumps(body).encode(), headers=headers))
        try:
            with post('/api/prepare', {'text': 'Hello\n\nHi'}) as res:
                self.assertEqual([t['speaker'] for t in json.load(res)['turns']], [1, 2])
            with self.assertRaises(urllib.error.HTTPError) as error:
                post('/api/prepare', {'text': '1. Hi\n4. Bad'})
            self.assertEqual(error.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as error:
                post('/api/destination', {'path': str(self.destination)}, 'https://untrusted.example')
            self.assertEqual(error.exception.code, 403)
            with post('/api/destination', {'path': str(self.destination)}) as res:
                self.assertTrue(json.load(res)['destination']['token'])
            config = self.request()['profiles'][0]
            with self.assertRaises(urllib.error.HTTPError) as error:
                post('/api/generate', {**config, 'api_key': 'test-secret', 'text': 'Single voice without a destination'})
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.calls, [])
            with post('/api/prepare-csv', {'text': 'video_id,segment_id,text\nVIDEO_001,topic,Title'}) as res:
                data = json.load(res)
                self.assertEqual(data['videos'], 1)
                self.assertEqual(data['turns'][0]['segment_id'], 'topic')
            source = 'video_id,intro,body\nONE,Hello,First\nTWO,,Second'
            with post('/api/inspect-csv', {'text': source}) as res:
                self.assertEqual([c['id'] for c in json.load(res)['columns']], ['intro', 'body'])
            with post('/api/prepare-csv', {'text': source, 'static_columns': ['intro']}) as res:
                data = json.load(res)
                self.assertEqual(data['recordings'], 3)
                self.assertEqual(data['turns'][2]['text'], 'Hello')
        finally:
            http.shutdown()
            http.server_close()


if __name__ == '__main__':
    unittest.main()
