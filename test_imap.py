import unittest
from unittest.mock import patch
from email.message import EmailMessage
from imap_mail import IMAPInbox


class Connection:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.ssl = kwargs['ssl_context']
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def login(self, username, password): self.calls.append(('login', username, password))
    def select(self, name, readonly=False):
        self.calls.append(('select', name, readonly)); return 'OK', [b'1']
    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == 'search': return 'OK', [b'42']
        message = EmailMessage()
        message['To'] = 'Student <student@gmail.com>'
        message['From'] = 'SSO <sso@example.invalid>'
        message['Subject'] = 'Иванов Иван Иванович #00'
        message.set_content('Код: 012345')
        return 'OK', [(b'42 (INTERNALDATE "19-Nov-2026 10:00:00 +0000" BODY[] {100})', message.as_bytes()), b')']


class IMAPTests(unittest.TestCase):
    def test_tls_readonly_peek_and_mime_decoding(self):
        connection = Connection(ssl_context=__import__('ssl').create_default_context())
        with patch('imap_mail.original_identity', return_value={'id':'fixture-signed-mail','recipient':'student@gmail.com','sent_at':'2026-11-19T10:00:00+00:00'}), patch('imap_mail.imaplib.IMAP4_SSL', return_value=connection) as factory:
            messages = list(IMAPInbox('dedicated@example.invalid', 'fixture-app-password').messages('2026-11-19T09:59:00+00:00'))
        self.assertEqual(messages[0]['subject'], 'Иванов Иван Иванович #00')
        self.assertIn('012345', messages[0]['body']['content'])
        self.assertEqual(messages[0]['from']['emailAddress']['address'], 'sso@example.invalid')
        self.assertIn(('select', 'INBOX', True), connection.calls)
        self.assertTrue(any(call[0] == 'fetch' and 'BODY.PEEK[]' in call[2] for call in connection.calls))
        self.assertEqual(messages[0]['original_recipient'], 'student@gmail.com')
        self.assertEqual(factory.call_args.kwargs['ssl_context'].verify_mode, __import__('ssl').CERT_REQUIRED)
