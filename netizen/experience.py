"""Map Feishu text into model-visible prompts or client-only controls."""

from __future__ import annotations

import shlex
from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum

from .domain import (
    ChannelInteraction,
    ControlIntent,
    ControlName,
    FeishuScope,
    NativeCapability,
    PromptInput,
)
from .skill_references import InvalidSkillReference, parse_skill_references


class InvalidInteraction(ValueError):
    pass


class CommandOwner(str, Enum):
    CHANNEL = "channel"
    NATIVE_THREAD = "native-thread"
    HYBRID = "hybrid"
    HOST = "host"


class CommandGroup(str, Enum):
    START = "开始与设置"
    TASK = "任务操作"
    SESSION = "会话管理"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    intent: ControlName | None
    owner: CommandOwner
    usage: str
    summary: str
    aliases: tuple[str, ...] = ()
    requires: NativeCapability | None = None
    unavailable_reason: str | None = None
    group: CommandGroup = CommandGroup.TASK


COMMAND_SPECS = (
    CommandSpec(
        "new",
        ControlName.NEW,
        CommandOwner.HYBRID,
        "/new",
        "选择项目并创建会话；创建后发送任务即可开始",
        group=CommandGroup.START,
    ),
    CommandSpec(
        "side",
        ControlName.SIDE,
        CommandOwner.HYBRID,
        "/side [首轮问题]",
        "从当前会话另开临时 Side 话题；话题内用 /side close 结束",
        requires=NativeCapability.SIDE,
        unavailable_reason=(
            "当前 SDK/App Server 的 Side Thread 兼容契约未通过"
        ),
    ),
    CommandSpec(
        "config",
        ControlName.CONFIG,
        CommandOwner.NATIVE_THREAD,
        "/config",
        "调整当前会话的模型、思考强度和速度，后续新任务生效",
        group=CommandGroup.START,
    ),
    CommandSpec(
        "compact",
        ControlName.COMPACT,
        CommandOwner.NATIVE_THREAD,
        "/compact",
        "压缩当前会话的上下文",
    ),
    CommandSpec(
        "settings",
        ControlName.SETTINGS,
        CommandOwner.CHANNEL,
        "/settings",
        "添加、启用和管理项目（任务使用的工作目录）",
        group=CommandGroup.START,
    ),
    CommandSpec(
        "cron",
        ControlName.CRON,
        CommandOwner.CHANNEL,
        "/cron",
        "管理定时任务：新建、修改、启停、删除和最近执行",
    ),
    CommandSpec(
        "sessions",
        ControlName.SESSIONS,
        CommandOwner.HYBRID,
        "/sessions [archived]",
        "列出当前聊天或话题的普通会话或已归档会话",
        aliases=("threads",),
        group=CommandGroup.SESSION,
    ),
    CommandSpec(
        "resume",
        ControlName.RESUME,
        CommandOwner.HYBRID,
        "/resume <会话短 ID>",
        "切换到已有会话；短 ID 可在 /sessions 查看",
        group=CommandGroup.SESSION,
    ),
    CommandSpec(
        "rename",
        ControlName.RENAME,
        CommandOwner.NATIVE_THREAD,
        "/rename [名称]",
        "重命名当前会话",
        group=CommandGroup.SESSION,
    ),
    CommandSpec(
        "archive",
        ControlName.ARCHIVE,
        CommandOwner.HYBRID,
        "/archive",
        "归档当前会话；归档后可以恢复",
        group=CommandGroup.SESSION,
    ),
    CommandSpec(
        "delete",
        ControlName.DELETE,
        CommandOwner.HYBRID,
        "/delete",
        "永久删除当前会话及其原生历史",
        group=CommandGroup.SESSION,
    ),
    CommandSpec(
        "unarchive",
        ControlName.UNARCHIVE,
        CommandOwner.HYBRID,
        "/unarchive <会话短 ID>",
        "恢复已归档会话并切换到它",
        group=CommandGroup.SESSION,
    ),
    CommandSpec(
        "status",
        ControlName.STATUS,
        CommandOwner.HYBRID,
        "/status",
        "查看当前项目、Git 分支、会话、任务状态和上下文用量",
    ),
    CommandSpec(
        "release",
        ControlName.RELEASE,
        CommandOwner.NATIVE_THREAD,
        "/release",
        "释放当前空闲会话的连接；会话和历史保留，之后仍可继续",
        requires=NativeCapability.RELEASE,
        unavailable_reason="当前 SDK/App Server 的 Thread 订阅释放契约未通过",
    ),
    CommandSpec(
        "stop",
        ControlName.STOP,
        CommandOwner.HYBRID,
        "/stop",
        "中断当前任务，并请求清理已登记的后台终端；不保证前台工具进程退出",
    ),
    CommandSpec(
        "help",
        ControlName.HELP,
        CommandOwner.CHANNEL,
        "/help",
        "显示本帮助",
        group=CommandGroup.START,
    ),
    CommandSpec(
        "goal",
        ControlName.GOAL,
        CommandOwner.NATIVE_THREAD,
        "/goal [objective|pause|resume|clear]",
        "查看、启动、暂停、恢复或清除持续执行的目标",
        requires=NativeCapability.GOAL,
        unavailable_reason=(
            "当前 SDK/App Server 的 Goal 兼容契约未通过"
        ),
    ),
    CommandSpec(
        "plan",
        None,
        CommandOwner.NATIVE_THREAD,
        "/plan [prompt]",
        "切换原生 Codex Plan 模式",
        unavailable_reason=(
            "当前锁定的 openai-codex 高层 SDK 缺少 collaboration mode / plan 控制"
        ),
    ),
    CommandSpec(
        "apps",
        None,
        CommandOwner.NATIVE_THREAD,
        "/apps",
        "发现并选择原生 Codex App",
        unavailable_reason=(
            "当前锁定的 openai-codex 高层 SDK 缺少 Apps discovery 的公开能力"
        ),
    ),
    CommandSpec(
        "copy",
        None,
        CommandOwner.HOST,
        "/copy",
        "复制宿主界面的最新输出",
        unavailable_reason="这是 Codex CLI/App 宿主界面命令，飞书中不适用",
    ),
    CommandSpec(
        "vim",
        None,
        CommandOwner.HOST,
        "/vim",
        "切换 CLI 输入模式",
        unavailable_reason="这是 Codex CLI 宿主界面命令，飞书中不适用",
    ),
    CommandSpec(
        "theme",
        None,
        CommandOwner.HOST,
        "/theme",
        "设置宿主界面主题",
        unavailable_reason="这是 Codex CLI/App 宿主界面命令，飞书中不适用",
    ),
    CommandSpec(
        "exit",
        None,
        CommandOwner.HOST,
        "/exit",
        "退出宿主应用",
        aliases=("quit",),
        unavailable_reason="这是 Codex CLI/App 宿主生命周期命令，飞书中不适用",
    ),
)


_COMMANDS: dict[str, CommandSpec] = {
    token: spec
    for spec in COMMAND_SPECS
    for token in (spec.name, *spec.aliases)
}

_CONFIG_ALIASES = frozenset({"model", "effort", "fast"})


def _command_error(
    reason: str,
    *,
    spec: CommandSpec | None = None,
) -> InvalidInteraction:
    parts = [reason]
    if spec is not None:
        parts.append(f"用法：{spec.usage}。")
    parts.append("发送 /help 查看快速开始和可用命令。")
    return InvalidInteraction(" ".join(parts))


def parse_message(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    text: str,
    available_capabilities: Collection[NativeCapability] = (),
) -> ChannelInteraction:
    capabilities = frozenset(available_capabilities)
    body = text.strip()
    if not body:
        raise _command_error("消息内容为空。")
    if body.startswith("//"):
        return PromptInput(scope, message_id, sender_id, body[1:])
    if not body.startswith("/"):
        try:
            skill_names = parse_skill_references(body)
        except InvalidSkillReference as error:
            raise InvalidInteraction(str(error)) from error
        if skill_names and NativeCapability.SKILLS not in capabilities:
            raise InvalidInteraction(
                "当前原生 Skills discovery 不可用，$skill 引用未执行。"
            )
        return PromptInput(scope, message_id, sender_id, body, skill_names)
    if body == "/":
        return ControlIntent(scope, message_id, sender_id, ControlName.MENU)

    raw_parts = body[1:].lstrip().split(maxsplit=1)
    raw_spec = _COMMANDS.get(raw_parts[0].lower()) if raw_parts else None
    if (
        raw_spec is not None
        and raw_spec.intent is ControlName.NEW
        and len(raw_parts) == 2
    ):
        # `/new` is deliberately card-only. Reject the raw tail before shlex
        # so quoted and even unterminated-quote variants all receive the same
        # migration result and can never reach a mutating ControlIntent.
        raise InvalidInteraction(
            "快捷创建已下线，请发送 /new 并在卡片中选择。"
        )
    if raw_spec is not None and raw_spec.intent in {
        ControlName.GOAL,
        ControlName.SIDE,
    }:
        # Goal objectives and Side first prompts are free-form text, so quotes
        # in the tail are data;
        # only the command head participates in command parsing.
        tokens = [raw_parts[0]]
    else:
        try:
            tokens = shlex.split(body[1:])
        except ValueError as error:
            raise _command_error(
                "命令格式错误：请补全成对引号，并检查末尾的反斜杠。",
                spec=raw_spec,
            ) from error
    if not tokens:
        return ControlIntent(scope, message_id, sender_id, ControlName.MENU)
    spec = _COMMANDS.get(tokens[0].lower())
    if spec is None:
        unavailable = tokens[0].lower()
        if unavailable in {"project", "projects"}:
            raise _command_error(
                "项目通过 /settings 添加或启用；准备好项目后，发送 /new 创建会话。"
                "本条消息未执行。"
            )
        if unavailable in _CONFIG_ALIASES:
            raise _command_error(
                "Model / Effort / Speed 不提供独立命令，请统一使用 /config。"
            )
        raise _command_error(f"未知命令：/{tokens[0]}，本条消息未执行。")
    if spec.intent is None:
        assert spec.unavailable_reason is not None
        raise InvalidInteraction(
            f"/{spec.name} 尚未开放：{spec.unavailable_reason}，本条消息未执行。"
        )
    if spec.requires is not None and spec.requires not in capabilities:
        assert spec.unavailable_reason is not None
        raise InvalidInteraction(
            f"/{spec.name} 尚未开放：{spec.unavailable_reason}，本条消息未执行。"
        )
    name = spec.intent
    assert name is not None
    arguments = tuple(tokens[1:])
    if name in {ControlName.GOAL, ControlName.SIDE}:
        # Goal objective and Side first prompt are free-form user text, not a
        # shell argv. Preserve the
        # tail (including internal whitespace and quoting) instead of rebuilding
        # it from shlex tokens. Control words remain ordinary one-word tails.
        arguments = (raw_parts[1],) if len(raw_parts) == 2 else ()
    elif name is ControlName.RENAME and len(tokens) > 1:
        arguments = (" ".join(tokens[1:]),)
    try:
        _validate_arguments(name, arguments)
    except InvalidInteraction as error:
        if name is ControlName.NEW:
            raise
        raise _command_error(str(error), spec=spec) from error
    return ControlIntent(scope, message_id, sender_id, name, arguments)


def _validate_arguments(name: ControlName, arguments: tuple[str, ...]) -> None:
    expected = {
        ControlName.MENU: 0,
        ControlName.NEW: 0,
        ControlName.SIDE: None,
        ControlName.CONFIG: 0,
        ControlName.COMPACT: 0,
        ControlName.SETTINGS: 0,
        ControlName.CRON: 0,
        ControlName.SESSIONS: None,
        ControlName.RESUME: 1,
        ControlName.RENAME: None,
        ControlName.ARCHIVE: 0,
        ControlName.DELETE: 0,
        ControlName.UNARCHIVE: 1,
        ControlName.STOP: 0,
        ControlName.RELEASE: 0,
        ControlName.STATUS: 0,
        ControlName.GOAL: None,
        ControlName.HELP: 0,
    }[name]
    if name is ControlName.SIDE and len(arguments) in {0, 1}:
        if arguments:
            value = arguments[0].strip()
            if not value:
                raise InvalidInteraction("Side 首轮问题不能为空。")
            if len(value) > 4_000:
                raise InvalidInteraction("Side 首轮问题不能超过 4000 个字符。")
        return
    if name is ControlName.SESSIONS and (
        not arguments
        or (
            len(arguments) == 1
            and arguments[0].lower() == "archived"
        )
    ):
        return
    if name is ControlName.RENAME and len(arguments) in {0, 1}:
        if arguments:
            value = arguments[0].strip()
            if not value:
                raise InvalidInteraction("会话名称不能为空。")
            if len(value) > 120:
                raise InvalidInteraction("会话名称不能超过 120 个字符。")
        return
    if name is ControlName.GOAL and len(arguments) in {0, 1}:
        if arguments:
            value = arguments[0].strip()
            if not value:
                raise InvalidInteraction("目标内容不能为空。")
            if len(value) > 4_000:
                raise InvalidInteraction("Goal objective 不能超过 4000 个字符。")
            try:
                skill_names = parse_skill_references(value)
            except InvalidSkillReference as error:
                raise InvalidInteraction(str(error)) from error
            if skill_names:
                raise InvalidInteraction(
                    "当前尚未验证 Goal objective 中的 $skill 语义；"
                    "请先用普通消息调用 Skill。"
                )
        return
    if expected is not None and len(arguments) == expected:
        return
    if name is ControlName.NEW:
        raise InvalidInteraction(
            "快捷创建已下线，请发送 /new 并在卡片中选择。"
        )
    if expected == 0:
        raise InvalidInteraction(f"/{name.value} 不接受参数。")
    raise InvalidInteraction("命令参数不正确。")


def command_help(
    available_capabilities: Collection[NativeCapability] = (),
) -> str:
    capabilities = frozenset(available_capabilities)
    available = tuple(
        spec
        for spec in COMMAND_SPECS
        if spec.intent is not None
        and (spec.requires is None or spec.requires in capabilities)
    )
    lines = [
        "快速开始：",
        "1. 还没有项目：发送 /settings 添加或启用项目（任务使用的工作目录）。",
        "2. 发送 /new，在卡片中选择项目并创建会话。",
        "3. 创建成功后，直接发送任务，例如：介绍一下这个项目。",
        "已有会话：发送 /sessions 查看并切换，继续之前的工作。",
    ]
    for group in CommandGroup:
        entries = tuple(spec for spec in available if spec.group is group)
        if entries:
            lines.extend(("", f"{group.value}："))
            lines.extend(f"{spec.usage}：{spec.summary}" for spec in entries)
    lines.extend(
        (
            "",
            "群主线和群话题中的每条消息都需要 @机器人；单聊及单聊话题无需 @。",
            "用 // 开头可把首个 / 作为普通消息发送。",
        )
    )
    return "\n".join(lines)


def side_command_help(*, requires_mention: bool) -> str:
    lines = [
        "当前是多轮 Side 话题。可用操作：",
        "直接发送消息：开始新任务；任务执行中发送的消息会补充到当前任务。",
        "/status：查看 Side 状态",
        "/stop：只中断当前 Side 任务，Side 仍可继续",
        "/side close：结束当前 Side 话题，结束后不能继续",
        "/help 或 /：显示本帮助",
        "用 // 开头可把首个 / 作为普通消息发送。",
    ]
    if requires_mention:
        lines.append("本群 Side 话题中的每条消息都需要 @机器人。")
    else:
        lines.append("本单聊 Side 话题无需 @机器人。")
    return "\n".join(lines)
