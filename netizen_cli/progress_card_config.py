"""Instance-wide progress card defaults saved in the existing instance YAML."""

from __future__ import annotations

from typing import Any

from .admin.port_config import ConfigSection, ConfigFileEditor
from .progress_card_settings import ProgressCardSettings


class ProgressCardConfigService(ConfigSection[ProgressCardSettings]):
    def __init__(self, settings: ProgressCardSettings = ProgressCardSettings(), *,
                 config_editor: ConfigFileEditor | None = None) -> None:
        super().__init__(settings, section="progressCard", editor=config_editor,
                         conflict_message="progress card settings changed; refresh before saving")

    def view(self) -> dict[str, Any]:
        settings, revision = self.snapshot()
        return {"settings": settings.to_dict(), "revision": revision,
                "editable": self.editable}
