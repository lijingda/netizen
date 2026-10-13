"""Read-only session snapshots, grouped for quick scanning in chat."""

from __future__ import annotations

from typing import Any

from lark_channel import OutboundCard

from .callbacks import _builder, _plain, _plain_text


def session_status_card(
    *,
    short_id: str,
    project_alias: str,
    native_thread_id: str,
    state: str,
    metadata_lines: tuple[str, str],
    git_branch_lines: tuple[str, ...],
    context_window_line: str,
    context_mode_lines: tuple[str, ...],
    model_lines: tuple[str, str, str, str],
    progress_lines: tuple[str, ...],
    subscription_line: str,
) -> OutboundCard:
    builder = _builder("当前会话", metadata_lines[0], template=_state_template(state))
    builder.raw(_columns(f"Project：{project_alias}", f"状态：{state}"))
    builder.raw(_plain(metadata_lines[1]))
    for line in git_branch_lines:
        builder.raw(_plain(line))

    builder.raw({"tag": "hr"})
    builder.markdown("**模型配置**")
    builder.raw(_plain(model_lines[0]))
    builder.raw(_columns(model_lines[1], model_lines[2]))
    builder.raw(_plain(model_lines[3]))

    builder.raw({"tag": "hr"})
    builder.raw(_plain(context_window_line))
    for line in context_mode_lines:
        builder.raw(_plain(line))
    if progress_lines:
        builder.raw({"tag": "hr"})
        builder.markdown("**任务进展**")
        builder.raw(_plain("\n".join(progress_lines[1:])))

    builder.raw({
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {"title": _plain_text("会话详情与订阅")},
        "elements": [_plain("\n".join((
            f"会话：{short_id}",
            f"Native Thread：{native_thread_id}",
            subscription_line,
        )))],
    })
    builder.markdown("<font color='grey'>本次查询快照 · 发送 /status 查看最新状态</font>")
    return OutboundCard(card=builder.to_dict())


def side_status_card(
    *,
    short_id: str,
    parent_short_id: str,
    state: str,
    turn_state: str,
    git_branch_lines: tuple[str, ...],
) -> OutboundCard:
    # A Side's lifecycle and current Turn are distinct observations.
    template_state = turn_state if state == "open" else state
    builder = _builder("当前 Side", f"Side：{short_id}", template=_state_template(template_state))
    builder.raw(_columns(f"状态：{state}", f"当前 Turn：{turn_state}"))
    builder.raw(_plain(f"Parent 会话：{parent_short_id}"))
    for line in git_branch_lines:
        builder.raw(_plain(line))
    builder.raw({"tag": "hr"})
    builder.raw(_plain("空闲 2 小时后自动结束；运行中的 Turn 不计入空闲时间。"))
    builder.markdown("<font color='grey'>本次查询快照 · 发送 /status 查看最新状态</font>")
    return OutboundCard(card=builder.to_dict())


def _columns(*values: str) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey-50",
        "columns": [
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "padding": "8px", "elements": [_plain(value)],
            }
            for value in values
        ],
    }


def _state_template(state: str) -> str:
    if state in {"idle", "open", "goal-complete"}:
        return "green"
    if state in {"running", "goal-starting", "goal-running", "compacting", "creating"}:
        return "blue"
    if state == "failed":
        return "red"
    if state in {"closed", "expired"}:
        return "grey"
    return "orange"
