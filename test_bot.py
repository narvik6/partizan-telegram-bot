import concurrent.futures
import hashlib
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from bot import Bot, Server, Store, TelegramError, handler_for


class FakeTelegram:
    def __init__(self):
        self.messages, self.options, self.deleted = [], [], []
        self.admins, self.blocked = [1], set()
        self.delete_error = 0
    def send(self, chat_id, text, **options):
        if chat_id in self.blocked: raise TelegramError("sendMessage",403)
        self.messages.append((chat_id,text)); self.options.append(options)
        return {"message_id":len(self.messages)}
    def call(self, method, **payload):
        if method == "getChatAdministrators":
            return [{"user":{"id":user}} for user in self.admins]
        if method == "deleteMessage":
            if self.delete_error: raise TelegramError(method,self.delete_error)
            self.deleted.append((payload["chat_id"],payload["message_id"])); return True
        raise AssertionError(method)


def command(chat_id,user_id,text,kind="private",update_id=1):
    return {"update_id":update_id,"message":{"chat":{"id":chat_id,"type":kind},"from":{"id":user_id},"text":text}}


class SharedCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/"state.sqlite3"
        self.now=[time.time()]
        self.store=Store(self.path,clock=lambda:self.now[0])
        self.telegram=FakeTelegram(); self.bot=Bot(self.telegram,self.store,"PartizanTestBot")
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def pair(self,owner):
        pairing=self.store.issue_pairing(owner)
        client=hashlib.sha256(str(owner).encode()).hexdigest()
        result=self.store.claim(pairing["phrase"],client)
        return result["token"],client,result["publisher_code"]


class BotTests(SharedCase):
    def test_phrase_is_spoiler_and_expires_with_durable_message_deletion(self):
        self.bot.handle(command(1,1,"/start"))
        chat,text=self.telegram.messages[0]
        phrase=text.split("\n\n")[1]
        entity=self.telegram.options[0]["entities"][0]
        units=text.encode("utf-16-le")
        revealed=units[2*entity["offset"]:2*(entity["offset"]+entity["length"])].decode("utf-16-le")
        self.assertEqual(revealed,phrase); self.assertEqual(entity["type"],"spoiler")
        self.assertNotIn(phrase,self.path.read_bytes().decode(errors="ignore"))
        self.store.close(); self.store=Store(self.path,clock=lambda:self.now[0])
        self.now[0]+=301; self.store.cleanup(self.telegram)
        self.assertEqual(self.telegram.deleted,[(1,1)])
        with self.assertRaises(PermissionError): self.store.claim(phrase,"x"*32)

    def test_group_phrase_only_goes_to_admin_private_chat(self):
        self.bot.handle(command(-100,2,"/connect","supergroup"))
        self.assertIn("администратор",self.telegram.messages[-1][1])
        self.bot.handle(command(-100,1,"/connect@PartizanTestBot","supergroup"))
        private=[(c,t) for c,t in self.telegram.messages if c==1]
        self.assertEqual(len(private),1)
        self.assertTrue(self.telegram.options[-2].get("entities"))
        self.assertNotIn(private[0][1].split("\n\n")[1],self.telegram.messages[-1][1])

    def test_missing_private_dialog_has_actionable_instruction(self):
        self.telegram.blocked.add(1)
        self.bot.handle(command(-100,1,"/connect","group"))
        self.assertIn("/start",self.telegram.messages[-1][1]); self.assertEqual(self.telegram.messages[-1][0],-100)

    def test_subscribe_list_unsubscribe_and_group_authority(self):
        _,_,code=self.pair(100)
        self.bot.handle(command(2,2,"/subscribe "+code))
        self.assertEqual(len(self.store.subscriptions(2)),1)
        self.bot.handle(command(2,2,"/subscriptions"))
        self.assertIn(code,self.telegram.messages[-1][1])
        self.bot.handle(command(-100,2,"/subscribe "+code,"group"))
        self.assertEqual(self.store.subscriptions(-100),[])
        self.bot.handle(command(-100,1,"/subscribe "+code,"group"))
        self.assertEqual(len(self.store.subscriptions(-100)),1)
        self.bot.handle(command(2,2,"/unsubscribe "+code))
        self.assertEqual(self.store.subscriptions(2),[])

    def test_stop_and_migration_preserve_publisher_scope(self):
        token,client,code=self.pair(-100)
        self.store.subscribe(5,code)
        self.bot.handle({"message":{"chat":{"id":-100},"migrate_to_chat_id":-200}})
        self.store.deliver(token,"migration","hello",self.telegram,client_id=client)
        self.assertEqual({c for c,_ in self.telegram.messages},{-200,5})
        self.store.disable(-200)
        with self.assertRaises(LookupError): self.store.deliver(token,"stop","hello",self.telegram,client_id=client)
        self.store.advance(30); self.assertEqual(self.store.offset(),31)

    def test_normal_text_and_other_bots_commands_are_ignored(self):
        for text in ["Hello","/connect@OtherBot","/unknown"]: self.bot.handle(command(1,1,text))
        self.assertEqual(self.telegram.messages,[])


class PairingTests(SharedCase):
    def test_single_use_idempotency_rotation_and_client_binding(self):
        pairing=self.store.issue_pairing(1)
        first=self.store.claim(pairing["phrase"],"a"*32)
        self.assertEqual(first,self.store.claim(pairing["phrase"],"a"*32))
        with self.assertRaises(PermissionError): self.store.claim(pairing["phrase"],"b"*32)
        self.store.deliver(first["token"],"one","hello",self.telegram,client_id="a"*32)
        with self.assertRaises(PermissionError): self.store.deliver(first["token"],"wrong","hello",self.telegram,client_id="b"*32)
        replacement=self.store.issue_pairing(1)
        second=self.store.claim(replacement["phrase"],"b"*32)
        with self.assertRaises(PermissionError): self.store.deliver(first["token"],"old","hello",self.telegram,client_id="a"*32)
        self.assertEqual(first["publisher_code"],second["publisher_code"])
        self.assertNotIn(first["token"],self.path.read_bytes().decode(errors="ignore"))

    def test_consuming_phrase_schedules_immediate_deletion(self):
        self.bot.handle(command(1,1,"/connect"))
        phrase=self.telegram.messages[0][1].split("\n\n")[1]
        self.store.claim(phrase,"a"*32);self.store.cleanup(self.telegram)
        self.assertEqual(self.telegram.deleted,[(1,1)])

    def test_cleanup_retries_temporary_failures_and_survives_restart(self):
        self.bot.handle(command(1,1,"/connect"));self.now[0]+=301
        self.telegram.delete_error=429;self.store.cleanup(self.telegram)
        self.assertEqual(self.telegram.deleted,[])
        self.store.close();self.store=Store(self.path,clock=lambda:self.now[0])
        self.telegram.delete_error=0;self.now[0]+=61;self.store.cleanup(self.telegram)
        self.assertEqual(self.telegram.deleted,[(1,1)])

    def test_reissue_invalidates_previous_phrase_and_rate_limit(self):
        old=self.store.issue_pairing(1);self.store.issue_pairing(1)
        with self.assertRaises(PermissionError):self.store.claim(old["phrase"],"a"*32)
        self.assertTrue(all(self.store.allow_claim("one-peer") for _ in range(10)))
        self.assertFalse(self.store.allow_claim("one-peer"));self.now[0]+=61
        self.assertTrue(self.store.allow_claim("one-peer"))


class SubscriptionTests(SharedCase):
    def test_multiple_publishers_semantic_duplicates_and_next_cycle(self):
        a,ca,codea=self.pair(1);b,cb,codeb=self.pair(2)
        self.store.subscribe(3,codea);self.store.subscribe(3,codeb);self.store.subscribe(1,codeb)
        self.store.deliver(a,"a","QR A",self.telegram,client_id=ca,scope="a"*64,signal="qr")
        self.now[0]+=10
        self.store.deliver(b,"b","QR B different wording",self.telegram,client_id=cb,scope="a"*64,signal="qr")
        self.assertEqual(sum(chat==3 for chat,_ in self.telegram.messages),1)
        self.assertEqual(sum(chat==1 for chat,_ in self.telegram.messages),1)
        self.now[0]+=61
        self.store.deliver(b,"c","new QR",self.telegram,client_id=cb,scope="a"*64,signal="qr")
        self.assertEqual(sum(chat==3 for chat,_ in self.telegram.messages),2)

    def test_other_webinar_or_control_type_is_not_suppressed(self):
        a,ca,code=self.pair(1);self.store.subscribe(3,code)
        for event,scope,signal in [("a","a"*64,"qr"),("b","b"*64,"qr"),("c","a"*64,"mts")]:
            self.store.deliver(a,event,"notice",self.telegram,client_id=ca,scope=scope,signal=signal)
        self.assertEqual(sum(chat==3 for chat,_ in self.telegram.messages),3)

    def test_personal_login_error_and_test_never_reach_subscribers(self):
        token,client,code=self.pair(1);self.store.subscribe(2,code)
        for kind in ["login_required","stage_failed","test"]:
            self.store.deliver(token,kind,"personal",self.telegram,client_id=client,kind=kind)
        self.assertEqual([c for c,_ in self.telegram.messages],[1,1,1])

    def test_partial_failure_does_not_repeat_successful_recipients(self):
        token,client,code=self.pair(1);self.store.subscribe(2,code);self.store.subscribe(3,code)
        self.telegram.blocked.add(2)
        with self.assertRaises(TelegramError):self.store.deliver(token,"one","event",self.telegram,client_id=client)
        self.telegram.blocked.clear()
        self.store.subscribe(2,code)
        self.store.deliver(token,"one","event",self.telegram,client_id=client)
        self.assertEqual([c for c,_ in self.telegram.messages],[1,3,2])

    def test_semantic_dedup_survives_process_restart(self):
        a,ca,codea=self.pair(1);b,cb,codeb=self.pair(2)
        self.store.subscribe(3,codea);self.store.subscribe(3,codeb)
        self.store.deliver(a,"first","QR",self.telegram,client_id=ca,scope="a"*64)
        self.store.close();self.store=Store(self.path,clock=lambda:self.now[0]);self.now[0]+=10
        self.store.deliver(b,"second","QR",self.telegram,client_id=cb,scope="a"*64)
        self.assertEqual(sum(c==3 for c,_ in self.telegram.messages),1)



class CodeTests(SharedCase):
    def test_four_digit_codes_keep_leading_zero_and_are_unique(self):
        with patch('pubsub.secrets.randbelow',return_value=0):
            a=self.store.publisher_code(1);b=self.store.publisher_code(2)
        self.assertEqual(a,'0000');self.assertEqual(b,'0001')
        self.store.subscribe(3,a)
        self.assertEqual(self.store.subscriptions(3)[0][0],'0000')

    def test_old_database_keys_cannot_authorize_publisher(self):
        import sqlite3
        self.store.close()
        connection=sqlite3.connect(self.path)
        connection.execute("INSERT INTO chats VALUES(4,'река лес',1)");connection.commit();connection.close()
        self.store=Store(self.path,clock=lambda:self.now[0])
        with self.assertRaises(PermissionError):
            self.store.deliver('река лес','old','event',self.telegram,client_id='a'*32)
        old=self.store.db.execute('SELECT key FROM chats WHERE chat_id=4').fetchone()[0]
        self.assertNotEqual(old,'река лес');self.assertGreater(len(old),40)

class DeliveryTests(SharedCase):
    def setUp(self):
        super().setUp()
        self.server=Server(("127.0.0.1",0),handler_for(self.store,self.telegram))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f"http://127.0.0.1:{self.server.server_port}/partizan_agent"
        self.key,self.client,_=self.pair(100)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();super().tearDown()
    def post(self,event=None,key=None,client=None):
        event=event or {"event_id":"event-1","kind":"attendance_required","title":"Контроль присутствия","body":"QR Pulse","scope":"a"*64,"signal":"qr"}
        request=Request(self.url,json.dumps(event).encode(),{"Content-Type":"application/json","Authorization":"Bearer "+(key or self.key),"X-Partizan-Client":client or self.client})
        try:
            with urlopen(request,timeout=3) as response:return response.status,json.load(response)
        except HTTPError as error:
            with error:return error.code,json.load(error)
    def test_actual_http_delivery_and_concurrent_deduplication(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:results=list(pool.map(lambda _:self.post(),range(4)))
        self.assertTrue(all(status==200 for status,_ in results));self.assertEqual(len(self.telegram.messages),1)
        self.assertEqual(sum(not body["duplicate"] for _,body in results),1)
    def test_publisher_bound_to_token_and_client_not_body_recipient(self):
        key,client,_=self.pair(200)
        self.assertEqual(self.post(key=key,client=client)[0],200)
        self.assertEqual(self.telegram.messages[-1][0],200)
        self.assertEqual(self.post(key=key,client=self.client)[0],401)
        self.assertEqual(self.post(key="x"*43)[0],401)
        self.assertEqual(self.post({"event_id":"bad","kind":"attendance_required","title":"a","body":"b","chat_id":200})[0],400)
    def test_unknown_event_types_and_extra_secret_fields_rejected(self):
        for event in [{"event_id":"bad","kind":"control_browser","title":"a","body":"b"},{"event_id":"bad","kind":"attendance_required","title":"a","body":"b","token":"secret"}]:
            self.assertEqual(self.post(event)[0],400)
    def test_disabled_chat_and_failed_delivery_are_not_success(self):
        self.telegram.blocked.add(100)
        self.assertEqual(self.post()[0],502);self.assertEqual(self.post()[0],410)
    def test_delivery_dedup_survives_process_restart(self):
        self.assertEqual(self.post()[0],200)
        self.store.close();self.store=Store(self.path,clock=lambda:self.now[0])
        self.server.RequestHandlerClass=handler_for(self.store,self.telegram)
        self.assertTrue(self.post()[1]["duplicate"]);self.assertEqual(len(self.telegram.messages),1)


if __name__=="__main__":unittest.main()
