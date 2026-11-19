import base64
import email
import email.policy
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import dkim
from nacl.signing import SigningKey
from mail_auth import original_identity


class SignatureTests(unittest.TestCase):
    def signed(self, minutes=0, headers=None):
        # Deterministic disposable fixture key, never used by any account/service.
        key = SigningKey(bytes(range(32)))
        message = EmailMessage()
        message['From'] = 'sso@example.invalid'
        message['To'] = 'student@gmail.com'
        message['Subject'] = 'Запрос #00'
        message['Date'] = (datetime.now(timezone.utc) - timedelta(minutes=minutes))
        message.set_content('Код: 012345')
        raw = message.as_bytes(policy=email.policy.SMTP)
        signature = dkim.sign(raw, b'fixture', b'example.invalid', base64.b64encode(bytes(key)),
            include_headers=headers or [b'from', b'to', b'date', b'subject'],
            signature_algorithm=b'ed25519-sha256')
        dns = lambda *_args, **_kwargs: b'v=DKIM1; k=ed25519; p=' + base64.b64encode(bytes(key.verify_key))
        return signature + raw, dns

    def identity(self, raw, dns):
        return original_identity(raw, email.message_from_bytes(raw, policy=email.policy.default),
                                 'sso@example.invalid', dns)

    def test_signed_recipient_and_stable_identity(self):
        raw, dns = self.signed()
        first = self.identity(raw, dns)
        self.assertEqual(first['recipient'], 'student@gmail.com')
        self.assertEqual(first['id'], self.identity(b'X-Forwarded-To: dedicated@gmail.com\r\n' + raw, dns)['id'])

    def test_forged_to_from_code_and_authentication_results_do_not_grant_access(self):
        raw, dns = self.signed()
        for mutated in [
            raw.replace(b'student@gmail.com', b'other@gmail.com'),
            raw.replace(b'012345', b'999999'),
            b'To: attacker@gmail.com\r\n' + raw,
            raw.replace(b'sso@example.invalid', b'attacker@example.invalid')]:
            self.assertIsNone(self.identity(mutated, dns))
        unsigned = b'Authentication-Results: mx.google.com; dkim=pass\r\n' + raw.split(b'\r\n\r\n', 1)[-1]
        self.assertIsNone(self.identity(unsigned, dns))

    def test_stale_date_or_unsigned_recipient_cannot_authorize_device(self):
        raw, dns = self.signed(minutes=16)
        self.assertIsNone(self.identity(raw, dns))
        raw, dns = self.signed(headers=[b'from', b'date', b'subject'])
        self.assertIsNone(self.identity(raw, dns))
