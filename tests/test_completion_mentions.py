from __future__ import annotations

import json
import unittest
from dataclasses import replace
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

from netizen.channel import reply_presenter
from netizen.channel_app import _outcome_completion_mention_user_id
from netizen.bindings import BindingTaskFeedback
from netizen.codex_runtime import (
    GoalFinalizationStatus,
    GoalOutcome,
    SideTurnOutcome,
    TurnOutcome,
)
from netizen.domain import (
    FeishuScope,
    GoalStatus,
    ScheduledConversation,
    ScheduledOrigin,
    ScopeKind,
)

import test_channel_app as fixtures


class CompletionMentionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ChannelApplicationTest()
        await self.fixture.asyncSetUp()
        self.app = self.fixture.app
        self.channel = self.fixture.channel
        self.runtime = self.fixture.runtime
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        await self.fixture.new()
        binding = self.fixture.store.active_binding(self.scope.key)
        self.fixture.store.assign_native_thread_id(binding.id, "native-one")
        self.binding = self.fixture.store.get(binding.id)
        self.origin = fixtures.FakeMessage(
            "run task", message_id="om_origin", sender_id="ou_other",
        )

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def outcome(self, *, side=False, **changes):
        values = dict(
            thread_id="native-one", turn_id="turn-one", owner_id="ou_initiator",
            origin=self.origin,
            result=fixtures.completed_turn_result(final_response="结果正文"),
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
            goal=fixtures.native_goal(GoalStatus.COMPLETE),
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
                for element in fixtures._elements(content, "markdown")
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

    def queue_reminder(self, card_id="om_progress", *, thread_id="omt_result"):
        self.channel.send_results.append(fixtures.sent_result(
            "om_reminder", chat_id="oc_direct", thread_id=thread_id,
            # Feishu can retain an older reply-tree root when promoting a card.
            root_id="om_older_root", parent_id=card_id,
        ))

    def assert_reminder(self, *, enabled=True, card_id="om_progress"):
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
        self.assertIs(opts.reply_in_thread, True)
        self.assertEqual(opts.reply_target_gone, "fail")
        self.assertTrue(opts.uuid)
        self.assertEqual(self.channel.send_results, [])

    async def start_progress(self, outcome):
        self.channel.reply_results.append(
            fixtures.sent_result("om_progress", chat_id="oc_direct"),
        )
        if isinstance(outcome, SideTurnOutcome):
            snapshot = fixtures.side_turn_activity_snapshot(
                side_id=outcome.side_id, thread_id=outcome.thread_id,
                turn_id=outcome.turn_id,
            )
            self.runtime.side_turn_activity_values[outcome.side_id] = snapshot
            started = await self.app._progress_cards.start_side(
                side_id=outcome.side_id, thread_id=outcome.thread_id,
                turn_id=outcome.turn_id, origin=outcome.origin,
            )
        else:
            snapshot = fixtures.turn_activity_snapshot(
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
        bot_seed = fixtures.FakeMessage(
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
        await self.app.handle_message(fixtures.FakeMessage("/new", message_id="om_new_form"))
        new_card = self.channel.replies[-1][1]
        values = self.fixture.new_form_values(new_card)
        self.assertTrue(values["new_completion_mention"].endswith(":on"))
        field = next(
            node for node in fixtures._elements(new_card.card, "select_static")
            if node["name"] == "new_completion_mention"
        )
        values["new_completion_mention"] = next(
            option["value"] for option in field["options"]
            if option["value"].endswith(":off")
        )
        await self.app.handle_card_action(self.fixture.direct_card_event(values))
        created = self.fixture.store.active_binding(self.scope.key)
        self.assertFalse(created.task_feedback.completion_mention_enabled)
        before_feedback = created.task_feedback
        await self.app.handle_message(fixtures.FakeMessage("/config", message_id="om_config_form"))
        config_card = self.channel.replies[-1][1]
        await self.app.handle_card_action(self.fixture.direct_card_event(
            self.fixture.config_form_values(config_card, completion_mention_enabled=True),
            message_id="om_config_card",
        ))
        configured = self.fixture.store.get(created.id)
        self.assertEqual(configured.task_feedback, replace(
            before_feedback, completion_mention_enabled=True,
        ))
        self.assertEqual(configured.feedback_revision, created.feedback_revision + 1)
        self.assertIsNone(configured.native_thread_id)
        self.assertEqual(self.runtime.submit_calls, [])

    async def test_progress_ordinary_and_side_send_one_topic_mention_after_terminal_card(self):
        for side in (False, True):
            for enabled in (False, True):
                with self.subTest(side=side, enabled=enabled):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    outcome = await self.start_progress(self.outcome(
                        side=side, turn_id=f"turn-{side}-{enabled}",
                        task_feedback=BindingTaskFeedback(
                            progress_card_enabled=True, completion_mention_enabled=enabled,
                        ),
                    ))
                    if enabled:
                        self.queue_reminder()
                    await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.replies), 1)
                    self.assertEqual(len(self.channel.updates), 1)
                    self.assertEqual(self.channel.updates[0][0], "om_progress")
                    self.assert_mention(self.channel.updates[0][1], enabled=False)
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
                    self.channel.card_update_results.append(
                        TimeoutError("terminal update response lost")
                        if response_lost else fixtures.retryable_sent_result()
                    )
                    with self.assertLogs("netizen.channel.reply_presenter", level="ERROR"):
                        await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.updates), 1)
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
                            patch("netizen.channel_app.turn_progress_card", side_effect=ValueError("cannot render")),
                            self.assertLogs("netizen.channel.reply_presenter", level="ERROR"),
                        ):
                            await self.app.handle_completion(outcome)
                    else:
                        await self.app.handle_completion(outcome)
                    self.assertEqual(self.channel.updates, [])
                    self.assertEqual(len(self.channel.replies), 1)
                    self.assert_mention(self.channel.replies[0][1], enabled=enabled)
                    self.assert_reminder(enabled=False)

    async def test_progress_existing_topic_reminder_replies_to_card_in_same_topic(self):
        origin = fixtures.FakeMessage(
            "run task", message_id="om_user_topic", chat_type="group",
            thread_id="omt_existing", sender_id="ou_other",
        )
        outcome = await self.start_progress(self.outcome(
            origin=origin, task_feedback=BindingTaskFeedback(progress_card_enabled=True),
        ))
        self.queue_reminder(thread_id="omt_existing")
        await self.app.handle_completion(outcome)
        self.assert_mention(self.channel.updates[-1][1], enabled=False)
        self.assert_reminder()
        self.assertEqual(self.fixture.store.list_bindings(self.app._scope(origin).key), [])

    async def test_reminder_failure_is_one_attempt_without_result_recovery(self):
        receipts = (
            TimeoutError("reminder response lost"),
            fixtures.retryable_sent_result(),
            fixtures.sent_result(
                "om_wrong_parent", chat_id="oc_direct", thread_id="omt_result",
                root_id="om_origin", parent_id="om_origin",
            ),
            fixtures.sent_result("om_main", chat_id="oc_direct"),
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
                with self.assertLogs("netizen.channel.completion_mentions", level="WARNING"):
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
                        if response_lost else fixtures.retryable_sent_result()
                    )
                    with self.assertLogs("netizen.channel_app", level="WARNING"):
                        await self.app.handle_completion(self.outcome(
                            result=fixtures.completed_turn_result(
                                fixtures.file_change_item("result.txt"), final_response="结果正文",
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
            patch("netizen.channel_app.turn_files_card", side_effect=ValueError("cannot render")),
            self.assertLogs("netizen.channel_app", level="ERROR"),
        ):
            await self.app.handle_completion(self.outcome(
                result=fixtures.completed_turn_result(
                    fixtures.file_change_item("result.txt"), final_response="结果正文",
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
                    result=fixtures.completed_turn_result(
                        fixtures.file_change_item(*paths), final_response="结果正文",
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
                page = fixtures._card_button_value(OutboundCard(card=card), "跳转")
                await self.app.handle_card_action(self.fixture.direct_button_event(
                    page, message_id="om_progress" if progress else "om_result",
                    form_value={"turn_file_page": "1"},
                ))
                self.assert_mention(self.channel.updates[-1][1], enabled=False)
                self.assertIn("result-08.txt", str(self.channel.updates[-1][1]))
                self.assertEqual(len(self.channel.replies), 1)
                self.assert_reminder(enabled=progress)

    async def test_goal_only_logical_known_ending_mentions_with_and_without_progress(self):
        cases = (
            (GoalStatus.COMPLETE, "completed", None, False, True),
            (GoalStatus.BLOCKED, "failed", None, False, True),
            (GoalStatus.USAGE_LIMITED, "completed", None, False, True),
            (GoalStatus.BUDGET_LIMITED, "completed", None, False, True),
            (GoalStatus.ACTIVE, "completed", None, False, False),
            (GoalStatus.PAUSED, "completed", None, False, False),
            (GoalStatus.COMPLETE, "interrupted", None, False, False),
            (GoalStatus.COMPLETE, "completed", None, True, False),
            (GoalStatus.COMPLETE, None, None, False, False),
            (None, "completed", None, False, False),
            (GoalStatus.COMPLETE, "completed", RuntimeError("state unknown"), False, False),
        )
        for progress in (False, True):
            for index, (status, turn_status, error, cleanup, enabled) in enumerate(cases):
                with self.subTest(progress=progress, status=status, turn_status=turn_status, error=error):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    self.channel.send_calls.clear()
                    self.channel.reply_results.append(fixtures.sent_result(
                        f"om_goal_{progress}_{index}", chat_id="oc_direct",
                    ))
                    outcome = self.goal_outcome(
                        logical_turn_id=f"goal-{progress}-{index}",
                        goal=fixtures.native_goal(
                            status, created_at=index + 1 + 100 * int(progress),
                        ) if status else None,
                        final_turn_status=turn_status, error=error,
                        background_cleanup_requested=cleanup,
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                        activity=replace(
                            fixtures.goal_activity_snapshot(binding_id=self.binding.id),
                            logical_turn_id=f"goal-{progress}-{index}",
                        ),
                    )
                    await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.replies), 1)
                    card = self.channel.replies[0][1]
                    self.assertEqual(
                        bool(fixtures._elements(card.card, "collapsible_panel")),
                        progress and turn_status in {"completed", "failed", "interrupted"},
                    )
                    if error is not None:
                        self.assertNotIn("<at ", json.dumps(card.card))
                    else:
                        self.assert_mention(card, enabled=enabled, text="Goal 结果正文")
                    self.assert_reminder(enabled=False)

    async def test_goal_original_card_terminal_update_preserves_toggle(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                running = fixtures.native_goal(created_at=1 + int(enabled))
                await self.fixture.register_goal_card(
                    scope=self.scope, binding=self.binding, goal=running,
                    message_id=f"om_goal_{enabled}", runtime_state="goal-running",
                    logical_turn_id=f"logical-{enabled}",
                )
                self.channel.replies.clear()
                origin = reply_presenter.GoalCardOrigin(
                    message_id=f"om_goal_{enabled}", scope=self.scope,
                    binding_id=self.binding.id, short_id=self.binding.short_id,
                    project_alias=self.binding.project_alias,
                    fallback_origin=self.origin,
                )
                await self.app.handle_completion(self.goal_outcome(
                    origin=origin, logical_turn_id=f"logical-{enabled}",
                    goal=replace(running, status=GoalStatus.COMPLETE),
                    task_feedback=BindingTaskFeedback(completion_mention_enabled=enabled),
                    finalization=GoalFinalizationStatus.UNKNOWN,
                    finalization_error=RuntimeError("clear reply lost"),
                ))
                self.assertEqual(self.channel.replies, [])
                self.assertEqual(len(self.channel.updates), 1)
                self.assert_mention(self.channel.updates[0][1], enabled=enabled, text="Goal 结果正文")

    async def test_uncertain_goal_update_and_card_fallback_only_mention_on_first_attempt(self):
        for progress in (False, True):
            with self.subTest(progress=progress):
                running = fixtures.native_goal(created_at=1 + int(progress))
                await self.fixture.register_goal_card(
                    scope=self.scope, binding=self.binding, goal=running,
                    message_id="om_goal", runtime_state="goal-running",
                )
                self.channel.replies.clear()
                origin = reply_presenter.GoalCardOrigin(
                    message_id="om_goal", scope=self.scope,
                    binding_id=self.binding.id, short_id=self.binding.short_id,
                    project_alias=self.binding.project_alias, fallback_origin=self.origin,
                )
                self.channel.card_update_results.append(TimeoutError("terminal update response lost"))
                self.channel.reply_results.append(TimeoutError("fallback card response lost"))
                with self.assertLogs("netizen.channel.reply_presenter", level="ERROR"):
                    await self.app.handle_completion(self.goal_outcome(
                        origin=origin, goal=replace(running, status=GoalStatus.COMPLETE),
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                    ))
                self.assertEqual(len(self.channel.updates), 1)
                self.assertEqual(self.channel.updates[0][0], "om_goal")
                self.assert_mention(self.channel.updates[0][1], enabled=not progress, text="Goal 结果正文")
                self.assertEqual(len(self.channel.replies), 2)
                self.assertIsInstance(self.channel.replies[0][1], OutboundCard)
                self.assert_mention(self.channel.replies[0][1], enabled=progress, text="Goal 结果正文")
                self.assert_mention(self.channel.replies[1][1], enabled=False, text="Goal 结果正文")
                self.assert_reminder(enabled=False)

    async def test_oversized_goal_result_keeps_mention_when_only_goal_controls_were_updated(self):
        for progress in (False, True):
            with self.subTest(progress=progress):
                running = fixtures.native_goal(created_at=1 + int(progress))
                await self.fixture.register_goal_card(
                    scope=self.scope, binding=self.binding, goal=running,
                    message_id="om_goal", runtime_state="goal-running",
                )
                self.channel.replies.clear()
                origin = reply_presenter.GoalCardOrigin(
                    message_id="om_goal", scope=self.scope,
                    binding_id=self.binding.id, short_id=self.binding.short_id,
                    project_alias=self.binding.project_alias, fallback_origin=self.origin,
                )
                answer = "Goal 结果正文\n" + "x" * 60_000
                with self.assertLogs("netizen.channel_app", level="WARNING"):
                    await self.app.handle_completion(self.goal_outcome(
                        origin=origin, goal=replace(running, status=GoalStatus.COMPLETE),
                        final_response=answer,
                        task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
                    ))
                self.assertEqual(len(self.channel.updates), 1)
                self.assertNotIn("<at ", json.dumps(self.channel.updates[0][1]))
                self.assertEqual(len(self.channel.replies), 1)
                self.assert_mention(self.channel.replies[0][1], enabled=True, text=answer)
                self.assert_reminder(enabled=False)

    async def test_progress_goal_reminder_uses_delivered_card_not_stale_origin(self):
        for update_fails in (False, True):
            with self.subTest(update_fails=update_fails):
                running = fixtures.native_goal(created_at=1 + int(update_fails))
                await self.fixture.register_goal_card(
                    scope=self.scope, binding=self.binding, goal=running,
                    message_id="om_current_goal", runtime_state="goal-running",
                )
                self.channel.replies.clear()
                self.channel.send_calls.clear()
                origin = reply_presenter.GoalCardOrigin(
                    message_id="om_stale_goal", scope=self.scope,
                    binding_id=self.binding.id, short_id=self.binding.short_id,
                    project_alias=self.binding.project_alias, fallback_origin=self.origin,
                )
                if update_fails:
                    self.channel.card_update_results.append(TimeoutError("update reply lost"))
                    self.channel.reply_results.append(fixtures.sent_result(
                        "om_new_goal", chat_id="oc_direct",
                    ))
                if not update_fails:
                    self.queue_reminder("om_current_goal")
                outcome = self.goal_outcome(
                    origin=origin, goal=replace(running, status=GoalStatus.COMPLETE),
                    task_feedback=BindingTaskFeedback(progress_card_enabled=True),
                )
                if update_fails:
                    with self.assertLogs("netizen.channel.reply_presenter", level="ERROR"):
                        await self.app.handle_completion(outcome)
                else:
                    await self.app.handle_completion(outcome)
                self.assertEqual(len(self.channel.updates), 1)
                self.assertEqual(self.channel.updates[0][0], "om_current_goal")
                self.assert_mention(self.channel.updates[0][1], enabled=False, text="Goal 结果正文")
                self.assertEqual(len(self.channel.replies), int(update_fails))
                if update_fails:
                    self.assert_mention(self.channel.replies[0][1], enabled=True, text="Goal 结果正文")
                self.assert_reminder(enabled=not update_fails, card_id="om_current_goal")

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
        (self.fixture.project / "result.png").write_bytes(fixtures.PNG)
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
                        result=fixtures.completed_turn_result(final_response=answer),
                        task_feedback=BindingTaskFeedback(
                            progress_card_enabled=False, completion_mention_enabled=mention,
                        ),
                    ))
                    self.assertEqual(len(self.channel.upload_calls), upload_count + 1)
                    self.assertGreaterEqual(len(requests), 2)
                    contents = [json.loads(request["content"]) for request in requests]
                    visible = "\n".join(
                        node["text"] for content in contents
                        for node in fixtures._elements(content, "md")
                    )
                    self.assertEqual(visible.count("完整结果"), 2000)
                    self.assertEqual(visible.count(f"![图](img_uploaded_{upload_count + 1})"), 1)
                    self.assertNotIn("result.png", visible)
                    mentions = [node for content in contents for node in fixtures._elements(content, "at")]
                    self.assertEqual([node["user_id"] for node in mentions], ["ou_initiator"] if mention else [])
                    self.assertEqual(len(fixtures._elements(contents[0], "at")), int(mention))
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
        with self.assertLogs("netizen.channel_app", level="WARNING"):
            await self.app.handle_completion(self.outcome(
                origin=sdk_origin,
                result=fixtures.completed_turn_result(final_response="完整结果\n" * 2000),
            ))
        self.assertEqual(driver.reply_message.await_count, 3)
        contents = [json.loads(call.kwargs["content"]) for call in driver.reply_message.call_args_list]
        # The SDK returns only the failed chunk's receipt, so the safe notice
        # must not infer that the first chunk's mention was never delivered.
        successful = [contents[0], contents[2]]
        mentions = [node for content in successful for node in fixtures._elements(content, "at")]
        self.assertEqual([mention["user_id"] for mention in mentions], ["ou_initiator"])
        self.assertIn("消息发送失败", str(contents[2]))
        self.assertEqual(fixtures._elements(contents[2], "at"), [])
        driver.create_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
