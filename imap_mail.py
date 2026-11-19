"""Read-only IMAP access to the dedicated forwarding mailbox."""
import email
import email.policy
import email.utils
import hashlib
import imaplib
import ssl
import time
from datetime import datetime, timezone

from mail_service import MailError, address
from mail_auth import original_identity


class IMAPInbox:
    def __init__(self, username, password, host='imap.gmail.com', port=993, sender=''):
        self.username, self.password, self.host, self.port = username, password, host, int(port)
        self.sender = sender
        if host.lower() == 'imap.gmail.com':
            self.password = ''.join(password.split())

    def messages(self, since):
        if not self.username or not self.password:
            raise MailError('mail_not_configured')
        start = datetime.fromisoformat(since.replace('Z', '+00:00'))
        try:
            with imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=15) as connection:
                connection.login(self.username, self.password)
                status, _ = connection.select('INBOX', readonly=True)
                if status != 'OK':
                    raise MailError('mail_unavailable')
                status, data = connection.uid('search', None, 'SINCE', start.strftime('%d-%b-%Y'))
                if status != 'OK':
                    raise MailError('mail_unavailable')
                ids = data[0].split() if data and data[0] else []
                for uid in reversed(ids[-250:]):
                    status, chunks = connection.uid('fetch', uid, '(INTERNALDATE BODY.PEEK[])')
                    if status != 'OK':
                        raise MailError('mail_unavailable')
                    for chunk in chunks:
                        if not isinstance(chunk, tuple):
                            continue
                        metadata, raw = chunk
                        received = imaplib.Internaldate2tuple(metadata)
                        if not received:
                            continue
                        timestamp = datetime.fromtimestamp(time.mktime(received), timezone.utc)
                        if timestamp < start:
                            continue
                        message = email.message_from_bytes(raw, policy=email.policy.default)
                        identity = original_identity(raw, message, self.sender)
                        original_recipient = identity['recipient'] if identity and identity['recipient'] != address(self.username) else ''
                        body = message.get_body(preferencelist=('plain', 'html'))
                        content = body.get_content() if body else ''
                        yield {'original_recipient': original_recipient, 'authenticated': bool(identity),
                            'sent_at': identity['sent_at'] if identity else '',
                            'id': identity['id'] if identity else 'imap:' + hashlib.sha256(raw).hexdigest(),
                            'from': {'emailAddress': {'address': email.utils.parseaddr(str(message.get('From', '')))[1]}},
                            'subject': str(message.get('Subject', '')),
                            'body': {'contentType': 'html' if body and body.get_content_type() == 'text/html' else 'text', 'content': content},
                            'receivedDateTime': timestamp.isoformat()}
        except (imaplib.IMAP4.error, OSError, ValueError, UnicodeError):
            raise MailError('mail_unavailable') from None
