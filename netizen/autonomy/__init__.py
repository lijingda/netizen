"""Experimental autonomous message selection; remove with its few host hooks."""

from .models import AutonomyConflict, AutonomyError, Candidate, Context, Decision, DecisionConfig, DecisionToken, Settings
from .store import AutonomyStore, create_schema, require_schema

__all__ = [
    "AutonomyConflict", "AutonomyError", "AutonomyService", "AutonomyStore", "Candidate",
    "CodexSummarizer", "Context", "Decision", "DecisionConfig", "DecisionToken", "Settings", "create_schema", "require_schema",
]


def __getattr__(name: str):
    # Deployment inventory can import BindingStore before installing runtime
    # dependencies. Keep schema/model imports stdlib-only; resolve optional
    # network and native helpers only when the application assembles them.
    if name == "AutonomyService":
        from .service import AutonomyService
        return AutonomyService
    if name == "CodexSummarizer":
        from .summary import CodexSummarizer
        return CodexSummarizer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
