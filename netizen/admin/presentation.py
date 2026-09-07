"""Pure Admin response projections over shared management facts."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlencode

from ..bindings import BindingTurnSettings
from ..runtime.contracts import (
    ReleaseDisposition,
    StopDisposition,
)
from ..management import BindingStatusProjection, ChatLabel


def _release_check_message(code: str | None) -> str | None:
    if code is None:
        return None
    return {
        "release_rate_limited": "GitHub 更新查询已触发限流，请稍后重试。",
        "release_access_denied": (
            "更新查询被拒绝（HTTP 403）。请检查服务所在机器的网络访问策略或代理配置；"
            "若持续出现，请联系维护者。"
        ),
        "release_not_found": (
            "未找到可访问的官方发布版本（HTTP 404）。请稍后重试；"
            "若持续出现，请联系维护者检查仓库与 Release 状态。"
        ),
        "release_service_unavailable": (
            "更新服务暂时异常（HTTP 5xx）。请稍后重新检查。"
        ),
        "release_http_error": (
            "更新查询收到异常 HTTP 响应。请稍后重试；"
            "若持续出现，请联系维护者检查更新接口或网络代理。"
        ),
        "release_check_timeout": (
            "查询 GitHub 更新服务超时。请稍后重试；"
            "若持续出现，请检查服务所在机器的网络或代理配置。"
        ),
        "release_network_error": (
            "访问 GitHub 更新服务时发生网络错误。请检查服务所在机器的网络、代理和 HTTPS 证书配置后再试。"
        ),
        "release_invalid_response": (
            "官方更新信息格式异常，或发布资产不完整、未通过校验。"
            "请稍后重试；若持续出现，请联系维护者检查 Release。"
        ),
    }.get(code, "检查更新失败，原因暂未识别。请稍后重试；若持续出现，请联系维护者。")


def _chat_open_url(chat: ChatLabel) -> str:
    if chat.chat_mode == "p2p" and chat.p2p_target_open_id is not None:
        query = urlencode({"openId": chat.p2p_target_open_id})
    else:
        query = urlencode({"openChatId": chat.chat_id})
    return f"https://applink.feishu.cn/client/chat/open?{query}"


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            key: _jsonable(item)
            for key, item in asdict(value).items()
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"unsupported JSON projection: {type(value).__name__}")


def _settings_json(settings: BindingTurnSettings | None) -> dict[str, str] | None:
    if settings is None:
        return None
    return {
        "modelId": settings.model_id,
        "effortId": settings.effort_id,
        "serviceTierId": settings.service_tier_id,
    }


def _runtime_binding_json(status: BindingStatusProjection) -> dict[str, object]:
    snapshot = status.snapshot
    turn = snapshot.turn
    goal = snapshot.goal
    lifecycle = snapshot.lifecycle
    subscription = snapshot.subscription
    usage = snapshot.context_window_usage
    return {
        "bindingId": snapshot.binding_id,
        "activityRevision": snapshot.activity_revision,
        "primaryStatus": status.primary_status,
        "primaryStatusResolution": status.primary_status_resolution.value,
        "subscriptionState": (
            status.subscription_state.value
            if status.subscription_state is not None
            else None
        ),
        "turn": (
            {
                "threadId": turn.thread_id,
                "turnId": turn.turn_id,
                "state": turn.state.value,
            }
            if turn is not None
            else None
        ),
        "goal": (
            {
                "threadId": goal.thread_id,
                "logicalTurnId": goal.logical_turn_id,
                "state": goal.state.value,
            }
            if goal is not None
            else None
        ),
        "compacting": snapshot.compacting,
        "lifecycle": (
            {
                "threadId": lifecycle.thread_id,
                "state": lifecycle.state.value,
            }
            if lifecycle is not None
            else None
        ),
        "subscription": (
            {
                "threadId": subscription.thread_id,
                "state": subscription.state.value,
                "releaseInSeconds": subscription.release_in_seconds,
            }
            if subscription is not None
            else None
        ),
        "contextWindow": (
            {
                "usedTokens": usage.used_tokens,
                "contextWindowTokens": usage.context_window_tokens,
            }
            if usage is not None
            else None
        ),
    }


def _stop_disposition_message(disposition: StopDisposition) -> str:
    return {
        StopDisposition.NOT_RUNNING: "该会话当前没有运行任务。",
        StopDisposition.REQUESTED: (
            "已请求中断 exact Codex Turn；确认终态前仍会显示为停止中。"
        ),
        StopDisposition.STOPPING: (
            "该会话正在停止；已再次尝试完成中断与终端清理。"
        ),
        StopDisposition.COMPACTING: (
            "该会话正在压缩，当前没有已验证的安全取消能力。"
        ),
        StopDisposition.GOAL_REQUESTED: (
            "已请求暂停 Goal 并中断当前物理 Turn。"
        ),
        StopDisposition.GOAL_STOPPING: "该 Goal 正在暂停。",
        StopDisposition.EXTERNAL_GOAL: (
            "这是外部 active Goal，当前无法安全重挂并暂停。"
        ),
    }[disposition]


def _release_disposition_message(disposition: ReleaseDisposition) -> str:
    return {
        ReleaseDisposition.NOT_MATERIALIZED: (
            "该会话尚未物化，没有原生 Thread 订阅可释放。"
        ),
        ReleaseDisposition.NOT_SUBSCRIBED: (
            "本进程当前没有该 Thread 的订阅；Binding 与原生历史均保留。"
        ),
        ReleaseDisposition.RELEASED: (
            "已取消本进程对该 Thread 的订阅；Binding 与原生历史均保留，"
            "下次消息仍会 resume 同一 Thread。"
        ),
    }[disposition]


def _runtime_side_json(snapshot: Any) -> dict[str, object]:
    return {
        "sideId": snapshot.side_id,
        "parentBindingId": snapshot.parent_binding_id,
        "threadId": snapshot.thread_id,
        "state": snapshot.state.value,
        "turnId": snapshot.turn_id,
        "turnState": (
            snapshot.turn_state.value if snapshot.turn_state is not None else None
        ),
    }


def _project_result(request_id: str, project: Any) -> dict[str, object]:
    return {
        "requestId": request_id,
        "alias": project.alias,
        "cwd": str(project.cwd),
        "enabled": project.enabled,
        "revision": project.revision,
    }


def _binding_result(request_id: str, binding: Any) -> dict[str, object]:
    return {
        "requestId": request_id,
        "bindingId": binding.id,
        "scopeKey": binding.scope_key,
        "current": binding.active,
        "nativeThreadId": binding.native_thread_id,
        "settingsRevision": binding.settings_revision,
        "messageContextMode": binding.message_context_mode.value,
        "contextRevision": binding.context_revision,
    }
