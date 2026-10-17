from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from netizen_cli.cards import (
    TurnFileCardLimitError,
    decode_turn_file_action,
    reply_card,
    reply_card_from_manifest,
    turn_files_card,
    turn_files_card_from_manifest,
    turn_progress_card,
    turn_progress_card_from_manifest,
)
from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.codex_runtime import GoalOutcome
from netizen_cli.completion_mention import valid_completion_mention_user_id
from netizen_cli.domain import (
    FeishuScope,
    NativeCapability,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardPartialAnswerModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    ScopeKind,
    TurnProgressManifest,
)
from netizen_cli.turn_files import TurnFile
from netizen_cli.sdk_gap_adapter import GoalStatus
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import native_goal


USER_ID = "ou_completion_owner"
MENTION = f"<at id={USER_ID}></at>"
ANSWER = "**完成**：报告已生成。"
SCOPE = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
PROGRESS = TurnProgressManifest(
    state="running", steer_count=0, plan_available=False,
    plan_generated=False, plan_may_be_stale=False, steps=(),
)


def elements(value, tag):
    if isinstance(value, dict):
        if value.get("tag") == tag:
            yield value
        for item in value.values():
            yield from elements(item, tag)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from elements(item, tag)


def visible_markdown(card):
    return [item["content"] for item in elements(card, "markdown")]


class CompletionMentionCardsTest(unittest.TestCase):
    def files(self):
        return tuple(
            TurnFile(
                display_path=f"result-{index}.txt",
                resolved_path=Path(f"/tmp/completion-mention/result-{index}.txt"),
                size=1, media_kind="file",
            )
            for index in range(9)
        )

    def test_target_validation_excludes_broadcast_and_markup(self):
        self.assertEqual(valid_completion_mention_user_id(USER_ID), USER_ID)
        for invalid in (
            None, "", "all", "ou_", " ou_user", "ou_user\n", "ou_中文",
            "ou_user></at><at id=all>", "ou_user'", 12, True,
            "ou_" + "a" * 126, "scheduled_plan:one",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(valid_completion_mention_user_id(invalid))
                if invalid is not None:
                    with self.assertRaisesRegex(ValueError, "user open_id"):
                        reply_card(ReplyCardProjection(result=ReplyCardResultModule(
                            ANSWER, completion_mention_user_id=invalid,
                        )))

    def test_terminal_result_adds_exact_mention_separate_from_answer(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                card = turn_progress_card(
                    snapshot=PROGRESS, terminal_status=status,
                    final_response=ANSWER,
                    completion_mention_user_id=USER_ID,
                )
                markdown = visible_markdown(card.card)
                self.assertIn(ANSWER, markdown)
                self.assertEqual(markdown.count(MENTION), 1)
                panel = next(elements(card.card, "collapsible_panel"))
                self.assertNotIn(MENTION, json.dumps(panel))
        without = turn_progress_card(
            snapshot=PROGRESS, terminal_status="completed", final_response=ANSWER,
        )
        self.assertNotIn(MENTION, json.dumps(without.card))

    def test_running_card_rejects_completion_mention(self):
        with self.assertRaisesRegex(ValueError, "running progress card"):
            turn_progress_card(
                snapshot=PROGRESS, completion_mention_user_id=USER_ID,
            )
        with self.assertRaisesRegex(ValueError, "running Activity"):
            reply_card(ReplyCardProjection(
                activity=ReplyCardActivityModule(PROGRESS),
                result=ReplyCardResultModule(ANSWER, USER_ID),
            ))

    def test_file_pagination_preserves_modules_without_repeating_mention(self):
        files = self.files()
        for kind in ("files", "progress", "partials"):
            with self.subTest(kind=kind):
                if kind == "partials":
                    first = reply_card(ReplyCardProjection(
                        scope=SCOPE,
                        partial_answer=ReplyCardPartialAnswerModule(("early physical turn answer",)),
                        activity=ReplyCardActivityModule(
                            PROGRESS, terminal_status="completed", collapsed=True,
                        ),
                        result=ReplyCardResultModule(ANSWER, USER_ID),
                        files=ReplyCardFilesModule(
                            binding_id="binding-one", turn_id="turn-one",
                            items=tuple(ReplyCardFileItem(
                                str(item.resolved_path), item.display_path,
                                item.size, item.media_kind,
                            ) for item in files),
                        ),
                    ))
                else:
                    arguments = dict(
                        scope=SCOPE, binding_id="binding-one", turn_id="turn-one",
                        final_response=ANSWER, files=files,
                        completion_mention_user_id=USER_ID,
                    )
                    first = (
                        turn_files_card(**arguments)
                        if kind == "files"
                        else turn_progress_card(
                            **arguments, snapshot=PROGRESS, terminal_status="completed",
                        )
                    )
                self.assertEqual(visible_markdown(first.card).count(MENTION), 1)
                callback_values = [
                    behavior["value"]
                    for button in elements(first.card, "button")
                    for behavior in button.get("behaviors", ())
                ]
                self.assertNotIn(USER_ID, json.dumps(callback_values))
                value = next(
                    item for item in callback_values
                    if item["intent"] == "turn-file.page"
                )
                self.assertEqual(value["v"], 5 if kind == "partials" else 4)
                intent = decode_turn_file_action(
                    app_id=SCOPE.app_id, message_id="om_card",
                    callback_chat_id=SCOPE.chat_id, sender_id="ou_clicker",
                    tag="button", value=value,
                    form_value={"turn_file_page": "1"},
                )
                arguments = dict(
                    scope=intent.scope, binding_id=intent.binding_id,
                    turn_id=intent.turn_id, manifest=intent.files, page=intent.page,
                )
                if kind == "partials":
                    self.assertIsNone(intent.reply.result.completion_mention_user_id)
                    rebuilt = reply_card_from_manifest(**arguments, reply=intent.reply)
                elif kind == "progress":
                    rebuilt = turn_progress_card_from_manifest(
                        **arguments, final_response=intent.answer,
                        progress=intent.progress,
                    )
                else:
                    rebuilt = turn_files_card_from_manifest(
                        **arguments, final_response=intent.answer,
                    )
                markdown = visible_markdown(rebuilt.card)
                self.assertIn(ANSWER, markdown)
                self.assertNotIn(USER_ID, json.dumps(rebuilt.card))
                self.assertIn("result-8.txt", "\n".join(markdown))
                if kind != "files":
                    self.assertTrue(list(elements(rebuilt.card, "collapsible_panel")))
                if kind == "partials":
                    self.assertIn("early physical turn answer", "\n".join(markdown))

    def test_mention_is_counted_in_card_size_limit(self):
        plain = ReplyCardProjection(result=ReplyCardResultModule(ANSWER))
        baseline = reply_card(plain)
        limit = len(json.dumps(baseline.card, ensure_ascii=False).encode("utf-8"))
        with patch("netizen_cli.cards.reply.TURN_FILE_CARD_JSON_LIMIT_BYTES", limit):
            reply_card(plain)
            with self.assertRaises(TurnFileCardLimitError):
                reply_card(replace(plain, result=ReplyCardResultModule(ANSWER, USER_ID)))


class CompletionMentionPresenterTest(unittest.IsolatedAsyncioTestCase):
    async def test_goal_management_queries_do_not_repeat_reply_or_terminal_mention(self):
        fixture = await self.enterAsyncContext(channel_fixture())
        await fixture.new()
        fixture.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = fixture.store.active_binding(scope.key)
        fixture.store.assign_native_thread_id(binding.id, "native-one")
        binding = fixture.store.get(binding.id)
        await fixture.app.handle_completion(GoalOutcome(
            binding_id=binding.id, thread_id="native-one", logical_turn_id="logical-one",
            final_physical_turn_id="physical-last", final_turn_status="completed",
            owner_id=USER_ID, origin=FakeMessage("work", message_id="om_origin"),
            goal=native_goal(GoalStatus.COMPLETE), final_response=ANSWER,
            task_feedback=BindingTaskFeedback(completion_mention_enabled=True),
        ))
        answer = fixture.channel.replies[-1][1]
        self.assertEqual(answer.markdown, ANSWER)
        self.assertEqual([identity.open_id for identity in answer.mentions], [USER_ID])
        for snapshot in (native_goal(GoalStatus.COMPLETE), None):
            fixture.runtime.goal_snapshot_value = snapshot
            await fixture.app.handle_message(FakeMessage("/goal", message_id="om_query"))
            card = fixture.channel.replies[-1][1].card
            self.assertNotIn(ANSWER, json.dumps(card, ensure_ascii=False))
            self.assertNotIn(USER_ID, json.dumps(card))
        self.assertEqual(len(fixture.channel.replies), 3)
        self.assertEqual(fixture.channel.updates, [])
