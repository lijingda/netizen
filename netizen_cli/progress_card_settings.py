"""Immutable presentation choices for the existing progress card content."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ProgressCardSettings:
    show_commentary: bool = True
    show_operations: bool = True
    show_plan: bool = True
    commentary_limit: int = 4
    operation_limit: int = 8
    commentary_max_chars: int = 160
    operation_max_chars: int = 120
    organization: Literal["content", "time"] = "content"

    def __post_init__(self) -> None:
        if not all(type(value) is bool for value in (
            self.show_commentary, self.show_operations, self.show_plan,
        )):
            raise ValueError("过程卡内容开关必须是布尔值。")
        for name, maximum in (
            ("commentary_limit", 8), ("operation_limit", 16),
            ("commentary_max_chars", 320), ("operation_max_chars", 240),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} 必须是 1 到 {maximum} 之间的整数。")
        if self.organization not in ("content", "time"):
            raise ValueError("过程卡组织方式必须是 content 或 time。")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProgressCardSettings:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("过程卡设置必须包含全部八个配置字段，且不能包含其他字段。")
        return cls(**value)
