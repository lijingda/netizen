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


class ActiveState(str, Enum):
    RUNNING = "running"
    STOPPING = "stopping"
    OBSERVATION_UNAVAILABLE = "turn-observation-unavailable"


ACTIVE_STATE_VALUES: frozenset[str] = frozenset(state.value for state in ActiveState)


class GoalOperationState(str, Enum):
    STARTING = "goal-starting"
    RUNNING = "goal-running"
    PAUSING = "goal-pausing"
    EXTERNAL_ACTIVE = "externally-active-goal"
    UNKNOWN = "goal-unknown"


class GoalStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    USAGE_LIMITED = "usageLimited"
    BUDGET_LIMITED = "budgetLimited"
    COMPLETE = "complete"

    @property
    def terminal_or_paused(self) -> bool:
        return self is not GoalStatus.ACTIVE


SESSION_IDLE_STATE = "idle"


def persisted_goal_session_state(status: GoalStatus) -> str:
    """Project one typed persisted Goal status into the sessions state space."""

    return f"goal-{status.value}"


SESSION_STOP_ACTION_STATES: frozenset[str] = frozenset(
    {
        ActiveState.RUNNING.value,
        ActiveState.STOPPING.value,
        ActiveState.OBSERVATION_UNAVAILABLE.value,
        GoalOperationState.RUNNING.value,
        GoalOperationState.PAUSING.value,
    }
)


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
    PREPARE_ARCHIVED_DELETE_BINDING = "binding.delete.archived.prepare"
    DELETE_ARCHIVED_BINDING = "binding.delete.archived"
    UNARCHIVE_BINDING = "binding.unarchive"
    ACTIVATE_BINDING = "binding.activate"
    ARCHIVE_EXACT_BINDING = "binding.archive.exact"
    STOP_EXACT_BINDING = "binding.stop.exact"
    RECHECK_EXACT_TURN = "binding.turn.recheck"
    SESSIONS_PAGE = "sessions.page"
    REFRESH_ARCHIVED_SESSIONS = "sessions.archived.refresh"
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
    expected_activity_revision: int | None = None
    expected_turn_id: str | None = None
    thread_name: str | None = None
    model_id: str | None = None
    effort_id: str | None = None
    service_tier_id: str | None = None
    reaction_pulse_enabled: bool | None = None
    progress_card_enabled: bool | None = None
    message_context_mode: MentionContextMode | None = None
    side_id: str | None = None
    page: int | None = None
    goal_generation: str | None = None
    expected_goal_status: str | None = None


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
class TurnActivityManifestEntry:
    kind: str
    status: str
    text: str | None = None
    count: int = 1


@dataclass(frozen=True, slots=True)
class TurnProgressManifest:
    """Sanitized, self-contained progress panel carried by file pagination."""

    state: str
    steer_count: int
    plan_available: bool
    plan_generated: bool
    plan_may_be_stale: bool
    steps: tuple[TurnProgressManifestStep, ...] = ()
    commentary: tuple[str, ...] = ()
    operations: tuple[TurnActivityManifestEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplyCardGoalModule:
    """Frozen, display-only Goal control projection for one Goal generation."""

    binding_id: str
    short_id: str
    project_alias: str
    goal_generation: str | None
    status: str | None
    runtime_state: str | None
    objective: str | None
    token_budget: int | None
    tokens_used: int
    notice: str | None = None
    notice_is_error: bool = False


@dataclass(frozen=True, slots=True)
class ReplyCardActivityModule:
    """Bounded, sanitized Turn activity plus its presentation state."""

    progress: TurnProgressManifest
    terminal_status: str | None = None
    collapsed: bool = False
    hidden_steps: int = 0


@dataclass(frozen=True, slots=True)
class ReplyCardResultModule:
    content: str


@dataclass(frozen=True, slots=True)
class ReplyCardFileItem:
    """One current file view; callbacks carry only path and display label."""

    path: str
    label: str
    size: int | None
    media_kind: str | None


@dataclass(frozen=True, slots=True)
class ReplyCardFilesModule:
    binding_id: str
    turn_id: str
    items: tuple[ReplyCardFileItem, ...]
    page: int = 0
    action_version: int = 5


@dataclass(frozen=True, slots=True)
class ReplyCardManifest:
    """Frozen non-file modules carried by a self-contained page callback."""

    goal: ReplyCardGoalModule | None = None
    activity: ReplyCardActivityModule | None = None
    result: ReplyCardResultModule | None = None


@dataclass(frozen=True, slots=True)
class ReplyCardProjection:
    """Closed Reply Card module set rendered atomically as one Card 2.0 body."""

    scope: FeishuScope | None = None
    goal: ReplyCardGoalModule | None = None
    activity: ReplyCardActivityModule | None = None
    result: ReplyCardResultModule | None = None
    files: ReplyCardFilesModule | None = None


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
    reply: ReplyCardManifest | None = None


ChannelInteraction = PromptInput | ControlIntent
