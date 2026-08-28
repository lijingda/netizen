"""Channel-facing domain types; native agent state stays in Codex."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote


class ScopeKind(str, Enum):
    DIRECT = "direct"
    GROUP = "group"
    TOPIC = "topic"


class MentionContextMode(str, Enum):
    """Binding-scoped policy for context gathered by an explicit mention."""

    CURRENT_ONLY = "current-only"
    CATCH_UP = "catch-up"


@dataclass(frozen=True, slots=True)
class MessageContextAnchor:
    """Exact Feishu message boundary for one catch-up Binding."""

    message_id: str
    create_time_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str) or not self.message_id:
            raise ValueError("context anchor message_id must not be empty")
        if (
            isinstance(self.create_time_ms, bool)
            or not isinstance(self.create_time_ms, int)
            or self.create_time_ms <= 0
        ):
            raise ValueError("context anchor create_time_ms must be positive")


@dataclass(frozen=True, slots=True)
class FeishuScope:
    app_id: str
    chat_id: str
    kind: ScopeKind
    topic_id: str | None = None

    def __post_init__(self) -> None:
        if not self.app_id or not self.chat_id:
            raise ValueError("app_id and chat_id are required")
        if self.kind is ScopeKind.TOPIC and not self.topic_id:
            raise ValueError("a topic scope requires topic_id")
        if self.kind is not ScopeKind.TOPIC and self.topic_id is not None:
            raise ValueError("only a topic scope may carry topic_id")

    @property
    def key(self) -> str:
        parts = (self.app_id, self.kind.value, self.chat_id, self.topic_id or "")
        return "scope:v1:" + ":".join(quote(part, safe="") for part in parts)


@dataclass(frozen=True, slots=True)
class PromptInput:
    scope: FeishuScope
    source_id: str
    sender_id: str
    text: str
    skill_names: tuple[str, ...] = ()


class NativeCapability(str, Enum):
    SKILLS = "skills"
    GOAL = "goal"
    SIDE = "side"
    RELEASE = "release"
    DELETE = "delete"


class ControlName(str, Enum):
    MENU = "menu"
    NEW = "new"
    SIDE = "side"
    CONFIG = "config"
    COMPACT = "compact"
    SETTINGS = "settings"
    SESSIONS = "sessions"
    RESUME = "resume"
    RENAME = "rename"
    ARCHIVE = "archive"
    DELETE = "delete"
    UNARCHIVE = "unarchive"
    STOP = "stop"
    RELEASE = "release"
    STATUS = "status"
    GOAL = "goal"
    HELP = "help"


class SettingsSection(str, Enum):
    PROJECTS = "projects"


@dataclass(frozen=True, slots=True)
class ControlIntent:
    scope: FeishuScope
    source_id: str
    sender_id: str
    name: ControlName
    arguments: tuple[str, ...] = ()


class CardControlName(str, Enum):
    OPEN_SETTINGS_SECTION = "settings.section.open"
    REFRESH_SETTINGS = "settings.refresh"
    REGISTER_PROJECT = "project.register"
    SET_PROJECT_ENABLED = "project.enabled.set"
    CREATE_BINDING = "binding.create"
    CONFIGURE_BINDING = "binding.configure"
    RENAME_BINDING = "binding.rename"
    ARCHIVE_BINDING = "binding.archive"
    DELETE_BINDING = "binding.delete"
    PREPARE_EXACT_DELETE_BINDING = "binding.delete.exact.prepare"
    DELETE_EXACT_BINDING = "binding.delete.exact"
    UNARCHIVE_BINDING = "binding.unarchive"
    ACTIVATE_BINDING = "binding.activate"
    ARCHIVE_EXACT_BINDING = "binding.archive.exact"
    SESSIONS_PAGE = "sessions.page"
    GOAL_PAUSE = "goal.pause"
    GOAL_RESUME = "goal.resume"
    GOAL_CLEAR = "goal.clear"
    SIDE_CLOSE = "side.close"


@dataclass(frozen=True, slots=True)
class CardControlIntent:
    scope: FeishuScope
    source_id: str
    sender_id: str
    name: CardControlName
    settings_section: SettingsSection | None = None
    project_alias: str | None = None
    expected_revision: int | None = None
    expected_settings_revision: int | None = None
    expected_context_revision: int | None = None
    feedback_revision: int | None = None
    enabled: bool | None = None
    project_path: str | None = None
    create_directory: bool | None = None
    binding_id: str | None = None
    expected_active_binding_id: str | None = None
    expected_native_thread_id: str | None = None
    thread_name: str | None = None
    model_id: str | None = None
    effort_id: str | None = None
    service_tier_id: str | None = None
    task_reactions_enabled: bool | None = None
    progress_card_enabled: bool | None = None
    message_context_mode: MentionContextMode | None = None
    side_id: str | None = None
    page: int | None = None


class TurnFileActionName(str, Enum):
    PAGE = "turn-file.page"
    SEND = "turn-file.send"


@dataclass(frozen=True, slots=True)
class TurnFileManifestItem:
    path: str
    label: str


@dataclass(frozen=True, slots=True)
class TurnProgressManifestStep:
    step: str
    status: str


@dataclass(frozen=True, slots=True)
class TurnProgressManifest:
    """Sanitized, self-contained progress panel carried by file pagination."""

    state: str
    steer_count: int
    plan_available: bool
    plan_generated: bool
    plan_may_be_stale: bool
    steps: tuple[TurnProgressManifestStep, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnFileActionIntent:
    scope: FeishuScope
    source_id: str
    sender_id: str
    name: TurnFileActionName
    binding_id: str
    turn_id: str
    page: int | None = None
    path: str | None = None
    files: tuple[TurnFileManifestItem, ...] = ()
    answer: str | None = None
    progress: TurnProgressManifest | None = None


ChannelInteraction = PromptInput | ControlIntent
