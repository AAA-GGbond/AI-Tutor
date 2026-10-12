import unittest
import base64
import json
import tempfile
import threading
import io
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import server
from unittest.mock import patch
from server import KNOWLEDGE, empty_profile, merge_profile, validate_analysis


class ProfileTests(unittest.TestCase):
    def test_ocr_text_model_receives_no_images(self):
        config = {'input_mode': 'ocr_review', 'api_key': 'test', 'model': 'text-model', 'base_url': 'https://example.com/v1', 'prompt': '分析', 'knowledge': KNOWLEDGE}
        result = {'summary': '识别后分析', 'questions': [self.question()]}
        response = io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(result)}}]}).encode())
        with patch('server.urlopen', return_value=response) as call:
            server.analyze({'name': '测试', 'stage': '七年级上学期', 'profile': json.dumps(empty_profile())}, config, [{'data': 'image-secret'}], '', 'real', '第1题：学生答案 -4')
        payload = json.loads(call.call_args[0][0].data)
        self.assertIsInstance(payload['messages'][1]['content'], str)
        self.assertNotIn('image-secret', json.dumps(payload))
        self.assertIn('学生答案', payload['messages'][1]['content'])

    def question(self, number='1', confidence=.9, status='incorrect'):
        return {'number': number, 'status': status, 'knowledge_id': 'power', 'confidence': confidence, 'reason': '符号错误', 'error_type': '概念错误', 'suggestion': '复习乘方'}

    def test_multiple_questions_raise_severity_once_and_keep_evidence(self):
        initial = empty_profile()
        analysis = {'summary': '测试', 'questions': [self.question(), self.question('2')]}
        updated = merge_profile(initial, analysis, KNOWLEDGE, 'paper1', '试卷', '2026-10-09')
        self.assertEqual(updated['weaknesses']['power']['severity'], 2)
        self.assertEqual(len(updated['weaknesses']['power']['evidence']), 2)
        self.assertEqual(initial, empty_profile())
        for i in range(10):
            updated = merge_profile(updated, analysis, KNOWLEDGE, str(i), '试卷', '2026-10-09')
        self.assertEqual(updated['weaknesses']['power']['severity'], 5)

    def test_uncertain_correct_and_low_confidence_do_not_change_weakness(self):
        analysis = {'summary': '测试', 'questions': [self.question('1', .5), self.question('2', status='uncertain'), self.question('3', status='correct')]}
        updated = merge_profile(empty_profile(), analysis, KNOWLEDGE, 'p', '试卷', '2026-10-09')
        self.assertEqual(updated['weaknesses'], {})
        self.assertEqual(updated['paper_count'], 1)

    def test_unknown_knowledge_and_duplicate_numbers_rejected(self):
        q = self.question()
        q['knowledge_id'] = 'invented'
        with self.assertRaises(ValueError):
            validate_analysis({'summary': '测试', 'questions': [q]}, KNOWLEDGE)
        with self.assertRaises(ValueError):
            validate_analysis({'summary': '测试', 'questions': [self.question(), self.question()]}, KNOWLEDGE)


class FlowTests(unittest.TestCase):
    def test_http_flow_persistence_and_duplicate_protection(self):
        original_db = server.DB
        with tempfile.TemporaryDirectory() as directory, patch.dict(server.os.environ, {'ANALYSIS_MODE': 'demo'}):
            server.DB = Path(directory) / 'test.sqlite3'
            try:
                with server.connect() as db, server.sqlite3.connect(original_db) as source:
                    source.backup(db)
                http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
                thread = threading.Thread(target=http.serve_forever, daemon=True)
                thread.start()
                base = 'http://127.0.0.1:' + str(http.server_port)

                def request(path, body=None):
                    req = Request(base + path, data=json.dumps(body).encode() if body else None, headers={'Content-Type': 'application/json'})
                    with urlopen(req) as response:
                        return json.load(response)

                try:
                    request('/api/students', {'name': '流程测试学生', 'stage': '七年级上学期'})
                    student = request('/api/state')['students'][0]
                    public_state = request('/api/state')
                    self.assertNotIn('settings', public_state)
                    self.assertNotIn('model_ready', public_state)
                    self.assertIn('stages', public_state)
                    with self.assertRaises(HTTPError) as settings_error:
                        request('/api/settings', {'prompt': 'should not be editable'})
                    self.assertEqual(settings_error.exception.code, 404)
                    body = {'student_id': student['id'], 'title': '测试试卷', 'mode': 'real', 'files': [{'name': 'test.png', 'data': 'data:image/png;base64,' + base64.b64encode(b'test-image-one').decode()}]}
                    request('/api/analyze', body)
                    self.assertEqual(request('/api/state')['papers'][0]['mode'], 'demo')
                    with self.assertRaises(HTTPError) as error:
                        request('/api/analyze', body)
                    self.assertEqual(error.exception.code, 400)
                    body['files'][0]['data'] = 'data:image/png;base64,' + base64.b64encode(b'test-image-two').decode()
                    request('/api/analyze', body)
                    current = next(s for s in request('/api/state')['students'] if s['id'] == student['id'])
                    self.assertEqual(current['version'], 2)
                    self.assertEqual(current['profile']['paper_count'], 2)
                    with server.connect() as reopened:
                        persisted = json.loads(reopened.execute('SELECT profile FROM students WHERE id=?', (student['id'],)).fetchone()[0])
                    self.assertEqual(persisted, current['profile'])
                    with self.assertRaises(HTTPError) as unauthorized:
                        request('/api/admin/config')
                    self.assertEqual(unauthorized.exception.code, 401)
                    admin_headers = {'Authorization': 'Bearer ' + server.ADMIN_TOKEN, 'Content-Type': 'application/json'}
                    with urlopen(Request(base + '/api/admin/config', headers=admin_headers)) as response:
                        config = json.load(response)
                    self.assertNotIn('api_key', config)
                    config.update({'mode': 'demo', 'api_key': 'test-secret', 'prompt': '制作方新提示词'})
                    with urlopen(Request(base + '/api/admin/config', data=json.dumps(config).encode(), headers=admin_headers)) as response:
                        self.assertTrue(json.load(response)['ok'])
                    with urlopen(Request(base + '/api/admin/config', headers=admin_headers)) as response:
                        saved = json.load(response)
                    self.assertTrue(saved['key_configured'])
                    self.assertNotIn('api_key', saved)
                    self.assertEqual(saved['prompt'], '制作方新提示词')
                    self.assertNotIn('制作方新提示词', json.dumps(request('/api/state'), ensure_ascii=False))
                    config['mode'] = 'manual'
                    with urlopen(Request(base + '/api/admin/config', data=json.dumps(config).encode(), headers=admin_headers)):
                        pass
                    body['files'][0]['data'] = 'data:image/png;base64,' + base64.b64encode(b'manual-paper').decode()
                    queued = request('/api/analyze', body)
                    self.assertTrue(queued['pending'])
                    self.assertEqual(len(request('/api/state')['pending']), 1)
                    with urlopen(Request(base + '/api/admin/jobs', headers=admin_headers)) as response:
                        job = json.load(response)['jobs'][0]
                    self.assertIn('制作方新提示词', job['material'])
                    self.assertNotIn('config', job)
                    invalid = {'id': queued['job_id'], 'action': 'import', 'analysis': {'summary': '坏结果', 'questions': [{'number': '1'}]}}
                    with self.assertRaises(HTTPError):
                        urlopen(Request(base + '/api/admin/job', data=json.dumps(invalid).encode(), headers=admin_headers))
                    result = {'id': queued['job_id'], 'action': 'import', 'analysis': {'summary': '手动分析', 'questions': [ProfileTests().question()]}}
                    with urlopen(Request(base + '/api/admin/job', data=json.dumps(result).encode(), headers=admin_headers)):
                        pass
                    with self.assertRaises(HTTPError):
                        urlopen(Request(base + '/api/admin/job', data=json.dumps(result).encode(), headers=admin_headers))
                    self.assertEqual(request('/api/state')['pending'], [])
                    self.assertEqual(next(s for s in request('/api/state')['students'] if s['id'] == student['id'])['version'], 3)
                finally:
                    http.shutdown()
                    http.server_close()
                    thread.join()
            finally:
                server.DB = original_db


if __name__ == '__main__':
    unittest.main()
