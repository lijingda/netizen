"""Shared callback envelopes, references, validation, and card primitives."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Mapping
from typing import Any

from lark_channel import new_card

from ..domain import (
    CardControlName,
    FeishuScope,
    MentionContextMode,
    SettingsSection,
    ScopeKind,
    TurnFileActionName,
)
from ..projects import Project
from ..sdk_gap_adapter import GoalSnapshot


ACTION_VERSION = 4
TURN_FILE_ACTION_VERSION = 4
REPLY_CARD_ACTION_VERSION = 5
MAX_MODEL_ID_CHARS = 256
MAX_ENCODED_MODEL_ID_CHARS = 1368
_INHERIT_MODEL_CHOICE = "inherit"
_PROJECT_REFERENCE = re.compile(
    r"project:v1:([a-z0-9][a-z0-9_-]{0,63}):([1-9][0-9]*)"
)
_BINDING_REFERENCE = re.compile(r"binding:v1:([A-Za-z0-9][A-Za-z0-9-]{0,127})")
_NATIVE_THREAD_REFERENCE = re.compile(
    r"native-thread:v1:([A-Za-z0-9][A-Za-z0-9._-]{0,191})"
)
_NEW_MODEL_REFERENCE = re.compile(
    r"new-model:v1:(inherit|explicit:([A-Za-z0-9_-]+))"
)
_CONFIG_MODEL_REFERENCE = re.compile(
    r"config-model:v4:([A-Za-z0-9][A-Za-z0-9-]{0,127}):"
    r"([1-9][0-9]{0,18}):([1-9][0-9]{0,18}):"
    r"([1-9][0-9]{0,18}):(inherit|explicit:([A-Za-z0-9_-]+))"
)
_CONTEXT_MODE_REFERENCE = re.compile(
    r"context-mode:v1:(current-only|catch-up)"
)
_TASK_FEEDBACK_REFERENCE = re.compile(r"task-feedback:v2:(off|on)")
_CALLBACK_NONCE = re.compile(r"[0-9a-f]{32}")
_PROJECT_MODE_REFERENCE = re.compile(
    r"project-mode:v2:(create|existing):([0-9a-f]{32})"
)
_SIDE_REFERENCE = re.compile(r"side:v1:([A-Za-z0-9][A-Za-z0-9-]{0,127})")
_TURN_REFERENCE = re.compile(
    r"turn:v1:([A-Za-z0-9][A-Za-z0-9._-]{0,191})"
)
_GOAL_GENERATION = re.compile(r"[A-Za-z0-9_-]{43}")
_REPEATABLE_CARD_CONTROL_NAMES = frozenset(
    {
        CardControlName.OPEN_SETTINGS_SECTION,
        CardControlName.REFRESH_SETTINGS,
        CardControlName.PREPARE_EXACT_DELETE_BINDING,
        CardControlName.PREPARE_ARCHIVED_DELETE_BINDING,
        CardControlName.ACTIVATE_BINDING,
        CardControlName.RECHECK_EXACT_TURN,
        CardControlName.SESSIONS_PAGE,
        CardControlName.REFRESH_ARCHIVED_SESSIONS,
        CardControlName.GOAL_PAUSE,
        CardControlName.GOAL_RESUME,
        CardControlName.SIDE_CLOSE,
    }
)
# These actions can legitimately reappear with the same semantic payload on
# one updated message. Revision-bearing and one-shot actions stay excluded.
_REPEATABLE_CALLBACK_INTENTS = frozenset(
    name.value for name in _REPEATABLE_CARD_CONTROL_NAMES
) | {TurnFileActionName.PAGE.value}


class CardActionError(ValueError):
    pass


class TurnFileCardLimitError(CardActionError):
    pass


class SettingsCardActionError(CardActionError):
    def __init__(
        self,
        message: str,
        *,
        scope: FeishuScope,
        section: SettingsSection,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.section = section


def goal_generation(snapshot: GoalSnapshot) -> str:
    """Return the strongest stable native Goal fingerprint the SDK exposes."""

    return _goal_generation(snapshot)


def _goal_generation(snapshot: GoalSnapshot) -> str:
    if not snapshot.thread_id or isinstance(snapshot.created_at, bool):
        raise ValueError("Goal generation requires a native identity and creation time")
    material = (
        "netizen-goal-generation:v2\0"
        f"{snapshot.thread_id}\0{snapshot.created_at}\0"
        f"{snapshot.objective}\0{snapshot.token_budget}"
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(material).digest()).decode(
        "ascii"
    ).rstrip("=")


def _decode_goal_generation(value: Any) -> str:
    generation = _required_string(value, "goal_generation")
    if _GOAL_GENERATION.fullmatch(generation) is None:
        raise CardActionError("Goal generation 无效或已过期。")
    return generation


def scope_from_fetched_card(
    *,
    app_id: str,
    callback_chat_id: str,
    fetched_message: Any,
    chat_type: str | None,
) -> FeishuScope:
    item = _fetched_card_item(
        callback_chat_id=callback_chat_id,
        fetched_message=fetched_message,
    )
    raw_topic_id = item.get("thread_id")
    if isinstance(raw_topic_id, str) and raw_topic_id:
        return FeishuScope(
            app_id,
            callback_chat_id,
            ScopeKind.TOPIC,
            raw_topic_id,
        )
    if chat_type == "p2p":
        kind = ScopeKind.DIRECT
    elif chat_type == "group":
        kind = ScopeKind.GROUP
    else:
        raise CardActionError("无法判断卡片来自单聊还是群聊。")
    return FeishuScope(app_id, callback_chat_id, kind)


def fetched_card_topic_id(
    *,
    callback_chat_id: str,
    fetched_message: Any,
) -> str | None:
    """Return the public thread id without requiring an unrelated chat lookup."""
    item = _fetched_card_item(
        callback_chat_id=callback_chat_id,
        fetched_message=fetched_message,
    )
    raw_topic_id = item.get("thread_id")
    return raw_topic_id if isinstance(raw_topic_id, str) and raw_topic_id else None


def _fetched_card_item(
    *,
    callback_chat_id: str,
    fetched_message: Any,
) -> Mapping[str, Any]:
    if not isinstance(fetched_message, Mapping):
        raise CardActionError("无法读取卡片原消息。")
    data = fetched_message.get("data")
    if not isinstance(data, Mapping):
        raise CardActionError("卡片原消息缺少 data。")
    items = data.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
        raise CardActionError("卡片原消息缺少 items。")
    item = items[0]
    if item.get("chat_id") != callback_chat_id:
        raise CardActionError("卡片原消息与回调聊天不一致。")
    return item


def _scope_from_envelope(
    *,
    app_id: str,
    chat_id: str,
    kind: Any,
    topic_id: Any,
) -> FeishuScope:
    try:
        scope_kind = ScopeKind(kind)
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    if scope_kind is ScopeKind.TOPIC:
        topic = _required_string(topic_id, "topic_id")
    else:
        if topic_id is not None:
            raise CardActionError("非话题 Scope 不能携带 topic_id。")
        topic = None
    return FeishuScope(app_id, chat_id, scope_kind, topic)


def _envelope(
    scope: FeishuScope,
    name: CardControlName,
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "v": ACTION_VERSION,
        "intent": name.value,
        "chat_id": scope.chat_id,
        "scope_kind": scope.kind.value,
        **extra,
    }
    if scope.kind is ScopeKind.TOPIC:
        value["topic_id"] = scope.topic_id
    return value


def _project_reference(project: Project) -> str:
    return f"project:v1:{project.alias}:{project.revision}"


def _decode_project_reference(value: str) -> tuple[str, int]:
    match = _PROJECT_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("Project 选择值无效，请刷新卡片后重试。")
    return match.group(1), int(match.group(2))


def _project_mode_reference(mode: str, callback_nonce: str) -> str:
    value = f"project-mode:v2:{mode}:{callback_nonce}"
    if _PROJECT_MODE_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Project mode callback reference")
    return value


def _decode_project_mode_reference(value: str) -> str:
    if value in {"create", "existing"}:
        return value
    prefix = "project-mode:v2:"
    if value.startswith(prefix):
        mode = value[len(prefix) :].split(":", 1)[0]
        if mode in {"create", "existing"}:
            return mode
    raise CardActionError("未知 Project 创建模式。")


def _binding_reference(binding_id: str) -> str:
    return f"binding:v1:{binding_id}"


def _decode_binding_reference(value: str) -> str:
    match = _BINDING_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("会话引用无效，请重新打开原卡片。")
    return match.group(1)


def _native_thread_reference(thread_id: str) -> str:
    value = f"native-thread:v1:{thread_id}"
    if _NATIVE_THREAD_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid native Thread reference")
    return value


def _decode_native_thread_reference(value: str) -> str:
    match = _NATIVE_THREAD_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("原生会话引用无效，请重新发送 /delete。")
    return match.group(1)


def _side_reference(side_id: str) -> str:
    value = f"side:v1:{side_id}"
    if _SIDE_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Side reference")
    return value


def _decode_side_reference(value: str) -> str:
    match = _SIDE_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("Side 引用无效，请回到原 Side 话题重试。")
    return match.group(1)


def _turn_reference(turn_id: str) -> str:
    value = f"turn:v1:{turn_id}"
    if _TURN_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid native Turn reference")
    return value


def _decode_turn_reference(value: str) -> str:
    match = _TURN_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("原生 Turn 引用无效，请重新执行任务。")
    return match.group(1)


def _rename_name_field(binding_id: str) -> str:
    if _BINDING_REFERENCE.fullmatch(_binding_reference(binding_id)) is None:
        raise ValueError("invalid Binding rename reference")
    return f"rename_name_v1__{binding_id}"


def _new_model_reference(model_id: str | None) -> str:
    choice = _encode_model_choice(model_id)
    value = f"new-model:v1:{choice}"
    if _NEW_MODEL_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid new Binding model reference")
    return value


def _decode_new_model_reference(value: str) -> str | None:
    match = _NEW_MODEL_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("新建会话卡片已过期，请重新发送 /new。")
    return _decode_model_choice(match.group(1), match.group(2), command="/new")


def _config_model_reference(
    *,
    binding_id: str,
    settings_revision: int,
    context_revision: int,
    feedback_revision: int,
    model_id: str | None,
) -> str:
    choice = _encode_model_choice(model_id)
    value = (
        f"config-model:v4:{binding_id}:{settings_revision}:"
        f"{context_revision}:{feedback_revision}:{choice}"
    )
    if _CONFIG_MODEL_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Binding model settings reference")
    return value


def _decode_config_model_reference(
    value: str,
) -> tuple[str, int, int, int, str | None]:
    match = _CONFIG_MODEL_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("会话配置卡片已过期，请重新发送 /config。")
    model_id = _decode_model_choice(
        match.group(5),
        match.group(6),
        command="/config",
    )
    return (
        match.group(1),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
        model_id,
    )


def _encode_model_choice(model_id: str | None) -> str:
    if model_id is None:
        return _INHERIT_MODEL_CHOICE
    if (
        not isinstance(model_id, str)
        or not model_id
        or len(model_id) > MAX_MODEL_ID_CHARS
        or "\x00" in model_id
    ):
        raise ValueError("invalid model ID")
    encoded_model = base64.urlsafe_b64encode(model_id.encode("utf-8")).decode(
        "ascii"
    ).rstrip("=")
    return f"explicit:{encoded_model}"


def _decode_model_choice(
    choice: str,
    encoded_model: str | None,
    *,
    command: str,
) -> str | None:
    if choice == _INHERIT_MODEL_CHOICE:
        if encoded_model is not None:
            raise CardActionError(
                f"会话配置卡片已过期，请重新发送 {command}。"
            )
        return None
    if encoded_model is None:
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        )
    if len(encoded_model) > MAX_ENCODED_MODEL_ID_CHARS:
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        )
    padding = "=" * (-len(encoded_model) % 4)
    try:
        model_id = base64.b64decode(
            encoded_model + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        ) from error
    if (
        not model_id
        or len(model_id) > MAX_MODEL_ID_CHARS
        or "\x00" in model_id
    ):
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        )
    return model_id


def _context_mode_reference(mode: MentionContextMode) -> str:
    return f"context-mode:v1:{mode.value}"


def _decode_context_mode_reference(value: str) -> MentionContextMode:
    match = _CONTEXT_MODE_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("消息范围选择已过期，请重新打开卡片。")
    return MentionContextMode(match.group(1))


def _task_feedback_reference(enabled: bool) -> str:
    return f"task-feedback:v2:{'on' if enabled else 'off'}"


def _decode_task_feedback_reference(value: Any, field: str) -> bool:
    match = _TASK_FEEDBACK_REFERENCE.fullmatch(_required_string(value, field))
    if match is None:
        raise CardActionError("任务反馈选择已过期，请重新打开卡片。")
    return match.group(1) == "on"


def _builder(title: str, subtitle: str, *, template: str = "blue"):
    return (
        new_card()
        .config(update_multi=True, width_mode="default")
        .header(title, subtitle=subtitle, template=template)
    )


def _button_row(*buttons: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [button],
            }
            for button in buttons
        ],
    }


def _new_callback_nonce() -> str:
    return secrets.token_hex(16)


def _valid_callback_nonce(value: Any) -> bool:
    return isinstance(value, str) and _CALLBACK_NONCE.fullmatch(value) is not None


def _repeatable_callback_button(
    *,
    label: str,
    value: dict[str, Any],
    style: str = "default",
    confirm: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if "nonce" in value:
        raise ValueError("callback value already contains a nonce")
    return _callback_button(
        label=label,
        value={**value, "nonce": _new_callback_nonce()},
        style=style,
        confirm=confirm,
    )


def _callback_button(
    *,
    label: str,
    value: dict[str, Any],
    style: str = "default",
    confirm: tuple[str, str] | None = None,
) -> dict[str, Any]:
    intent = value.get("intent")
    is_repeatable = intent in _REPEATABLE_CALLBACK_INTENTS
    has_valid_nonce = _valid_callback_nonce(value.get("nonce"))
    if (is_repeatable and not has_valid_nonce) or (
        not is_repeatable and "nonce" in value
    ):
        raise ValueError(
            "repeatable callback values must carry exactly one valid nonce"
        )
    button: dict[str, Any] = {
        "tag": "button",
        "text": _plain_text(label),
        "type": style,
        "behaviors": [{"type": "callback", "value": value}],
    }
    if confirm is not None:
        button["confirm"] = {
            "title": _plain_text(confirm[0]),
            "text": _plain_text(confirm[1]),
        }
    return button


def _notice(message: str, *, error: bool = False) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "background_style": "red-50" if error else "blue-50",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "8px",
                "elements": [_plain(message)],
            }
        ],
    }


def _plain(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": _plain_text(content)}


def _plain_text(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CardActionError(f"{field} 必须是非空字符串。")
    return value


def _bounded_string(value: Any, field: str, *, max_chars: int) -> str:
    result = _required_string(value, field)
    if len(result) > max_chars or "\x00" in result:
        raise CardActionError(f"{field} 内容无效或超过 {max_chars} 个字符。")
    return result


def _md_code(value: str) -> str:
    return value.replace("`", "ˋ")
