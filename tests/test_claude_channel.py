import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest

from netizen_cli.backends import BackendPolicy
from netizen_cli.channel_app import ChannelApplication
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import InstanceManagementService, ManagementRuntimePort, ScopeCoordinator
from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions, GRACE, UNKNOWN
from tests.support.channel_cards import direct_card_event, form_values, elements
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.claude_client import FakeClaudeClients


@asynccontextmanager
async def owner_channel():
    async with channel_fixture() as fixture:
        fixture.channel.chat_types["oc_direct"] = "p2p"
        clients = FakeClaudeClients()
        clock = [0.]
        async def completion(event):
            await app.handle_ordinary_completion(event)

        ordinary = ClaudeOrdinarySessions(bindings=fixture.store, clients=clients, owner_id="ou_user",
            on_completion=completion, notice=fixture.channel.reply, clock=lambda: clock[0])
        management = InstanceManagementService(bindings=fixture.store, projects=fixture.projects,
            runtime=ManagementRuntimePort(None, ordinary=ordinary, local=ordinary), scope_coordinator=ScopeCoordinator())
        app = ChannelApplication(app_id="cli_test", channel=fixture.channel, runtime=None, ordinary=ordinary,
            bindings=fixture.store, projects=fixture.projects, management=management, policy=BackendPolicy("claude", "ou_user"))
        ordinary.open_admission()
        try:
            yield app, fixture, clients, clock, ordinary
        finally:
            await ordinary.cancel_tasks()
            await app.close()
            await management.close()


class OwnerChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_messages_and_cards_have_no_effects(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            for kind, topic in (("p2p", None), ("p2p", "omt"), ("group", None), ("group", "omt")):
                for body in ("go", "/", "/help more", "/enable", "/new", "/projects", "/config"):
                    with self.subTest(kind=kind, topic=topic, body=body):
                        await app.handle_message(FakeMessage(body, message_id="om", sender_id="other",
                            thread_id=topic, chat_type=kind))
                        self.assertEqual(fx.channel.replies[-1][1], "这个 Claude 实例仅限所有者使用。")
                await app.handle_message(FakeMessage("/help", message_id="om", sender_id="other", thread_id=topic, chat_type=kind))
                self.assertIn("/new", fx.channel.replies[-1][1])
            for sender, second in (("", "ou_user"), ("other", "ou_user"), ("", None)):
                msg = FakeMessage("/help", message_id="om", sender_id=sender)
                msg.sender_id = second
                await app.handle_message(msg)
                self.assertIn("仅限所有者", fx.channel.replies[-1][1])
            msg = FakeMessage("/help", message_id="om", sender_id="other", reply_id="quote")
            await app.handle_message(msg)
            self.assertIn("仅限所有者", fx.channel.replies[-1][1])
            before = len(fx.channel.replies)
            await app.handle_message(FakeMessage("go", message_id="om", sender_id="other", chat_type="group", mentioned_bot=False))
            self.assertEqual(len(fx.channel.replies), before)
            for value in ({}, {"intent": "binding.create"}, {"intent": "turn-file.send"}):
                event = SimpleNamespace(operator=SimpleNamespace(open_id="other"), message_id="om", chat_id="oc_direct",
                                        action=SimpleNamespace(tag="button", value=value))
                await app.handle_card_action(event)
                self.assertIn("仅限所有者", fx.channel.replies[-1][1])
            self.assertFalse(fx.store.has_bindings(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT).key))
            self.assertEqual((clients.calls, fx.channel.reactions, fx.channel.fetch_quoted_calls), ([], [], []))

    async def test_grace_typed_gaps_and_local_forms(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            async def send(text):
                await app.handle_message(FakeMessage(text, message_id="om"))
                return fx.channel.replies[-1][1]

            for body in ("go", "/new", "/defaults", "/config"):
                self.assertEqual(await send(body), GRACE)
            clock[0] = 2.
            for cmd in ("goal this", "side close", "fork", "usage", "compact", "cron", "archive", "delete",
                        "model", "effort", "fast", "speed", "release", "threads archived", "quit", "plan"):
                with self.subTest(command=cmd):
                    self.assertIn("试点暂不支持", await send("/" + cmd))
            self.assertIn("试点暂不支持", await send("$sample do this"))
            self.assertIn("/new", await send("go"))
            for cmd in ("/new", "/defaults"):
                with self.subTest(command=cmd):
                    content = json.dumps((await send(cmd)).card, ensure_ascii=False)
                    self.assertIn("原生 Claude 设置", content)
                    self.assertNotIn("_progress_card", content)
            await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            self.assertIn("本地任务", await send("/status"))
            for cmd in ("/config", "/sessions"):
                content = json.dumps((await send(cmd)).card, ensure_ascii=False)
                self.assertNotIn("Codex", content)
                self.assertNotIn("binding.delete", content)
            self.assertEqual(clients.calls, [])

    async def test_owner_enable_with_only_sender_id(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            seen = []
            def membership(app_id, open_id):
                seen.append((app_id, open_id))
                return open_id == "ou_user"
            app._is_instance_admin = membership
            msg = FakeMessage("/enable", message_id="om", chat_type="group", chat_id="oc_group")
            msg.sender_id = "ou_user"
            del msg.sender.open_id
            await app.handle_message(msg)
            self.assertEqual(seen, [("cli_test", "ou_user")])
            self.assertFalse(clients.calls)

    async def test_owner_forms_and_old_callbacks(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            for kind, feature in (("netizen_cron", "定时任务"), ("netizen_fork", "会话分支"), ("netizen_question", "原生提问")):
                event = direct_card_event(fx.channel, {})
                event.action.value = {"kind": kind}
                await app.handle_card_action(event)
                self.assertIn(feature, fx.channel.replies[-1][1])
            await app.handle_message(FakeMessage("/new", message_id="new"))
            card = fx.channel.replies[-1][1]
            form = form_values(card)
            form["new_project"] = next(item for item in elements(card.card, "select_static") if item["name"] == "new_project")["options"][0]["value"]
            event = direct_card_event(fx.channel, {**form, "new_effort": "high"})
            await app.handle_card_action(event)
            self.assertIn("模型、思考强度或速度", fx.channel.replies[-1][1])
            self.assertIsNone(fx.store.active_binding(scope.key))
            await app.handle_card_action(direct_card_event(fx.channel, form))
            binding = fx.store.active_binding(scope.key)
            self.assertIsNotNone(binding, fx.channel.replies[-1])
            await app.handle_message(FakeMessage("/config", message_id="config"))
            form = form_values(fx.channel.replies[-1][1])
            await app.handle_card_action(direct_card_event(fx.channel, form))
            self.assertIn("设置已保存", fx.channel.replies[-1][1])
            self.assertFalse(clients.calls)

    async def test_terminal_and_unknown_are_text_only(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            for index in (0, 1):
                msg = FakeMessage("hello", message_id=f"om{index}")
                if index == 0:
                    msg.sender_id = msg.sender.open_id
                    del msg.sender.open_id
                await app.handle_message(msg)
                self.assertEqual(len(clients.calls), index+1, fx.channel.replies)
                client = clients.calls[-1]
                client.init()
                if index:
                    client.stream.put_nowait(None)
                else:
                    client.result(result="answer with ![file](unavailable.png)")
                self.assertTrue(await ordinary.wait_idle(2))
                self.assertEqual(fx.channel.replies[-1][1], UNKNOWN if index else "answer with ![file](unavailable.png)")
            self.assertFalse(fx.channel.upload_calls)
