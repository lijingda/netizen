"""Instance-owned managed Worktree settings in the existing instance YAML."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..admin.port_config import ConfigSection, ConfigFileEditor, ConfigFileSnapshot


class WorktreeSettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorktreeSettings:
    root: Path | None = None
    fetch_before_create: bool = False

    def __post_init__(self) -> None:
        if self.root is not None and (
            not isinstance(self.root, Path) or not self.root.is_absolute()
            or any(ord(character) < 32 for character in str(self.root))
        ):
            raise WorktreeSettingsError("worktrees.root must be an absolute path")
        if not isinstance(self.fetch_before_create, bool):
            raise WorktreeSettingsError("worktrees.fetchBeforeCreate must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {**({"root": str(self.root)} if self.root is not None else {}),
                "fetchBeforeCreate": self.fetch_before_create}

    @classmethod
    def from_mapping(cls, value: object) -> WorktreeSettings:
        if not isinstance(value, Mapping) or value.keys() - {"root", "fetchBeforeCreate"}:
            raise WorktreeSettingsError("worktrees must contain only root and fetchBeforeCreate")
        raw = value.get("root", "")
        if not isinstance(raw, str) or raw != raw.strip():
            raise WorktreeSettingsError("worktrees.root must be an absolute path or empty")
        return cls(Path(raw).expanduser() if raw else None, value.get("fetchBeforeCreate", False))


class WorktreeSettingsService(ConfigSection[WorktreeSettings]):
    def __init__(self, settings: WorktreeSettings = WorktreeSettings(), *, default_root: Path,
                 config_snapshot: ConfigFileSnapshot | None = None,
                 config_editor: ConfigFileEditor | None = None) -> None:
        if not default_root.is_absolute():
            raise WorktreeSettingsError("default worktree root must be absolute")
        self._default_root = default_root
        super().__init__(settings, section="worktrees",
                         editor=config_editor or (ConfigFileEditor(config_snapshot) if config_snapshot else None),
                         conflict_message="worktree settings changed; refresh before saving")

    @property
    def root(self) -> Path:
        return self.resolve_root(self.current)

    def resolve_root(self, settings: WorktreeSettings) -> Path:
        return settings.root or self._default_root

    def view(self) -> dict[str, Any]:
        settings, revision = self.snapshot()
        return {"revision": revision, "root": str(settings.root) if settings.root else "",
                "effectiveRoot": str(self.resolve_root(settings)),
                "fetchBeforeCreate": settings.fetch_before_create, "editable": self.editable}
