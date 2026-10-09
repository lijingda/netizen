"""Render the shared account's single, read-only quota snapshot."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from lark_channel import OutboundCard

from ..account_rate_limits import AccountRateLimitsSnapshot, AccountRateLimitWindow


def account_usage_card(snapshot: AccountRateLimitsSnapshot) -> OutboundCard:
    lines = ["共享账号额度；重置时间按你的客户端时区显示。"]
    buckets = sorted(
        snapshot.buckets,
        key=lambda bucket: (
            (bucket.limit_id or bucket.limit_name or "codex").casefold() != "codex"
            and (bucket.limit_name or "").casefold() != "codex"
        ),
    )
    for bucket in buckets:
        rows = []
        for fallback, window in (("主额度", bucket.primary), ("次额度", bucket.secondary)):
            if window is not None:
                rows.append(_quota_row(
                    _window_label(fallback, window),
                    100 - window.used_percent,
                    window.resets_at,
                ))
        if bucket.credits is not None:
            credits = bucket.credits
            if credits.unlimited:
                rows.append("Credits：无限")
            elif credits.has_credits:
                balance = "可用（余额未提供）" if credits.balance is None else f"{credits.balance:,} credits"
                rows.append(f"Credits：{balance}")
        if bucket.monthly_limit is not None:
            monthly = bucket.monthly_limit
            rows.append(_quota_row(
                "月度 Credits 额度", monthly.remaining_percent, monthly.resets_at,
                details=f"已用 {monthly.used:,} / {monthly.limit:,} credits",
            ))
        if rows:
            name = _escape_label(bucket.limit_name or bucket.limit_id or "Codex")
            lines.extend((f"**{name}**", *rows))
    if len(lines) == 1:
        lines.append("额度数据暂不可用。")
    lines.append("这是本次查询的快照；重置时间不保证任务届时一定可运行。")
    return OutboundCard(card={
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "Codex 账号额度"}},
        "body": {"elements": [{"tag": "markdown", "content": "\n\n".join(lines)}]},
    })


def _window_label(fallback: str, window: AccountRateLimitWindow) -> str:
    minutes = window.window_duration_mins
    if minutes is not None:
        for duration, label in (
            (300, "5 小时额度"), (1440, "每日额度"), (10080, "每周额度"),
            (43200, "每月额度"), (525600, "每年额度"),
        ):
            if duration * 95 <= minutes * 100 <= duration * 105:
                return label
    return fallback


def _quota_row(
    label: str, remaining_percent: int, resets_at: int | None, *, details: str | None = None,
) -> str:
    remaining = max(0, min(100, remaining_percent))
    filled = (remaining + 2) // 5
    bar = "█" * filled + "░" * (20 - filled)
    detail = f" · {details}" if details else ""
    return (
        f"{label}：[{bar}] 剩余 {remaining}%{detail}\n"
        f"重置时间：{_reset_time(resets_at)}"
    )


def _reset_time(resets_at: int | None) -> str:
    if resets_at is None:
        return "未提供"
    try:
        # Validate the epoch without formatting in the service's local timezone.
        datetime.fromtimestamp(resets_at, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "暂不可用"
    timestamp = resets_at * 1000
    return " ".join(
        f"<local_datetime millisecond='{timestamp}' format_type='{format_type}'></local_datetime>"
        for format_type in ("date_num", "time", "timezone")
    )


def _escape_label(value: str) -> str:
    escaped = html.escape(" ".join(value.split()).replace("\\", "\\\\"), quote=False)
    for marker in ("`", "*", "_", "~", "[", "]", "(", ")"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped
