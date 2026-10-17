from __future__ import annotations

import json
import unittest
from contextlib import nullcontext
from dataclasses import replace
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import (
    FeishuChannel,
    Identity,
    OutboundCard,
    OutboundConfig,
    OutboundPost,
    OutboundSender,
    OutboundText,
    RetryConfig,
)

from netizen_cli.channel import reply_presenter
from netizen_cli.channel_app import _outcome_completion_mention_user_id
from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.codex_runtime import (
    GoalFinalizationStatus,
    GoalOutcome,
    GoalSubmission,
    SideTurnOutcome,
    TurnOutcome,
)
from netizen_cli.domain import (
    FeishuScope,
    GoalStatus,
    NativeCapability,
    ScheduledConversation,
    ScheduledOrigin,
    ScopeKind,
)

from tests.support.channel_messages import FakeMessage, PNG
from tests.support.channel_cards import (
    _card_button_value,
    _elements,
    config_form_values,
    direct_button_event,
    direct_card_event,
    new_form_values,
)
from tests.support.channel_results import (
    completed_turn_result,
    file_change_item,
    goal_activity_snapshot,
    native_goal,
    retryable_sent_result,
    sent_result,
    side_turn_activity_snapshot,
    turn_activity_snapshot,
)
from tests.support.channel_fixtures import channel_fixture


class CompletionMentionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(reply_presenter, "_TERMINAL_CARD_RETRY_SECONDS", 0.01))
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app = self.fixture.app
        self.channel = self.fixture.channel
        self.runtime = self.fixture.runtime
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        await self.fixture.new()
        binding = self.fixture.store.active_binding(self.scope.key)
        self.fixture.store.assign_native_thread_id(binding.id, "native-one")
        self.binding = self.fixture.store.get(binding.id)
        self.origin = FakeMessage(
            "run task", message_id="om_origin", sender_id="ou_other",
        )

    def outcome(self, *, side=False, **changes):
        values = dict(
            thread_id="native-one", turn_id="turn-one", owner_id="ou_initiator",
            origin=self.origin,
            result=completed_turn_result(final_response="结果正文"),
        )
        values.update(changes)
        if side:
            return SideTurnOutcome(
                side_id="side-one", parent_binding_id=self.binding.id,
                cwd=self.fixture.project, **values,
            )
        return TurnOutcome(binding_id=self.binding.id, **values)

    def goal_outcome(self, **changes):
        values = dict(
            binding_id=self.binding.id, thread_id="native-one",
            logical_turn_id="goal-one", owner_id="ou_initiator", origin=self.origin,
            goal=native_goal(GoalStatus.COMPLETE),
            final_physical_turn_id="turn-final", final_turn_status="completed",
            final_response="Goal 结果正文",
        )
        values.update(changes)
        return GoalOutcome(**values)

    def assert_mention(self, content, *, enabled, text="结果正文"):
        if isinstance(content, OutboundCard):
            content = content.card
        if isinstance(content, dict):
            visible = "\n".join(
                element["content"]
                for element in _elements(content, "markdown")
            )
            self.assertIn(text, visible)
            self.assertEqual(visible.count('<at id=ou_initiator></at>'), int(enabled))
            self.assertEqual(visible.count("<at "), int(enabled))
        elif enabled:
            self.assertIsInstance(content, OutboundPost)
            self.assertIn(text, content.markdown)
            self.assertEqual(content.mentions, [Identity(open_id="ou_initiator")])
        else:
            self.assertIsInstance(content, str)
            self.assertIn(text, content)

    def queue_reminder(self, card_id="om_progress", *, thread_id=None):
        self.channel.send_results.append(sent_result(
            "om_reminder", chat_id="oc_direct", thread_id=thread_id,
            # Feishu can retain an older reply-tree root when replying to a card.
            root_id="om_older_root", parent_id=card_id,
        ))

    async def start_goal_reply(self, outcome, *, scope=None, binding=None):
        scope, binding = scope or self.scope, binding or self.binding
        snapshot = replace(goal_activity_snapshot(binding_id=binding.id),
            thread_id=outcome.thread_id, logical_turn_id=outcome.logical_turn_id)
        self.runtime.goal_activity_values[binding.id] = snapshot
        if outcome.task_feedback.progress_card_enabled:
            self.channel.reply_results.append(sent_result("om_progress", chat_id=scope.chat_id,
                                                          thread_id=scope.topic_id))
        self.assertTrue(await self.app._present_running_goal(
            binding=binding, origin=outcome.origin,
            submission=GoalSubmission(binding.id, outcome.thread_id, outcome.logical_turn_id,
                lambda: None, task_feedback=outcome.task_feedback)))
        return replace(outcome, activity=snapshot)

    def assert_reminder(self, *, enabled=True, card_id="om_progress", topic=False):
        self.assertEqual(len(self.channel.send_calls), int(enabled))
        if not enabled:
            return
        chat_id, content, opts = self.channel.send_calls[0]
        self.assertEqual(chat_id, "oc_direct")
        self.assertIsInstance(content, OutboundText)
        self.assertEqual(content.text, "本轮任务已结束。")
        self.assertEqual(content.mentions, [Identity(open_id="ou_initiator")])
        self.assertEqual(opts.receive_id_type, "chat_id")
        self.assertEqual(opts.reply_to, card_id)
        self.assertNotEqual(opts.reply_to, self.origin.id)
        self.assertIs(opts.reply_in_thread, topic)
        self.assertEqual(opts.reply_target_gone, "fail")
        self.assertTrue(opts.uuid)
        self.assertEqual(self.channel.send_results, [])

    async def start_progress(self, outcome):
        self.channel.reply_results.append(
            sent_result("om_progress", chat_id="oc_direct"),
        )
        if isinstance(outcome, SideTurnOutcome):
            snapshot = side_turn_activity_snapshot(
                side_id=outcome.side_id, thread_id=outcome.thread_id,
                turn_id=outcome.turn_id,
            )
            self.runtime.side_turn_activity_values[outcome.side_id] = snapshot
            started = await self.app._progress_cards.start_side(
                side_id=outcome.side_id, thread_id=outcome.thread_id,
                turn_id=outcome.turn_id, origin=outcome.origin,
            )
        else:
            snapshot = turn_activity_snapshot(
                binding_id=outcome.binding_id, thread_id=outcome.thread_id,
                turn_id=outcome.turn_id,
            )
            self.runtime.turn_activity_values[outcome.binding_id] = snapshot
            started = await self.app._progress_cards.start(
                binding_id=outcome.binding_id, thread_id=outcome.thread_id,
                turn_id=outcome.turn_id, origin=outcome.origin,
            )
        self.assertTrue(started)
        self.assertNotIn("<at ", json.dumps(self.channel.replies[-1][1].card))
        return replace(outcome, activity=snapshot)

    async def test_plain_ordinary_and_side_result_use_captured_owner_with_toggle(self):
        bot_seed = FakeMessage(
            "Side seed", message_id="om_seed", sender_id="ou_bot", is_bot=True,
        )
        for side in (False, True):
            for enabled in (False, True):
                with self.subTest(side=side, enabled=enabled):
                    self.channel.replies.clear()
                    outcome = self.outcome(
                        side=side, origin=bot_seed if side else self.origin,
                        task_feedback=BindingTaskFeedback(completion_mention_enabled=enabled),
                    )
                    await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.replies), 1)
                    self.assertEqual(self.channel.replies[0][0], outcome.origin.id)
                    self.assert_mention(self.channel.replies[0][1], enabled=enabled)

    async def test_new_defaults_on_and_config_can_change_only_completion_mention(self):
        await self.app.handle_message(FakeMessage("/new", message_id="om_new_form"))
        new_card = self.channel.replies[-1][1]
        values = new_form_values(new_card)
        self.assertTrue(values["new_completion_mention"].endswith(":on"))
        field = next(
            node for node in _elements(new_card.card, "select_static")
            if node["name"] == "new_completion_mention"
        )
        values["new_completion_mention"] = next(
            option["value"] for option in field["options"]
            if option["value"].endswith(":off")
        )
        await self.app.handle_card_action(direct_card_event(self.channel, values))
        created = self.fixture.store.active_binding(self.scope.key)
        self.assertFalse(created.task_feedback.completion_mention_enabled)
        before_feedback = created.task_feedback
        await self.app.handle_message(FakeMessage("/config", message_id="om_config_form"))
        config_card = self.channel.replies[-1][1]
        await self.app.handle_card_action(direct_card_event(self.channel,
            config_form_values(config_card, completion_mention_enabled=True),
            message_id="om_config_card",
        ))
        configured = self.fixture.store.get(created.id)
        self.assertEqual(configured.task_feedback, replace(
            before_feedback, completion_mention_enabled=True,
        ))
        self.assertEqual(configured.feedback_revision, created.feedback_revision + 1)
        self.assertIsNone(configured.native_thread_id)
        self.assertEqual(self.runtime.submit_calls, [])

    async def test_progress_ordinary_and_side_send_one_mention_after_terminal_card(self):
        for side in (False, True):
            for enabled, retry in product((False, True), repeat=2):
                with self.subTest(side=side, enabled=enabled, retry=retry):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = await self.start_progress(self.outcome(
                        side=side, turn_id=f"turn-{side}-{enabled}-{retry}",
                        task_feedback=BindingTaskFeedback(
                            progress_card_enabled=True, completion_mention_enabled=enabled,
                        ),
                    ))
                    if enabled:
                        self.queue_reminder()
                    if retry:
                        self.channel.card_update_results.append(TimeoutError("update response lost"))
                    with self.assertLogs("netizen_cli.channel.reply_presenter", "ERROR") if retry else nullcontext():
                        await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.replies), 1)
                    self.assertEqual(len(self.channel.updates), 2 if retry else 1)
                    for message_id, card in self.channel.updates:
                        self.assertEqual(message_id, "om_progress")
                        self.assert_mention(card, enabled=False)
                    self.assert_reminder(enabled=enabled)

    async def test_uncertain_progress_update_mentions_new_text_fallback_once(self):
        for side in (False, True):
            for response_lost in (False, True):
                with self.subTest(side=side, response_lost=response_lost):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = await self.start_progress(self.outcome(
                        side=side, turn_id=f"uncertain-{side}-{response_lost}",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True),
                    ))
                    # FakeChannel records the applied payload before losing
                    # its response, just as a successful remote update can.
                    self.channel.card_update_results.extend(
                        TimeoutError("terminal update response lost")
                        if response_lost else retryable_sent_result()
                        for _ in range(3)
                    )
                    with self.assertLogs("netizen_cli.channel.reply_presenter", level="ERROR"):
                        await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.updates), 3)
                    self.assertEqual(self.channel.updates[0][0], "om_progress")
                    self.assert_mention(self.channel.updates[0][1], enabled=False)
                    self.assertEqual(len(self.channel.replies), 2)
                    self.assertEqual(self.channel.replies[-1][0], self.origin.id)
                    self.assert_mention(self.channel.replies[-1][1], enabled=True)
                    self.assert_reminder(enabled=False)

    async def test_unconfirmed_progress_update_skips_reminder(self):
        for side in (False, True):
            for index, receipt in enumerate((
                object(), SimpleNamespace(success=True, raw={"code": 230071}),
            )):
                with self.subTest(side=side, receipt=receipt):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = await self.start_progress(self.outcome(
                        side=side, turn_id=f"unconfirmed-update-{side}-{index}",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=True),
                    ))
                    self.channel.card_update_results.append(receipt)
                    await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.replies), 1)
                    self.assertEqual(len(self.channel.updates), 1)
                    self.assert_mention(self.channel.updates[0][1], enabled=False)
                    self.assert_reminder(enabled=False)

    async def test_unpublished_progress_result_mentions_original_text_fallback(self):
        for side in (False, True):
            for failure, enabled in (("no-session", True), ("render", True), ("no-session", False), ("render", False)):
                with self.subTest(side=side, failure=failure, enabled=enabled):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = self.outcome(
                        side=side, turn_id=f"unpublished-{side}-{failure}",
                        task_feedback=BindingTaskFeedback(
                            progress_card_enabled=True, completion_mention_enabled=enabled,
                        ),
                    )
                    if failure == "render":
                        outcome = await self.start_progress(outcome)
                        self.channel.replies.clear()
                        with (
                            patch("netizen_cli.channel_app.turn_progress_card", side_effect=ValueError("cannot render")),
                            self.assertLogs("netizen_cli.channel.reply_presenter", level="ERROR"),
                        ):
                            await self.app.handle_completion(outcome)
                    else:
                        await self.app.handle_completion(outcome)
                    self.assertEqual(self.channel.updates, [])
                    self.assertEqual(len(self.channel.replies), 1)
                    self.assert_mention(self.channel.replies[0][1], enabled=enabled)
                    self.assert_reminder(enabled=False)

    async def test_progress_existing_topic_reminder_replies_to_card_in_same_topic(self):
        origin = FakeMessage(
            "run task", message_id="om_user_topic", chat_type="group",
            thread_id="omt_existing", sender_id="ou_other",
        )
        outcome = await self.start_progress(self.outcome(
            origin=origin, task_feedback=BindingTaskFeedback(progress_card_enabled=True),
        ))
        self.queue_reminder(thread_id="omt_existing")
        await self.app.handle_completion(outcome)
        self.assert_mention(self.channel.updates[-1][1], enabled=False)
        self.assert_reminder(topic=True)
        self.assertEqual(self.fixture.store.list_bindings(self.app._scope(origin).key), [])

    async def test_reminder_failure_is_one_attempt_without_result_recovery(self):
        receipts = (
            TimeoutError("reminder response lost"),
            retryable_sent_result(),
            sent_result(
                "om_wrong_parent", chat_id="oc_direct",
                root_id="om_origin", parent_id="om_origin",
            ),
            sent_result("om_main", chat_id="oc_direct"),
        )
        for index, receipt in enumerate(receipts):
            with self.subTest(receipt=receipt):
                self.channel.replies.clear()
                self.channel.updates.clear()
                self.channel.send_calls.clear()
                outcome = await self.start_progress(self.outcome(
                    turn_id=f"reminder-failure-{index}",
                    task_feedback=BindingTaskFeedback(progress_card_enabled=True),
                ))
                self.channel.send_results.append(receipt)
                with self.assertLogs("netizen_cli.channel.completion_mentions", level="WARNING"):
                    await self.app.handle_completion(outcome)
                self.assertEqual(len(self.channel.replies), 1)
                self.assertEqual(len(self.channel.updates), 1)
                self.assert_mention(self.channel.updates[-1][1], enabled=False)
                self.assert_reminder()

    async def test_reminder_idempotency_key_is_stable_and_bound_to_task_and_card(self):
        outcome = self.outcome(task_feedback=BindingTaskFeedback(progress_card_enabled=True))
        uuids = []
        for candidate, card_id in (
            (outcome, "om_result"),
            (outcome, "om_result"),
            (replace(outcome, turn_id="turn-two"), "om_result"),
            (outcome, "om_other_result"),
        ):
            self.channel.send_calls.clear()
            self.queue_reminder(card_id)
            await self.app._remind_task_completion(candidate, card_id)
            self.assert_reminder(card_id=card_id)
            uuids.append(self.channel.send_calls[0][2].uuid)
        self.assertEqual(uuids[0], uuids[1])
        self.assertEqual(len(set(uuids)), 3)

    async def test_uncertain_file_card_send_does_not_repeat_mention_in_text_fallback(self):
        (self.fixture.project / "result.txt").write_text("result", encoding="utf-8")
        for progress in (False, True):
            for response_lost in (False, True):
                with self.subTest(progress=progress, response_lost=response_lost):
                    self.channel.replies.clear()
                    self.channel.reply_results.append(
                        TimeoutError("file card response lost")
                        if response_lost else retryable_sent_result()
                    )
                    with self.assertLogs("netizen_cli.channel_app", level="WARNING"):
                        await self.app.handle_completion(self.outcome(
                            result=completed_turn_result(
                                file_change_item("result.txt"), final_response="结果正文",
                            ),
                            task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                        ))
                    self.assertEqual(len(self.channel.replies), 2)
                    self.assertIsInstance(self.channel.replies[0][1], OutboundCard)
                    self.assert_mention(self.channel.replies[0][1], enabled=True)
                    self.assert_mention(self.channel.replies[1][1], enabled=False)
                    self.assertIn("result.txt", str(self.channel.replies[0][1].card))
                    self.assert_reminder(enabled=False)

    async def test_file_card_construction_failure_keeps_mention_for_text_fallback(self):
        (self.fixture.project / "result.txt").write_text("result", encoding="utf-8")
        with (
            patch("netizen_cli.channel_app.turn_files_card", side_effect=ValueError("cannot render")),
            self.assertLogs("netizen_cli.channel_app", level="ERROR"),
        ):
            await self.app.handle_completion(self.outcome(
                result=completed_turn_result(
                    file_change_item("result.txt"), final_response="结果正文",
                ),
                task_feedback=BindingTaskFeedback(progress_card_enabled=False),
            ))
        self.assertEqual(self.channel.updates, [])
        self.assertEqual(len(self.channel.replies), 1)
        self.assert_mention(self.channel.replies[0][1], enabled=True)

    async def test_known_failure_and_error_mention_but_interrupt_and_unknown_do_not(self):
        cases = (
            ("failed", None, False, True, "任务未完成"),
            (None, RuntimeError("execution failed"), False, True, "execution failed"),
            ("interrupted", None, False, False, "中断"),
            ("interrupted", RuntimeError("cancelled"), False, False, "cancelled"),
            ("completed", None, True, False, "结果正文"),
            ("interrupted", None, True, False, "中断"),
            ("running", None, False, False, "任务未完成"),
        )
        for progress in (False, True):
            for index, (status, error, cleanup, enabled, text) in enumerate(cases):
                with self.subTest(progress=progress, status=status, error=error, cleanup=cleanup):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = self.outcome(
                        turn_id=f"terminal-{progress}-{index}", error=error,
                        background_cleanup_requested=cleanup,
                        result=SimpleNamespace(status=status, final_response="结果正文", items=()),
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                    )
                    if progress:
                        outcome = await self.start_progress(outcome)
                        if enabled:
                            self.queue_reminder()
                    await self.app.handle_completion(outcome)
                    content = self.channel.updates[-1][1] if progress else self.channel.replies[-1][1]
                    self.assert_mention(content, enabled=enabled and not progress, text=text)
                    self.assert_reminder(enabled=enabled and progress)

    async def test_side_known_failure_mentions_but_unknown_terminal_error_does_not(self):
        cases = (
            (SimpleNamespace(status="failed", final_response="native failure", items=()), None, True),
            (None, RuntimeError("terminal observation failed"), False),
        )
        for progress in (False, True):
            for index, (result, error, enabled) in enumerate(cases):
                with self.subTest(progress=progress, error=error):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = self.outcome(
                        side=True, turn_id=f"side-terminal-{progress}-{index}",
                        result=result, error=error,
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                    )
                    if progress:
                        outcome = await self.start_progress(outcome)
                        if enabled:
                            self.queue_reminder()
                    await self.app.handle_completion(outcome)
                    content = self.channel.updates[-1][1] if progress else self.channel.replies[-1][1]
                    self.assert_mention(
                        content, enabled=enabled and not progress,
                        text="terminal observation failed" if error else "native failure",
                    )
                    self.assert_reminder(enabled=enabled and progress)

    async def test_side_stop_intent_suppresses_mention_when_completion_wins_interrupt_race(self):
        for progress in (False, True):
            for status in ("completed", "failed"):
                with self.subTest(progress=progress, status=status):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = self.outcome(
                        side=True, turn_id=f"side-stop-{progress}-{status}",
                        stop_requested=True, background_cleanup_requested=False,
                        result=SimpleNamespace(status=status, final_response="结果正文", items=()),
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                    )
                    if progress:
                        outcome = await self.start_progress(outcome)
                    await self.app.handle_completion(outcome)
                    content = self.channel.updates[-1][1] if progress else self.channel.replies[-1][1]
                    self.assert_mention(content, enabled=False)
                    self.assert_reminder(enabled=False)

    async def test_file_card_paging_does_not_repeat_completion_mention(self):
        paths = tuple(f"result-{index:02}.txt" for index in range(10))
        for path in paths:
            (self.fixture.project / path).write_text(path)
        for progress in (False, True):
            with self.subTest(progress=progress):
                self.channel.replies.clear()
                self.channel.updates.clear()
                self.channel.send_calls.clear()
                outcome = self.outcome(
                    turn_id=f"files-{progress}",
                    result=completed_turn_result(
                        file_change_item(*paths), final_response="结果正文",
                    ),
                    task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                )
                if progress:
                    outcome = await self.start_progress(outcome)
                    self.queue_reminder()
                await self.app.handle_completion(outcome)
                card = self.channel.updates[-1][1] if progress else self.channel.replies[-1][1].card
                self.assert_mention(card, enabled=not progress)
                self.assert_reminder(enabled=progress)
                page = _card_button_value(OutboundCard(card=card), "跳转")
                await self.app.handle_card_action(direct_button_event(
                    page, message_id="om_progress" if progress else "om_result",
                    form_value={"turn_file_page": "1"},
                ))
                self.assert_mention(self.channel.updates[-1][1], enabled=False)
                self.assertIn("result-08.txt", str(self.channel.updates[-1][1]))
                self.assertEqual(len(self.channel.replies), 1)
                self.assert_reminder(enabled=progress)

    async def test_goal_logical_reply_completion_is_independent_of_goal_management_state(self):
        cases = (
            (GoalStatus.COMPLETE, "completed", False, True),
            (GoalStatus.BLOCKED, "failed", False, True),
            (GoalStatus.USAGE_LIMITED, "completed", False, True),
            (GoalStatus.BUDGET_LIMITED, "completed", False, True),
            (GoalStatus.PAUSED, "completed", False, True),
            (None, "completed", False, True),  # Explicit clear does not discard the Turn result.
            (GoalStatus.PAUSED, "interrupted", False, False),
            (GoalStatus.COMPLETE, "completed", True, False),
        )
        for progress in (False, True):
            for index, (status, turn_status, cleanup, enabled) in enumerate(cases):
                with self.subTest(progress=progress, status=status, turn_status=turn_status):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = self.goal_outcome(
                        logical_turn_id=f"goal-{progress}-{index}",
                        goal=native_goal(status) if status else None,
                        final_turn_status=turn_status, background_cleanup_requested=cleanup,
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress))
                    outcome = await self.start_goal_reply(outcome)
                    if progress and enabled:
                        self.queue_reminder()
                    await self.app.handle_completion(outcome)
                    expected = "中断" if turn_status == "interrupted" else "Goal 结果正文"
                    if progress:
                        self.assertEqual(len(self.channel.replies), 1)
                        self.assertEqual(len(self.channel.updates), 1)
                        self.assert_mention(self.channel.updates[-1][1], enabled=False, text=expected)
                    else:
                        self.assertEqual(len(self.channel.replies), 1)
                        self.assert_mention(self.channel.replies[-1][1], enabled=enabled, text=expected)
                    self.assert_reminder(enabled=progress and enabled)
                    self.assertNotIn("恢复 Goal", str(self.channel.replies))
                    self.assertNotIn("清除 Goal", str(self.channel.updates))

    async def test_goal_unconfirmed_terminal_does_not_notify_completion(self):
        for progress in (False, True):
            with self.subTest(progress=progress):
                self.channel.replies.clear()
                self.channel.updates.clear()
                self.channel.send_calls.clear()
                outcome = self.goal_outcome(final_physical_turn_id=None, final_turn_status=None,
                    final_response=None, error=RuntimeError("native terminal is unknown"),
                    task_feedback=BindingTaskFeedback(progress_card_enabled=progress))
                outcome = await self.start_goal_reply(outcome)
                await self.app.handle_completion(outcome)
                content = self.channel.updates[-1][1] if progress else self.channel.replies[-1][1]
                self.assert_mention(content, enabled=False, text="native terminal is unknown")
                self.assert_reminder(enabled=False)

    async def test_goal_reply_reminder_respects_scope_and_feedback(self):
        for kind in (ScopeKind.DIRECT, ScopeKind.GROUP, ScopeKind.TOPIC):
            scope = FeishuScope("cli_test", "oc_direct", kind,
                                "omt_goal" if kind is ScopeKind.TOPIC else None)
            binding = self.binding
            if kind is not ScopeKind.DIRECT:
                created = await self.fixture.create_binding(scope)
                self.fixture.store.assign_native_thread_id(created.binding.id, f"native-{kind.value}")
                binding = self.fixture.store.get(created.binding.id)
            origin = FakeMessage("run goal", message_id="om_goal_origin", sender_id="ou_other",
                chat_type="p2p" if kind is ScopeKind.DIRECT else "group", thread_id=scope.topic_id)
            for progress, enabled in product((False, True), repeat=2):
                with self.subTest(kind=kind, progress=progress, enabled=enabled):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = self.goal_outcome(binding_id=binding.id, thread_id=binding.native_thread_id,
                        origin=origin, logical_turn_id=f"goal-{kind}-{progress}-{enabled}",
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress,
                                                          completion_mention_enabled=enabled))
                    outcome = await self.start_goal_reply(outcome, scope=scope, binding=binding)
                    if progress and enabled:
                        self.queue_reminder(thread_id=scope.topic_id)
                    await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.replies), 1)
                    content = self.channel.updates[-1][1] if progress else self.channel.replies[-1][1]
                    self.assert_mention(content, enabled=enabled and not progress, text="Goal 结果正文")
                    self.assert_reminder(enabled=enabled and progress, topic=kind is ScopeKind.TOPIC)

    async def test_goal_uncertain_card_update_falls_back_once_without_updating_management(self):
        outcome = await self.start_goal_reply(self.goal_outcome(
            task_feedback=BindingTaskFeedback(progress_card_enabled=True),
            finalization=GoalFinalizationStatus.UNKNOWN,
            finalization_error=RuntimeError("clear reply lost")))
        self.channel.card_update_results.extend(TimeoutError("reply lost") for _ in range(3))
        with self.assertLogs("netizen_cli.channel.reply_presenter", "ERROR"):
            await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.updates), 3)
        self.assertEqual({message_id for message_id, _ in self.channel.updates}, {"om_progress"})
        self.assertEqual(len(self.channel.replies), 2)
        self.assert_mention(self.channel.replies[-1][1], enabled=True, text="Goal 结果正文")
        self.assertIn("clear reply lost", self.channel.replies[-1][1].markdown)
        self.assertNotIn("清除 Goal", str(self.channel.replies[-1][1]))
        self.assert_reminder(enabled=False)

    async def test_goal_file_paging_keeps_final_physical_turn_id_and_does_not_repeat_mention(self):
        paths = tuple(f"goal-result-{index:02}.txt" for index in range(10))
        for path in paths:
            (self.fixture.project / path).write_text(path)
        for progress in (False, True):
            with self.subTest(progress=progress):
                self.channel.replies.clear()
                self.channel.updates.clear()
                self.channel.send_calls.clear()
                outcome = self.goal_outcome(logical_turn_id="physical-first",
                    final_physical_turn_id="physical-last", final_items=(file_change_item(*paths),),
                    task_feedback=BindingTaskFeedback(progress_card_enabled=progress))
                outcome = await self.start_goal_reply(outcome)
                if progress:
                    self.queue_reminder()
                await self.app.handle_completion(outcome)
                card = self.channel.updates[-1][1] if progress else self.channel.replies[-1][1].card
                self.assert_mention(card, enabled=not progress, text="Goal 结果正文")
                page = _card_button_value(OutboundCard(card=card), "跳转")
                self.assertEqual(page["turn_id"], "turn:v1:physical-last")
                await self.app.handle_card_action(direct_button_event(page,
                    message_id="om_progress" if progress else "om_result",
                    form_value={"turn_file_page": "1"}))
                updated = self.channel.updates[-1][1]
                self.assert_mention(updated, enabled=False, text="Goal 结果正文")
                self.assertIn("goal-result-08.txt", str(updated))
                next_page = _card_button_value(OutboundCard(card=updated), "跳转")
                self.assertEqual(next_page["turn_id"], "turn:v1:physical-last")
                self.assertEqual(len(self.channel.replies), 1)
                self.assert_reminder(enabled=progress)

    async def test_goal_resumed_from_direct_chat_card_retains_scope_for_result_files(self):
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        path = self.fixture.project / "resumed.txt"
        path.write_text("file result")
        for progress in (False, True):
            with self.subTest(progress=progress):
                self.channel.replies.clear()
                self.channel.updates.clear()
                self.runtime.goal_snapshot_value = native_goal(GoalStatus.PAUSED)
                self.runtime.goal_activity_values[self.binding.id] = goal_activity_snapshot(binding_id=self.binding.id)
                feedback = BindingTaskFeedback(progress_card_enabled=progress, completion_mention_enabled=False)
                self.runtime.goal_submission = GoalSubmission(
                    self.binding.id, "native-one", "goal-one", lambda: None, task_feedback=feedback,
                )
                if progress:
                    self.channel.reply_results.append(sent_result("om_progress", chat_id="oc_direct"))
                management = await self.app._goal_management_card(self.binding, self.scope)
                await self.app.handle_card_action(direct_button_event(
                    _card_button_value(management, "恢复 Goal"), message_id="om_management",
                ))
                origin = self.runtime.resume_goal_calls[-1]["origin"]
                await self.app.handle_completion(self.goal_outcome(
                    origin=origin, final_items=(file_change_item(str(path)),), task_feedback=feedback,
                ))
                result = (OutboundCard(card=self.channel.updates[-1][1]) if progress
                          else self.channel.replies[-1][1])
                self.assertIsInstance(result, OutboundCard)
                self.assertIn("resumed.txt", str(result.card))
                self.assertEqual(self.app._scope(origin), self.scope)

    def test_scheduled_initial_turn_never_mentions_a_human_even_with_valid_owner(self):
        origin = ScheduledOrigin(
            app_id="cli_test", chat_id="oc_group", message_id="om_seed",
            conversation=ScheduledConversation("omt_run"), plan_id="plan-one", run_id="run-one",
        )
        for progress in (False, True):
            for owner in ("schedule:run-one", "ou_plan_creator"):
                with self.subTest(progress=progress, owner=owner):
                    self.assertIsNone(_outcome_completion_mention_user_id(
                        self.outcome(
                            origin=origin, owner_id=owner,
                            task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                        ),
                    ))
        self.assertEqual(_outcome_completion_mention_user_id(self.outcome()), "ou_initiator")

    async def test_invalid_or_broadcast_owner_never_becomes_a_mention(self):
        for owner in ("all", "", "user", "ou_", 'ou_user\"/><at id="all', "ou_user\n", None):
            with self.subTest(owner=owner):
                self.channel.replies.clear()
                await self.app.handle_completion(self.outcome(owner_id=owner))
                self.assertEqual(self.channel.replies, [(self.origin.id, "结果正文")])

    def use_sdk_reply(self, driver):
        sender = OutboundSender(driver, OutboundConfig(retry=RetryConfig(max_attempts=1)))

        async def send(to, content, opts):
            outbound = OutboundPost(markdown=content) if isinstance(content, str) else content
            return await sender.send(
                outbound, receive_id=to, receive_id_type=opts.receive_id_type,
                reply_to=opts.reply_to, reply_in_thread=opts.reply_in_thread,
                reply_target_gone=opts.reply_target_gone, uuid_=opts.uuid,
            )

        async def sdk_reply(origin, content, opts=None):
            return await FeishuChannel.reply(SimpleNamespace(send=send), origin, content, opts)

        self.channel.reply = sdk_reply
        return SimpleNamespace(
            id=self.origin.id, message_id=self.origin.id, chat_id="oc_direct",
            conversation=SimpleNamespace(thread_id="omt_task"),
        )

    async def test_sdk_post_images_keep_chunk_routes_and_first_chunk_mention(self):
        (self.fixture.project / "result.png").write_bytes(PNG)
        answer = "完整结果\n" * 900 + "\n![图](result.png)\n\n" + "完整结果\n" * 1100
        for topic in (False, True):
            for mention in (False, True):
                with self.subTest(topic=topic, mention=mention):
                    requests = []

                    async def sent(**kwargs):
                        requests.append(kwargs)
                        return {"code": 0, "data": {
                            "message_id": f"om_result_{len(requests)}", "chat_id": "oc_direct",
                        }}

                    driver = SimpleNamespace(
                        reply_message=AsyncMock(side_effect=sent),
                        create_message=AsyncMock(side_effect=sent),
                    )
                    sdk_origin = self.use_sdk_reply(driver)
                    sdk_origin.conversation.thread_id = "omt_task" if topic else None
                    upload_count = len(self.channel.upload_calls)
                    await self.app.handle_completion(self.outcome(
                        origin=sdk_origin,
                        result=completed_turn_result(final_response=answer),
                        task_feedback=BindingTaskFeedback(
                            progress_card_enabled=False, completion_mention_enabled=mention,
                        ),
                    ))
                    self.assertEqual(len(self.channel.upload_calls), upload_count + 1)
                    self.assertGreaterEqual(len(requests), 2)
                    contents = [json.loads(request["content"]) for request in requests]
                    visible = "\n".join(
                        node["text"] for content in contents
                        for node in _elements(content, "md")
                    )
                    self.assertEqual(visible.count("完整结果"), 2000)
                    self.assertEqual(visible.count(f"![图](img_uploaded_{upload_count + 1})"), 1)
                    self.assertNotIn("result.png", visible)
                    mentions = [node for content in contents for node in _elements(content, "at")]
                    self.assertEqual([node["user_id"] for node in mentions], ["ou_initiator"] if mention else [])
                    self.assertEqual(len(_elements(contents[0], "at")), int(mention))
                    self.assertTrue(all(request["msg_type"] == "post" for request in requests))
                    for call in driver.reply_message.call_args_list:
                        self.assertEqual(call.kwargs["message_id"], self.origin.id)
                        self.assertEqual(call.kwargs["reply_in_thread"], topic)
                    if topic:
                        driver.create_message.assert_not_awaited()
                    else:
                        driver.reply_message.assert_awaited_once()
                        self.assertEqual(driver.create_message.await_count, len(requests) - 1)

    async def test_later_chunk_audit_failure_safe_notice_does_not_repeat_successful_mention(self):
        driver = SimpleNamespace(
            reply_message=AsyncMock(side_effect=[
                {"code": 0, "data": {"message_id": "om_first", "chat_id": "oc_direct"}},
                {
                    "code": 230028,
                    "msg": "The messages do NOT pass the audit, ext=contain sensitive data: EMAIL_ADDRESS",
                },
                {"code": 0, "data": {"message_id": "om_notice", "chat_id": "oc_direct"}},
            ]),
            create_message=AsyncMock(),
        )
        sdk_origin = self.use_sdk_reply(driver)
        with self.assertLogs("netizen_cli.channel_app", level="WARNING"):
            await self.app.handle_completion(self.outcome(
                origin=sdk_origin,
                result=completed_turn_result(final_response="完整结果\n" * 2000),
            ))
        self.assertEqual(driver.reply_message.await_count, 3)
        contents = [json.loads(call.kwargs["content"]) for call in driver.reply_message.call_args_list]
        # The SDK returns only the failed chunk's receipt, so the safe notice
        # must not infer that the first chunk's mention was never delivered.
        successful = [contents[0], contents[2]]
        mentions = [node for content in successful for node in _elements(content, "at")]
        self.assertEqual([mention["user_id"] for mention in mentions], ["ou_initiator"])
        self.assertIn("消息发送失败", str(contents[2]))
        self.assertEqual(_elements(contents[2], "at"), [])
        driver.create_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
