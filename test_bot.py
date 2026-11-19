import concurrent.futures
import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bot import Bot, Server, Store, TelegramError, handler_for


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.admins = [1]
        self.blocked = set()

    def send(self, chat_id, text):
        if chat_id in self.blocked:
            raise TelegramError("sendMessage", 403)
        self.messages.append((chat_id, text))

    def call(self, method, **payload):
        if method == "getChatAdministrators":
            return [{"user": {"id": user}} for user in self.admins]
        raise AssertionError(method)


def command(chat_id, user_id, text, kind="private", update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id, "type": kind},
            "from": {"id": user_id}, "text": text}}


class SharedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.telegram = FakeTelegram()
        self.bot = Bot(self.telegram, self.store, "PartizanTestBot")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

class BotTests(SharedCase):
    def test_private_subscription_is_isolated_and_persistent(self):
        self.bot.handle(command(1, 1, "/start"))
        first = self.store.connection_key(1)
        second = self.store.connection_key(2)
        self.assertNotEqual(first, second)
        self.assertIn(first, self.telegram.messages[0][1])
        self.store.advance(30)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.connection_key(1), first)
        self.assertEqual(self.store.offset(), 31)

    def test_only_group_admin_receives_group_key_in_private(self):
        self.bot.handle(command(-100, 2, "/connect", "supergroup"))
        self.assertIn("администратор", self.telegram.messages[-1][1])
        self.bot.handle(command(-100, 1, "/connect@PartizanTestBot", "supergroup"))
        key = self.store.connection_key(-100)
        self.assertTrue(any(chat == 1 and key in text for chat, text in self.telegram.messages))
        self.assertFalse(any(chat == -100 and key in text for chat, text in self.telegram.messages))

    def test_missing_private_dialog_has_actionable_instruction(self):
        self.telegram.blocked.add(1)
        self.bot.handle(command(-100, 1, "/connect", "group"))
        self.assertIn("/start", self.telegram.messages[-1][1])
        self.assertEqual(self.telegram.messages[-1][0], -100)

    def test_stop_removal_and_migration(self):
        key = self.store.connection_key(-100)
        self.bot.handle({"message": {"chat": {"id": -100}, "migrate_to_chat_id": -200}})
        self.store.deliver(key, "migration", "hello", self.telegram)
        self.assertEqual(self.telegram.messages[-1][0], -200)
        self.bot.handle({"my_chat_member": {"chat": {"id": -200}, "new_chat_member": {"status": "kicked"}}})
        with self.assertRaises(LookupError):
            self.store.deliver(key, "removed", "hello", self.telegram)
        self.bot.handle(command(1, 1, "/start"))
        private_key = self.store.connection_key(1)
        self.bot.handle(command(1, 1, "/stop"))
        with self.assertRaises(LookupError):
            self.store.deliver(private_key, "stopped", "hello", self.telegram)

    def test_normal_text_and_other_bots_commands_are_ignored(self):
        for text in ["Hello", "/connect@OtherBot", "/unknown"]:
            self.bot.handle(command(1, 1, text))
        self.assertEqual(self.telegram.messages, [])


class DeliveryTests(SharedCase):
    def setUp(self):
        super().setUp()
        self.server = Server(("127.0.0.1", 0), handler_for(self.store, self.telegram))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/attendance"
        self.key = self.store.connection_key(100)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def post(self, event=None, key=None):
        if event is None:
            event = {"event_id": "event-1", "kind": "attendance_required",
                     "title": "Контроль присутствия", "body": "На вебинаре появился QR Pulse."}
        credential = key or self.key
        if ' ' in credential:
            credential = 'phrase.' + base64.urlsafe_b64encode(credential.encode()).decode().rstrip('=')
        request = Request(self.url, json.dumps(event).encode(),
                          {"Content-Type": "application/json", "Authorization": "Bearer " + credential})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_actual_http_delivery_and_concurrent_deduplication(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.post(), range(4)))
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.telegram.messages[0][0], 100)
        self.assertEqual(sum(not body["duplicate"] for _, body in results), 1)

    def test_key_is_bound_to_one_chat_and_not_body_supplied_recipient(self):
        other = self.store.connection_key(200)
        self.assertEqual(self.post(key=other)[0], 200)
        self.assertEqual(self.telegram.messages[-1][0], 200)
        self.assertEqual(self.post(key="not-a-real-key-but-long-enough")[0], 401)
        injected = {"event_id": "bad", "kind": "attendance_required", "title": "test", "body": "test", "chat_id": 200}
        self.assertEqual(self.post(injected)[0], 400)

    def test_other_event_types_and_extra_secret_fields_rejected(self):
        for event in [{"event_id": "bad", "kind": "login_required", "title": "a", "body": "b"},
                      {"event_id": "bad", "kind": "attendance_required", "title": "a", "body": "b", "token": "secret"}]:
            self.assertEqual(self.post(event)[0], 400)
        self.assertEqual(self.telegram.messages, [])

    def test_disabled_chat_and_failed_delivery_are_not_success(self):
        self.telegram.blocked.add(100)
        self.assertEqual(self.post()[0], 502)
        self.assertEqual(self.post()[0], 410)
        self.assertEqual(self.telegram.messages, [])

    def test_delivery_dedup_survives_process_restart(self):
        self.assertEqual(self.post()[0], 200)
        self.store.close()
        self.store = Store(self.path)
        self.server.RequestHandlerClass = handler_for(self.store, self.telegram)
        status, body = self.post()
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicate"])
        self.assertEqual(len(self.telegram.messages), 1)


if __name__ == "__main__":
    unittest.main()
