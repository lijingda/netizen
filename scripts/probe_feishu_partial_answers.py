#!/usr/bin/env python3
"""Opt-in renderer/transport probe, not native E2E or Feishu client acceptance.

Sends at most 11 clearly labelled synthetic messages. Never click their Goal or
Files controls: no real Binding exists and the running service may be older.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from lark_channel import (
    FeishuChannel, LogLevel, OutboundCard, OutboundConfig, OutboundPost,
    RetryConfig, SendOpts,
)

from netizen_cli.cards import decode_turn_file_action, reply_card, reply_card_from_manifest
from netizen_cli.channel.reply_presenter import _ReplyCardPresenter
from netizen_cli.domain import (
    FeishuScope, ReplyCardActivityModule, ReplyCardFileItem, ReplyCardFilesModule,
    ReplyCardGoalModule, ReplyCardPartialAnswerModule, ReplyCardProjection,
    ReplyCardResultModule, ScopeKind, TurnCommentaryManifestEntry, TurnProgressManifest,
)
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.settings import Settings

_NOTICE = "【Netizen 阶段性答案验收／模拟】无真实任务或会话。请勿点击 Goal／文件控件：运行服务可能尚未升级。"
_FILES_MARKER = "PA-FILES-RETAINED"
_FILES_PARTIAL = f"【验收／模拟】Files 翻页后仍保留的阶段性答案。{_FILES_MARKER}"
_FINAL = "【Netizen 阶段性答案验收／模拟】普通与 Side 的两段富文本已分别发送；这是统一最终回复样例，无真实任务执行，也不代表客户端展示或点击回调已验收。"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-rich-text", action="store_true",
        help="send only the five rich-text samples; never resend the six cards")
    args = parser.parse_args(argv)
    if re.fullmatch(r"oc_[A-Za-z0-9_]{1,125}", args.chat_id) is None:
        parser.error("--chat-id must be a Feishu chat ID")
    return args


def _activity(label: str, *, terminal_status: str | None = None) -> ReplyCardActivityModule:
    return ReplyCardActivityModule(
        TurnProgressManifest(
            state="running", steer_count=0, plan_available=True,
            plan_generated=False, plan_may_be_stale=False,
            commentary=(TurnCommentaryManifestEntry(text=f"{_NOTICE}\n场景：{label}"),),
        ),
        terminal_status=terminal_status,
        collapsed=terminal_status is not None,
    )


def _page_value(value: object) -> dict | None:
    if isinstance(value, dict):
        if value.get("type") == "callback":
            candidate = value.get("value")
            if isinstance(candidate, dict) and candidate.get("intent") == "turn-file.page":
                return candidate
        for child in value.values():
            found = _page_value(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _page_value(child)
            if found is not None:
                return found
    return None


def _card_scenarios(scope: FeishuScope) -> list[tuple[str, list[tuple[str, OutboundCard]]]]:
    scenarios = []
    for label in ("普通会话", "持久 fork", "Side"):
        initial = ReplyCardProjection(activity=_activity(label))
        first = replace(initial, partial_answer=ReplyCardPartialAnswerModule((f"【验收／模拟：{label}】第一段稳定答案。",)))
        second = replace(first, partial_answer=ReplyCardPartialAnswerModule((*first.partial_answer.contents,
            f"【验收／模拟：{label}】第二段稳定答案；阶段性内容始终展开。")))
        terminal = replace(second, activity=_activity(label, terminal_status="completed"),
            result=ReplyCardResultModule(f"{_NOTICE}\n模拟最终回复；阶段性正文独立保留，不拼入最终正文。"))
        scenarios.append((label, [(name, reply_card(projection)) for name, projection in (
            ("initial_without_partial", initial), ("first_partial", first),
            ("second_partial", second), ("terminal_retains_partials", terminal),
        )]))
    for progress in (True, False):
        label = f"Goal／进度{'开启' if progress else '关闭'}"
        goal = ReplyCardGoalModule(binding_id="partial-probe-goal", short_id="probe", project_alias="probe",
            goal_generation="g" * 43, status="active", runtime_state="goal-running", token_budget=None, tokens_used=0,
            objective=f"{_NOTICE}\n{label}：模拟两个物理 Turn 及手动恢复。", notice=_NOTICE)
        initial = ReplyCardProjection(scope=scope, goal=goal, activity=_activity(label) if progress else None)
        first = replace(initial, partial_answer=ReplyCardPartialAnswerModule(("【验收／模拟】Goal 第一物理 Turn 的稳定答案。",)))
        rollover = replace(first, partial_answer=ReplyCardPartialAnswerModule((*first.partial_answer.contents,
            "【验收／模拟】Goal 第二物理 Turn 的稳定答案；上一轮内容仍保留。")))
        paused = replace(rollover, goal=replace(goal, status="paused", runtime_state="goal-paused"),
            activity=_activity(label, terminal_status="interrupted") if progress else None,
            result=ReplyCardResultModule(f"{_NOTICE}\n模拟暂停；阶段性内容仍保留。"))
        resumed = replace(initial, goal=replace(goal, notice=f"{_NOTICE}\n模拟手动恢复：新执行段，阶段性模块已清空。"))
        resumed_answer = replace(resumed, partial_answer=ReplyCardPartialAnswerModule((
            "【验收／模拟】手动恢复后的新执行段答案；旧执行段两片已清空。",)))
        scenarios.append((label, [(name, reply_card(projection)) for name, projection in (
            ("initial_without_partial", initial), ("first_turn_partial", first),
            ("automatic_rollover_retains_partials", rollover), ("pause_retains_partials", paused),
            ("manual_resume_resets_partials", resumed), ("resumed_run_new_partial", resumed_answer),
        )]))
    files = ReplyCardProjection(scope=scope,
        partial_answer=ReplyCardPartialAnswerModule((_FILES_PARTIAL,)),
        result=ReplyCardResultModule(f"{_NOTICE}\n本卡含 9 个模拟已删除项，没有真实文件；脚本只执行本地解码及整卡重绘。"),
        files=ReplyCardFilesModule(binding_id="partial-probe-files", turn_id="partial-probe-turn",
            items=tuple(ReplyCardFileItem(path=f"/tmp/netizen-partial-probe/not-created-{i}.txt",
                label=f"模拟已删除项-{i}.txt", size=None, media_kind=None, deleted=True,
                additions=0, deletions=1) for i in range(9)), additions=0, deletions=9))
    initial = reply_card(files)
    value = _page_value(initial.card)
    if value is None:
        raise ValueError("synthetic Files card has no page callback")
    intent = decode_turn_file_action(app_id=scope.app_id, message_id="om_partial_probe_synthetic",
        callback_chat_id=scope.chat_id, sender_id="probe", tag="button", value=value,
        form_value={"turn_file_page": "1"})
    if value.get("v") != 5 or intent.page != 1 or intent.reply is None or intent.reply.partial_answer != files.partial_answer:
        raise ValueError("v5 manifest lost partial answers")
    page = reply_card_from_manifest(scope=scope, binding_id=intent.binding_id, turn_id=intent.turn_id,
        manifest=intent.files, reply=intent.reply, page=intent.page,
        additions=intent.additions, deletions=intent.deletions)
    scenarios.append(("Files v5", [("page_zero", initial), ("decoded_page_one", page)]))
    return scenarios


async def _text_samples(send_post) -> int:
    # No native runtime or notification consumer: finish uses only supplied
    # exact completed items and the real production rich-text delivery method.
    presenter = _ReplyCardPresenter(None, None, operation_timeout_seconds=30)
    try:
        for side in (False, True):
            label = "Side" if side else "普通会话"
            thread_id, turn_id = f"probe-thread-{side}", f"probe-turn-{side}"
            first = PartialAnswer(thread_id, turn_id, "first", f"{_NOTICE}\n{label}／关闭进度卡：第一段稳定答案。")
            second = PartialAnswer(thread_id, turn_id, "second", f"{_NOTICE}\n{label}／关闭进度卡：第二段稳定答案。")
            confirmed = await presenter.finish_partial_answers(owner_id=f"probe-owner-{side}", thread_id=thread_id,
                turn_id=turn_id, partial_answers=(first, first, second), side=side, progress_enabled=False,
                reply=lambda post: send_post(label, post))
            if not confirmed:
                raise RuntimeError("synthetic rich-text sample delivery was not confirmed")
        return 4
    finally:
        await presenter.close()


def _unsupported_card_placeholder(content: object) -> bool:
    """Recognize only the observed Card 2.0 readback placeholder, not missing text."""
    if not isinstance(content, dict) or set(content) != {"title", "elements"} or not isinstance(content["title"], str):
        return False
    rows = content["elements"]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list) or len(rows[0]) != 3:
        return False
    image, notice, trailing = rows[0]
    return (
        isinstance(image, dict) and set(image) == {"tag", "image_key"}
        and image["tag"] == "img" and isinstance(image["image_key"], str) and bool(image["image_key"])
        and notice == {"tag": "text", "text": "请升级至最新版本客户端，以查看内容"}
        and trailing == {"tag": "text", "text": ""}
    )


async def _refetch(channel, chat_id: str, message_id: str, *, expected_partial: str | None = None) -> bool | None:
    async with asyncio.timeout(15):
        response = await channel.fetch_message(message_id)
    items = response.get("data", {}).get("items") if isinstance(response, dict) else None
    if not (isinstance(response, dict) and response.get("code") == 0 and isinstance(items, list)
            and len(items) == 1 and isinstance(items[0], dict)
            and items[0].get("message_id") == message_id and items[0].get("chat_id") == chat_id):
        raise RuntimeError("exact destination refetch not confirmed")
    if expected_partial is not None:
        body = items[0].get("body")
        content = body.get("content") if isinstance(body, dict) else None
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                pass
        if content is None or content == "" or content == {} or _unsupported_card_placeholder(content):
            return None  # No readable card body; identity alone is verified.
        if expected_partial not in json.dumps(content, ensure_ascii=False):
            raise RuntimeError("refetched Files card did not retain its partial answer")
        return True
    return None


async def _probe(args: argparse.Namespace) -> dict[str, object]:
    only_rich_text = getattr(args, "only_rich_text", False)
    result = {"dry_run": args.dry_run, "kind": "synthetic_renderer_transport",
        "only_rich_text": only_rich_text, "planned_messages": 5 if only_rich_text else 11,
        "client_visual": False, "client_callback": False, "native_e2e": False,
        "send_attempts": 0, "update_attempts": 0, "operations": []}
    if args.dry_run:
        scenarios = [] if only_rich_text else _card_scenarios(FeishuScope("cli_probe_synthetic", args.chat_id, ScopeKind.GROUP))
        result["cards"] = [{"scenario": name, "states": [{"step": step, "card": card.card} for step, card in states]}
            for name, states in scenarios]
        posts = []
        async def collect(label, post):
            posts.append({"scenario": label, "markdown": post.markdown, "mentions": post.mentions})
            return SimpleNamespace(success=True, raw={"code": 0})
        await _text_samples(collect)
        result["posts"] = [*posts, {"scenario": "unified_final", "markdown": _FINAL, "mentions": []}]
        result["payload_validation"] = True
        return result
    channel = None
    # Suppress SDK/Presenter diagnostic payloads; the JSON below contains only
    # exact message IDs and static phase errors, never raw responses or secrets.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    phase = "configuration"
    try:
        settings = Settings.from_file(args.config)
        channel = FeishuChannel(app_id=settings.app_id, app_secret=settings.app_secret, log_level=LogLevel.CRITICAL,
            outbound=OutboundConfig(retry=RetryConfig(max_attempts=1)))
        scenarios = [] if only_rich_text else _card_scenarios(FeishuScope(settings.app_id, args.chat_id, ScopeKind.GROUP))
        async def send(label, content):
            nonlocal phase
            phase = f"{label}:send"
            result["send_attempts"] += 1
            async with asyncio.timeout(15):
                sent = await channel.send(args.chat_id, content, SendOpts(receive_id_type="chat_id", reply_target_gone="fail"))
            message_id = getattr(sent, "message_id", None)
            if getattr(sent, "success", None) is not True or not isinstance(message_id, str) or not message_id.startswith("om_"):
                raise RuntimeError("send not confirmed")
            operation = {"scenario": label, "action": "send", "message_id": message_id,
                "api_success": True, "exact_message_refetch": False}
            result["operations"].append(operation)
            phase = f"{label}:refetch"
            await _refetch(channel, args.chat_id, message_id)
            operation["exact_message_refetch"] = True
            return sent
        async with asyncio.timeout(180):
            for label, states in scenarios:
                sent = await send(label, states[0][1])
                for step, card in states[1:]:
                    phase = f"{label}:{step}"
                    result["update_attempts"] += 1
                    async with asyncio.timeout(15):
                        updated = await channel.update_card(sent.message_id, card.card)
                    if getattr(updated, "success", None) is not True:
                        raise RuntimeError("update not confirmed")
                    operation = {"scenario": label, "action": "update", "step": step,
                        "message_id": sent.message_id, "api_success": True, "exact_message_refetch": False}
                    result["operations"].append(operation)
                    partial_preserved = await _refetch(channel, args.chat_id, sent.message_id,
                        expected_partial=_FILES_MARKER if label == "Files v5" else None)
                    operation["exact_message_refetch"] = True
                    if label == "Files v5":
                        operation["refetched_partial_preserved"] = partial_preserved
                        operation["content_evidence"] = "unique_marker" if partial_preserved else "identity_only_body_unavailable"
            await _text_samples(send)
            await send("unified_final", OutboundPost(markdown=_FINAL))
        result.update(success=True, local_v5_callback_roundtrip=not only_rich_text, exact_item_dedup=True)
    except Exception:
        result.update(success=False, error=f"{phase} was not confirmed; no automatic retry was attempted")
    finally:
        try:
            if channel is not None:
                async with asyncio.timeout(10):
                    await asyncio.to_thread(channel.stop)
        except Exception:
            result["success"] = False
            result.setdefault("error", "probe shutdown was not confirmed")
        finally:
            logging.disable(previous_logging)
    result["sent_messages"] = sum(op["action"] == "send" for op in result["operations"])
    result["updated_messages"] = sum(op["action"] == "update" for op in result["operations"])
    return result


def main() -> int:
    args = _parse_args()
    try:
        result = asyncio.run(_probe(args))
    except Exception:
        result = {"success": False, "error": "probe setup or shutdown failed", "client_visual": False,
            "client_callback": False, "native_e2e": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("payload_validation") or result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
