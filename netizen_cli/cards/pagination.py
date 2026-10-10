"""Reusable page selection inside a caller-owned Card 2.0 form."""

from __future__ import annotations

from typing import Any

from .callbacks import CardActionError, _plain_text


def decode_page_selection(value: Any, total_pages: int) -> int:
    """Accept only the exact ASCII option values advertised by the control."""
    if not isinstance(value, str) or value not in {
        str(index) for index in range(total_pages)
    }:
        raise CardActionError("页码超出范围。")
    return int(value)


def pagination_controls(
    *, page_field: str, page: int, total_pages: int, button: dict[str, Any],
) -> dict[str, Any] | None:
    """Render one selector and the sole caller-supplied manifest submit button.

    The owner decides the form boundary, payload, and capacity limit. Options
    carry only zero-based page strings; the current page is the footer label.
    """
    if total_pages <= 1:
        return None
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "elements": [{
                    "tag": "select_static",
                    "name": page_field,
                    "required": True,
                    "initial_option": str(page),
                    "placeholder": _plain_text("选择页码"),
                    "options": [
                        {"text": _plain_text(f"第{index + 1}页"), "value": str(index)}
                        for index in range(total_pages)
                    ],
                }],
            },
            {"tag": "column", "width": "auto", "elements": [button]},
        ],
    }
