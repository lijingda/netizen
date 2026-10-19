"""Instance-owned managed Worktree settings in the existing instance YAML."""
from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..admin.port_config import ConfigFileEditor, ConfigFileSnapshot


class WorktreeSettingsError(ValueError):
    pass


class WorktreeSettingsConflict(WorktreeSettingsError):
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


class WorktreeSettingsService:
    def __init__(self, settings: WorktreeSettings = WorktreeSettings(), *, default_root: Path,
                 config_snapshot: ConfigFileSnapshot | None = None,
                 config_editor: ConfigFileEditor | None = None) -> None:
        if not default_root.is_absolute():
            raise WorktreeSettingsError("default worktree root must be absolute")
        self._settings = settings
        self._default_root = default_root
        self._editor = config_editor or (ConfigFileEditor(config_snapshot) if config_snapshot else None)
        self._revision = uuid.uuid4().hex
        self._lock = threading.RLock()

    @property
    def revision(self) -> str:
        return self._revision

    @property
    def editable(self) -> bool:
        return self._editor is not None

    @property
    def root(self) -> Path:
        with self._lock:
            return self._settings.root or self._default_root

    def snapshot(self) -> tuple[WorktreeSettings, str]:
        with self._lock:
            return self._settings, self._revision

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {"revision": self._revision, "root": str(self._settings.root) if self._settings.root else "",
                    "effectiveRoot": str(self.root), "fetchBeforeCreate": self._settings.fetch_before_create,
                    "editable": self.editable}

    def update(self, settings: WorktreeSettings, *, expected_revision: str) -> None:
        with self._lock:
            if expected_revision != self._revision:
                raise WorktreeSettingsConflict("worktree settings changed; refresh before saving")
            if self._editor is None:
                raise WorktreeSettingsError("instance configuration is unavailable")
            self._editor.update_section("worktrees", settings.to_dict())
            self._settings = settings
            self._revision = uuid.uuid4().hex
