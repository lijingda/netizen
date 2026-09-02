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


COMMAND_SPECS = (
    CommandSpec(
        "new",
        ControlName.NEW,
        CommandOwner.HYBRID,
        "/new",
        "用卡片新建 lazy 会话并选择 Project 与后续 Turn 配置",
    ),
    CommandSpec(
        "side",
        ControlName.SIDE,
        CommandOwner.HYBRID,
        "/side [首轮问题]",
        "从当前原生 Thread 新建多轮临时 Side 话题；Side 内用 /side close 结束",
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
        "配置当前会话后续新 Turn 的 Model / Effort / Speed",
    ),
    CommandSpec(
        "compact",
        None,
        CommandOwner.NATIVE_THREAD,
        "/compact",
        "压缩当前原生 Codex 会话上下文",
        unavailable_reason=(
            "固定 openai-codex 0.147.0 的压缩后同连接继续 Turn 兼容验证未通过"
        ),
    ),
    CommandSpec(
        "settings",
        ControlName.SETTINGS,
        CommandOwner.CHANNEL,
        "/settings",
        "打开 Netizen 实例级设置卡片",
    ),
    CommandSpec(
        "sessions",
        ControlName.SESSIONS,
        CommandOwner.HYBRID,
        "/sessions [archived]",
        "列出当前聊天或话题的普通会话或已归档会话",
        aliases=("threads",),
    ),
    CommandSpec(
        "resume",
        ControlName.RESUME,
        CommandOwner.HYBRID,
        "/resume <会话短 ID>",
        "切换会话",
    ),
    CommandSpec(
        "rename",
        ControlName.RENAME,
        CommandOwner.NATIVE_THREAD,
        "/rename [名称]",
        "重命名当前原生 Codex 会话",
    ),
    CommandSpec(
        "archive",
        ControlName.ARCHIVE,
        CommandOwner.HYBRID,
        "/archive",
        "归档当前会话；归档后可以恢复",
    ),
    CommandSpec(
        "delete",
        ControlName.DELETE,
        CommandOwner.HYBRID,
        "/delete",
        "永久删除当前会话及其原生历史",
    ),
    CommandSpec(
        "unarchive",
        ControlName.UNARCHIVE,
        CommandOwner.HYBRID,
        "/unarchive <会话短 ID>",
        "恢复已归档会话并切换到它",
    ),
    CommandSpec(
        "status",
        ControlName.STATUS,
        CommandOwner.HYBRID,
        "/status",
        "查看 Project、Git Branch、原生 Thread、任务状态和上下文窗口用量",
    ),
    CommandSpec(
        "release",
        ControlName.RELEASE,
        CommandOwner.NATIVE_THREAD,
        "/release",
        "取消本进程对当前空闲 Thread 的订阅；Binding 和历史保留",
        requires=NativeCapability.RELEASE,
        unavailable_reason="当前 SDK/App Server 的 Thread 订阅释放契约未通过",
    ),
    CommandSpec(
        "stop",
        ControlName.STOP,
        CommandOwner.HYBRID,
        "/stop",
        "中断当前 Turn，并请求清理已登记的后台终端；不保证前台工具进程退出",
    ),
    CommandSpec(
        "help",
        ControlName.HELP,
        CommandOwner.CHANNEL,
        "/help",
        "显示本帮助",
    ),
    CommandSpec(
        "goal",
        ControlName.GOAL,
        CommandOwner.NATIVE_THREAD,
        "/goal [objective|pause|resume|clear]",
        "查看、启动、暂停、恢复或清除原生 Codex Goal",
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
        raise InvalidInteraction("消息内容为空。")
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
            raise InvalidInteraction(f"命令格式错误：{error}") from error
    if not tokens:
        return ControlIntent(scope, message_id, sender_id, ControlName.MENU)
    spec = _COMMANDS.get(tokens[0].lower())
    if spec is None:
        unavailable = tokens[0].lower()
        if unavailable in _CONFIG_ALIASES:
            raise InvalidInteraction(
                "Model / Effort / Speed 不提供独立命令，请统一使用 /config。"
            )
        raise InvalidInteraction(f"未知命令：/{tokens[0]}。发送 /help 查看可用命令。")
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
    _validate_arguments(name, arguments)
    return ControlIntent(scope, message_id, sender_id, name, arguments)


def _validate_arguments(name: ControlName, arguments: tuple[str, ...]) -> None:
    expected = {
        ControlName.MENU: 0,
        ControlName.NEW: 0,
        ControlName.SIDE: None,
        ControlName.CONFIG: 0,
        ControlName.COMPACT: 0,
        ControlName.SETTINGS: 0,
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
                raise InvalidInteraction("用法：/side [首轮问题]")
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
                raise InvalidInteraction(
                    "用法：/goal [objective|pause|resume|clear]"
                )
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
    if name is ControlName.SIDE:
        raise InvalidInteraction("用法：/side [首轮问题]")
    if name is ControlName.RESUME:
        raise InvalidInteraction("用法：/resume <会话短 ID>")
    if name is ControlName.SESSIONS:
        raise InvalidInteraction("用法：/sessions [archived]")
    if name is ControlName.RENAME:
        raise InvalidInteraction("用法：/rename [名称]")
    if name is ControlName.UNARCHIVE:
        raise InvalidInteraction("用法：/unarchive <会话短 ID>")
    if name is ControlName.GOAL:
        raise InvalidInteraction("用法：/goal [objective|pause|resume|clear]")
    raise InvalidInteraction(f"/{name.value} 不接受参数。")


def command_help(
    available_capabilities: Collection[NativeCapability] = (),
) -> str:
    capabilities = frozenset(available_capabilities)
    lines = ["可用命令："]
    lines.extend(
        f"{spec.usage}：{spec.summary}"
        for spec in COMMAND_SPECS
        if spec.intent is not None
        and (spec.requires is None or spec.requires in capabilities)
    )
    lines.extend(
        (
            "群主线和群话题中的每条消息都需要 @机器人；单聊及单聊话题无需 @。",
            "用 // 开头可把首个 / 作为普通 prompt 发送。",
        )
    )
    return "\n".join(lines)


def side_command_help(*, requires_mention: bool) -> str:
    lines = [
        "当前是多轮 Side 话题。可用操作：",
        "直接发送消息：开始新 Turn；当前 Turn 运行时会作为 steer",
        "/status：查看 Side 状态",
        "/stop：只中断当前 Side Turn，Side 仍可继续",
        "/side close：结束 Side 并取消原生订阅",
        "/help 或 /：显示本帮助",
        "用 // 开头可把首个 / 作为普通 prompt 发送。",
    ]
    if requires_mention:
        lines.append("本群 Side 话题中的每条消息都需要 @机器人。")
    else:
        lines.append("本单聊 Side 话题无需 @机器人。")
    return "\n".join(lines)
