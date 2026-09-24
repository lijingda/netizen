from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from openai_codex.generated.v2_all import ModelUpgradeInfo

from netizen.bindings import BindingTurnSettings
from netizen.cards import config_card, new_binding_card
from netizen.cards.model_info import with_model_details
from netizen.cards.reply import TURN_FILE_CARD_JSON_LIMIT_BYTES
from netizen.domain import FeishuScope, ScopeKind
from netizen.model_settings import EffortOption, ModelCatalog, ModelOption
from netizen.projects import Project
from support.channel_cards import _elements


class ModelInfoCardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = FeishuScope("app", "chat", ScopeKind.DIRECT)
        self.model = ModelOption(
            id="alpha", model="wire-alpha", display_name="Alpha",
            description="适合日常开发", is_default=True,
            default_effort_id="low", default_service_tier_id="default",
            efforts=(EffortOption("low", "low", "low"),), service_tiers=(),
            input_modalities=("text", "image"), upgrade="beta",
            upgrade_info=ModelUpgradeInfo(
                model="beta", upgrade_copy="建议升级", migration_markdown="迁移说明内容",
                retirement_at=1790812800, model_link="https://example.com/models/beta",
            ),
        )
        self.catalog = ModelCatalog((self.model,))

    def render(self, *, config: bool, catalog: ModelCatalog | None, inherit: bool = False):
        if config:
            return config_card(
                scope=self.scope, binding_id="binding-123", short_id="binding",
                project_alias="test", settings_revision=3, context_revision=4,
                feedback_revision=5,
                turn_settings=None if inherit else BindingTurnSettings("alpha", "low", "default"),
                catalog=catalog,
            )
        return new_binding_card(
            scope=self.scope, projects=(Project("test", Path("/tmp/project"), True, 1),),
            initial_project_alias="test", catalog=catalog,
        )

    def test_new_and_config_show_optional_metadata_without_changing_form(self) -> None:
        lean = ModelCatalog((replace(self.model, input_modalities=None, upgrade=None, upgrade_info=None),))
        for config, inherit in ((False, False), (True, False), (True, True)):
            with self.subTest(config=config, inherit=inherit):
                rich_card = self.render(config=config, catalog=self.catalog, inherit=inherit).card
                lean_card = self.render(config=config, catalog=lean, inherit=inherit).card
                self.assertEqual(_elements(rich_card, "form"), _elements(lean_card, "form"))
                panels = _elements(rich_card, "collapsible_panel")
                self.assertTrue(panels)
                self.assertTrue(all(panel["expanded"] is False for panel in panels))
                text = json.dumps(panels, ensure_ascii=False)
                for expected in ("适合日常开发", "文字、图片", "beta", "不会自动切换", "建议升级", "迁移说明内容", "UTC"):
                    self.assertIn(expected, text)
                self.assertEqual(_elements(panels, "button"), [])
                self.assertEqual(_elements(panels, "form"), [])

    def test_unavailable_catalog_has_no_model_info(self) -> None:
        for config in (False, True):
            card = self.render(config=config, catalog=None).card
            self.assertEqual(_elements(card, "collapsible_panel"), [])
            self.assertIn("继承 Codex", json.dumps(card, ensure_ascii=False))

    def test_no_projects_does_not_show_an_unusable_model_picker_or_info(self) -> None:
        card = new_binding_card(scope=self.scope, projects=(), catalog=self.catalog).card
        self.assertEqual(_elements(card, "form"), [])
        self.assertEqual(_elements(card, "collapsible_panel"), [])

    def test_missing_optional_metadata_does_not_invent_capabilities_or_migration(self) -> None:
        model = replace(self.model, description="", input_modalities=None, upgrade=None, upgrade_info=None)
        card = self.render(config=False, catalog=ModelCatalog((model,))).card
        panel = _elements(card, "collapsible_panel")[0]
        text = json.dumps(panel, ensure_ascii=False)
        self.assertIn("未提供简介", text)
        for unexpected in ("支持输入", "建议迁移", "退役时间"):
            self.assertNotIn(unexpected, text)

    def test_upgrade_id_alone_is_displayed_without_switching_model(self) -> None:
        model = replace(self.model, upgrade_info=None)
        card = self.render(config=False, catalog=ModelCatalog((model,))).card
        self.assertIn("beta", json.dumps(_elements(card, "collapsible_panel"), ensure_ascii=False))
        model_select = next(item for item in _elements(card, "select_static") if item["name"] == "new_model")
        self.assertEqual(model_select["initial_option"], model_select["options"][1]["value"])

    def test_catalog_markup_is_inert_plain_text_and_oversized_values_are_bounded(self) -> None:
        markup = '<at id="all"></at> [link](javascript:bad) <button>do it</button>'
        model = replace(self.model, description=markup + "中" * 10000,
            upgrade_info=ModelUpgradeInfo(model="beta", migration_markdown=markup, retirement_at=10**100))
        card = self.render(config=False, catalog=ModelCatalog((model,))).card
        panel = _elements(card, "collapsible_panel")[0]
        self.assertEqual(_elements(panel, "markdown"), [])
        self.assertEqual(_elements(panel, "button"), [])
        rendered = json.dumps(panel, ensure_ascii=False)
        self.assertIn("已省略", rendered)
        self.assertNotIn("退役时间", rendered)
        self.assertLess(len(rendered), 2000)

    def test_large_catalog_bounds_presentation_without_dropping_options(self) -> None:
        catalog = ModelCatalog(tuple(replace(
            self.model, id=f"model-{index}", display_name=f"Model {index}",
            is_default=index == 0, description="中" * 600,
        ) for index in range(60)))
        card = self.render(config=False, catalog=catalog).card
        select = next(item for item in _elements(card, "select_static") if item["name"] == "new_model")
        self.assertEqual(len(select["options"]), 61)
        self.assertLessEqual(len(json.dumps(card, ensure_ascii=False).encode()), TURN_FILE_CARD_JSON_LIMIT_BYTES)
        self.assertIn("部分模型介绍未展示", json.dumps(card, ensure_ascii=False))

    def test_original_near_limit_card_survives_optional_info(self) -> None:
        original = {"body": {"elements": [{"tag": "markdown", "content": "x" * (TURN_FILE_CARD_JSON_LIMIT_BYTES - 80)}]}}
        card = deepcopy(original)
        self.assertEqual(with_model_details(card, self.catalog), original)


if __name__ == "__main__":
    unittest.main()
