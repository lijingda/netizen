"""Session snapshots with a closed set of session-bound command aliases."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lark_channel import OutboundCard

from ..domain import ControlIntent, ControlName, FeishuScope
from .callbacks import (
    CardActionError, _builder, _button_row, _repeatable_callback_button, _plain,
    _plain_text, _required_string,
)
from .session_ids import session_identity_lines


_STATUS_COMMAND = "status.command"
_STATUS_CARD_MAX_BYTES = 55_000
_BINDING_COMMANDS = (
    ("查看状态", ControlName.STATUS, ()),
    ("停止", ControlName.STOP, ()),
    ("会话配置", ControlName.CONFIG, ()),
    ("会话列表", ControlName.SESSIONS, ()),
)
_SIDE_COMMANDS = (
    ("查看状态", ControlName.STATUS, ()),
    ("停止", ControlName.STOP, ()),
    ("结束临时会话", ControlName.SIDE, ("close",)),
)


@dataclass(frozen=True, slots=True)
class StatusCommandAction:
    target_kind: str
    target_id: str
    command: ControlIntent
    snapshot: dict[str, Any]

    def card(self) -> OutboundCard:
        """Renew transport nonces while preserving the original display snapshot."""
        if self.target_kind == "side":
            return side_status_card(scope=self.command.scope, side_id=self.target_id, **self.snapshot)
        return session_status_card(scope=self.command.scope, binding_id=self.target_id, **self.snapshot)


def is_status_command(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("intent") == _STATUS_COMMAND


def decode_status_command(
    *, scope: FeishuScope, message_id: str, sender_id: str, value: Any,
) -> StatusCommandAction:
    fields = {"v", "intent", "scope", "target_kind", "target_id", "command", "snapshot"}
    if isinstance(value, Mapping):
        value = {key: item for key, item in value.items() if key != "nonce"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CardActionError("状态卡操作无效，请重新发送 /status。")
    if type(value["v"]) is not int or value["v"] != 1 or not is_status_command(value):
        raise CardActionError("状态卡版本已过期，请重新发送 /status。")
    if value["scope"] != scope.key:
        raise CardActionError("状态卡与当前聊天或话题不一致，本次未执行。")
    target_kind = value["target_kind"]
    if target_kind not in ("binding", "side"):
        raise CardActionError("状态卡会话类型无效，请重新发送 /status。")
    commands = _SIDE_COMMANDS if target_kind == "side" else _BINDING_COMMANDS
    selected = next((item for item in commands if item[1].value == value["command"]), None)
    if selected is None:
        raise CardActionError("状态卡不支持这个命令，请重新发送 /status。")
    return StatusCommandAction(
        target_kind=target_kind,
        target_id=_required_string(value["target_id"], "target_id"),
        command=ControlIntent(
            scope=scope, source_id=_required_string(message_id, "message_id"),
            sender_id=_required_string(sender_id, "sender_id"),
            name=selected[1], arguments=selected[2],
        ),
        snapshot=_decode_snapshot(target_kind, value["snapshot"]),
    )


def _decode_snapshot(target_kind: str, value: Any) -> dict[str, Any]:
    # Only inert display strings cross the callback. Never accept raw card JSON,
    # and never use these historical labels as command admission or identity.
    if target_kind == "side":
        strings = {"short_id", "parent_short_id", "state", "turn_state"}
        lines = {"git_branch_lines": 1}
    else:
        strings = {"short_id", "project_alias", "native_thread_id", "state", "context_window_line", "subscription_line"}
        lines = {"metadata_lines": 2, "git_branch_lines": 1, "context_mode_lines": 4, "model_lines": 4, "progress_lines": 32}
    if not isinstance(value, Mapping) or set(value) != strings | lines.keys():
        raise CardActionError("状态卡展示快照无效，请重新发送 /status。")
    if any(
        not isinstance(value[key], str)
        and not (key == "native_thread_id" and value[key] is None)
        for key in strings
    ):
        raise CardActionError("状态卡展示文字无效，请重新发送 /status。")
    for key, limit in lines.items():
        items = value[key]
        if (not isinstance(items, (list, tuple)) or len(items) > limit
                or any(not isinstance(item, str) for item in items)):
            raise CardActionError("状态卡展示字段无效，请重新发送 /status。")
    if target_kind == "binding" and (len(value["metadata_lines"]) != 2 or len(value["model_lines"]) != 4):
        raise CardActionError("状态卡展示字段缺失，请重新发送 /status。")
    return dict(value)


def _command_buttons(
    scope: FeishuScope, target_kind: str, target_id: str, snapshot: dict[str, Any],
) -> dict[str, Any]:
    commands = _SIDE_COMMANDS if target_kind == "side" else _BINDING_COMMANDS
    return _button_row(*(
        _repeatable_callback_button(label=label, value={
            "v": 1, "intent": _STATUS_COMMAND, "scope": scope.key,
            "target_kind": target_kind, "target_id": target_id,
            "command": command.value,
            "snapshot": snapshot,
        })
        for label, command, _arguments in commands
    ), equal_width=False)


def _status_card(card: dict[str, Any]) -> OutboundCard:
    # The snapshot appears in each button. Check the complete transport payload,
    # including non-ASCII text, rather than just the visible body or one action.
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > _STATUS_CARD_MAX_BYTES:
        raise CardActionError("状态卡内容过长，暂不能展示；可直接使用 /stop、/config 或 /sessions。")
    return OutboundCard(card=card)


def session_status_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    native_thread_id: str | None,
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
            *session_identity_lines(
                short_id=short_id, native_thread_id=native_thread_id,
            ),
            subscription_line,
        )))],
    })
    builder.raw(_command_buttons(scope, "binding", binding_id, {
        "short_id": short_id, "project_alias": project_alias,
        "native_thread_id": native_thread_id, "state": state,
        "metadata_lines": metadata_lines, "git_branch_lines": git_branch_lines,
        "context_window_line": context_window_line, "context_mode_lines": context_mode_lines,
        "model_lines": model_lines, "progress_lines": progress_lines,
        "subscription_line": subscription_line,
    }))
    builder.markdown("<font color='grey'>本次查询快照 · 按钮与对应命令的处理方式相同</font>")
    return _status_card(builder.to_dict())


def side_status_card(
    *,
    scope: FeishuScope,
    side_id: str,
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
    builder.raw(_command_buttons(scope, "side", side_id, {
        "short_id": short_id, "parent_short_id": parent_short_id,
        "state": state, "turn_state": turn_state, "git_branch_lines": git_branch_lines,
    }))
    builder.markdown("<font color='grey'>本次查询快照 · 按钮与对应命令的处理方式相同</font>")
    return _status_card(builder.to_dict())


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
