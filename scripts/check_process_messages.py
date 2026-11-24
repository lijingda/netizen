#!/usr/bin/env python3
"""Recheck ADR 0107 in one explicitly selected Feishu chat. Stdout is a structure-only JSON report."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Never

import lark_oapi as lark
from lark_channel import FeishuChannel, LogLevel, OutboundConfig, OutboundText, RetryConfig, SendOpts
from lark_oapi.api.im.v1 import GetMessageRequest

from netizen_cli.channel.topics import validate_topic_message
from netizen_cli.lark_app import LARK_APP_RELATIVE_PATH, load_lark_app
from netizen_cli.process_messages import (
    CONTENT_LIMIT, Created, Ok, ProcessItem, ProcessMessages, Rejected, RejectionCode,
    StepItem, TextItem, ToolItem, Unknown, finish_items, item_events,
)


class GateFailed(Exception):
    """Stop at an unconfirmed target or unexpected receipt."""


def code(result: Created | Ok | Rejected | Unknown) -> int | None:
    if isinstance(result, Rejected):
        return result.code
    return None if isinstance(result, Unknown) else 0


def placement_mismatch(report: dict[str, object], reason: str) -> Never:
    report["placement_mismatch"] = reason
    raise GateFailed


async def exact(client: lark.Client, chat: str, message_id: str, report: dict[str, object], phase: str):
    report["phase"] = phase
    request = GetMessageRequest.builder().message_id(message_id).build()
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(1)
        try:
            response = await client.im.v1.message.aget(request)
            if type(response.code) is not int:
                raise TypeError("invalid message read response code")
        except Exception as exc:
            report[f"{phase}_read_failure"] = type(exc).__name__
        else:
            if response.code == 0:
                break
            report[f"{phase}_read_failure"] = response.code
    else:
        raise GateFailed
    if response.data is None or len(response.data.items or []) != 1:
        placement_mismatch(report, "item_count")
    item, = response.data.items
    if item.message_id != message_id or item.chat_id != chat or item.deleted:
        placement_mismatch(report, "message_identity_or_deleted")
    return item


async def probe(root: Path, chat: str, report: dict[str, object]) -> None:
    creds = load_lark_app(root / LARK_APP_RELATIVE_PATH)
    client = (lark.Client.builder().app_id(creds.app_id).app_secret(creds.app_secret)
              .timeout(10).log_level(lark.LogLevel.ERROR).build())
    port = ProcessMessages(client)
    channel = FeishuChannel(app_id=creds.app_id, app_secret=creds.app_secret,
                            log_level=LogLevel.CRITICAL,
                            outbound=OutboundConfig(retry=RetryConfig(max_attempts=1)))
    cases = {
        "A": "活动条目：桌面端请记录工具、清单和进展文字以及完成对勾的显示。",
        "B": "大小预算：验证 1,800 字符的单事件与完成后拒绝更新。",
        "C": "并发样本：在本消息内发出六次重叠更新。",
    }
    reports: dict[str, dict[str, object]] = {}
    report["cases"] = reports
    labels = {case: f"[过程消息验收 {case}] {report['run_utc']}" for case in cases}
    print(*labels.values(), sep="\n", file=sys.stderr, flush=True)
    used_cots: set[str] = set()
    used_messages: set[str] = set()
    try:
        for case, instruction in cases.items():
            report["phase"] = case
            checks: dict[str, object] = {}
            reports[case] = checks
            target = await create_in_topic(channel, client, port, chat, labels[case], instruction, checks)
            if target.cot_id in used_cots or target.message_id in used_messages:
                placement_mismatch(checks, "reused_process_target")
            used_cots.add(target.cot_id)
            used_messages.add(target.message_id)
            await check_writes(port, target, case, checks)
        report["phase"] = "finished"
    finally:
        await asyncio.to_thread(channel.stop)


async def create_in_topic(
    channel: FeishuChannel, client: lark.Client, port: ProcessMessages,
    chat: str, label: str, instruction: str, report: dict[str, object],
) -> Created:
    report["phase"] = "send_root"
    seed = validate_topic_message(await channel.send(
        chat, OutboundText(text=label), SendOpts(receive_id_type="chat_id"),
    ), chat)
    report["phase"] = "send_parent"
    origin = validate_topic_message(await channel.send(
        chat, OutboundText(text=instruction),
        SendOpts(receive_id_type="chat_id", reply_to=seed.message_id,
                 reply_in_thread=True, reply_target_gone="fail"),
    ), chat)

    parent = await exact(client, chat, origin.message_id, report, "read_parent")
    anchor = await exact(client, chat, seed.message_id, report, "read_root")
    if (origin.message_id == seed.message_id or not parent.thread_id
            or parent.root_id != seed.message_id or parent.parent_id != seed.message_id
            or anchor.parent_id or (anchor.root_id and anchor.root_id != seed.message_id)
            or (anchor.thread_id and anchor.thread_id != parent.thread_id)):
        report["phase"] = "read_parent_and_root"
        placement_mismatch(report, "parent_or_root_relationship")
    report["parent_and_root_verified"] = True
    report["phase"] = "create"
    created = await port.create(chat, origin.message_id, True)
    report["create_code"] = code(created)
    if not isinstance(created, Created):
        raise GateFailed

    placed = await exact(client, chat, created.message_id, report, "read_process_placement")
    if (created.message_id in (origin.message_id, seed.message_id)
            or placed.thread_id != parent.thread_id or placed.root_id != seed.message_id
            or placed.parent_id != origin.message_id):
        placement_mismatch(report, "process_relationship")
    report["process_placement_verified"] = True
    return created


def budget_item(observed_at: int) -> ToolItem:
    item = ToolItem("budget", "边界", observed_at, observed_at)
    start, _ = item_events((item,))
    # A JSON-escaped unit separator takes six characters in the inner event.
    separators, remainder = divmod(CONTENT_LIMIT - len(start["content"]), 6)
    label = item.text + "\x1f" * separators + "x" * remainder
    return ToolItem(item.item_id, label, observed_at, observed_at)


async def check_writes(port: ProcessMessages, target: Created, case: str, report: dict[str, object]) -> None:
    started_at = int(time.time())

    async def write(items: tuple[ProcessItem, ...]) -> Ok | Rejected | Unknown:
        return await port.update(target.cot_id, target.message_id, items)

    async def require_write(phase: str, items: tuple[ProcessItem, ...]) -> None:
        report["phase"] = phase
        result = await write(items)
        report[f"{phase}_code"] = code(result)
        if not isinstance(result, Ok):
            raise GateFailed

    reason: Literal["done", "error"] = "error"
    try:
        if case == "A":
            tool = ToolItem("operation", "读取项目文件", started_at)
            step = StepItem("step", "核对结果", started_at)
            await require_write("running", (tool, step))
            observed_at = int(time.time())
            text = TextItem("progress", "已核对结果", observed_at, observed_at)
            await require_write("final_items", finish_items((tool, step, text), observed_at))
        elif case == "B":
            items = (budget_item(started_at),)
            size = max(len(e["content"]) for e in item_events(items))
            report["max_event_characters"] = size
            if size != CONTENT_LIMIT:
                raise GateFailed
            await require_write("budget", items)
        elif case == "C":
            report["phase"] = "concurrent_updates"
            # This isolated burst is the sole intentional multi-writer sample.
            sample = (TextItem("progress", "并发样本", started_at, started_at),)
            outcomes = await asyncio.gather(*(write(sample) for _ in range(6)))
            codes = [code(outcome) for outcome in outcomes]
            report["concurrent_codes"] = codes
            report["concurrent_10020_informational"] = RejectionCode.CONCURRENT in codes
            if any(value not in (0, RejectionCode.CONCURRENT) for value in codes):
                raise GateFailed
        else:
            raise ValueError("unknown probe case")
        reason = "done"
    finally:
        completed = await port.complete(target.cot_id, target.message_id, reason)
        report["complete_code"] = code(completed)
        report["complete_reason"] = reason

    report["phase"] = "complete"
    if not isinstance(completed, Ok):
        raise GateFailed
    if case == "B":
        report["phase"] = "post_complete_update"
        late = await write((TextItem("late", "完成后校验", started_at, started_at),))
        report["post_complete_code"] = code(late)
        if late != Rejected(RejectionCode.TERMINAL_OR_INVALID):
            raise GateFailed
    report["phase"] = "finished"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--chat", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"oc_[A-Za-z0-9_]{1,125}", args.chat):
        parser.error("--chat requires a Feishu chat ID")
    # Only the operator-provided chat and root are used. Strip SDK diagnostic output.
    logging.disable(logging.CRITICAL)
    report: dict[str, object] = {
        "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "credentials", "api_passed": False,
        "desktop_rendering": "separate maintainer observation",
    }
    try:
        asyncio.run(probe(args.root.expanduser().resolve(), args.chat, report))
        report["api_passed"] = True
    except Exception:
        pass  # Retain only the structural diagnostics recorded at the failed phase.
    print(json.dumps(report, indent=2))
    return 0 if report["api_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
