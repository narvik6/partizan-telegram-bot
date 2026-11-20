"""Publisher pairing, subscriptions and durable delivery state."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from phrases import generate, normalize

SHARED_KINDS = {'lecture_soon', 'webinar_found', 'attendance_required', 'lecture_finished'}
PERSONAL_KINDS = {'login_required', 'stage_failed', 'test'}
PAIR_TTL = 300
DEDUP_SECONDS = 60


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, clock=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        if os.name != 'nt': path.chmod(0o600)
        self.clock, self.lock = clock, threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value INTEGER);
                CREATE TABLE IF NOT EXISTS publishers(owner INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, label TEXT NOT NULL, client_id TEXT, token_hash TEXT, active INTEGER NOT NULL DEFAULT 1);
                CREATE UNIQUE INDEX IF NOT EXISTS publisher_token_index ON publishers(token_hash);
                CREATE TABLE IF NOT EXISTS pairings(phrase_hash TEXT PRIMARY KEY, owner INTEGER NOT NULL, expires REAL NOT NULL, used_by TEXT);
                CREATE TABLE IF NOT EXISTS subscriptions(chat_id INTEGER NOT NULL, owner INTEGER NOT NULL, PRIMARY KEY(chat_id,owner));
                CREATE TABLE IF NOT EXISTS published_sent(chat_id INTEGER, owner INTEGER, event_id TEXT, sent_at REAL, PRIMARY KEY(chat_id,owner,event_id));
                CREATE TABLE IF NOT EXISTS recent_sent(chat_id INTEGER, scope TEXT, kind TEXT, signal TEXT, sent_at REAL, PRIMARY KEY(chat_id,scope,kind,signal));
                CREATE TABLE IF NOT EXISTS ephemeral(chat_id INTEGER, message_id INTEGER, phrase_hash TEXT, delete_at REAL, created_at REAL, PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS pairing_attempts(peer TEXT, at REAL);
            ''')
            # Old reusable phrases are not credentials in the new protocol.
            for (chat,) in self.db.execute("SELECT chat_id FROM chats WHERE key NOT LIKE 'retired:%' OR length(key)<40").fetchall():
                self.db.execute("UPDATE chats SET key=? WHERE chat_id=?", ("retired:"+secrets.token_urlsafe(32),chat))
            if not self.db.execute("SELECT 1 FROM meta WHERE name='publisher_seed'").fetchone():
                self.db.execute("INSERT INTO meta VALUES('publisher_seed',?)", (secrets.token_hex(32),))

    def _chat(self, chat_id):
        self.db.execute("INSERT INTO chats(chat_id,key,active) VALUES(?,?,1) ON CONFLICT(chat_id) DO UPDATE SET active=1", (chat_id, 'retired:' + secrets.token_urlsafe(32)))

    def _publisher(self, owner, label=''):
        row = self.db.execute('SELECT code FROM publishers WHERE owner=?', (owner,)).fetchone()
        if not row:
            start = secrets.randbelow(10000)
            for offset in range(10000):
                code = f'{(start + offset) % 10000:04}'
                if not self.db.execute('SELECT 1 FROM publishers WHERE code=?', (code,)).fetchone():
                    self.db.execute('INSERT INTO publishers(owner,code,label) VALUES(?,?,?)', (owner, code, label[:80] or 'Автор ' + code))
                    break
            else: raise ValueError('Все коды подписки заняты')
        elif label:
            self.db.execute('UPDATE publishers SET label=? WHERE owner=?', (label[:80], owner))
        return self.db.execute('SELECT code,label FROM publishers WHERE owner=?', (owner,)).fetchone()

    def publisher_code(self, owner, label=''):
        with self.lock, self.db:
            self._chat(owner)
            return self._publisher(owner, label)[0]

    def issue_pairing(self, owner, label=''):
        with self.lock, self.db:
            now = self.clock(); self._chat(owner)
            code, _ = self._publisher(owner, label)
            self.db.execute('UPDATE ephemeral SET delete_at=? WHERE phrase_hash IN (SELECT phrase_hash FROM pairings WHERE owner=?)', (now, owner))
            self.db.execute('DELETE FROM pairings WHERE owner=?', (owner,))
            for _ in range(256):
                phrase = generate(); hashed = digest(normalize(phrase))
                if not self.db.execute('SELECT 1 FROM pairings WHERE phrase_hash=?', (hashed,)).fetchone(): break
            else: raise ValueError('Не удалось создать фразу')
            self.db.execute('INSERT INTO pairings VALUES(?,?,?,NULL)', (hashed, owner, now + PAIR_TTL))
            return {'phrase': phrase, 'code': code, 'expires_at': now + PAIR_TTL}

    def allow_claim(self, peer):
        with self.lock, self.db:
            now = self.clock(); peer = digest(peer)
            self.db.execute('DELETE FROM pairing_attempts WHERE at<?', (now - 60,))
            if self.db.execute('SELECT COUNT(*) FROM pairing_attempts WHERE peer=?', (peer,)).fetchone()[0] >= 10: return False
            self.db.execute('INSERT INTO pairing_attempts VALUES(?,?)', (peer, now)); return True

    def claim(self, phrase, client_id):
        if not isinstance(phrase, str) or len(phrase) > 256 or not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', client_id): raise ValueError('invalid_pairing')
        with self.lock, self.db:
            hashed = digest(normalize(phrase))
            row = self.db.execute('SELECT owner,expires,used_by FROM pairings WHERE phrase_hash=?', (hashed,)).fetchone()
            if not row or self.clock() >= row[1] or (row[2] and row[2] != client_id): raise PermissionError
            owner = row[0]
            seed = self.db.execute("SELECT value FROM meta WHERE name='publisher_seed'").fetchone()[0]
            token = base64.urlsafe_b64encode(hmac.new(bytes.fromhex(seed), (hashed + ':' + client_id).encode(), hashlib.sha256).digest()).decode().rstrip('=')
            if row[2]:
                current = self.db.execute('SELECT token_hash,client_id FROM publishers WHERE owner=?', (owner,)).fetchone()
                if current != (digest(token), client_id): raise PermissionError
            self.db.execute('UPDATE pairings SET used_by=? WHERE phrase_hash=?', (client_id, hashed))
            self.db.execute('UPDATE publishers SET client_id=?,token_hash=?,active=1 WHERE owner=?', (client_id, digest(token), owner))
            self.db.execute('UPDATE ephemeral SET delete_at=? WHERE phrase_hash=?', (self.clock(), hashed))
            return {'token': token, 'publisher_code': self._publisher(owner)[0]}

    def subscribe(self, chat_id, code):
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{4}', code): raise ValueError('Укажите четыре цифры: /subscribe 0123')
        with self.lock, self.db:
            row = self.db.execute('SELECT owner,label FROM publishers WHERE code=?', (code,)).fetchone()
            if not row: raise LookupError('Такого кода подписки нет')
            self._chat(chat_id)
            if row[0] != chat_id: self.db.execute('INSERT OR IGNORE INTO subscriptions VALUES(?,?)', (chat_id, row[0]))
            return row[1]

    def unsubscribe(self, chat_id, code):
        with self.lock, self.db:
            self.db.execute('DELETE FROM subscriptions WHERE chat_id=? AND owner IN (SELECT owner FROM publishers WHERE code=?)', (chat_id, code))

    def subscriptions(self, chat_id):
        with self.lock:
            return self.db.execute('SELECT p.code,p.label FROM subscriptions s JOIN publishers p ON p.owner=s.owner WHERE s.chat_id=? ORDER BY p.code', (chat_id,)).fetchall()

    def disable(self, chat_id):
        with self.lock, self.db:
            self.db.execute('UPDATE chats SET active=0 WHERE chat_id=?', (chat_id,))
            self.db.execute('UPDATE publishers SET active=0 WHERE owner=?', (chat_id,))
            self.db.execute('DELETE FROM subscriptions WHERE chat_id=?', (chat_id,))
            self.db.execute('DELETE FROM pairings WHERE owner=?', (chat_id,))

    def move_chat(self, old, new):
        with self.lock, self.db:
            for table, column in [('chats','chat_id'),('publishers','owner'),('pairings','owner'),('subscriptions','chat_id'),('subscriptions','owner'),('published_sent','chat_id'),('published_sent','owner'),('recent_sent','chat_id'),('ephemeral','chat_id')]:
                self.db.execute(f'UPDATE OR IGNORE {table} SET {column}=? WHERE {column}=?', (new, old))

    def deliver(self, token, event_id, text, telegram, *, client_id, kind='attendance_required', scope='', signal=''):
        with self.lock:
            row = self.db.execute('SELECT owner,code,label,active,client_id FROM publishers WHERE token_hash=?', (digest(token),)).fetchone()
            if not row or row[4] != client_id: raise PermissionError
            owner, code, label, active, _ = row
            if not active: raise LookupError
            targets = {owner}
            if kind in SHARED_KINDS:
                targets.update(r[0] for r in self.db.execute('SELECT chat_id FROM subscriptions WHERE owner=?', (owner,)))
            targets = {chat for chat in targets if self.db.execute('SELECT active FROM chats WHERE chat_id=?',(chat,)).fetchone() == (1,)}
            if not targets: raise LookupError
            error = None; delivered = False; now = self.clock()
            for chat_id in sorted(targets):
                enabled = self.db.execute('SELECT active FROM chats WHERE chat_id=?', (chat_id,)).fetchone()
                if not enabled or not enabled[0]: continue
                if self.db.execute('SELECT 1 FROM published_sent WHERE chat_id=? AND owner=? AND event_id=?', (chat_id, owner, event_id)).fetchone(): continue
                recent = self.db.execute('SELECT sent_at FROM recent_sent WHERE chat_id=? AND scope=? AND kind=? AND signal=?', (chat_id, scope, kind, signal)).fetchone() if scope and kind in SHARED_KINDS else None
                if not recent or now - recent[0] >= DEDUP_SECONDS:
                    try:
                        telegram.send(chat_id, text if chat_id == owner else f'{label} · {code}\n\n{text}')
                    except Exception as failure:
                        error = failure
                        if getattr(failure, 'code', 0) == 403:
                            with self.db: self.db.execute('UPDATE chats SET active=0 WHERE chat_id=?', (chat_id,))
                        continue
                    delivered = True
                    if scope and kind in SHARED_KINDS:
                        with self.db: self.db.execute('INSERT OR REPLACE INTO recent_sent VALUES(?,?,?,?,?)', (chat_id, scope, kind, signal, now))
                with self.db:
                    self.db.execute('INSERT INTO published_sent VALUES(?,?,?,?)', (chat_id, owner, event_id, now))
            with self.db:
                self.db.execute('DELETE FROM published_sent WHERE sent_at<?', (now - 86400,))
                self.db.execute('DELETE FROM recent_sent WHERE sent_at<?', (now - DEDUP_SECONDS,))
            if error: raise error
            return not delivered

    def track_message(self, chat_id, message_id, phrase):
        with self.lock, self.db:
            now = self.clock(); hashed = digest(normalize(phrase))
            row = self.db.execute('SELECT expires,used_by FROM pairings WHERE phrase_hash=?', (hashed,)).fetchone()
            delete_at = min(now + PAIR_TTL, row[0]) if row and not row[1] else now
            self.db.execute('INSERT OR REPLACE INTO ephemeral VALUES(?,?,?,?,?)', (chat_id, message_id, hashed, delete_at, now))

    def cleanup(self, telegram):
        with self.lock:
            due = self.db.execute('SELECT chat_id,message_id,created_at FROM ephemeral WHERE delete_at<=? LIMIT 50', (self.clock(),)).fetchall()
        for chat_id, message_id, created in due:
            try: telegram.call('deleteMessage', chat_id=chat_id, message_id=message_id)
            except Exception as error:
                if getattr(error, 'code', 0) not in {400,403} and self.clock() - created < 47 * 3600:
                    with self.lock, self.db: self.db.execute('UPDATE ephemeral SET delete_at=? WHERE chat_id=? AND message_id=?', (self.clock()+60, chat_id, message_id))
                    continue
            with self.lock, self.db: self.db.execute('DELETE FROM ephemeral WHERE chat_id=? AND message_id=?', (chat_id, message_id))
        with self.lock, self.db: self.db.execute('DELETE FROM pairings WHERE expires<?', (self.clock()-86400,))

    def offset(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE name='offset'").fetchone(); return int(row[0]) if row else 0

    def advance(self, update_id):
        with self.lock, self.db: self.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (update_id+1,))

    def close(self):
        with self.lock: self.db.close()
