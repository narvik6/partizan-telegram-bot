#!/usr/bin/env python3
"""Telegram delivery for partizan. Python 3.10+ with requests[socks]."""
from __future__ import annotations

import json
from pubsub import Store, SHARED_KINDS, PERSONAL_KINDS
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests


class TelegramError(Exception):
    def __init__(self, method: str, code: int = 0, retry_after: int = 0):
        super().__init__(f"Telegram: {method}, код {code or 'сеть'}")
        self.code, self.retry_after = code, retry_after


def load_env(path: Path) -> None:
    if not path.exists():
        return
    if os.name != "nt":
        path.chmod(0o600)
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
    API_URL = "https://api.telegram.org"

    def __init__(self, token: str, proxy: str | None = None):
        self._token = token
        proxy = (proxy if proxy is not None else os.getenv("TELEGRAM_PROXY", "")).strip()
        if proxy:
            try:
                parsed = urlsplit(proxy)
                valid = (parsed.scheme in {"http", "https", "socks5", "socks5h"}
                         and parsed.hostname and parsed.port
                         and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("TELEGRAM_PROXY: укажите http(s)://host:port или socks5(h)://host:port")
        # Explicit per-request proxies override environment settings. Without
        # TELEGRAM_PROXY, requests honors HTTPS_PROXY / ALL_PROXY / NO_PROXY.
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    def call(self, method: str, **payload):
        # Never include the request URL, token or message body in exceptions.
        try:
            with requests.post(
                f"{self.API_URL}/bot{self._token}/{method}", json=payload,
                proxies=self._proxies, timeout=(8, 35 if method == "getUpdates" else 8),
                allow_redirects=False, stream=True,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise TelegramError(method, response.status_code)
                raw = bytearray()
                for chunk in response.iter_content(64_000):
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise ValueError("Response too large")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                if response.status_code != 200 or not data.get("ok"):
                    raise TelegramError(method, int(data.get("error_code", response.status_code)),
                                        int(data.get("parameters", {}).get("retry_after", 0)))
                return data["result"]
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError):
            raise TelegramError(method) from None

    def send(self, chat_id: int, text: str, **options):
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         link_preview_options={"is_disabled": True}, **options)


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
            self.store.move_chat(chat["id"], message["migrate_to_chat_id"]); return
        if user.get("is_bot") or chat.get("type") not in {"private", "group", "supergroup"}:
            return
        parts = message.get("text", "").split()
        if not parts or not parts[0].startswith("/"): return
        command, _, mention = parts[0].partition("@")
        if mention and mention.casefold() != self.username.casefold(): return
        command = command.casefold()
        allowed = {"/start","/connect","/stop","/help","/code","/subscribe","/unsubscribe","/subscriptions"}
        if command not in allowed: return
        private = chat["type"] == "private"
        if command == "/help" or (command == "/start" and not private):
            self.telegram.send(chat["id"], "partizan: /connect — подключить приложение, /code — код автора, "
                "/subscribe 0123 — подписаться, /subscriptions — список, /unsubscribe 0123 — отписаться, /stop — отключить этот чат.")
            return
        if not private:
            admins = self.telegram.call("getChatAdministrators", chat_id=chat["id"])
            if not any(member["user"]["id"] == user.get("id") for member in admins):
                self.telegram.send(chat["id"], "Настройки группы меняет её администратор."); return
        label = chat.get("title") or chat.get("first_name") or ""
        if command == "/stop":
            self.store.disable(chat["id"])
            self.telegram.send(chat["id"], "Этот чат отключён, подписки удалены. Подключить приложение: /connect; подписаться: /subscribe 0123."); return
        if command == "/code":
            code = self.store.publisher_code(chat["id"], label)
            self.telegram.send(chat["id"], f"Код автора: {code}\nДля подписки отправьте боту /subscribe {code}. Это публичный код получения уведомлений, не пароль отправителя."); return
        if command == "/subscriptions":
            rows = self.store.subscriptions(chat["id"])
            self.telegram.send(chat["id"], "\n".join(f"{code} · {name}" for code,name in rows) if rows else "Подписок нет. Добавить: /subscribe 0123."); return
        if command in {"/subscribe","/unsubscribe"}:
            if len(parts) != 2 or not re.fullmatch(r"[0-9]{4}", parts[1]):
                self.telegram.send(chat["id"], f"Укажите четыре цифры: {command} 0123"); return
            try:
                if command == "/subscribe":
                    name = self.store.subscribe(chat["id"], parts[1])
                    reply = f"Подписка на {name} · {parts[1]} включена. Просьбы о входе, личные ошибки и тесты не пересылаются."
                else:
                    self.store.unsubscribe(chat["id"], parts[1]); reply = "Подписка удалена."
            except LookupError:
                reply = "Такого кода подписки нет."
            self.telegram.send(chat["id"], reply); return
        pairing = self.store.issue_pairing(chat["id"], label)
        prefix = "Одноразовая фраза подключения partizan:\n\n"
        phrase = pairing["phrase"]
        text = (prefix + phrase + "\n\nВведите её в приложении в течение 5 минут. "
                "Фраза подключает одно приложение; новое подключение отзывает прежний ключ.\n"
                f"Код для подписчиков: {pairing['code']} — команда /subscribe {pairing['code']}.\n"
                "Сообщение будет удалено после использования или через 5 минут.")
        recipient = chat["id"] if private else user["id"]
        try:
            sent = self.telegram.send(recipient, text, entities=[{
                "type":"spoiler","offset":len(prefix.encode("utf-16-le"))//2,
                "length":len(phrase.encode("utf-16-le"))//2}])
            self.store.track_message(recipient, sent["message_id"], phrase)
        except TelegramError as error:
            if private or error.code not in {400,403}: raise
            self.telegram.send(chat["id"], "Сначала откройте личный чат со мной и нажмите /start, затем повторите /connect в группе. Фраза в группу не публикуется."); return
        if not private:
            self.telegram.send(chat["id"], "Одноразовая фраза отправлена администратору в личный чат.")

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

        def reply(self, http_status: int, **payload):
            data = json.dumps(payload).encode()
            self.send_response(http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in {"/health", "/"}:
                self.reply(200, ok=True)
            else:
                self.reply(404, ok=False)

        def do_POST(self):
            if self.path not in {"/v1/attendance", "/partizan_agent", "/partizan_agent/connect"}:
                return self.reply(404, ok=False)
            if telegram is None:
                return self.reply(503, ok=False, error="telegram_disabled")
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                return self.reply(400, ok=False, error="expected_json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 4096: return self.reply(413, ok=False, error="body_too_large")
                event = json.loads(self.rfile.read(length))
                if not isinstance(event, dict): raise ValueError
            except (ValueError,TypeError):
                return self.reply(400, ok=False, error="invalid_json")
            if self.path == "/partizan_agent/connect":
                if not store.allow_claim(self.client_address[0]):
                    return self.reply(429, ok=False, error="pairing_rate_limited")
                try:
                    if set(event) != {"phrase","client_id"}: raise ValueError
                    paired = store.claim(event["phrase"], event["client_id"])
                except PermissionError:
                    return self.reply(401, ok=False, error="phrase_expired_or_used")
                except ValueError:
                    return self.reply(400, ok=False, error="invalid_pairing")
                return self.reply(200, ok=True, **paired)
            authorization = self.headers.get("Authorization", "")
            client_id = self.headers.get("X-Partizan-Client", "")
            if not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", authorization) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", client_id):
                return self.reply(401, ok=False, error="pairing_required")
            try:
                if not {"event_id","kind","title","body"} <= set(event) or set(event) - {"event_id","kind","title","body","scope","signal"}: raise ValueError
                if event["kind"] not in SHARED_KINDS | PERSONAL_KINDS: raise ValueError
                if not isinstance(event["event_id"],str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",event["event_id"]): raise ValueError
                if any(not isinstance(event[k],str) or not 1 <= len(event[k]) <= 1000 for k in ("title","body")): raise ValueError
                if not isinstance(event.get("scope",""),str) or not re.fullmatch(r"(?:[0-9a-f]{64})?",event.get("scope","")): raise ValueError
                if not isinstance(event.get("signal",""),str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,32}",event.get("signal","")): raise ValueError
            except (ValueError,TypeError,KeyError):
                return self.reply(400, ok=False, error="invalid_event")
            try:
                duplicate = store.deliver(authorization[7:], event["event_id"], event["title"]+"\n\n"+event["body"], telegram,
                    client_id=client_id,kind=event["kind"],scope=event.get("scope",""),signal=event.get("signal",""))
            except PermissionError: return self.reply(401, ok=False, error="invalid_publisher")
            except LookupError: return self.reply(410, ok=False, error="publisher_disabled")
            except TelegramError: return self.reply(502, ok=False, error="telegram_delivery_failed")
            self.reply(200, ok=True, duplicate=duplicate)
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def maintain_messages(telegram, store, stop):
    while not stop.is_set():
        store.cleanup(telegram)
        stop.wait(5)


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
    cleanup = threading.Thread(target=maintain_messages, args=(telegram,store,stop), daemon=True)
    polling.start()
    http.start()
    cleanup.start()
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
        cleanup.join(timeout=1)
        # A pending long poll owns the store until the daemon exits.
        if not polling.is_alive() and not cleanup.is_alive():
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
