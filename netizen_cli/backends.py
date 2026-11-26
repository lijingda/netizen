"""Native backend identifiers supported by this program version."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal

from .domain import NativeCapability
from .management_errors import ManagementError

BackendId = Literal["codex"]
Surface = Literal["command", "card", "api"]
DEFAULT_BACKEND: Literal["codex"] = "codex"
KNOWN_BACKENDS = (DEFAULT_BACKEND,)


class BackendError(ValueError):
    pass


def instance_backend(instance: Mapping[str, object]) -> BackendId:
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
        super().__init__(detail or f"当前实例不支持【{feature}】。")


class Provider(Enum):
    REPLY_PROGRESS = "reply-progress"
    HOST = "host"
    ORDINARY = "ordinary"
    CODEX_RUNTIME = "codex-runtime"
    SCHEDULER = "scheduler"
    TITLE_WRITER = "title-writer"
    PERSISTENT_FORK = "persistent-fork"
    NAME_GENERATOR = "name-generator"


class OperationId(str, Enum):
    HELP = "help"
    NEW = "new"
    STOP = "stop"
    RECHECK = "recheck"
    STATUS = "status"
    LIST = "list_bindings"
    ACTIVATE = "activate_binding"
    PROJECTS = "projects"
    WORKTREES = "worktrees"
    CONTEXT = "context"
    DEFAULTS = "defaults"
    ADMIN = "admin"
    ENABLE_GROUP = "enable_group"
    MODEL = "model"
    SIDE = "side"
    GOAL = "goal"
    FORK = "fork"
    COMPACT = "compact"
    USAGE = "usage"
    SCHEDULE = "schedule"
    RELEASE = "release"
    ARCHIVE = "archive"
    UNARCHIVE = "unarchive"
    DELETE = "delete"
    MANUAL_RENAME = "rename"
    GENERATE_NAME = "generate_name"
    ARCHIVED = "archived"
    HISTORY = "history"
    COLLABORATION = "collaboration"
    REPLY_CARD = "reply_card"
    PROGRESS = "progress"
    QUESTIONS = "questions"
    SKILLS = "skills"
    FILES = "files"


@dataclass(frozen=True, slots=True)
class Operation:
    label: str
    provider: Provider
    commands: tuple[str, ...] = ()
    cards: tuple[str, ...] = ()
    routes: tuple[tuple[str, str], ...] = ()
    command_probe: NativeCapability | None = None
    grant: str = ""
    action_key: str = ""
    excluded_surfaces: tuple[Surface, ...] = ()

# Each route is an actual authenticated endpoint. The router retains ownership
# of unknown paths, HTTP methods, request schemas and one-time grant redemption.
OPERATIONS = {
    OperationId.HELP: Operation("帮助", Provider.HOST, ("help", "menu")),
    OperationId.NEW: Operation(
        "新建会话", Provider.ORDINARY, ("new",), ("binding.create",)
    ),
    OperationId.STOP: Operation(
        "停止",
        Provider.ORDINARY,
        ("stop",),
        ("binding.stop.exact",),
        (("POST", "/sessions/stop"),),
    ),
    OperationId.RECHECK: Operation(
        "重新检查", Provider.ORDINARY, cards=("binding.turn.recheck",)
    ),
    OperationId.STATUS: Operation("本地状态", Provider.ORDINARY, ("status",)),
    OperationId.LIST: Operation(
        "会话列表",
        Provider.ORDINARY,
        ("sessions", "threads"),
        ("sessions.page",),
        (("GET", "/sessions"),),
    ),
    OperationId.ACTIVATE: Operation(
        "切换会话",
        Provider.ORDINARY,
        ("resume",),
        ("binding.activate",),
        (("POST", "/sessions/activate"),),
    ),
    OperationId.PROJECTS: Operation(
        "项目",
        Provider.HOST,
        ("projects",),
        (
            "settings.section.open",
            "settings.refresh",
            "project.register",
            "project.enabled.set",
        ),
    ),
    OperationId.WORKTREES: Operation(
        "worktree", Provider.HOST, cards=("binding.workspace.prepare",)
    ),
    OperationId.CONTEXT: Operation(
        "会话设置",
        Provider.ORDINARY,
        ("config",),
        ("binding.configure",),
        (("POST", "/sessions/configure"),),
    ),
    OperationId.DEFAULTS: Operation(
        "默认设置",
        Provider.HOST,
        ("defaults",),
        routes=(
            ("GET", "/defaults"),
            ("POST", "/defaults/save"),
            ("POST", "/defaults/delete"),
            ("POST", "/defaults/reorder"),
        ),
    ),
    OperationId.ADMIN: Operation("管理页面", Provider.HOST, ("admin",)),
    OperationId.ENABLE_GROUP: Operation("群聊启用", Provider.HOST, ("enable",)),
    OperationId.MODEL: Operation(
        "模型、思考强度或速度设置",
        Provider.CODEX_RUNTIME,
        ("model", "effort", "fast", "speed"),
    ),
    OperationId.SIDE: Operation(
        "Side",
        Provider.CODEX_RUNTIME,
        ("side",),
        ("side.close",),
        (
            ("GET", "/side-topics"),
            ("POST", "/side-topics/stop"),
            ("POST", "/side-topics/close"),
        ),
        NativeCapability.SIDE,
    ),
    OperationId.GOAL: Operation(
        "Goal",
        Provider.CODEX_RUNTIME,
        ("goal",),
        ("goal.refresh", "goal.pause", "goal.resume", "goal.clear"),
        command_probe=NativeCapability.GOAL,
    ),
    OperationId.FORK: Operation("会话分支", Provider.PERSISTENT_FORK, ("fork",)),
    OperationId.COMPACT: Operation("上下文压缩", Provider.CODEX_RUNTIME, ("compact",)),
    OperationId.USAGE: Operation(
        "账号额度",
        Provider.CODEX_RUNTIME,
        ("usage",),
        command_probe=NativeCapability.ACCOUNT_RATE_LIMITS,
    ),
    OperationId.SCHEDULE: Operation(
        "定时任务",
        Provider.SCHEDULER,
        ("cron",),
        routes=(
            ("GET", "/schedules"),
            ("POST", "/schedules/create"),
            ("POST", "/schedules/update"),
            ("POST", "/schedules/delete"),
            ("POST", "/schedules/run-now"),
        ),
    ),
    OperationId.RELEASE: Operation(
        "释放会话",
        Provider.CODEX_RUNTIME,
        ("release",),
        routes=(("POST", "/sessions/release"),),
        command_probe=NativeCapability.RELEASE,
        excluded_surfaces=("card",),
    ),
    OperationId.ARCHIVE: Operation(
        "会话归档",
        Provider.CODEX_RUNTIME,
        ("archive",),
        ("binding.archive", "binding.archive.exact"),
        (("POST", "/sessions/archive"),),
    ),
    OperationId.UNARCHIVE: Operation(
        "会话恢复",
        Provider.CODEX_RUNTIME,
        ("unarchive",),
        ("binding.unarchive",),
        (("POST", "/sessions/unarchive"),),
    ),
    OperationId.DELETE: Operation(
        "会话删除",
        Provider.CODEX_RUNTIME,
        ("delete",),
        (
            "binding.delete",
            "binding.delete.exact.prepare",
            "binding.delete.exact",
            "binding.delete.archived.prepare",
            "binding.delete.archived",
        ),
        (("POST", "/sessions/delete-lazy"), ("POST", "/sessions/delete-materialized")),
    ),
    OperationId.MANUAL_RENAME: Operation(
        "会话命名",
        Provider.TITLE_WRITER,
        ("rename",),
        ("binding.rename",),
        (("POST", "/sessions/rename"),),
        grant="sessions.rename",
        action_key="rename",
    ),
    OperationId.GENERATE_NAME: Operation(
        "生成会话名称",
        Provider.NAME_GENERATOR,
        cards=("binding.name.generate",),
        routes=(("POST", "/sessions/generate-name"),),
        grant="sessions.generate-name",
        action_key="generateName",
    ),
    OperationId.ARCHIVED: Operation(
        "已归档会话", Provider.CODEX_RUNTIME, cards=("sessions.archived.refresh",)
    ),
    OperationId.HISTORY: Operation(
        "原生会话目录或历史",
        Provider.CODEX_RUNTIME,
        routes=(("GET", "/sessions/conversation"),),
    ),
    OperationId.COLLABORATION: Operation(
        "会话协作权限",
        Provider.CODEX_RUNTIME,
        routes=(("GET", "/session-tool-access"), ("POST", "/session-tool-access/save")),
    ),
    OperationId.REPLY_CARD: Operation("执行回复卡", Provider.REPLY_PROGRESS),
    OperationId.PROGRESS: Operation(
        "过程展示设置",
        Provider.REPLY_PROGRESS,
        routes=(
            ("GET", "/progress-card/settings"),
            ("POST", "/progress-card/settings"),
        ),
    ),
    OperationId.QUESTIONS: Operation(
        "原生提问通知",
        Provider.CODEX_RUNTIME,
        routes=(
            ("GET", "/question-notifications/settings"),
            ("POST", "/question-notifications/settings"),
        ),
    ),
    OperationId.SKILLS: Operation(
        "显式 Skill 调用", Provider.CODEX_RUNTIME, command_probe=NativeCapability.SKILLS
    ),
    OperationId.FILES: Operation("本轮文件", Provider.REPLY_PROGRESS),
}
_COMMANDS = {
    command: name for name, op in OPERATIONS.items() for command in op.commands
}
_CARDS = {card: name for name, op in OPERATIONS.items() for card in op.cards}
_ROUTES = {route: name for name, op in OPERATIONS.items() for route in op.routes}

# These families also protect future authenticated descendants and alternate methods.
_API_FAMILIES = {
    path: operation for (_, path), operation in _ROUTES.items()
    if path.startswith("/sessions/") or path in ("/side-topics", "/schedules", "/session-tool-access")
} | {
    "/session-tools": OperationId.COLLABORATION,
    "/progress-card": OperationId.PROGRESS,
    "/question-notifications": OperationId.QUESTIONS,
}


def command_operation(command: str, *, archived: bool = False) -> OperationId | None:
    op = _COMMANDS.get(command)
    return OperationId.ARCHIVED if op is OperationId.LIST and archived else op


@dataclass(frozen=True, slots=True)
class BackendPolicy:
    backend: str
    providers: Callable[[Provider], bool] | None = None

    def __post_init__(self) -> None:
        if self.backend not in KNOWN_BACKENDS:
            raise BackendError("unknown backend policy")

    def bind(self, providers: Callable[[Provider], bool]) -> "BackendPolicy":
        return replace(self, providers=providers)

    @property
    def display_name(self) -> str:
        return "Codex"


    @property
    def inherited_model_label(self) -> str:
        return "继承 Codex"

    def has_provider(self, provider: Provider) -> bool:
        if self.providers is not None:
            return self.providers(provider)
        # Standalone parser/identity compatibility. Application services bind the
        # concrete provider lookup before admission.
        return True

    def allows(self, operation: OperationId, surface: Surface = "command") -> bool:
        provider = self.provider_for(operation, surface)
        return provider is not None and self.has_provider(provider)

    def provider_for(self, operation: OperationId, surface: Surface) -> Provider | None:
        definition = OPERATIONS[operation]
        return None if surface in definition.excluded_surfaces else definition.provider

    def require_provider(self, provider: Provider, label: str) -> None:
        if not self.has_provider(provider):
            raise BackendUnsupported(label, detail=self.refusal(label))

    def refusal(self, label: str) -> str:
        return f"当前实例不支持【{label}】。"

    def require(self, operation: OperationId, surface: Surface = "command") -> None:
        if not self.allows(operation, surface):
            label = OPERATIONS[operation].label
            raise BackendUnsupported(label, detail=self.refusal(label))


    def command_refusal(self, command: str, *, archived: bool = False) -> str | None:
        selected = command_operation(command, archived=archived)
        if selected is None:
            # The command parser owns unsupported built-ins and unknown commands.
            return None
        return (
            None if self.allows(selected) else self.refusal(OPERATIONS[selected].label)
        )

    def require_card(self, card: str) -> None:
        selected = _CARDS.get(card)
        if selected is None:
            raise BackendUnsupported("卡片操作", detail=self.refusal("卡片操作"))
        self.require(selected, "card")

    def require_api(self, method: str, path: str) -> None:
        if (selected := _ROUTES.get((method, path))) is not None:
            self.require(selected, "api")
            return
        for family, operation in _API_FAMILIES.items():
            if path == family or path.startswith(family + "/"):
                self.require(operation, "api")
                return
