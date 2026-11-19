import concurrent.futures
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mail_service import MailError, MailService


class Inbox:
    def __init__(self):
        self.items = []
        self.calls = 0
    def messages(self, since):
        self.calls += 1
        return iter(self.items)


class MailCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Inbox()
        self.service = MailService(self.inbox, Path(self.tmp.name) / 'clients.json', 'sso@example.invalid')
        self.key = self.service.add_client('student@gmail.com')
        self.now = datetime.now(timezone.utc)
        self.query = {'request_number': '#00',
                      'requested_at': (self.now - timedelta(seconds=10)).isoformat()}

    def message(self, text, **extra):
        return {'authenticated': True, 'sent_at': self.now.isoformat(), 'original_recipient': 'student@gmail.com', 'id': 'mail-1', 'from': {'emailAddress': {'address': 'sso@example.invalid'}},
                'subject': 'Подтверждение входа', 'body': {'contentType': 'text', 'content': text},
                'receivedDateTime': self.now.isoformat(), **extra}


class MailTests(MailCase):
    def test_matches_bound_recipient_attempt_and_time_returns_only_otp(self):
        self.inbox.items = [self.message('Иванов Иван Иванович, запрос #00. Код: 012345')]
        result = self.service.find(self.key, self.query)
        self.assertEqual(result, {'code': '012345', 'request_number': '#00'})
        self.assertNotIn(self.key, Path(self.service.clients_path).read_text())

    def test_missing_or_delayed_mail_is_pending_and_next_poll_finds_it(self):
        self.assertIsNone(self.service.find(self.key, self.query))
        self.inbox.items = [self.message('Иванов Иван Иванович #00 Код: 111111')]
        self.assertEqual(self.service.find(self.key, self.query)['code'], '111111')

    def test_other_person_attempt_old_time_and_sender_are_excluded(self):
        self.inbox.items = [
            self.message('Любое ФИО #00 Код: 123456', original_recipient='other@gmail.com'),
            self.message('Иванов Иван Иванович #01 Код: 123456', id='mail-2'),
            self.message('Иванов Иван Иванович #000 Код: 123456', id='mail-3'),
            self.message('Иванов Иван Иванович #00 Код: 123456', id='mail-4',
                         receivedDateTime=(self.now - timedelta(minutes=1)).isoformat()),
            self.message('Иванов Иван Иванович #00 Код: 123456', id='mail-5',
                         **{'from': {'emailAddress': {'address': 'other@example.invalid'}}})]
        self.assertIsNone(self.service.find(self.key, self.query))

    def test_client_cannot_choose_another_recipient(self):
        with self.assertRaises(PermissionError):
            self.service.find('unknown-connection-key', self.query)
        with self.assertRaises(ValueError):
            self.service.find(self.key, {**self.query, 'recipient': 'other@gmail.com'})
        self.assertEqual(self.inbox.calls, 0)

    def test_forwarded_html_and_ambiguity(self):
        self.inbox.items = [self.message('', **{'from': {'emailAddress': {'address': 'sso@example.invalid'}},
            'body': {'contentType': 'HTML', 'content': '<p>От: sso@example.invalid</p><p>Иванов Иван Иванович #00</p><p>Код: <b>012345</b></p>'}})]
        self.assertEqual(self.service.find(self.key, self.query)['code'], '012345')
        self.inbox.items.append(self.message('Иванов Иван Иванович #00 Код: 999999', id='mail-2'))
        with self.assertRaises(MailError):
            self.service.find(self.key, self.query)

    def test_invalid_and_unbounded_queries_rejected(self):
        for changes in [{'requested_at': (self.now - timedelta(days=1)).isoformat()},
                        {'request_number': '#0'}, {'extra': 'list-all-mail'}, {'full_name': None}]:
            with self.assertRaises((ValueError, PermissionError)):
                self.service.find(self.key, {**self.query, **changes})

    def test_registration_is_serialized_and_persistent(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            keys = list(pool.map(lambda i: self.service.add_client(f'student{i}@gmail.com'), range(8)))
        clients = json.loads(self.service.clients_path.read_text())["clients"]
        self.assertEqual(len(clients), 9)
        self.assertEqual(len(set(keys)), 8)


if __name__ == '__main__':
    unittest.main()
