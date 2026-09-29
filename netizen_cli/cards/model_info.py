"""Read-only model catalog information, with no selection callbacks or state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..model_settings import ModelCatalog, ModelOption
from .callbacks import _plain, _plain_text
from .reply import TURN_FILE_CARD_JSON_LIMIT_BYTES


def _text(value: object, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…（已省略）"


def _model_description(model: ModelOption) -> str:
    lines = [_text(model.description) or "模型目录未提供简介。"]
    if model.input_modalities:
        labels = {"text": "文字", "image": "图片", "audio": "音频"}
        lines.append("支持输入：" + "、".join(
            labels.get(item, _text(item, 40)) for item in model.input_modalities
        ))
    info = model.upgrade_info
    upgrade = _text(info.model if info is not None else model.upgrade, 120)
    if upgrade:
        lines.append("建议迁移至：" + upgrade + "（不会自动切换）")
    if info is not None:
        if copy := _text(info.upgrade_copy):
            lines.append(copy)
        if isinstance(info.retirement_at, int) and not isinstance(info.retirement_at, bool):
            try:
                retirement = datetime.fromtimestamp(info.retirement_at, timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
            else:
                lines.append("目录提示的退役时间：" + retirement.strftime("%Y-%m-%d %H:%M UTC"))
        if migration := _text(info.migration_markdown, 1000):
            lines.append("迁移说明：" + migration)
        if link := _text(info.model_link, 500):
            lines.append("详情：" + link)
    # Catalog prose is plain text: HTML, Markdown and mention-like strings
    # must never become interactive card markup or callback payloads.
    return "\n\n".join(lines)


def with_model_details(card: dict[str, Any], catalog: ModelCatalog | None) -> dict[str, Any]:
    """Append bounded optional information without changing the original form."""
    if catalog is None or not catalog.models:
        return card
    note = _plain("以下为打开卡片时的模型目录说明；展开对应模型查阅，不会改变选择或保存配置。")
    panel: dict[str, Any] = {
        "tag": "collapsible_panel", "expanded": False,
        "header": {"title": _plain_text("模型介绍与迁移提示")},
        "elements": [note],
    }
    card["body"]["elements"].append(panel)
    shown = 0
    for model in catalog.models[:40]:
        entry = {
            "tag": "collapsible_panel", "expanded": False,
            "header": {"title": _plain_text(_text(model.display_name, 100))},
            "elements": [_plain(_model_description(model))],
        }
        panel["elements"].append(entry)
        # Reserve space for the omission notice. Presentation cannot prevent
        # users from receiving or submitting the original model picker.
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > TURN_FILE_CARD_JSON_LIMIT_BYTES - 600:
            panel["elements"].pop()
            break
        shown += 1
    if shown < len(catalog.models):
        panel["elements"].append(_plain("卡片空间有限，部分模型介绍未展示；模型选项和保存逻辑不受影响。"))
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > TURN_FILE_CARD_JSON_LIMIT_BYTES:
        card["body"]["elements"].pop()
    return card
