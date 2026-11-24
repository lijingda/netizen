import asyncio
from itertools import product
import json
from unittest.mock import AsyncMock, PropertyMock, patch
from uuid import UUID

from lark_channel import SendResult
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest

from netizen_cli.backends import BackendPolicy, BackendUnsupported
from netizen_cli.channel_app import ChannelApplication
from netizen_cli.channel.physical_delivery import INCOMPLETE
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import InstanceManagementService, ManagementRuntimePort, ScopeCoordinator
from tests.support import has_claude_sdk, requires_claude_sdk

from tests.support.channel_cards import _card_button_value, _elements, direct_button_event, direct_card_event, form_values, elements
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import sent_result
from tests.support.presenter_traces import CLAUDE_FIXTURE, expect_presenter_trace


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
    def freeze_trace(self, fx, scenario):
        self.enterContext(patch("netizen_cli.runtime.claude_ordinary.uuid4",
                               side_effect=(UUID(int=1), UUID(int=2))))
        def per_turn_headings(trace):
            for i in (3, 5, 6, 7):
                card = trace[i][1][1]
                if i == 6:
                    card = card["card"]
                for field in (card["header"]["title"], card["config"]["summary"]):
                    self.assertEqual(field["content"], "任务已完成")
                    field["content"] = "本轮已完成"

        return expect_presenter_trace(self, fx, scenario, reference=CLAUDE_FIXTURE,
                                      expected_edits=per_turn_headings if scenario == "background_cards" else None)

    async def until(self, predicate):
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(.005)

    async def test_slow_first_card_terminal_takeover_and_text_fallback(self):
        from netizen_cli.bindings import BindingTaskFeedback
        from lark_channel import OutboundCard
        for case in ("late", "oversize", "body limit", "invalid card", "rejected", "unknown", "cancelled", "retired"):
            with self.subTest(case=case):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
                    started, release = asyncio.Event(), asyncio.Event()
                    original = fx.channel.reply
                    async def first(source, content, opts=None):
                        result = await original(source, content, opts)
                        if isinstance(content, OutboundCard):
                            started.set()
                            await release.wait()
                        return result
                    fx.channel.reply_results.append(SendResult.ok(message_id="first"))
                    if case == "rejected":
                        fx.channel.card_update_success = False
                    elif case == "unknown":
                        fx.channel.fail_card_updates = True
                    elif case == "cancelled":
                        from netizen_cli.channel.reply_presenter import _TERMINAL_CARD_UPDATE_ATTEMPTS
                        fx.channel.card_update_results.extend(asyncio.CancelledError() for _ in range(_TERMINAL_CARD_UPDATE_ATTEMPTS))
                    body = "答" * {"oversize": 40_000, "body limit": 100_001}[case] if case in ("oversize", "body limit") else "the physical answer"
                    from netizen_cli.channel.presentation import turn_progress_card
                    def render(**fields):
                        if case == "invalid card" and fields.get("final_response"):
                            raise ValueError("synthetic pre-send validation")
                        return turn_progress_card(**fields)
                    with (patch.object(fx.channel, "reply", side_effect=first),
                          patch("netizen_cli.channel.presentation.turn_progress_card", side_effect=render)):
                        await app.handle_message(FakeMessage("work", message_id="H1"))
                        cli = clients.calls[-1]
                        self.assertFalse(fx.channel.replies or fx.channel.reactions)
                        cli.init()
                        await asyncio.wait_for(started.wait(), 10)
                        if case == "retired":
                            execution = next(iter(ordinary._active.values()))
                            anchor = execution.human.origin
                            cli.stream.put_nowait(None)
                            await self.until(lambda: anchor.retired and not execution.delivery.cleanups)
                        else:
                            cli.result(result=body)
                            await self.until(lambda: cli.live.physical is None)
                        release.set()
                        self.assertTrue(await ordinary.wait_idle(10))
                    cards = [item for _, item in fx.channel.replies if isinstance(item, OutboundCard)]
                    self.assertEqual(len(cards), 1)
                    if case == "retired":
                        self.assertFalse(app._progress_cards._sessions)
                        self.assertFalse(app._reactions._pulses)
                        self.assertFalse(app._progress_cards._card_owners)
                        continue
                    if case == "late":
                        self.assertIn(("first",), [(mid,) for mid, _ in fx.channel.updates])
                        self.assertIn(body, str(fx.channel.updates))
                    elif case in ("oversize", "body limit", "invalid card"):
                        self.assertEqual([(target, item) for target, item in fx.channel.replies if isinstance(item, str)],
                                         [("H1", body)])
                    else:
                        self.assertEqual([item for _, item in fx.channel.replies if not isinstance(item, OutboundCard)],
                                         [INCOMPLETE])


    @requires_claude_sdk
    async def test_explore_bodyless_partial_handoff(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
        from lark_channel import OutboundCard, OutboundPost
        from netizen_cli.bindings import BindingTaskFeedback

        for cards, cut in ((False, "before"), (False, "after"), (True, "before"), (True, "after"), (True, "in flight")):
            with self.subTest(cards=cards, poll=cut):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=cards, reaction_pulse_enabled=True))
                    app._progress_cards._poll_seconds = 60 if cut == "before" else .01
                    # Hide Thinking before the native Result. An empty reaction ID
                    # alone cannot show whether the pulse will add it again.
                    app._reactions._visible_seconds = .001
                    app._reactions._hidden_seconds = 60
                    polled, release = asyncio.Event(), asyncio.Event()
                    wires = []
                    updates = []
                    concurrent = []
                    writing = set()
                    async def reply(source, item, opts=None):
                        mid = f"wire-{len(wires)}"
                        wires.append((mid, source.id, item))
                        if isinstance(item, OutboundPost):
                            polled.set()
                        return SendResult.ok(message_id=mid)
                    async def update(mid, card):
                        if mid in writing:
                            concurrent.append(mid)
                        writing.add(mid)
                        try:
                            if "final partial" in str(card) and "Claude：执行中" in str(card):
                                polled.set()
                                if cut == "in flight":
                                    await release.wait()
                            updates.append((mid, str(card)))
                        finally:
                            writing.discard(mid)
                        return SendResult.ok(message_id=mid)
                    with (patch.object(fx.channel, "reply", side_effect=reply),
                          patch.object(fx.channel, "update_card", side_effect=update)):
                        await app.handle_message(FakeMessage("launch", message_id="H"))
                        cli = clients.calls[-1]
                        cli.init()
                        await self.until(lambda: bool(app._progress_cards._sessions))
                        pulse = next(iter(app._reactions._pulses.values()))
                        await self.until(lambda: pulse.thinking_reaction_id is None)
                        self.assertFalse(pulse.stopped.is_set())
                        def system(kind, **fields):
                            cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **fields}))
                        for uid, block in (("text", TextBlock("final partial")), ("use", ToolUseBlock("tool", "Bash", {}))):
                            cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                uuid=uid, message_id="one-model-message"))
                        system("task_started", task_id="A", run_id="R", tool_use_id="tool")
                        system("background_tasks_changed", tasks=[{"task_id": "A", "run_id": "R"}])
                        if cut != "before":
                            await asyncio.wait_for(polled.wait(), 10)
                        else:
                            self.assertFalse(polled.is_set())
                        cli.result(result=None, reason=None)
                        execution = next(iter(ordinary._active.values()))
                        await self.until(lambda: execution.admissible)
                        try:
                            if cut != "in flight":
                                await self.until(lambda: not execution.delivery.queues)
                                self.assertTrue(pulse.stopped.is_set())
                                self.assertTrue(pulse.task.done())
                                self.assertIsNotNone(pulse.typing_reaction_id)
                                if cards:
                                    self.assertIn("后台任务 1 个", updates[-1][1])
                                    self.assertIn("final partial", updates[-1][1])
                                else:
                                    self.assertEqual([body.markdown for _, _, body in wires],
                                                     ["**阶段性答案**\n\nfinal partial"])
                            system("background_tasks_changed", tasks=[])
                            cli.init(user_message_uuids=[])
                            for index, answer in enumerate(("wake opening", "wake finished")):
                                mid = f"wake-model-{index}"
                                for block in (TextBlock(answer), ToolUseBlock(f"wake-tool-{index}", "Read", {})):
                                    cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                        uuid=mid + type(block).__name__, message_id=mid))
                            cli.result(result=None, reason=None, user_message_uuid=None, idle=False,
                                       origin={"kind": "task-notification", "producer": "session-task", "runId": "R"})
                            system("session_state_changed", state="idle")
                            await self.until(lambda: cli.closes == 1)
                        finally:
                            release.set()
                        self.assertTrue(await ordinary.wait_idle(10))
                    self.assertEqual(concurrent, [])
                    self.assertEqual(fx.channel.reactions.count(("H", "THINKING")), 1)
                    self.assertEqual([mid for _, mid, _ in wires], ["H"] * (2 if cards else 3))
                    if cards:
                        self.assertTrue(all(isinstance(item, OutboundCard) for _, _, item in wires))
                        old = [body for mid, body in updates if mid == wires[0][0]][-1]
                        self.assertIn("final partial", old)
                        self.assertNotIn("turnanswerv1", old)
                        self.assertNotIn("Claude：", old)
                        self.assertNotIn("全部完成", old)
                    else:
                        self.assertIsInstance(wires[0][2], OutboundPost)
                    wake = " ".join(str(item) for _, _, item in wires[1:])
                    self.assertIn("wake opening", wake)
                    self.assertIn("wake finished", wake)
                    self.assertLess(wake.index("wake opening"), wake.index("wake finished"))
                    self.assertNotIn("final partial", wake)

    @requires_claude_sdk
    async def test_card_write_receipts_stay_in_the_humans_scope(self):
        from lark_channel import OutboundCard
        from netizen_cli.bindings import BindingTaskFeedback

        for receipt, field in (("first", "chat_id"), ("first", "thread_id"), ("first", "root_id"),
                               ("first", "parent_id"), ("new terminal card", "chat_id"), ("terminal update", "root_id")):
            with self.subTest(receipt=receipt, field=field):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    cards, updates, fallback = {}, [], []
                    def address(h):
                        return dict(chat_id=f"oc_{h}", thread_id=f"thread-{h}", root_id=f"root-{h}", parent_id=h)
                    def response(h, *, conflict=False, mid=None):
                        mid = mid or f"card-{h}"
                        location = address(h)
                        if conflict:
                            location[field] = address("H1")[field]
                        return SendResult.ok(message_id=mid, raw={"code": 0, "data": {"message_id": mid, **location}})
                    async def send(source, item, opts=None):
                        if isinstance(item, OutboundCard):
                            cards[source.id] = str(item)
                            if source.id == "H2" and receipt != "terminal update":
                                return response("H2", conflict=True, mid="card-H1")
                            return response(source.id)
                        fallback.append((source.id, opts.reply_to, source.chat_id, str(item)))
                        return SendResult.ok(message_id="text-" + source.id)
                    async def update(mid, card):
                        updates.append((mid, str(card)))
                        if mid == "card-H2" and receipt == "terminal update":
                            return response("H2", conflict=True)
                        return SendResult.ok()
                    start = app._progress_cards.open
                    skipped = asyncio.Event()
                    async def start_card(event):
                        if receipt == "new terminal card" and event.target.origin.id == "H2":
                            skipped.set()
                            return False
                        return await start(event)
                    with (patch.object(fx.channel, "reply", side_effect=send),
                          patch.object(fx.channel, "update_card", side_effect=update),
                          patch.object(app._progress_cards, "open", side_effect=start_card)):
                        for h in ("H1", "H2"):
                            loc = address(h)
                            scope = FeishuScope("cli_test", loc["chat_id"], ScopeKind.TOPIC, loc["thread_id"])
                            fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                                task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
                            await app.handle_message(FakeMessage("work", message_id=h, chat_id=scope.chat_id, chat_type="group",
                                                               thread_id=scope.topic_id, raw={"root_id": loc["root_id"]}))
                            cli = clients.calls[-1]
                            cli.init()
                            await self.until(lambda: h in cards or skipped.is_set())
                            cli.result(result="answer-" + h)
                            self.assertTrue(await ordinary.wait_idle(10))
                    self.assertTrue(any(mid == "card-H1" and "answer-H1" in card for mid, card in updates))
                    self.assertFalse(any(mid == "card-H1" and "answer-H2" in card for mid, card in updates))
                    self.assertEqual(sum(mid == "card-H2" for mid, _ in updates), int(receipt == "terminal update"))
                    self.assertEqual(fallback, [("H2", "H2", "oc_H2", "answer-H2" if receipt == "first" else INCOMPLETE)])

    @requires_claude_sdk
    async def test_active_topics_cannot_share_a_writable_card(self):
        from lark_channel import OutboundCard
        from netizen_cli.bindings import BindingTaskFeedback

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            chat = "oc_shared"
            cards, writes, texts = [], [], []
            async def reply(source, content, opts=None):
                if not isinstance(content, OutboundCard):
                    texts.append((opts.reply_to, content))
                    return SendResult.ok(message_id="text-" + source.id)
                cards.append(source.id)
                data = {"message_id": "same-card", "chat_id": chat}
                if source.id == "V":
                    data.update(thread_id="topic-V", root_id="root-V", parent_id="V")
                return SendResult.ok(message_id="same-card", raw={"data": data})
            async def update(mid, card):
                writes.append((mid, str(card)))
                return SendResult.ok(message_id=mid)
            async def start(h):
                scope = FeishuScope("cli_test", chat, ScopeKind.TOPIC, "topic-" + h)
                fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                    task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
                await app.handle_message(FakeMessage("work", message_id=h, chat_id=chat, chat_type="group",
                    thread_id=scope.topic_id, raw={"root_id": "root-" + h}))
                cli = clients.calls[-1]
                cli.init()
                await self.until(lambda: h in cards and len(app._progress_cards._sessions) == len(ordinary._active))
                return cli
            with patch.object(fx.channel, "reply", side_effect=reply), patch.object(fx.channel, "update_card", side_effect=update):
                victim = await start("V")
                other = await start("H")
                other.result(result="answer-H")
                await self.until(lambda: other.closes and len(ordinary._active) == 1)
                victim.result(result="answer-V")
                self.assertTrue(await ordinary.wait_idle(10))
                # Once V retires a fresh anchor can acquire the same ID.
                later = await start("after")
                later.result(result="answer-after")
                self.assertTrue(await ordinary.wait_idle(10))
            self.assertEqual(texts, [("H", "answer-H")])
            self.assertFalse(any("answer-H" in card for _, card in writes))
            for answer in ("answer-V", "answer-after"):
                self.assertTrue(any(mid == "same-card" and answer in card for mid, card in writes))
            self.assertFalse(app._progress_cards._card_owners)

    @requires_claude_sdk
    async def test_contradictory_text_and_partial_receipts_settle_at_the_original_input(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback

        for poll, field in product((False, True), ("chat_id", "thread_id", "root_id", "parent_id")):
            with self.subTest(poll=poll, field=field):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    app._progress_cards._poll_seconds = .01
                    scope = FeishuScope("cli_test", "oc_shared", ScopeKind.TOPIC, "thread-H")
                    binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=False, completion_mention_enabled=True))
                    attempts = []
                    async def reply(source, content, opts=None):
                        attempts.append((opts.reply_to, getattr(content, "markdown", content)))
                        mid = "notice-V" if content == INCOMPLETE else "body-V"
                        return SendResult.ok(message_id=mid, raw={"data": {"message_id": mid, field: "foreign"}})
                    with patch.object(fx.channel, "reply", side_effect=reply):
                        await app.handle_message(FakeMessage("work", message_id="H", chat_id=scope.chat_id, chat_type="group",
                            thread_id=scope.topic_id, raw={"root_id": "root-H"}))
                        cli = clients.calls[-1]
                        cli.init()
                        if poll:
                            for block in (TextBlock("same text"), ToolUseBlock("tool", "Read", {})):
                                cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                    uuid=type(block).__name__, message_id="model"))
                            await self.until(lambda: bool(attempts))
                        cli.result(result="same text")
                        self.assertTrue(await ordinary.wait_idle(10))
                    self.assertEqual(attempts, [("H", "**阶段性答案**\n\nsame text" if poll else "same text"), ("H", INCOMPLETE)])
                    self.assertFalse(fx.channel.send_calls)
                    self.assertEqual(app._progress_cards.binding_location(binding.id).unusable, {"body-V", "notice-V"})

    @staticmethod
    def binding_cache_sizes(presenter, ordinary):
        return tuple(map(len, (presenter._binding_locations, presenter._card_owners, presenter._sessions,
                               ordinary._activity, ordinary._locks, ordinary._active, ordinary._owned_cwds,
                               ordinary._unavailable, ordinary.observations._sessions)))

    @requires_claude_sdk
    async def test_preparing_image_and_quote_across_project_deletion_does_not_restore_bindings(self):
        from lark_channel import ImageContent, ResourceDescriptor
        from tests.support.channel_messages import quoted_inbound

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            baseline = self.binding_cache_sizes(app._progress_cards, ordinary)
            topic = FeishuScope("cli_test", "oc_topic", ScopeKind.TOPIC, "thread")
            group = FeishuScope("cli_test", "oc_quote", ScopeKind.GROUP)
            for scope in (topic, group):
                await app._management.create_current_binding(scope=scope, creator_id="ou_user", project_alias="test")
            image = FakeMessage("![image](img)", message_id="image", chat_id=topic.chat_id, chat_type="group",
                thread_id=topic.topic_id, raw={"root_id": "root"}, raw_content_type="image",
                content=ImageContent(image_key="img"), resources=[ResourceDescriptor(type="image", file_key="img")])
            quote = FakeMessage("work", message_id="quote", chat_id=group.chat_id, chat_type="group", reply_id="original")
            release = asyncio.Event()
            fx.channel.resource_bodies[(image.id, "img")] = release
            async def read_quote(message_id):
                self.assertEqual(message_id, quote.reply.message_id)
                await release.wait()
                return quoted_inbound(message_id=message_id, chat_id=group.chat_id)

            with patch.object(fx.channel, "fetch_inbound_message", side_effect=read_quote) as fetch:
                inputs = [asyncio.create_task(app.handle_message(message)) for message in (image, quote)]
                try:
                    await self.until(lambda: fx.channel.download_resource_calls and fetch.await_count)
                    args = dict(alias="test", expected_revision=fx.store.get_project("test").revision,
                                deadline=asyncio.get_running_loop().time() + 30)
                    preview = await app._management.preview_project_delete(**args)
                    result = await app._management.delete_project(**args, expected_inventory_fingerprint=preview.fingerprint)
                    self.assertTrue(result.deleted, result)
                    self.assertEqual(result.deleted_session_count, 2)
                    self.assertEqual(self.binding_cache_sizes(app._progress_cards, ordinary), baseline)
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(*inputs), 10)
            self.assertFalse(clients.calls)
            self.assertCountEqual([mid for mid, text in fx.channel.replies if "未发送" in str(text)], [image.id, quote.id])
            self.assertEqual(self.binding_cache_sizes(app._progress_cards, ordinary), baseline)

    @requires_claude_sdk
    async def test_deleted_bindings_release_their_addresses_and_runtime_state(self):
        from netizen_cli.channel.topics import CardReplyLocation
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            clients.configure = lambda client: setattr(client, "failure", "connect")
            presenter = app._progress_cards
            baseline = self.binding_cache_sizes(presenter, ordinary)
            for i in range(4):
                scope = FeishuScope("cli_test", "oc_shared", ScopeKind.TOPIC, f"thread-{i}")
                binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user")
                await app.handle_message(FakeMessage("work", message_id="H", chat_id=scope.chat_id, chat_type="group",
                    thread_id=scope.topic_id, raw={"root_id": "root"}))
                self.assertTrue(await ordinary.wait_idle(10))
                self.assertEqual(clients.calls[-1].closes, 1)
                location = presenter.binding_location(binding.id)
                self.assertEqual(location.root, "root")
                anchor = CardReplyLocation(scope, "H", "root", location)
                sent = SendResult.ok(message_id="card")
                self.assertTrue(anchor.validate(sent, card=True).success)
                ordinary.observations.init(binding.id, "s", "p", "model", "/tmp")
                await ordinary.resource_usage(binding.id)
                await ordinary.delete_lazy_resource(binding.id)
                await ordinary.binding_pointer_changed(binding.id, None)
                self.assertFalse(anchor.validate(sent, card=True).success)
                self.assertEqual(self.binding_cache_sizes(presenter, ordinary), baseline)

    @requires_claude_sdk
    async def test_physical_delivery_settles_each_item_once_at_its_target(self):
        from pathlib import Path
        import tempfile
        from lark_channel import OutboundPost, FeishuChannelError, FeishuChannelErrorCode
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.physical_delivery import BODY, PhysicalDelivery
        from netizen_cli.ordinary_sessions import ClaudeInputAnchor, ConversationTarget, LogicalReply, PhysicalAnswer
        from netizen_cli.partial_answers import PartialAnswer
        from tests.support.channel_messages import PNG

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            scope = FeishuScope("cli_test", "oc_topic", ScopeKind.TOPIC, "thread")
            human = FakeMessage("work", message_id="H", chat_id=scope.chat_id, chat_type="group", thread_id=scope.topic_id)
            anchor = ClaudeInputAnchor(scope, human, "root-H", AsyncMock())
            for anonymous, outcome in product((False, True), ("confirmed", "rejected", "unknown", "unattempted", "image failed", "partial rejected")):
                with self.subTest(anonymous=anonymous, outcome=outcome), tempfile.TemporaryDirectory() as cwd:
                    (Path(cwd) / "local.png").write_bytes(PNG)
                    target = ConversationTarget(scope, anchor, "physical") if anonymous else human
                    parts = (PartialAnswer("s", "p", "stage", "segment"), PartialAnswer("foreign", "p", "x", "foreign"))
                    deadline = asyncio.get_running_loop().time() if outcome == "unattempted" else None
                    event = PhysicalAnswer("binding", "s", "p", "owner", target, "completed", "body ![pic](local.png)",
                        cwd=Path(cwd), partial_answers=parts, reply=None if anonymous else LogicalReply(),
                        task_feedback=BindingTaskFeedback(progress_card_enabled=False), delivery_deadline=deadline)
                    notices, attempts, uuids = [], [], []
                    def answer(who, item, opts):
                        if anonymous:
                            self.assertEqual((who, opts.reply_to, opts.reply_in_thread), (scope.chat_id, "root-H", True))
                            uuids.append(opts.uuid)
                        else:
                            self.assertIs(who, human)
                        if item == INCOMPLETE:
                            notices.append(item)
                            raise OSError("notice failed")
                        text = item.markdown if isinstance(item, OutboundPost) else str(item)
                        attempts.append(text)
                        if outcome == "rejected" or (outcome == "partial rejected" and "segment" in text):
                            return SendResult(success=False, raw={"code": 99991400})
                        if outcome == "unknown":
                            raise OSError("lost receipt")
                        return SendResult.ok(message_id="mid", raw={"code": 0, "data": {
                            "message_id": "mid", "chat_id": scope.chat_id, "thread_id": scope.topic_id, "root_id": "root-H"}})
                    async def send(who, item, opts=None):
                        return answer(who, item, opts)
                    async def upload(*args, **kwargs):
                        if outcome == "rejected":
                            raise FeishuChannelError(FeishuChannelErrorCode.UPLOAD_FAILED, "denied", context={"raw_code": 99991400})
                        if outcome in ("unknown", "image failed"):
                            raise OSError("lost upload receipt")
                        return "img_ledger"
                    ledger = PhysicalDelivery()
                    with (patch.object(fx.channel, "reply", side_effect=send),
                          patch.object(fx.channel, "send", side_effect=send),
                          patch.object(fx.channel, "upload_media", side_effect=upload) as uploads,
                          patch.object(app._progress_cards, "physical_delivery", return_value=ledger)):
                        await app.handle_ordinary_completion(event)
                    body = "confirmed" if outcome in ("image failed", "partial rejected") else outcome
                    self.assertEqual(ledger.items, {BODY: body, ("partial", "stage"): "rejected" if outcome == "partial rejected" else body,
                                                    ("image", "local.png"): "unknown" if outcome == "image failed" else body})
                    self.assertEqual(len(notices), int(outcome != "confirmed"))
                    self.assertEqual(len(attempts), 0 if outcome == "unattempted" else 2)
                    self.assertEqual(uploads.await_count, int(outcome != "unattempted"))
                    self.assertNotIn("foreign", str(attempts))
                    if outcome == "confirmed":
                        self.assertIn("img_ledger", attempts[-1])
                    self.assertEqual(len(uuids), len(set(uuids)))

    @requires_claude_sdk
    async def test_cron_deadline_records_the_inflight_fragment_and_the_unattempted_tail(self):
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.physical_delivery import BODY, PhysicalDelivery
        from netizen_cli.ordinary_sessions import ClaudeInputAnchor, ConversationTarget, PhysicalAnswer
        from netizen_cli.partial_answers import PartialAnswer
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            scope = FeishuScope("cli_test", "oc_chat", ScopeKind.TOPIC, "thread")
            target = ConversationTarget(scope, ClaudeInputAnchor(scope, object(), "root", AsyncMock()), "cron")
            deadline = asyncio.get_running_loop().time() + 30
            parts = tuple(PartialAnswer("s", "p", str(i), f"segment[{i}]") for i in range(13))
            event = PhysicalAnswer("binding", "s", "p", "owner", target, "completed", "final",
                                   partial_answers=parts, task_feedback=BindingTaskFeedback(progress_card_enabled=False),
                                   delivery_deadline=deadline)
            timeout_at = asyncio.timeout_at
            timers, attempts = [], []
            def track(when):
                timer = timeout_at(when)
                if when == deadline:
                    timers.append(timer)
                return timer
            async def send(chat, content, opts):
                self.assertEqual((chat, opts.reply_to, opts.reply_in_thread), (scope.chat_id, "root", True))
                attempts.append((content, opts.uuid))
                if "segment[1]" in content:
                    timers[0].reschedule(asyncio.get_running_loop().time())
                    await asyncio.Future()
                return SendResult.ok(message_id="mid", raw={"code": 0, "data": {"message_id": "mid",
                    "chat_id": chat, "thread_id": scope.topic_id, "root_id": "root"}})
            ledger = PhysicalDelivery()
            with (patch("asyncio.timeout_at", side_effect=track), patch.object(fx.channel, "send", side_effect=send),
                  patch.object(app._progress_cards, "physical_delivery", return_value=ledger)):
                await app.handle_ordinary_completion(event)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(attempts[-1][0], INCOMPLETE)
            self.assertEqual(len({uid for _, uid in attempts}), 3)
            self.assertEqual(ledger.items[BODY], "unattempted")
            self.assertEqual([ledger.items[("partial", str(i))] for i in range(13)],
                             ["confirmed", "unknown", *(["unattempted"] * 11)])
            self.assertFalse(ledger.settle())

    @requires_claude_sdk
    async def test_later_humans_share_the_verified_root_and_revoke_a_foreign_card(self):
        from lark_channel import OutboundCard
        from netizen_cli.bindings import BindingTaskFeedback
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_shared", ScopeKind.TOPIC, "thread-H")
            binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
            cards, writes, plain = [], [], []
            async def reply(source, content, opts=None):
                mid = "card-H" if source.id == "H1" else "card-V"
                if not isinstance(content, OutboundCard):
                    plain.append((source.id, content))
                    return SendResult.ok(message_id="text-" + source.id)
                cards.append(source.id)
                data = {"message_id": mid, "chat_id": scope.chat_id, "parent_id": source.id}
                if source.id != "H3":
                    data["root_id"] = "root-H" if source.id == "H1" else "root-V"
                return SendResult.ok(message_id=mid, raw={"code": 0, "data": data})
            async def update(mid, content):
                writes.append(mid)
                return SendResult.ok()
            with patch.object(fx.channel, "reply", side_effect=reply), patch.object(fx.channel, "update_card", side_effect=update):
                for mid in ("H1", "H2", "H3"):
                    await app.handle_message(FakeMessage("work", message_id=mid, chat_id=scope.chat_id, chat_type="group",
                        thread_id=scope.topic_id, raw={"root_id": "root-H"} if mid == "H1" else {}))
                    cli = clients.calls[-1]
                    cli.init()
                    if mid == "H1":
                        await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                    await self.until(lambda: mid in cards)
                    cli.result(result="body-" + mid)
                    await self.until(lambda: not ordinary._active[binding.id].humans)
                self.assertEqual(len(clients.calls), 1)
            self.assertNotIn("card-V", writes)
            self.assertIn("card-H", writes)
            self.assertEqual(plain, [("H2", "body-H2"), ("H3", "body-H3")])

    @requires_claude_sdk
    async def test_unconfirmed_partial_matching_the_prepared_body_is_never_replayed(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            app._progress_cards._poll_seconds = .01
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=False, completion_mention_enabled=False))
            attempts = []
            async def reject(source, item, opts=None):
                attempts.append(getattr(item, "markdown", item))
                return SendResult(success=False, raw={"code": 99991400})
            with patch.object(fx.channel, "reply", side_effect=reject):
                await app.handle_message(FakeMessage("work", message_id="H"))
                cli = clients.calls[-1]
                cli.init()
                for block in (TextBlock("same text"), ToolUseBlock("tool", "Read", {})):
                    cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                        uuid=type(block).__name__, message_id="model"))
                await self.until(lambda: bool(attempts))
                cli.result(result="same text")
                self.assertTrue(await ordinary.wait_idle(10))
            self.assertEqual(attempts, ["**阶段性答案**\n\nsame text", INCOMPLETE])

    @requires_claude_sdk
    async def test_displayed_local_image_partial_keeps_the_prepared_final_image(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=False, completion_mention_enabled=False))
            app._progress_cards._poll_seconds = .01
            (fx.project / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            fx.channel.upload_results.append("img_v3_prepared")
            raw = "![图](chart.png)"
            await app.handle_message(FakeMessage("work", message_id="H"))
            cli = clients.calls[-1]
            cli.init()
            for block in (TextBlock(raw), ToolUseBlock("tool", "Read", {})):
                cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                    uuid=type(block).__name__, message_id="model"))
            await self.until(lambda: bool(fx.channel.replies))
            cli.result(result=raw)
            self.assertTrue(await ordinary.wait_idle(10))
            self.assertIn(raw, str(fx.channel.replies[0][1]))
            self.assertEqual(len(fx.channel.upload_calls), 1)
            self.assertEqual(fx.channel.replies[1:], [("H", "![图](img_v3_prepared)")])

    @requires_claude_sdk
    async def test_partial_batch_settles_its_tail_before_done(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from lark_channel import OutboundCard, OutboundPost
        from netizen_cli.bindings import BindingTaskFeedback

        for case in ("plain", "oversize card", "deadline", "deadline exhausted"):
            with self.subTest(case=case):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    cards = case == "oversize card"
                    fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=False))
                    app._progress_cards._poll_seconds = 60
                    attempted, notices, order = [], [], []
                    original_reaction = fx.channel.add_reaction
                    async def react(mid, emoji):
                        if emoji == "DONE":
                            order.append("done")
                        return await original_reaction(mid, emoji)
                    async def notice(source, text):
                        notices.append((source.id, text))
                        order.append("notice")
                    async def send(source, item, opts=None):
                        if isinstance(item, OutboundCard):
                            return SendResult.ok(message_id="initial")
                        if item == INCOMPLETE:
                            await notice(source, item)
                            return SendResult.ok(message_id="notice")
                        attempted.append(item.markdown if isinstance(item, OutboundPost) else str(item))
                        order.append("send")
                        if case in ("deadline", "deadline exhausted"):
                            if len(attempted) == 1 or case == "deadline exhausted":
                                await asyncio.Future()
                        else:
                            await asyncio.sleep(.5)  # Each request fits in 5 s; twelve exceed it together.
                        order.append("ack")
                        return SendResult.ok(message_id=f"reply-{len(attempted)}")
                    with (patch.object(fx.channel, "reply", side_effect=send),
                          patch.object(fx.channel, "add_reaction", side_effect=react),
                          patch.object(ordinary, "_notice", side_effect=notice)):
                        await app.handle_message(FakeMessage("launch", message_id="H"))
                        cli = clients.calls[-1]
                        cli.init()
                        await self.until(lambda: bool(app._progress_cards._sessions))
                        if case in ("deadline", "deadline exhausted"):
                            # The first request times out while the physical turn still has time for more items.
                            app._progress_cards._operation_timeout_seconds = 1
                        for i in range(12):
                            mid = f"model-{i}"
                            answer = f"fragment[{i}]" + ("字" * 3600 if cards else "")
                            for block in (TextBlock(answer), ToolUseBlock(f"tool-{i}", "Read", {})):
                                cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                    uuid=mid + type(block).__name__, message_id=mid))
                        original_budget = ordinary._remaining
                        def remaining(seconds):
                            if seconds == 30 and case in ("deadline", "deadline exhausted"):
                                return 1.5
                            return original_budget(seconds)
                        with patch.object(ordinary, "_remaining", side_effect=remaining):
                            cli.result(result="final body")
                            self.assertTrue(await ordinary.wait_idle(20))
                    expected = 2 if case == "deadline exhausted" else 12
                    for i in range(12):
                        self.assertEqual(sum(f"fragment[{i}]" in post for post in attempted), int(i < expected))
                    self.assertEqual(sum("final body" in post for post in attempted), int(expected == 12))
                    self.assertEqual(order[-1], "done")
                    if case in ("deadline", "deadline exhausted"):
                        self.assertEqual(order[-2:], ["notice", "done"])
                        self.assertEqual(notices, [("H", INCOMPLETE)])
                    else:
                        self.assertEqual(order[-2:], ["ack", "done"])
                        self.assertFalse(notices)

    @requires_claude_sdk
    async def test_card_fallback_freezes_the_last_visible_partials(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from lark_channel import OutboundCard
        from netizen_cli.bindings import BindingTaskFeedback

        for case in ("first rejected", "terminal rejected", "oversize body", "oversize partials", "running validation"):
            with self.subTest(case=case):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True))
                    visible = {}
                    app._progress_cards._poll_seconds = .01
                    async def reply(source, item, opts=None):
                        self.assertEqual(source.id, "H")
                        if case == "first rejected" and not visible and isinstance(item, OutboundCard):
                            return SendResult(success=False)
                        mid = f"reply-{len(visible)}"
                        visible[mid] = str(item)
                        return SendResult.ok(message_id=mid)
                    async def update(mid, card):
                        if case == "terminal rejected" and "terminal body" in str(card):
                            return SendResult(success=False)
                        visible[mid] = str(card)
                        return SendResult.ok(message_id=mid)
                    from netizen_cli.channel.reply_presenter import turn_progress_card
                    def render(**fields):
                        if case == "running validation" and fields["snapshot"].partial_answers:
                            raise ValueError("running card validation failed")
                        return turn_progress_card(**fields)
                    with (patch.object(fx.channel, "reply", side_effect=reply),
                          patch.object(fx.channel, "update_card", side_effect=update),
                          patch("netizen_cli.channel.reply_presenter.turn_progress_card", side_effect=render)):
                        await app.handle_message(FakeMessage("work", message_id="H"))
                        cli = clients.calls[-1]
                        cli.init()
                        await self.until(lambda: bool(app._progress_cards._sessions))
                        def partial(index, text):
                            mid = f"model-{index}"
                            for block in (TextBlock(text), ToolUseBlock(f"tool-{index}", "Read", {})):
                                cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                    uuid=mid + type(block).__name__, message_id=mid))
                        partial(0, "polled fragment")
                        await self.until(lambda: any("polled fragment" in item for item in visible.values()))
                        # Stop the poll at a known snapshot, before the terminal-only fragments arrive.
                        session = next(iter(app._progress_cards._sessions.values()))
                        session.stopped.set()
                        await asyncio.wait_for(session.task, 10)
                        labels = ["polled fragment"]
                        for i in range(1, 10 if case == "oversize partials" else 2):
                            label = f"terminal fragment {i}"
                            partial(i, label + ("字" * 3600 if case == "oversize partials" else ""))
                            labels.append(label)
                        body = "terminal body" if case == "terminal rejected" else "大" * 40000 if case == "oversize body" else None
                        cli.result(result=body, reason="completed" if body else None)
                        self.assertTrue(await ordinary.wait_idle(10))
                    for label in labels + ([body] if body else []):
                        expected = int(case != "terminal rejected" or label == "polled fragment")
                        self.assertEqual(sum(label in msg for msg in visible.values()), expected, label[:35])
                    if case == "terminal rejected":
                        self.assertEqual(sum(INCOMPLETE in item for item in visible.values()), 1)
                    if case == "running validation":
                        self.assertNotIn("polled fragment", visible["reply-0"])
                        self.assertNotIn("terminal fragment", visible["reply-0"])
                    elif case != "first rejected":
                        self.assertIn("polled fragment", visible["reply-0"])
                        self.assertNotIn("terminal fragment", visible["reply-0"])
                        if case != "terminal rejected":
                            self.assertNotIn("Claude：", visible["reply-0"])
                        self.assertNotIn("全部完成", visible["reply-0"])

    @requires_claude_sdk
    async def test_card_fallback_freezes_the_first_card_with_the_physical_outcome(self):
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.presentation import turn_progress_card

        def render(**fields):
            if fields.get("final_response"):
                raise ValueError("terminal card validation failed")
            return turn_progress_card(**fields)

        cases = (("error_max_turns", "max_turns", True, "任务未完成"),
                 ("error_during_execution", "aborted_streaming", True, "任务已中断"),
                 ("success", "completed", False, "任务已完成"))
        for subtype, reason, is_error, header in cases:
            with self.subTest(reason=reason):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
                    fx.channel.reply_results.append(SendResult.ok(message_id="first"))
                    await app.handle_message(FakeMessage("work", message_id="H"))
                    cli = clients.calls[-1]
                    cli.init()
                    await self.until(lambda: bool(app._progress_cards._sessions))
                    with patch("netizen_cli.channel.presentation.turn_progress_card", side_effect=render):
                        cli.result(subtype, reason, is_error)
                        self.assertTrue(await ordinary.wait_idle(10))
                    self.assertIsInstance(fx.channel.replies[1][1], str)
                    mid, frozen = fx.channel.updates[-1]
                    self.assertEqual(mid, "first")
                    self.assertEqual(frozen["header"]["title"]["content"], header)

    @requires_claude_sdk
    async def test_completion_mention_uses_the_feishu_receipt(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback

        receipts = ((None, "raw", None, "raw"), ("top", None, None, "top"), ("same", "same", None, "same"),
                    ("wrong", "other", None, "H"), ("first", "last", ["first", "last"], "H"),
                    ("first", None, ["first", "last"], "first"), (None, None, None, None))
        cases = [*product(("body", "partial"), receipts), ("empty", (None, None, None, "H"))]
        for kind, (direct, nested, chunks, anchor) in cases:
            with self.subTest(kind=kind, direct=direct, nested=nested, chunks=chunks):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=False, completion_mention_enabled=True))
                    await app.handle_message(FakeMessage("work", message_id="H"))
                    cli = clients.calls[-1]
                    cli.init()
                    if anchor:
                        receipt = SendResult.ok(message_id=direct, chunk_ids=chunks, raw={"code": 0, "data": {"message_id": nested}})
                    else:
                        receipt = OSError("delivery unknown")
                    with patch.object(fx.channel, "reply", side_effect=[receipt, SendResult.ok(message_id="notice")]) as sends:
                        if kind == "partial":
                            for block in (TextBlock("answer"), ToolUseBlock("tool", "Read", {})):
                                cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                    uuid=type(block).__name__, message_id="model"))
                        if kind == "body":
                            cli.result(result="answer")
                        else:
                            cli.result(result=None, reason=None)
                        self.assertTrue(await ordinary.wait_idle(10))
                        self.assertEqual(sends.await_count, int(kind != "empty") + int(not anchor))
                        if not anchor:
                            self.assertEqual(sends.await_args.args[1], INCOMPLETE)
                    self.assertEqual([(opts.reply_to, [m.open_id for m in item.mentions])
                                      for _, item, opts in fx.channel.send_calls], [(anchor, ["ou_user"])] if anchor else [])

    @requires_claude_sdk
    async def test_bodyless_human_tracks_background_and_best_effort_updates_do_not_gate_terminal(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel import constants

        for failure in ("none", "cancelled", "error"):
            with self.subTest(failure=failure):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True, reaction_pulse_enabled=True,
                                                          completion_mention_enabled=True))
                    fx.channel.reply_results.extend(SendResult.ok(message_id=mid) for mid in ("initial", "wake"))
                    await app.handle_message(FakeMessage("launch", message_id="H1"))
                    cli = clients.calls[-1]
                    cli.init()
                    await self.until(lambda: bool(app._progress_cards._sessions))
                    def system(kind, **data):
                        cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))
                    cli.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {})], "model", session_id=cli.session))
                    system("task_started", task_id="A", run_id="R", tool_use_id="tool")
                    system("background_tasks_changed", tasks=[{"task_id": "A", "run_id": "R"}])
                    cli.result(result=None, reason=None)
                    await self.until(lambda: "后台任务 1 个" in str(fx.channel.updates))
                    pulse = next(iter(app._reactions._pulses.values()))
                    await self.until(lambda: not ordinary._active[binding.id].delivery.queues)
                    self.assertTrue(pulse.stopped.is_set())
                    self.assertTrue(pulse.task.done())
                    self.assertIsNone(pulse.thinking_reaction_id)
                    self.assertIsNotNone(pulse.typing_reaction_id)
                    self.assertEqual(len(fx.channel.replies), 1)
                    human = ordinary._active[binding.id].human
                    old = human.reply.refresh
                    cleared = []
                    async def cleanup(state):
                        if state is None:
                            cleared.append(human.reply.message_id)
                            if failure != "none":
                                raise asyncio.CancelledError() if failure == "cancelled" else OSError()
                        await old(state)
                    human.reply.refresh = cleanup
                    system("background_tasks_changed", tasks=[])
                    cli.init(user_message_uuids=[])
                    cli.result(result="wake-answer", user_message_uuid=None, origin={"kind": "task-notification", "producer": "session-task", "runId": "R"}, idle=False)
                    await self.until(lambda: bool(cleared))
                    self.assertEqual(cleared, ["wake"])
                    refreshed = []
                    refresh = human.reply.refresh
                    async def final(state):
                        refreshed.append(state.line)
                        if failure != "none":
                            raise asyncio.CancelledError() if failure == "cancelled" else OSError()
                        await refresh(state)
                    human.reply.refresh = final
                    system("session_state_changed", state="idle")
                    self.assertTrue(await ordinary.wait_idle(10))
                    self.assertIn("全部完成", refreshed)
                    self.assertEqual(sum("wake-answer" in str(item) for _, item in fx.channel.replies), 1)
                    self.assertIn(("H1", constants.DONE_REACTION), fx.channel.reactions)
                    self.assertEqual([opts.reply_to for _, body, opts in fx.channel.send_calls if body.mentions], ["wake"])
                    if failure == "none":
                        last_initial = [card for mid, card in fx.channel.updates if mid == "initial"][-1]
                        self.assertNotIn("Claude：", str(last_initial))

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
                    self.assertIn("_progress_card", content)
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

    async def test_stop_before_physical_init_cannot_suppress_a_later_success(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            await app.handle_message(FakeMessage("work", message_id="H1"))
            await app.handle_message(FakeMessage("/stop", message_id="control"))
            cli = clients.calls[-1]
            self.assertEqual(cli.interrupts, 1)
            cli.init()
            fx.channel.reply_results.append(SendResult.ok(message_id="answer"))
            cli.result()
            self.assertTrue(await ordinary.wait_idle(10))
            self.assertEqual([opts.reply_to for _, body, opts in fx.channel.send_calls if getattr(body, "mentions", ())],
                             ["answer"])
    async def test_explore_owned_answers_on_the_channel(self):
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.ordinary_sessions import PhysicalAnswer
        for cards in (False, True):
            for order in ("BC", "CB"):
                with self.subTest(cards=cards, departures=order):
                    async with owner_channel() as (app, fx, clients, clock, ordinary):
                        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
                        clock[0] = 2.
                        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                        binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                            task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=True))
                        held, release = asyncio.Event(), asyncio.Event()
                        confirmed = {}
                        wire = {"H1": [], "H2": []}
                        last_version = {}
                        versions = {}
                        bodies = ("launch-AB", "launch-C", "wake-A", "wake-B", "wake-C")
                        original_reply, original_update = fx.channel.reply, fx.channel.update_card
                        def observe(mid, content):
                            last_version[mid] = str(content)
                            for body in bodies:
                                if body in str(content) and body not in versions.setdefault(mid, set()):
                                    versions[mid].add(body)
                                    wire[confirmed[mid]].append(body)
                        async def reply(source, content, opts=None):
                            await original_reply(source, content, opts)
                            mid = "reply-" + str(len(confirmed))
                            confirmed[mid] = source.id
                            observe(mid, content)
                            return SendResult.ok(message_id=mid)
                        fallback = cards and order == "CB"
                        async def update(mid, card):
                            if fallback and "launch-AB" in str(card):
                                return SendResult(success=False)
                            observe(mid, card)
                            return await original_update(mid, card)
                        deliver = ordinary._completion
                        async def handoff(event):
                            if isinstance(event, PhysicalAnswer) and event.final_response == "wake-A":
                                held.set()
                                await release.wait()
                            await deliver(event)
                        with (patch.object(fx.channel, "reply", side_effect=reply),
                              patch.object(fx.channel, "update_card", side_effect=update),
                              patch.object(ordinary, "_completion", side_effect=handoff)):
                            await app.handle_message(FakeMessage("first", message_id="H1"))
                            cli = clients.calls[-1]
                            tasks = set()
                            def system(kind, **fields):
                                cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **fields}))
                            def membership():
                                system("background_tasks_changed", tasks=[{"task_id": k, "run_id": "r"+k} for k in sorted(tasks)])
                            def assistant(block, message_id, parent=None):
                                cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                    uuid=message_id+type(block).__name__, message_id=message_id, parent_tool_use_id=parent))
                            def launch(keys):
                                for k in keys:
                                    assistant(ToolUseBlock("tool"+k, "Bash", {}), "launch"+k)
                                    system("task_started", task_id=k, run_id="r"+k, tool_use_id="tool"+k)
                                    tasks.add(k)
                                membership()
                            def result(k):
                                cli.result(result="wake-"+k, user_message_uuid=None, idle=False,
                                    origin={"kind": "task-notification", "producer": "session-task", "runId": "r"+k})
                            cli.init()
                            h1 = cli.inputs[0]["uuid"]
                            def activity():
                                return ordinary.turn_activity(binding.id, thread_id=cli.session, turn_id=h1)
                            await self.until(lambda: activity() and (not cards or bool(confirmed)))
                            revision = activity().revision
                            assistant(TextBlock("initial-fragment"), "initial")
                            assistant(ToolUseBlock("read", "Read", {}), "initial")
                            await self.until(lambda: bool(activity().partial_answers))
                            self.assertGreater(activity().revision, revision)
                            launch("AB")
                            cli.result(result="launch-AB")
                            await self.until(lambda: ordinary._active[binding.id].admissible
                                             and not ordinary._active[binding.id].delivery.queues)
                            self.assertEqual(wire["H1"], [] if fallback else ["launch-AB"])
                            if cards and not fallback:
                                self.assertTrue(any("空闲，后台任务 2 个" in s for s in last_version.values()))
                            elif fallback:
                                self.assertEqual(fx.channel.replies.count(("H1", INCOMPLETE)), 1)
                            else:
                                self.assertIn("后台任务 2 个运行中，可继续发送消息", str(fx.channel.replies))
                            await app.handle_message(FakeMessage("second", message_id="H2"))
                            tasks.remove("A")
                            membership()
                            cli.init(user_message_uuids=[])
                            assistant(TextBlock("child-secret"), "child", "toolA")
                            assistant(ToolUseBlock("nested-child", "Read", {}), "child", "toolA")
                            assistant(TextBlock("wake-fragment"), "answer")
                            assistant(ToolUseBlock("more", "Read", {}), "answer")
                            await self.until(lambda: bool(cli.live.partials))
                            self.assertIsNone(ordinary.turn_activity(binding.id, thread_id=cli.session, turn_id=cli.inputs[-1]["uuid"]))
                            self.assertFalse(any(anchor == "H2" for anchor, _ in fx.channel.reactions + fx.channel.replies))
                            stop_wake = order == "CB"
                            if stop_wake:
                                await app.handle_message(FakeMessage("/stop", message_id="esc"))
                                self.assertEqual(cli.interrupts, 1)
                            result("A")
                            await asyncio.wait_for(held.wait(), 10)
                            try:
                                cli.init()
                                launch("C")
                                cli.result(result="launch-C")
                                await self.until(lambda: "launch-C" in wire["H2"])
                                if not cards:
                                    self.assertIn(("H2", "launch-C\n\n后台任务 1 个运行中，可继续发送消息"), fx.channel.replies)
                                for k in order:
                                    tasks.remove(k)
                                    membership()
                                    cli.init(user_message_uuids=[])
                                    result(k)
                                    system("session_state_changed", state="idle")
                                await self.until(lambda: cli.closes == 1)
                            finally:
                                release.set()
                            self.assertTrue(await ordinary.wait_idle(10))
                        self.assertEqual(wire, {"H1": ([] if fallback else ["launch-AB"]) + ["wake-A", "wake-B"], "H2": ["launch-C", "wake-C"]})
                        self.assertFalse(any("child-secret" in s for s in last_version.values()))
                        self.assertTrue(any("wake-fragment" in s and confirmed[mid] == "H1" for mid, s in last_version.items()))
                        reminders = [opts.reply_to for _, body, opts in fx.channel.send_calls if getattr(body, "mentions", ())]
                        self.assertCountEqual([confirmed[mid] for mid in reminders], ["H2"] if stop_wake else ["H1", "H2"])
                        self.assertCountEqual([next(b for b in bodies if b in last_version[mid]) for mid in reminders],
                                              ["wake-C"] if stop_wake else ["wake-B", "wake-C"])
                        if cards:
                            for mid, text in last_version.items():
                                if any(body in text for body in ("launch-AB", "launch-C", "wake-A")):
                                    self.assertNotIn("Claude：", text)
                                    self.assertNotIn("全部完成", text)
                        self.assertEqual((cli.consumers, len(cli.inputs), cli.closes), (1, 2, 1))

    @requires_claude_sdk
    async def test_explore_stop_acknowledgment_and_final_membership(self):
        from claude_agent_sdk import SystemMessage

        for removed_before_ack in (True, False):
            with self.subTest(removed_before_ack=removed_before_ack):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    binding = (await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))).binding
                    await app.handle_message(FakeMessage("run", message_id="H"))
                    cli = clients.calls[-1]
                    execution = ordinary._active[binding.id]
                    def system(kind, **data):
                        cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))
                    cli.init()
                    system("background_tasks_changed", tasks=[{"task_id": "A", "run_id": "R"}])
                    cli.result()
                    await self.until(lambda: execution.admissible)
                    before_ack = []
                    def remove(task):
                        system("task_notification", task_id=task, status="stopped")
                        system("background_tasks_changed", tasks=[])
                    async def stop(task):
                        if removed_before_ack:
                            remove(task)
                            await self.until(lambda: not cli.live.tasks)
                        before_ack.append((execution.close is None, cli.closes))
                    with patch.object(cli, "stop_task", side_effect=stop):
                        await app.handle_message(FakeMessage("/stop all", message_id="esc"))
                    self.assertEqual(before_ack, [(True, 0)])
                    self.assertTrue(any(mid == "esc" and "1 个后台任务发送停止请求" in body for mid, body in fx.channel.replies))
                    if not removed_before_ack:
                        remove("A")
                    self.assertTrue(await ordinary.wait_idle(10))
                    self.assertEqual(cli.closes, 1)

    async def test_preinit_stop_deduplicates_only_the_same_physical_target(self):
        for named_stop in ("/stop", "/stop all"):
            with self.subTest(named_stop=named_stop):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    await fx.create_binding(scope)
                    await app.handle_message(FakeMessage("run", message_id="H"))
                    cli = clients.calls[-1]
                    await app.handle_message(FakeMessage("/stop", message_id="before"))
                    self.assertEqual(cli.interrupts, 1)
                    cli.init()
                    await self.until(lambda: cli.live.physical is not None)
                    await app.handle_message(FakeMessage(named_stop, message_id="named"))
                    await app.handle_message(FakeMessage("/stop", message_id="duplicate"))
                    self.assertEqual(cli.interrupts, 2)
                    fx.channel.reply_results.append(SendResult.ok(message_id="body"))
                    cli.result(result="finished")
                    self.assertTrue(await ordinary.wait_idle(10))
                    self.assertIn(("H", "finished"), fx.channel.replies)
                    self.assertFalse([body for _, body, _ in fx.channel.send_calls if body.mentions])

    async def test_stop_during_anonymous_wake_targets_its_result_owner(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            await app.handle_message(FakeMessage("launch", message_id="H1"))
            cli = clients.calls[-1]
            def system(kind, **data):
                cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))
            cli.init()
            cli.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {})], "model", session_id=cli.session))
            system("task_started", task_id="A", run_id="R", tool_use_id="tool")
            system("background_tasks_changed", tasks=[{"task_id": "A", "run_id": "R"}])
            cli.result(result="first")
            await self.until(lambda: ordinary._active[binding.id].admissible)
            await app.handle_message(FakeMessage("second", message_id="H2"))
            system("background_tasks_changed", tasks=[])
            cli.init(user_message_uuids=[])
            await self.until(lambda: cli.live.physical is not None)
            for failure in (OSError(), asyncio.CancelledError()):
                with patch.object(cli, "interrupt", side_effect=failure):
                    await app.handle_message(FakeMessage("/stop", message_id="control"))
                    self.assertIn("停止结果未知", fx.channel.replies[-1][1])
                    self.assertIsNotNone(cli.live.physical)
            await app.handle_message(FakeMessage("/stop", message_id="control"))
            self.assertEqual(cli.interrupts, 1)
            fx.channel.reply_results.append(SendResult.ok(message_id="wake-card"))
            cli.result(result="wake", idle=False, user_message_uuid=None,
                       origin={"kind": "task-notification", "producer": "session-task", "runId": "R"})
            await self.until(lambda: ("H1", "wake") in fx.channel.replies)
            cli.init()
            fx.channel.reply_results.append(SendResult.ok(message_id="h2-answer"))
            cli.result(result="second")
            self.assertTrue(await ordinary.wait_idle(10))
            self.assertEqual([opts.reply_to for _, body, opts in fx.channel.send_calls if getattr(body, "mentions", ())],
                             ["h2-answer"])

    async def test_stop_all_uses_only_verified_membership_and_status_buttons_recheck_revision(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            from claude_agent_sdk import SystemMessage
            clock[0] = 2.
            binding = (await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))).binding
            await app.handle_message(FakeMessage("launch", message_id="H1"))
            cli = clients.calls[-1]
            def frame(kind, **data):
                cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))
            def membership(*ids):
                frame("background_tasks_changed", tasks=[{"task_id": k, "run_id": "run-" + k} for k in ids])
            async def until(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(.005)
            async def send(text):
                await app.handle_message(FakeMessage(text, message_id="view"))
                return fx.channel.replies[-1][1]
            cli.init()
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
            membership("A", "B")
            cli.result()
            await until(lambda: cli.live.ordered_idle and not ordinary._active[binding.id].delivery.queues)
            self.assertIn("/stop all", await send("/stop"))
            self.assertEqual(cli.interrupts, 0)
            action = _card_button_value(await send("/status"), "全部停止")
            stale = _card_button_value(await send("/sessions"), "全部停止")
            membership("A", "C")
            frame("session_state_changed", state="idle")
            await until(lambda: set(cli.live.tasks) == {"A", "C"})
            direct_card_event(fx.channel, {}, message_id="view")
            for button in (action, stale):
                await app.handle_card_action(direct_button_event(button, message_id="view"))
                self.assertIn("变化", str(fx.channel.replies[-1][1]))
            self.assertFalse(cli.stopped_tasks)
            cli.init(user_message_uuids=[])
            await until(lambda: cli.live.physical is not None)
            self.assertIn("当前观测到 2 个后台任务", await send("/stop"))
            self.assertEqual((cli.interrupts, cli.stopped_tasks), (1, []))
            old = _card_button_value(await send("/status"), "全部停止")
            await app.handle_card_action(direct_button_event(old, message_id="view"))
            self.assertEqual(cli.stopped_tasks, ["A", "C"])
            self.assertEqual(set(cli.live.tasks), {"A", "C"})
            self.assertEqual(cli.closes, 0)
            cli.result(user_message_uuid=None, result=None, reason=None)
            frame("session_state_changed", state="idle")
            await until(lambda: cli.live.ordered_idle)
            async def changed(task):
                cli.stopped_tasks.append(task)
                membership("A", "D")
                frame("session_state_changed", state="idle")
                await until(lambda: set(cli.live.tasks) == {"A", "D"})
                raise OSError()
            with patch.object(cli, "stop_task", side_effect=changed):
                self.assertIn("部分停止结果未知", await send("/stop all"))
            self.assertEqual(cli.stopped_tasks, ["A", "C", "A"])
            self.assertTrue(cli.live.crons)
            self.assertEqual((len(cli.inputs), cli.closes), (1, 0))

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
            form["new_progress_card"] = "progress-card:v1:[false,null]"
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
            self.assertEqual(fx.channel.replies[-1][1].split("\n\n")[0], "plain answer")
            await app.handle_message(FakeMessage("/config", message_id="config"))
            form = form_values(fx.channel.replies[-1][1])
            missing_mention = {key: value for key, value in form.items() if key != "config_completion_mention"}
            await app.handle_card_action(direct_card_event(fx.channel, missing_mention))
            self.assertIn("卡片已过期", fx.channel.replies[-1][1])
            self.assertFalse(fx.store.get(binding.id).task_feedback.completion_mention_enabled)
            await app.handle_card_action(direct_card_event(fx.channel, form))
            self.assertIn("设置已保存", fx.channel.replies[-1][1])
            self.assertEqual(len(clients.calls), 1)

    async def test_background_presentation_is_per_physical_result_and_mentions_last_reply_once(self):
        from netizen_cli.bindings import BindingTaskFeedback
        for cards in (False, True):
            async with owner_channel() as (app, fx, clients, clock, ordinary):
                from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
                # ADR 0106 section 4 covers only card.header.title.content and
                # card.config.summary.content. In the decoded background_cards trace,
                # writes 3, 5 and 7 update "first"; 6 sends the wake answer before
                # logical completion. Write 8 is the verified whole-task redraw.
                self.freeze_trace(fx, "background_cards" if cards else "background_plain")
                clock[0] = 2.
                scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                    task_feedback=BindingTaskFeedback(progress_card_enabled=cards, reaction_pulse_enabled=True))
                fx.channel.reply_results.append(SendResult.ok(message_id="first"))
                if not cards:
                    fx.channel.reply_results.append(SendResult.ok(message_id="first-body"))
                fx.channel.send_results.append(sent_result("reminder", chat_id="oc_direct", parent_id="wake", root_id="H1"))
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                def system(kind, **data):
                    cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))
                def assistant(block, mid, **fields):
                    cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                        uuid=mid + str(type(block)), message_id=mid, **fields))
                cli.init(uuid="human-physical")
                await self.until(lambda: bool(app._progress_cards._sessions))
                assistant(TextBlock("first fragment"), "model1")
                assistant(ToolUseBlock("tool", "Bash", {}), "model1")
                assistant(TextBlock("private child"), "nested", parent_tool_use_id="tool")
                system("task_started", task_id="t", run_id="r", tool_use_id="tool")
                system("background_tasks_changed", tasks=[{"task_id": "t", "run_id": "r"}])
                cli.result(result="physical one")
                ex = ordinary._active[binding.id]
                async with asyncio.timeout(5):
                    while not ex.admissible or ex.delivery.queues:
                        await asyncio.sleep(.005)
                self.assertFalse(fx.channel.send_calls)
                self.assertNotIn(("H1", "DONE"), fx.channel.reactions)
                self.assertIn("后台任务 1 个运行中", ordinary.local_snapshot(binding.id).label)
                if cards:
                    self.assertIn("first fragment", str(fx.channel.updates))
                    self.assertIn("后台任务 1 个运行中", str(fx.channel.updates))
                else:
                    self.assertTrue(any("first fragment" in str(body) for _, body in fx.channel.replies))
                    self.assertIn(("H1", "physical one\n\n后台任务 1 个运行中，可继续发送消息"), fx.channel.replies)
                fx.channel.reply_results.append(SendResult.ok(message_id="wake"))
                system("background_tasks_changed", tasks=[])
                cli.init(user_message_uuids=[])
                assistant(TextBlock("physical two"), "final")
                cli.result(user_message_uuid=None, result="physical two", origin={"kind": "task-notification",
                           "producer": "session-task", "runId": "r"})
                system("session_state_changed", state="idle")
                self.assertTrue(await ordinary.wait_idle(5))
                _, mention, opts = fx.channel.send_calls[-1]
                self.assertEqual(opts.reply_to, "wake")
                self.assertEqual([m.open_id for m in mention.mentions], ["ou_user"])
                self.assertEqual(len(fx.channel.send_calls), 1)
                self.assertEqual(fx.channel.reactions.count(("H1", "DONE")), 1)
                self.assertNotIn("private child", str(fx.channel.replies) + str(fx.channel.updates))
                wakes = [body for anchor, body in fx.channel.replies if anchor == "H1" and "physical two" in str(body)]
                self.assertEqual(len(wakes), 1)
                self.assertNotIn("first fragment", str(wakes[0]))
                if not cards:
                    self.assertEqual(wakes, ["physical two"])

    @requires_claude_sdk
    async def test_rejected_physical_body_issues_one_unconfirmed_notice(self):
        from netizen_cli.bindings import BindingTaskFeedback
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            self.freeze_trace(fx, "rejected_body")
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=False))
            fx.channel.reply_results.extend([SendResult(success=False, raw={"code": 99991400}),
                                            sent_result("notice", chat_id=scope.chat_id)])
            await app.handle_message(FakeMessage("work", message_id="H"))
            cli = clients.calls[-1]
            cli.init()
            await self.until(lambda: bool(app._progress_cards._sessions))
            cli.result(result="body")
            self.assertTrue(await ordinary.wait_idle(5))
            self.assertEqual(fx.channel.replies, [("H", "body"), ("H", INCOMPLETE)])
            self.assertFalse(fx.channel.send_calls)

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
                    self.assertEqual(reply, INCOMPLETE)
                    self.assertIn("图片文件不存在", fx.channel.replies[-2][1])
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
            async def prepare(result, **kwargs):
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
                while not any(str(body) == "cron body" for _, body, _ in fx.channel.send_calls):
                    await asyncio.sleep(.005)
            (destination, _, opts), = [call for call in fx.channel.send_calls if str(call[1]) == "cron body"]
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

    async def test_unowned_cron_card_and_plain_fragments_reach_the_exact_topic_once(self):
        from netizen_cli.bindings import BindingTaskFeedback
        from lark_channel import OutboundCard
        for cards in (False, True):
            async with owner_channel() as (app, fx, clients, clock, ordinary):
                from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
                clock[0] = 2.
                scope = FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "topic")
                binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                    task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=False))
                await app.handle_message(FakeMessage("go", message_id="H1", thread_id="topic", raw={"root_id": "root"}))
                cli = clients.calls[-1]
                cli.init()
                await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                cli.result(result=None, reason=None)
                async with asyncio.timeout(5):
                    while ordinary._active[binding.id].humans:
                        await asyncio.sleep(.005)
                await self.until(lambda: not ordinary._active[binding.id].delivery.queues)
                if cards:
                    self.freeze_trace(fx, "ownerless_cron")
                cli.init(user_message_uuids=[])
                for i, block in enumerate((TextBlock("stage"), ToolUseBlock("tool", "Bash", {}))):
                    cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session, message_id="m", uuid=str(i)))
                raw = {"code": 0, "data": {"message_id": "sent", "chat_id": scope.chat_id,
                                           "thread_id": scope.topic_id, "root_id": "root"}}
                count = 1 if cards else 2
                fx.channel.send_results.extend([SendResult.ok(message_id="sent", raw=raw)] * count)
                cli.result(user_message_uuid=None, uuid="cron-result", result="final text")
                await self.until(lambda: len(fx.channel.send_calls) >= count)
                calls = fx.channel.send_calls
                self.assertEqual(len(calls), count)
                for to, body, opts in calls:
                    self.assertEqual((to, opts.reply_to, opts.reply_in_thread), (scope.chat_id, "root", True))
                    self.assertEqual(isinstance(body, OutboundCard), cards)
                self.assertIn("stage", str(calls[0][1]))
                self.assertIn("final text", str(calls[-1][1]))
                self.assertEqual(len({opts.uuid for _, _, opts in calls}), count)
                if cards:
                    await self.until(lambda: not ordinary._active[binding.id].delivery.queues)
                    expect_presenter_trace(self, fx, "bodyless_anonymous_wake", reference=CLAUDE_FIXTURE)
                    cli.init(user_message_uuids=[])
                    cli.result(user_message_uuid=None, uuid="empty-wake", reason=None, result=None,
                               origin={"kind": "task-notification", "producer": "session-task", "runId": "unknown"})
                    await self.until(lambda: "empty-wake" in cli.live.seen
                                     and not ordinary._active[binding.id].delivery.queues)

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
                try:
                    await complete(event)
                finally:
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
            cli = clients.calls[0]
            self.assertNotIn(human.input_id, app._reactions._pulses)
            cli.init()
            await self.until(lambda: (pulse := app._reactions._pulses.get(human.input_id)) and pulse.typing_reaction_id)
            pulse = app._reactions._pulses[human.input_id]
            thinking, typing = pulse.thinking_reaction_id, pulse.typing_reaction_id
            attempted = []
            async def remove(mid, reaction_id):
                attempted.append((mid, reaction_id))
                if len(attempted) == 1:
                    raise asyncio.CancelledError()
                return SendResult.ok()
            with patch.object(fx.channel, "remove_reaction", side_effect=remove):
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
                self.assertNotIn(human.input_id, app._reactions._pulses)
                cli.init()
                await self.until(lambda: human.input_id in app._reactions._pulses)
                with patch.object(app, "_safe_add_reaction", side_effect=error) as done:
                    await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                    cli.result(reason=None, result=None)
                    async with asyncio.timeout(5):
                        while execution.humans or not execution.admissible or execution.delivery.cleanups:
                            await asyncio.sleep(.005)
                    done.assert_awaited_once()
                self.assertNotIn(human.input_id, app._reactions._pulses)
                self.assertIn(mid, [source for source, _ in fx.channel.reaction_removals])
                self.assertEqual([opts.reply_to for _, _, opts in fx.channel.send_calls],
                                 [f"human-{n}" for n in range(i + 1)])
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
            with patch("netizen_cli.channel.claude_reply_events.ClaudeReplyEvents.result") as view, patch.object(app, "_prepare_task_result_images") as images:
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
