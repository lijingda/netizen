"""Native backend identifiers supported by this program version."""

from collections.abc import Mapping
from dataclasses import dataclass

from .management_errors import ManagementError

DEFAULT_BACKEND = "codex"
KNOWN_BACKENDS = (DEFAULT_BACKEND, "claude")


class BackendError(ValueError):
    pass


def instance_backend(instance: Mapping[str, object]) -> str:
    value = instance.get("backend", DEFAULT_BACKEND)
    if not isinstance(value, str) or value not in KNOWN_BACKENDS:
        raise BackendError(
            f"invalid instance.backend ({type(value).__name__}). "
            f"This program version supports only: {', '.join(KNOWN_BACKENDS)}"
        )
    return value


class BackendUnsupported(ManagementError):
    """A stable application gap shared by all transports."""
    code = "backend_unsupported"

    def __init__(self, feature: str, *, detail: str | None = None) -> None:
        super().__init__(detail or f"Claude 试点暂不支持【{feature}】。")


@dataclass(frozen=True, slots=True)
class _Operation:
    label: str
    local: bool = False
    commands: tuple[str, ...] = ()
    cards: tuple[str, ...] = ()
    api: tuple[str, ...] = ()


_OPERATIONS = {
    "help": _Operation("帮助", True, ("help", "menu")),
    "new": _Operation("新建会话", True, ("new",), ("binding.create",)),
    "stop": _Operation("停止", True, ("stop",), ("binding.stop.exact",)),
    "recheck": _Operation("重新检查", True, cards=("binding.turn.recheck",)),
    "status": _Operation("本地状态", True, ("status",)),
    "list_bindings": _Operation("会话列表", True, ("sessions",), ("sessions.page",)),
    "activate_binding": _Operation("切换会话", True, ("resume",), ("binding.activate",)),
    "projects": _Operation("项目", True, ("projects",), (
        "settings.section.open", "settings.refresh", "project.register", "project.enabled.set",
    )),
    "worktrees": _Operation("worktree", True, cards=("binding.workspace.prepare",)),
    "context": _Operation("会话设置", True, ("config", "defaults"), ("binding.configure",)),
    "admin": _Operation("管理页面", True, ("admin",)),
    "enable_group": _Operation("群聊启用", True, ("enable",)),
    "model": _Operation("模型、思考强度或速度设置", commands=("model", "effort", "fast", "speed")),
    "side": _Operation("Side", commands=("side",), cards=("side.close",), api=("/side-topics", "/sessions/temporary")),
    "goal": _Operation("Goal", commands=("goal",), cards=("goal.refresh", "goal.pause", "goal.resume", "goal.clear")),
    "fork": _Operation("会话分支", commands=("fork",)),
    "compact": _Operation("上下文压缩", commands=("compact",)),
    "usage": _Operation("账号额度", commands=("usage",)),
    "schedule": _Operation("定时任务", commands=("cron",), api=("/schedules",)),
    "release": _Operation("释放会话订阅", commands=("release",), api=("/sessions/release",)),
    "archive": _Operation("会话归档", commands=("archive",), cards=("binding.archive", "binding.archive.exact"), api=("/sessions/archive",)),
    "unarchive": _Operation("会话恢复", commands=("unarchive",), cards=("binding.unarchive",), api=("/sessions/unarchive",)),
    "delete": _Operation("会话删除", commands=("delete",), cards=(
        "binding.delete", "binding.delete.exact.prepare", "binding.delete.exact",
        "binding.delete.archived.prepare", "binding.delete.archived",
    ), api=("/sessions/delete-lazy", "/sessions/delete-materialized")),
    "rename": _Operation("会话命名", commands=("rename",), cards=("binding.rename", "binding.name.generate"), api=("/sessions/rename", "/sessions/generate-name")),
    "archived": _Operation("已归档会话", cards=("sessions.archived.refresh",)),
    "history": _Operation("原生会话目录或历史", api=("/sessions/conversation",)),
    "collaboration": _Operation("会话协作权限", api=("/session-tools", "/session-tool-access")),
    "progress": _Operation("过程卡", api=("/progress-card",)),
    "questions": _Operation("原生提问通知", api=("/question-notifications",)),
}
_COMMANDS = {command: name for name, op in _OPERATIONS.items() for command in op.commands}
_CARDS = {card: name for name, op in _OPERATIONS.items() for card in op.cards}
_API_FAMILIES = {path: name for name, op in _OPERATIONS.items() for path in op.api}


@dataclass(frozen=True, slots=True)
class BackendPolicy:
    backend: str
    owner_id: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in ("codex", "claude"):
            raise BackendError("unknown backend policy")
        if self.backend == "claude" and not self.owner_id:
            raise BackendError("Claude 实例需要一名所有者。")

    def operation_refusal(self, operation: str, label: str) -> str | None:
        if self.backend == "claude" and (operation not in _OPERATIONS or not _OPERATIONS[operation].local):
            return f"Claude 试点暂不支持【{label}】。"
        return None

    def command_refusal(self, command: str, *, archived: bool = False) -> str | None:
        operation = "archived" if command == "sessions" and archived else _COMMANDS.get(command, command)
        feature = _OPERATIONS.get(operation)
        return self.operation_refusal(operation, feature.label if feature else "/" + command)

    def require_card(self, card: str) -> None:
        name = _CARDS.get(card)
        if self.backend == "claude" and (name is None or not _OPERATIONS[name].local):
            raise BackendUnsupported(_OPERATIONS[name].label if name is not None else "卡片操作")

    def require_api(self, path: str) -> None:
        for prefix, name in _API_FAMILIES.items():
            if path == prefix or path.startswith(prefix + "/"):
                if self.backend == "claude" and not _OPERATIONS[name].local:
                    raise BackendUnsupported(_OPERATIONS[name].label)
                return

    def sender_refusal(self, sender_id: str | None, *, bare_help: bool = False) -> str | None:
        if self.backend == "claude" and (not sender_id or (sender_id != self.owner_id and not bare_help)):
            return "这个 Claude 实例仅限所有者使用。"
        return None
