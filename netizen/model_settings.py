"""Model-catalog projection and native Turn setting validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# App Server uses this protocol-level request value to explicitly select
# Standard processing instead of inheriting a previous accelerated tier.  It
# is not a model capability; accelerated/alternative tiers still come only
# from ``codex.models()``.  Keep this aligned with the exact release source:
# https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/protocol/src/config_types.rs
STANDARD_SERVICE_TIER_ID = "default"


class ModelCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EffortOption:
    id: str
    description: str
    wire_value: object


@dataclass(frozen=True, slots=True)
class ServiceTierOption:
    id: str
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class ModelOption:
    id: str
    model: str
    display_name: str
    description: str
    is_default: bool
    default_effort_id: str
    default_service_tier_id: str
    efforts: tuple[EffortOption, ...]
    service_tiers: tuple[ServiceTierOption, ...]


@dataclass(frozen=True, slots=True)
class TurnModelSettings:
    """Validated wire values for one native ``turn/start`` override."""

    model_id: str
    model: str
    effort_id: str
    effort: object
    service_tier_id: str
    service_tier_name: str


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    models: tuple[ModelOption, ...]

    @classmethod
    def from_response(cls, response: object) -> ModelCatalog:
        if getattr(response, "next_cursor", None) is not None:
            # The pinned public ``models()`` facade has no cursor parameter.
            # Treat a paginated response as incomplete instead of silently
            # presenting only the first page as the full native catalog.
            raise ModelCatalogError(
                "Codex 模型目录需要分页，但当前高层 SDK 无法读取后续页面。"
            )
        raw_models = getattr(response, "data", None)
        if not isinstance(raw_models, list) or not raw_models:
            raise ModelCatalogError("Codex 没有返回可用模型。")

        models = tuple(_model_option(raw) for raw in raw_models)
        ids = [model.id for model in models]
        if len(ids) != len(set(ids)):
            raise ModelCatalogError("Codex 模型目录包含重复 ID。")
        defaults = [model for model in models if model.is_default]
        if len(defaults) != 1:
            raise ModelCatalogError("Codex 模型目录必须包含且仅包含一个默认模型。")
        return cls(models)

    @property
    def default_model(self) -> ModelOption:
        return next(model for model in self.models if model.is_default)

    @property
    def effort_options(self) -> tuple[EffortOption, ...]:
        seen: set[str] = set()
        options: list[EffortOption] = []
        for model in self.models:
            for option in model.efforts:
                if option.id in seen:
                    continue
                seen.add(option.id)
                options.append(option)
        return tuple(options)

    @property
    def service_tier_options(self) -> tuple[ServiceTierOption, ...]:
        options = [
            ServiceTierOption(
                STANDARD_SERVICE_TIER_ID,
                "Standard",
                "Codex 标准服务层",
            )
        ]
        seen = {STANDARD_SERVICE_TIER_ID}
        for model in self.models:
            for option in model.service_tiers:
                if option.id in seen:
                    continue
                seen.add(option.id)
                options.append(option)
        return tuple(options)

    def resolve(
        self,
        *,
        model_id: str,
        effort_id: str,
        service_tier_id: str,
    ) -> TurnModelSettings:
        model = next((item for item in self.models if item.id == model_id), None)
        if model is None:
            raise ModelCatalogError("所选 Model 已不可用，请重新打开卡片。")
        effort = next((item for item in model.efforts if item.id == effort_id), None)
        if effort is None:
            raise ModelCatalogError(
                f"Model {model.display_name} 不支持 Effort {effort_id}，"
                "请重新选择。"
            )
        if service_tier_id == STANDARD_SERVICE_TIER_ID:
            service_tier_name = "Standard"
        else:
            service_tier = next(
                (
                    item
                    for item in model.service_tiers
                    if item.id == service_tier_id
                ),
                None,
            )
            if service_tier is None:
                raise ModelCatalogError(
                    f"Model {model.display_name} 不支持 Speed {service_tier_id}，"
                    "请重新选择。"
                )
            service_tier_name = service_tier.name
        return TurnModelSettings(
            model_id=model.id,
            model=model.model,
            effort_id=effort.id,
            effort=effort.wire_value,
            service_tier_id=service_tier_id,
            service_tier_name=service_tier_name,
        )


def _model_option(raw: Any) -> ModelOption:
    model_id = _nonempty_string(getattr(raw, "id", None), "model.id")
    model = _nonempty_string(getattr(raw, "model", None), "model.model")
    display_name = _nonempty_string(
        getattr(raw, "display_name", None),
        "model.display_name",
    )
    description = str(getattr(raw, "description", "") or "")

    efforts = tuple(
        _effort_option(option)
        for option in (getattr(raw, "supported_reasoning_efforts", None) or [])
    )
    if not efforts:
        raise ModelCatalogError(f"Model {display_name} 没有可用 Effort。")
    effort_ids = [option.id for option in efforts]
    if len(effort_ids) != len(set(effort_ids)):
        raise ModelCatalogError(f"Model {display_name} 包含重复 Effort。")

    default_effort_id = _enum_value(
        getattr(raw, "default_reasoning_effort", None),
        "model.default_reasoning_effort",
    )
    if default_effort_id not in set(effort_ids):
        raise ModelCatalogError(
            f"Model {display_name} 的默认 Effort 不在支持列表中。"
        )

    tiers = tuple(
        _service_tier_option(option)
        for option in (getattr(raw, "service_tiers", None) or [])
    )
    tier_ids = [option.id for option in tiers]
    if len(tier_ids) != len(set(tier_ids)):
        raise ModelCatalogError(f"Model {display_name} 包含重复 Service Tier。")
    default_tier = getattr(raw, "default_service_tier", None)
    if default_tier is None:
        default_tier_id = STANDARD_SERVICE_TIER_ID
    else:
        default_tier_id = _nonempty_string(
            default_tier,
            "model.default_service_tier",
        )
        if default_tier_id not in set(tier_ids):
            raise ModelCatalogError(
                f"Model {display_name} 的默认 Service Tier 不在支持列表中。"
            )

    return ModelOption(
        id=model_id,
        model=model,
        display_name=display_name,
        description=description,
        is_default=bool(getattr(raw, "is_default", False)),
        default_effort_id=default_effort_id,
        default_service_tier_id=default_tier_id,
        efforts=efforts,
        service_tiers=tiers,
    )


def _effort_option(raw: Any) -> EffortOption:
    wire_value = getattr(raw, "reasoning_effort", None)
    return EffortOption(
        id=_enum_value(wire_value, "reasoning_effort"),
        description=str(getattr(raw, "description", "") or ""),
        wire_value=wire_value,
    )


def _service_tier_option(raw: Any) -> ServiceTierOption:
    return ServiceTierOption(
        id=_nonempty_string(getattr(raw, "id", None), "service_tier.id"),
        name=_nonempty_string(getattr(raw, "name", None), "service_tier.name"),
        description=str(getattr(raw, "description", "") or ""),
    )


def _enum_value(value: Any, field: str) -> str:
    return _nonempty_string(getattr(value, "value", value), field)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelCatalogError(f"Codex 模型目录字段 {field} 无效。")
    return value
