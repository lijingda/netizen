from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from netizen.cards import (
    CardActionError,
    TurnFileCardLimitError,
    decode_turn_file_action,
    reply_card,
    reply_card_from_manifest,
    turn_files_card_from_manifest,
    turn_progress_card_from_manifest,
)
from netizen.domain import (
    FeishuScope,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardGoalModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    ScopeKind,
    TurnFileActionIntent,
    TurnProgressManifest,
    TurnProgressManifestStep,
)


def elements(value: object, tag: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(elements(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(elements(child, tag))
    return found


def page_buttons(card: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (button["text"]["content"], behavior["value"])
        for button in elements(card, "button")
        for behavior in button.get("behaviors", ())
        if behavior["value"].get("intent") == "turn-file.page"
    ]


def encoded_size(card: dict[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


class FilePaginationTest(unittest.TestCase):
    scope = FeishuScope(
        "cli_test", "oc_" + "a" * 32, ScopeKind.TOPIC, "omt_" + "b" * 32
    )

    def projection(self, count: int = 17, **files_options: Any) -> ReplyCardProjection:
        return ReplyCardProjection(
            scope=self.scope,
            result=ReplyCardResultModule("完成报告"),
            files=ReplyCardFilesModule(
                binding_id="binding-123",
                turn_id="turn-123",
                items=tuple(
                    ReplyCardFileItem(
                        path=f"/srv/work/result-{index:03}.txt",
                        label=f"result-{index:03}.txt",
                        size=1,
                        media_kind="file",
                        additions=12,
                        deletions=3,
                    )
                    for index in range(count)
                ),
                additions=count * 12,
                deletions=count * 3,
                **files_options,
            ),
        )

    def decode(self, value: object, form_value: object = None) -> TurnFileActionIntent:
        return decode_turn_file_action(
            app_id=self.scope.app_id,
            message_id="om_card",
            callback_chat_id=self.scope.chat_id,
            sender_id="ou_user",
            tag="button",
            value=value,
            form_value=form_value,
        )

    def test_every_page_uses_a_root_form_and_single_page_needs_no_navigation(self) -> None:
        for version in (4, 5):
            for page in range(3):
                with self.subTest(version=version, page=page):
                    card = reply_card(self.projection(page=page, action_version=version)).card
                    buttons = page_buttons(card)
                    self.assertEqual([(label, value["page"]) for label, value in buttons], [("跳转", page)])
                    self.assertEqual(buttons[0][1]["pagination"], "select")
                    self.assertIn(f"第 {page + 1}/3 页", str(card))
                    form = elements(card, "form")[0]
                    self.assertIn(form, card["body"]["elements"])
                    self.assertEqual(len(elements(form, "button")), 1)
                    selector = elements(form, "select_static")[0]
                    self.assertEqual(selector["initial_option"], str(page))
                    self.assertEqual([option["value"] for option in selector["options"]], ["0", "1", "2"])
                    for target in ("0", "1", "2"):
                        self.assertEqual(self.decode(buttons[0][1], {"turn_file_page": target}).page, int(target))
            single = reply_card(self.projection(8, action_version=version)).card
            self.assertEqual(page_buttons(single), [])
            self.assertEqual(elements(single, "form"), [])

    def test_actual_utf8_byte_boundary_checks_all_pages_before_first_render(self) -> None:
        for version in (4, 5):
            with self.subTest(version=version):
                projection = self.projection(action_version=version)
                assert projection.files is not None
                projection = replace(projection, files=replace(projection.files, items=tuple(
                    replace(item, label=("中文" * 100 + item.label)) if 8 <= index < 16 else item
                    for index, item in enumerate(projection.files.items)
                )))

                def render_all(answer: str) -> list[dict[str, Any]]:
                    return [reply_card(replace(
                        projection, result=ReplyCardResultModule(answer),
                        files=replace(projection.files, page=page),
                    )).card for page in range(3)]

                base = max(map(encoded_size, render_all("界")))
                increment = max(map(encoded_size, render_all("界x"))) - base
                answer = "界" + "x" * ((55_000 - base) // increment)
                fitting = render_all(answer)
                self.assertLessEqual(max(map(encoded_size, fitting)), 55_000)
                with patch("netizen.cards.reply.TURN_FILE_CARD_JSON_LIMIT_BYTES", 1_000_000):
                    oversized = render_all(answer + "x")
                self.assertLessEqual(encoded_size(oversized[0]), 55_000)
                self.assertGreater(encoded_size(oversized[1]), 55_000)
                with self.assertRaises(TurnFileCardLimitError):
                    reply_card(replace(projection, result=ReplyCardResultModule(answer + "x")))

    def test_select_has_one_manifest_and_all_fifty_pages_for_four_hundred_files(self) -> None:
        for version in (4, 5):
            with self.subTest(version=version):
                card = reply_card(self.projection(400, action_version=version))
                forms = elements(card.card, "form")
                self.assertEqual(len(forms), 1)
                self.assertIn(forms[0], card.card["body"]["elements"])
                selector = elements(forms[0], "select_static")[0]
                self.assertEqual(selector["name"], "turn_file_page")
                self.assertTrue(selector["required"])
                self.assertEqual(selector["initial_option"], "0")
                self.assertEqual([option["value"] for option in selector["options"]], [str(page) for page in range(50)])
                buttons = page_buttons(card.card)
                self.assertEqual(len(buttons), 1)
                label, value = buttons[0]
                self.assertEqual(label, "跳转")
                self.assertEqual(value["pagination"], "select")
                self.assertEqual(value["page"], 0)
                self.assertEqual(len(value["files"]), 400)
                self.assertEqual(str(card.card).count("'files':"), 1)
                self.assertEqual(elements(forms[0], "button")[0]["form_action_type"], "submit")
                self.assertEqual(len(elements(forms[0], "button")), 1)
                self.assertLessEqual(encoded_size(card.card), 55_000)
                intent = self.decode(value, {"turn_file_page": "49"})
                self.assertEqual(intent.page, 49)

    def test_select_decoder_requires_exact_canonical_in_range_form(self) -> None:
        for version in (4, 5):
            value = page_buttons(reply_card(self.projection(action_version=version)).card)[0][1]
            for form in (
                None, {}, [], "1", {"turn_file_page": 1}, {"turn_file_page": True},
                {"turn_file_page": ""}, {"turn_file_page": "01"}, {"turn_file_page": "+1"},
                {"turn_file_page": "-1"}, {"turn_file_page": " 1"}, {"turn_file_page": "1\n"},
                {"turn_file_page": "１"}, {"turn_file_page": "١"}, {"turn_file_page": "3"},
                {"turn_file_page": "999999999999999999999999999999"},
                {"turn_file_page": "1", "extra": "field"}, {"other": "1"},
            ):
                with self.subTest(version=version, form=form), self.assertRaises(CardActionError):
                    self.decode(value, form)
            for target in ("0", "1", "2"):
                intent = self.decode(value, {"turn_file_page": target})
                self.assertEqual(intent.page, int(target))
            for current in (-1, 3, True, "0"):
                with self.subTest(version=version, current=current), self.assertRaises(CardActionError):
                    self.decode({**value, "page": current}, {"turn_file_page": "1"})

    def test_legacy_pages_rebuild_as_select_and_only_page_accepts_marker(self) -> None:
        for version in (4, 5):
            card = reply_card(self.projection(action_version=version)).card
            value = page_buttons(card)[0][1]
            legacy = {key: item for key, item in value.items() if key != "pagination"}
            legacy["page"] = 1
            intent = self.decode(legacy)
            self.assertEqual(intent.page, 1)
            rebuild_args = dict(
                scope=intent.scope, binding_id=intent.binding_id, turn_id=intent.turn_id,
                manifest=intent.files, page=intent.page,
            )
            if version == 5:
                rebuilt = reply_card_from_manifest(reply=intent.reply, **rebuild_args)
            else:
                rebuilt = turn_files_card_from_manifest(
                    final_response=intent.answer, **rebuild_args,
                )
            self.assertEqual(page_buttons(rebuilt.card)[0][1]["pagination"], "select")
            self.assertEqual(page_buttons(rebuilt.card)[0][1]["v"], version)
            send = next(
                behavior["value"]
                for button in elements(card, "button")
                for behavior in button.get("behaviors", ())
                if behavior["value"].get("intent") == "turn-file.send"
            )
            for action in (legacy, send):
                for form in ({}, {"turn_file_page": "1"}):
                    with self.subTest(version=version, action=action["intent"], form=form), self.assertRaises(CardActionError):
                        self.decode(action, form)
            for pagination in (None, "", "buttons", "other", True, 1, []):
                with self.subTest(version=version, pagination=pagination), self.assertRaises(CardActionError):
                    self.decode({**value, "pagination": pagination})
            for pagination in ("buttons", "select"):
                with self.subTest(version=version, send=pagination), self.assertRaises(CardActionError):
                    self.decode({**send, "pagination": pagination})

    def test_select_roundtrip_keeps_modules_and_counts_after_file_removal(self) -> None:
        progress = TurnProgressManifest(
            state="running", steer_count=0, plan_available=True,
            plan_generated=True, plan_may_be_stale=False,
            steps=(TurnProgressManifestStep("检查结果", "completed"),),
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = tuple(Path(directory).resolve() / f"result-{index:03}.txt" for index in range(17))
            for path in paths:
                path.write_text("result", encoding="utf-8")
            current = paths[-1]
            for version in (4, 5):
                with self.subTest(version=version):
                    projection = self.projection(action_version=version)
                    assert projection.files is not None
                    projection = replace(
                        projection,
                        goal=ReplyCardGoalModule(
                            binding_id="binding-123", short_id="binding1", project_alias="test",
                            goal_generation="z" * 43, status="paused", runtime_state="goal-paused",
                            objective="生成报告", token_budget=None, tokens_used=50,
                        ) if version == 5 else None,
                        activity=ReplyCardActivityModule(progress=progress, terminal_status="completed", collapsed=True),
                        files=replace(projection.files, items=tuple(
                            replace(item, path=str(path))
                            for item, path in zip(projection.files.items, paths)
                        )),
                    )
                    value = page_buttons(reply_card(projection).card)[0][1]
                    intent = self.decode(value, {"turn_file_page": "2"})
                    kwargs = dict(
                        scope=intent.scope, binding_id=intent.binding_id, turn_id=intent.turn_id,
                        manifest=intent.files, page=intent.page,
                        additions=intent.additions, deletions=intent.deletions,
                    )
                    current.unlink(missing_ok=True)
                    if version == 5:
                        rebuilt = reply_card_from_manifest(reply=intent.reply, **kwargs)
                    else:
                        rebuilt = turn_progress_card_from_manifest(
                            final_response=intent.answer, progress=intent.progress, **kwargs,
                        )
                    next_value = page_buttons(rebuilt.card)[0][1]
                    self.assertEqual(next_value["pagination"], "select")
                    self.assertEqual(next_value["page"], 2)
                    self.assertEqual((next_value["a"], next_value["d"]), (204, 51))
                    self.assertEqual(next_value["files"][:-1], value["files"][:-1])
                    self.assertEqual(next_value["files"][-1], {
                        "path": str(current), "label": "result-016.txt",
                    })
                    module_key = "reply" if version == 5 else "progress"
                    self.assertEqual(next_value[module_key], value[module_key])
                    visible = "\n".join(item["content"] for item in elements(rebuilt.card, "markdown"))
                    self.assertIn("完成报告", visible)
                    self.assertIn("result-016.txt", visible)
                    self.assertIn("文件当前不可用", visible)
                    self.assertIn("+204", visible)
                    self.assertIn("-51", visible)
                    self.assertFalse(elements(rebuilt.card, "collapsible_panel")[0]["expanded"])
                    self.assertEqual(elements(rebuilt.card, "select_static")[0]["initial_option"], "2")
                    # The plain v4 rebuild uses the same page selector.
                    plain = turn_files_card_from_manifest(final_response=intent.answer, **kwargs)
                    self.assertEqual(page_buttons(plain.card)[0][1]["pagination"], "select")
                    current.write_text("result", encoding="utf-8")
