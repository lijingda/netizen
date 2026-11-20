"""Native backend identifiers supported by this program version."""

from collections.abc import Mapping

DEFAULT_BACKEND = "codex"
KNOWN_BACKENDS = (DEFAULT_BACKEND,)


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
