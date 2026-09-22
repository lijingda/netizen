"""Card payload inspection and callback builders shared by Channel tests."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from lark_channel import OutboundCard


def _elements(value: object, tag: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(_elements(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(_elements(child, tag))
    return found


def _card_button_value(card: OutboundCard, label: str) -> dict[str, object]:
    values = _card_button_values(card, label)
    if values:
        return values[0]
    raise AssertionError(f"button not found: {label}")


def _card_button_values(card: OutboundCard, label: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for button in _elements(card.card, "button"):
        text = button.get("text")
        if isinstance(text, dict) and text.get("content") == label:
            behaviors = button.get("behaviors")
            if isinstance(behaviors, list) and len(behaviors) == 1:
                behavior = behaviors[0]
                if isinstance(behavior, dict):
                    value = behavior.get("value")
                    if isinstance(value, dict):
                        values.append(value)
    return values


def direct_card_event(
    channel,
    form_value: dict[str, object],
    *,
    message_id: str = "om_card",
) -> object:
    channel.fetched_messages[message_id] = {
        "data": {"items": [{"chat_id": "oc_direct", "thread_id": None}]}
    }
    channel.chat_types["oc_direct"] = "p2p"
    return SimpleNamespace(
        message_id=message_id,
        chat_id="oc_direct",
        operator=SimpleNamespace(open_id="ou_user"),
        action=SimpleNamespace(
            tag="button",
            value={},
            form_value=form_value,
        ),
    )


def direct_button_event(
    value: dict[str, object],
    *,
    message_id: str = "om_card",
    form_value: dict[str, object] | None = None,
) -> object:
    return SimpleNamespace(
        message_id=message_id,
        chat_id="oc_direct",
        operator=SimpleNamespace(open_id="ou_user"),
        action=SimpleNamespace(
            tag="button",
            value=value,
            form_value=form_value,
        ),
    )


def new_form_values(
    card: OutboundCard,
    *,
    project_alias: str = "test",
) -> dict[str, object]:
    form = next(
        item
        for item in _elements(card.card, "form")
        if item["name"] == "new_binding_v6"
    )
    fields = {
        item["name"]: item
        for item in form["elements"]
        if "name" in item
    }
    project_reference = next(
        option["value"]
        for option in fields["new_project"]["options"]
        if option["text"]["content"].startswith(f"{project_alias} ·")
    )
    values: dict[str, object] = {"new_project": project_reference}
    for name in (
        "new_context_mode",
        "new_model",
        "new_effort",
        "new_speed",
        "new_task_reactions",
        "new_progress_card",
        "new_completion_mention",
    ):
        if name in fields:
            values[name] = fields[name]["initial_option"]
    return values


def config_form_values(
    card: OutboundCard,
    *,
    effort_id: str | None = None,
    speed_id: str | None = None,
    inherit: bool = False,
    reaction_pulse_enabled: bool | None = None,
    progress_card_enabled: bool | None = None,
    completion_mention_enabled: bool | None = None,
) -> dict[str, object]:
    form = next(
        item
        for item in _elements(card.card, "form")
        if item["name"] == "binding_config_v6"
    )
    fields = {
        item["name"]: item
        for item in form["elements"]
        if "name" in item
    }
    model_field = fields["config_model"]
    model_value = model_field["initial_option"]
    if not inherit:
        model_value = next(
            (
                option["value"]
                for option in model_field["options"]
                if ":explicit:" in option["value"]
            ),
            model_value,
        )
    values = {"config_model": model_value}
    values["config_task_reactions"] = fields["config_task_reactions"][
        "initial_option"
    ]
    values["config_progress_card"] = fields["config_progress_card"][
        "initial_option"
    ]
    values["config_completion_mention"] = fields["config_completion_mention"][
        "initial_option"
    ]
    for name, enabled in (
        ("config_task_reactions", reaction_pulse_enabled),
        ("config_progress_card", progress_card_enabled),
        ("config_completion_mention", completion_mention_enabled),
    ):
        if enabled is not None:
            suffix = ":on" if enabled else ":off"
            values[name] = next(
                option["value"]
                for option in fields[name]["options"]
                if option["value"].endswith(suffix)
            )
    if "config_context_mode" in fields:
        values["config_context_mode"] = fields["config_context_mode"][
            "initial_option"
        ]
    if "config_effort" in fields:
        values["config_effort"] = (
            effort_id or fields["config_effort"]["initial_option"]
        )
    if "config_speed" in fields:
        values["config_speed"] = (
            speed_id or fields["config_speed"]["initial_option"]
        )
    return values


elements = _elements


def callback(card, label):
    return next(button["behaviors"][0]["value"] for button in elements(card.card, "button")
                if button.get("text", {}).get("content") == label)


def form_values(card):
    form = elements(card.card, "form")[0]
    result = {}
    for item in sum((elements(form, tag) for tag in ("input", "select_static", "multi_select_static", "date_picker", "picker_time")), []):
        if item["tag"] == "input":
            result[item["name"]] = item.get("default_value", "")
        elif item["tag"] == "select_static":
            result[item["name"]] = item.get("initial_option", "")
        elif item["tag"] == "multi_select_static":
            result[item["name"]] = item.get("selected_values", [])
        elif item["tag"] == "date_picker":
            result[item["name"]] = item.get("initial_date", "")
        elif item["tag"] == "picker_time":
            result[item["name"]] = item.get("initial_time", "")
    return result


def option_value(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def manager_form(card, form_name, *, option=None, plan_id=None):
    form = next(item for item in elements(card.card, "form") if item["name"] == form_name)
    select = elements(form, "select_static")[0]
    value = select.get("initial_option", "")
    if plan_id is not None:
        value = next(item["value"] for item in select["options"] if option_value(item["value"]).get("plan_id") == plan_id)
    elif option is not None:
        value = next(item["value"] for item in select["options"] if item["value"] == option or option_value(item["value"]).get("filter") == option)
    return {select["name"]: value}
