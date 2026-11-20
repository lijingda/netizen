"""Native backend identifiers supported by this program version."""

from collections.abc import Mapping
from dataclasses import dataclass

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


# These are operations, so an allowed form or page can still reject an
# unsupported operation requested by an old card or direct API client.
_CLAUDE_OPERATIONS = frozenset({
    "help", "prompt", "new", "stop", "recheck", "status", "list_bindings",
    "activate_binding", "projects", "worktrees", "context", "reactions",
    "admin", "enable_group", "cleanup_local_resources",
})


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
        if self.backend == "claude" and operation not in _CLAUDE_OPERATIONS:
            return f"Claude 试点暂不支持【{label}】。"
        return None

    def sender_refusal(self, sender_id: str | None, *, bare_help: bool = False) -> str | None:
        if self.backend == "claude" and (not sender_id or (sender_id != self.owner_id and not bare_help)):
            return "这个 Claude 实例仅限所有者使用。"
        return None
