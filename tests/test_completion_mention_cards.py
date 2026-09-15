from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen.cards import (
    TurnFileCardLimitError,
    decode_turn_file_action,
    reply_card,
    reply_card_from_manifest,
    turn_files_card,
    turn_files_card_from_manifest,
    turn_progress_card,
    turn_progress_card_from_manifest,
)
from netizen.channel.reply_presenter import (
    GoalCardOrigin,
    _GoalCardDelivery,
    _ReplyCardPresenter,
)
from netizen.completion_mention import valid_completion_mention_user_id
from netizen.domain import (
    FeishuScope,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardGoalModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    ScopeKind,
    TurnProgressManifest,
)
from netizen.turn_files import TurnFile


USER_ID = "ou_completion_owner"
MENTION = f"<at id={USER_ID}></at>"
ANSWER = "**完成**：报告已生成。"
SCOPE = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
PROGRESS = TurnProgressManifest(
    state="running", steer_count=0, plan_available=False,
    plan_generated=False, plan_may_be_stale=False, steps=(),
)
GOAL = ReplyCardGoalModule(
    binding_id="binding-one", short_id="one", project_alias="test",
    goal_generation="z" * 43, status="blocked", runtime_state="goal-blocked",
    objective="generate reports", token_budget=None, tokens_used=50,
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
        for kind in ("files", "progress", "goal"):
            with self.subTest(kind=kind):
                if kind == "goal":
                    first = reply_card(ReplyCardProjection(
                        scope=SCOPE, goal=GOAL,
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
                self.assertEqual(value["v"], 5 if kind == "goal" else 4)
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
                if kind == "goal":
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
                if kind == "goal":
                    self.assertIn(GOAL.objective, "\n".join(markdown))

    def test_mention_is_counted_in_card_size_limit(self):
        plain = ReplyCardProjection(result=ReplyCardResultModule(ANSWER))
        baseline = reply_card(plain)
        limit = len(json.dumps(baseline.card, ensure_ascii=False).encode("utf-8"))
        with patch("netizen.cards.reply.TURN_FILE_CARD_JSON_LIMIT_BYTES", limit):
            reply_card(plain)
            with self.assertRaises(TurnFileCardLimitError):
                reply_card(replace(plain, result=ReplyCardResultModule(ANSWER, USER_ID)))


class CompletionMentionPresenterTest(unittest.IsolatedAsyncioTestCase):
    async def test_retained_goal_controls_do_not_repeat_terminal_mention(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                channel = SimpleNamespace(
                    update_card=AsyncMock(return_value=SimpleNamespace(success=True)),
                    reply=AsyncMock(return_value=SimpleNamespace(
                        success=True, message_id="om_card",
                    )),
                )
                presenter = _ReplyCardPresenter(channel, SimpleNamespace())
                self.addAsyncCleanup(presenter.close)
                projection = ReplyCardProjection(
                    scope=SCOPE, goal=GOAL,
                    result=ReplyCardResultModule(ANSWER, USER_ID),
                )
                origin = GoalCardOrigin(
                    message_id="om_card", scope=SCOPE, binding_id=GOAL.binding_id,
                    short_id=GOAL.short_id, project_alias=GOAL.project_alias,
                )
                arguments = dict(
                    binding_id=GOAL.binding_id, thread_id="thread-one",
                    logical_turn_id="logical-one", generation=GOAL.goal_generation,
                    origin=origin, projection=projection, retain_session=True,
                )
                if fallback:
                    result = await presenter.reply_goal_fallback(
                        **arguments, target=object(), card=reply_card(projection),
                    )
                    delivered = channel.reply.call_args.args[1].card
                else:
                    result = await presenter.finish_goal(**arguments)
                    delivered = channel.update_card.call_args.args[1]
                self.assertIs(result if fallback else result.status, _GoalCardDelivery.DELIVERED)
                self.assertEqual(visible_markdown(delivered).count(MENTION), 1)
                retained = presenter.goal_projection(
                    source_id="om_card", generation=GOAL.goal_generation,
                )
                self.assertIsNone(retained.result.completion_mention_user_id)
                presenter._goal_cards.clear()
                session_retained = presenter.goal_projection(
                    source_id="om_card", generation=GOAL.goal_generation,
                )
                self.assertEqual(session_retained, retained)
                self.assertTrue(await presenter.update_goal_module(
                    source_id="om_card", generation=GOAL.goal_generation, scope=SCOPE,
                    goal=replace(GOAL, notice="Updated status"), retain_session=True,
                ))
                updated = channel.update_card.call_args.args[1]
                self.assertIn(ANSWER, visible_markdown(updated))
                self.assertNotIn(USER_ID, json.dumps(updated))
