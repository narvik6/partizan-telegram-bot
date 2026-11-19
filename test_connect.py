from mail_service import MailError
from test_mail import MailCase


class ConnectionTests(MailCase):
    def proof(self, **change):
        value = {**self.query, 'action': 'connect', 'username': 'student@edu.mirea.ru',
                 'code': '012345', 'device_id': 'local-installation-identifier-12345678'}
        value.update(change)
        return value

    def test_manual_code_connects_one_device_and_retry_is_idempotent(self):
        self.assertIsNone(self.service.connect(self.proof()))
        self.inbox.items = [self.message('Иванов Иван Иванович #00 Код аутентификации: 012345')]
        first = self.service.connect(self.proof())
        self.assertEqual(self.service.connect(self.proof()), first)
        self.assertNotIn(first['connection_token'], self.service.clients_path.read_text())
        self.assertEqual(self.service.find(first['connection_token'], self.query)['code'], '012345')
        with self.assertRaises(PermissionError):
            self.service.connect(self.proof(device_id='another-installation-identifier-123456'))

    def test_other_code_cannot_enroll_and_guessing_is_bounded(self):
        self.inbox.items = [self.message('Иванов Иван Иванович #00 Код: 012345')]
        for _ in range(5):
            with self.assertRaises(PermissionError):
                self.service.connect(self.proof(code='000000'))
        with self.assertRaises(MailError):
            self.service.connect(self.proof())

    def test_no_code_is_exposed_for_unknown_device_and_wrong_domain(self):
        self.inbox.items = [self.message('Иванов Иван Иванович #00 Код: 012345')]
        with self.assertRaises(ValueError):
            self.service.connect(self.proof(username='student@example.invalid'))
        with self.assertRaises(PermissionError):
            self.service.find('unknown-key', self.query)

    def test_first_code_binds_recipient_and_never_reads_other_students_mail(self):
        self.inbox.items = [
            self.message('#00 Код: 012345'),
            self.message('#00 Код: 999999', id='other-mail', original_recipient='other@gmail.com')]
        token = self.service.connect(self.proof())['connection_token']
        self.assertEqual(self.service.find(token, self.query)['code'], '012345')
        self.inbox.items = [self.inbox.items[1]]
        self.assertIsNone(self.service.find(token, self.query))
        with self.assertRaises(ValueError):
            self.service.find(token, {**self.query, 'recipient': 'other@gmail.com'})

    def test_missing_or_ambiguous_original_recipient_never_creates_grant(self):
        self.inbox.items = [self.message('#00 Код: 012345', original_recipient='')]
        with self.assertRaisesRegex(MailError, 'original_recipient_missing'):
            self.service.connect(self.proof())
        self.inbox.items = [
            self.message('#00 Код: 012345'),
            self.message('#00 Код: 012345', id='other-mail', original_recipient='other@gmail.com')]
        with self.assertRaisesRegex(MailError, 'ambiguous_mail'):
            self.service.connect(self.proof())
