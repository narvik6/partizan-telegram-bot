#!/usr/bin/env python3
"""Telegram delivery for Partizan. Python 3.10+, standard library only."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, method: str, code: int = 0, retry_after: int = 0):
        super().__init__(f"Telegram: {method}, код {code or 'сеть'}")
        self.code, self.retry_after = code, retry_after


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name.strip()):
            raise ValueError("Некорректная строка .env")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)


class Telegram:
    def __init__(self, token: str):
        self._token = token

    def call(self, method: str, **payload):
        # Never include the request URL, token or message body in exceptions.
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=35 if method == "getUpdates" else 8) as response:
                data = json.loads(response.read(2_000_000))
        except HTTPError as error:
            try:
                with error:
                    data = json.loads(error.read(64_000))
                retry = int(data.get("parameters", {}).get("retry_after", 0))
            except (ValueError, TypeError):
                retry = 0
            raise TelegramError(method, error.code, retry) from None
        except (URLError, OSError, ValueError):
            raise TelegramError(method) from None
        if not data.get("ok"):
            raise TelegramError(method, int(data.get("error_code", 0)))
        return data["result"]

    def send(self, chat_id: int, text: str):
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         link_preview_options={"is_disabled": True})


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        if os.name != "nt":
            path.chmod(0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value INTEGER);
                CREATE TABLE IF NOT EXISTS sent (
                    chat_id INTEGER, event_id TEXT, sent_at REAL,
                    PRIMARY KEY(chat_id, event_id));
            """)

    def connection_key(self, chat_id: int) -> str:
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO chats(chat_id,key) VALUES (?,?)",
                            (chat_id, secrets.token_urlsafe(32)))
            self.db.execute("UPDATE chats SET active=1 WHERE chat_id=?", (chat_id,))
            return self.db.execute("SELECT key FROM chats WHERE chat_id=?", (chat_id,)).fetchone()[0]

    def disable(self, chat_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))

    def move_chat(self, old: int, new: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE OR IGNORE chats SET chat_id=? WHERE chat_id=?", (new, old))
            self.db.execute("UPDATE OR IGNORE sent SET chat_id=? WHERE chat_id=?", (new, old))

    def offset(self) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE name='offset'").fetchone()
            return int(row[0]) if row else 0

    def advance(self, update_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (update_id + 1,))

    def deliver(self, key: str, event_id: str, text: str, telegram: Telegram) -> bool:
        # Serialize the check + delivery so concurrent HTTP retries do not
        # produce duplicate messages. Telegram acknowledgements are persisted.
        with self.lock:
            row = self.db.execute("SELECT chat_id,active FROM chats WHERE key=?", (key,)).fetchone()
            if not row:
                raise PermissionError
            chat_id, active = row
            if not active:
                raise LookupError
            if self.db.execute("SELECT 1 FROM sent WHERE chat_id=? AND event_id=?",
                               (chat_id, event_id)).fetchone():
                return True
            try:
                telegram.send(chat_id, text)
            except TelegramError as error:
                if error.code == 403:
                    self.disable(chat_id)
                raise
            with self.db:
                self.db.execute("INSERT INTO sent VALUES (?,?,?)", (chat_id, event_id, time.time()))
                self.db.execute("DELETE FROM sent WHERE sent_at<?", (time.time() - 86400,))
            return False

    def close(self) -> None:
        with self.lock:
            self.db.close()


class Bot:
    def __init__(self, telegram: Telegram, store: Store, username: str):
        self.telegram, self.store, self.username = telegram, store, username

    def handle(self, update: dict) -> None:
        membership = update.get("my_chat_member")
        if membership:
            if membership["new_chat_member"]["status"] in {"left", "kicked"}:
                self.store.disable(membership["chat"]["id"])
            return
        message = update.get("message", {})
        chat, user = message.get("chat", {}), message.get("from", {})
        if message.get("migrate_to_chat_id"):
            self.store.move_chat(chat["id"], message["migrate_to_chat_id"])
            return
        if user.get("is_bot") or chat.get("type") not in {"private", "group", "supergroup"}:
            return
        text = message.get("text", "").split()
        if not text or not text[0].startswith("/"):
            return
        command, _, mention = text[0].partition("@")
        if mention and mention.casefold() != self.username.casefold():
            return
        command = command.lower()
        if command not in {"/start", "/connect", "/stop", "/help"}:
            return
        private = chat["type"] == "private"
        if command == "/help" or (command == "/start" and not private):
            self.telegram.send(chat["id"], "Partizan уведомляет о контроле присутствия. "
                               "Личный чат: /connect. Группа: администратор вводит /connect. "
                               "Отключить этот чат: /stop.")
            return
        if not private:
            admins = self.telegram.call("getChatAdministrators", chat_id=chat["id"])
            if not any(member["user"]["id"] == user.get("id") for member in admins):
                self.telegram.send(chat["id"], "Подключить или отключить группу может её администратор.")
                return
        if command == "/stop":
            self.store.disable(chat["id"])
            self.telegram.send(chat["id"], "Уведомления для этого чата отключены. Подключить снова: /connect.")
            return
        key = self.store.connection_key(chat["id"])
        label = "личного чата" if private else "группы"
        credentials = (f"Код подключения {label} Partizan:\n\n{key}\n\n"
                       "В приложении откройте настройки уведомлений, укажите адрес сервера бота "
                       "и этот код, включите Telegram. Код разрешает отправку только в этот чат. "
                       "Для отключения: /stop в соответствующем чате.")
        if private:
            self.telegram.send(chat["id"], credentials)
        else:
            try:
                self.telegram.send(user["id"], credentials)
            except TelegramError as error:
                if error.code not in {400, 403}:
                    raise
                self.telegram.send(chat["id"], "Сначала откройте личный чат со мной и нажмите /start, "
                                   "затем повторите /connect в группе. Код в группу не публикуется.")
                return
            self.telegram.send(chat["id"], "Код подключения группы отправлен администратору в личный чат.")

    def poll(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                updates = self.telegram.call("getUpdates", offset=self.store.offset(), timeout=25,
                                             allowed_updates=["message", "my_chat_member"])
                for update in updates:
                    if stop.is_set():
                        break
                    try:
                        self.handle(update)
                    except TelegramError as error:
                        if error.code not in {400, 403}:
                            raise
                        # A removed/blocked chat must not poison the update queue.
                        print(str(error), file=sys.stderr)
                    self.store.advance(update["update_id"])
            except TelegramError as error:
                print(str(error), file=sys.stderr)
                if error.code in {401, 409}:
                    print("Проверьте токен, отсутствие webhook и другой запущенной копии бота.", file=sys.stderr)
                    stop.set()
                    return
                stop.wait(min(max(error.retry_after, 3), 60))


def handler_for(store: Store, telegram: Telegram):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # No authorization headers, keys or URLs in request logs.

        def reply(self, code: int, **payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, ok=True)
            else:
                self.reply(404, ok=False)

        def do_POST(self):
            if self.path != "/v1/attendance":
                return self.reply(404, ok=False)
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer ") or not 20 <= len(authorization[7:]) <= 128:
                return self.reply(401, ok=False, error="invalid_key")
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                return self.reply(400, ok=False, error="expected_json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400, ok=False, error="invalid_length")
            if not 1 <= length <= 4096:
                return self.reply(413, ok=False, error="body_too_large")
            try:
                event = json.loads(self.rfile.read(length))
                if not isinstance(event, dict) or set(event) != {"event_id", "kind", "title", "body"}:
                    raise ValueError
                if event["kind"] not in {"attendance_required", "test"}:
                    raise ValueError
                if not isinstance(event["event_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event["event_id"]):
                    raise ValueError
                if any(not isinstance(event[k], str) or not 1 <= len(event[k]) <= 1000 for k in ("title", "body")):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                return self.reply(400, ok=False, error="invalid_event")
            try:
                duplicate = store.deliver(authorization[7:], event["event_id"],
                                          event["title"] + "\n\n" + event["body"], telegram)
            except PermissionError:
                return self.reply(401, ok=False, error="invalid_key")
            except LookupError:
                return self.reply(410, ok=False, error="chat_disabled")
            except TelegramError:
                return self.reply(502, ok=False, error="telegram_delivery_failed")
            self.reply(200, ok=True, duplicate=duplicate)
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def main() -> int:
    root = Path(__file__).resolve().parent
    try:
        load_env(root / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажите BOT_TOKEN в .env: токен выдаёт @BotFather")
        host = os.getenv("HTTP_HOST", "127.0.0.1")
        port = int(os.getenv("HTTP_PORT", "8787"))
        state = Path(os.getenv("STATE_FILE", str(root / "data" / "bot.sqlite3")))
        telegram = Telegram(token)
        me = telegram.call("getMe")
        if telegram.call("getWebhookInfo").get("url"):
            raise ValueError("Для этого бота установлен webhook. Используйте отдельного бота без webhook.")
        store = Store(state)
        server = Server((host, port), handler_for(store, telegram))
    except (ValueError, TelegramError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    stop = threading.Event()
    bot = Bot(telegram, store, me["username"])
    polling = threading.Thread(target=bot.poll, args=(stop,), daemon=True)
    http = threading.Thread(target=server.serve_forever, daemon=True)
    polling.start()
    http.start()
    print(f"@{me['username']} запущен; API: http://{host}:{port}. Откройте личный чат и нажмите /start.", flush=True)
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        polling.join(timeout=1)
        # A pending long poll owns the store until the daemon exits.
        if not polling.is_alive():
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
