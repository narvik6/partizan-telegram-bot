"""Mailbox access stays on the server; device access is bound to a proven recipient."""
from __future__ import annotations
import base64
import contextlib
import fcntl
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


class MailError(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False)
    os.replace(temporary, path)


@contextlib.contextmanager
def cache_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', value):
        return ''
    return value.casefold()


class Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def plain(message):
    body = message.get('body', {})
    value = body.get('content', '')
    if body.get('contentType', '').casefold() == 'html':
        parser = Text(); parser.feed(value); value = ' '.join(parser.parts)
    return html.unescape(message.get('subject', '') + '\n' + value)


class MailService:
    def __init__(self, inbox, clients_path, sender):
        self.inbox, self.clients_path, self.sender = inbox, Path(clients_path), sender.casefold().strip()
        self.lock = threading.Lock()

    def _state(self):
        state = json.loads(self.clients_path.read_text()) if self.clients_path.exists() else {}
        if state.get('version') != 2:
            # Old name-scoped grants cannot become recipient-scoped permissions.
            state = {'version': 2, 'clients': {}, 'proofs': {}, 'seed': secrets.token_hex(32)}
        state.setdefault('failed_proofs', {})
        return state

    def add_client(self, recipient):
        # Test helper; production grants are created only by connect().
        if not address(recipient):
            raise ValueError('invalid_recipient')
        token = secrets.token_urlsafe(32)
        with cache_lock(self.clients_path):
            state = self._state()
            state['clients'][hashlib.sha256(token.encode()).hexdigest()] = {'recipient': address(recipient)}
            atomic_json(self.clients_path, state)
        return token

    def find(self, token, query):
        grant = self._state()['clients'].get(hashlib.sha256(token.encode()).hexdigest())
        if not isinstance(grant, dict) or not address(grant.get('recipient')):
            raise PermissionError
        # Recipient is taken only from the grant, never from a desktop query.
        messages, unreadable, _ = self._candidates(query, grant['recipient'])
        if unreadable:
            raise MailError('mail_format_unrecognized')
        if len(messages) > 1:
            raise MailError('ambiguous_mail')
        if messages:
            return {'code': messages[0]['code'], 'request_number': query['request_number']}

    def connect(self, proof):
        fields = {'action', 'username', 'request_number', 'requested_at', 'code', 'device_id'}
        if not isinstance(proof, dict) or set(proof) != fields or not all(isinstance(v, str) for v in proof.values()):
            raise ValueError('invalid_proof')
        if proof['action'] != 'connect' or not re.fullmatch(r'[A-Za-z0-9._%+-]+@edu\.mirea\.ru', proof['username'], re.I):
            raise ValueError('invalid_username')
        if not re.fullmatch(r'[A-Za-z0-9]{4,6}', proof['code']) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', proof['device_id']):
            raise ValueError('invalid_proof')
        query = {k: proof[k] for k in ('request_number', 'requested_at')}
        messages, unreadable, missing_recipient = self._candidates(query)
        matches = [m for m in messages if secrets.compare_digest(m['code'], proof['code'])]
        if len(matches) > 1:
            raise MailError('ambiguous_mail')
        if not messages:
            if missing_recipient:
                raise MailError('original_recipient_missing')
            if unreadable:
                raise MailError('mail_format_unrecognized')
            return None
        device = hashlib.sha256((proof['device_id'] + ':' + proof['username'].casefold()).encode()).hexdigest()
        with cache_lock(self.clients_path):
            state = self._state()
            if not matches:
                # Bound guessing per actual mail, even if device_id is changed.
                for message in messages:
                    identity = hashlib.sha256(message['id'].encode()).hexdigest()
                    state['failed_proofs'][identity] = min(5, state['failed_proofs'].get(identity, 0) + 1)
                atomic_json(self.clients_path, state)
                raise PermissionError
            message = matches[0]
            proof_id = hashlib.sha256(message['id'].encode()).hexdigest()
            if state['failed_proofs'].get(proof_id, 0) >= 5:
                raise MailError('manual_code_attempts_exhausted')
            if state['proofs'].get(proof_id, device) != device:
                raise PermissionError
            material = (proof_id + ':' + device + ':' + message['recipient']).encode()
            token = base64.urlsafe_b64encode(hmac.new(bytes.fromhex(state['seed']), material, hashlib.sha256).digest()).decode().rstrip('=')
            state['clients'][hashlib.sha256(token.encode()).hexdigest()] = {
                'recipient': message['recipient'], 'username': proof['username'].casefold()}
            state['proofs'][proof_id] = device
            atomic_json(self.clients_path, state)
        return {'connection_token': token}

    def _candidates(self, query, recipient=None):
        if not isinstance(query, dict) or set(query) != {'request_number', 'requested_at'} or not all(isinstance(v, str) for v in query.values()):
            raise ValueError('invalid_query')
        number = query['request_number']
        if not re.fullmatch(r'#[0-9A-Za-zА-Яа-я]{2,8}', number):
            raise ValueError('invalid_request_number')
        since = datetime.fromisoformat(query['requested_at'].replace('Z', '+00:00'))
        if since.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - since).total_seconds() <= 900:
            raise ValueError('invalid_request_time')
        if not self.sender:
            raise MailError('mail_sender_not_configured')
        matches = {}
        unreadable = missing_recipient = False
        with self.lock:
            for message in self.inbox.messages(since.astimezone(timezone.utc).isoformat()):
                sender = message.get('from', {}).get('emailAddress', {}).get('address', '').casefold()
                text = plain(message)
                if sender != self.sender:
                    continue
                if not re.search(re.escape(number) + r'(?!\w)', text):
                    continue
                received = datetime.fromisoformat(message['receivedDateTime'].replace('Z', '+00:00'))
                if received < since:
                    continue
                original = address(message.get('original_recipient')) if message.get('authenticated') is True else ''
                if not original:
                    missing_recipient = True
                    continue
                if recipient and original != recipient:
                    continue
                if datetime.fromisoformat(message['sent_at'].replace('Z', '+00:00')) < since:
                    continue
                codes = set(re.findall(r'(?:код(?:\s+(?:подтверждения|доступа))?|code)\s*[:—–-]\s*([A-Za-z0-9]{4,6})(?!\w)', text, re.I))
                if not codes:
                    codes = set(re.findall(r'(?<![\w#])([0-9]{6})(?!\w)', text))
                if len(codes) == 1:
                    matches[message['id']] = {'id': message['id'], 'recipient': original, 'code': codes.pop()}
                else:
                    unreadable = True
        return list(matches.values()), unreadable, missing_recipient
