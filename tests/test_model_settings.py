from __future__ import annotations

import unittest
from enum import Enum
from types import SimpleNamespace

from openai_codex.generated.v2_all import ModelListResponse, ReasoningEffort

from netizen.model_settings import (
    ModelCatalog,
    ModelCatalogError,
    STANDARD_SERVICE_TIER_ID,
)


class Effort(str, Enum):
    LOW = "low"
    ULTRA_FUTURE = "ultra-future"


def effort(value: Effort, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(reasoning_effort=value, description=description)


def tier(identifier: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=identifier, name=name, description=f"{name} description")


def model(
    identifier: str,
    *,
    default: bool,
    efforts: list[SimpleNamespace],
    default_effort: Effort,
    tiers: list[SimpleNamespace] | None = None,
    default_tier: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        model=f"wire-{identifier}",
        display_name=identifier.upper(),
        description=f"{identifier} description",
        is_default=default,
        supported_reasoning_efforts=efforts,
        default_reasoning_effort=default_effort,
        service_tiers=tiers or [],
        default_service_tier=default_tier,
    )


class ModelCatalogTest(unittest.TestCase):
    def test_native_max_and_ultra_follow_each_models_catalog_capabilities(self) -> None:
        response = ModelListResponse.model_validate(
            {
                "data": [
                    {
                        "id": identifier,
                        "model": f"wire-{identifier}",
                        "displayName": identifier.upper(),
                        "description": "Native catalog fixture",
                        "hidden": False,
                        "isDefault": identifier == "alpha",
                        "defaultReasoningEffort": efforts[-1],
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": value, "description": value}
                            for value in efforts
                        ],
                    }
                    for identifier, efforts in (
                        ("alpha", ["low", "max", "ultra"]),
                        ("beta", ["low", "max"]),
                    )
                ]
            }
        )
        catalog = ModelCatalog.from_response(response)
        self.assertEqual(
            [option.id for option in catalog.effort_options], ["low", "max", "ultra"]
        )
        self.assertEqual(catalog.default_model.default_effort_id, "ultra")
        for effort_value in (ReasoningEffort.max, ReasoningEffort.ultra):
            with self.subTest(effort=effort_value):
                selected = catalog.resolve(
                    model_id="alpha",
                    effort_id=effort_value.value,
                    service_tier_id=STANDARD_SERVICE_TIER_ID,
                )
                self.assertIs(selected.effort, effort_value)
        with self.assertRaises(ModelCatalogError):
            catalog.resolve(
                model_id="beta", effort_id="ultra", service_tier_id=STANDARD_SERVICE_TIER_ID
            )

    def test_catalog_preserves_dynamic_efforts_tiers_and_defaults(self) -> None:
        low = effort(Effort.LOW, "low description")
        future = effort(Effort.ULTRA_FUTURE, "future description")
        response = SimpleNamespace(
            data=[
                model(
                    "alpha",
                    default=True,
                    efforts=[low, future],
                    default_effort=Effort.ULTRA_FUTURE,
                    tiers=[tier("priority-v2", "Fast v2")],
                    default_tier="priority-v2",
                ),
                model(
                    "beta",
                    default=False,
                    efforts=[low],
                    default_effort=Effort.LOW,
                ),
            ]
        )

        catalog = ModelCatalog.from_response(response)

        self.assertEqual(catalog.default_model.id, "alpha")
        self.assertEqual(
            [option.id for option in catalog.effort_options],
            ["low", "ultra-future"],
        )
        self.assertEqual(
            [option.id for option in catalog.service_tier_options],
            [STANDARD_SERVICE_TIER_ID, "priority-v2"],
        )
        self.assertEqual(catalog.default_model.default_effort_id, "ultra-future")
        self.assertEqual(
            catalog.default_model.default_service_tier_id,
            "priority-v2",
        )

    def test_resolve_returns_original_effort_wire_value(self) -> None:
        low = effort(Effort.LOW)
        catalog = ModelCatalog.from_response(
            SimpleNamespace(
                data=[
                    model(
                        "alpha",
                        default=True,
                        efforts=[low],
                        default_effort=Effort.LOW,
                        tiers=[tier("priority", "Fast")],
                    )
                ]
            )
        )

        selected = catalog.resolve(
            model_id="alpha",
            effort_id="low",
            service_tier_id="priority",
        )

        self.assertEqual(selected.model, "wire-alpha")
        self.assertIs(selected.effort, Effort.LOW)
        self.assertEqual(selected.service_tier_id, "priority")
        self.assertEqual(selected.service_tier_name, "Fast")

    def test_standard_is_explicitly_selectable_for_every_model(self) -> None:
        catalog = ModelCatalog.from_response(
            SimpleNamespace(
                data=[
                    model(
                        "alpha",
                        default=True,
                        efforts=[effort(Effort.LOW)],
                        default_effort=Effort.LOW,
                    )
                ]
            )
        )

        selected = catalog.resolve(
            model_id="alpha",
            effort_id="low",
            service_tier_id=STANDARD_SERVICE_TIER_ID,
        )

        self.assertEqual(selected.service_tier_id, "default")
        self.assertEqual(selected.service_tier_name, "Standard")
        self.assertEqual(catalog.default_model.default_service_tier_id, "default")

    def test_stale_or_incompatible_form_values_fail_closed(self) -> None:
        catalog = ModelCatalog.from_response(
            SimpleNamespace(
                data=[
                    model(
                        "alpha",
                        default=True,
                        efforts=[effort(Effort.LOW)],
                        default_effort=Effort.LOW,
                    )
                ]
            )
        )

        for kwargs in (
            {
                "model_id": "missing",
                "effort_id": "low",
                "service_tier_id": "default",
            },
            {
                "model_id": "alpha",
                "effort_id": "ultra-future",
                "service_tier_id": "default",
            },
            {
                "model_id": "alpha",
                "effort_id": "low",
                "service_tier_id": "priority",
            },
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ModelCatalogError):
                catalog.resolve(**kwargs)

    def test_malformed_catalog_is_rejected(self) -> None:
        with self.assertRaises(ModelCatalogError):
            ModelCatalog.from_response(SimpleNamespace(data=[]))
        with self.assertRaisesRegex(ModelCatalogError, "仅包含一个默认模型"):
            ModelCatalog.from_response(
                SimpleNamespace(
                    data=[
                        model(
                            "alpha",
                            default=True,
                            efforts=[effort(Effort.LOW)],
                            default_effort=Effort.LOW,
                        ),
                        model(
                            "beta",
                            default=True,
                            efforts=[effort(Effort.LOW)],
                            default_effort=Effort.LOW,
                        ),
                    ]
                )
            )

    def test_paginated_catalog_fails_closed_instead_of_hiding_models(self) -> None:
        response = SimpleNamespace(
            data=[
                model(
                    "alpha",
                    default=True,
                    efforts=[effort(Effort.LOW)],
                    default_effort=Effort.LOW,
                )
            ],
            next_cursor="page-two",
        )

        with self.assertRaisesRegex(ModelCatalogError, "无法读取后续页面"):
            ModelCatalog.from_response(response)


if __name__ == "__main__":
    unittest.main()
