#!/usr/bin/env python3
"""partizan backend: scoped IMAP OTP lookup and optional Telegram delivery."""
from __future__ import annotations
import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from bot import Bot, Server, Store, Telegram, TelegramError, handler_for, load_env, maintain_messages
from imap_mail import IMAPInbox
from mail_service import MailError, MailService


def api_handler(store, telegram, mail):
    base = handler_for(store, telegram)
    class Handler(base):
        def do_GET(self):
            if self.path == '/':
                return self.reply(200, ok=True, forwarding_address=getattr(mail.inbox, 'username', ''))
            return super().do_GET()

        def do_POST(self):
            if self.path != '/':
                return super().do_POST()
            if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
                return self.reply(400, ok=False, error='expected_json')
            authorization = self.headers.get('Authorization', '')
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 1 <= length <= 4096:
                    raise ValueError('invalid_length')
                query = json.loads(self.rfile.read(length))
                if isinstance(query, dict) and query.get('action') == 'connect':
                    result = mail.connect(query)
                else:
                    if not authorization.startswith('Bearer ') or not 20 <= len(authorization[7:]) <= 128:
                        raise PermissionError
                    result = mail.find(authorization[7:], query)
            except PermissionError:
                return self.reply(401, ok=False, error='invalid_key')
            except (ValueError, TypeError, KeyError):
                return self.reply(400, ok=False, error='invalid_query')
            except MailError as error:
                return self.reply(503, ok=False, error=str(error))
            except Exception:
                return self.reply(503, ok=False, error='mail_unavailable')
            if result is None:
                return self.reply(202, ok=True, state='pending')
            self.reply(200, ok=True, **result)
    return Handler


def run_telegram(telegram, store, stop):
    while not stop.is_set():
        try:
            me = telegram.call('getMe')
            if telegram.call('getWebhookInfo').get('url'):
                print('Telegram: установлен webhook; long polling отключён.', file=sys.stderr)
                return
            Bot(telegram, store, me['username']).poll(stop)
            return
        except TelegramError as error:
            print(str(error), file=sys.stderr)
            if error.code in {401, 409}:
                return
            stop.wait(3)


def main():
    load_env(Path(__file__).with_name('.env'))
    parser = argparse.ArgumentParser(description='partizan backend')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('mail-check', help='Проверить IMAP без вывода писем и кодов')
    args = parser.parse_args()
    state = Path(os.getenv('STATE_FILE', str(Path(__file__).parent / 'data/bot.sqlite3')))
    inbox = IMAPInbox(os.getenv('MAIL_USERNAME', ''), os.getenv('MAIL_PASSWORD', ''),
        os.getenv('MAIL_HOST', 'imap.gmail.com'), int(os.getenv('MAIL_PORT', '993')), sender=os.getenv('SSO_MAIL_SENDER', ''))
    mail = MailService(inbox, state.parent / 'mail-clients.json', os.getenv('SSO_MAIL_SENDER', ''))
    if args.command == 'mail-check':
        from datetime import datetime, timedelta, timezone
        count = sum(1 for _ in inbox.messages((datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()))
        print(f'IMAP подключён. Писем за последние 15 минут: {count}')
        return 0

    token = os.getenv('BOT_TOKEN', '').strip()
    choice = os.getenv('TELEGRAM_ENABLED', 'auto').casefold()
    enabled = bool(token) if choice in {'', 'auto'} else choice in {'true', '1', 'yes'}
    telegram = Telegram(token) if enabled else None
    store = Store(state)
    stop = threading.Event()
    host, port = os.getenv('HTTP_HOST', '127.0.0.1'), int(os.getenv('HTTP_PORT', '8787'))
    server = Server((host, port), api_handler(store, telegram, mail))
    workers = [threading.Thread(target=server.serve_forever, daemon=True)]
    telegram_stop = threading.Event()
    if telegram:
        workers.append(threading.Thread(target=run_telegram, args=(telegram, store, telegram_stop), daemon=True))
        workers.append(threading.Thread(target=maintain_messages,args=(telegram,store,telegram_stop),daemon=True))
    for worker in workers:
        worker.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print('partizan backend запущен; Telegram ' + ('включён' if telegram else 'отключён'), flush=True)
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop.set(); telegram_stop.set(); server.shutdown(); server.server_close()
        for worker in workers:
            worker.join(timeout=1)
        if all(not worker.is_alive() for worker in workers):
            store.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (MailError, TelegramError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
