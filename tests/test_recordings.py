import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

import after_effects
import recordings
import server as app


class TestRecordingFolders(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.output_patch = mock.patch.object(app, 'OUTPUT_DIR', self.root)
        self.output_patch.start()
        app.JOBS.clear()
        self.config = app.validate_payload({
            'api_key': 'test-key', 'voice_id': 'test_voice_12345', 'voice_name': 'Narrator',
            'name': 'Garden stories', 'episode_id': '01', 'text': 'Hello world.',
            'sample_rate': 24000,
        })

    def tearDown(self):
        self.output_patch.stop()
        app.JOBS.clear()
        self.temp.cleanup()

    def test_readable_takes_preserve_previous_files_and_survive_reload(self):
        first = app.create_job(self.config)
        first_dir = app.job_directory(first)
        original = first_dir / 'transcript.txt'
        original.write_text('Original take')
        app.JOBS.clear()
        second = app.create_job(self.config)
        self.assertEqual(first_dir.relative_to(self.root).parts,
                         ('recordings', 'Garden stories', 'Episode 01', 'Voiceover - Narrator', 'Take 01'))
        self.assertEqual(app.job_directory(second), first_dir.with_name('Take 02'))
        self.assertEqual(original.read_text(), 'Original take')
        self.assertEqual(app.read_job(first)['name'], 'Garden stories')
        self.assertEqual({j['id'] for j in app.load_history()}, {first, second})
        self.assertFalse((self.root / first).exists())

    def test_messages_videos_and_shared_clips_are_identifiable(self):
        cases = [({'turn_number': 2, 'speaker': 3}, 'Message 002 - Voice 3 - Narrator'),
                 ({'video_id': 'VIDEO_001', 'segment_id': 'narration'}, 'VIDEO_001 - narration - Narrator'),
                 ({'video_id': 'VIDEO_001', 'segment_id': 'intro', 'static_key': 'intro'}, 'Shared - intro - Narrator')]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                job_id = app.create_job({**self.config, **extra})
                self.assertEqual(app.job_directory(job_id).parent.name, expected)

    def test_names_handle_unicode_reserved_names_and_traversal(self):
        job_id = app.create_job({**self.config, 'name': '../../Привіт / garden',
                                 'episode_id': '', 'voice_name': 'CON'})
        path = app.job_directory(job_id)
        self.assertTrue(path.resolve().is_relative_to(self.root))
        self.assertEqual(path.relative_to(self.root).parts,
                         ('recordings', 'Привіт - garden', 'No episode', 'Voiceover - _CON', 'Take 01'))
        for value in ('../escape', '', None, 'a' * 31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                recordings.directory_for(self.root, value)

    def test_legacy_folders_are_read_and_updated_without_moving(self):
        job_id = 'a' * 32
        directory = self.root / job_id
        directory.mkdir()
        (directory / 'metadata.json').write_text(json.dumps({
            'id': job_id, 'name': 'Old project', 'status': 'complete', 'has_wav': True,
        }))
        self.assertEqual(app.read_job(job_id)['name'], 'Old project')
        app.JOBS[job_id] = app.read_job(job_id)
        app.persist_job(job_id)
        self.assertEqual(app.job_directory(job_id), directory)
        self.assertFalse((self.root / 'recordings').exists())
        self.assertEqual(app.load_history()[0]['id'], job_id)

    def test_symlink_cannot_move_new_recordings_outside_history(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / 'recordings').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Directory symlinks are unavailable')
            with self.assertRaises(OSError):
                app.create_job(self.config)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_generation_reload_playback_downloads_and_ae_export(self):
        aligned = {'words': [{'text': 'Hello', 'start': 0, 'end': .5}]}
        with mock.patch.object(app, 'api_request', return_value=b'\0\0' * 24000), \
                mock.patch.object(app, 'forced_alignment', return_value=aligned):
            job_id = app.create_job(self.config)
            self.assertTrue(app.GENERATE_LOCK.acquire(blocking=False))
            app.run_generation(job_id, self.config)
        self.assertEqual(app.JOBS[job_id]['status'], 'complete')
        app.JOBS.clear()
        http = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{http.server_port}'
        try:
            for path in (f'/api/jobs/{job_id}', f'/api/media/{job_id}/wav',
                         f'/api/download/{job_id}/wav', f'/api/download/{job_id}/alignment',
                         f'/api/download/{job_id}/transcript'):
                with self.subTest(path=path), urllib.request.urlopen(url + path) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
        finally:
            http.shutdown()
            http.server_close()
        manifest = after_effects.build_manifest('Garden stories', 'single',
                                               [{'job': app.read_job(job_id)}], self.root, self.root, {})
        self.assertEqual(Path(manifest['assets'][0]['path']).parent, app.job_directory(job_id))
        self.assertEqual(manifest['assets'][0]['words'], aligned['words'])
