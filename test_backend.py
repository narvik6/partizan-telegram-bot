import base64
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from datetime import datetime, timezone

from backend import api_handler
from bot import Server
from mail_service import MailService
from test_bot import SharedCase
from test_mail import Inbox


class BackendAPITests(SharedCase):
    def setUp(self):
        super().setUp()
        self.inbox = Inbox()
        self.mail = MailService(self.inbox, self.path.parent / 'clients.json', 'sso@example.invalid')
        self.mail_key = self.mail.add_client('student@gmail.com')
        self.server = Server(('127.0.0.1', 0), api_handler(self.store, None, self.mail))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        super().tearDown()
    def post(self, path, body, key='', client=''):
        if ' ' in key:
            key = 'phrase.' + base64.urlsafe_b64encode(key.encode()).decode().rstrip('=')
        request = Request(f'http://127.0.0.1:{self.server.server_port}' + path,
            json.dumps(body).encode(), {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key, 'X-Partizan-Client': client})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)
    def test_manual_enrollment_over_http_without_manual_key(self):
        now = datetime.now(timezone.utc).isoformat()
        self.inbox.items = [{'authenticated': True, 'sent_at': datetime.now(timezone.utc).isoformat(), 'original_recipient': 'student@gmail.com', 'id': 'manual-proof', 'from': {'emailAddress': {'address': 'sso@example.invalid'}},
            'subject': 'Иванов Иван Иванович #00', 'body': {'content': 'Код: 012345'},
            'receivedDateTime': now}]
        query = {'request_number': '#00', 'requested_at': now}
        proof = {**query, 'action': 'connect', 'username': 'student@edu.mirea.ru',
                 'code': '012345', 'device_id': 'fixture-installation-device-id-12345678'}
        status, result = self.post('/', proof)
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertNotIn('code', result)
        self.assertEqual(self.post('/', proof)[1], result)
        self.assertEqual(self.post('/', query, result['connection_token'])[1]['code'], '012345')
    def test_root_mail_lookup_and_telegram_disabled(self):
        query = {'request_number': '#00',
                 'requested_at': datetime.now(timezone.utc).isoformat()}
        self.assertEqual(self.post('/', query, self.mail_key), (202, {'ok': True, 'state': 'pending'}))
        self.assertEqual(self.post('/', query, 'invalid-key-but-long-enough')[0], 401)
        self.inbox.items = [{'authenticated': True, 'sent_at': datetime.now(timezone.utc).isoformat(), 'original_recipient': 'student@gmail.com', 'id': 'one', 'from': {'emailAddress': {'address': 'sso@example.invalid'}},
            'subject': 'Иванов Иван Иванович #00', 'body': {'content': 'Код: 012345'},
            'receivedDateTime': datetime.now(timezone.utc).isoformat()}]
        status, response = self.post('/', query, self.mail_key)
        self.assertEqual((status, response), (200, {'ok': True, 'code': '012345', 'request_number': '#00'}))
        self.assertEqual(self.post('/partizan_agent', {}, self.mail_key)[0], 503)
    def test_optional_telegram_pairs_then_publishes_on_short_route(self):
        self.server.RequestHandlerClass = api_handler(self.store, self.telegram, self.mail)
        pairing = self.store.issue_pairing(123)
        client = 'test-client-identifier-1234567890123456'
        status, linked = self.post('/partizan_agent/connect', {'phrase':pairing['phrase'],'client_id':client})
        self.assertEqual(status,200)
        event = {'event_id':'qr-1','kind':'attendance_required','title':'Контроль','body':'QR','scope':'a'*64}
        self.assertEqual(self.post('/partizan_agent',event,linked['token'],client)[0],200)
        self.assertEqual(self.telegram.messages,[(123,'Контроль\n\nQR')])
        self.assertEqual(self.post('/v1/attendance',event,linked['token'],client)[0],200)
        self.assertEqual(len(self.telegram.messages),1)
        self.assertEqual(self.post('/',{'request_number':'#00'},linked['token'])[0],401)

    def test_phrase_cannot_publish_and_second_client_cannot_redeem_it(self):
        self.server.RequestHandlerClass = api_handler(self.store,self.telegram,self.mail)
        pairing = self.store.issue_pairing(123)
        self.assertEqual(self.post('/partizan_agent/connect',{'phrase':pairing['phrase'],'client_id':'a'*32})[0],200)
        self.assertEqual(self.post('/partizan_agent/connect',{'phrase':pairing['phrase'],'client_id':'b'*32})[0],401)
        self.assertEqual(self.post('/partizan_agent',{},pairing['phrase'],'a'*32)[0],401)
