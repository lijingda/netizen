"""Card 2.0 rendering and strict callback decoding for Channel controls."""

from __future__ import annotations

import base64
import html
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lark_channel import OutboundCard, new_card

from .bindings import BindingTurnSettings, SideTopicState
from .domain import (
    CardControlIntent,
    CardControlName,
    FeishuScope,
    SettingsSection,
    ScopeKind,
    TurnFileActionIntent,
    TurnFileActionName,
    TurnFileManifestItem,
)
from .model_settings import ModelCatalog, TurnModelSettings
from .projects import Project
from .sdk_gap_adapter import GoalSnapshot, GoalStatus
from .turn_files import (
    TurnFile,
    TurnFilePage,
    format_file_size,
    inspect_turn_file_path,
    paginate_turn_files,
)


ACTION_VERSION = 3
TURN_FILE_ACTION_VERSION = 4
TURN_FILE_MANIFEST_LIMIT = 500
TURN_FILE_CARD_JSON_LIMIT_BYTES = 55_000
_TURN_ANSWER_ELEMENT_ID = "turnanswerv1"
_TURN_FILES_ELEMENT_ID = "turnfilesv4"
MAX_PROJECT_ROWS = 12
MAX_THREAD_NAME_CHARS = 120
_PROJECT_REFERENCE = re.compile(
    r"project:v1:([a-z0-9][a-z0-9_-]{0,63}):([1-9][0-9]*)"
)
_BINDING_REFERENCE = re.compile(r"binding:v1:([A-Za-z0-9][A-Za-z0-9-]{0,127})")
_CONFIG_MODEL_REFERENCE = re.compile(
    r"config-model:v2:([A-Za-z0-9][A-Za-z0-9-]{0,127}):"
    r"([1-9][0-9]*):([A-Za-z0-9_-]+)"
)
_RENAME_NAME_FIELD = re.compile(
    r"rename_name_v1__([A-Za-z0-9][A-Za-z0-9-]{0,127})"
)
_SIDE_REFERENCE = re.compile(r"side:v1:([A-Za-z0-9][A-Za-z0-9-]{0,127})")
_TURN_REFERENCE = re.compile(
    r"turn:v1:([A-Za-z0-9][A-Za-z0-9._-]{0,191})"
)


@dataclass(frozen=True, slots=True)
class ArchivedSessionCardItem:
    binding_id: str
    short_id: str
    project_alias: str
    native_thread_id: str
    title: str


class CardActionError(ValueError):
    pass


class TurnFileCardLimitError(CardActionError):
    pass


class SettingsCardActionError(CardActionError):
    def __init__(
        self,
        message: str,
        *,
        scope: FeishuScope,
        section: SettingsSection,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.section = section


def turn_files_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    final_response: str,
    files: tuple[TurnFile, ...],
    page: int = 0,
) -> OutboundCard:
    if not files:
        raise CardActionError("本轮文件当前已不可用。")
    if len(files) > TURN_FILE_MANIFEST_LIMIT:
        raise TurnFileCardLimitError(
            f"本轮文件共 {len(files)} 个，超过卡片完整分页上限 "
            f"{TURN_FILE_MANIFEST_LIMIT} 个；未截断文件清单。"
        )
    manifest = tuple(
        TurnFileManifestItem(
            path=str(turn_file.resolved_path),
            label=turn_file.display_path,
        )
        for turn_file in files
    )
    return _render_and_validate_turn_file_pages(
        scope=scope,
        binding_id=binding_id,
        turn_id=turn_id,
        final_response=final_response,
        files=files,
        manifest=manifest,
        page=page,
    )


def _render_and_validate_turn_file_pages(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    final_response: str,
    files: tuple[TurnFile, ...],
    manifest: tuple[TurnFileManifestItem, ...],
    page: int,
) -> OutboundCard:
    """Reject an initial card unless every advertised page fits."""

    requested = paginate_turn_files(files, page)
    selected: OutboundCard | None = None
    for candidate_page in range(requested.total_pages):
        candidate = _render_turn_files_card(
            scope=scope,
            binding_id=binding_id,
            turn_id=turn_id,
            final_response=final_response,
            files=files,
            manifest=manifest,
            page=candidate_page,
        )
        if candidate_page == page:
            selected = candidate
    assert selected is not None
    return selected


def turn_files_card_from_manifest(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    final_response: str,
    manifest: tuple[TurnFileManifestItem, ...],
    page: int,
) -> OutboundCard:
    """Rebuild a v4 card using only state carried by its page callback."""

    if not manifest:
        raise CardActionError("本轮文件清单为空，请重新执行任务。")
    if len(manifest) > TURN_FILE_MANIFEST_LIMIT:
        raise TurnFileCardLimitError(
            f"本轮文件共 {len(manifest)} 个，超过卡片完整分页上限 "
            f"{TURN_FILE_MANIFEST_LIMIT} 个；未截断文件清单。"
        )
    files = tuple(
        inspect_turn_file_path(entry.path, entry.label) for entry in manifest
    )
    return _render_turn_files_card(
        scope=scope,
        binding_id=binding_id,
        turn_id=turn_id,
        final_response=final_response,
        files=files,
        manifest=manifest,
        page=page,
    )


def _render_turn_files_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    final_response: str,
    files: tuple[TurnFile, ...],
    manifest: tuple[TurnFileManifestItem, ...],
    page: int,
) -> OutboundCard:
    visible = paginate_turn_files(files, page)
    subtitle = (
        f"本轮文件 {visible.total_items} 个 · "
        f"第 {visible.page + 1}/{visible.total_pages} 页"
    )
    builder = (
        new_card()
        .config(
            update_multi=True,
            width_mode="default",
            summary={"content": f"任务已完成 · 本轮文件 {visible.total_items} 个"},
        )
        .header(
            "任务已完成",
            subtitle=subtitle,
            template="green",
            icon={"tag": "standard_icon", "token": "todo_colorful"},
        )
    )
    builder.raw(_turn_answer_block(final_response))
    builder.raw(
        _turn_files_block(
            scope=scope,
            binding_id=binding_id,
            turn_id=turn_id,
            page=visible,
            manifest=manifest,
            final_response=final_response,
        )
    )
    card = builder.to_dict()
    body = card.get("body")
    if isinstance(body, dict):
        body.update(
            {
                "direction": "vertical",
                "padding": "12px 12px 20px 12px",
                "vertical_spacing": "12px",
            }
        )
    # Match the Channel SDK's actual outbound Card serialization rather than
    # undercounting with compact separators.
    encoded_size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    if encoded_size > TURN_FILE_CARD_JSON_LIMIT_BYTES:
        raise TurnFileCardLimitError(
            "本轮文件卡片编码后为 "
            f"{encoded_size} bytes，超过已验证的平台安全上限 "
            f"{TURN_FILE_CARD_JSON_LIMIT_BYTES} bytes；未截断文件清单。"
        )
    return OutboundCard(card=card)


def is_turn_file_action(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return value.get("intent") in {item.value for item in TurnFileActionName}


def decode_turn_file_action(
    *,
    app_id: str,
    message_id: str,
    callback_chat_id: str,
    sender_id: str,
    tag: str,
    value: Any,
) -> TurnFileActionIntent:
    if tag != "button":
        raise CardActionError(f"不支持的本轮文件组件：{tag or 'unknown'}")
    if not message_id or not callback_chat_id or not sender_id:
        raise CardActionError("本轮文件回调缺少消息、聊天或操作者标识。")
    if not isinstance(value, Mapping):
        raise CardActionError("本轮文件动作 value 必须是对象。")
    payload = dict(value)
    try:
        name = TurnFileActionName(payload.get("intent"))
    except (TypeError, ValueError) as error:
        raise CardActionError("未知本轮文件动作。") from error
    version = payload.get("v")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != TURN_FILE_ACTION_VERSION
    ):
        raise CardActionError("本轮文件卡片已过期，请重新执行任务。")
    try:
        scope_kind = ScopeKind(payload.get("scope_kind"))
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    common = {
        "v",
        "intent",
        "chat_id",
        "scope_kind",
        "binding_id",
        "turn_id",
    }
    scope_fields = {"topic_id"} if scope_kind is ScopeKind.TOPIC else set()
    action_fields = (
        {"page", "files", "answer"}
        if name is TurnFileActionName.PAGE
        else {"path"}
    )
    if set(payload) != common | scope_fields | action_fields:
        raise CardActionError("本轮文件动作字段不完整或包含未知字段。")
    if payload["chat_id"] != callback_chat_id:
        raise CardActionError("本轮文件卡片与当前聊天不一致。")
    scope = _scope_from_envelope(
        app_id=app_id,
        chat_id=callback_chat_id,
        kind=scope_kind,
        topic_id=payload.get("topic_id"),
    )
    binding_id = _decode_binding_reference(
        _required_string(payload["binding_id"], "binding_id")
    )
    turn_id = _decode_turn_reference(
        _required_string(payload["turn_id"], "turn_id")
    )
    page = None
    path = None
    files: tuple[TurnFileManifestItem, ...] = ()
    answer = None
    if name is TurnFileActionName.PAGE:
        raw_page = payload["page"]
        if (
            isinstance(raw_page, bool)
            or not isinstance(raw_page, int)
            or raw_page < 0
        ):
            raise CardActionError("本轮文件页码必须是非负整数。")
        page = raw_page
        files = _decode_turn_file_manifest(payload["files"])
        answer = _required_string(payload["answer"], "answer")
        if len(answer) > 100_000 or "\x00" in answer:
            raise CardActionError("本轮文件卡片回答内容无效。")
    else:
        path = _decode_turn_file_path(payload["path"])
    return TurnFileActionIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=name,
        binding_id=binding_id,
        turn_id=turn_id,
        page=page,
        path=path,
        files=files,
        answer=answer,
    )


def _decode_turn_file_manifest(value: Any) -> tuple[TurnFileManifestItem, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > TURN_FILE_MANIFEST_LIMIT
    ):
        raise CardActionError(
            f"本轮文件清单必须包含 1–{TURN_FILE_MANIFEST_LIMIT} 个文件。"
        )
    results: list[TurnFileManifestItem] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "label"}:
            raise CardActionError("本轮文件清单条目字段无效。")
        path = _decode_turn_file_path(item["path"])
        label = _required_string(item["label"], "label")
        if len(label) > 1024 or "\x00" in label:
            raise CardActionError("本轮文件显示名称无效。")
        if path in seen:
            raise CardActionError("本轮文件清单包含重复路径。")
        seen.add(path)
        results.append(TurnFileManifestItem(path=path, label=label))
    return tuple(results)


def _decode_turn_file_path(value: Any) -> str:
    path = _required_string(value, "path")
    if len(path) > 8192 or "\x00" in path or not Path(path).is_absolute():
        raise CardActionError("本轮文件路径必须是有效的绝对路径。")
    return path


def settings_card(
    *,
    scope: FeishuScope,
    projects: tuple[Project, ...],
    project_root: str,
    section: SettingsSection = SettingsSection.PROJECTS,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    builder = _builder("Netizen 设置", _settings_section_label(section))
    builder.raw(_settings_navigation(scope=scope, active=section))
    builder.markdown(
        "当前 Scope 的参与者均可操作；实例级设置会影响其他 Scope。"
    )
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))

    if section is SettingsSection.PROJECTS:
        _render_projects_settings(
            builder,
            scope=scope,
            projects=projects,
            project_root=project_root,
        )
    else:  # pragma: no cover - enum and renderer must evolve together
        raise ValueError(f"unsupported settings section: {section}")

    return OutboundCard(card=builder.to_dict())


def _render_projects_settings(
    builder: Any,
    *,
    scope: FeishuScope,
    projects: tuple[Project, ...],
    project_root: str,
) -> None:
    builder.markdown(
        "**实例级 Project Registry**\n"
        "Project 对整个 Netizen 实例共享。"
    )
    system_project = next(
        (project for project in projects if project.alias == "none"),
        None,
    )
    if system_project is not None:
        builder.markdown(
            "**系统 Project**\n"
            f"`none` · 始终启用 · `{_md_code(str(system_project.cwd))}`"
        )

    manageable = tuple(project for project in projects if project.alias != "none")
    builder.markdown("**管理 Project**")
    if manageable:
        builder.raw(_project_management_form(manageable))
    else:
        builder.markdown("当前没有可管理的 Project，请在下方新增。")

    builder.raw(
        _callback_button(
            label="刷新当前配置",
            value=_envelope(
                scope,
                CardControlName.REFRESH_SETTINGS,
                settings_section=SettingsSection.PROJECTS.value,
            ),
        )
    )
    builder.divider()
    builder.markdown("**新增 Project**")
    builder.raw(_project_create_form(project_root))
    builder.markdown(
        f"<font color='grey'>空 Project 默认创建在 `{_md_code(project_root)}`；"
        "Netizen 从不删除 Project 目录。</font>"
    )


def _settings_navigation(
    *,
    scope: FeishuScope,
    active: SettingsSection,
) -> dict[str, Any]:
    return _button_row(
        *(
            _callback_button(
                label=_settings_section_label(section),
                value=_envelope(
                    scope,
                    CardControlName.OPEN_SETTINGS_SECTION,
                    settings_section=section.value,
                ),
                style="primary_filled" if section is active else "default",
            )
            for section in SettingsSection
        )
    )


def _settings_section_label(section: SettingsSection) -> str:
    if section is SettingsSection.PROJECTS:
        return "Projects"
    raise ValueError(f"unsupported settings section: {section}")


def _project_management_form(projects: tuple[Project, ...]) -> dict[str, Any]:
    return {
        "tag": "form",
        "name": "project_manage_v1",
        "elements": [
            {
                "tag": "select_static",
                "name": "project_manage_target",
                "required": True,
                "placeholder": _plain_text("选择 Project"),
                "options": [
                    {
                        "text": _plain_text(
                            f"{project.alias} · "
                            f"{'已启用' if project.enabled else '已停用'}"
                        ),
                        "value": _project_reference(project),
                    }
                    for project in projects
                ],
            },
            {
                "tag": "select_static",
                "name": "project_manage_operation",
                "required": True,
                "placeholder": _plain_text("选择操作"),
                "options": [
                    {"text": _plain_text("启用"), "value": "enable"},
                    {"text": _plain_text("停用"), "value": "disable"},
                ],
            },
            {
                "tag": "button",
                "name": "project_manage_submit_v1",
                "text": _plain_text("应用到所选 Project"),
                "type": "primary",
                "width": "fill",
                "form_action_type": "submit",
                "confirm": {
                    "title": _plain_text("确认修改 Project？"),
                    "text": _plain_text(
                        "状态会立即更新；停用只阻止创建新会话，已有会话仍可继续。"
                    ),
                },
            },
        ],
    }


def _project_create_form(project_root: str) -> dict[str, Any]:
    return {
        "tag": "form",
        "name": "project_create_v1",
        "elements": [
            {
                "tag": "input",
                "name": "project_alias",
                "required": True,
                "label": _plain_text("Alias"),
                "placeholder": _plain_text("例如：demo_project"),
                "max_length": 64,
            },
            {
                "tag": "div",
                "text": _plain_text("模式"),
            },
            {
                "tag": "select_static",
                "name": "project_mode",
                "required": True,
                "initial_option": "create",
                "options": [
                    {"text": _plain_text("创建空目录"), "value": "create"},
                    {"text": _plain_text("登记已有目录"), "value": "existing"},
                ],
            },
            {
                "tag": "input",
                "name": "project_path",
                "label": _plain_text("绝对路径（创建模式可留空）"),
                "placeholder": _plain_text(str(project_root)),
                "max_length": 1000,
            },
            {
                "tag": "button",
                "name": "project_submit_v1",
                "text": _plain_text("保存 Project"),
                "type": "primary_filled",
                "width": "fill",
                "form_action_type": "submit",
            },
        ],
    }


def _new_binding_form(
    projects: tuple[Project, ...],
    catalog: ModelCatalog,
) -> dict[str, Any]:
    elements = [
        _form_label("Project"),
        _static_select(
            name="new_project",
            placeholder="选择 Project",
            options=tuple(
                (
                    f"{project.alias} · {project.cwd}",
                    _project_reference(project),
                )
                for project in projects
            ),
            initial_option=_project_reference(projects[0]),
        ),
        *_model_settings_form_elements(prefix="new", catalog=catalog),
        _form_submit_button(name="new_binding_submit_v4", label="新建会话"),
    ]
    return {
        "tag": "form",
        "name": "new_binding_v4",
        "elements": elements,
    }


def _binding_config_form(
    *,
    binding_id: str,
    settings_revision: int,
    turn_settings: BindingTurnSettings | None,
    catalog: ModelCatalog,
) -> dict[str, Any]:
    return {
        "tag": "form",
        "name": "binding_config_v4",
        "elements": [
            *_model_settings_form_elements(
                prefix="config",
                catalog=catalog,
                turn_settings=turn_settings,
                model_value_encoder=lambda model_id: _config_model_reference(
                    binding_id=binding_id,
                    settings_revision=settings_revision,
                    model_id=model_id,
                ),
            ),
            _form_submit_button(
                name="binding_config_submit_v4",
                label="保存会话配置",
            ),
        ],
    }


def _model_settings_form_elements(
    *,
    prefix: str,
    catalog: ModelCatalog,
    turn_settings: BindingTurnSettings | None = None,
    model_value_encoder: Callable[[str], str] | None = None,
    model_label: str = "Model",
) -> list[dict[str, Any]]:
    default = catalog.default_model
    model_id = default.id
    effort_id = default.default_effort_id
    service_tier_id = default.default_service_tier_id
    if turn_settings is not None:
        try:
            catalog.resolve(
                model_id=turn_settings.model_id,
                effort_id=turn_settings.effort_id,
                service_tier_id=turn_settings.service_tier_id,
            )
        except ValueError:
            pass
        else:
            model_id = turn_settings.model_id
            effort_id = turn_settings.effort_id
            service_tier_id = turn_settings.service_tier_id

    return [
        _form_label(model_label),
        _static_select(
            name=f"{prefix}_model",
            placeholder="选择 Model",
            options=tuple(
                (
                    model.display_name
                    + (" · 默认" if model.is_default else ""),
                    (
                        model_value_encoder(model.id)
                        if model_value_encoder is not None
                        else model.id
                    ),
                )
                for model in catalog.models
            ),
            initial_option=(
                model_value_encoder(model_id)
                if model_value_encoder is not None
                else model_id
            ),
        ),
        _form_label("Effort"),
        _static_select(
            name=f"{prefix}_effort",
            placeholder="选择 Effort",
            options=tuple(
                (option.id, option.id) for option in catalog.effort_options
            ),
            initial_option=effort_id,
        ),
        _form_label("Speed"),
        _static_select(
            name=f"{prefix}_speed",
            placeholder="选择 Speed",
            options=tuple(
                (option.name, option.id)
                for option in catalog.service_tier_options
            ),
            initial_option=service_tier_id,
        ),
    ]


def _form_label(label: str) -> dict[str, Any]:
    return {"tag": "div", "text": _plain_text(label)}


def _static_select(
    *,
    name: str,
    placeholder: str,
    options: tuple[tuple[str, str], ...],
    initial_option: str,
) -> dict[str, Any]:
    return {
        "tag": "select_static",
        "name": name,
        "required": True,
        "placeholder": _plain_text(placeholder),
        "initial_option": initial_option,
        "options": [
            {"text": _plain_text(label), "value": value}
            for label, value in options
        ],
    }


def _form_submit_button(*, name: str, label: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "name": name,
        "text": _plain_text(label),
        "type": "primary_filled",
        "width": "fill",
        "form_action_type": "submit",
    }


def new_binding_card(
    *,
    scope: FeishuScope,
    projects: tuple[Project, ...],
    catalog: ModelCatalog | None = None,
    catalog_error: str | None = None,
) -> OutboundCard:
    builder = _builder("新建会话", "选择 Project 与模型配置")
    builder.markdown(
        "只创建 lazy Binding，不会立即启动任务。所选 Model / Effort / Speed "
        "会保存到当前会话，并应用于 Netizen 后续启动的每条新 Turn。"
    )
    visible = projects[:MAX_PROJECT_ROWS]
    if not visible:
        builder.markdown("当前没有可用 Project。请先发送 `/settings` 新增或启用。")
    elif catalog is not None:
        builder.raw(_new_binding_form(visible, catalog))
        if len(projects) > len(visible):
            builder.markdown(
                f"<font color='grey'>仅显示前 {MAX_PROJECT_ROWS} 个 Project；"
                "也可使用 `/new alias`。</font>"
            )
    else:
        builder.raw(
            _notice(
                (catalog_error or "Model / Effort / Speed 暂不可用。")
                + " 请稍后重试，或使用 `/new alias` 创建继承 Codex 的会话。",
                error=True,
            )
        )
    return OutboundCard(card=builder.to_dict())


def config_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    settings_revision: int,
    turn_settings: BindingTurnSettings | None,
    catalog: ModelCatalog,
) -> OutboundCard:
    builder = _builder("当前会话配置", f"{short_id} · {project_alias}")
    builder.markdown(
        "请选择当前会话使用的 Model / Effort / Speed；保存不会启动任务。"
        "Netizen 后续每次启动新 Turn 时都会重新校验并应用这三项配置。"
    )
    if turn_settings is not None:
        try:
            catalog.resolve(
                model_id=turn_settings.model_id,
                effort_id=turn_settings.effort_id,
                service_tier_id=turn_settings.service_tier_id,
            )
        except ValueError:
            builder.raw(
                _notice(
                    "已保存的会话配置不再出现在当前模型目录中；"
                    "发送任务会明确失败并保留配置。请在下方重新选择三项配置。",
                    error=True,
                )
            )
    builder.raw(
        _binding_config_form(
            binding_id=binding_id,
            settings_revision=settings_revision,
            turn_settings=turn_settings,
            catalog=catalog,
        )
    )
    return OutboundCard(card=builder.to_dict())


def rename_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    current_title: str,
) -> OutboundCard:
    builder = _builder("重命名当前会话", f"{short_id} · {project_alias}")
    builder.markdown(
        f"当前名称：`{_md_code(current_title)}`\n"
        "名称直接写入原生 Codex Thread，并同步显示在 Codex App/CLI。"
    )
    builder.raw(
        {
            "tag": "form",
            "name": "binding_rename_v1",
            "elements": [
                {
                    "tag": "input",
                    "name": _rename_name_field(binding_id),
                    "required": True,
                    "label": _plain_text("新名称"),
                    "placeholder": _plain_text("输入新的会话名称"),
                    "max_length": MAX_THREAD_NAME_CHARS,
                },
                _form_submit_button(
                    name="binding_rename_submit_v1",
                    label="保存新名称",
                ),
            ],
        }
    )
    return OutboundCard(card=builder.to_dict())


def archive_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
) -> OutboundCard:
    builder = _builder("归档当前会话", f"{short_id} · {project_alias}")
    builder.markdown(
        f"即将归档：`{_md_code(title)}`\n"
        "归档会同步影响 Codex App/CLI；历史不会删除，之后可以恢复。"
    )
    builder.raw(
        _callback_button(
            label="确认归档当前会话",
            value=_envelope(
                scope,
                CardControlName.ARCHIVE_BINDING,
                binding_id=_binding_reference(binding_id),
            ),
            style="primary_filled",
            confirm=(
                "确认归档当前会话？",
                "成功后当前会话会清空，后续消息不会自动切换到其他会话。",
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def delete_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
) -> OutboundCard:
    builder = _builder(
        "永久删除当前会话",
        f"{short_id} · {project_alias}",
        template="red",
    )
    builder.markdown(
        f"即将删除：`{_md_code(title)}`\n"
        "该 Lazy 会话尚无原生历史；确认后只删除本地 Binding。"
    )
    builder.raw(
        _callback_button(
            label="永久删除当前会话",
            value=_envelope(
                scope,
                CardControlName.DELETE_BINDING,
                binding_id=_binding_reference(binding_id),
            ),
            confirm=(
                "永久删除且无法恢复",
                "请再次确认：该本地会话映射将被永久删除。",
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def archived_sessions_card(
    *,
    scope: FeishuScope,
    sessions: tuple[ArchivedSessionCardItem, ...],
) -> OutboundCard:
    builder = _builder("已归档会话", "恢复后自动切换")
    builder.markdown(
        "已归档会话不会出现在普通 `/sessions` 中。恢复不会修改历史或会话配置。"
    )
    if not sessions:
        builder.markdown("当前 Scope 没有已归档会话。")
        return OutboundCard(card=builder.to_dict())
    for session in sessions:
        builder.raw(
            _archived_session_row(
                scope=scope,
                session=session,
            )
        )
    return OutboundCard(card=builder.to_dict())


def binding_lifecycle_result_card(
    *,
    title: str,
    short_id: str,
    project_alias: str,
    message: str,
) -> OutboundCard:
    builder = _builder(
        title,
        f"{short_id} · {project_alias}",
        template="green",
    )
    builder.markdown(message)
    return OutboundCard(card=builder.to_dict())


def goal_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    goal: GoalSnapshot | None,
    runtime_state: str | None = None,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    builder = _builder("Codex Goal", f"{short_id} · {project_alias}")
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    if goal is None:
        builder.markdown(
            "当前原生 Thread 没有 Goal。使用 `/goal <objective>` 启动；"
            "Goal 可跨多个物理 Turn 自动继续。"
        )
        return OutboundCard(card=builder.to_dict())
    state = runtime_state or f"goal-{goal.status.value}"
    budget = "未设置" if goal.token_budget is None else str(goal.token_budget)
    builder.markdown(
        f"**状态**：`{_md_code(state)}`\n"
        f"**Objective**：{_md_code(goal.objective)}\n"
        f"**Tokens**：{goal.tokens_used} / {budget} · "
        f"**Time**：{goal.time_used_seconds}s"
    )
    buttons: list[dict[str, Any]] = []
    if goal.status is GoalStatus.ACTIVE and runtime_state != "externally-active-goal":
        buttons.append(
            _callback_button(
                label="暂停 Goal",
                value=_envelope(
                    scope,
                    CardControlName.GOAL_PAUSE,
                    binding_id=_binding_reference(binding_id),
                ),
                style="primary",
                confirm=("暂停 Goal", "将暂停 Goal 并中断当前物理 Turn。"),
            )
        )
    if goal.status is GoalStatus.PAUSED and runtime_state != "externally-active-goal":
        buttons.append(
            _callback_button(
                label="恢复 Goal",
                value=_envelope(
                    scope,
                    CardControlName.GOAL_RESUME,
                    binding_id=_binding_reference(binding_id),
                ),
                style="primary_filled",
            )
        )
    if goal.status is not GoalStatus.ACTIVE:
        buttons.append(
            _callback_button(
                label="清除 Goal",
                value=_envelope(
                    scope,
                    CardControlName.GOAL_CLEAR,
                    binding_id=_binding_reference(binding_id),
                ),
                confirm=("清除 Goal", "清除后将无法从此 Goal 状态恢复。"),
            )
        )
    if buttons:
        builder.raw(_button_row(*buttons))
    elif runtime_state == "externally-active-goal":
        builder.raw(
            _notice(
                "这是重启前或外部客户端启动的 active Goal；"
                "当前 SDK 无法安全补收通知并重挂。请先在原生 Codex 中暂停。"
            )
        )
    return OutboundCard(card=builder.to_dict())


def side_topic_card(
    *,
    scope: FeishuScope,
    side_id: str,
    parent_short_id: str,
    creator_id: str,
    created_at: str,
    state: SideTopicState,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    labels = {
        SideTopicState.CREATING: "创建中",
        SideTopicState.OPEN: "可用",
        SideTopicState.CLOSED: "已结束",
        SideTopicState.EXPIRED: "已过期",
        SideTopicState.FAILED: "创建或清理失败",
    }
    templates = {
        SideTopicState.CREATING: "blue",
        SideTopicState.OPEN: "green",
        SideTopicState.CLOSED: "grey",
        SideTopicState.EXPIRED: "grey",
        SideTopicState.FAILED: "red",
    }
    builder = _builder(
        "Codex Side",
        f"{labels[state]} · Parent {parent_short_id}",
        template=templates[state],
    )
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    builder.markdown(
        f"**状态**：`{state.value}`\n"
        f"**Parent 会话**：`{_md_code(parent_short_id)}`\n"
        f"**创建者**：`{_md_code(creator_id)}`\n"
        f"**创建时间**：`{_md_code(created_at)}`\n\n"
        "Side 使用独立的 ephemeral Codex Thread，并与 Parent 共享 Project cwd；"
        "文件改动彼此可见。服务重启后本 Side 会过期，历史消息仅保留在飞书中。"
    )
    if state is SideTopicState.OPEN and scope.kind is not ScopeKind.TOPIC:
        raise ValueError("an open Side card requires its exact Topic scope")
    if (
        state in {SideTopicState.CREATING, SideTopicState.OPEN}
        and scope.kind is ScopeKind.TOPIC
    ):
        creating = state is SideTopicState.CREATING
        builder.raw(
            _button_row(
                _callback_button(
                    label="取消 Side" if creating else "结束 Side",
                    value=_envelope(
                        scope,
                        CardControlName.SIDE_CLOSE,
                        side_id=_side_reference(side_id),
                    ),
                    style="danger",
                    confirm=(
                        "取消 Side" if creating else "结束 Side",
                        (
                            "将重试清理未完成的 Side，并取消原生订阅。"
                            if creating
                            else "将中断当前 Side Turn、清理后台终端并取消原生订阅。"
                        ),
                    ),
                )
            )
        )
    return OutboundCard(card=builder.to_dict())


def binding_created_card(
    *,
    short_id: str,
    project_alias: str,
    settings: TurnModelSettings | None = None,
) -> OutboundCard:
    builder = _builder(
        "Project 选择成功",
        f"{project_alias} · {short_id}",
        template="green",
    )
    builder.markdown(
        f"✅ 已选择 Project `{_md_code(project_alias)}`，"
        f"并创建、切换到会话 **{short_id}**。"
    )
    builder.markdown(
        "现在可以直接发送任务；首条普通消息将创建原生 Codex Thread。"
    )
    if settings is None:
        builder.markdown("Model / Effort / Speed：继承 Codex。")
    else:
        builder.markdown(
            "会话后续新 Turn 将使用："
            f"Model=`{_md_code(settings.model)}` · "
            f"Effort=`{_md_code(settings.effort_id)}` · "
            f"Speed=`{_md_code(settings.service_tier_name)}`"
        )
    return OutboundCard(card=builder.to_dict())


def binding_configured_card(
    *,
    short_id: str,
    project_alias: str,
    settings: TurnModelSettings,
) -> OutboundCard:
    builder = _builder(
        "会话配置已保存",
        f"{project_alias} · {short_id}",
        template="green",
    )
    builder.markdown(
        "会话后续新 Turn 将使用："
        f"Model=`{_md_code(settings.model)}` · "
        f"Effort=`{_md_code(settings.effort_id)}` · "
        f"Speed=`{_md_code(settings.service_tier_name)}`"
    )
    return OutboundCard(card=builder.to_dict())


def error_card(message: str, *, scope: FeishuScope | None = None) -> OutboundCard:
    builder = _builder("操作失败", "请修正后重试", template="red")
    builder.raw(_notice(message, error=True))
    if scope is not None:
        builder.raw(
            _callback_button(
                label="刷新设置",
                value=_envelope(
                    scope,
                    CardControlName.REFRESH_SETTINGS,
                    settings_section=SettingsSection.PROJECTS.value,
                ),
                style="primary_filled",
            )
        )
    return OutboundCard(card=builder.to_dict())


def decode_button_action(
    *,
    app_id: str,
    message_id: str,
    callback_chat_id: str,
    sender_id: str,
    tag: str,
    value: Any,
) -> CardControlIntent:
    if tag != "button":
        raise CardActionError(f"不支持的卡片组件：{tag or 'unknown'}")
    if not message_id or not callback_chat_id or not sender_id:
        raise CardActionError("卡片回调缺少消息、聊天或操作者标识。")
    if not isinstance(value, Mapping):
        raise CardActionError("卡片动作 value 必须是对象。")
    payload = dict(value)
    raw_intent = payload.get("intent")
    try:
        name = CardControlName(raw_intent)
    except (TypeError, ValueError) as error:
        raise CardActionError("未知卡片动作。") from error
    if payload.get("v") != ACTION_VERSION or isinstance(payload.get("v"), bool):
        raise CardActionError("卡片版本已过期，请重新发送原命令打开新卡片。")
    form_only = {
        CardControlName.REGISTER_PROJECT,
        CardControlName.CREATE_BINDING,
        CardControlName.CONFIGURE_BINDING,
        CardControlName.RENAME_BINDING,
    }
    if name in form_only:
        raise CardActionError("该操作只能通过对应表单提交。")

    common = {"v", "intent", "chat_id", "scope_kind"}
    extra_by_name = {
        CardControlName.OPEN_SETTINGS_SECTION: {"settings_section"},
        CardControlName.REFRESH_SETTINGS: {"settings_section"},
        CardControlName.SET_PROJECT_ENABLED: {
            "project_alias",
            "enabled",
            "expected_revision",
        },
        CardControlName.ARCHIVE_BINDING: {"binding_id"},
        CardControlName.DELETE_BINDING: {"binding_id"},
        CardControlName.UNARCHIVE_BINDING: {"binding_id"},
        CardControlName.GOAL_PAUSE: {"binding_id"},
        CardControlName.GOAL_RESUME: {"binding_id"},
        CardControlName.GOAL_CLEAR: {"binding_id"},
        CardControlName.SIDE_CLOSE: {"side_id"},
    }
    try:
        scope_kind = ScopeKind(payload.get("scope_kind"))
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    scope_fields = {"topic_id"} if scope_kind is ScopeKind.TOPIC else set()
    expected = common | scope_fields | extra_by_name[name]
    if set(payload) != expected:
        raise CardActionError("卡片动作字段不完整或包含未知字段。")
    if payload["chat_id"] != callback_chat_id:
        raise CardActionError("卡片 Scope 与当前聊天不一致。")
    scope = _scope_from_envelope(
        app_id=app_id,
        chat_id=callback_chat_id,
        kind=scope_kind,
        topic_id=payload.get("topic_id"),
    )

    alias = None
    binding_id = None
    side_id = None
    revision = None
    enabled = None
    section = None
    if "settings_section" in payload:
        try:
            section = SettingsSection(payload["settings_section"])
        except (TypeError, ValueError) as error:
            raise CardActionError("未知 Settings 分区。") from error
    if "project_alias" in payload:
        alias = _required_string(payload["project_alias"], "project_alias")
    if "binding_id" in payload:
        binding_id = _decode_binding_reference(
            _required_string(payload["binding_id"], "binding_id")
        )
    if "side_id" in payload:
        side_id = _decode_side_reference(
            _required_string(payload["side_id"], "side_id")
        )
    if "expected_revision" in payload:
        revision = payload["expected_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise CardActionError("expected_revision 必须是正整数。")
    if "enabled" in payload:
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise CardActionError("enabled 必须是布尔值。")
    if name is CardControlName.SET_PROJECT_ENABLED:
        section = SettingsSection.PROJECTS
    if name is CardControlName.SIDE_CLOSE and scope.kind is not ScopeKind.TOPIC:
        raise CardActionError("Side 结束动作必须来自原 Side 话题。")
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=name,
        settings_section=section,
        project_alias=alias,
        expected_revision=revision,
        enabled=enabled,
        binding_id=binding_id,
        side_id=side_id,
    )


def decode_card_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Any,
) -> CardControlIntent:
    if not isinstance(form_value, Mapping):
        raise CardActionError("卡片表单值必须是对象。")
    fields = set(form_value)
    has_new = any(field.startswith("new_") for field in fields)
    has_config = any(field.startswith("config_") for field in fields)
    has_rename = any(field.startswith("rename_name_v1__") for field in fields)
    if sum((has_new, has_config, has_rename)) > 1:
        raise CardActionError("卡片表单混合了不同操作的字段。")
    if has_new:
        return _decode_new_binding_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    if has_config:
        return _decode_config_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    if has_rename:
        return _decode_rename_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    return decode_settings_form(
        scope=scope,
        message_id=message_id,
        sender_id=sender_id,
        tag=tag,
        form_value=form_value,
    )


def _decode_new_binding_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Mapping[str, Any],
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("会话配置表单回调不完整。")
    expected_fields = {
        "new_project",
        "new_model",
        "new_effort",
        "new_speed",
    }
    payload = dict(form_value)
    if set(payload) != expected_fields:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")

    project_alias, expected_revision = _decode_project_reference(
        _required_string(payload["new_project"], "new_project")
    )
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.CREATE_BINDING,
        project_alias=project_alias,
        expected_revision=expected_revision,
        model_id=_required_string(payload["new_model"], "new_model"),
        effort_id=_required_string(payload["new_effort"], "new_effort"),
        service_tier_id=_required_string(payload["new_speed"], "new_speed"),
    )


def _decode_config_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Mapping[str, Any],
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("会话配置表单回调不完整。")
    payload = dict(form_value)
    if set(payload) != {"config_model", "config_effort", "config_speed"}:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")
    binding_id, expected_settings_revision, model_id = _decode_config_model_reference(
        _required_string(payload["config_model"], "config_model")
    )
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.CONFIGURE_BINDING,
        expected_settings_revision=expected_settings_revision,
        binding_id=binding_id,
        model_id=model_id,
        effort_id=_required_string(payload["config_effort"], "config_effort"),
        service_tier_id=_required_string(payload["config_speed"], "config_speed"),
    )


def _decode_rename_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Mapping[str, Any],
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("会话重命名表单回调不完整。")
    payload = dict(form_value)
    if len(payload) != 1:
        raise CardActionError("会话重命名表单字段不完整或包含未知字段。")
    field, raw_name = next(iter(payload.items()))
    match = _RENAME_NAME_FIELD.fullmatch(field)
    if match is None:
        raise CardActionError("会话重命名卡片已过期，请重新发送 /rename。")
    name = " ".join(_required_string(raw_name, "thread_name").split())
    if not name:
        raise CardActionError("会话名称不能为空。")
    if len(name) > MAX_THREAD_NAME_CHARS:
        raise CardActionError(
            f"会话名称不能超过 {MAX_THREAD_NAME_CHARS} 个字符。"
        )
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.RENAME_BINDING,
        binding_id=match.group(1),
        thread_name=name,
    )


def decode_settings_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Any,
) -> CardControlIntent:
    try:
        return _decode_settings_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    except SettingsCardActionError:
        raise
    except CardActionError as error:
        raise SettingsCardActionError(
            str(error),
            scope=scope,
            section=SettingsSection.PROJECTS,
        ) from error


def _decode_settings_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Any,
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("Settings 表单回调不完整。")
    if not isinstance(form_value, Mapping):
        raise CardActionError("Settings 表单值必须是对象。")
    payload = dict(form_value)
    if set(payload) == {"project_manage_target", "project_manage_operation"}:
        return _decode_project_management_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            payload=payload,
        )

    required = {"project_alias", "project_mode"}
    allowed = required | {"project_path"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise CardActionError("未知、字段不完整或包含额外字段的 Settings 表单。")
    alias = _required_string(payload["project_alias"], "project_alias").strip()
    mode = _required_string(payload["project_mode"], "project_mode")
    if mode not in {"create", "existing"}:
        raise CardActionError("未知 Project 创建模式。")
    raw_path = payload.get("project_path", "")
    if not isinstance(raw_path, str):
        raise CardActionError("project_path 必须是字符串。")
    path = raw_path.strip() or None
    if mode == "existing" and path is None:
        raise CardActionError("登记已有目录时必须填写绝对路径。")
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.REGISTER_PROJECT,
        settings_section=SettingsSection.PROJECTS,
        project_alias=alias,
        project_path=path,
        create_directory=mode == "create",
    )


def _decode_project_management_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    payload: Mapping[str, Any],
) -> CardControlIntent:
    alias, revision = _decode_project_reference(
        _required_string(payload["project_manage_target"], "project_manage_target")
    )
    operation = _required_string(
        payload["project_manage_operation"],
        "project_manage_operation",
    )
    if operation == "enable":
        enabled = True
    elif operation == "disable":
        enabled = False
    else:
        raise CardActionError("未知 Project 操作。")
    if alias == "none":
        raise CardActionError("系统 Project none 不参与启停操作。")
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.SET_PROJECT_ENABLED,
        settings_section=SettingsSection.PROJECTS,
        project_alias=alias,
        expected_revision=revision,
        enabled=enabled,
    )


def scope_from_fetched_card(
    *,
    app_id: str,
    callback_chat_id: str,
    fetched_message: Any,
    chat_type: str | None,
) -> FeishuScope:
    item = _fetched_card_item(
        callback_chat_id=callback_chat_id,
        fetched_message=fetched_message,
    )
    raw_topic_id = item.get("thread_id")
    if isinstance(raw_topic_id, str) and raw_topic_id:
        return FeishuScope(
            app_id,
            callback_chat_id,
            ScopeKind.TOPIC,
            raw_topic_id,
        )
    if chat_type == "p2p":
        kind = ScopeKind.DIRECT
    elif chat_type == "group":
        kind = ScopeKind.GROUP
    else:
        raise CardActionError("无法判断卡片来自单聊还是群聊。")
    return FeishuScope(app_id, callback_chat_id, kind)


def fetched_card_topic_id(
    *,
    callback_chat_id: str,
    fetched_message: Any,
) -> str | None:
    """Return the public thread id without requiring an unrelated chat lookup."""
    item = _fetched_card_item(
        callback_chat_id=callback_chat_id,
        fetched_message=fetched_message,
    )
    raw_topic_id = item.get("thread_id")
    return raw_topic_id if isinstance(raw_topic_id, str) and raw_topic_id else None


def _fetched_card_item(
    *,
    callback_chat_id: str,
    fetched_message: Any,
) -> Mapping[str, Any]:
    if not isinstance(fetched_message, Mapping):
        raise CardActionError("无法读取卡片原消息。")
    data = fetched_message.get("data")
    if not isinstance(data, Mapping):
        raise CardActionError("卡片原消息缺少 data。")
    items = data.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
        raise CardActionError("卡片原消息缺少 items。")
    item = items[0]
    if item.get("chat_id") != callback_chat_id:
        raise CardActionError("卡片原消息与回调聊天不一致。")
    return item


def _scope_from_envelope(
    *,
    app_id: str,
    chat_id: str,
    kind: Any,
    topic_id: Any,
) -> FeishuScope:
    try:
        scope_kind = ScopeKind(kind)
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    if scope_kind is ScopeKind.TOPIC:
        topic = _required_string(topic_id, "topic_id")
    else:
        if topic_id is not None:
            raise CardActionError("非话题 Scope 不能携带 topic_id。")
        topic = None
    return FeishuScope(app_id, chat_id, scope_kind, topic)


def _envelope(
    scope: FeishuScope,
    name: CardControlName,
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "v": ACTION_VERSION,
        "intent": name.value,
        "chat_id": scope.chat_id,
        "scope_kind": scope.kind.value,
        **extra,
    }
    if scope.kind is ScopeKind.TOPIC:
        value["topic_id"] = scope.topic_id
    return value


def _project_reference(project: Project) -> str:
    return f"project:v1:{project.alias}:{project.revision}"


def _decode_project_reference(value: str) -> tuple[str, int]:
    match = _PROJECT_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("Project 选择值无效，请刷新卡片后重试。")
    return match.group(1), int(match.group(2))


def _binding_reference(binding_id: str) -> str:
    return f"binding:v1:{binding_id}"


def _decode_binding_reference(value: str) -> str:
    match = _BINDING_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("会话引用无效，请重新打开原卡片。")
    return match.group(1)


def _side_reference(side_id: str) -> str:
    value = f"side:v1:{side_id}"
    if _SIDE_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Side reference")
    return value


def _decode_side_reference(value: str) -> str:
    match = _SIDE_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("Side 引用无效，请回到原 Side 话题重试。")
    return match.group(1)


def _turn_reference(turn_id: str) -> str:
    value = f"turn:v1:{turn_id}"
    if _TURN_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid native Turn reference")
    return value


def _decode_turn_reference(value: str) -> str:
    match = _TURN_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("原生 Turn 引用无效，请重新执行任务。")
    return match.group(1)


def _rename_name_field(binding_id: str) -> str:
    if _BINDING_REFERENCE.fullmatch(_binding_reference(binding_id)) is None:
        raise ValueError("invalid Binding rename reference")
    return f"rename_name_v1__{binding_id}"


def _config_model_reference(
    *,
    binding_id: str,
    settings_revision: int,
    model_id: str,
) -> str:
    encoded_model = base64.urlsafe_b64encode(model_id.encode("utf-8")).decode(
        "ascii"
    ).rstrip("=")
    value = (
        f"config-model:v2:{binding_id}:{settings_revision}:{encoded_model}"
    )
    if _CONFIG_MODEL_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Binding model settings reference")
    return value


def _decode_config_model_reference(value: str) -> tuple[str, int, str]:
    match = _CONFIG_MODEL_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("会话配置卡片已过期，请重新发送 /config。")
    encoded_model = match.group(3)
    padding = "=" * (-len(encoded_model) % 4)
    try:
        model_id = base64.b64decode(
            encoded_model + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise CardActionError(
            "会话配置卡片已过期，请重新发送 /config。"
        ) from error
    if not model_id:
        raise CardActionError("会话配置卡片已过期，请重新发送 /config。")
    return match.group(1), int(match.group(2)), model_id


def _builder(title: str, subtitle: str, *, template: str = "blue"):
    return (
        new_card()
        .config(update_multi=True, width_mode="default")
        .header(title, subtitle=subtitle, template=template)
    )


def _turn_answer_block(final_response: str) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "element_id": _TURN_ANSWER_ELEMENT_ID,
        "flex_mode": "none",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": [
                    {"tag": "markdown", "content": final_response},
                ],
            }
        ],
    }


def _turn_files_block(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    page: TurnFilePage,
    manifest: tuple[TurnFileManifestItem, ...],
    final_response: str,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**本轮文件** · 共 {page.total_items} 个 · "
                f"第 {page.page + 1}/{page.total_pages} 页\n"
                "<font color='grey'>点击后以图片或文件消息发送到本卡片话题。</font>"
            ),
        }
    ]
    elements.extend(
        _turn_file_row(
            scope=scope,
            binding_id=binding_id,
            turn_id=turn_id,
            turn_file=turn_file,
        )
        for turn_file in page.items
    )
    if page.total_pages > 1:
        elements.append(
            _turn_file_pagination(
                scope=scope,
                binding_id=binding_id,
                turn_id=turn_id,
                page=page.page,
                total_pages=page.total_pages,
                manifest=manifest,
                final_response=final_response,
            )
        )
    return {
        "tag": "column_set",
        "element_id": _TURN_FILES_ELEMENT_ID,
        "flex_mode": "none",
        "background_style": "grey-50",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "12px",
                "vertical_spacing": "8px",
                "elements": elements,
            }
        ],
    }


def _turn_file_row(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    turn_file: TurnFile,
) -> dict[str, Any]:
    if not turn_file.available:
        return {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "padding": "8px",
                    "vertical_spacing": "2px",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": (
                                f"⚠️ `{_turn_file_label(turn_file.display_path)}`\n"
                                "<font color='grey'>文件当前不可用</font>"
                            ),
                        }
                    ],
                }
            ],
        }
    assert turn_file.size is not None
    assert turn_file.media_kind is not None
    icon = "🖼️" if turn_file.media_kind == "image" else "📄"
    label = (
        "发送原图到话题"
        if turn_file.media_kind == "image"
        else "发送文件到话题"
    )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "padding": "8px",
                "vertical_spacing": "2px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"{icon} `{_turn_file_label(turn_file.display_path)}`\n"
                            f"<font color='grey'>{format_file_size(turn_file.size)}</font>"
                        ),
                    }
                ],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": [
                    _callback_button(
                        label=label,
                        value=_turn_file_envelope(
                            scope,
                            TurnFileActionName.SEND,
                            binding_id=_binding_reference(binding_id),
                            turn_id=_turn_reference(turn_id),
                            path=str(turn_file.resolved_path),
                        ),
                    )
                ],
            },
        ],
    }


def _turn_file_pagination(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    page: int,
    total_pages: int,
    manifest: tuple[TurnFileManifestItem, ...],
    final_response: str,
) -> dict[str, Any]:
    columns: list[dict[str, Any]] = [
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "center",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"<font color='grey'>第 {page + 1}/{total_pages} 页</font>",
                }
            ],
        }
    ]
    target = 0 if page + 1 >= total_pages else page + 1
    label = "回到第一页" if target == 0 else "下一页"
    columns.append(
        {
            "tag": "column",
            "width": "auto",
            "elements": [
                _callback_button(
                    label=label,
                    value=_turn_file_envelope(
                        scope,
                        TurnFileActionName.PAGE,
                        binding_id=_binding_reference(binding_id),
                        turn_id=_turn_reference(turn_id),
                        page=target,
                        files=[
                            {"path": item.path, "label": item.label}
                            for item in manifest
                        ],
                        answer=final_response,
                    ),
                )
            ],
        }
    )
    return {"tag": "column_set", "flex_mode": "none", "columns": columns}


def _turn_file_envelope(
    scope: FeishuScope,
    name: TurnFileActionName,
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "v": TURN_FILE_ACTION_VERSION,
        "intent": name.value,
        "chat_id": scope.chat_id,
        "scope_kind": scope.kind.value,
        **extra,
    }
    if scope.kind is ScopeKind.TOPIC:
        value["topic_id"] = scope.topic_id
    return value


def _turn_file_label(value: str) -> str:
    visible = "".join(
        character if character.isprintable() else "�" for character in value
    )
    return html.escape(visible, quote=False).replace("`", "ˋ")


def _project_row(
    project: Project,
    controls: list[dict[str, Any]],
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey-50",
        "margin": "0 0 8px 0",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "padding": "8px",
                "elements": [
                    _plain(f"{project.alias} · {status}\n{project.cwd}"),
                ],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": controls,
            },
        ],
    }


def _archived_session_row(
    *,
    scope: FeishuScope,
    session: ArchivedSessionCardItem,
) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey-50",
        "margin": "0 0 8px 0",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "padding": "8px",
                "elements": [
                    _plain(
                        f"{session.title}\n"
                        f"会话：{session.short_id} · "
                        f"Project：{session.project_alias} · "
                        f"Native：{session.native_thread_id[:8]}"
                    )
                ],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": [
                    _callback_button(
                        label="恢复并切换",
                        value=_envelope(
                            scope,
                            CardControlName.UNARCHIVE_BINDING,
                            binding_id=_binding_reference(session.binding_id),
                        ),
                        style="primary_filled",
                    )
                ],
            },
        ],
    }


def _button_row(*buttons: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [button],
            }
            for button in buttons
        ],
    }


def _callback_button(
    *,
    label: str,
    value: dict[str, Any],
    style: str = "default",
    confirm: tuple[str, str] | None = None,
) -> dict[str, Any]:
    button: dict[str, Any] = {
        "tag": "button",
        "text": _plain_text(label),
        "type": style,
        "behaviors": [{"type": "callback", "value": value}],
    }
    if confirm is not None:
        button["confirm"] = {
            "title": _plain_text(confirm[0]),
            "text": _plain_text(confirm[1]),
        }
    return button


def _notice(message: str, *, error: bool = False) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "background_style": "red-50" if error else "blue-50",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "8px",
                "elements": [_plain(message)],
            }
        ],
    }


def _plain(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": _plain_text(content)}


def _plain_text(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CardActionError(f"{field} 必须是非空字符串。")
    return value


def _md_code(value: str) -> str:
    return value.replace("`", "ˋ")
