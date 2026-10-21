"""Instance-wide progress card defaults saved in the existing instance YAML."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from .admin.port_config import ConfigFileEditor
from .progress_card_settings import ProgressCardSettings


class ProgressCardConfigError(ValueError):
    pass


class ProgressCardConfigConflict(ProgressCardConfigError):
    pass


class ProgressCardConfigService:
    def __init__(self, settings: ProgressCardSettings = ProgressCardSettings(), *,
                 config_editor: ConfigFileEditor | None = None) -> None:
        self._snapshot = (settings, uuid.uuid4().hex)
        self._editor = config_editor
        # Readers use the last published snapshot without waiting for file I/O.
        self._write_lock = threading.Lock()

    @property
    def current(self) -> ProgressCardSettings:
        return self._snapshot[0]

    def view(self) -> dict[str, Any]:
        settings, revision = self._snapshot
        return {"settings": settings.to_dict(), "revision": revision,
                "editable": self._editor is not None}

    def update(self, settings: ProgressCardSettings, *, expected_revision: str) -> None:
        with self._write_lock:
            if expected_revision != self._snapshot[1]:
                raise ProgressCardConfigConflict("progress card settings changed; refresh before saving")
            if self._editor is None:
                raise ProgressCardConfigError("instance configuration is unavailable")
            self._editor.update_section("progressCard", settings.to_dict())
            self._snapshot = (settings, uuid.uuid4().hex)
