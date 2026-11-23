import asyncio
import json
from unittest.mock import AsyncMock, PropertyMock, patch

from lark_channel import SendResult
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest

from netizen_cli.backends import BackendPolicy, BackendUnsupported
from netizen_cli.channel_app import ChannelApplication
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import InstanceManagementService, ManagementRuntimePort, ScopeCoordinator
from tests.support import has_claude_sdk, requires_claude_sdk

from tests.support.channel_cards import _card_button_value, _elements, direct_button_event, direct_card_event, form_values, elements
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage


if has_claude_sdk:
    from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions, DEGRADED, GRACE
    from tests.support.claude_client import FakeClaudeClients

@asynccontextmanager
@requires_claude_sdk
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
            await app.handle_message(FakeMessage("  /help  ", message_id="om", sender_id="other"))
            self.assertIn("/new", fx.channel.replies[-1][1])
            fx.channel.bot_identity = SimpleNamespace(open_id="ou_bot")
            await app.handle_message(FakeMessage("@Bot /help ", message_id="om", sender_id="other",
                raw_content_type="post", chat_type="group", mentions=[SimpleNamespace(open_id="ou_bot", key="@_user_1")],
                content=SimpleNamespace(post={"content": [[{"tag": "at", "user_id": "@_user_1", "user_name": "Bot"},
                                                           {"tag": "text", "text": " /help "}]]})))
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
            self.assertIn("本地任务", json.dumps((await send("/status")).card, ensure_ascii=False))
            for cmd in ("/config", "/sessions"):
                content = json.dumps((await send(cmd)).card, ensure_ascii=False)
                self.assertNotIn("Codex", content)
                self.assertNotIn("binding.delete", content)
            self.assertEqual(clients.calls, [])

    async def test_running_session_and_status_cards_stop_the_exact_turn(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            await app.handle_message(FakeMessage("work", message_id="om-input"))
            client = clients.calls[-1]
            client.init()
            def check_model_columns(card):
                rows = [[column["elements"][0]["text"]["content"] for column in group["columns"]]
                        for group in _elements(card, "column_set")
                        if all(column["elements"][0]["tag"] == "div" for column in group["columns"])]
                self.assertIn(["Effort：未观测", "Speed：不适用"], rows)

            try:
                for command in ("/sessions", "/status"):
                    with self.subTest(command=command):
                        await app.handle_message(FakeMessage(command, message_id="om-view"))
                        card = fx.channel.replies[-1][1]
                        self.assertIn("执行中", json.dumps(card.card, ensure_ascii=False))
                        action = _card_button_value(card, "停止")
                        self.assertEqual(action["intent"], "binding.stop.exact")
                        self.assertEqual(action["expected_turn_id"], "turn:v1:" + client.inputs[0]["uuid"])
                        self.assertEqual(action["expected_activity_revision"], ordinary.local_snapshot(binding.binding.id).revision)
                check_model_columns(card.card)
                direct_card_event(fx.channel, {}, message_id="om-view")
                await app.handle_card_action(direct_button_event(action, message_id="om-view"))
                self.assertEqual(client.interrupts, 1)
                check_model_columns(fx.channel.updates[-1][1])
            finally:
                client.result()
                self.assertTrue(await ordinary.wait_idle(2))

    async def test_local_commands_never_read_codex_accessors(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            port = app._management._runtime
            with self.assertRaises(BackendUnsupported):
                _ = app._runtime
            with self.assertRaises(BackendUnsupported):
                await port.read_scheduled_turn("unused", "unused")
            await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            with (patch.object(type(app), "_runtime", new_callable=PropertyMock) as channel_codex,
                  patch.object(type(port), "_ManagementRuntimePort__runtime", new_callable=PropertyMock) as management_codex):
                for command in ("/help", "/status", "/new", "/sessions", "/defaults", "/config", "/stop", "/projects", "/admin",
                                "/goal", "/usage", "/archive", "/side", "/fork", "/cron"):
                    await app.handle_message(FakeMessage(command, message_id="om"))
                channel_codex.assert_not_called()
                management_codex.assert_not_called()

    async def test_each_recognized_slash_command_is_checked_once(self):
        from netizen_cli.experience import require_command
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            with patch("netizen_cli.experience.require_command", wraps=require_command) as check:
                for text in ("/", "/help", "/new", "/'config'", "/goal x", "/threads archived"):
                    check.reset_mock()
                    await app.handle_message(FakeMessage(text, message_id="om"))
                    self.assertEqual(check.call_count, 1, text)

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
            fx.channel.fetch_message = AsyncMock(wraps=fx.channel.fetch_message)
            fx.channel.get_chat_info = AsyncMock(wraps=fx.channel.get_chat_info)
            for kind, feature in (("netizen_cron", "定时任务"), ("netizen_fork", "会话分支"), ("netizen_question", "原生提问")):
                event = direct_card_event(fx.channel, {})
                event.action.value = {"kind": kind}
                await app.handle_card_action(event)
                self.assertIn(feature, fx.channel.replies[-1][1])
            fx.channel.reply_results.append(SendResult.ok(message_id="om_card"))
            await app.handle_message(FakeMessage("/new", message_id="new"))
            card = fx.channel.replies[-1][1]
            form = form_values(card)
            form["new_project"] = next(item for item in elements(card.card, "select_static") if item["name"] == "new_project")["options"][0]["value"]
            fx.channel.fetch_message.reset_mock()
            fx.channel.get_chat_info.reset_mock()
            event = direct_card_event(fx.channel, {**form, "new_effort": "high"})
            await app.handle_card_action(event)
            self.assertIn("模型、思考强度或速度", fx.channel.replies[-1][1])
            self.assertIsNone(fx.store.active_binding(scope.key))
            await app.handle_card_action(direct_card_event(fx.channel, {"defaults_session_effort": "high"}))
            self.assertIn("模型、思考强度或速度", fx.channel.replies[-1][1])
            fx.channel.fetch_message.assert_not_awaited()
            fx.channel.get_chat_info.assert_not_awaited()
            form["new_completion_mention"] = next(item for item in elements(card.card, "select_static")
                if item["name"] == "new_completion_mention")["options"][0]["value"]
            await asyncio.gather(*(app.handle_card_action(direct_card_event(fx.channel, form)) for _ in range(2)))
            self.assertIn("会话创建成功", json.dumps(fx.channel.updates[-1], ensure_ascii=False))
            self.assertFalse(elements(fx.channel.updates[-1][1], "form"))
            self.assertEqual(len(fx.store.list_bindings(scope.key)), 1)
            binding = fx.store.active_binding(scope.key)
            self.assertIsNotNone(binding, fx.channel.replies[-1])
            self.assertFalse(binding.task_feedback.completion_mention_enabled)
            await app.handle_message(FakeMessage("without a mention", message_id="om-input"))
            client = clients.calls[-1]
            client.init()
            client.result(result="plain answer")
            self.assertTrue(await ordinary.wait_idle(2))
            self.assertEqual(fx.channel.replies[-1][1], "plain answer")
            await app.handle_message(FakeMessage("/config", message_id="config"))
            form = form_values(fx.channel.replies[-1][1])
            missing_mention = {key: value for key, value in form.items() if key != "config_completion_mention"}
            await app.handle_card_action(direct_card_event(fx.channel, missing_mention))
            self.assertIn("卡片已过期", fx.channel.replies[-1][1])
            self.assertFalse(fx.store.get(binding.id).task_feedback.completion_mention_enabled)
            await app.handle_card_action(direct_card_event(fx.channel, form))
            self.assertIn("设置已保存", fx.channel.replies[-1][1])
            self.assertEqual(len(clients.calls), 1)

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
                    fx.channel.send_results.append(SendResult.ok(message_id="notice", raw={"code": 0, "data": {
                        "message_id": "notice", "chat_id": "oc_direct"}}))
                    client.stream.put_nowait(None)
                else:
                    fx.channel.reply_results.append(SendResult.ok(message_id="om_result"))
                    fx.channel.send_results.append(SendResult.ok(message_id="reminder"))
                    client.result(result="answer with ![file](unavailable.png)")
                self.assertTrue(await ordinary.wait_idle(2))
                reply = fx.channel.replies[-1][1]
                if index:
                    to, notice, opts = fx.channel.send_calls[-1]
                    self.assertEqual((to, notice, opts.reply_to), ("oc_direct", DEGRADED, None))
                else:
                    self.assertIn("图片文件不存在", reply)
                    _, reminder, opts = fx.channel.send_calls[-1]
                    self.assertEqual(opts.reply_to, "om_result")
                    self.assertEqual([mention.open_id for mention in reminder.mentions], ["ou_user"])
            self.assertFalse(fx.channel.upload_calls)

    async def test_degradation_waiting_on_presentation_stays_in_its_topic(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "omt_exact")
            await fx.create_binding(scope)
            source = FakeMessage("go", message_id="om_source", thread_id=scope.topic_id, raw={"root_id": "om_root"})
            fx.channel.send_results.append(SendResult.ok(message_id="om_notice", raw={"code": 0, "data": {
                "message_id": "om_notice", "chat_id": scope.chat_id, "thread_id": scope.topic_id}}))
            await app.handle_message(source)
            cli = clients.calls[-1]
            rendering, release = asyncio.Event(), asyncio.Event()
            async def prepare(result):
                rendering.set()
                await release.wait()
                return result.content
            with patch.object(app, "_prepare_task_result_images", side_effect=prepare):
                cli.init()
                cli.result(result="discard this answer", idle=False)
                await asyncio.wait_for(rendering.wait(), 2)
                cli.result(session_id="foreign", result="wrong-session")
                await asyncio.wait_for(cli.closing.wait(), 2)
                release.set()
                self.assertTrue(await ordinary.wait_idle(2))
            self.assertFalse(fx.channel.replies)
            (chat, notice, opts), = fx.channel.send_calls
            self.assertEqual((chat, notice, opts.reply_to, opts.reply_in_thread, opts.reply_target_gone),
                             (scope.chat_id, DEGRADED, "om_root", True, "fail"))
            self.assertTrue(fx.channel.reaction_removals)
            self.assertNotIn(("om_source", "DONE"), fx.channel.reactions)

    async def test_conversation_result_retries_with_its_uuid_and_accepts_split_sdk_reply(self):
        from netizen_cli.ordinary_sessions import ConversationTarget
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "thread")
            binding = (await fx.create_binding(scope)).binding
            async def send_human(name, root=None):
                await app.handle_message(FakeMessage(name, message_id=name, thread_id="thread", raw={"root_id": root}))
            await send_human("H1")
            cli = clients.calls[-1]
            cli.init()
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
            cli.result(reason=None, result=None)
            async with asyncio.timeout(3):
                while not cli.live.ordered_idle:
                    await asyncio.sleep(.002)
            await send_human("H2", "om_root")
            cli.init()
            cli.result(reason=None, result=None)
            async with asyncio.timeout(5):
                while not ordinary._active[binding.id].admissible:
                    await asyncio.sleep(0)
            await send_human("conflict", "om_foreign")
            self.assertIn("话题根", fx.channel.replies[-1][1])
            self.assertEqual(len(cli.inputs), 2)
            await send_human("H3")
            address = ordinary._active[binding.id].conversation_source
            self.assertEqual(address.topic_root_id, "om_root")
            self.assertEqual(len(cli.inputs), 3)
            raw = {"code": 0, "data": {"message_id": "last", "chat_id": scope.chat_id,
                                       "thread_id": scope.topic_id, "root_id": "om_root"}}
            # The native cron turn has no human owner, so only the learned root can receive its body.
            cli.init()
            cli.result(reason=None, result=None)
            async with asyncio.timeout(5):
                while ordinary._active[binding.id].humans:
                    await asyncio.sleep(.005)
            cli.init(user_message_uuids=[])
            fx.channel.send_results.append(SendResult.ok(message_id="first", chunk_ids=["first", "last"], raw=raw))
            cli.result(result="cron body", user_message_uuid=None, origin={"kind": "task-notification", "producer": "cron"})
            async with asyncio.timeout(5):
                while not any(body == "cron body" for _, body, _ in fx.channel.send_calls):
                    await asyncio.sleep(.005)
            (destination, _, opts), = [call for call in fx.channel.send_calls if call[1] == "cron body"]
            self.assertEqual((destination, opts.reply_to, opts.reply_in_thread), (scope.chat_id, "om_root", True))
            for uid in ("result-a", "result-b"):
                fx.channel.send_results.extend([OSError(), SendResult.ok(message_id="first", chunk_ids=["first", "last"], raw=raw)])
                target = ConversationTarget(scope, address, uid)
                sent = await app._reply_to_origin(target, "long unowned body")
                self.assertEqual(sent.message_id, "first")
            calls = [opts for _, body, opts in fx.channel.send_calls if body == "long unowned body"]
            self.assertEqual([opts.reply_to for opts in calls], ["om_root"] * 4)
            self.assertEqual(calls[0].uuid, calls[1].uuid)
            self.assertEqual(calls[2].uuid, calls[3].uuid)
            self.assertNotEqual(calls[0].uuid, calls[2].uuid)

    async def test_queued_cron_answer_resolves_the_topic_root_at_channel_handoff(self):
        from netizen_cli.ordinary_sessions import PhysicalAnswer
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            from claude_agent_sdk import SystemMessage
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "thread")
            binding = (await fx.create_binding(scope)).binding
            await app.handle_message(FakeMessage("go", message_id="H1", thread_id="thread"))
            cli = clients.calls[-1]
            execution = ordinary._active[binding.id]
            cli.init()
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
            cli.result(reason=None, result=None)
            async with asyncio.timeout(5):
                while execution.humans:
                    await asyncio.sleep(.005)
            first, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
            complete = ordinary._completion
            async def handoff(event):
                if isinstance(event, PhysicalAnswer) and event.final_response == "first cron":
                    first.set()
                    await release.wait()
                await complete(event)
                if isinstance(event, PhysicalAnswer) and event.final_response == "second cron":
                    second.set()
            with patch.object(ordinary, "_completion", side_effect=handoff):
                for body in ("first cron", "second cron"):
                    cli.init(user_message_uuids=[])
                    cli.result(user_message_uuid=None, result=body, idle=False)
                cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
                try:
                    await asyncio.wait_for(first.wait(), 5)
                    async with asyncio.timeout(5):
                        while not execution.admissible or not execution.delivery.queues[None]:
                            await asyncio.sleep(.005)
                    await app.handle_message(FakeMessage("more", message_id="H2", thread_id="thread", raw={"root_id": "root"}))
                    fx.channel.send_results.append(SendResult.ok(message_id="answer", raw={"code": 0, "data": {
                        "message_id": "answer", "chat_id": scope.chat_id, "thread_id": scope.topic_id, "root_id": "root"}}))
                finally:
                    release.set()
                await asyncio.wait_for(second.wait(), 5)
            self.assertEqual([(body, opts.reply_to) for _, body, opts in fx.channel.send_calls if isinstance(body, str)],
                             [("second cron", "root")])

    async def test_settlement_of_cancelled_reaction_cleanup_leaves_only_the_uncertain_id_for_close(self):
        from netizen_cli.bindings import BindingTaskFeedback
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(reaction_pulse_enabled=True))
            await app.handle_message(FakeMessage("go", message_id="H1"))
            execution = ordinary._active[binding.id]
            human = execution.human
            pulse = app._reactions._pulses[human.input_id]
            thinking, typing = pulse.thinking_reaction_id, pulse.typing_reaction_id
            attempted = []
            async def remove(mid, reaction_id):
                attempted.append((mid, reaction_id))
                if len(attempted) == 1:
                    raise asyncio.CancelledError()
                return SendResult.ok()
            with patch.object(fx.channel, "remove_reaction", side_effect=remove):
                cli = clients.calls[0]
                cli.init()
                cli.result(reason=None, result=None)
                self.assertTrue(await ordinary.wait_idle(5))
                self.assertEqual(attempted, [("H1", thinking), ("H1", typing)])
                self.assertFalse(execution.humans or execution.delivery.cleanups)
                self.assertIsNone(pulse.typing_reaction_id)
                self.assertEqual(pulse.thinking_reaction_id, thinking)
                await app.close()
                self.assertEqual(attempted, [("H1", thinking), ("H1", typing), ("H1", thinking)])
                self.assertFalse(app._reactions._pulses)

    async def test_long_conversation_body_reconciles_only_its_unconfirmed_chunk(self):
        from lark_channel import OutboundPost
        from netizen_cli.ordinary_sessions import ConversationTarget
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            def confirm(mid):
                return SendResult.ok(message_id=mid, raw={"code": 0, "data": {
                    "chat_id": scope.chat_id, "message_id": mid, "root_id": mid}})
            fx.channel.send_results.extend([confirm("first"), OSError(), confirm("second")])
            target = ConversationTarget(scope, object(), "result")
            sent = await app._reply_to_origin(target, "a" * 2100 + "\n" + "b" * 2100)
            self.assertEqual(sent.message_id, "first")
            first, lost, retry = fx.channel.send_calls
            self.assertIsInstance(first[1], OutboundPost)
            self.assertIsNotNone(first[1].post)
            self.assertNotEqual(first[2].uuid, lost[2].uuid)
            self.assertEqual((lost[1], lost[2].uuid), (retry[1], retry[2].uuid))
            self.assertTrue(all(opts.reply_to is None for _, _, opts in fx.channel.send_calls))

    async def test_cancelled_and_failed_final_reactions_retire_each_pulse_on_a_retained_client(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            for i, error in enumerate((asyncio.CancelledError(), OSError(), asyncio.CancelledError())):
                mid = f"human-{i}"
                await app.handle_message(FakeMessage("go", message_id=mid))
                cli = clients.calls[-1]
                execution = ordinary._active[binding.id]
                human = execution.human
                self.assertIn(human.input_id, app._reactions._pulses)
                with patch.object(app, "_safe_add_reaction", side_effect=error) as done:
                    cli.init()
                    await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                    cli.result(reason=None, result=None)
                    async with asyncio.timeout(5):
                        while execution.humans or not execution.admissible or execution.delivery.cleanups:
                            await asyncio.sleep(.005)
                    done.assert_awaited_once()
                self.assertNotIn(human.input_id, app._reactions._pulses)
                self.assertIn(mid, [source for source, _ in fx.channel.reaction_removals])
                self.assertFalse(fx.channel.send_calls)
            self.assertEqual((len(clients.calls), len(cli.inputs), cli.closes), (1, 3, 0))

    async def test_cancelled_terminal_waiter_leaves_the_pulse_owned_until_its_reactions_are_removed(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            from netizen_cli.bindings import BindingTaskFeedback
            binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(reaction_pulse_enabled=True))
            frozen, release = asyncio.Event(), asyncio.Event()
            async def hold_pulse(stopped, seconds):
                await stopped.wait()
                frozen.set()
                await release.wait()
                return True
            with patch.object(app._reactions, "_wait", side_effect=hold_pulse):
                try:
                    await app.handle_message(FakeMessage("run", message_id="H1"))
                    cli = clients.calls[-1]
                    execution = ordinary._active[binding.id]
                    cli.init()
                    await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                    cli.result(reason=None, result=None)
                    await asyncio.wait_for(frozen.wait(), 5)
                    worker, = execution.delivery.workers
                    worker.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(worker, 5)
                finally:
                    release.set()
                await asyncio.wait_for(asyncio.gather(*execution.delivery.cleanups), 5)
            self.assertFalse(execution.humans or app._reactions._pulses)
            self.assertEqual([mid for mid, _ in fx.channel.reaction_removals], ["H1", "H1"])
            self.assertNotIn(("H1", "DONE"), fx.channel.reactions)

    async def test_revoked_answer_skips_view_and_image_preparation(self):
        from netizen_cli.ordinary_sessions import PhysicalAnswer
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            event = PhysicalAnswer("b", "session", "turn", "owner", object(), "completed",
                                   "![x](image.png)", None, publishable=lambda: False)
            with patch.object(app, "_result_view") as view, patch.object(app, "_prepare_task_result_images") as images:
                await app.handle_ordinary_completion(event)
            view.assert_not_called()
            images.assert_not_called()
            self.assertFalse(fx.channel.upload_calls or fx.channel.replies)

    async def test_slow_h1_handoff_does_not_block_h2_and_eof_retires_h1_pulse(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            await fx.create_binding(scope)
            rendering, release, h2_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            complete = ordinary._completion
            async def record(event):
                from netizen_cli.ordinary_sessions import LogicalTerminal, PhysicalAnswer
                if isinstance(event, PhysicalAnswer) and event.origin.id == "H1":
                    rendering.set()
                    await release.wait()
                await complete(event)
                if isinstance(event, LogicalTerminal) and event.origin.id == "H2":
                    h2_finished.set()
            ordinary._completion = record
            try:
                await app.handle_message(FakeMessage("first", message_id="H1"))
                cli = clients.calls[-1]
                cli.init()
                await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                cli.result()
                await asyncio.wait_for(rendering.wait(), 3)
                async with asyncio.timeout(3):
                    while not cli.live.ordered_idle:
                        await asyncio.sleep(.005)
                await app.handle_message(FakeMessage("second", message_id="H2"))
                cli.init()
                cli.result()
                await asyncio.wait_for(h2_finished.wait(), 3)
                self.assertIn(("H2", "DONE"), fx.channel.reactions)
                fx.channel.send_results.append(SendResult.ok(message_id="notice", raw={"code": 0, "data": {
                    "message_id": "notice", "chat_id": scope.chat_id}}))
                cli.stream.put_nowait(None)
                await asyncio.wait_for(cli.closing.wait(), 2)
            finally:
                release.set()
            self.assertTrue(await ordinary.wait_idle(3))
            self.assertIn("H1", [mid for mid, _ in fx.channel.reaction_removals])
            self.assertNotIn(("H1", "DONE"), fx.channel.reactions)
            self.assertEqual(sum(body == DEGRADED for _, body, _ in fx.channel.send_calls), 1)
