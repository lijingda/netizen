"""Shared workspace-policy controls; card state never creates resources."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from ..worktrees.policy import WorkspacePolicy, WorkspacePolicyError
from .callbacks import CardActionError, _notice, _plain_text
from .controls import _form_hint


def workspace_field_names(prefix: str) -> set[str]:
    return {prefix + "_workspace", prefix + "_worktree_ref"}


def workspace_reference(project: str, revision: int | None, policy: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps({
        "project": project, "project_revision": revision,
        **{key: value for key, value in policy.items() if key != "ref"},
    }, ensure_ascii=False, separators=(",", ":")).encode()).decode().rstrip("=")


def read_workspace_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise CardActionError("工作区选项无效，请重新加载项目目录选项。")
    try:
        selected = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError) as error:
        raise CardActionError("工作区选项无效，请重新加载项目目录选项。") from error
    if (not isinstance(selected, dict) or not isinstance(selected.get("project"), str)
            or (selected.get("project_revision") is not None
                and (type(selected["project_revision"]) is not int or selected["project_revision"] < 1))
            or not {"project", "project_revision"} <= selected.keys()):
        raise CardActionError("工作区所属项目或版本无效，请重新加载项目目录选项。")
    try:
        WorkspacePolicy.from_dict({key: value for key, value in selected.items()
                                   if key not in {"project", "project_revision"}})
    except WorkspacePolicyError as error:
        raise CardActionError(str(error)) from error
    return selected


def decode_workspace_form(
    fields: Mapping[str, Any], *, prefix: str, project: str, revision: int | None,
) -> dict[str, str]:
    if not fields:
        return {"mode": "project"}
    selected = read_workspace_reference(fields.get(prefix + "_workspace"))
    if selected["project"] != project or selected["project_revision"] != revision:
        raise CardActionError("工作区选项不属于当前 Project 或版本已变化，请重新加载项目目录选项。")
    policy = {key: value for key, value in selected.items() if key not in {"project", "project_revision"}}
    if policy["mode"] == "new":
        ref = fields.get(prefix + "_worktree_ref", "HEAD")
        if not isinstance(ref, str) or len(ref) > 256 or any(ord(character) < 32 for character in ref):
            raise CardActionError("新建工作区起点无效，请重新选择。")
        if ref.strip() and ref.strip() != "HEAD":
            policy["ref"] = ref.strip()
    return policy


def workspace_summary(policy: Mapping[str, Any] | None, *, new_label: str = "每次新建 Worktree") -> str:
    if policy is None:
        return "沿用目标 Agent 会话的实际工作目录"
    if policy["mode"] == "existing":
        return "指定 Worktree · " + str(policy["worktree_id"])
    if policy["mode"] == "new":
        return new_label + "（Detached）· 起点 " + str(policy.get("ref") or "创建时的 Project HEAD")
    return "Project 项目目录"


def workspace_form_elements(
    project: str | None, revision: int | None, policy: Mapping[str, Any], *,
    prefix: str, load_button: dict[str, Any], options: Mapping[str, Any] | None,
    error: str | None = None, draft_fields: Mapping[str, str] | None = None,
    new_label: str = "每个新会话新建 Worktree", footer: str,
) -> list[dict[str, Any]]:
    elements = [
        _form_hint("使用上方项目加载工作区选项；切换项目后请重新加载。保存只记录策略，不会创建工作区。"),
        load_button,
    ]
    if error:
        elements.append(_notice(error, error=True))
    if project is None:
        return elements
    if options is not None:
        elements.append(_form_hint(
            f"已加载项目「{options['projectAlias']}」的工作区。"
            if options.get("isGit") is True else "该项目不是 Git 仓库，仅支持项目目录。"
        ))
    option_project = options["projectAlias"] if options is not None else project
    option_revision = options["projectRevision"] if options is not None else revision
    choices = [("项目目录", {"mode": "project"})]
    if options is not None and options.get("isGit") is True:
        choices.append((new_label + "（Detached）", {"mode": "new"}))
        for item in options.get("items", ()):
            if item.get("state") == "ready" and item.get("available") is True:
                choices.append((
                    f"{item['id']} · {item.get('branch') or 'detached'} · {item['cwd']}",
                    {"mode": "existing", "worktree_id": item["id"]},
                ))
    selected = workspace_reference(project, revision, policy)
    if draft_fields is not None and prefix + "_workspace" in draft_fields:
        selected = draft_fields[prefix + "_workspace"]
    rendered_choices = [{"text": _plain_text(label),
                         "value": workspace_reference(option_project, option_revision, choice)}
                        for label, choice in choices]
    if not any(choice["value"] == selected for choice in rendered_choices):
        rendered_choices.append({"text": _plain_text(workspace_summary(policy, new_label=new_label) + " · 保留原配置"),
                                 "value": selected})
        elements.append(_notice("原工作区选择暂不可用或属于其他项目，已保留；可重新加载并选择有效策略。", error=True))
    elements.extend([
        {"tag": "markdown", "content": "工作区策略"},
        {"tag": "select_static", "name": prefix + "_workspace", "required": True,
         "placeholder": _plain_text("选择工作区策略"), "options": rendered_choices, "initial_option": selected},
    ])
    if policy["mode"] == "new" or any(choice["mode"] == "new" for _, choice in choices):
        refs = [("创建时的 Project HEAD", "HEAD")]
        if options is not None:
            refs.extend((("远端 · " if branch.get("remote") else "本地 · ") + branch["name"], branch["ref"])
                        for branch in options.get("branches", ()))
        ref = (draft_fields or {}).get(prefix + "_worktree_ref", policy.get("ref") or "HEAD")
        if not any(value == ref for _, value in refs):
            refs.append((str(ref) + " · 保留原配置", ref))
        elements.extend([
            {"tag": "markdown", "content": "新建起点（仅新建策略生效）"},
            {"tag": "select_static", "name": prefix + "_worktree_ref",
             "placeholder": _plain_text("选择新建起点"),
             "options": [{"text": _plain_text(label), "value": value} for label, value in refs],
             "initial_option": ref},
            _form_hint("每次创建独立 Detached 工作区；只检出提交内容，不复制未提交改动或初始化环境。"),
        ])
    elements.append(_form_hint(footer))
    return elements
