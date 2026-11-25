import asyncio
from dataclasses import replace
from itertools import product
import json
from unittest.mock import AsyncMock, PropertyMock, patch
from uuid import UUID

from lark_channel import SendResult
from contextlib import ExitStack, asynccontextmanager, contextmanager
from types import SimpleNamespace
import unittest

from netizen_cli.backends import BackendPolicy, BackendUnsupported
from netizen_cli.cards.controls import error_card, release_confirmation_card
from netizen_cli.channel_app import ChannelApplication
from netizen_cli.channel.physical_delivery import INCOMPLETE
from netizen_cli.domain import FeishuScope, MessageContextAnchor, ScopeKind
from netizen_cli.ordinary_sessions import CLAUDE_RELEASE_HINT, ReleaseConfirmation
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


def _has_card_answer(card):
    return any(element.get("element_id") in {"turnanswerv1", "partialanswerv1"} for element in card["body"]["elements"])


def _system_frame(cli, kind, **data):
    from claude_agent_sdk import SystemMessage
    cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))


def _start_background_tool(cli, task):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock
    cli.stream.put_nowait(AssistantMessage([ToolUseBlock(task, "Bash", {})], "model", session_id=cli.session))
    _system_frame(cli, "task_started", task_id=task, run_id=task, tool_use_id=task)


class _MentionTrace:
    """Infer the expected reminder from public Feishu calls in a direct-chat explorer row."""

    def __init__(self, channel, stack):
        self.channel = channel
        self.published = []
        self.confirmed = set()
        self.disqualified = set()
        self.attempted = False
        self.ambiguous_answer = False

        def watch(method, *, update):
            async def call(target, payload, *args, **kwargs):
                card = getattr(payload, "card", payload)
                if isinstance(card, dict):
                    answer = _has_card_answer(card)
                else:
                    answer = payload != INCOMPLETE
                self.attempted |= answer
                result = await method(target, payload, *args, **kwargs)
                raw = result.raw or {}
                data = raw.get("data") or {}
                direct, nested = result.message_id, data.get("message_id")
                mid = target if update else direct or nested
                ids = {i for i in (mid, direct, nested, *(result.chunk_ids or ())) if i}
                split_conflict = result.chunk_ids and mid and mid not in result.chunk_ids
                confirmed = result.success and raw.get("code") in (None, 0)
                if data.get("chat_id", "oc_direct") != "oc_direct":
                    self.disqualified.update(ids)
                elif isinstance(card, dict) and len(ids) > 1:
                    self.disqualified.update(ids)
                elif confirmed and ((direct and nested and direct != nested) or split_conflict):
                    self.ambiguous_answer |= answer
                elif confirmed and mid:
                    if not update and (isinstance(card, dict) or answer) and mid not in self.published:
                        self.published.append(mid)
                    if answer:
                        self.confirmed.add(mid)
                return result
            return call

        stack.enter_context(patch.object(channel, "reply", watch(channel.reply, update=False)))
        stack.enter_context(patch.object(channel, "update_card", watch(channel.update_card, update=True)))

    def assert_reminder(self, case):
        eligible = self.confirmed - self.disqualified
        anchor = next((mid for mid in reversed(self.published) if mid in eligible), None)
        if anchor is None:
            anchor = next((mid for mid in reversed(self.published) if mid not in self.disqualified), None)
        if anchor is None and (not self.attempted or self.ambiguous_answer) and "H1" not in self.disqualified:
            anchor = "H1"
        actual = [(options.reply_to, [m.open_id for m in body.mentions])
                  for _, body, options in self.channel.send_calls if getattr(body, "mentions", ())]
        case.assertEqual(actual, [(anchor, ["ou_user"])] if anchor else [], "completion mention anchor")
        return anchor


@contextmanager
def _mutate_mention(mutation):
    from netizen_cli.channel.reply_presenter import _ReplyCardPresenter
    from netizen_cli.ordinary_sessions import LogicalReply

    attempted = set()
    update = _ReplyCardPresenter._attempt_update_message
    async def attempt(presenter, mid, card, **kwargs):
        location = kwargs.get("location")
        if location and _has_card_answer(card.card):
            attempted.add((location.usable, mid))
        return await update(presenter, mid, card, **kwargs)

    def select(reply, verified_source):
        if reply.messages is None:
            return None
        rows = reply.messages.values() if mutation == "oldest" else reversed(reply.messages.values())
        for row in rows:
            unconfirmed = mutation == "count unconfirmed" and (row.location_usable, row.message_id) in attempted
            if (row.usable or mutation == "ignore usable") and (row.confirmed_answer_items or unconfirmed):
                return row.message_id
        for row in reversed(reply.messages.values()):
            if row.usable and row.confirmed_any:
                return row.message_id
        if mutation != "drop input exception" and (not reply.attempted or reply.unaddressed_confirmation):
            return verified_source
        return None

    with patch.object(_ReplyCardPresenter, "_attempt_update_message", attempt), patch.object(LogicalReply, "mention_target", select):
        yield


class OwnerChannelTests(unittest.IsolatedAsyncioTestCase):
    def freeze_trace(self, fx, scenario, *, stack=None):
        if stack is None:
            stack = self.enterContext(ExitStack())
        stack.enter_context(patch("netizen_cli.runtime.claude_ordinary.uuid4",
                               side_effect=(UUID(int=1), UUID(int=2))))
        stack.enter_context(patch("netizen_cli.runtime.claude_ordinary.time_ns", return_value=1_000_000_000))
        def expected_changes(trace):
            # PR 315, merged after the fixture was recorded on a1b9eee.
            hint = "如需在终端继续这个会话，请先 /release"
            if scenario == "background_plain":
                self.assertEqual(trace[3][1][1], "physical one\n\n后台任务 1 个运行中，可继续发送消息")
                trace[3][1][1] += "\n\n" + hint
            if scenario == "background_cards":
                line = trace[3][1][1]["body"]["elements"][0]["header"]["title"]
                self.assertEqual(line["content"], "执行过程 · 已完成 · Claude：空闲，后台任务 1 个运行中，可以发送新消息")
                line["content"] += "。" + hint

            # ADR 0106 §4 changes only the title and collapsed summary of the first answer.
            if scenario in {"background_cards", "late_first_receipt"}:
                writes = [args[1] for op, args, _ in trace if op == "update_card" and args[0] == "first"]
                self.assertEqual(len(writes), 2 if scenario == "late_first_receipt" else 3)
                for card in writes:
                    for field in (card["header"]["title"], card["config"]["summary"]):
                        self.assertEqual(field["content"], "任务已完成")
                        field["content"] = "本轮已完成"

        return expect_presenter_trace(self, fx, scenario, reference=CLAUDE_FIXTURE,
                                      expected_edits=expected_changes, stack=stack,
                                      process_mode=scenario in {"background_plain", "rejected_body"})

    async def until(self, predicate):
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(.005)

    async def result_before_idle(self, cli, ordinary, **fields):
        """Observe the separate-reminder path after this result has finished sending."""
        from claude_agent_sdk import SystemMessage
        execution = next(iter(ordinary._active.values()))
        previous = len(cli.live.seen)
        cli.result(idle=False, **fields)
        await self.until(lambda: len(cli.live.seen) > previous and not execution.delivery.queues)
        cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))

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
                    from netizen_cli.channel.presentation import turn_progress_projection
                    def render(**fields):
                        if case == "invalid card" and fields.get("final_response"):
                            raise ValueError("synthetic pre-send validation")
                        return turn_progress_projection(**fields)
                    with (patch.object(fx.channel, "reply", side_effect=first),
                          patch("netizen_cli.channel.presentation.turn_progress_projection", side_effect=render)):
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
            self.assertEqual(plain, [("H2", f"body-H2\n\n{CLAUDE_RELEASE_HINT}"), ("H3", f"body-H3\n\n{CLAUDE_RELEASE_HINT}")])

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
                    with (patch("netizen_cli.channel.constants.ITEM_RECEIPT_REQUEST_TIMEOUT_SECONDS",
                                1 if case.startswith("deadline") else 15),
                          patch.object(fx.channel, "reply", side_effect=send),
                          patch.object(fx.channel, "add_reaction", side_effect=react),
                          patch.object(ordinary, "_notice", side_effect=notice)):
                        await app.handle_message(FakeMessage("launch", message_id="H"))
                        cli = clients.calls[-1]
                        cli.init()
                        await self.until(lambda: bool(app._progress_cards._sessions))
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
        from netizen_cli.channel.presentation import turn_progress_projection

        def render(**fields):
            if fields.get("final_response"):
                raise ValueError("terminal card validation failed")
            return turn_progress_projection(**fields)

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
                    with patch("netizen_cli.channel.presentation.turn_progress_projection", side_effect=render):
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
                        await self.result_before_idle(cli, ordinary, result="answer" if kind == "body" else None,
                                                      reason="completed" if kind == "body" else None)
                        self.assertTrue(await ordinary.wait_idle(10))
                        self.assertEqual(sends.await_count, int(kind != "empty") + int(not anchor))
                        if not anchor:
                            self.assertEqual(sends.await_args.args[1], INCOMPLETE)
                    self.assertEqual([(opts.reply_to, [m.open_id for m in item.mentions])
                                      for _, item, opts in fx.channel.send_calls], [(anchor, ["ou_user"])] if anchor else [])

    @requires_claude_sdk
    async def test_background_task_appearing_during_the_mention_wait_is_in_the_final_text(self):
        from netizen_cli.channel import constants

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = (await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))).binding
            await app.handle_message(FakeMessage("work", message_id="H"))
            cli = clients.calls[-1]
            cli.init()
            _start_background_tool(cli, "A")
            execution = ordinary._active[binding.id]
            human = execution.human
            entered = asyncio.Event()
            wait = human.reply.mention_decided.wait

            async def observe_wait():
                entered.set()
                return await wait()

            fx.channel.reply_results.append(SendResult.ok(message_id="first"))
            with (patch.object(human.reply.mention_decided, "wait", observe_wait),
                  patch.object(constants, "COMPLETION_MENTION_DECISION_TIMEOUT_SECONDS", .5)):
                cli.result(result="first", idle=False)
                await asyncio.wait_for(entered.wait(), 3)
                _system_frame(cli, "background_tasks_changed", tasks=[{"task_id": "A", "run_id": "A"}])
                _system_frame(cli, "session_state_changed", state="idle")
                await self.until(lambda: bool(fx.channel.replies))
                self.assertEqual(execution.cli_state(human.input_id).tasks, 1)
                self.assertEqual(fx.channel.replies, [("H", f"first\n\n后台任务 1 个运行中，可继续发送消息\n\n{CLAUDE_RELEASE_HINT}")])
            _system_frame(cli, "background_tasks_changed", tasks=[])
            cli.init(user_message_uuids=[])
            cli.result(result="wake-A", user_message_uuid=None, idle=False,
                       origin={"kind": "task-notification", "producer": "session-task", "runId": "A"})
            _system_frame(cli, "session_state_changed", state="idle")
            self.assertTrue(await ordinary.wait_idle(3))

    @requires_claude_sdk
    async def test_attributed_wake_queued_behind_separate_reminder_mentions_once(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = (await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))).binding
            await app.handle_message(FakeMessage("work", message_id="H"))
            cli = clients.calls[-1]
            cli.init()
            await self.until(lambda: bool(app._progress_cards._sessions))
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "retain"}]}, None, None)
            execution = ordinary._active[binding.id]
            human = execution.human
            sending, release = asyncio.Event(), asyncio.Event()
            send = fx.channel.send

            async def hold_reminder(to, content, opts=None):
                sending.set()
                await release.wait()
                return await send(to, content, opts)

            fx.channel.send_results.append(sent_result("reminder", chat_id="oc_direct", parent_id="H", root_id="H"))
            fx.channel.reply_results.append(SendResult.ok(message_id="wake"))
            with patch.object(fx.channel, "send", hold_reminder):
                _start_background_tool(cli, "A")
                _system_frame(cli, "background_tasks_changed", tasks=[{"task_id": "A", "run_id": "A"}])
                cli.result(reason=None, result=None)
                await self.until(lambda: execution.admissible and not execution.delivery.queues)
                _system_frame(cli, "background_tasks_changed", tasks=[])
                _system_frame(cli, "session_state_changed", state="idle")
                await asyncio.wait_for(sending.wait(), 3)
                try:
                    self.assertTrue(human.terminal_sent)
                    self.assertTrue(human.reply.mention_attempted, "the in-flight reminder must own the mention attempt")
                    cli.init(user_message_uuids=[])
                    cli.result(result="wake-A", user_message_uuid=human.input_id, idle=False,
                               origin={"kind": "task-notification", "producer": "session-task", "runId": "A"})
                    await self.until(lambda: len(cli.live.seen) == 2)
                finally:
                    release.set()
                await self.until(lambda: not execution.delivery.queues)
                self.assertIsNone(human.reply.messages)
                self.assertEqual([(opts.reply_to, post.text, [m.open_id for m in post.mentions])
                                  for _, post, opts in fx.channel.send_calls], [("H", "本轮任务已结束。", ["ou_user"])])
                self.assertIn(("H", f"wake-A\n\n{CLAUDE_RELEASE_HINT}"), fx.channel.replies)
                self.assertFalse(any(getattr(post, "mentions", ()) for _, post in fx.channel.replies))
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": []}, None, None)
            _system_frame(cli, "session_state_changed", state="idle")
            self.assertTrue(await ordinary.wait_idle(3))

    @requires_claude_sdk
    async def test_final_post_mention_at_the_logical_terminal_boundary(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel import constants

        for order, receipt in (*product(("early", "waiting", "late"), ("confirmed", "unknown", "rejected")),
                               ("stop", "confirmed"), ("build failure", "confirmed")):
            with self.subTest(order=order, receipt=receipt):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    binding = fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                        project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=False, completion_mention_enabled=True))
                    app._progress_cards._poll_seconds = 60
                    sent = []
                    segment, body, release_segment, release_body = (asyncio.Event() for _ in range(4))
                    async def send(origin, post, opts=None):
                        text = getattr(post, "markdown", post)
                        sent.append((text, [m.open_id for m in getattr(post, "mentions", ()) or ()]))
                        if text == "**阶段性答案**\n\nsegment":
                            segment.set()
                            await release_segment.wait()
                            return SendResult.ok(message_id="segment")
                        if text == "final answer":
                            body.set()
                            await release_body.wait()
                            if receipt == "unknown":
                                raise OSError("final receipt lost")
                            if receipt == "rejected":
                                return SendResult(success=False, raw={"code": 99991400})
                            return SendResult.ok(message_id="final")
                        return SendResult.ok(message_id="other")

                    with ExitStack() as stack:
                        stack.enter_context(patch.object(fx.channel, "reply", side_effect=send))
                        if order == "late":
                            stack.enter_context(patch.object(constants, "COMPLETION_MENTION_DECISION_TIMEOUT_SECONDS", .03))
                        if order == "build failure":
                            stack.enter_context(patch("netizen_cli.channel.presentation.mentioned_answer", side_effect=ValueError))
                        await app.handle_message(FakeMessage("work", message_id="H"))
                        cli = clients.calls[-1]
                        cli.init()
                        ex = ordinary._active[binding.id]
                        decision = ex.human.reply.mention_decided
                        wait_for_decision = decision.wait
                        waiting = asyncio.Event()

                        async def observe_wait():
                            waiting.set()
                            return await wait_for_decision()

                        stack.enter_context(patch.object(decision, "wait", observe_wait))
                        await self.until(lambda: bool(app._progress_cards._sessions))
                        if order == "stop":
                            await app.handle_message(FakeMessage("/stop", message_id="stop"))
                        for block in (TextBlock("segment"), ToolUseBlock("tool", "Read", {})):
                            cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                uuid=type(block).__name__, message_id="model"))
                        cli.result(result="final answer", idle=False)
                        try:
                            await asyncio.wait_for(segment.wait(), 3)
                            if order in ("waiting", "late", "stop"):
                                release_segment.set()
                                await self.until(lambda: waiting.is_set() or body.is_set())
                            if order == "late":
                                await asyncio.wait_for(body.wait(), 3)
                                self.assertTrue(waiting.is_set())
                                self.assertFalse(decision.is_set())
                            cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
                            await self.until(lambda: ex.human.terminal_sent)
                        finally:
                            release_segment.set()
                            release_body.set()
                        self.assertTrue(await ordinary.wait_idle(5))
                    inline = order in ("early", "waiting")
                    self.assertEqual([(t, ids) for t, ids in sent if ids], [("final answer", ["ou_user"])] if inline else [])
                    self.assertEqual(sum(t == "final answer" for t, _ in sent), 1)
                    self.assertEqual(sum(t == INCOMPLETE for t, _ in sent), int(receipt != "confirmed"))
                    self.assertEqual([(post.text, [m.open_id for m in post.mentions], opts.reply_to)
                                      for _, post, opts in fx.channel.send_calls],
                                     [] if inline or order == "stop" else [
                                         ("本轮任务已结束。", ["ou_user"], "final" if receipt == "confirmed" else "segment")])

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
                    self.assertFalse(any(message.confirmed_answer_items for message in human.reply.messages.values()))
                    attempts = []
                    update = fx.channel.update_card

                    async def transport(mid, card):
                        attempts.append((mid, card))
                        if failure == "cancelled":
                            raise asyncio.CancelledError()
                        if failure == "error":
                            raise OSError("replacement receipt lost")
                        return await update(mid, card)

                    with patch.object(fx.channel, "update_card", transport):
                        system("background_tasks_changed", tasks=[])
                        cli.init(user_message_uuids=[])
                        cli.result(result="wake-answer", user_message_uuid=None,
                                   origin={"kind": "task-notification", "producer": "session-task", "runId": "R"}, idle=False)
                        await self.until(lambda: "wake" in human.reply.messages and any(
                            mid == "initial" and "Claude：" not in str(card) for mid, card in attempts))
                        system("session_state_changed", state="idle")
                        self.assertTrue(await ordinary.wait_idle(10))
                    self.assertTrue(any(mid == "wake" and "全部完成" in str(card) for mid, card in attempts))
                    self.assertEqual(sum("wake-answer" in str(item) for _, item in fx.channel.replies), 1)
                    self.assertIn(("H1", constants.DONE_REACTION), fx.channel.reactions)
                    self.assertEqual([opts.reply_to for _, body, opts in fx.channel.send_calls if body.mentions], ["wake"])
                    if failure == "none":
                        last_initial = [card for mid, card in fx.channel.updates if mid == "initial"][-1]
                        self.assertNotIn("Claude：", str(last_initial))

    async def test_only_allowlisted_activity_reaches_either_reply_transport(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage
        from netizen_cli.bindings import BindingTaskFeedback
        from tests.support.presenter_traces import delivery_recorder
        from tests.support.process_transport import ProcessTransport, check_pairs

        secret = "PRIVATEPAYLOAD7Q"
        # Union of input property names from the pinned CLI's decoded tool catalogs.
        fields = (
            "-A -B -C -i -n -o action activeForm addBlockedBy addBlocks allow_large allowedPrompts "
            "allowed_domains annotations answers args blocked_domains cell_id cell_type channel command "
            "content context cron cursor dangerouslyDisableSandbox delaySeconds description "
            "discard_changes durable edit_mode effort file_path findings glob head_limit id isolation "
            "level limit max_results message metadata model multiline name new_source new_string noop "
            "notebook_path notify_when_idle offset old_string output_mode owner pages path pattern prompt "
            "q query questions reason recurring replace_all resumeFromRunId run_in_background script "
            "scriptPath shell_id skill status stop subagent_type subject summary taskId task_id timeout "
            "timeout_ms title to todos type url ws "
        ).split()
        names = (
            "Agent AskUserQuestion Bash CronCreate CronDelete CronList DeferredToolPlaceholder Edit "
            "EnterPlanMode EnterWorktree ExitPlanMode ExitWorktree Glob Grep ListAgents Monitor "
            "NotebookEdit Read ReportFindings ScheduleWakeup SendMessage Skill Task TaskCreate TaskGet "
            "TaskList TaskStop TaskUpdate TodoWrite ToolSearch WebFetch WebSearch Workflow Write "
        ).split() + ["mcp__" + secret, "custom_" + secret]
        allowed = {"Bash": ("command",), "Read": ("file_path",), "Glob": ("pattern",), "Grep": ("pattern", "path"),
                   "Write": ("file_path",), "Edit": ("file_path",), "NotebookEdit": ("notebook_path",),
                   "WebFetch": ("url",), "WebSearch": ("query",)}
        # Decoded SDK result envelopes. Keep the effective paths and checklist labels public,
        # and poison the unprojected text, including nested file, patch, task and agent fields.
        patch_hunk = {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": [secret]}
        task = {"id": "1", "subject": "safe-step", "status": "in_progress", "blockedBy": [secret]}
        result_samples = {
            "Agent": [{"status": "completed", "agentId": secret, "agentType": secret, "prompt": secret,
                       "toolsUsed": [secret], "content": [{"type": "text", "text": secret}]},
                      {"status": "async_launched", "isAsync": True, "description": secret, "prompt": secret,
                       "agentId": secret, "outputFile": secret, "canReadOutputFile": True}],
            "Bash": [{"stdout": secret, "stderr": secret, "interrupted": False, "isImage": False,
                      "noOutputExpected": False}, secret],
            "CronCreate": [{"id": secret, "humanSchedule": secret, "nextFireAt": 1000,
                            "recurring": False, "durable": False}],
            "Edit": [{"filePath": "safe.txt", "oldString": secret, "newString": secret, "originalFile": secret,
                      "structuredPatch": [patch_hunk], "userModified": False, "replaceAll": False}],
            "NotebookEdit": [{"notebook_path": "safe.ipynb", "new_source": secret, "old_source": secret,
                              "original_file": secret, "updated_file": secret, "edit_mode": "replace",
                              "cell_id": secret, "cell_type": "code", "language": "python", "error": ""}],
            "Read": [{"type": "notebook", "file": {"filePath": secret, "cells": [
                {"cellType": "code", "source": secret, "cell_id": secret, "language": "python"}]}},
                     {"type": "text", "file": {"filePath": secret, "content": secret, "numLines": 1,
                                              "startLine": 1, "totalLines": 1}}],
            "TaskCreate": [{"task": {"id": "1", "subject": "safe-step", "description": secret}}],
            "TaskGet": [{"task": {**task, "description": secret, "blocks": [secret]}}],
            "TaskList": [{"tasks": [task]}],
            "TaskUpdate": [{"success": True, "taskId": "1", "updatedFields": ["status"],
                            "statusChange": {"from": "in_progress", "to": "completed"}},
                           {"success": False, "taskId": "unknown", "updatedFields": [], "error": secret}],
            "TodoWrite": [{"oldTodos": [{"content": secret, "activeForm": secret, "status": "completed"}],
                           "newTodos": [{"content": "safe-step", "activeForm": secret, "status": "pending"}]}],
            "Write": [{"type": "create", "filePath": "safe.txt", "content": secret, "originalFile": None,
                       "structuredPatch": [], "userModified": False}],
        }
        samples = [(name, payload) for name in names for payload in result_samples.get(name, [secret])]
        for cards in (True, False):
            with self.subTest(cards=cards), ExitStack() as stack:
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    events = delivery_recorder(fx.channel, stack)
                    process = ProcessTransport(events)
                    app._progress_cards._process_port = process.port
                    app._progress_cards._poll_seconds = .01
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=False))
                    fx.channel.reply_results.append(SendResult.ok(message_id="reply"))
                    await app.handle_message(FakeMessage("work", message_id="H"))
                    cli = clients.calls[-1]
                    cli.init()
                    def assistant(blocks, parent=None):
                        cli.stream.put_nowait(AssistantMessage(blocks, "model", session_id=cli.session, uuid="fragment",
                                                              message_id="assistant", parent_tool_use_id=parent))
                    def result(key, payload, *, failed=False, parent=None):
                        cli.stream.put_nowait(UserMessage([ToolResultBlock(key, content=secret, is_error=failed)],
                                                          tool_use_result=payload, parent_tool_use_id=parent))
                    try:
                        assistant([ThinkingBlock(secret, secret)])
                        assistant([TextBlock(secret), ToolUseBlock("child", "Bash", {"command": secret})], "parent")
                        result("child", result_samples["Bash"][0], parent="parent")
                        for offset in range(0, len(samples), 5):
                            for n, (name, payload) in enumerate(samples[offset:offset + 5], offset):
                                key = f"tool-{n}"
                                inputs = dict.fromkeys(fields, secret)
                                inputs.update(dict.fromkeys(allowed.get(name, ()), "safe-detail"))
                                assistant([ToolUseBlock(key, name, inputs)])
                                result(key, payload)
                            # Sample every batch on the live wire before bounded windows can evict it.
                            mark = f"batch-{offset}"
                            assistant([ToolUseBlock(mark, "Read", {"file_path": mark, "description": secret})])
                            await self.until(lambda: mark in str(events))
                            result(mark, secret, failed=True)
                        assistant([ToolUseBlock("steps", "TaskList", {})])
                        result("steps", {"tasks": [{"id": "1", "subject": "final-step", "status": "in_progress",
                                                    "description": secret}], "raw": secret})
                        await self.until(lambda: "final-step" in str(events))
                        self.assertEqual(len(process.created), int(not cards))
                        self.assertNotIn(secret, str(events))
                    finally:
                        cli.result(result="final answer", permission_denials=[{"tool_name": secret, "tool_input": secret}])
                        self.assertTrue(await ordinary.wait_idle(10))
                    if not cards:
                        await self.until(lambda: bool(process.completed))
                        frames = [args[1] for name, args, _ in events if name == "process.update"]
                        offered, finished = set(), set()
                        for update in frames:
                            starts, ends = check_pairs(update)
                            offered.update(starts)
                            finished.update(ends)
                        self.assertGreater(len(offered), len(names) // 2)
                        self.assertEqual(offered, finished)
                        self.assertEqual([args[1] for name, args, _ in events if name == "process.complete"], ["done"])
                    self.assertIn("safe-detail", str(events))
                    self.assertNotIn(secret, str(events))

    @requires_claude_sdk
    async def test_files_writer_settles_per_page_and_content_with_fresh_transport_nonces(self):
        from pathlib import Path
        from netizen_cli.cards import turn_files_card
        from netizen_cli.cards.callbacks import without_callback_nonces
        from netizen_cli.channel.reply_presenter import _CardWriter
        from netizen_cli.turn_files import TurnFile, TURN_FILE_PAGE_SIZE

        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        files = tuple(TurnFile(str(i), Path(f"/fixture/{i}"), 1, "file") for i in range(TURN_FILE_PAGE_SIZE + 1))
        page, answer = 0, "first answer"
        def render():
            return turn_files_card(scope=scope, binding_id="binding", turn_id="turn", files=files,
                                   final_response=answer, page=page)
        one, two = render().card, render().card
        self.assertNotEqual(one, two)
        self.assertEqual(without_callback_nonces(one), without_callback_nonces(two))
        writer = _CardWriter()
        writes = []
        async def write(card):
            writes.append(card)
            self.assertLessEqual(len(writes), 3)
            return "confirmed"
        for page, answer, count in ((0, "first answer", 1), (0, "first answer", 1),
                                    (1, "first answer", 2), (1, "second answer", 3)):
            self.assertTrue(await writer.reconcile(render, write))
            self.assertEqual(len(writes), count)

    @requires_claude_sdk
    async def test_file_only_answers_offer_effective_outputs_and_owner_callbacks(self):
        from claude_agent_sdk import AssistantMessage, ToolUseBlock, UserMessage, ToolResultBlock
        from lark_channel import OutboundCard, OutboundFile
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.turn_files import TURN_FILE_PAGE_SIZE

        for cards in (True, False):
            with self.subTest(cards=cards):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=False))
                    update = fx.channel.update_card
                    async def bounded_update(mid, card):
                        if len(fx.channel.updates) >= 8:
                            self.fail("the Files card did not settle")
                        return await update(mid, card)
                    fx.channel.update_card = bounded_update
                    fx.channel.reply_results.append(SendResult.ok(message_id="files"))
                    await app.handle_message(FakeMessage("run", message_id="H"))
                    cli = clients.calls[-1]
                    cli.init(uuid="physical-files")
                    await self.until(lambda: bool(app._progress_cards._sessions))
                    decoy = cli.cwd / "requested.txt"
                    decoy.touch()
                    expected = []
                    for index in range(TURN_FILE_PAGE_SIZE + 2):
                        child = index == TURN_FILE_PAGE_SIZE + 1
                        parent = "agent" if child else None
                        path = cli.cwd / f"effective-{index}.txt"
                        path.write_text("fixture")
                        key = f"write-{index}"
                        cli.stream.put_nowait(AssistantMessage([ToolUseBlock(key, "Write", {"file_path": str(decoy)})],
                            "model", session_id=cli.session, parent_tool_use_id=parent))
                        cli.stream.put_nowait(UserMessage([ToolResultBlock(key)], tool_use_result={"filePath": str(path)},
                            parent_tool_use_id=parent))
                        if not child:
                            expected.append(str(path))
                    cli.result(reason=None, result=None)
                    self.assertTrue(await ordinary.wait_idle(10))
                    def actions(card, intent):
                        return [behavior["value"] for button in _elements(card, "button") for behavior in button["behaviors"]
                                if behavior["value"].get("intent") == intent]
                    card = (fx.channel.updates[-1][1] if cards else
                            next(body.card for _, body in fx.channel.replies if isinstance(body, OutboundCard)))
                    send = actions(card, "turn-file.send")
                    page, = actions(card, "turn-file.page")
                    self.assertEqual((send[0]["binding_id"], send[0]["turn_id"]), (f"binding:v1:{binding.id}", "turn:v1:physical-files"))
                    for value, form in ((send[0], None), (page, {"turn_file_page": "1"})):
                        event = direct_button_event(value, message_id="files", form_value=form)
                        event.operator.open_id = "other"
                        before = (len(fx.channel.send_calls), len(fx.channel.updates))
                        await app.handle_card_action(event)
                        self.assertIn("仅限所有者", fx.channel.replies[-1][1])
                        self.assertEqual((len(fx.channel.send_calls), len(fx.channel.updates)), before)
                    fx.channel.send_results.append(sent_result("attachment", chat_id=scope.chat_id, thread_id="topic",
                                                              root_id="files", parent_id="files"))
                    await app.handle_card_action(direct_button_event(send[0], message_id="files"))
                    to, body, opts = fx.channel.send_calls[-1]
                    self.assertIsInstance(body, OutboundFile)
                    self.assertEqual((to, opts.reply_to, body.source.path), (scope.chat_id, "files", send[0]["path"]))
                    await app.handle_card_action(direct_button_event(page, message_id="files", form_value={"turn_file_page": "1"}))
                    mid, second = fx.channel.updates[-1]
                    self.assertEqual(mid, "files")
                    self.assertCountEqual([v["path"] for v in [*send, *actions(second, "turn-file.send")]], expected)

    @requires_claude_sdk
    async def test_anonymous_file_only_answer_uses_verified_root_and_settles_once(self):
        from claude_agent_sdk import AssistantMessage, ToolUseBlock, UserMessage, ToolResultBlock
        from lark_channel import OutboundCard
        from netizen_cli.bindings import BindingTaskFeedback

        for cards, lost in product((False, True), repeat=2):
            with self.subTest(cards=cards, lost=lost):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "topic")
                    binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=False))
                    await app.handle_message(FakeMessage("go", message_id="H", thread_id="topic", raw={"root_id": "root"}))
                    cli = clients.calls[-1]
                    cli.init()
                    await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                    cli.result(reason=None, result=None)
                    execution = ordinary._active[binding.id]
                    await self.until(lambda: not execution.humans and not execution.delivery.queues)
                    cli.init(uuid="cron-init", user_message_uuids=[])
                    path = cli.cwd / "report.txt"
                    path.touch()
                    cli.stream.put_nowait(AssistantMessage([ToolUseBlock("write", "Write", {})], "model", session_id=cli.session))
                    cli.stream.put_nowait(UserMessage([ToolResultBlock("write")], tool_use_result={"filePath": str(path)}))
                    confirmed = sent_result("sent", chat_id=scope.chat_id, thread_id="topic", root_id="root")
                    fx.channel.send_results.extend([TimeoutError() if lost else confirmed, confirmed])
                    cli.result(user_message_uuid=None, uuid="cron-result", reason=None, result=None)
                    await self.until(lambda: "cron-result" in cli.live.seen and not execution.delivery.queues)
                    ((to, body, opts), *others) = fx.channel.send_calls
                    self.assertIsInstance(body, OutboundCard)
                    self.assertIn(str(path), str(body.card))
                    self.assertEqual((to, opts.reply_to, opts.reply_in_thread), (scope.chat_id, "root", True))
                    self.assertEqual([text for _, text, _ in others], [INCOMPLETE] if lost else [])

    @requires_claude_sdk
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

    @requires_claude_sdk
    async def test_release_settles_launcher_at_close_deadline_and_drains_before_readmission(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.runtime.claude_ordinary import CLOSE_FAILED
        for late in (True, False):
            with self.subTest(late=late):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    binding = fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True, reaction_pulse_enabled=True,
                                                          completion_mention_enabled=True))
                    fx.channel.reply_results.append(SendResult.ok(message_id="launcher"))
                    await app.handle_message(FakeMessage("launch", message_id="H1"))
                    cli = clients.calls[-1]
                    cli.init()
                    await self.until(lambda: bool(app._progress_cards._sessions))
                    cli.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {})], "model", session_id=cli.session))
                    cli.stream.put_nowait(SystemMessage("task_started", dict(session_id=cli.session, task_id="A", run_id="R", tool_use_id="tool")))
                    cli.stream.put_nowait(SystemMessage("background_tasks_changed", dict(session_id=cli.session, tasks=[dict(task_id="A", run_id="R")])))
                    cli.result(result=None, reason=None)
                    await self.until(lambda: "后台任务 1 个" in str(fx.channel.updates))
                    await app.handle_message(FakeMessage("/release", message_id="command"))
                    card, = [body for target, body in fx.channel.replies if target == "command"]
                    event = direct_button_event(_card_button_value(card, "确认释放"))
                    direct_card_event(fx.channel, {}, message_id=event.message_id)
                    reaction = fx.channel.add_reaction
                    entered, drain = asyncio.Event(), asyncio.Event()
                    async def gated(message_id, emoji):
                        if message_id == "H1" and emoji == "DONE":
                            entered.set()
                            await drain.wait()
                        return await reaction(message_id, emoji)
                    if late:
                        cli.close_gate.clear()
                    try:
                        with patch.object(fx.channel, "add_reaction", side_effect=gated), \
                             patch("netizen_cli.runtime.claude_ordinary._CLOSE_SECONDS", .1):
                            callback = asyncio.create_task(app.handle_card_action(event))
                            await asyncio.wait_for(entered.wait(), 2)
                            self.assertIn("全部完成", str(fx.channel.updates[-1]))
                            expected = CLOSE_FAILED if late else "已释放 Claude 会话连接。"
                            if late:
                                await asyncio.wait_for(asyncio.shield(callback), 2)
                                self.assertIn((event.message_id, CLOSE_FAILED), fx.channel.replies)
                                await app.handle_message(FakeMessage("before disconnect", message_id="blocked"))
                                self.assertIn(("blocked", CLOSE_FAILED), fx.channel.replies)
                                cli.close_gate.set()
                                await self.until(lambda: binding.id not in ordinary._unavailable)
                            else:
                                self.assertFalse(callback.done())
                            await app.handle_message(FakeMessage("before terminal delivery", message_id="draining"))
                            self.assertIn("收尾", [body for target, body in fx.channel.replies if target == "draining"][0])
                            self.assertEqual(len(clients.calls), 1)
                    finally:
                        drain.set()
                        cli.close_gate.set()
                    self.assertTrue(await ordinary.wait_idle(2))
                    await asyncio.wait_for(callback, 2)
                    self.assertEqual([body for target, body in fx.channel.replies if target == event.message_id], [expected])
                    self.assertEqual(fx.channel.reactions.count(("H1", "DONE")), 1)
                    self.assertFalse([body for _, body, _ in fx.channel.send_calls if body.mentions])
                    await app.handle_message(FakeMessage("cold", message_id="H2"))
                    cold = clients.calls[-1]
                    self.assertTrue(cold.resume)
                    cold.init()
                    cold.result()
                    self.assertTrue(await ordinary.wait_idle(2))

    @requires_claude_sdk
    async def test_divergence_button_releases_without_delivering_rejected_message(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            await fx.create_binding(scope)
            await app.handle_message(FakeMessage("work", message_id="H1"))
            cli = clients.calls[-1]
            cli.init()
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
            cli.result()
            await self.until(lambda: cli.live.ordered_idle)
            clients.history = "terminal"
            with patch.object(app, "_report_input_error", wraps=app._report_input_error) as report:
                await app.handle_message(FakeMessage("continue", message_id="H2"))
            report.assert_awaited_once()
            card, = [body for target, body in fx.channel.replies if target == "H2"]
            button = _card_button_value(card, "释放会话")
            self.assertEqual((len(cli.inputs), cli.closes), (1, 0))
            event = direct_button_event(button)
            direct_card_event(fx.channel, {}, message_id=event.message_id)
            await app.handle_card_action(event)
            self.assertEqual((len(cli.inputs), cli.closes), (1, 1))
            self.assertIn((event.message_id, "已释放 Claude 会话连接。"), fx.channel.replies)

    @requires_claude_sdk
    async def test_divergence_reply_contains_sdk_failures_and_propagates_caller_cancellation(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = (await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))).binding
            await app.handle_message(FakeMessage("launch", message_id="H1"))
            cli = clients.calls[-1]
            cli.init()
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
            cli.result()
            await self.until(lambda: ordinary._active[binding.id].last_assistant == clients.history)
            for index, failure in enumerate((OSError(), asyncio.CancelledError(), None)):
                clients.history = f"foreign-{index}"
                entered = asyncio.Event()
                async def fail_card(message, content, opts=None):
                    _card_button_value(content, "释放会话")
                    if failure is not None:
                        raise failure
                    entered.set()
                    await asyncio.Future()
                with patch.object(fx.channel, "reply", side_effect=fail_card) as reply:
                    attempt = asyncio.create_task(app.handle_message(FakeMessage("continue", message_id="H2")))
                    if failure is None:
                        await asyncio.wait_for(entered.wait(), 2)
                        attempt.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await attempt
                    else:
                        with self.assertLogs("netizen_cli.channel_app", level="WARNING"):
                            await attempt
                    reply.assert_awaited_once()
            self.assertEqual(len(cli.inputs), 1)

    async def test_codex_release_button_reports_original_error_without_feishu_lookups(self):
        async with channel_fixture() as fx:
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            card = release_confirmation_card(scope, binding.id, ReleaseConfirmation(1, "turn", 0, 1))
            event = direct_button_event(_card_button_value(card, "确认释放"))
            direct_card_event(fx.channel, {}, message_id=event.message_id)
            with patch.object(fx.channel, "fetch_message", wraps=fx.channel.fetch_message) as fetch:
                await fx.app.handle_card_action(event)
                fetch.assert_not_awaited()
            self.assertEqual(fx.channel.chat_info_calls, [])
            self.assertEqual(fx.channel.updates, [(event.message_id, error_card("当前实例不支持【确认释放 Claude 会话】。").card)])

    @requires_claude_sdk
    async def test_release_scope_validation_errors_reply_in_the_card_topic(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "original")
            binding = (await fx.create_binding(scope)).binding
            card = release_confirmation_card(scope, binding.id, ReleaseConfirmation(1, "turn", 0, 1))
            event = direct_button_event(_card_button_value(card, "确认释放"))
            for topic, error in (("moved", None), ("original", OSError("chat lookup failed"))):
                fx.channel.fetched_messages[event.message_id] = {"data": {"items": [{"chat_id": scope.chat_id, "thread_id": topic}]}}
                with (patch.object(app._management, "release_current_binding") as release,
                      patch.object(fx.channel, "get_chat_info", side_effect=error, wraps=fx.channel.get_chat_info),
                      patch.object(fx.channel, "reply", wraps=fx.channel.reply) as reply):
                    await app.handle_card_action(event)
                release.assert_not_awaited()
                target, _, opts = reply.await_args.args
                self.assertEqual((target.message_id, opts.reply_to, opts.reply_in_thread), (event.message_id, event.message_id, True))
            self.assertFalse(clients.calls)

    @requires_claude_sdk
    async def test_release_card_rechecks_owner_scope_and_exact_connection(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            await fx.create_binding(scope)
            await app.handle_message(FakeMessage("work", message_id="H1"))
            cli = clients.calls[-1]
            cli.init()
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
            cli.result()
            await self.until(lambda: cli.live.ordered_idle)
            await app.handle_message(FakeMessage("/release", message_id="release"))
            card = next(body for target, body in fx.channel.replies if target == "release")
            value = _card_button_value(card, "确认释放")
            event = direct_button_event(value)
            with self.assertRaises(BackendUnsupported):
                BackendPolicy("codex").require_card(value["intent"])
            with self.assertRaises(BackendUnsupported):
                app._policy.require_api("POST", "/sessions/release")
            event.operator.open_id = "other"
            await app.handle_card_action(event)
            event.operator.open_id = "ou_user"
            fx.channel.fetched_messages[event.message_id] = {"data": {"items": [{"chat_id": scope.chat_id, "thread_id": "foreign"}]}}
            await app.handle_card_action(event)
            self.assertEqual(cli.closes, 0)
            direct_card_event(fx.channel, {}, message_id=event.message_id)
            await app.handle_card_action(event)
            await app.handle_card_action(event)
            self.assertEqual(cli.closes, 1)
            self.assertEqual(sum(body == "已释放 Claude 会话连接。" for _, body in fx.channel.replies), 1)

    async def test_grace_typed_gaps_and_local_forms(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            async def send(text):
                await app.handle_message(FakeMessage(text, message_id="om"))
                return fx.channel.replies[-1][1]

            for body in ("go", "/new", "/defaults", "/config"):
                self.assertEqual(await send(body), GRACE)
            clock[0] = 2.
            for cmd in ("goal this", "side close", "fork", "usage", "compact", "cron", "archive", "delete",
                        "model", "effort", "fast", "speed", "threads archived", "quit", "plan"):
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
            self.assertIn("/release", await send("/help"))
            self.assertIn("没有可释放", await send("/release"))
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
            await self.result_before_idle(cli, ordinary)
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
                            assistant(ToolUseBlock("lookup", "TaskGet", {}), "initial")
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
                                    self.assertIn(("H2", f"launch-C\n\n后台任务 1 个运行中，可继续发送消息\n\n{CLAUDE_RELEASE_HINT}"), fx.channel.replies)
                                for k in order:
                                    tasks.remove(k)
                                    membership()
                                    cli.init(user_message_uuids=[])
                                    result(k)
                                    if not cards and k == "C":
                                        await self.until(lambda: "wake-C" in wire["H2"])
                                    system("session_state_changed", state="idle")
                                await self.until(lambda: cli.closes == 1)
                            finally:
                                release.set()
                            self.assertTrue(await ordinary.wait_idle(10))
                        self.assertEqual(wire, {"H1": ([] if fallback else ["launch-AB"]) + ["wake-A", "wake-B"], "H2": ["launch-C", "wake-C"]})
                        self.assertFalse(any("child-secret" in s for s in last_version.values()))
                        self.assertTrue(any("wake-fragment" in s and confirmed[mid] == "H1" for mid, s in last_version.items()))
                        reminders = [opts.reply_to for _, body, opts in fx.channel.send_calls if getattr(body, "mentions", ())]
                        inline = [(anchor, post.markdown, [m.open_id for m in post.mentions])
                                  for anchor, post in fx.channel.replies if getattr(post, "mentions", ())]
                        self.assertEqual(inline, [("H1", "wake-A", ["ou_user"])] if not cards and not stop_wake else [])
                        separate_h1 = cards and not stop_wake
                        self.assertCountEqual([confirmed[mid] for mid in reminders], ["H1", "H2"] if separate_h1 else ["H2"])
                        self.assertCountEqual([next(b for b in bodies if b in last_version[mid]) for mid in reminders],
                                              ["wake-B", "wake-C"] if separate_h1 else ["wake-C"])
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
            await self.result_before_idle(cli, ordinary, result="second")
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
            replies_before_save = len(fx.channel.replies)
            await app.handle_card_action(direct_card_event(fx.channel, form))
            mid, saved = fx.channel.updates[-1]
            self.assertEqual(mid, "om_card")
            self.assertEqual(len(fx.channel.replies), replies_before_save)
            self.assertIn("会话配置已保存", str(saved))
            self.assertIn("模型来源：原生 Claude 设置。", str(saved))
            self.assertNotIn("Codex", str(saved))
            self.assertFalse(elements(saved, "form"))

            await app.handle_message(FakeMessage("/config", message_id="config-again"))
            fx.channel.card_update_success = False
            await app.handle_card_action(direct_card_event(fx.channel, form_values(fx.channel.replies[-1][1])))
            self.assertEqual(fx.channel.replies[-1], ("om_card", f"会话 {binding.short_id} 的设置已保存。"))
            self.assertEqual(len(clients.calls), 1)

    async def test_group_created_and_configured_cards_name_the_context_recipient(self):
        async with owner_channel() as (app, fx, _, clock, _):
            clock[0] = 2.
            app._message_history = AsyncMock()
            app._message_history.resolve_anchor.return_value = MessageContextAnchor("new-card", 1000)
            for command in ("new", "config"):
                mid = f"{command}-card"
                fx.channel.reply_results.append(SendResult.ok(message_id=mid))
                await app.handle_message(FakeMessage(f"/{command}", message_id=command, chat_id="oc_group", chat_type="group"))
                picker = fx.channel.replies[-1][1]
                self.assertIn("作为背景交给 Claude Code。", str(picker.card))
                self.assertNotIn("Codex", str(picker.card))
                form = form_values(picker)
                options = {item["name"]: item["options"] for item in elements(picker.card, "select_static")}
                if command == "new":
                    form["new_project"] = options["new_project"][0]["value"]
                    form["new_context_mode"] = next(option["value"] for option in options["new_context_mode"]
                                                    if option["value"].endswith(":catch-up"))
                fx.channel.fetched_messages[mid] = {"data": {"items": [{"chat_id": "oc_group"}]}}
                fx.channel.chat_types["oc_group"] = "group"
                event = direct_button_event({}, message_id=mid, form_value=form)
                event.chat_id = "oc_group"
                await app.handle_card_action(event)
                updated_id, card = fx.channel.updates[-1]
                self.assertEqual(updated_id, mid)
                self.assertIn("作为背景交给 Claude Code。", str(card))
                self.assertNotIn("Codex", str(card))

    @requires_claude_sdk
    async def test_explore_card_reconciliation_orderings(self):
        # The two golden rows retain the complete call order on the original main.
        await self.exercise_card_order("before", "none", "single", reference="single_result_before_idle")
        await self.exercise_card_order("end", "none", "final wake", reference="late_first_receipt")
        for fault in ("unknown", "fail"):
            await self.exercise_card_order("before", "none", "single", at=0, fault=fault, matching_partial=True)
        for fault, final in (*product(("unknown", "fail"), ("matching", "oversize", "posted")), ("lost A, rejected B", "matching")):
            with self.subTest(running=fault, final=final):
                await self.exercise_partial_mention(fault, final)
        for card_count in (1, 2):
            with self.subTest(confirmed_cards=card_count):
                await self.exercise_disqualified_mention(card_count)
        for cards, case in (*product((False, True), ("preparation error", "preparation deadline")),
                            (True, "rejected"), (True, "lost"), (True, "empty"), (False, "empty"), (False, "conflicting receipt")):
            with self.subTest(cards=cards, answer=case):
                await self.exercise_answer_receipt(cards, case)
        for outcome in ("rejected", "unknown", "confirmed"):
            with self.subTest(replacement_receipt=outcome):
                await self.exercise_replacement_receipt(outcome)
        for cards, outcome in product((True, False), ("code", "rejection exception", "unknown exception")):
            with self.subTest(opening_card=cards, running_partial=outcome):
                await self.exercise_initial_partial(cards, outcome)
        for cut in ("preparation error", "preparation deadline", "running rejection", "terminal rejection",
                    "fallback", "uncertain matching", "uncertain distinct", "uncertain matching then confirmed freeze",
                    "budget", "registry", "newer header"):
            with self.subTest(review_cut=cut):
                await self.exercise_review_cut(cut)
        # Hold the busy receipt across a bodyless wake, a cron change and the final idle.
        for fault in ("ok", "unknown", "fail"):
            with self.subTest(busy_receipt=fault):
                await self.exercise_card_order("before", "wake", "bodyless", hold_busy=True, fault=fault)
        await self.exercise_card_order("before", "wake", "bodyless", hold_busy=True, fault="unknown", repeated=True)
        await self.exercise_card_order("before", "wake", "body", hold_busy=True, fault="fail", repeated=True)
        for receipt, wake in product(("result", "wake"), ("body", "bodyless")):
            await self.exercise_card_order(receipt, "before", wake, early_idle=True)

        for receipt, cron, wake in product(("before", "result", "wake"), ("before", "wake", "result"), ("body", "bodyless")):
            with self.subTest(receipt=receipt, cron=cron, wake=wake):
                count = await self.exercise_card_order(receipt, cron, wake)
                # Each replacement of an owned message may be rejected or may take
                # effect with its receipt lost. The next native trigger is healthy.
                for index, fault in product(range(count), ("unknown", "fail")):
                    with self.subTest(write=index, outcome=fault):
                        await self.exercise_card_order(receipt, cron, wake, at=index, fault=fault)

    async def exercise_card_order(self, receipt, cron, wake, *, at=None, fault="ok", hold_busy=False,
                                  repeated=False, early_idle=False, reference=None, matching_partial=False):
        from dataclasses import replace
        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
        from lark_channel import OutboundCard
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.cards import turn_progress_card
        from netizen_cli.ordinary_sessions import PhysicalAnswer

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            if matching_partial:
                app._progress_cards._poll_seconds = .01
            binding = fx.store.create_channel_binding(
                scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT), project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=True),
            )
            fx.channel.reply_results.extend(SendResult.ok(message_id=mid) for mid in ("first", "wake"))
            last_id = "first" if wake in ("single", "bodyless") else "wake"
            fx.channel.send_results.append(sent_result("reminder", chat_id="oc_direct", parent_id=last_id, root_id="H1"))
            opened, release_first, busy, release_busy = (asyncio.Event() for _ in range(4))
            visible, known, physical, text_answers = {}, {}, {}, {}
            attempts, redundant = [], []
            unavailable = set()
            send, update, complete = fx.channel.reply, fx.channel.update_card, app.handle_ordinary_completion

            async def reply(*args, **kwargs):
                if not isinstance(args[1], OutboundCard):
                    answer = next((mid for mid, event in physical.items() if str(args[1]).startswith(event.final_response)), None)
                    mid = f"text-{answer}" if answer else "notice"
                    fx.channel.reply_results.insert(0, SendResult.ok(message_id=mid))
                    if answer:
                        text_answers[answer] = mid
                result = await send(*args, **kwargs)
                if isinstance(args[1], OutboundCard):
                    visible[result.message_id] = args[1].card
                    if result.message_id == "first":
                        opened.set()
                        await release_first.wait()
                    known[result.message_id] = args[1].card
                return result

            async def replace_card(mid, card):
                if known.get(mid) == card:
                    redundant.append(mid)
                edge = len(attempts)
                attempts.append(mid)
                is_busy = (hold_busy and not busy.is_set() and cli.live.physical == "wake-physical"
                           and "Claude：执行中" in str(card))
                outcome = fault if edge == at or is_busy else "ok"
                if repeated and is_busy:
                    unavailable.add(mid)
                if mid in unavailable:
                    outcome = fault
                if outcome != "fail":
                    visible[mid] = card
                if is_busy:
                    busy.set()
                    await release_busy.wait()
                if outcome == "unknown":
                    known.pop(mid, None)
                    raise TimeoutError("applied replacement, lost receipt")
                if outcome == "fail":
                    return SendResult(success=False, raw={"code": 99991400})
                known[mid] = card
                return await update(mid, card)

            async def observe(event):
                # Only the two fixture bodies create result cards. A bodyless wake
                # still hands its physical verdict to the presenter.
                if isinstance(event, PhysicalAnswer) and event.final_response:
                    physical["first" if event.final_response in ("first answer", "one answer") else "wake"] = event
                await complete(event)

            def expected_card(mid, line):
                event = physical[mid]
                return turn_progress_card(
                    snapshot=replace(event.activity, state_line=line if mid == last_id else None),
                    partial_answers=event.partial_answers, final_response=event.final_response,
                    terminal_status="completed", run_continues=(wake != "single" and (mid == "first" or wake == "body")),
                    collapsed=True,
                ).card

            def assert_current(line):
                for mid in physical:
                    expected = expected_card(mid, line)
                    if mid in text_answers:
                        frozen = visible[mid]
                        self.assertEqual(frozen["header"], expected["header"])
                        self.assertFalse(_has_card_answer(frozen))
                        self.assertNotIn("Claude：", str(frozen))
                        self.assertNotIn("全部完成", str(frozen))
                        self.assertEqual(known.get(mid), frozen)
                        continue
                    self.assertEqual(visible[mid], expected, mid)
                    self.assertEqual(known.get(mid), expected, mid)

            with ExitStack() as stack:
                stack.enter_context(patch.object(fx.channel, "reply", reply))
                stack.enter_context(patch.object(fx.channel, "update_card", replace_card))
                mentions = _MentionTrace(fx.channel, stack)
                stack.enter_context(patch.object(app, "handle_ordinary_completion", observe))
                if reference:
                    self.freeze_trace(replace(fx, app=app), reference, stack=stack)
                if receipt == "before":
                    release_first.set()
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                ex = ordinary._active[binding.id]

                def system(kind, **data):
                    cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))

                async def set_cron(active):
                    await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}] if active else []},
                                             None, None)

                try:
                    cli.init(uuid="human-physical")
                    await asyncio.wait_for(opened.wait(), 5)
                    if receipt == "before":
                        await self.until(lambda: bool(app._progress_cards._sessions))
                    if matching_partial:
                        for block in (TextBlock("one answer"), ToolUseBlock("tool", "Read", {})):
                            cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                                uuid=type(block).__name__, message_id="partial"))
                        await self.until(lambda: bool(attempts))
                        self.assertEqual("one answer" in str(visible["first"]), fault == "unknown")
                    if wake != "single":
                        cli.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {})], "model", session_id=cli.session))
                        system("task_started", task_id="t", run_id="r", tool_use_id="tool")
                        system("background_tasks_changed", tasks=[{"task_id": "t", "run_id": "r"}])
                    if early_idle:
                        system("session_state_changed", state="idle")
                    cli.result(result="one answer" if wake == "single" else "first answer", idle=False)
                    if receipt == "result":
                        release_first.set()
                    if receipt in ("before", "result") and not hold_busy:
                        await self.until(lambda: "first" in physical and not ex.delivery.queues)
                    if wake == "single":
                        self.assertEqual(visible["first"]["header"]["title"]["content"], "任务已完成")
                        self.assertEqual(visible["first"]["config"]["summary"]["content"], "任务已完成")
                    system("session_state_changed", state="idle")
                    if wake != "single":
                        await self.until(lambda: ex.admissible)
                        if receipt in ("before", "result"):
                            await self.until(lambda: not ex.delivery.queues)
                        if cron == "before":
                            await set_cron(True)
                        if wake == "final wake":
                            system("background_tasks_changed", tasks=[])
                        cli.init(user_message_uuids=[], uuid="wake-physical")
                        if hold_busy:
                            await asyncio.wait_for(busy.wait(), 5)
                        elif receipt not in ("wake", "end"):
                            await self.until(lambda: cli.live.physical and not ex.delivery.queues)
                        if receipt == "wake":
                            release_first.set()
                        if cron == "wake":
                            await set_cron(True)
                        if early_idle:
                            system("session_state_changed", state="idle")
                        cli.result(user_message_uuid=None, uuid="wake-result", result=None if wake == "bodyless" else "wake answer",
                                   reason=None if wake == "bodyless" else "completed",
                                   origin={"kind": "task-notification", "producer": "session-task", "runId": "r"})
                        await self.until(lambda: "wake-result" in cli.live.seen)
                        if cron == "result":
                            await set_cron(True)
                        system("session_state_changed", state="idle")
                        await self.until(lambda: ex.human.terminal_sent if receipt == "end" else ex.admissible)
                finally:
                    release_first.set()
                    release_busy.set()

                if wake in ("body", "bodyless"):
                    await self.until(lambda: not ex.delivery.queues)
                    if repeated:
                        # A run of lost receipts on the old card must not stop it or another card.
                        for active in (False, True, False, True):
                            offset = len(attempts)
                            await set_cron(active)
                            await self.until(lambda: bool(ex.human.last_display[0].crons) == active
                                             and not ex.delivery.queues)
                            self.assertEqual(attempts[offset:], ["first"])
                            if fault == "unknown":
                                self.assertNotIn("first", known)
                            else:
                                self.assertEqual(known["first"], visible["first"])
                        unavailable.clear()
                    # Cron is an independent natural trigger. With a task present the rendered
                    # line is identical, so it repairs only unknown or still-outdated messages.
                    line = f"Claude：空闲，后台任务 1 个运行中，可以发送新消息。{CLAUDE_RELEASE_HINT}"
                    needed = {mid for mid in physical if mid not in text_answers and known.get(mid) != expected_card(mid, line)}
                    before = len(attempts)
                    await set_cron(False)
                    await self.until(lambda: ex.human.last_display and not ex.human.last_display[0].crons
                                     and not ex.delivery.queues)
                    self.assertCountEqual(attempts[before:], needed)
                    assert_current(line)
                    system("background_tasks_changed", tasks=[])
                    system("session_state_changed", state="idle")
                else:
                    before = len(attempts)
                self.assertTrue(await ordinary.wait_idle(10))
                assert_current("全部完成")
                if wake == "bodyless":
                    self.assertNotIn("wake", visible)
                if matching_partial:
                    self.assertTrue(all(isinstance(item, OutboundCard) for _, item in fx.channel.replies))
                self.assertEqual(redundant, [])
                self.assertEqual(mentions.assert_reminder(self), text_answers.get(last_id, last_id))
                return before

    async def exercise_partial_mention(self, fault, final):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT), project_alias="test",
                creator_id="ou_user", task_feedback=BindingTaskFeedback(progress_card_enabled=True))
            app._progress_cards._poll_seconds = .01
            fx.channel.reply_results.append(SendResult.ok(message_id="card-A"))
            visible, second = {}, asyncio.Event()
            update, reply = fx.channel.update_card, fx.channel.reply
            oversized = "字" * 40_000

            async def replace_card(mid, card):
                if "partial B" in str(card):
                    second.set()
                    if fault == "fail" or fault == "lost A, rejected B":
                        return SendResult(success=False, raw={"code": 99991400})
                    visible[mid] = card
                    raise TimeoutError("applied A+B, lost receipt")
                visible[mid] = card
                if fault == "lost A, rejected B" and card["header"]["title"]["content"] == "任务进行中":
                    raise TimeoutError("applied A, lost receipt")
                return await update(mid, card)

            async def post(target, content, *args):
                if content == oversized:
                    return SendResult.ok(message_id="posted") if final == "posted" else SendResult(success=False)
                if content == "partial B" or getattr(content, "markdown", "").endswith("partial B"):
                    fx.channel.reply_results.insert(0, SendResult.ok(message_id="fallback-B"))
                return await reply(target, content, *args)

            with ExitStack() as stack:
                stack.enter_context(patch.object(fx.channel, "update_card", replace_card))
                stack.enter_context(patch.object(fx.channel, "reply", post))
                mentions = _MentionTrace(fx.channel, stack)
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                cli.init()
                await self.until(lambda: bool(app._progress_cards._sessions))
                for label in ("partial A", "partial B"):
                    for block in (TextBlock(label), ToolUseBlock("tool-" + label, "Read", {})):
                        cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                            uuid=label + type(block).__name__, message_id=label))
                    if label == "partial A":
                        await self.until(lambda: "partial A" in str(visible.get("card-A")) and
                                         not app._progress_cards._writers["card-A"].locked())
                await asyncio.wait_for(second.wait(), 5)
                run_reply = next(iter(ordinary._active.values())).human.reply
                confirmed = run_reply.messages["card-A"].confirmed_answer_items
                self.assertEqual([kind for _, kind, _ in confirmed], [] if fault == "lost A, rejected B" else ["partial"])
                cli.result(result="partial B" if final == "matching" else oversized)
                self.assertTrue(await ordinary.wait_idle(10))
                self.assertIn("partial A", str(visible["card-A"]))
                if fault == "lost A, rejected B":
                    self.assertEqual(visible["card-A"]["header"]["title"]["content"], "任务已完成")
                    self.assertNotIn("partial B", str(visible["card-A"]))
                    self.assertEqual(mentions.assert_reminder(self), "fallback-B")
                    return
                recovered = fault == "fail" and final in ("posted", "matching")
                self.assertEqual(sum(content == INCOMPLETE for _, content in fx.channel.replies), int(not recovered))
                if final == "posted":
                    self.assertEqual(mentions.assert_reminder(self), "posted")
                elif fault == "fail":
                    self.assertEqual(mentions.assert_reminder(self), "fallback-B")
                else:
                    self.assertEqual(mentions.assert_reminder(self), "card-A")

    async def exercise_initial_partial(self, cards, outcome):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
        from lark_channel import OutboundCard, OutboundPost
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.topics import TopicPublishError

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            fx.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=False))
            presenter = app._progress_cards
            presenter._poll_seconds = .01
            open_card = presenter.open

            async def open_with_stage(event):
                await self.until(lambda: (change := event.observe()) and change.snapshot.partial_answers)
                return await open_card(event)

            unknown = outcome == "unknown exception"
            fx.channel.reply_results.append(SendResult(success=False, raw={"code": 99991400}) if outcome == "code"
                                            else TopicPublishError("opening send", unknown=unknown))
            with patch.object(presenter, "open", open_with_stage):
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                cli.init()
                for block in (TextBlock("stage"), ToolUseBlock("tool", "Read", {})):
                    cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                        uuid=type(block).__name__, message_id="initial"))
                await self.until(lambda: presenter._sessions and fx.channel.replies)
                session = next(iter(presenter._sessions.values()))
                first = fx.channel.replies[0][1]
                self.assertIsInstance(first, OutboundCard if cards else OutboundPost)
                self.assertIn("stage", str(first))
                opening_receipt = list(session.delivery.items.values())
                cli.result(result="distinct final")
                self.assertTrue(await ordinary.wait_idle(10))
            posts = [getattr(body, "markdown", body) for _, body in fx.channel.replies if not isinstance(body, OutboundCard)]
            expected = [] if cards and unknown else ["**阶段性答案**\n\nstage"]
            expected.append("distinct final")
            if unknown or not cards:
                expected.append(INCOMPLETE)
            self.assertEqual(posts, expected)
            self.assertEqual(sum(isinstance(body, OutboundCard) for _, body in fx.channel.replies), int(cards))
            self.assertTrue(all(target == "H1" for target, _ in fx.channel.replies))
            self.assertEqual(opening_receipt, ["unknown" if unknown else "rejected"])

    async def exercise_review_cut(self, cut):
        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel import constants
        from netizen_cli.channel.reply_events import RunDisplay

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                project_alias="test", creator_id="ou_user", task_feedback=BindingTaskFeedback(progress_card_enabled=True))
            presenter = app._progress_cards
            presenter._poll_seconds = .01
            fx.channel.reply_results.append(SendResult.ok(message_id="first"))
            with ExitStack() as stack:
                mentions = _MentionTrace(fx.channel, stack)
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                cli.init()
                await self.until(lambda: bool(presenter._sessions))
                session = next(iter(presenter._sessions.values()))
                run_reply = session.run_reply
                writer = session.writer
                initial = writer.known
                preparation = cut.startswith("preparation")
                uncertain = cut.startswith("uncertain")
                if preparation or uncertain or cut in ("running rejection", "fallback"):
                    if preparation:
                        await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
                    else:
                        update = fx.channel.update_card
                        async def reject_answer(mid, card):
                            if _has_card_answer(card):
                                if uncertain and card["header"]["title"]["content"] == "任务进行中":
                                    await update(mid, card)
                                    raise TimeoutError("applied running partial, receipt lost")
                                if (cut.endswith("confirmed freeze") and
                                        not any(e.get("element_id") == "turnanswerv1" for e in card["body"]["elements"])):
                                    return await update(mid, card)
                                return SendResult(success=False, raw={"code": 99991400})
                            return await update(mid, card)
                        stack.enter_context(patch.object(fx.channel, "update_card", reject_answer))
                    for block in (TextBlock("streamed partial"), ToolUseBlock("tool", "Read", {})):
                        cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session,
                            uuid=type(block).__name__, message_id="partial"))
                    await self.until(lambda: bool(session.delivery.items) and not writer.locked())
                    if cut == "running rejection":
                        self.assertIs(writer.known, initial)
                    if preparation:
                        self.assertIn("confirmed", session.delivery.items.values())
                        prepare = self.fail_preparation(stack, app, cut)
                        cli.result(result="answer ![preview](local.png)")
                        execution = ordinary._active[binding.id]
                        await self.until(lambda: not execution.humans and not execution.delivery.queues)
                        prepare.assert_called_once()
                        self.assertIs(ordinary._active[binding.id], execution)
                        self.assertNotIn(session.target.key.index, presenter._sessions)
                        self.assertTrue(session.task.done())
                        self.assertEqual(fx.channel.updates[-1][1]["header"]["title"]["content"], "任务已完成")
                        self.assertIn("streamed partial", str(fx.channel.updates[-1][1]))
                        self.assertEqual(mentions.assert_reminder(self), "first")
                        return
                    matching = cut.startswith("uncertain matching")
                    cli.result(result="streamed partial" if matching else "final answer")
                    self.assertTrue(await ordinary.wait_idle(10))
                    if cut == "fallback" or uncertain:
                        posts = [getattr(c, "markdown", c) for _, c in fx.channel.replies if not hasattr(c, "card")]
                        self.assertEqual(posts.count("final answer"), int(not matching))
                        self.assertEqual(sum(isinstance(c, str) and "streamed partial" in c for c in posts), int(cut == "fallback"))
                        if cut.endswith("confirmed freeze"):
                            self.assertEqual(fx.channel.updates[-1][1]["header"]["title"]["content"], "任务已完成")
                            self.assertIn("streamed partial", str(fx.channel.updates[-1][1]))
                            self.assertEqual(posts, [])
                            self.assertCountEqual([(kind, state) for (kind, _), state in session.delivery.items.items()],
                                                  [("body", "confirmed"), ("partial", "confirmed")])
                            self.assertEqual(mentions.assert_reminder(self), "first")
                    return
                if cut == "budget":
                    end = app._present_run_end
                    async def locked_end(event):
                        # Another caller owns this message's writer when RunEnded arrives.
                        async with writer:
                            await end(event)
                    stack.enter_context(patch.object(app, "_present_run_end", locked_end))
                    cli.result(result="final answer")
                    self.assertTrue(await ordinary.wait_idle(7), "run-end reconciliation exceeded its five-second budget")
                    self.assertIn(("H1", constants.DONE_REACTION), fx.channel.reactions)
                    self.assertEqual(mentions.assert_reminder(self), "first")
                    return
                if cut == "terminal rejection":
                    snapshots = []
                    post = fx.channel.reply
                    async def after_rejection(*args, **kwargs):
                        if args[1] == "final answer":
                            snapshots.append(writer.known)
                        return await post(*args, **kwargs)
                    stack.enter_context(patch.object(fx.channel, "reply", after_rejection))
                    stack.enter_context(patch.object(fx.channel, "update_card", return_value=SendResult(success=False, raw={"code": 99991400})))
                    cli.result(result="final answer")
                    self.assertTrue(await ordinary.wait_idle(10))
                    self.assertEqual(snapshots, [initial])
                    return
                cli.result(result="answer", idle=False)
                await self.until(lambda: bool(presenter._published.get(run_reply)) and not ordinary._active[binding.id].delivery.queues)
                registry = presenter._published[run_reply]
                view = registry["first"]
                display = RunDisplay(run_reply, lambda: True, lambda: "状态变化")
                if cut == "newer header":
                    from netizen_cli.channel.physical_delivery import record_sent_answer
                    fx.channel.reply_results.append(SendResult.ok(message_id="second"))
                    sent = await fx.channel.reply(FakeMessage("work", message_id="H1"), initial)
                    sent = view.location.validate(sent, card=True)
                    record_sent_answer(run_reply, view.location, "second-turn", (), sent)
                    update = fx.channel.update_card
                    async def lose_new(mid, card):
                        if mid == "second":
                            raise TimeoutError("answer on newer header unconfirmed")
                        return await update(mid, card)
                    stack.enter_context(patch.object(fx.channel, "update_card", lose_new))
                    await presenter.publish_card(display, "second", view.key, view.location, view.content,
                                                 delivery=view.delivery, items=view.items)
                    cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
                    self.assertTrue(await ordinary.wait_idle(10))
                    self.assertEqual(mentions.assert_reminder(self), "first")
                    return
                entered, release = asyncio.Event(), asyncio.Event()
                update = fx.channel.update_card
                async def hold(mid, card):
                    if mid == "first":
                        entered.set()
                        await release.wait()
                    return await update(mid, card)
                with patch.object(fx.channel, "update_card", hold):
                    running = asyncio.create_task(presenter.reconcile(display))
                    try:
                        await asyncio.wait_for(entered.wait(), 3)
                        view.location.validate(sent_result("first", chat_id="oc_other"), card=True)
                        view.location.validate(SendResult.ok(message_id="second"), card=True)
                        await presenter.publish_card(display, "second", view.key, view.location, view.content,
                                                     delivery=view.delivery, items=view.items)
                        self.assertIs(presenter._published[run_reply], registry, "in-flight writer lost its run registry")
                        self.assertEqual(list(registry), ["second"])
                    finally:
                        release.set()
                        await running
                cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
                self.assertTrue(await ordinary.wait_idle(10))
                self.assertNotIn(run_reply, presenter._published)

    async def exercise_replacement_receipt(self, outcome):
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.physical_delivery import BODY, PhysicalDelivery

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                project_alias="test", creator_id="ou_user", task_feedback=BindingTaskFeedback(progress_card_enabled=True))
            fx.channel.reply_results.append(SendResult.ok(message_id="first"))
            if outcome == "rejected":
                fx.channel.reply_results.append(SendResult(success=False, raw={"code": 99991400}))
            replace_card, settle = fx.channel.update_card, PhysicalDelivery.settle
            settles, writes = [], []
            image_item = ("image", "img_sent")

            def at_settle(ledger):
                settles.append((dict(ledger.items), run_reply.attempted))
                return settle(ledger)

            async def update(mid, card):
                if _has_card_answer(card):
                    writes.append((mid, physical.items.get(BODY), physical.items.get(image_item), run_reply.attempted))
                    if outcome == "rejected":
                        return SendResult(success=False, raw={"code": 99991400})
                    result = await replace_card(mid, card)
                    if outcome == "unknown":
                        raise TimeoutError("answer and image applied, receipt lost")
                    return result
                return await replace_card(mid, card)

            with ExitStack() as stack:
                stack.enter_context(patch.object(PhysicalDelivery, "settle", at_settle))
                stack.enter_context(patch.object(fx.channel, "update_card", update))
                mentions = _MentionTrace(fx.channel, stack)
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                run_reply = ordinary._active[binding.id].human.reply
                cli.init()
                await self.until(lambda: bool(app._progress_cards._sessions))
                physical = next(iter(app._progress_cards._sessions.values())).delivery
                cli.result(result="answer ![preview](img_sent)")
                self.assertTrue(await ordinary.wait_idle(10))
                self.assertEqual(writes[:1], [("first", "unknown", "unknown", True)])
                self.assertEqual(settles, [({BODY: outcome, image_item: outcome}, True)])
                self.assertEqual(sum(content == INCOMPLETE for _, content in fx.channel.replies), int(outcome != "confirmed"))
                self.assertEqual(mentions.assert_reminder(self), "first")

    async def test_deadline_during_text_fallback_preserves_the_incomplete_notice(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
        from lark_channel import OutboundCard
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.physical_delivery import BODY
        from netizen_cli.ordinary_sessions import PhysicalAnswer

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
            running, terminal, release_running = (asyncio.Event() for _ in range(3))
            calls = []
            present = app.handle_ordinary_completion

            async def observe(event):
                if isinstance(event, PhysicalAnswer):
                    terminal.set()
                await present(event)

            async def update(mid, card):
                if "final-answer" in str(card):
                    if "notice" not in calls:
                        calls.append("reject terminal")
                    return SendResult(success=False, raw={"code": 99991400})
                if not running.is_set() and "segment-one" in str(card):
                    calls.append("running")
                    running.set()
                    await release_running.wait()
                elif "body" in calls and "notice" not in calls:
                    calls.append("freeze")
                    await asyncio.Future()
                return SendResult.ok(message_id=mid)

            async def reply(origin, item, opts=None):
                if isinstance(item, OutboundCard):
                    return SendResult.ok(message_id="card")
                if item == "final-answer":
                    calls.append("body")
                    await asyncio.Future()
                if item == INCOMPLETE:
                    calls.append("notice")
                return SendResult.ok(message_id="post")

            with (patch.object(app, "handle_ordinary_completion", observe), patch.object(fx.channel, "update_card", update),
                  patch.object(fx.channel, "reply", reply), patch("netizen_cli.runtime.claude_ordinary._CONTROL_SECONDS", .2),
                  patch.object(ordinary, "_remaining", side_effect=lambda seconds: .5 if seconds == 30 else seconds)):
                app._progress_cards._poll_seconds = .005
                await app.handle_message(FakeMessage("work", message_id="H"))
                cli = clients.calls[-1]
                cli.init()
                await self.until(lambda: bool(app._progress_cards._sessions))
                ledger = next(iter(app._progress_cards._sessions.values())).delivery
                for block in (TextBlock("segment-one"), ToolUseBlock("tool", "Read", {})):
                    cli.stream.put_nowait(AssistantMessage([block], "m", session_id=cli.session,
                        uuid=type(block).__name__, message_id="model"))
                await asyncio.wait_for(running.wait(), 3)
                cli.result(result="final-answer", idle=False)
                try:
                    await asyncio.wait_for(terminal.wait(), 3)
                finally:
                    release_running.set()
                await self.until(lambda: not ordinary._active[binding.id].delivery.queues)
                self.assertEqual(calls, ["running", "reject terminal", "body", "notice"])
                self.assertTrue(ledger.settled)
                self.assertEqual(ledger.items[BODY], "unknown")
                cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
                self.assertTrue(await ordinary.wait_idle(3))

    async def test_terminal_replacement_settles_after_its_bounded_attempts(self):
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.physical_delivery import BODY, PhysicalDelivery

        for responses, expected, posts in (
            (("slow",), "confirmed", []),
            (("unknown", "confirmed"), "confirmed", []),
            (("unknown", "unknown", "unknown"), "unknown", [INCOMPLETE]),
            (("unknown", "rejected"), "unknown", [INCOMPLETE]),
            (("rejected",), "confirmed", ["final answer"]),
            (("deadline",), "unknown", [INCOMPLETE]),
        ):
            with self.subTest(responses=responses):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    binding = fx.store.create_channel_binding(
                        scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                        project_alias="test", creator_id="ou_user",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
                    fx.channel.reply_results.append(SendResult.ok(message_id="running"))
                    app._progress_cards._operation_timeout_seconds = .005
                    calls = []
                    settlements = []
                    settle = PhysicalDelivery.settle

                    def at_settlement(ledger):
                        # Subsequent CLI state updates may write this card again.
                        settlements.append((calls.copy(), ledger.items[BODY]))
                        return settle(ledger)

                    inflight = peak = 0
                    entered, release = asyncio.Event(), asyncio.Event()

                    async def update(mid, card):
                        nonlocal inflight, peak
                        self.assertEqual(mid, "running")
                        inflight += 1
                        peak = max(peak, inflight)
                        try:
                            if "final answer" in str(card):
                                response = responses[len(calls)] if len(calls) < len(responses) else "extra"
                                calls.append(response)
                                if response in {"slow", "deadline"}:
                                    entered.set()
                                    await release.wait()
                                if response == "unknown":
                                    raise OSError("lost replacement receipt")
                                if response == "rejected":
                                    return SendResult(success=False, raw={"code": 99991400})
                            return SendResult.ok(message_id=mid)
                        finally:
                            inflight -= 1

                    with (patch.object(fx.channel, "update_card", side_effect=update),
                          patch.object(PhysicalDelivery, "settle", at_settlement),
                          patch("netizen_cli.channel.constants.ITEM_RECEIPT_REQUEST_TIMEOUT_SECONDS", 1.0),
                          patch("netizen_cli.channel.reply_presenter._TERMINAL_CARD_RETRY_SECONDS", .001),
                          patch("netizen_cli.channel.reply_presenter._TERMINAL_CARD_UPDATE_ATTEMPTS", 3)):
                        await app.handle_message(FakeMessage("work", message_id="H"))
                        cli = clients.calls[-1]
                        cli.init()
                        await self.until(lambda: bool(app._progress_cards._sessions))
                        ledger = next(iter(app._progress_cards._sessions.values())).delivery
                        budget = .05 if responses == ("deadline",) else 2.
                        with patch.object(ordinary, "_remaining", side_effect=lambda seconds: budget if seconds == 30 else seconds):
                            cli.result(result="final answer", idle=False)
                            if responses == ("slow",):
                                await asyncio.wait_for(entered.wait(), 2)
                                # Release after the old request bound and before the item-receipt bound.
                                asyncio.get_running_loop().call_later(.025, release.set)
                            await self.until(lambda: ledger.settled and not ordinary._active[binding.id].delivery.queues)
                    self.assertEqual(settlements, [(list(responses), expected)])
                    self.assertEqual(peak, 1)
                    self.assertEqual([item for _, item in fx.channel.replies if isinstance(item, str)], posts)

    async def test_idle_during_a_confirmed_terminal_retry_updates_the_card_after_settlement(self):
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel.physical_delivery import BODY

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=True, completion_mention_enabled=False))
            fx.channel.reply_results.append(SendResult.ok(message_id="card"))
            await app.handle_message(FakeMessage("work", message_id="H"))
            cli = clients.calls[-1]
            cli.init()
            await self.until(lambda: bool(app._progress_cards._sessions))
            ledger = next(iter(app._progress_cards._sessions.values())).delivery
            second, release = asyncio.Event(), asyncio.Event()
            writes = []

            async def update(mid, card):
                if "final-answer" in str(card):
                    writes.append((ledger.settled, "idle" if "全部完成" in str(card) else "running"))
                    if len(writes) == 2:
                        second.set()
                        await release.wait()
                    elif not ledger.settled:
                        raise OSError("lost receipt")
                return SendResult.ok(message_id=mid)

            with (patch.object(fx.channel, "update_card", update),
                  patch("netizen_cli.channel.reply_presenter._TERMINAL_CARD_RETRY_SECONDS", .005)):
                cli.result(result="final-answer", idle=False)
                await asyncio.wait_for(second.wait(), 3)
                try:
                    _system_frame(cli, "session_state_changed", state="idle")
                    await self.until(lambda: ordinary._active[binding.id].human.terminal_sent)
                finally:
                    release.set()
                self.assertTrue(await ordinary.wait_idle(3))
            self.assertEqual([state for settled, state in writes if not settled], ["running", "running"])
            self.assertIn((True, "idle"), writes)
            self.assertEqual(ledger.items[BODY], "confirmed")
            self.assertNotIn(("H", INCOMPLETE), fx.channel.replies)

    async def test_writer_recomputes_after_each_unknown_delay(self):
        from netizen_cli.channel.replacement_writer import ReplacementWriter

        for receipts, next_desired, expected_calls, expected_known in (
            (["confirmed"], "B", ["A"], "A"),
            (["rejected"], "B", ["A"], "previous"),
            (["unknown", "confirmed"], "B", ["A", "B"], "B"),
            (["unknown", "rejected"], "B", ["A", "B"], None),
            (["unknown"] * 3, "B", ["A", "B", "B"], None),
            (["unknown"], None, ["A"], None),
        ):
            with self.subTest(receipts=receipts, next_desired=next_desired):
                writer = ReplacementWriter(str)
                writer.known = "previous"
                desired = "A"
                write = AsyncMock(side_effect=receipts)
                waits = []

                async def delay(seconds):
                    nonlocal desired
                    self.assertTrue(writer.locked())
                    waits.append(seconds)
                    desired = next_desired

                async with writer:
                    with patch("netizen_cli.channel.replacement_writer.asyncio.sleep", delay):
                        confirmed = await writer.reconcile_locked(lambda: desired, write, unknown_retry_delays=(1, 2))
                self.assertEqual([call.args[0] for call in write.await_args_list], expected_calls)
                self.assertEqual(waits, [1, 2][:min(receipts.count("unknown"), 2)])
                self.assertEqual(writer.known, expected_known)
                self.assertEqual(confirmed, receipts[-1] == "confirmed")

    def fail_preparation(self, stack, app, case):
        from dataclasses import replace
        from netizen_cli.ordinary_sessions import PhysicalAnswer

        if case == "preparation error":
            return stack.enter_context(patch("netizen_cli.result_images.parse_markdown", side_effect=ValueError("parse failed")))
        complete = app.handle_ordinary_completion
        async def deadline(event):
            if isinstance(event, PhysicalAnswer):
                event = replace(event, delivery_deadline=asyncio.get_running_loop().time())
            await complete(event)
        async def wait_for_path(*args):
            await asyncio.Future()
        stack.enter_context(patch.object(app, "handle_ordinary_completion", deadline))
        local_io = SimpleNamespace(**vars(asyncio))
        local_io.to_thread = prepare = AsyncMock(side_effect=wait_for_path)
        stack.enter_context(patch("netizen_cli.result_images.asyncio", local_io))
        return prepare

    async def exercise_answer_receipt(self, cards, case):
        from netizen_cli.bindings import BindingTaskFeedback
        from netizen_cli.channel import constants

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT), project_alias="test",
                creator_id="ou_user", task_feedback=BindingTaskFeedback(progress_card_enabled=cards))
            body = "answer"
            with ExitStack() as stack:
                if cards:
                    fx.channel.reply_results.append(SendResult.ok(message_id="first"))
                if case == "rejected":
                    fx.channel.reply_results.append(SendResult(success=False, raw={"code": 99991400}))
                if case.startswith("preparation"):
                    body = "![picture](local.png)"
                    prepare = self.fail_preparation(stack, app, case)
                elif case in ("lost", "rejected"):
                    replace_card = fx.channel.update_card
                    async def update(mid, card):
                        if case == "lost":
                            await replace_card(mid, card)
                            raise TimeoutError("answer replacement receipt lost")
                        return SendResult(success=False, raw={"code": 99991400})
                    stack.enter_context(patch.object(fx.channel, "update_card", update))
                elif case == "conflicting receipt":
                    fx.channel.reply_results.append(SendResult.ok(message_id="top", raw={"code": 0, "data": {"message_id": "raw"}}))
                elif case == "empty":
                    body = None
                mentions = _MentionTrace(fx.channel, stack)
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                cli.init()
                await self.until(lambda: bool(app._progress_cards._sessions))
                await self.result_before_idle(cli, ordinary, result=body, reason="completed" if body else None)
                self.assertTrue(await ordinary.wait_idle(10))
                if case.startswith("preparation"):
                    prepare.assert_called_once()
                unconfirmed = case in ("lost", "rejected")
                self.assertEqual(mentions.attempted, unconfirmed or case == "conflicting receipt")
                self.assertFalse(mentions.confirmed)
                self.assertEqual(sum(content == INCOMPLETE for _, content in fx.channel.replies),
                                 int(unconfirmed or case.startswith("preparation")))
                self.assertIn(("H1", constants.DONE_REACTION), fx.channel.reactions)
                self.assertEqual(mentions.assert_reminder(self), "first" if cards else "H1")
                if unconfirmed:
                    self.assertEqual(fx.channel.replies[0][1].card["header"]["title"]["content"], "任务进行中")
                    self.assertEqual(bool(fx.channel.updates), case == "lost")
                if cards and not unconfirmed:
                    self.assertEqual(fx.channel.updates[-1][1]["header"]["title"]["content"], "任务已完成")

    async def exercise_disqualified_mention(self, card_count):
        from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2.
            binding = fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                project_alias="test", creator_id="ou_user", task_feedback=BindingTaskFeedback(progress_card_enabled=True))
            bad_id = "card-B" if card_count == 2 else "card-A"
            fx.channel.reply_results.extend(SendResult.ok(message_id=mid) for mid in ("card-A", "card-B"))
            reply, update = fx.channel.reply, fx.channel.update_card
            async def send(target, content, *args):
                if "disputable answer" in str(content):
                    return sent_result(bad_id, chat_id="oc_other")
                return await reply(target, content, *args)
            async def replace_card(mid, card):
                if mid == "card-A" and "Claude：" not in str(card) and "全部完成" not in str(card):
                    return SendResult(success=False)  # Keep A's state-line removal uncertain.
                return await update(mid, card)

            with ExitStack() as stack:
                stack.enter_context(patch.object(fx.channel, "reply", send))
                stack.enter_context(patch.object(fx.channel, "update_card", replace_card))
                mentions = _MentionTrace(fx.channel, stack)
                await app.handle_message(FakeMessage("work", message_id="H1"))
                cli = clients.calls[-1]
                ex = ordinary._active[binding.id]
                def system(kind, **data):
                    cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))
                cli.init()
                await self.until(lambda: bool(app._progress_cards._sessions))
                cli.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {})], "model", session_id=cli.session))
                system("task_started", task_id="t", run_id="r", tool_use_id="tool")
                system("background_tasks_changed", tasks=[{"task_id": "t", "run_id": "r"}])
                cli.result(result="first answer")
                await self.until(lambda: "card-A" in mentions.confirmed and ex.admissible and not ex.delivery.queues)
                answers = ["second answer", "disputable answer"] if card_count == 2 else ["disputable answer"]
                for index, answer in enumerate(answers):
                    cli.init(user_message_uuids=[], uuid=f"wake-{index}")
                    cli.result(user_message_uuid=None, uuid=f"result-{index}", result=answer,
                               origin={"kind": "task-notification", "producer": "session-task", "runId": "r"})
                    system("session_state_changed", state="idle")
                    await self.until(lambda: f"result-{index}" in cli.live.seen and ex.admissible and not ex.delivery.queues)
                self.assertFalse(ex.human.reply.messages[bad_id].usable)
                self.assertTrue(ex.human.reply.messages[bad_id].confirmed_answer_items)
                system("background_tasks_changed", tasks=[])
                system("session_state_changed", state="idle")
                self.assertTrue(await ordinary.wait_idle(10))
                self.assertEqual(mentions.disqualified, {bad_id})
                self.assertEqual(mentions.assert_reminder(self), "card-A" if card_count == 2 else None)

    @requires_claude_sdk
    async def test_explorer_rejects_incorrect_mention_anchors(self):
        for mutation, witness, args in (
            ("oldest", self.exercise_card_order, ("before", "before", "body")),
            ("ignore usable", self.exercise_disqualified_mention, (2,)),
            ("count unconfirmed", self.exercise_review_cut, ("newer header",)),
            ("drop input exception", self.exercise_answer_receipt, (False, "empty")),
        ):
            with self.subTest(mutation=mutation), _mutate_mention(mutation), self.assertRaisesRegex(AssertionError, "completion mention anchor"):
                await witness(*args)

    def test_mention_rows_follow_current_card_ownership(self):
        from netizen_cli.channel.physical_delivery import BODY, PhysicalDelivery, record_sent_answer
        from netizen_cli.channel.topics import BindingCardLocation, CardReplyLocation
        from netizen_cli.ordinary_sessions import LogicalReply

        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = BindingCardLocation({})
        first = CardReplyLocation(scope, "H1", "H1", binding)
        second = CardReplyLocation(scope, "H2", "H2", binding)
        reply = LogicalReply()
        ledger = PhysicalDelivery()
        for mid in ("older", "newer"):
            ledger.begin((BODY,), reply)
            receipt = first.validate(SendResult.ok(message_id=mid))
            record_sent_answer(reply, first, mid, (BODY,), receipt)
        row = reply.messages["newer"]
        self.assertEqual((row.usable, reply.mention_target("H1")), (True, "newer"))
        self.assertTrue(second.validate(SendResult.ok(message_id="newer"), card=True).success)
        self.assertEqual((row.usable, reply.mention_target("H1")), (False, "older"))
        second.retire()
        self.assertEqual((row.usable, reply.mention_target("H1")), (True, "newer"))

    @requires_claude_sdk
    async def test_explorer_rejects_a_queued_intermediate_view(self):
        from dataclasses import replace
        from netizen_cli.ordinary_sessions import CliStateChanged

        enqueue = ClaudeOrdinarySessions._queue

        def capture_state(runtime, execution, human, event, **kwargs):
            if isinstance(event, CliStateChanged):
                captured = event.current()
                event = replace(event, current=lambda: captured)
            return enqueue(runtime, execution, human, event, **kwargs)

        with patch.object(ClaudeOrdinarySessions, "_queue", capture_state), self.assertRaisesRegex(AssertionError, "late_first_receipt"):
            await self.exercise_card_order("end", "none", "final wake", reference="late_first_receipt")

    @requires_claude_sdk
    async def test_retired_or_disqualified_cards_release_their_payloads_while_cron_keeps_client_open(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock
        from netizen_cli.bindings import BindingTaskFeedback

        for conflict in ("none", "current", "older"):
            with self.subTest(conflict=conflict):
                async with owner_channel() as (app, fx, clients, clock, ordinary):
                    clock[0] = 2.
                    binding = fx.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                        project_alias="test", creator_id="ou_user", task_feedback=BindingTaskFeedback(progress_card_enabled=True))
                    fx.channel.reply_results.extend(SendResult.ok(message_id=mid) for mid in ("first", "wake"))
                    await app.handle_message(FakeMessage("work", message_id="H"))
                    cli = clients.calls[-1]
                    cli.init()
                    presenter = app._progress_cards
                    await self.until(lambda: bool(presenter._sessions))
                    ex = ordinary._active[binding.id]
                    reply = ex.human.reply
                    await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)

                    def system(kind, **data):
                        cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))

                    cli.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {})], "model", session_id=cli.session))
                    system("task_started", task_id="A", run_id="R", tool_use_id="tool")
                    system("background_tasks_changed", tasks=[{"task_id": "A", "run_id": "R"}])
                    cli.result(result="first answer")
                    await self.until(lambda: ex.admissible and not ex.delivery.queues and "first" in presenter._published.get(reply, {}))
                    self.assertEqual(list(presenter._published[reply]), ["first"])
                    writer = presenter._writers["first"]
                    self.assertIsNotNone(writer.known)
                    location = presenter.binding_location(binding.id)
                    if conflict == "current":
                        with patch.object(fx.channel, "update_card", return_value=sent_result("first", chat_id="oc_other")):
                            cli.init(user_message_uuids=[])
                            await self.until(lambda: "first" in location.unusable and not ex.delivery.queues)
                        self.assertFalse(presenter._published.get(reply))
                        self.assertIsNone(writer.known)
                    else:
                        cli.init(user_message_uuids=[])
                    origin = {"kind": "task-notification", "producer": "session-task", "runId": "R"}
                    if conflict == "older":
                        await self.until(lambda: cli.live.physical and not ex.delivery.queues)
                        update = fx.channel.update_card
                        async def remove_old_line(mid, card):
                            if mid == "first":
                                return sent_result(mid, chat_id="oc_other")
                            return await update(mid, card)
                        with patch.object(fx.channel, "update_card", remove_old_line):
                            cli.result(result="wake answer", user_message_uuid=None, idle=False, origin=origin)
                            await self.until(lambda: "first" in location.unusable and not ex.delivery.queues)
                        self.assertIsNone(writer.known)
                        self.assertEqual(list(presenter._published[reply]), ["wake"])
                        writer = presenter._writers["wake"]
                        self.assertIsNotNone(writer.known)
                    else:
                        cli.result(result=None, reason=None, user_message_uuid=None, idle=False, origin=origin)
                    system("background_tasks_changed", tasks=[])
                    system("session_state_changed", state="idle")
                    await self.until(lambda: not ex.humans and not ex.delivery.queues)
                    self.assertIs(ordinary._active[binding.id], ex)
                    self.assertTrue(cli.live.crons)
                    self.assertNotIn(reply, presenter._published)
                    self.assertIsNone(reply.messages)
                    self.assertIsNone(writer.known)
                    self.assertEqual(location.unusable, set() if conflict == "none" else {"first"})

    async def test_background_presentation_is_per_physical_result_and_mentions_last_reply_once(self):
        from netizen_cli.bindings import BindingTaskFeedback
        for cards in (False, True):
            async with owner_channel() as (app, fx, clients, clock, ordinary):
                from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
                # ADR 0106 section 4 covers only card.header.title.content and
                # card.config.summary.content. In the decoded background_cards trace,
                # writes 3, 5 and 7 update "first". The wake publishes with no owned
                # work remaining and keeps its heading through the state-line update.
                self.freeze_trace(replace(fx, app=app), "background_cards" if cards else "background_plain")
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
                    self.assertIn(("H1", f"physical one\n\n后台任务 1 个运行中，可继续发送消息\n\n{CLAUDE_RELEASE_HINT}"),
                                  fx.channel.replies)
                fx.channel.reply_results.append(SendResult.ok(message_id="wake"))
                system("background_tasks_changed", tasks=[])
                cli.init(user_message_uuids=[])
                assistant(TextBlock("physical two"), "final")
                cli.result(user_message_uuid=None, result="physical two", origin={"kind": "task-notification",
                           "producer": "session-task", "runId": "r"})
                if not cards:
                    await self.until(lambda: ("H1", "physical two") in fx.channel.replies)
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
            self.freeze_trace(replace(fx, app=app), "rejected_body")
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
            await self.result_before_idle(cli, ordinary, result="body")
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
                    await self.result_before_idle(client, ordinary, result="answer with ![file](unavailable.png)")
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
                while not any(str(body) == f"cron body\n\n{CLAUDE_RELEASE_HINT}" for _, body, _ in fx.channel.send_calls):
                    await asyncio.sleep(.005)
            (destination, _, opts), = [call for call in fx.channel.send_calls if str(call[1]) == f"cron body\n\n{CLAUDE_RELEASE_HINT}"]
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
        from netizen_cli.channel.topics import TopicPublishError
        from lark_channel import OutboundCard
        for cards, outcome in ((False, "confirmed"), (True, "lost"), (True, "rejected"),
                               (True, "rejection exception"), (True, "confirmed")):
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
                if cards and outcome == "confirmed":
                    # H1's typing reaction can outlive its delivery queue.
                    await self.until(lambda: ("H1", "reaction-1") in fx.channel.reaction_removals)
                    self.freeze_trace(replace(fx, app=app), "ownerless_cron")
                cli.init(user_message_uuids=[])
                for i, block in enumerate((TextBlock("stage"), ToolUseBlock("tool", "Bash", {}))):
                    cli.stream.put_nowait(AssistantMessage([block], "model", session_id=cli.session, message_id="m", uuid=str(i)))
                raw = {"code": 0, "data": {"message_id": "sent", "chat_id": scope.chat_id,
                                           "thread_id": scope.topic_id, "root_id": "root"}}
                if outcome == "rejected":
                    fx.channel.send_results.append(SendResult(success=False, raw={"code": 99991400}))
                elif outcome == "rejection exception":
                    fx.channel.send_results.append(TopicPublishError("explicit non-delivery", unknown=False))
                elif outcome == "lost":
                    fx.channel.send_results.append(TimeoutError("fresh card receipt lost"))
                fx.channel.send_results.extend([SendResult.ok(message_id="sent", raw=raw)] * 2)
                cli.result(user_message_uuid=None, uuid="cron-result", result="final text")
                await self.until(lambda: "cron-result" in cli.live.seen and not ordinary._active[binding.id].delivery.queues)
                calls = fx.channel.send_calls
                self.assertEqual(sum(isinstance(body, OutboundCard) for _, body, _ in calls), int(cards))
                for to, body, opts in calls:
                    self.assertEqual((to, opts.reply_to, opts.reply_in_thread), (scope.chat_id, "root", True))
                self.assertEqual(len({opts.uuid for _, _, opts in calls}), len(calls))
                texts = [body for _, body, _ in calls if isinstance(body, str)]
                self.assertEqual(len(calls), int(cards) + len(texts))
                if outcome == "lost":
                    self.assertEqual(texts, [INCOMPLETE])
                elif not cards or outcome in ("rejected", "rejection exception"):
                    self.assertEqual(texts, ["**阶段性答案**\n\nstage", f"final text\n\n{CLAUDE_RELEASE_HINT}"])
                else:
                    self.assertEqual(texts, [])
                if cards:
                    self.assertIn("stage", str(calls[0][1]))
                    self.assertIn("final text", str(calls[0][1]))
                if cards and outcome == "confirmed":
                    await self.until(lambda: not ordinary._active[binding.id].delivery.queues)
                    expect_presenter_trace(self, replace(fx, app=app), "bodyless_anonymous_wake", reference=CLAUDE_FIXTURE)
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
                             [(f"second cron\n\n{CLAUDE_RELEASE_HINT}", "root")])

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
