"""Immutable session configuration intent shared by Bindings and plans."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .domain import MentionContextMode

if TYPE_CHECKING:
    from .bindings import ThreadBinding
    from .model_settings import ModelCatalog


class SessionSettingsError(ValueError):
    code = "invalid_session_settings"


@dataclass(frozen=True, slots=True)
class BindingTurnSettings:
    """Catalog selection to apply to every new Turn started by Netizen."""

    model_id: str
    effort_id: str
    service_tier_id: str

    def __post_init__(self) -> None:
        values = (self.model_id, self.effort_id, self.service_tier_id)
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("Binding Turn settings IDs must not be empty")


@dataclass(frozen=True, slots=True)
class BindingTaskFeedback:
    """Binding-scoped, opt-in pulse/card feedback for Turns."""

    reaction_pulse_enabled: bool = False
    progress_card_enabled: bool = False

    def __post_init__(self) -> None:
        values = (self.reaction_pulse_enabled, self.progress_card_enabled)
        if not all(type(value) is bool for value in values):
            raise ValueError("Binding task feedback values must be booleans")


_FIELDS = frozenset({"turn_settings", "reaction_pulse_enabled", "progress_card_enabled", "message_context_mode"})
_TURN_FIELDS = frozenset({"model_id", "effort_id", "service_tier_id"})


@dataclass(frozen=True, slots=True)
class SessionSettings:
    turn_settings: BindingTurnSettings | None = None
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY

    def __post_init__(self) -> None:
        if self.turn_settings is not None and not isinstance(self.turn_settings, BindingTurnSettings):
            raise SessionSettingsError("turn_settings 必须是完整模型设置或 null。")
        if not isinstance(self.task_feedback, BindingTaskFeedback):
            raise SessionSettingsError("执行反馈必须是明确的布尔设置。")
        if not isinstance(self.message_context_mode, MentionContextMode):
            raise SessionSettingsError("消息上下文必须是 current-only 或 catch-up。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_settings": asdict(self.turn_settings) if self.turn_settings else None,
            "reaction_pulse_enabled": self.task_feedback.reaction_pulse_enabled,
            "progress_card_enabled": self.task_feedback.progress_card_enabled,
            "message_context_mode": self.message_context_mode.value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionSettings:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise SessionSettingsError("会话设置必须包含全部四个配置字段，且不能包含其他字段。")
        raw_turn = value["turn_settings"]
        turn = None
        if raw_turn is not None:
            if not isinstance(raw_turn, dict) or set(raw_turn) != _TURN_FIELDS:
                raise SessionSettingsError("模型设置需要 model_id、effort_id、service_tier_id，或用 null 继承 Codex。")
            try:
                turn = BindingTurnSettings(**raw_turn)
            except ValueError as error:
                raise SessionSettingsError("模型、思考强度和速度 ID 必须是非空字符串。") from error
        try:
            feedback = BindingTaskFeedback(value["reaction_pulse_enabled"], value["progress_card_enabled"])
        except ValueError as error:
            raise SessionSettingsError("Reaction Pulse 和 Progress Card 必须是布尔值。") from error
        try:
            mode = MentionContextMode(value["message_context_mode"])
        except (ValueError, TypeError) as error:
            raise SessionSettingsError("消息上下文必须是 current-only 或 catch-up。") from error
        return cls(turn, feedback, mode)

    def merge(self, partial: dict[str, Any]) -> SessionSettings:
        if not isinstance(partial, dict) or set(partial) - _FIELDS:
            raise SessionSettingsError("会话设置更新必须是仅包含支持字段的对象。")
        return self.from_dict({**self.to_dict(), **partial})

    @classmethod
    def from_binding(cls, binding: ThreadBinding) -> SessionSettings:
        # All nested objects are immutable. Do not copy the Binding's context
        # anchor: each new session must establish its own message boundary.
        return cls(binding.turn_settings, binding.task_feedback, binding.message_context_mode)

    @classmethod
    def new_defaults(cls, catalog: ModelCatalog | None) -> SessionSettings:
        if catalog is None:
            return cls()
        model = catalog.default_model
        return cls(turn_settings=BindingTurnSettings(
            model.id, model.default_effort_id, model.default_service_tier_id,
        ))

    def validate_catalog(self, catalog: ModelCatalog) -> None:
        if self.turn_settings is not None:
            catalog.resolve(
                model_id=self.turn_settings.model_id,
                effort_id=self.turn_settings.effort_id,
                service_tier_id=self.turn_settings.service_tier_id,
            )
