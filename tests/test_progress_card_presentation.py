from __future__ import annotations

import copy
import itertools
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from netizen_cli.cards import (
    CardActionError,
    TurnFileCardLimitError,
    decode_turn_file_action,
    reply_card,
    reply_card_from_manifest,
    turn_progress_card,
    turn_progress_card_from_manifest,
)
from netizen_cli.domain import (
    FeishuScope,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardPartialAnswerModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    ScopeKind,
)
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.turn_activity import (
    TurnActivityEntrySnapshot,
    TurnActivityKind,
    TurnActivityStatus,
)
from netizen_cli.turn_files import TurnFile
from tests.support.channel_cards import _elements


class ProgressCardPresentationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        self.files = tuple(
            TurnFile(
                display_path=f"result-{index}.txt",
                resolved_path=Path(f"/tmp/result-{index}.txt"),
                size=1,
                media_kind="file",
            )
            for index in range(9)
        )

    def snapshot(self, settings: ProgressCardSettings, **changes):
        value = dict(
            state="running",
            steer_count=0,
            plan_available=True,
            plan_generated=True,
            plan_may_be_stale=False,
            steps=(SimpleNamespace(step="计划内容", status="inProgress"),),
            commentary=(
                self.entry("进展甲", 1, commentary=True, timestamp=300),
                self.entry("进展乙", 3, commentary=True, timestamp=100),
            ),
            operations=(self.entry("操作甲", 2), self.entry("操作乙", 4)),
            progress_card_settings=settings,
        )
        value.update(changes)
        return SimpleNamespace(**value)

    @staticmethod
    def entry(text, sequence, *, commentary=False, timestamp=100):
        return TurnActivityEntrySnapshot(
            kind=TurnActivityKind.COMMENTARY if commentary else TurnActivityKind.COMMAND,
            status=TurnActivityStatus.COMPLETED,
            event_timestamp_ms=timestamp,
            text=text,
            sequence=sequence,
            segment=(sequence + 1) // 2,
        )

    @staticmethod
    def visible(card):
        def contents(value):
            if isinstance(value, dict):
                if value.get("tag") in {"markdown", "plain_text", "lark_md"}:
                    yield value["content"]
                else:
                    for key, child in value.items():
                        if key != "behaviors":
                            yield from contents(child)
            elif isinstance(value, list):
                for child in value:
                    yield from contents(child)
        return "\n".join(contents(card.card))

    def render(self, snapshot, *, files=()):
        return turn_progress_card(
            snapshot=snapshot,
            final_response="最终答案",
            files=files,
            terminal_status="completed",
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
        )

    @staticmethod
    def page_value(card):
        return next(
            behavior["value"]
            for button in _elements(card.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )

    def decode(self, value):
        return decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id=self.scope.chat_id,
            sender_id="ou_user",
            tag="button",
            form_value={"turn_file_page": "1"},
            value=value,
        )

    def test_content_and_time_order_ignore_timestamp_sorting(self):
        for organization, expected in (
            ("content", ("进展甲", "进展乙", "操作甲", "操作乙", "计划内容")),
            ("time", ("进展甲", "操作甲", "进展乙", "操作乙", "计划内容")),
        ):
            with self.subTest(organization=organization):
                visible = self.visible(self.render(self.snapshot(
                    ProgressCardSettings(organization=organization)
                )))
                positions = [visible.index(text) for text in expected]
                self.assertEqual(positions, sorted(positions))
                self.assertIn("millisecond='300'", visible)
                self.assertIn("millisecond='100'", visible)

    def test_missing_settings_use_the_same_defaults_and_pagination(self):
        defaults = ProgressCardSettings()
        explicit = self.render(self.snapshot(defaults), files=self.files)
        snapshot = self.snapshot(defaults)
        del snapshot.progress_card_settings
        implicit = self.render(snapshot, files=self.files)
        self.assertEqual(self.visible(implicit), self.visible(explicit))
        self.assertEqual(
            self.page_value(implicit)["progress"],
            self.page_value(explicit)["progress"],
        )
        self.assertEqual(
            self.decode(self.page_value(implicit)).progress.progress_card_settings,
            defaults,
        )

    def test_toggles_hide_only_selected_content(self):
        for organization in ("content", "time"):
            for commentary, operations, plan in itertools.product((False, True), repeat=3):
                with self.subTest(organization=organization, flags=(commentary, operations, plan)):
                    settings = ProgressCardSettings(
                        show_commentary=commentary,
                        show_operations=operations,
                        show_plan=plan,
                        organization=organization,
                    )
                    card = self.render(self.snapshot(settings))
                    visible = self.visible(card)
                    self.assertEqual("进展甲" in visible, commentary)
                    self.assertEqual("操作甲" in visible, operations)
                    self.assertEqual("计划内容" in visible, plan)
                    self.assertIn("最终答案", visible)
                    self.assertIn("已完成", json.dumps(card.card, ensure_ascii=False))
        hidden = ProgressCardSettings(show_commentary=False, show_operations=False, show_plan=False)
        unavailable = self.render(self.snapshot(hidden, plan_available=False))
        self.assertIn("过程信息：暂不可用", self.visible(unavailable))
        # A terminal page carries only its displayed content. Hidden runtime
        # windows/segment boundaries do not need a callback history copy.
        intent = self.decode(self.page_value(self.render(
            self.snapshot(hidden), files=self.files,
        )))
        self.assertEqual(intent.progress.commentary, ())
        self.assertEqual(intent.progress.operations, ())
        self.assertEqual(intent.progress.steps, ())

    def test_maximum_capacity_is_not_recut_to_legacy_defaults(self):
        settings = ProgressCardSettings(
            commentary_limit=8, operation_limit=16,
            commentary_max_chars=320, operation_max_chars=240,
            organization="time",
        )
        snapshot = self.snapshot(
            settings,
            commentary=tuple(self.entry(f"进展{i}" + "文" * 317, i * 3 + 1, commentary=True) for i in range(8)),
            operations=tuple(self.entry(f"op{i:02}" + "文" * 236, (i // 2) * 3 + i % 2 + 2) for i in range(16)),
        )
        visible = self.visible(self.render(snapshot))
        for entry in snapshot.commentary + snapshot.operations:
            self.assertIn(entry.text, visible)
        # Combined full windows, full plan and pagination retain the existing
        # explicit whole-card limit instead of silently trimming optional rows.
        snapshot.steps = tuple(SimpleNamespace(step="计" * 160, status="pending") for _ in range(12))
        with self.assertRaises(TurnFileCardLimitError):
            self.render(snapshot, files=self.files)

    def test_v4_and_v5_pagination_freeze_settings_and_order(self):
        settings = ProgressCardSettings(
            show_plan=False, organization="time",
            commentary_max_chars=320, operation_max_chars=240,
        )
        snapshot = self.snapshot(settings)
        snapshot.commentary = (replace(snapshot.commentary[0], text="进" * 320), snapshot.commentary[1])
        snapshot.operations = (replace(snapshot.operations[0], text="操" * 240), snapshot.operations[1])
        first = self.render(snapshot, files=self.files)
        v4 = self.decode(self.page_value(first))
        self.assertEqual(v4.progress.progress_card_settings, settings)
        self.assertEqual(v4.progress.commentary[0].text, "进" * 320)
        self.assertEqual(v4.progress.operations[0].text, "操" * 240)
        rebuilt = turn_progress_card_from_manifest(
            scope=v4.scope, binding_id=v4.binding_id, turn_id=v4.turn_id,
            final_response=v4.answer or "", manifest=v4.files,
            progress=v4.progress, page=v4.page or 0,
        )
        projection = ReplyCardProjection(
            scope=self.scope,
            activity=ReplyCardActivityModule(progress=v4.progress, terminal_status="completed", collapsed=True),
            partial_answer=ReplyCardPartialAnswerModule(("阶段性答案",)),
            result=ReplyCardResultModule("最终答案"),
            files=ReplyCardFilesModule(
                binding_id="binding-123", turn_id="turn-123",
                items=tuple(ReplyCardFileItem(path=str(file.resolved_path), label=file.display_path, size=1, media_kind="file") for file in self.files),
            ),
        )
        v5 = self.decode(self.page_value(reply_card(projection)))
        self.assertEqual(v5.reply.activity.progress, v4.progress)
        rebuilt_v5 = reply_card_from_manifest(
            scope=v5.scope, binding_id=v5.binding_id, turn_id=v5.turn_id,
            manifest=v5.files, reply=v5.reply, page=v5.page or 0,
        )
        for card in (rebuilt, rebuilt_v5):
            visible = self.visible(card)
            expected = ("进" * 320, "操" * 240, "进展乙", "操作乙")
            positions = [visible.index(text) for text in expected]
            self.assertEqual(positions, sorted(positions))
            self.assertNotIn("计划内容", visible)
            self.assertIn("最终答案", visible)
            self.assertIn("result-8.txt", visible)
        self.assertIn("阶段性答案", self.visible(rebuilt_v5))

    def test_callback_rejects_invalid_settings_and_order_metadata(self):
        value = self.page_value(self.render(
            self.snapshot(ProgressCardSettings(organization="time")), files=self.files,
        ))
        mutations = (
            lambda p: p["progress_card_settings"].update(operation_limit=17),
            lambda p: p["progress_card_settings"].update(show_plan=1),
            lambda p: p["progress_card_settings"].update(extra=True),
            lambda p: p["commentary"][0].pop("sequence"),
            lambda p: p["commentary"][0].update(sequence=True),
            lambda p: p["commentary"][0].update(sequence=0),
            lambda p: p["commentary"][0].update(sequence=10**18 + 1),
            lambda p: p["operations"][0].update(sequence=1),
            lambda p: p["commentary"].reverse(),
            lambda p: p["operations"][0].update(text="文" * 241),
            lambda p: p["operations"].extend(p["operations"] * 4),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                malformed = copy.deepcopy(value)
                mutate(malformed["progress"])
                with self.assertRaises(CardActionError):
                    self.decode(malformed)
