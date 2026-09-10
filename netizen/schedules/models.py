"""Immutable scheduling facts and the shared wall-clock rule calculator."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..session_settings import SessionSettings


class ScheduleError(ValueError):
    code = "invalid_schedule"


class ScheduleNotFound(ScheduleError):
    code = "schedule_not_found"


class ScheduleConflict(ScheduleError):
    code = "schedule_conflict"


class ScheduleRevisionConflict(ScheduleConflict):
    code = "revision_conflict"


class ScheduleRequestConflict(ScheduleConflict):
    code = "request_conflict"


class AmbiguousLocalTime(ScheduleError):
    code = "ambiguous_local_time"

    def __init__(self, choices: tuple[dict[str, str], ...]) -> None:
        super().__init__("所选时间因夏令时回拨会出现两次，请选择对应的 UTC 偏移。")
        self.choices = choices


def resolve_once_local(
    local_at: str, timezone: str, *, original_at: str | None = None,
    utc_offset: str | None = None,
) -> str:
    """Resolve a minute-precision picker value without guessing a DST fold."""
    try:
        if not isinstance(local_at, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::00)?", local_at,
        ):
            raise ValueError("invalid local datetime")
        zone = ZoneInfo(timezone)
        local = datetime.fromisoformat(local_at)
        moments = []
        for fold in (0, 1):
            moment = local.replace(tzinfo=zone, fold=fold)
            if datetime.fromtimestamp(moment.timestamp(), zone).replace(tzinfo=None) != local:
                continue
            if not moments or moment.timestamp() != moments[0].timestamp():
                moments.append(moment)
        if not moments:
            raise ScheduleError("该时区在这一天没有所选时间（夏令时跳时），请选择其他时间。")
        choices = tuple({
            "utc_offset": moment.isoformat(timespec="minutes")[16:],
            "utc": moment.astimezone(UTC).isoformat(timespec="minutes"),
        } for moment in moments)
        if utc_offset is not None:
            for moment, choice in zip(moments, choices, strict=True):
                if choice["utc_offset"] == utc_offset:
                    return moment.isoformat(timespec="minutes")
            raise ScheduleError("所选 UTC 偏移不适用于该日期和时区，请重新预览。")
        if original_at is not None:
            original = datetime.fromisoformat(original_at)
            if original.tzinfo is not None and original.astimezone(zone).replace(tzinfo=None) == local:
                return original.isoformat(timespec="minutes")
        if len(moments) > 1:
            raise AmbiguousLocalTime(choices)
        return moments[0].isoformat(timespec="minutes")
    except ScheduleError:
        raise
    except (TypeError, ValueError, ZoneInfoNotFoundError, OSError, OverflowError) as error:
        raise ScheduleError("请检查执行日期与时区。") from error


@dataclass(frozen=True, slots=True)
class ScheduleRule:
    kind: str
    timezone: str
    at: str | None = None
    weekdays: tuple[int, ...] = ()
    every_minutes: int | None = None
    anchor: float | None = None
    end_at: str | None = None

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError, OSError) as error:
            raise ScheduleError("请选择有效的 IANA 时区。") from error
        if not isinstance(self.kind, str) or self.kind not in {"once", "daily", "weekly", "interval"}:
            raise ScheduleError("仅支持一次性、每天、每周和固定间隔。")
        if self.kind == "once" and self.end_at is not None:
            raise ScheduleError("一次性计划不能设置截止时间。")
        if self.end_at is not None:
            try:
                if not isinstance(self.end_at, str):
                    raise ValueError("invalid end time")
                end = datetime.fromisoformat(self.end_at)
                if end.tzinfo is None or end.second or end.microsecond:
                    raise ValueError("end time must be absolute and minute-precision")
                datetime.fromtimestamp(end.timestamp(), UTC)
                datetime.fromtimestamp(end.timestamp(), ZoneInfo(self.timezone))
                object.__setattr__(self, "end_at", end.isoformat(timespec="minutes"))
            except (TypeError, ValueError, OverflowError, OSError) as error:
                raise ScheduleError("截止时间必须是有效的带偏移 ISO 时间，精度为分钟。") from error
        if not isinstance(self.weekdays, (tuple, list)):
            raise ScheduleError("weekdays 必须是星期列表。")
        object.__setattr__(self, "weekdays", tuple(self.weekdays))
        if self.kind == "interval":
            if (
                type(self.every_minutes) is not int or self.every_minutes < 1
                or type(self.anchor) not in {int, float}
                or self.at is not None or self.weekdays
            ):
                raise ScheduleError("固定间隔需要正整数分钟和 UTC anchor。")
            try:
                if not math.isfinite(self.anchor):
                    raise ScheduleError("固定间隔需要有限的 UTC anchor。")
                datetime.fromtimestamp(self.anchor, UTC)
                datetime.fromtimestamp(self.anchor + self.every_minutes * 60, UTC)
            except (ValueError, OverflowError, OSError) as error:
                raise ScheduleError("固定间隔超出支持的日期范围。") from error
            return
        if self.every_minutes is not None or self.anchor is not None:
            raise ScheduleError("只有固定间隔可以指定间隔和 anchor。")
        if self.kind == "once":
            if not isinstance(self.at, str):
                raise ScheduleError("一次性时间必须是带偏移的 ISO 时间。")
            try:
                moment = datetime.fromisoformat(self.at)
            except (ValueError, TypeError) as error:
                raise ScheduleError("一次性时间必须是带偏移的 ISO 时间。") from error
            if moment.tzinfo is None or moment.second or moment.microsecond or self.weekdays:
                raise ScheduleError("一次性时间须明确偏移，精度为分钟。")
            try:
                datetime.fromtimestamp(moment.timestamp(), UTC)
                datetime.fromtimestamp(moment.timestamp(), ZoneInfo(self.timezone))
            except (ValueError, OverflowError, OSError) as error:
                raise ScheduleError("一次性时间超出支持的日期范围。") from error
            object.__setattr__(self, "at", moment.isoformat(timespec="minutes"))
            return
        if not isinstance(self.at, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.at):
            raise ScheduleError("每天/每周时间格式为 HH:mm。")
        if self.kind == "weekly":
            if not self.weekdays or any(type(day) is not int or not 0 <= day <= 6 for day in self.weekdays):
                raise ScheduleError("每周需要非空星期集合，周一为 0、周日为 6。")
            object.__setattr__(self, "weekdays", tuple(sorted(set(self.weekdays))))
        elif self.weekdays:
            raise ScheduleError("只有每周规则可以指定星期。")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ScheduleRule:
        if not isinstance(value, dict) or set(value) - {"kind", "timezone", "at", "weekdays", "every_minutes", "anchor", "end_at"}:
            raise ScheduleError("时间规则包含不支持的字段。")
        fields = dict(value)
        if "weekdays" in fields:
            if not isinstance(fields["weekdays"], (list, tuple)):
                raise ScheduleError("weekdays 必须是星期列表。")
            fields["weekdays"] = tuple(fields["weekdays"])
        try:
            return cls(**fields)
        except TypeError as error:
            raise ScheduleError("时间规则缺少类型或时区。") from error

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "timezone": self.timezone}
        if self.at is not None:
            result["at"] = self.at
        if self.kind == "weekly":
            result["weekdays"] = list(self.weekdays)
        if self.kind == "interval":
            result.update(every_minutes=self.every_minutes, anchor=self.anchor)
        if self.end_at is not None:
            result["end_at"] = self.end_at
        return result

    def _local_occurrence(self, date: datetime) -> float | None:
        if self.kind == "weekly" and date.weekday() not in self.weekdays:
            return None
        hour, minute = map(int, (self.at or "").split(":"))
        local = date.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
        stamp = local.timestamp()
        roundtrip = datetime.fromtimestamp(stamp, ZoneInfo(self.timezone))
        # A nonexistent wall time normalizes to a different clock reading.
        if roundtrip.replace(tzinfo=None) != local.replace(tzinfo=None):
            return None
        return stamp

    def next_after(self, after: float) -> float | None:
        """Return the first occurrence strictly after an absolute UTC boundary."""
        try:
            end = datetime.fromisoformat(self.end_at).timestamp() if self.end_at is not None else None
            if end is not None and after >= end:
                return None
            result = self._next_after(after)
            if result is not None and end is not None and result > end:
                return None
            if result is not None:
                # Both projections are part of the common preview contract.
                datetime.fromtimestamp(result, UTC)
                datetime.fromtimestamp(result, ZoneInfo(self.timezone))
            return result
        except ScheduleError:
            raise
        except (TypeError, ValueError, OverflowError, OSError) as error:
            raise ScheduleError("计划时间超出支持的日期范围。") from error

    def _next_after(self, after: float) -> float | None:
        if not math.isfinite(after):
            raise ScheduleError("时间边界必须是有限 UTC 时间。")
        if self.kind == "once":
            stamp = datetime.fromisoformat(self.at or "").timestamp()
            return stamp if stamp > after else None
        if self.kind == "interval":
            assert self.anchor is not None and self.every_minutes is not None
            interval = self.every_minutes * 60
            index = max(1, math.floor((after - self.anchor) / interval) + 1)
            return self.anchor + index * interval
        date = datetime.fromtimestamp(after, ZoneInfo(self.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        # At most a skipped DST day plus the next selected weekday.
        for _ in range(16):
            stamp = self._local_occurrence(date)
            if stamp is not None and stamp > after:
                return stamp
            date += timedelta(days=1)
        raise ScheduleError("无法计算下一次当地时间。")

    def preview(self, after: float, count: int = 3) -> tuple[float, ...]:
        if type(count) is not int or not 1 <= count <= 100:
            raise ScheduleError("预览数量必须在 1 到 100 之间。")
        result = []
        for _ in range(count):
            following = self.next_after(after)
            if following is None:
                break
            result.append(following)
            after = following
        return tuple(result)

    def through(self, first: float, until: float) -> tuple[float, int]:
        """Last occurrence and count in [first, until], without minute iteration."""
        if self.end_at is not None:
            until = min(until, datetime.fromisoformat(self.end_at).timestamp())
        if until < first:
            return first, 0
        if self.kind == "once":
            return first, 1
        if self.kind == "interval":
            assert self.every_minutes is not None
            count = math.floor((until - first) / (self.every_minutes * 60)) + 1
            return first + (count - 1) * self.every_minutes * 60, count
        last, count = first, 1
        while (following := self.next_after(last)) is not None and following <= until:
            last, count = following, count + 1
        return last, count


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    revision: int
    name: str
    instructions: str
    project_alias: str
    app_id: str
    chat_id: str
    schedule: ScheduleRule | None
    enabled: bool
    next_due_at: float | None
    processed_through: float | None
    created_at: float
    updated_at: float
    source: str
    deleted: bool = False
    session_settings: SessionSettings = SessionSettings()


@dataclass(frozen=True, slots=True)
class PlanLifecycle:
    ended: bool
    has_future: bool
    has_trigger: bool

    @property
    def can_toggle(self) -> bool:
        return self.has_trigger


def _trigger_opportunities(
    rule: ScheduleRule | None, *, enabled: bool, next_due_at: float | None,
    processed_through: float | None, now: float,
) -> tuple[bool, bool]:
    if rule is None:
        return False, False
    boundary = max(now, processed_through) if processed_through is not None else now
    has_future = rule.next_after(boundary) is not None
    # A due point remains claimable through the scheduler's inclusive 60-second
    # grace period. Pausing removes that opportunity, and a clock rollback must
    # never revive a point already covered by the processed high-water mark.
    claimable_due = (
        enabled and next_due_at is not None
        and (processed_through is None or next_due_at > processed_through)
        and (rule.end_at is None or next_due_at <= datetime.fromisoformat(rule.end_at).timestamp())
        and 0 <= now - next_due_at <= 60
    )
    return has_future, has_future or claimable_due


def has_trigger_opportunity(
    rule: ScheduleRule | None, *, enabled: bool, next_due_at: float | None,
    processed_through: float | None, now: float,
) -> bool:
    """Whether the current rule has a future or still-claimable occurrence."""
    return _trigger_opportunities(
        rule, enabled=enabled, next_due_at=next_due_at,
        processed_through=processed_through, now=now,
    )[1]


def plan_lifecycle(plan: Plan, *, now: float, has_pending: bool) -> PlanLifecycle:
    """Project lifecycle separately from enablement and native execution state."""
    has_future, has_trigger = _trigger_opportunities(
        plan.schedule, enabled=plan.enabled, next_due_at=plan.next_due_at,
        processed_through=plan.processed_through, now=now,
    )
    return PlanLifecycle(
        ended=not has_trigger and not has_pending,
        has_future=has_future, has_trigger=has_trigger,
    )


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    plan_id: str
    plan_revision: int
    due_at: float
    project_alias: str
    app_id: str
    chat_id: str
    phase: str
    barrier: str
    root_uuid: str
    seed_uuid: str
    root_message_id: str | None
    topic_id: str | None
    origin_message_id: str | None
    binding_id: str | None
    initial_turn_id: str | None
    error_code: str | None
    delivery_state: str | None
    created_at: float
    updated_at: float
    missed_from: float | None = None
    missed_count: int = 0
    binding_removed: bool = False


@dataclass(frozen=True, slots=True)
class Claim:
    run: Run
    plan: Plan


@dataclass(frozen=True, slots=True)
class MutationResult:
    plan_id: str
    revision: int
    replayed: bool = False
