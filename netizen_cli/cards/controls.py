"""Management cards, forms, and strict Channel control decoding."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lark_channel import OutboundCard

from ..bindings import (
    BindingTaskFeedback,
    BindingTurnSettings,
    SideTopicState,
)
from ..completion_mention import valid_completion_mention_user_id
from ..channel.session_pages import SessionCardPage
from ..domain import (
    ActiveState,
    CardControlIntent,
    CardControlName,
    FeishuScope,
    GoalStatus,
    MentionContextMode,
    ReplyCardGoalModule,
    ReplyCardProjection,
    SettingsSection,
    SESSION_IDLE_STATE,
    ScopeKind,
    session_stop_available,
)
from ..model_settings import ModelCatalog, TurnModelSettings
from ..projects import Project
from ..progress_card_settings import ProgressCardSettings
from ..session_settings import SessionSettings
from ..sdk_gap_adapter import GoalSnapshot
from .callbacks import (
    ACTION_VERSION,
    CardActionError,
    SettingsCardActionError,
    _binding_reference,
    _bounded_string,
    _builder,
    _button_row,
    _callback_button,
    _config_model_reference,
    _context_mode_reference,
    _decode_binding_reference,
    _decode_config_model_reference,
    _decode_context_mode_reference,
    _decode_goal_generation,
    _decode_native_thread_reference,
    _decode_new_model_reference,
    _decode_project_mode_reference,
    _decode_project_reference,
    _decode_side_reference,
    _decode_task_feedback_reference,
    _decode_turn_reference,
    _envelope,
    _goal_generation,
    _md_code,
    _native_thread_reference,
    _new_callback_nonce,
    _new_model_reference,
    _notice,
    _plain,
    _plain_text,
    _project_mode_reference,
    _project_reference,
    _rename_name_field,
    _repeatable_callback_button,
    _required_string,
    _scope_from_envelope,
    _side_reference,
    _task_feedback_reference,
    _turn_reference,
)
from .reply import reply_card
from .session_ids import session_identity_lines


SESSIONS_PAGE_SIZE = 10
MAX_THREAD_NAME_CHARS = 120
MAX_SETTING_ID_CHARS = 128
_PROGRESS_CARD_FIELDS = (
    "show_commentary", "show_operations", "show_plan", "commentary_limit",
    "operation_limit", "commentary_max_chars", "operation_max_chars", "organization",
)
_PROGRESS_CARD_REFERENCE_PREFIX = "progress-card:v1:"
_RENAME_NAME_FIELD = re.compile(
    r"rename_name_v1__([A-Za-z0-9][A-Za-z0-9-]{0,127})"
)


@dataclass(frozen=True, slots=True)
class ArchivedSessionCardItem:
    binding_id: str
    short_id: str
    project_alias: str
    native_thread_id: str
    title: str


@dataclass(frozen=True, slots=True)
class SessionCardItem:
    binding_id: str
    short_id: str
    project_alias: str
    native_thread_id: str | None
    title: str
    state: str
    active: bool
    activity_revision: int = 0
    turn_id: str | None = None
    catalog_unconfirmed: bool = False


@dataclass(frozen=True, slots=True)
class SideTopicCardContext:
    parent_title: str = "原会话信息暂不可用"
    project_alias: str | None = None
    project_cwd: str | None = None
    source_url: str | None = None
    source_is_topic: bool = False
    can_resume_parent: bool = False
    creator_name: str | None = None


def settings_card(
    *,
    scope: FeishuScope,
    projects: tuple[Project, ...],
    project_root: str,
    section: SettingsSection = SettingsSection.PROJECTS,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    builder = _builder("Netizen 项目管理", _settings_section_label(section))
    builder.raw(_settings_navigation(scope=scope, active=section))
    builder.markdown(
        "当前聊天的参与者均可管理项目；修改会影响使用这个机器人的其他聊天。"
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
        "**项目工作目录**\n"
        "项目是 Codex 实际工作的目录，使用这个机器人的会话共享这些项目。"
        "可创建空目录，或登记机器人所在机器上的已有目录。"
    )
    builder.markdown("**管理项目**")
    if projects:
        builder.raw(_project_management_form(projects))
    else:
        builder.markdown("还没有项目，请在下方新增，然后发送 `/new` 创建会话。")

    builder.raw(
        _repeatable_callback_button(
            label="刷新项目列表",
            value=_envelope(
                scope,
                CardControlName.REFRESH_SETTINGS,
                settings_section=SettingsSection.PROJECTS.value,
            ),
        )
    )
    builder.divider()
    builder.markdown("**新增项目**")
    builder.raw(_project_create_form(project_root))
    builder.markdown(
        f"<font color='grey'>空项目默认创建在 `{_md_code(project_root)}`；"
        "Netizen 从不删除项目目录。</font>"
    )


def _settings_navigation(
    *,
    scope: FeishuScope,
    active: SettingsSection,
) -> dict[str, Any]:
    return _button_row(
        *(
            _repeatable_callback_button(
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
        return "项目"
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
                "placeholder": _plain_text("选择项目"),
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
                "text": _plain_text("应用到所选项目"),
                "type": "primary",
                "width": "fill",
                "form_action_type": "submit",
                "confirm": {
                    "title": _plain_text("确认修改项目？"),
                    "text": _plain_text(
                        "状态会立即更新；停用只阻止创建新会话，已有会话仍可继续。"
                    ),
                },
            },
        ],
    }


def _project_create_form(project_root: str) -> dict[str, Any]:
    callback_nonce = _new_callback_nonce()
    return {
        "tag": "form",
        "name": "project_create_v1",
        "elements": [
            {
                "tag": "input",
                "name": "project_alias",
                "required": True,
                "label": _plain_text("项目标识（小写字母或数字开头，可含 _ 和 -）"),
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
                "initial_option": _project_mode_reference(
                    "create",
                    callback_nonce,
                ),
                "options": [
                    {
                        "text": _plain_text("创建空目录"),
                        "value": _project_mode_reference(
                            "create",
                            callback_nonce,
                        ),
                    },
                    {
                        "text": _plain_text("登记已有目录"),
                        "value": _project_mode_reference(
                            "existing",
                            callback_nonce,
                        ),
                    },
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
                "text": _plain_text("保存项目"),
                "type": "primary_filled",
                "width": "fill",
                "form_action_type": "submit",
            },
        ],
    }


def _new_binding_form(
    projects: tuple[Project, ...],
    catalog: ModelCatalog | None,
    *,
    initial_project_alias: str | None,
    allow_context_mode: bool,
    message_context_mode: MentionContextMode,
    task_feedback: BindingTaskFeedback,
    workspace_options: Mapping[str, Any] | None = None,
    allow_workspace_selection: bool = False,
    workspace_error: str | None = None,
    settings_draft: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    initial_project = next(
        (
            project
            for project in projects
            if project.alias == initial_project_alias
        ),
        None,
    )
    elements = [_form_section("项目", [
        _static_select(
            name="new_project",
            placeholder="选择项目",
            options=tuple(
                (
                    f"{project.alias} · {project.cwd}",
                    _project_reference(project),
                )
                for project in projects
            ),
            initial_option=(
                _project_reference(initial_project)
                if initial_project is not None
                else None
            ),
        ),
    ])]
    if catalog is None:
        model_fields = [
            _form_label("Model"),
            _static_select(
                name="new_model",
                placeholder="选择 Model 来源",
                options=(("继承 Codex", _new_model_reference(None)),),
                initial_option=_new_model_reference(None),
            ),
        ]
        for name, label in (("new_effort", "Effort"), ("new_speed", "Speed")):
            if settings_draft is not None and name in settings_draft:
                value = settings_draft[name]
                model_fields.extend([
                    _form_label(label),
                    _static_select(name=name, placeholder=label,
                                   options=((value, value),), initial_option=value),
                ])
    else:
        model_fields = _model_settings_form_elements(
            prefix="new",
            catalog=catalog,
            model_value_encoder=_new_model_reference,
            inherit_initial_when_unset=False,
        )
    context = (
        _context_mode_form_elements(prefix="new", initial_mode=message_context_mode)
        if allow_context_mode else []
    )
    feedback = _task_feedback_form_elements(prefix="new", initial=task_feedback)
    _restore_settings_draft([*model_fields, *context, *feedback], settings_draft)
    elements.extend(_session_settings_sections(
        model_fields=model_fields, context_fields=context, feedback_fields=feedback,
    ))
    if allow_workspace_selection:
        workspace_elements = [
            _form_hint("默认使用项目目录，Git 项目可选择或新建 worktree。加载选项不会创建会话；切换项目后请重新加载。"),
            _new_workspace_load_button(),
        ]
        if workspace_error:
            workspace_elements.append(_notice(workspace_error, error=True))
        if workspace_options is not None:
            workspace_elements.append(_form_hint(
                "已加载项目 " + _md_code(str(workspace_options["projectAlias"]))
                + (" 的 worktree 选项；新建 worktree 只检出提交内容，不复制本地改动或初始化运行环境。"
                   if workspace_options.get("isGit") is True
                   else " 不是 Git 仓库，本次仅使用项目目录。")
            ))
        if workspace_options is not None and workspace_options.get("isGit") is True:
            workspace_elements.extend(_new_workspace_elements(workspace_options))
        elements.append(_form_section("worktree", workspace_elements))
    elements.append(
        _form_submit_button(name="new_binding_submit_v6", label="新建会话")
    )
    return {
        "tag": "form",
        "name": "new_binding_v6",
        "vertical_spacing": "12px",
        "elements": elements,
    }


def _restore_settings_draft(
    elements: list[dict[str, Any]], draft: Mapping[str, str] | None,
) -> None:
    """Retain this form submission only; never silently replace an unavailable choice."""
    if draft is None:
        return
    for element in elements:
        if element.get("tag") != "select_static" or element["name"] not in draft:
            continue
        value = draft[element["name"]]
        if not value:
            element.pop("initial_option", None)
            continue
        if element["name"].endswith("_progress_card"):
            # /new redraws start from defaults; both toggle choices must keep
            # the presentation settings already carried by this draft.
            _, settings = _decode_progress_card_reference(value, element["name"])
            for option in element["options"]:
                enabled, _ = _decode_progress_card_reference(option["value"], element["name"])
                option["value"] = _progress_card_reference(enabled, settings)
        if not any(option["value"] == value for option in element["options"]):
            label = "保留已选值（当前不可用）"
            if element["name"].endswith("_model"):
                label += " · " + str(_decode_new_model_reference(value))
            element["options"].append({"text": _plain_text(label), "value": value})
        element["initial_option"] = value


def _form_columns(*columns: tuple[int, list[dict[str, Any]]]) -> dict[str, Any]:
    """Arrange existing controls without changing their submitted values."""
    rendered = []
    for weight, elements in columns:
        styled = []
        for element in elements:
            if element["tag"] in {"select_static", "input"}:
                element = {**element, "width": "fill"}
            elif element["tag"] == "markdown" and element["content"].startswith("<font color='grey'>"):
                element = {**element, "text_size": "notation"}
            styled.append(element)
        rendered.append({
            "tag": "column", "width": "weighted", "weight": weight,
            "vertical_spacing": "8px", "vertical_align": "center", "elements": styled,
        })
    return {
        "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
        "columns": rendered,
    }


def _form_section(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return _form_columns((1, [{"tag": "markdown", "content": f"**{title}**"}, *elements]))


def _session_settings_sections(
    *,
    model_fields: list[dict[str, Any]],
    context_fields: list[dict[str, Any]],
    feedback_fields: list[dict[str, Any]],
    model_notices: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Share presentation while each caller owns field values and validation."""
    model_elements = [*model_notices, model_fields[1]]
    if len(model_fields) > 2:
        # These are fixed form labels, not submitted text. Markdown avoids a
        # separate div/plain_text pair in cards that also contain search pages.
        model_elements.append(_form_columns(*(
            (1, [{"tag": "markdown", "content": label["text"]["content"]}, select])
            for label, select in zip(model_fields[2::2], model_fields[3::2], strict=True)
        )))
    sections = [_form_section("模型配置", model_elements)]
    if context_fields:
        sections.append(_form_section("消息上下文", [
            context_fields[1],
            _form_hint("机器人始终只响应 @。选择“自动带上”会把两次 @ 之间未 @ 机器人的群聊消息交给 Codex。"),
        ]))
    feedback_hints = (
        "执行时闪烁；关闭仍保留状态表情。部分移动端会显示为消息。",
        "展示执行状态与清单，结束后自动折叠。",
        "完成或失败时 @ 发起人；主动停止、定时自动首轮不提醒。",
    )
    sections.append(_form_section("任务反馈", [
        _form_columns((3, [{"tag": "markdown",
            "content": label["text"]["content"] + "\n<font color='grey'>" + hint + "</font>"}]), (1, [select]))
        for label, select, hint in zip(
            feedback_fields[::3], feedback_fields[1::3], feedback_hints, strict=True,
        )
    ]))
    return sections


def _workspace_reference(options: Mapping[str, Any], choice: str) -> str:
    return (
        f"workspace:v1:{options['projectAlias']}:{options['projectRevision']}:"
        + choice
    )


def _new_workspace_elements(options: Mapping[str, Any]) -> list[dict[str, Any]]:
    choices = [
        ("项目目录（不使用 worktree）", _workspace_reference(options, "project")),
        ("新建 worktree", _workspace_reference(options, "new")),
    ]
    for item in options.get("items", ()):
        if item.get("state") != "ready" or item.get("available") is not True:
            continue
        branch = item.get("branch") or "detached"
        choices.append((
            f"已有 worktree · {branch} · {item['cwd']}",
            _workspace_reference(options, "id:" + item["id"]),
        ))
    return [
        _form_label("worktree（切换项目后请重新加载选项）"),
        _static_select(
            name="new_workspace", placeholder="选择 worktree", options=tuple(choices),
            initial_option=_workspace_reference(options, "project"),
        ),
        _form_label("以下分支配置仅在“新建 worktree”时生效；已有 worktree 不会切换分支"),
        _static_select(
            name="new_worktree_mode", placeholder="选择检出方式",
            options=(("Detached（不绑定命名分支）", "detached"),
                     ("检出已有本地分支（不能已被占用）", "existing"),
                     ("创建并检出新分支", "new")),
            initial_option="detached",
        ),
        _form_label("起点 revision；检出已有分支时填写本地分支名"),
        {"tag": "input", "name": "new_worktree_ref", "max_length": 256,
         "placeholder": _plain_text("HEAD、分支、标签或提交"), "default_value": "HEAD"},
        {"tag": "input", "name": "new_worktree_branch", "max_length": 255,
         "label": _plain_text("新分支名（仅创建新分支时填写）"),
         "placeholder": _plain_text("例如：feature/my-task")},
    ]


def _new_workspace_load_button() -> dict[str, Any]:
    button = _form_submit_button(name="new_workspace_load_v1", label="加载 worktree 选项")
    button["type"] = "default"
    button["behaviors"] = [{"type": "callback", "value": {
        "v": ACTION_VERSION, "intent": CardControlName.PREPARE_NEW_WORKSPACE.value,
        "nonce": _new_callback_nonce(),
    }}]
    return button


def _binding_config_form(
    *,
    binding_id: str,
    settings_revision: int,
    context_revision: int,
    feedback_revision: int,
    turn_settings: BindingTurnSettings | None,
    message_context_mode: MentionContextMode,
    task_feedback: BindingTaskFeedback,
    allow_context_mode: bool,
    catalog: ModelCatalog | None,
) -> dict[str, Any]:
    def model_reference(model_id: str | None) -> str:
        return _config_model_reference(
            binding_id=binding_id,
            settings_revision=settings_revision,
            context_revision=context_revision,
            feedback_revision=feedback_revision,
            model_id=model_id,
        )

    if catalog is None:
        model_fields = [
            _form_label("Model"),
            _static_select(
                name="config_model",
                placeholder="选择 Model 来源",
                options=(("继承 Codex", model_reference(None)),),
                initial_option=model_reference(None),
            ),
        ]
    else:
        model_fields = _model_settings_form_elements(
            prefix="config",
            catalog=catalog,
            turn_settings=turn_settings,
            model_value_encoder=model_reference,
            inherit_initial_when_unset=True,
        )
    elements = _session_settings_sections(
        model_fields=model_fields,
        context_fields=(
            _context_mode_form_elements(prefix="config", initial_mode=message_context_mode)
            if allow_context_mode else []
        ),
        feedback_fields=_task_feedback_form_elements(prefix="config", initial=task_feedback),
    )
    elements.append(
        _form_submit_button(
            name="binding_config_submit_v6",
            label="保存会话配置",
        )
    )
    return {
        "tag": "form",
        "name": "binding_config_v6",
        "vertical_spacing": "12px",
        "elements": elements,
    }


def _model_settings_form_elements(
    *,
    prefix: str,
    catalog: ModelCatalog,
    turn_settings: BindingTurnSettings | None = None,
    model_value_encoder: Callable[[str | None], str],
    inherit_initial_when_unset: bool,
    model_label: str = "Model",
) -> list[dict[str, Any]]:
    default = catalog.default_model
    model_id: str | None = (
        None if inherit_initial_when_unset else default.id
    )
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
                [("继承 Codex", model_value_encoder(None))]
                + [
                    (
                        model.display_name
                        + (" · 默认" if model.is_default else ""),
                        model_value_encoder(model.id),
                    )
                    for model in catalog.models
                ]
            ),
            initial_option=model_value_encoder(model_id),
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


def context_mode_display(mode: MentionContextMode) -> str:
    if mode is MentionContextMode.CATCH_UP:
        return "自动带上期间的群聊讨论"
    return "仅这条 @ 消息"


def _context_mode_form_elements(
    *,
    prefix: str,
    initial_mode: MentionContextMode,
) -> list[dict[str, Any]]:
    return [
        _form_label("@ 时读取的消息范围"),
        _static_select(
            name=f"{prefix}_context_mode",
            placeholder="选择 @ 时读取的消息范围",
            options=(
                (
                    "仅这条 @ 消息（默认）",
                    _context_mode_reference(MentionContextMode.CURRENT_ONLY),
                ),
                (
                    "自动带上期间的群聊讨论",
                    _context_mode_reference(MentionContextMode.CATCH_UP),
                ),
            ),
            initial_option=_context_mode_reference(initial_mode),
        ),
        _form_hint(
            "机器人始终只响应 @ 它的消息。选择“自动带上”后，"
            "两次 @ 之间群里其他成员未 @ 机器人的消息，"
            "也会在下一次 @ 时被读取，作为背景交给 Codex。"
        ),
    ]


def _task_feedback_form_elements(
    *,
    prefix: str,
    initial: BindingTaskFeedback,
) -> list[dict[str, Any]]:
    return [
        _form_label("执行中表情闪烁"),
        _static_select(
            name=f"{prefix}_task_reactions",
            placeholder="选择是否显示执行中表情闪烁",
            options=(
                ("关闭（默认）", _task_feedback_reference(False)),
                ("开启", _task_feedback_reference(True)),
            ),
            initial_option=_task_feedback_reference(
                initial.reaction_pulse_enabled
            ),
        ),
        _form_hint(
            "任务接收、成功调整和结束时始终显示表情。"
            "开启后，执行中还会间歇显示动态表情；"
            "部分移动端可能将其显示为单独消息。"
        ),
        _form_label("进度卡"),
        _static_select(
            name=f"{prefix}_progress_card",
            placeholder="选择是否使用进度卡",
            options=(
                ("关闭", _progress_card_reference(False, initial.progress_card_settings)),
                ("开启（默认）", _progress_card_reference(True, initial.progress_card_settings)),
            ),
            initial_option=_progress_card_reference(
                initial.progress_card_enabled, initial.progress_card_settings,
            ),
        ),
        _form_hint(
            "开启后会逐步更新任务状态与清单；"
            "完成后执行过程自动折叠。"
        ),
        _form_label("结束时 @ 提醒"),
        _static_select(
            name=f"{prefix}_completion_mention",
            placeholder="选择是否在任务结束时 @ 提醒",
            options=(
                ("关闭", _task_feedback_reference(False)),
                ("开启（默认）", _task_feedback_reference(True)),
            ),
            initial_option=_task_feedback_reference(initial.completion_mention_enabled),
        ),
        _form_hint("任务完成或失败时 @ 本轮发起人；主动停止不提醒。定时任务自动首轮不提醒。"),
    ]


def _progress_card_reference(enabled: bool, settings: ProgressCardSettings | None) -> str:
    # Ordinary Feishu forms only edit the existing toggle. Carry the Admin
    # presentation choices with both options, without a card session or new UI.
    values = (
        [enabled, None] if settings is None else
        [enabled, *(getattr(settings, name) for name in _PROGRESS_CARD_FIELDS)]
    )
    return _PROGRESS_CARD_REFERENCE_PREFIX + json.dumps(values, separators=(",", ":"))


def _decode_progress_card_reference(value: Any, field: str) -> tuple[bool, ProgressCardSettings | None]:
    value = _bounded_string(value, field, max_chars=128)
    if not value.startswith(_PROGRESS_CARD_REFERENCE_PREFIX):
        return _decode_task_feedback_reference(value, field), ProgressCardSettings()
    try:
        values = json.loads(value[len(_PROGRESS_CARD_REFERENCE_PREFIX):])
        if not isinstance(values, list) or not values or type(values[0]) is not bool:
            raise ValueError("invalid progress card reference")
        settings = (
            None if values[1:] == [None] else
            ProgressCardSettings.from_dict(dict(zip(_PROGRESS_CARD_FIELDS, values[1:], strict=True)))
        )
    except (ValueError, TypeError) as error:
        raise CardActionError("过程卡设置无效，请重新打开配置卡片。") from error
    return values[0], settings


def _decode_task_feedback_fields(payload: Mapping[str, Any], *, prefix: str) -> BindingTaskFeedback:
    progress_field = f"{prefix}_progress_card"
    progress_enabled, settings = _decode_progress_card_reference(payload[progress_field], progress_field)
    return BindingTaskFeedback(
        _decode_task_feedback_reference(payload[f"{prefix}_task_reactions"], f"{prefix}_task_reactions"),
        progress_enabled,
        _decode_task_feedback_reference(payload[f"{prefix}_completion_mention"], f"{prefix}_completion_mention"),
        settings,
    )


def session_settings_form_elements(
    *,
    prefix: str,
    settings: SessionSettings,
    catalog: ModelCatalog | None,
    catalog_error: str | None = None,
    allow_context_mode: bool,
    grouped: bool = False,
    settings_draft: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Use ordinary controls while preserving unavailable explicit intent."""
    usable_catalog = catalog
    if catalog is not None:
        try:
            settings.validate_catalog(catalog)
        except ValueError:
            usable_catalog = None
            catalog_error = "已有 Model / Effort / Speed 暂不可用，当前选择完整保留。"
    model_notices: list[dict[str, Any]] = []
    if usable_catalog is not None:
        model_fields = _model_settings_form_elements(
            prefix=prefix, catalog=usable_catalog, turn_settings=settings.turn_settings,
            model_value_encoder=_new_model_reference, inherit_initial_when_unset=True,
        )
    else:
        model_notices.append(_notice(catalog_error or "模型目录暂不可用；可以保留原配置或明确选择继承 Codex。"))
        options = [("继承 Codex", _new_model_reference(None))]
        turn = settings.turn_settings
        if turn is not None:
            options.append(("保留已有显式配置 · " + turn.model_id, _new_model_reference(turn.model_id)))
        model_fields = [
            _form_label("Model"),
            _static_select(name=f"{prefix}_model", placeholder="选择 Model 来源", options=tuple(options),
                initial_option=_new_model_reference(turn.model_id if turn else None)),
        ]
        if turn is not None:
            for field, label, value in (("effort", "Effort", turn.effort_id), ("speed", "Speed", turn.service_tier_id)):
                model_fields.extend([
                    _form_label(label),
                    _static_select(name=f"{prefix}_{field}", placeholder=label,
                        options=((value, value),), initial_option=value),
                ])
        elif settings_draft is not None:
            for field, label in (("effort", "Effort"), ("speed", "Speed")):
                name = f"{prefix}_{field}"
                if name in settings_draft:
                    value = settings_draft[name]
                    model_fields.extend([
                        _form_label(label),
                        _static_select(name=name, placeholder=label,
                            options=((value, value),) if value else (), initial_option=value or None),
                    ])
    context = (
        _context_mode_form_elements(prefix=prefix, initial_mode=settings.message_context_mode)
        if allow_context_mode else []
    )
    feedback = _task_feedback_form_elements(prefix=prefix, initial=settings.task_feedback)
    fields = [*model_fields, *context, *feedback]
    _restore_settings_draft(fields, settings_draft)
    if grouped:
        return _session_settings_sections(
            model_fields=model_fields, context_fields=context, feedback_fields=feedback,
            model_notices=model_notices,
        )
    return [*model_notices, *fields]


def session_settings_summary(settings: SessionSettings, *, allow_context_mode: bool = True) -> str:
    turn = settings.turn_settings
    model = "Model：继承 Codex" if turn is None else (
        f"Model：{turn.model_id}\nEffort：{turn.effort_id}\nSpeed：{turn.service_tier_id}"
    )
    context = "\n" + _context_mode_summary(settings.message_context_mode) if allow_context_mode else ""
    return model + context + "\n" + _task_feedback_summary(settings.task_feedback)


def decode_session_settings_fields(
    payload: Mapping[str, Any], *, prefix: str, model_id: str | None,
) -> SessionSettings:
    """Shared value validation for ordinary and scheduled session forms."""
    context_field = f"{prefix}_context_mode"
    context_mode = _decode_context_mode_reference(_required_string(
        payload.get(context_field, _context_mode_reference(MentionContextMode.CURRENT_ONLY)), context_field,
    ))
    feedback = _decode_task_feedback_fields(payload, prefix=prefix)
    catalog_fields = {f"{prefix}_effort", f"{prefix}_speed"}
    has_catalog_fields = catalog_fields.issubset(payload)
    turn = None
    if model_id is not None and not has_catalog_fields:
        raise CardActionError("显式 Model 必须同时选择 Effort 与 Speed，请重新打开卡片。")
    if has_catalog_fields:
        effort_id = _bounded_string(payload[f"{prefix}_effort"], f"{prefix}_effort", max_chars=MAX_SETTING_ID_CHARS)
        speed_id = _bounded_string(payload[f"{prefix}_speed"], f"{prefix}_speed", max_chars=MAX_SETTING_ID_CHARS)
        if model_id is not None:
            turn = BindingTurnSettings(model_id, effort_id, speed_id)
    return SessionSettings(turn, feedback, context_mode)


def _require_completion_mention_field(payload: Mapping[str, Any], *, prefix: str) -> None:
    if f"{prefix}_completion_mention" not in payload:
        raise CardActionError("会话配置卡片已过期，请重新打开配置卡片。")


def decode_session_settings_form(payload: Mapping[str, Any], *, prefix: str) -> SessionSettings:
    _require_completion_mention_field(payload, prefix=prefix)
    fields = {f"{prefix}_{name}" for name in ("model", "task_reactions", "progress_card", "completion_mention")}
    context = {f"{prefix}_context_mode"} if f"{prefix}_context_mode" in payload else set()
    catalog = {f"{prefix}_effort", f"{prefix}_speed"}
    if frozenset(payload) not in {frozenset(fields | context), frozenset(fields | context | catalog)}:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")
    model_id = _decode_new_model_reference(_required_string(payload[f"{prefix}_model"], f"{prefix}_model"))
    return decode_session_settings_fields(payload, prefix=prefix, model_id=model_id)


def _form_label(label: str) -> dict[str, Any]:
    return {"tag": "div", "text": _plain_text(label)}


def _form_hint(text: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": f"<font color='grey'>{text}</font>",
    }


def _static_select(
    *,
    name: str,
    placeholder: str,
    options: tuple[tuple[str, str], ...],
    initial_option: str | None,
) -> dict[str, Any]:
    select = {
        "tag": "select_static",
        "name": name,
        "required": True,
        "placeholder": _plain_text(placeholder),
        "options": [
            {"text": _plain_text(label), "value": value}
            for label, value in options
        ],
    }
    if initial_option is not None:
        select["initial_option"] = initial_option
    return select


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
    initial_project_alias: str | None = None,
    catalog: ModelCatalog | None = None,
    catalog_error: str | None = None,
    allow_context_mode: bool = True,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
    workspace_options: Mapping[str, Any] | None = None,
    allow_workspace_selection: bool = False,
    workspace_error: str | None = None,
    settings_draft: Mapping[str, str] | None = None,
) -> OutboundCard:
    builder = _builder("新建会话", "选择项目，开始一次对话")
    builder.markdown(
        "默认在项目目录工作，也可为 Git 项目选择或新建 worktree。创建会话后，直接发送任务即可开始。"
        "创建会话本身不会启动任务。"
    )
    if not projects:
        builder.markdown(
            "还没有已启用的项目。请先发送 `/projects` 创建、登记或启用项目，"
            "再发送 `/new`。发送 `/help` 查看快速开始。"
        )
    else:
        builder.markdown(
            "不确定模型配置时，可选择“继承 Codex”；"
            "也可选择 Model（模型）、Effort（思考强度）和 Speed（速度），"
            "应用于这个会话后续的新任务。空闲时可通过 `/config` 调整。"
        )
        if catalog is None:
            builder.raw(
                _notice(
                    (catalog_error or "Model / Effort / Speed 暂不可用。")
                    + " 已填写的选择会保留，也可选择继承 Codex 创建会话。",
                    error=True,
                )
            )
        builder.raw(
            _new_binding_form(
                projects,
                catalog,
                initial_project_alias=initial_project_alias,
                allow_context_mode=allow_context_mode,
                message_context_mode=message_context_mode,
                task_feedback=(
                    task_feedback or SessionSettings.new_defaults(catalog).task_feedback
                ),
                workspace_options=workspace_options,
                allow_workspace_selection=allow_workspace_selection,
                workspace_error=workspace_error,
                settings_draft=settings_draft,
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
    catalog: ModelCatalog | None,
    context_revision: int = 1,
    feedback_revision: int = 1,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
    allow_context_mode: bool = True,
    catalog_error: str | None = None,
) -> OutboundCard:
    builder = _builder("当前会话配置", f"Netizen 会话编号：{short_id} · {project_alias}")
    builder.markdown(
        "Model 可继承 Codex，也可显式选择 Model / Effort / Speed；"
        "保存不会启动任务。Model 是模型，Effort 是思考强度，Speed 是速度。"
    )
    if catalog is None:
        builder.raw(
            _notice(
                (catalog_error or "Model / Effort / Speed 暂不可用。")
                + " 当前仍可清除显式配置并改为继承 Codex。",
                error=True,
            )
        )
    elif turn_settings is not None:
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
            context_revision=context_revision,
            feedback_revision=feedback_revision,
            turn_settings=turn_settings,
            message_context_mode=message_context_mode,
            task_feedback=task_feedback or BindingTaskFeedback(),
            allow_context_mode=allow_context_mode,
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
    builder = _builder("重命名当前会话", f"Netizen 会话编号：{short_id} · {project_alias}")
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
    builder = _builder("归档当前会话", f"Netizen 会话编号：{short_id} · {project_alias}")
    builder.markdown(
        f"即将归档：`{_md_code(title)}`\n"
        "Codex 会处理该 Thread 当前的原生活动后执行归档。"
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
    native_thread_id: str | None = None,
) -> OutboundCard:
    builder = _builder(
        "永久删除当前会话",
        f"Netizen 会话编号：{short_id} · {project_alias}",
        template="red",
    )
    if native_thread_id is None:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "该 Lazy 会话尚无原生历史；确认后只删除本地 Binding。"
        )
        confirm_body = "请再次确认：该本地会话映射将被永久删除。"
        callback_extra: dict[str, object] = {}
    else:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "Codex 会处理该 Thread 当前的原生活动。确认后将永久删除原生 "
            "Codex Thread、其 spawned descendants 与本地 "
            "Binding；Codex App/CLI 中的对应历史也会消失。"
        )
        confirm_body = (
            "请再次确认：原生会话、派生会话与本地 Binding 都将永久删除，无法恢复。"
        )
        callback_extra = {
            "expected_native_thread_id": _native_thread_reference(
                native_thread_id
            )
        }
    builder.raw(
        _callback_button(
            label="永久删除当前会话",
            value=_envelope(
                scope,
                CardControlName.DELETE_BINDING,
                binding_id=_binding_reference(binding_id),
                **callback_extra,
            ),
            confirm=(
                "永久删除且无法恢复",
                confirm_body,
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def sessions_delete_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
    native_thread_id: str | None,
    page: int,
    session_cursor: str | None = None,
) -> OutboundCard:
    builder = _builder(
        "永久删除会话",
        f"Netizen 会话编号：{short_id} · {project_alias}",
        template="red",
    )
    if native_thread_id is None:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "该 Lazy 会话尚无原生历史；确认后只永久删除本地 Binding。"
        )
        confirm_body = "请再次确认：该本地会话映射将被永久删除。"
    else:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "确认后将永久删除原生 Codex Thread、其 spawned descendants 与本地 "
            "Binding；Codex App/CLI 中的对应历史也会消失。"
        )
        confirm_body = (
            "请再次确认：原生会话、派生会话与本地 Binding 都将永久删除，无法恢复。"
        )
    expected_native_thread_id = (
        _native_thread_reference(native_thread_id)
        if native_thread_id is not None
        else None
    )
    builder.raw(
        _button_row(
            _callback_button(
                label="永久删除此会话",
                value=_envelope(
                    scope,
                    CardControlName.DELETE_EXACT_BINDING,
                    binding_id=_binding_reference(binding_id),
                    expected_native_thread_id=expected_native_thread_id,
                    page=page,
                    **({"session_cursor": session_cursor} if session_cursor else {}),
                ),
                style="danger",
                confirm=("永久删除且无法恢复", confirm_body),
            ),
            _repeatable_callback_button(
                label="返回会话列表",
                value=_sessions_page_envelope(scope=scope, target=page, cursor=session_cursor),
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def archived_sessions_card(
    *,
    scope: FeishuScope,
    sessions: tuple[ArchivedSessionCardItem, ...],
    native_delete_available: bool,
    notice: str | None = None,
    notice_is_error: bool = False,
    page: int = 0,
    session_cursor: str | None = None,
    previous_cursor: str | None = None,
    next_cursor: str | None = None,
) -> OutboundCard:
    session_cursor = _session_action_cursor(scope, session_cursor)
    builder = _builder("已归档会话", "恢复后自动切换")
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    builder.markdown(
        "已归档会话不会出现在普通 `/sessions` 中。恢复不会修改历史或会话配置；"
        "删除会永久移除原生 Thread、其 spawned descendants 与本地 Binding。\n"
        "更多管理请使用 Admin 会话列表，发送 `/admin` 打开。"
    )
    if not sessions:
        builder.markdown(
            "本页没有已归档会话。发送 `/sessions` 查看普通会话，"
            "或发送 `/new` 新建会话。"
        )
    for session in sessions:
        builder.raw(
            _archived_session_row(
                scope=scope,
                session=session,
                native_delete_available=native_delete_available,
                session_cursor=session_cursor,
            )
        )
    builder.raw(_sessions_pagination(
        scope=scope, page=page, previous_cursor=previous_cursor, next_cursor=next_cursor,
        state="archived",
    ))
    return _session_list_card(builder.to_dict())


def archived_sessions_delete_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
    native_thread_id: str,
    session_cursor: str | None = None,
) -> OutboundCard:
    builder = _builder(
        "永久删除已归档会话",
        f"Netizen 会话编号：{short_id} · {project_alias}",
        template="red",
    )
    builder.markdown(
        f"即将删除：`{_md_code(title)}`\n"
        "确认后将永久删除原生 Codex Thread、其 spawned descendants 与本地 "
        "Binding；Codex App/CLI 中的对应历史也会消失。"
    )
    builder.raw(
        _button_row(
            _callback_button(
                label="永久删除已归档会话",
                value=_envelope(
                    scope,
                    CardControlName.DELETE_ARCHIVED_BINDING,
                    binding_id=_binding_reference(binding_id),
                    expected_native_thread_id=_native_thread_reference(
                        native_thread_id
                    ),
                    **({"session_cursor": session_cursor} if session_cursor else {}),
                ),
                style="danger",
                confirm=(
                    "永久删除且无法恢复",
                    "原生会话、派生会话与本地 Binding 都将永久删除。",
                ),
            ),
            _repeatable_callback_button(
                label="返回归档列表",
                value=_envelope(
                    scope,
                    CardControlName.REFRESH_ARCHIVED_SESSIONS,
                    **({"session_cursor": session_cursor} if session_cursor else {}),
                ),
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def sessions_card(
    *,
    scope: FeishuScope,
    sessions: tuple[SessionCardItem, ...],
    native_delete_available: bool,
    active_binding_id: str | None,
    page: int = 0,
    notice: str | None = None,
    notice_is_error: bool = False,
    session_cursor: str | None = None,
    previous_cursor: str | None = None,
    next_cursor: str | None = None,
    current: SessionCardItem | None = None,
    lazy: bool = False,
    has_lazy: bool = False,
) -> OutboundCard:
    session_cursor = _session_action_cursor(scope, session_cursor)
    subtitle = "尚未开始对话的会话" if lazy else "按 Codex 更新时间排序"
    builder = _builder("会话", subtitle)
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    builder.markdown(
        "切换不会停止其他会话正在运行的任务。"
        "归档保留历史，可恢复；删除需红色确认，并级联删除派生会话。\n"
        "更多管理请使用 Admin 会话列表，发送 `/admin` 打开。"
    )
    if current is not None:
        builder.markdown("**当前会话**")
        builder.raw(_session_row(
            scope=scope, session=current, page=page,
            expected_active_binding_id=active_binding_id,
            native_delete_available=native_delete_available,
            session_cursor=session_cursor,
        ))
        builder.markdown("**本页会话**")
    if not sessions:
        builder.markdown(
            ("本页没有未开始的会话。" if lazy else "本页没有普通会话。")
            + "发送 `/new` 新建会话，"
            "或发送 `/sessions archived` 查找已归档会话。"
        )
    for session in sessions:
        builder.raw(
            _session_row(
                scope=scope,
                session=session,
                page=page,
                expected_active_binding_id=active_binding_id,
                native_delete_available=native_delete_available,
                session_cursor=session_cursor,
            )
        )

    builder.raw(_sessions_pagination(
        scope=scope, page=page, previous_cursor=previous_cursor, next_cursor=next_cursor,
        state="lazy" if lazy else "active", has_lazy=has_lazy,
    ))
    return _session_list_card(builder.to_dict())


def _session_action_cursor(scope: FeishuScope, token: str | None) -> str | None:
    if token is None:
        return None
    navigation = SessionCardPage.decode(token, scope)
    # Row actions need the exact return position, not a copy of the entire
    # back stack in every button. Navigation buttons retain that history.
    return SessionCardPage(
        navigation.state, navigation.page, (navigation.cursor,),
    ).encode(scope)


def _session_list_card(card: dict[str, Any]) -> OutboundCard:
    # Use the Channel SDK's outbound serialization, including every callback.
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > 55_000:
        raise CardActionError(
            "会话列表卡片内容过长，暂不能展示；请使用 Admin 会话列表。"
        )
    return OutboundCard(card=card)


def _session_row(
    *,
    scope: FeishuScope,
    session: SessionCardItem,
    page: int,
    expected_active_binding_id: str | None,
    native_delete_available: bool,
    session_cursor: str | None = None,
) -> dict[str, Any]:
    navigation = {"session_cursor": session_cursor} if session_cursor else {}
    expected_turn_id = (
        _turn_reference(session.turn_id) if session.turn_id is not None else None
    )
    marker_text = "● 当前" if session.active else "○"
    text = (
        f"{marker_text} {session.title}\n"
        f"Project：{session.project_alias} · 状态：{session.state}"
    )
    if session.catalog_unconfirmed:
        text += " · 归档状态：未确认"
    text += "\n" + "\n".join(session_identity_lines(
        short_id=session.short_id, native_thread_id=session.native_thread_id,
    ))
    controls: list[dict[str, Any]] = []
    if not session.active:
        controls.append(
            _repeatable_callback_button(
                label="设为当前",
                value=_envelope(
                    scope,
                    CardControlName.ACTIVATE_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    **navigation,
                ),
                style="primary_filled",
            )
        )
    if session.native_thread_id is not None:
        controls.append(
            _callback_button(
                label="归档",
                value=_envelope(
                    scope,
                    CardControlName.ARCHIVE_EXACT_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    **navigation,
                    page=page,
                ),
                confirm=(
                    "确认归档此会话？",
                    (
                        f"将归档 Netizen 会话 {session.short_id}（{session.title}）。"
                        "历史不会删除，之后可以恢复。"
                    ),
                ),
            )
        )
    if (
        (
            session.native_thread_id is None
            and session.state == SESSION_IDLE_STATE
        )
        or (
            session.native_thread_id is not None
            and native_delete_available
        )
    ):
        controls.append(
            _repeatable_callback_button(
                label="删除",
                value=_envelope(
                    scope,
                    CardControlName.PREPARE_EXACT_DELETE_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    **navigation,
                    expected_native_thread_id=(
                        _native_thread_reference(session.native_thread_id)
                        if session.native_thread_id is not None
                        else None
                    ),
                    page=page,
                ),
                style="danger",
            )
        )
    if session_stop_available(session.state):
        controls.append(
            _callback_button(
                label="停止",
                value=_envelope(
                    scope,
                    CardControlName.STOP_EXACT_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    **navigation,
                    expected_active_binding_id=(
                        _binding_reference(expected_active_binding_id)
                        if expected_active_binding_id is not None
                        else None
                    ),
                    expected_activity_revision=session.activity_revision,
                    expected_turn_id=expected_turn_id,
                    page=page,
                ),
            )
        )
    if (
        session.state == ActiveState.OBSERVATION_UNAVAILABLE.value
        and session.turn_id is not None
    ):
        controls.append(
            _repeatable_callback_button(
                label="重新检查",
                value=_envelope(
                    scope,
                    CardControlName.RECHECK_EXACT_TURN,
                    binding_id=_binding_reference(session.binding_id),
                    **navigation,
                    expected_active_binding_id=(
                        _binding_reference(expected_active_binding_id)
                        if expected_active_binding_id is not None
                        else None
                    ),
                    expected_activity_revision=session.activity_revision,
                    expected_turn_id=expected_turn_id,
                    page=page,
                ),
            )
        )
    columns = [
        {
            "tag": "column",
            "width": "weighted",
            "weight": 5,
            "padding": "8px",
            "elements": [_plain(text)],
        },
    ]
    if controls:
        columns.extend(
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": [control],
            }
            for control in controls
        )
    background = "blue-50" if session.active else "grey-50"
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": background,
        "margin": "0 0 8px 0",
        "columns": columns,
    }


def _sessions_pagination(
    *,
    scope: FeishuScope,
    page: int,
    previous_cursor: str | None,
    next_cursor: str | None,
    state: str = "active",
    has_lazy: bool = False,
) -> dict[str, Any]:
    buttons = []
    if previous_cursor is not None:
        buttons.append(_repeatable_callback_button(
            label="上一页", value=_sessions_page_envelope(
                scope=scope, target=page - 1, cursor=previous_cursor,
            ),
        ))
    if next_cursor is not None:
        buttons.append(_repeatable_callback_button(
            label="下一页", value=_sessions_page_envelope(
                scope=scope, target=page + 1, cursor=next_cursor,
            ),
        ))
    # A fresh first-page query is always available, including after native
    # cursor expiry or when the bounded back stack no longer reaches page one.
    buttons.append(_repeatable_callback_button(
        label="返回首页" if page else "刷新列表",
        value=_sessions_page_envelope(
            scope=scope, target=0, cursor=SessionCardPage(state).encode(scope),
        ),
    ))
    for target, label in (("active", "普通会话"), ("archived", "已归档"), ("lazy", "未开始")):
        if target == state or target == "lazy" and not has_lazy:
            continue
        buttons.append(_repeatable_callback_button(
            label=label, value=_sessions_page_envelope(
                scope=scope, target=0, cursor=SessionCardPage(target).encode(scope),
            ),
        ))
    return {
        "tag": "column_set", "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [{"tag": "markdown", "content": f"第 {page + 1} 页"}]},
            *({"tag": "column", "width": "auto", "elements": [button]} for button in buttons),
        ],
    }


def _sessions_page_envelope(
    *, scope: FeishuScope, target: int, cursor: str | None = None,
) -> dict[str, Any]:
    return _envelope(
        scope, CardControlName.SESSIONS_PAGE, page=target,
        **({"session_cursor": cursor} if cursor else {}),
    )


def binding_lifecycle_result_card(
    *,
    title: str,
    short_id: str,
    project_alias: str,
    message: str,
) -> OutboundCard:
    builder = _builder(
        title,
        f"Netizen 会话编号：{short_id} · {project_alias}",
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
    goal_generation: str | None = None,
) -> OutboundCard:
    generation = (
        goal_generation
        if goal_generation is not None
        else (None if goal is None else _goal_generation(goal))
    )
    return reply_card(
        ReplyCardProjection(
            scope=scope,
            goal=ReplyCardGoalModule(
                binding_id=binding_id,
                short_id=short_id,
                project_alias=project_alias,
                goal_generation=generation,
                status=None if goal is None else goal.status.value,
                runtime_state=runtime_state,
                objective=None if goal is None else goal.objective,
                token_budget=None if goal is None else goal.token_budget,
                tokens_used=0 if goal is None else goal.tokens_used,
                notice=notice,
                notice_is_error=notice_is_error,
            ),
        )
    )


def side_topic_card(
    *,
    scope: FeishuScope,
    side_id: str,
    parent_short_id: str,
    creator_id: str,
    created_at: str,
    state: SideTopicState,
    context: SideTopicCardContext = SideTopicCardContext(),
    requires_mention: bool = False,
    show_creator_profile: bool = True,
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
        "Side 临时对话",
        labels[state],
        template=templates[state],
    )
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    # Native names and filesystem paths are display data, never card markup.
    builder.raw(_plain(f"来源会话：{context.parent_title}"))
    if context.project_alias is not None:
        builder.raw(_plain(f"所属项目：{context.project_alias}"))
    if show_creator_profile and valid_completion_mention_user_id(creator_id):
        builder.raw({
            "tag": "column_set",
            "vertical_align": "center",
            "columns": [
                {"tag": "column", "width": "auto", "elements": [_plain("发起人：")]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                    "tag": "person", "user_id": creator_id, "size": "small",
                    "show_avatar": True, "show_name": True,
                }]},
            ],
        })
    else:
        builder.raw(_plain(f"发起人：{context.creator_name or '姓名暂不可用'}"))
    builder.markdown(f"**创建时间**：{_side_created_time(created_at)}")
    if state is SideTopicState.OPEN:
        builder.raw(_plain(
            "在本话题 @ 机器人继续提问。" if requires_mention
            else "在本话题继续提问。"
        ))
    elif state in {SideTopicState.CLOSED, SideTopicState.EXPIRED}:
        builder.raw(_plain(
            "需要继续讨论时，请回到来源位置，选择原会话后重新发送 /side。"
        ))
    builder.raw(_plain(
        "继承原会话创建时的上下文，之后独立对话；双方共享项目文件。"
        "空闲两小时或服务重启后失效，历史消息仍保留在飞书中。"
    ))
    if state is SideTopicState.OPEN and scope.kind is not ScopeKind.TOPIC:
        raise ValueError("an open Side card requires its exact Topic scope")
    buttons: list[dict[str, Any]] = []
    if (
        state in {SideTopicState.CREATING, SideTopicState.OPEN}
        and scope.kind is ScopeKind.TOPIC
    ):
        creating = state is SideTopicState.CREATING
        buttons.append(
            _repeatable_callback_button(
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
    if context.source_url is not None:
        buttons.append({
            "tag": "button", "type": "default",
            "text": _plain_text("查看来源话题" if context.source_is_topic else "查看来源聊天"),
            "behaviors": [{"type": "open_url", "default_url": context.source_url}],
        })
    if buttons:
        builder.raw(_button_row(*buttons))
    details = [f"原会话编号：{parent_short_id}", f"Side 编号：{side_id[:8]}"]
    if context.can_resume_parent:
        details.append(f"在来源位置选择原会话：/resume {parent_short_id}")
    if context.project_cwd is not None:
        details.append(f"项目工作目录：{context.project_cwd}")
    builder.raw({
        "tag": "collapsible_panel", "expanded": False,
        "header": {"title": _plain_text("会话详情")},
        "elements": [_plain("\n".join(details))],
    })
    return OutboundCard(card=builder.to_dict())


def _side_created_time(created_at: str) -> str:
    try:
        value = datetime.fromisoformat(created_at)
        if value.utcoffset() is None:
            return "时间暂不可用"
        timestamp = int(value.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return "时间暂不可用"
    return (
        f"<local_datetime millisecond='{timestamp}' format_type='date_num'></local_datetime> "
        f"<local_datetime millisecond='{timestamp}' format_type='time'></local_datetime>"
    )


def binding_created_card(
    *,
    short_id: str,
    project_alias: str,
    settings: TurnModelSettings | None = None,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
    workspace_cwd: str | None = None,
) -> OutboundCard:
    builder = _builder(
        "会话创建成功",
        f"{project_alias} · Netizen 会话编号：{short_id}",
        template="green",
    )
    builder.markdown(
        f"✅ 已选择项目 `{_md_code(project_alias)}`，"
        f"并创建、切换到 Netizen 会话 **{short_id}**。"
    )
    if workspace_cwd is not None:
        builder.markdown(f"实际工作目录：`{_md_code(workspace_cwd)}`。")
    builder.markdown(
        "现在可以直接发送任务，例如：**梳理这个项目的结构。**\n"
        "如果刚才的任务因没有会话而未执行，请重新发送。\n"
        "在群聊和群话题中，每条消息都需要 @机器人。"
    )
    builder.markdown(_model_source_summary(settings))
    builder.markdown(_context_mode_summary(message_context_mode))
    builder.markdown(
        _task_feedback_summary(task_feedback or BindingTaskFeedback())
    )
    return OutboundCard(card=builder.to_dict())


def binding_configured_card(
    *,
    short_id: str,
    project_alias: str,
    settings: TurnModelSettings | None,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
) -> OutboundCard:
    builder = _builder(
        "会话配置已保存",
        f"{project_alias} · Netizen 会话编号：{short_id}",
        template="green",
    )
    builder.markdown(_model_source_summary(settings))
    builder.markdown(_context_mode_summary(message_context_mode))
    builder.markdown(
        _task_feedback_summary(task_feedback or BindingTaskFeedback())
    )
    return OutboundCard(card=builder.to_dict())


def _model_source_summary(settings: TurnModelSettings | None) -> str:
    if settings is None:
        return "Model 来源：继承 Codex，使用 Codex 自身的模型配置。"
    return (
        "Model 来源：Netizen 会话显式配置。后续新任务将使用："
        f"Model=`{_md_code(settings.model)}` · "
        f"Effort=`{_md_code(settings.effort_id)}` · "
        f"Speed=`{_md_code(settings.service_tier_name)}`"
    )


def _context_mode_summary(mode: MentionContextMode) -> str:
    if mode is MentionContextMode.CATCH_UP:
        return (
            "@ 时读取的消息范围：自动带上期间的群聊讨论。"
            "两次 @ 之间群里未 @ 机器人的成员消息，"
            "也会在下一次 @ 时作为背景交给 Codex。"
        )
    return "@ 时读取的消息范围：仅这条 @ 消息。"


def _task_feedback_summary(feedback: BindingTaskFeedback) -> str:
    pulse = "开启" if feedback.reaction_pulse_enabled else "关闭"
    progress = "开启" if feedback.progress_card_enabled else "关闭"
    mention = "开启" if feedback.completion_mention_enabled else "关闭"
    return f"执行中表情闪烁：{pulse}\n进度卡：{progress}\n结束时 @ 提醒：{mention}"


def error_card(message: str, *, scope: FeishuScope | None = None) -> OutboundCard:
    builder = _builder("操作失败", "请修正后重试", template="red")
    builder.raw(_notice(message, error=True))
    if scope is not None:
        builder.raw(
            _repeatable_callback_button(
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
    payload.pop("nonce", None)
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
        CardControlName.PREPARE_NEW_WORKSPACE,
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
        CardControlName.ARCHIVE_EXACT_BINDING: {
            "binding_id",
            "page",
        },
        CardControlName.DELETE_BINDING: {
            "binding_id",
        },
        CardControlName.PREPARE_EXACT_DELETE_BINDING: {
            "binding_id",
            "expected_native_thread_id",
            "page",
        },
        CardControlName.DELETE_EXACT_BINDING: {
            "binding_id",
            "expected_native_thread_id",
            "page",
        },
        CardControlName.PREPARE_ARCHIVED_DELETE_BINDING: {
            "binding_id",
            "expected_native_thread_id",
        },
        CardControlName.DELETE_ARCHIVED_BINDING: {
            "binding_id",
            "expected_native_thread_id",
        },
        CardControlName.UNARCHIVE_BINDING: {"binding_id"},
        CardControlName.ACTIVATE_BINDING: {"binding_id"},
        CardControlName.STOP_EXACT_BINDING: {
            "binding_id",
            "expected_active_binding_id",
            "expected_activity_revision",
            "expected_turn_id",
            "page",
        },
        CardControlName.RECHECK_EXACT_TURN: {
            "binding_id",
            "expected_active_binding_id",
            "expected_activity_revision",
            "expected_turn_id",
            "page",
        },
        CardControlName.SESSIONS_PAGE: {"page"},
        CardControlName.REFRESH_ARCHIVED_SESSIONS: set(),
        CardControlName.GOAL_REFRESH: {"binding_id"},
        CardControlName.GOAL_PAUSE: {
            "binding_id",
            "goal_generation",
            "expected_goal_status",
        },
        CardControlName.GOAL_RESUME: {
            "binding_id",
            "goal_generation",
            "expected_goal_status",
        },
        CardControlName.GOAL_CLEAR: {
            "binding_id",
            "goal_generation",
            "expected_goal_status",
        },
        CardControlName.SIDE_CLOSE: {"side_id"},
    }
    try:
        scope_kind = ScopeKind(payload.get("scope_kind"))
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    scope_fields = {"topic_id"} if scope_kind is ScopeKind.TOPIC else set()
    expected = common | scope_fields | extra_by_name[name]
    session_navigation_names = {
        CardControlName.ACTIVATE_BINDING, CardControlName.ARCHIVE_EXACT_BINDING,
        CardControlName.PREPARE_EXACT_DELETE_BINDING, CardControlName.DELETE_EXACT_BINDING,
        CardControlName.PREPARE_ARCHIVED_DELETE_BINDING, CardControlName.DELETE_ARCHIVED_BINDING,
        CardControlName.STOP_EXACT_BINDING, CardControlName.RECHECK_EXACT_TURN,
        CardControlName.SESSIONS_PAGE, CardControlName.REFRESH_ARCHIVED_SESSIONS,
    }
    if name in session_navigation_names and "session_cursor" in payload:
        expected.add("session_cursor")
    if (
        name is CardControlName.DELETE_BINDING
        and "expected_native_thread_id" in payload
    ):
        expected.add("expected_native_thread_id")
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
    expected_active_binding_id = None
    expected_native_thread_id = None
    side_id = None
    revision = None
    enabled = None
    section = None
    page = None
    session_cursor = None
    expected_activity_revision = None
    expected_turn_id = None
    goal_generation_value = None
    expected_goal_status = None
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
    if "expected_active_binding_id" in payload:
        raw_active_binding_id = payload["expected_active_binding_id"]
        if raw_active_binding_id is not None:
            expected_active_binding_id = _decode_binding_reference(
                _required_string(
                    raw_active_binding_id,
                    "expected_active_binding_id",
                )
            )
    if "expected_native_thread_id" in payload:
        raw_native_thread_id = payload["expected_native_thread_id"]
        if raw_native_thread_id is not None:
            expected_native_thread_id = _decode_native_thread_reference(
                _required_string(
                    raw_native_thread_id,
                    "expected_native_thread_id",
                )
            )
    if "expected_activity_revision" in payload:
        raw_activity_revision = payload["expected_activity_revision"]
        if (
            isinstance(raw_activity_revision, bool)
            or not isinstance(raw_activity_revision, int)
            or raw_activity_revision < 0
        ):
            raise CardActionError(
                "expected_activity_revision 必须是非负整数。"
            )
        expected_activity_revision = raw_activity_revision
    if "expected_turn_id" in payload:
        raw_turn_id = payload["expected_turn_id"]
        if raw_turn_id is not None:
            expected_turn_id = _decode_turn_reference(
                _required_string(raw_turn_id, "expected_turn_id")
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
    if "page" in payload:
        raw_page = payload["page"]
        if (
            isinstance(raw_page, bool)
            or not isinstance(raw_page, int)
            or raw_page < 0
        ):
            raise CardActionError("页码必须是非负整数。")
        page = raw_page
    if "session_cursor" in payload:
        session_cursor = _bounded_string(payload["session_cursor"], "session_cursor", max_chars=1024)
        try:
            navigation = SessionCardPage.decode(session_cursor, scope)
        except ValueError as error:
            raise CardActionError(str(error)) from error
        if page is not None and page != navigation.page:
            raise CardActionError("会话分页已过期，请重新发送 /sessions。")
    if "goal_generation" in payload:
        goal_generation_value = _decode_goal_generation(
            payload["goal_generation"]
        )
    if "expected_goal_status" in payload:
        expected_goal_status = _required_string(
            payload["expected_goal_status"],
            "expected_goal_status",
        )
        valid_goal_statuses = {item.value for item in GoalStatus}
        if expected_goal_status not in valid_goal_statuses:
            raise CardActionError("Goal 预期状态无效。")
        expected_by_action = {
            CardControlName.GOAL_PAUSE: {GoalStatus.ACTIVE.value},
            CardControlName.GOAL_RESUME: {GoalStatus.PAUSED.value},
            CardControlName.GOAL_CLEAR: valid_goal_statuses,
        }
        if expected_goal_status not in expected_by_action[name]:
            raise CardActionError("Goal 动作与预期状态不一致。")
    if name is CardControlName.SET_PROJECT_ENABLED:
        section = SettingsSection.PROJECTS
    if name is CardControlName.SIDE_CLOSE and scope.kind is not ScopeKind.TOPIC:
        raise CardActionError("Side 结束动作必须来自原 Side 话题。")
    if (
        name is CardControlName.RECHECK_EXACT_TURN
        and expected_turn_id is None
    ):
        raise CardActionError("Turn 重新检查动作缺少 exact Turn。")
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
        expected_active_binding_id=expected_active_binding_id,
        expected_native_thread_id=expected_native_thread_id,
        expected_activity_revision=expected_activity_revision,
        expected_turn_id=expected_turn_id,
        side_id=side_id,
        page=page,
        session_cursor=session_cursor,
        goal_generation=goal_generation_value,
        expected_goal_status=expected_goal_status,
    )


def decode_card_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Any,
    value: Any = None,
) -> CardControlIntent:
    if not isinstance(form_value, Mapping):
        raise CardActionError("卡片表单值必须是对象。")
    fields = set(form_value)
    has_new = any(field.startswith("new_") for field in fields)
    has_config = any(field.startswith("config_") for field in fields)
    has_rename = any(field.startswith("rename_name_v1__") for field in fields)
    if sum((has_new, has_config, has_rename)) > 1:
        raise CardActionError("卡片表单混合了不同操作的字段。")
    if value is not None and value != {} and not has_new:
        raise CardActionError("卡片表单操作与字段不匹配。")
    if has_new:
        return _decode_new_binding_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
            value=value,
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
    value: Any,
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("会话配置表单回调不完整。")
    payload = dict(form_value)
    load_workspace = value is not None and value != {}
    if load_workspace and (
        not isinstance(value, Mapping)
        or set(value) - {"v", "intent", "nonce"}
        or type(value.get("v")) is not int or value.get("v") != ACTION_VERSION
        or value.get("intent") != CardControlName.PREPARE_NEW_WORKSPACE.value
    ):
        raise CardActionError("新建会话表单操作无效，请重新发送 /new。")
    _require_completion_mention_field(payload, prefix="new")
    workspace = {}
    workspace_fields = {
        "new_workspace", "new_worktree_mode", "new_worktree_ref", "new_worktree_branch",
    }
    if workspace_fields & set(payload):
        if not load_workspace and not workspace_fields - {"new_worktree_branch"} <= set(payload):
            raise CardActionError("worktree 表单不完整，请重新加载 worktree 选项。")
        workspace = {name: payload.pop(name) for name in workspace_fields if name in payload}
    base_fields = {
        "new_project",
        "new_model",
        "new_task_reactions",
        "new_progress_card",
        "new_completion_mention",
    }
    context_fields = (
        {"new_context_mode"} if "new_context_mode" in payload else set()
    )
    catalog_fields = {"new_effort", "new_speed"}
    if frozenset(payload) not in {
        frozenset(base_fields | context_fields),
        frozenset(base_fields | context_fields | catalog_fields),
    }:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")

    project_alias, expected_revision = _decode_project_reference(
        _required_string(payload["new_project"], "new_project")
    )
    model_id = _decode_new_model_reference(
        _required_string(payload["new_model"], "new_model")
    )
    settings = decode_session_settings_fields(payload, prefix="new", model_id=model_id)
    turn = settings.turn_settings
    worktree_id, worktree_create = (None, None) if load_workspace else _decode_new_workspace(
        workspace, project_alias, expected_revision,
    )
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=(CardControlName.PREPARE_NEW_WORKSPACE if load_workspace else CardControlName.CREATE_BINDING),
        project_alias=project_alias,
        expected_revision=expected_revision,
        model_id=model_id,
        effort_id=turn.effort_id if turn else None,
        service_tier_id=turn.service_tier_id if turn else None,
        message_context_mode=settings.message_context_mode,
        reaction_pulse_enabled=settings.task_feedback.reaction_pulse_enabled,
        progress_card_enabled=settings.task_feedback.progress_card_enabled,
        completion_mention_enabled=settings.task_feedback.completion_mention_enabled,
        progress_card_settings=settings.task_feedback.progress_card_settings,
        worktree_id=worktree_id,
        worktree_create=worktree_create,
        new_settings_draft=(
            {name: field for name, field in payload.items() if name != "new_project"}
            if load_workspace else None
        ),
    )


def _decode_new_workspace(
    payload: Mapping[str, Any], project_alias: str, project_revision: int,
) -> tuple[str | None, dict[str, str | None] | None]:
    if not payload:
        return None, None
    reference = _required_string(payload["new_workspace"], "new_workspace")
    match = re.fullmatch(
        r"workspace:v1:([a-z0-9][a-z0-9_-]{0,63}):([1-9][0-9]*):(project|new|id:([A-Za-z0-9_-]{1,128}))",
        reference,
    )
    if match is None:
        raise CardActionError("worktree 选择无效，请重新加载 worktree 选项。")
    mode = _required_string(payload["new_worktree_mode"], "new_worktree_mode")
    ref_value = payload["new_worktree_ref"]
    ref = _bounded_string(ref_value, "new_worktree_ref", max_chars=256).strip() if ref_value != "" else ""
    branch_value = payload.get("new_worktree_branch")
    branch = (
        _bounded_string(branch_value, "new_worktree_branch", max_chars=255).strip()
        if branch_value not in (None, "") else ""
    )
    if mode not in {"detached", "existing", "new"}:
        raise CardActionError("未知的 worktree 分支配置。")
    choice = match.group(3)
    if choice == "project":
        return None, None
    if (match.group(1), int(match.group(2))) != (project_alias, project_revision):
        raise CardActionError("项目已切换，请先重新加载该项目的 worktree 选项。")
    if choice.startswith("id:"):
        # Creation settings cannot change an existing workspace's branch.
        return match.group(4), None
    if mode == "new" and not branch:
        raise CardActionError("创建新分支时请填写新分支名。")
    if mode == "existing":
        branch = ref.removeprefix("refs/heads/")
        if not branch or branch == "HEAD" or branch.startswith("refs/"):
            raise CardActionError("检出已有分支时，请填写本地分支名。")
        ref = "refs/heads/" + branch
    return None, {"mode": mode, "ref": ref or "HEAD", "branch": branch if mode != "detached" else None}


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
    _require_completion_mention_field(payload, prefix="config")
    base_fields = {
        "config_model",
        "config_task_reactions",
        "config_progress_card",
        "config_completion_mention",
    }
    context_fields = (
        {"config_context_mode"} if "config_context_mode" in payload else set()
    )
    catalog_fields = {"config_effort", "config_speed"}
    if frozenset(payload) not in {
        frozenset(base_fields | context_fields),
        frozenset(base_fields | context_fields | catalog_fields),
    }:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")
    (
        binding_id,
        expected_settings_revision,
        expected_context_revision,
        feedback_revision,
        model_id,
    ) = _decode_config_model_reference(
        _required_string(payload["config_model"], "config_model")
    )
    settings = decode_session_settings_fields(payload, prefix="config", model_id=model_id)
    turn = settings.turn_settings
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.CONFIGURE_BINDING,
        expected_settings_revision=expected_settings_revision,
        expected_context_revision=expected_context_revision,
        feedback_revision=feedback_revision,
        binding_id=binding_id,
        model_id=model_id,
        effort_id=turn.effort_id if turn else None,
        service_tier_id=turn.service_tier_id if turn else None,
        message_context_mode=settings.message_context_mode,
        reaction_pulse_enabled=settings.task_feedback.reaction_pulse_enabled,
        progress_card_enabled=settings.task_feedback.progress_card_enabled,
        completion_mention_enabled=settings.task_feedback.completion_mention_enabled,
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
    mode = _decode_project_mode_reference(
        _required_string(payload["project_mode"], "project_mode")
    )
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


def _archived_session_row(
    *,
    scope: FeishuScope,
    session: ArchivedSessionCardItem,
    native_delete_available: bool,
    session_cursor: str | None = None,
) -> dict[str, Any]:
    controls = [
        _callback_button(
            label="恢复并切换",
            value=_envelope(
                scope,
                CardControlName.UNARCHIVE_BINDING,
                binding_id=_binding_reference(session.binding_id),
            ),
            style="primary_filled",
        )
    ]
    if native_delete_available:
        controls.append(
            _repeatable_callback_button(
                label="删除",
                value=_envelope(
                    scope,
                    CardControlName.PREPARE_ARCHIVED_DELETE_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    expected_native_thread_id=_native_thread_reference(
                        session.native_thread_id
                    ),
                    **({"session_cursor": session_cursor} if session_cursor else {}),
                ),
                style="danger",
            )
        )
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
                        f"Project：{session.project_alias}\n"
                        + "\n".join(session_identity_lines(
                            short_id=session.short_id,
                            native_thread_id=session.native_thread_id,
                        ))
                    )
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
