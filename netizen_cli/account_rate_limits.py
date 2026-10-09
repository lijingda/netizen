"""One read-only, removable bridge for native account quota snapshots."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Protocol

from openai_codex import AsyncCodex
from pydantic import ValidationError, field_validator

from .sdk_gap_adapter import (
    SdkFacadeMigrationRequired,
    SdkGapCapabilityUnavailable,
    _generated_type,
    _initialized_client,
    _require_model_fields,
)


_CAPABILITY = "account-rate-limits"
_READ_METHOD = "account/rateLimits/read"
_READ_TIMEOUT_SECONDS = 10.0
_MAX_BUCKETS = 64
_MAX_LABEL_LENGTH = 256


class AccountRateLimitsUnavailable(RuntimeError):
    """The response cannot supply a trustworthy quota projection."""


@dataclass(frozen=True, slots=True)
class AccountRateLimitWindow:
    used_percent: int
    window_duration_mins: int | None
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class AccountCredits:
    has_credits: bool
    unlimited: bool
    balance: int | None


@dataclass(frozen=True, slots=True)
class AccountMonthlyCreditLimit:
    limit: int
    used: int
    remaining_percent: int
    resets_at: int


@dataclass(frozen=True, slots=True)
class AccountRateLimitBucket:
    limit_id: str | None
    limit_name: str | None
    primary: AccountRateLimitWindow | None
    secondary: AccountRateLimitWindow | None
    credits: AccountCredits | None = None
    monthly_limit: AccountMonthlyCreditLimit | None = None


@dataclass(frozen=True, slots=True)
class AccountRateLimitsSnapshot:
    buckets: tuple[AccountRateLimitBucket, ...]


class AccountRateLimits(Protocol):
    async def read(self) -> AccountRateLimitsSnapshot: ...


def facade_migration_requirements() -> tuple[str, ...]:
    """Require review when a candidate public account-quota facade appears."""

    candidates = (
        "rate_limits",
        "rate_limits_read",
        "account_rate_limits",
        "account_rate_limits_read",
        "get_account_rate_limits",
        "read_account_rate_limits",
    )
    names = tuple(
        f"AsyncCodex.{name}" for name in candidates if hasattr(AsyncCodex, name)
    )
    return (
        (f"migration-required:{_CAPABILITY}:{','.join(names)}",) if names else ()
    )


class AppServerAccountRateLimits:
    """Read once through the same initialized SDK-owned client.

    Timeout/cancellation stops waiting, not the SDK's underlying sync request.
    This bridge never retries, resets quota, consumes notifications, or closes
    the shared client. Late results remain owned by the SDK transport.
    """

    __slots__ = ("_client", "_params_model", "_response_model", "_bucket_model")

    def __init__(self, codex: AsyncCodex) -> None:
        if requirements := facade_migration_requirements():
            raise SdkFacadeMigrationRequired("; ".join(requirements))
        self._client = _initialized_client(codex, capability=_CAPABILITY)
        self._params_model = _model(
            "GetAccountRateLimitsParams",
            {
                "exclude_reset_credit_details": "excludeResetCreditDetails",
                "supports_luna_reserve": "supportsLunaReserve",
            },
        )
        self._response_model = _model(
            "GetAccountRateLimitsResponse",
            {
                "account_id": "accountId",
                "ordinary_usage_allowed": "ordinaryUsageAllowed",
                "rate_limit_reset_credits": "rateLimitResetCredits",
                "rate_limit_upsell": "rateLimitUpsell",
                "rate_limits": "rateLimits",
                "rate_limits_by_limit_id": "rateLimitsByLimitId",
            },
        )
        self._bucket_model = _model(
            "RateLimitSnapshot",
            {
                "credits": "credits",
                "individual_limit": "individualLimit",
                "limit_id": "limitId",
                "limit_name": "limitName",
                "normal_model_slug": "normalModelSlug",
                "plan_type": "planType",
                "primary": "primary",
                "rate_limit_reached_type": "rateLimitReachedType",
                "secondary": "secondary",
                "spend_control_reached": "spendControlReached",
            },
        )
        _model(
            "RateLimitWindow",
            {
                "resets_at": "resetsAt",
                "used_percent": "usedPercent",
                "window_duration_mins": "windowDurationMins",
            },
        )
        _model(
            "CreditsSnapshot",
            {
                "balance": "balance",
                "has_credits": "hasCredits",
                "unlimited": "unlimited",
            },
        )
        _model(
            "SpendControlLimitSnapshot",
            {
                "limit": "limit",
                "used": "used",
                "remaining_percent": "remainingPercent",
                "resets_at": "resetsAt",
            },
        )
        self._response_model = _strict_response_model(
            self._response_model, self._bucket_model
        )

    async def read(self) -> AccountRateLimitsSnapshot:
        params = self._params_model(
            excludeResetCreditDetails=True, supportsLunaReserve=False
        )
        try:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                response = await self._client.request(
                    _READ_METHOD,
                    params.model_dump(by_alias=True),
                    response_model=self._response_model,
                )
        except ValidationError as error:
            raise AccountRateLimitsUnavailable("Codex 额度响应格式无效。") from error
        if not isinstance(response, self._response_model):
            raise AccountRateLimitsUnavailable("Codex 额度响应类型无效。")
        multiple = response.rate_limits_by_limit_id
        if multiple is None:
            buckets = (self._project_bucket(response.rate_limits),)
        else:
            if not isinstance(multiple, dict) or not 0 < len(multiple) <= _MAX_BUCKETS:
                raise AccountRateLimitsUnavailable("Codex 额度分组为空或超出读取范围。")
            projected = []
            for key, raw in multiple.items():
                limit_id = _label(key, required=True)
                if not isinstance(raw, dict):
                    raise AccountRateLimitsUnavailable("Codex 额度分组格式无效。")
                try:
                    # JSON strict validation preserves valid string enums while
                    # rejecting coercion of window strings/bools into numbers.
                    bucket = self._bucket_model.model_validate_json(
                        json.dumps(raw, allow_nan=False), strict=True
                    )
                except (ValidationError, TypeError, ValueError) as error:
                    raise AccountRateLimitsUnavailable(
                        "Codex 额度分组格式无效。"
                    ) from error
                projected.append(self._project_bucket(bucket, limit_id=limit_id))
            buckets = tuple(projected)
        if not any(
            bucket.primary is not None or bucket.secondary is not None
            or bucket.credits is not None or bucket.monthly_limit is not None
            for bucket in buckets
        ):
            raise AccountRateLimitsUnavailable("Codex 尚未提供可用的额度数据。")
        return AccountRateLimitsSnapshot(buckets)

    def _project_bucket(
        self, bucket: Any, *, limit_id: str | None = None
    ) -> AccountRateLimitBucket:
        if not isinstance(bucket, self._bucket_model):
            raise AccountRateLimitsUnavailable("Codex 额度分组格式无效。")
        supplied_id = _label(bucket.limit_id)
        if limit_id is not None and supplied_id is not None and limit_id != supplied_id:
            raise AccountRateLimitsUnavailable("Codex 额度分组标识不一致。")
        return AccountRateLimitBucket(
            limit_id=limit_id if limit_id is not None else supplied_id,
            limit_name=_label(bucket.limit_name),
            primary=_window(bucket.primary),
            secondary=_window(bucket.secondary),
            credits=_credits(bucket.credits),
            monthly_limit=_monthly_limit(bucket.individual_limit),
        )


def _model(name: str, aliases: dict[str, str]) -> type[Any]:
    model = _generated_type(name, capability=_CAPABILITY)
    _require_model_fields(model, capability=_CAPABILITY, aliases=aliases)
    if not callable(getattr(model, "model_validate_json", None)):
        raise SdkGapCapabilityUnavailable(f"{_CAPABILITY} model validation changed")
    return model


def _strict_response_model(
    response_model: type[Any], bucket_model: type[Any]
) -> type[Any]:
    # Inherit every field from the installed SDK. Its request decoder calls
    # model_validate on the raw response, so validate legacy windows before
    # Pydantic can coerce false, "0", or 0.0 into a known zero percent.
    class StrictAccountRateLimitsResponse(response_model):
        @field_validator("rate_limits", mode="before")
        @classmethod
        def _strict_legacy_bucket(cls, value: Any) -> Any:
            if not isinstance(value, dict):
                raise ValueError("expected a quota bucket object")
            try:
                return bucket_model.model_validate_json(
                    json.dumps(value, allow_nan=False), strict=True
                )
            except TypeError as error:
                raise ValueError("invalid quota bucket object") from error

    return StrictAccountRateLimitsResponse


def _label(value: Any, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > _MAX_LABEL_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise AccountRateLimitsUnavailable("Codex 额度分组名称或标识无效。")
    return value


def _window(value: Any) -> AccountRateLimitWindow | None:
    if value is None:
        return None
    used = _nonnegative_int(value.used_percent)
    duration = (
        None if value.window_duration_mins is None
        else _nonnegative_int(value.window_duration_mins)
    )
    resets = None if value.resets_at is None else _nonnegative_int(value.resets_at)
    if duration == 0:
        raise AccountRateLimitsUnavailable("Codex 额度窗口时长无效。")
    # Native i32 percentages can exceed 100; do not invent a capped usage value.
    return AccountRateLimitWindow(used, duration, resets)


def _credits(value: Any) -> AccountCredits | None:
    if value is None or not (value.unlimited or value.has_credits):
        return None
    # The native display treats a hidden or invalid balance as Available; it
    # never exposes the raw string or infers requests from credit balances.
    balance = _credit_amount(value.balance, positive=True)
    return AccountCredits(value.has_credits, value.unlimited, balance)


def _monthly_limit(value: Any) -> AccountMonthlyCreditLimit | None:
    if value is None:
        return None
    limit, used = _credit_amount(value.limit), _credit_amount(value.used)
    if limit is None or used is None:
        return None
    # Native remainingPercent can fall outside the display range; clamp only
    # while rendering, independently of the reported used/limit amounts.
    remaining = value.remaining_percent
    if isinstance(remaining, bool) or not isinstance(remaining, int):
        raise AccountRateLimitsUnavailable("Codex 月度额度数值无效。")
    return AccountMonthlyCreditLimit(
        limit, used, remaining, _nonnegative_int(value.resets_at)
    )


def _credit_amount(value: str | None, *, positive: bool = False) -> int | None:
    if value is None:
        return None
    raw = value.strip()
    if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw) is None:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if (
        not amount.is_finite() or amount < 0 or (positive and amount == 0)
        or amount > 2**63 - 1
    ):
        return None
    return int(amount.to_integral_value(rounding=ROUND_HALF_UP))


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccountRateLimitsUnavailable("Codex 额度窗口数值无效。")
    return value
