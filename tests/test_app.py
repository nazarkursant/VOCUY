import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as app


class TestApp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_output = app.OUTPUT_DIR
        self.original_api = app.api_request
        app.OUTPUT_DIR = Path(self.temp.name)
        app.JOBS.clear()

    def tearDown(self):
        app.OUTPUT_DIR = self.original_output
        app.api_request = self.original_api
        app.JOBS.clear()
        self.temp.cleanup()

    def config(self):
        return app.validate_payload({
            'api_key':'secure-test-key', 'voice_id':'test_voice_12345',
            'name':'Sample · Episode 01', 'episode_id':'SCENE_01',
            'voice_name':'Test narrator', 'text':'Imagine: Привіт! Sample voiceover.',
            'model_id':'eleven_multilingual_v2', 'sample_rate':24000,
            'settings':{'stability':.5,'similarity_boost':.75,'style':0,
                        'speed':1, 'use_speaker_boost':True}
        })

    def create_job(self, config):
        job_id = 'a' * 32
        app.JOBS[job_id] = {
            'id':job_id,'name':config['name'],'episode_id':config['episode_id'],
            'voice_id':config['voice_id'],'voice_name':config['voice_name'],
            'model_id':config['model_id'],'sample_rate':config['sample_rate'],
            'created_at':'2025-01-01T12:00:00+00:00','status':'working','step':'Queued',
            'error':'','has_wav':False,'has_alignment':False,'duration':None,'word_count':None
        }
        self.assertTrue(app.GENERATE_LOCK.acquire(blocking=False))
        return job_id

    def test_generation_and_forced_alignment(self):
        config=self.config()
        job_id=self.create_job(config)
        requests=[]
        def fake_api(path,key,data=None,content_type=None,timeout=0):
            self.assertEqual(key,'secure-test-key')
            requests.append(path)
            if 'text-to-speech' in path:
                self.assertEqual('application/json',content_type)
                payload=json.loads(data)
                self.assertEqual(payload['text'], config['text'])
                self.assertEqual(payload['voice_settings']['stability'], .5)
                return b'\0\0'*48000
            if '/forced-alignment' in path:
                self.assertIn(config['text'].encode('utf8'),data)
                self.assertIn(b'RIFF',data)
                self.assertIn('multipart/form-data',content_type)
                return json.dumps({'words':[{'text':'Imagine','start':0,'end':.5}],
                                   'characters':[],'loss':.2}).encode()
            raise AssertionError(path)
        app.api_request=fake_api
        app.run_generation(job_id,config)
        item=app.JOBS[job_id]
        self.assertEqual(item['status'],'complete')
        self.assertEqual(item['word_count'],1)
        self.assertAlmostEqual(item['duration'],2)
        self.assertEqual(len(requests),2)
        directory=app.OUTPUT_DIR/job_id
        wav=list(directory.glob('*.wav'))[0]
        with wave.open(str(wav),'rb') as wf:
            self.assertEqual(wf.getnframes(),48000)
            self.assertEqual(wf.getframerate(),24000)
            self.assertEqual(wf.getsampwidth(),2)
            self.assertEqual(wf.getnchannels(),1)
        alignment=list(directory.glob('*_alignment.json'))[0]
        self.assertEqual(json.loads(alignment.read_text())['words'][0]['text'],'Imagine')
        self.assertNotIn('secure-test-key',str(directory.joinpath('metadata_internal.json').read_text()))
        self.assertNotIn('secure-test-key',str(directory.joinpath('metadata.json').read_text()))
        self.assertEqual(app.load_history()[0]['id'],job_id)

    def test_partial_generation_and_retry(self):
        config=self.config()
        job_id=self.create_job(config)
        state={'align':0, 'tts':0}
        def fake_api(path,key,data=None,content_type=None,timeout=0):
            if 'text-to-speech' in path:
                state['tts']+=1
                return b'\0\0'*24000
            if 'forced-alignment' in path:
                state['align']+=1
                if state['align']==1: raise app.ApiError(503,'Temporary alignment outage')
                return json.dumps({'words':[],'characters':[],'loss':0}).encode()
            raise AssertionError(path)
        app.api_request=fake_api
        app.run_generation(job_id, config)
        self.assertEqual(app.JOBS[job_id]['status'],'partial')
        self.assertTrue(app.JOBS[job_id]['has_wav'])
        self.assertFalse(app.JOBS[job_id]['has_alignment'])
        self.assertTrue(app.GENERATE_LOCK.acquire(blocking=False))
        app.run_retry(job_id,config['api_key'])
        self.assertEqual(app.JOBS[job_id]['status'],'complete')
        self.assertEqual(state['tts'],1)
        self.assertEqual(state['align'],2)

    def test_validation(self):
        good=self.config()
        self.assertEqual(good['settings']['speed'],1.0)
        with self.assertRaises(app.ApiError):
            app.validate_payload({**good,'voice_id':'../secret'})
        with self.assertRaises(app.ApiError):
            app.validate_payload({**good,'sample_rate':96000})
        with self.assertRaises(app.ApiError):
            app.validate_payload({**good,'text':'x'*10001})

    def test_provider_errors_redact_key_before_truncation(self):
        key = 'test-only-private-credential'
        message = 'x' * 1090 + key
        error = urllib.error.HTTPError(
            app.API_ROOT, 401, 'Unauthorized', {},
            io.BytesIO(json.dumps({'detail': {'message': message}}).encode()))
        with mock.patch.object(app.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(app.ApiError) as ctx:
                app.api_request('/v2/voices', key)
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn(key[:10], ctx.exception.message)
        self.assertIn('[redacted]', ctx.exception.message)

    def test_alignment_errors_do_not_persist_credentials_or_paths(self):
        config = self.config()
        job_id = self.create_job(config)

        def fake_api(path, key, data=None, content_type=None, timeout=0):
            if 'text-to-speech' in path:
                return b'\0\0' * 24000
            raise app.ApiError(503, f'Failed for {key} at {app.OUTPUT_DIR}/audio.wav')

        app.api_request = fake_api
        app.run_generation(job_id, config)
        self.assertEqual(app.JOBS[job_id]['status'], 'partial')
        self.assertTrue(app.GENERATE_LOCK.acquire(blocking=False))
        app.run_retry(job_id, config['api_key'])
        self.assertEqual(app.JOBS[job_id]['status'], 'partial')
        for file in (app.OUTPUT_DIR / job_id).glob('*.json'):
            saved = file.read_text()
            self.assertNotIn(config['api_key'], saved)
            self.assertNotIn(str(app.OUTPUT_DIR), saved)
        self.assertIn('[redacted]', app.JOBS[job_id]['error'])

    def test_unexpected_errors_do_not_persist_credentials_or_paths(self):
        config = self.config()
        job_id = self.create_job(config)

        def fake_api(*args, **kwargs):
            raise OSError(f"Cannot write {app.APP_DIR}/audio.wav with {config['api_key']}")

        app.api_request = fake_api
        app.run_generation(job_id, config)
        self.assertEqual(app.JOBS[job_id]['status'], 'failed')
        saved = (app.OUTPUT_DIR / job_id / 'metadata.json').read_text()
        self.assertNotIn(config['api_key'], saved)
        self.assertNotIn(str(app.APP_DIR), saved)
        self.assertIn('OSError', saved)

    def test_http_endpoints(self):
        http=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        root=f'http://127.0.0.1:{http.server_port}'
        try:
            with urllib.request.urlopen(root+'/') as res:
                html=res.read().decode('utf8')
                self.assertIn('Generate WAV + JSON',html)
            with urllib.request.urlopen(root+'/style.css') as res:
                self.assertEqual(res.status,200)
            with urllib.request.urlopen(root+'/api/history') as res:
                self.assertEqual(json.loads(res.read())['items'],[])
            data=json.dumps({'api_key':'safe'}).encode()
            req=urllib.request.Request(root+'/api/voices', data=data, headers={
                'Content-Type':'application/json','Origin':'https://malicious.example'},method='POST')
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code,403)
        finally:
            http.shutdown();http.server_close()


if __name__=='__main__':
    unittest.main()
