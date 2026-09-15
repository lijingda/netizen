#!/usr/bin/env python3
"""Preview or send one card and a separate completion mention in its topic.

An accepted API update does not verify a notification in the Feishu client.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path

from lark_channel import (
    FeishuChannel, Identity, LogLevel, OutboundCard, OutboundConfig,
    OutboundText, RetryConfig, SendOpts,
)

from netizen.cards import reply_card
from netizen.channel.topics import validate_topic_message
from netizen.completion_mention import valid_completion_mention_user_id
from netizen.domain import (
    ReplyCardActivityModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    TurnCommentaryManifestEntry,
    TurnProgressManifest,
)
from netizen.settings import Settings


def _validate_args(args: argparse.Namespace) -> None:
    if re.fullmatch(r"oc_[A-Za-z0-9_]{1,125}", args.chat_id) is None:
        raise ValueError("--chat-id must be a Feishu chat ID")
    if valid_completion_mention_user_id(args.user_id) is None:
        raise ValueError("--user-id must be one Feishu user open_id")
    if (
        args.reply_to_message_id is not None
        and re.fullmatch(r"om_[A-Za-z0-9_]{1,125}", args.reply_to_message_id) is None
    ):
        raise ValueError("--reply-to-message-id must be a Feishu message ID")
    if args.reply_in_thread and args.reply_to_message_id is None:
        raise ValueError("--reply-in-thread requires --reply-to-message-id")
    if not 0 <= args.delay_seconds <= 60:
        raise ValueError("--delay-seconds must be between 0 and 60")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--reply-to-message-id")
    parser.add_argument("--reply-in-thread", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=15)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        _validate_args(args)
    except ValueError as error:
        parser.error(str(error))
    return args


def _cards(user_id: str) -> tuple[OutboundCard, OutboundCard, OutboundCard]:
    if valid_completion_mention_user_id(user_id) is None:
        raise ValueError("completion mention requires one Feishu user open_id")
    progress = TurnProgressManifest(
        state="running", steer_count=0, plan_available=False,
        plan_generated=False, plan_may_be_stale=False, steps=(),
        commentary=(TurnCommentaryManifestEntry(
            text="通知验收：请切换至其他聊天，等待本卡片完成更新。",
        ),),
    )
    initial = ReplyCardProjection(activity=ReplyCardActivityModule(progress))
    completed = replace(
        initial,
        activity=ReplyCardActivityModule(
            progress, terminal_status="completed", collapsed=True,
        ),
        result=ReplyCardResultModule(
            "通知验收：本卡片已完成更新，结束提醒会单独回复到本卡片的话题。",
        ),
    )
    redraw = replace(
        completed,
        result=replace(completed.result, completion_mention_user_id=None),
    )
    return reply_card(initial), reply_card(completed), reply_card(redraw)


def _mention(args: argparse.Namespace, message_id: str) -> tuple[OutboundText, SendOpts]:
    identity = repr((args.chat_id, message_id, args.user_id)).encode("utf-8")
    return (
        OutboundText(
            text="通知验收：本轮已结束，结果见卡片。请检查是否收到新提及通知。",
            mentions=[Identity(open_id=args.user_id)],
        ),
        SendOpts(
            receive_id_type="chat_id", reply_to=message_id, reply_in_thread=True,
            reply_target_gone="fail",
            uuid="completion-probe-" + hashlib.sha256(identity).hexdigest()[:32],
        ),
    )


async def _probe(args: argparse.Namespace) -> dict[str, object]:
    _validate_args(args)
    initial, completed, redraw = _cards(args.user_id)
    opts = SendOpts(
        receive_id_type="chat_id",
        reply_to=args.reply_to_message_id,
        reply_in_thread=True if args.reply_in_thread else None,
        reply_target_gone="fail",
    )
    if args.dry_run:
        mention, mention_opts = _mention(args, "<sent.message_id>")
        return {
            "dry_run": True,
            "send": {"chat_id": args.chat_id, "opts": asdict(opts), "card": initial.card},
            "completion_update": {"message_id": "<sent.message_id>", "card": completed.card},
            "completion_mention": {
                "chat_id": args.chat_id, "opts": asdict(mention_opts),
                "text": mention.text, "mentions": [asdict(user) for user in mention.mentions],
            },
            "redraw_preview": {"message_id": "<sent.message_id>", "card": redraw.card},
            "client_notification_verified": False,
        }

    settings = Settings.from_file(args.config)
    channel = FeishuChannel(
        app_id=settings.app_id, app_secret=settings.app_secret,
        log_level=LogLevel.WARNING,
        outbound=OutboundConfig(retry=RetryConfig(max_attempts=1)),
    )
    result: dict[str, object] = {
        "message_id": None, "send_success": False,
        "completion_update_success": False, "completion_mention_success": False,
        "completion_mention_message_id": None, "client_notification_verified": False,
    }
    phase = "reply target validation"
    try:
        target_thread_id = None
        target_root_id = None
        if args.reply_to_message_id is not None:
            fetched = await channel.fetch_message(args.reply_to_message_id)
            data = fetched.get("data") if isinstance(fetched, dict) else None
            items = data.get("items") if isinstance(data, dict) else None
            if (
                not isinstance(fetched, dict) or fetched.get("code") != 0
                or not isinstance(items, list) or len(items) != 1
                or not isinstance(items[0], dict)
                or items[0].get("message_id") != args.reply_to_message_id
                or items[0].get("chat_id") != args.chat_id
            ):
                result["error"] = "reply target does not confirm the exact requested chat/message"
                return result
            if items[0].get("thread_id") and not args.reply_in_thread:
                result["error"] = "reply target is in a topic; --reply-in-thread is required"
                return result
            target_thread_id = items[0].get("thread_id") or None
            target_root_id = items[0].get("root_id") or args.reply_to_message_id
        phase = "initial card send"
        sent = await channel.send(args.chat_id, initial, opts)
        card_message = validate_topic_message(sent, args.chat_id)
        message_id = card_message.message_id
        if (
            re.fullmatch(r"om_[A-Za-z0-9_]{1,125}", message_id) is None
            or card_message.parent_id != args.reply_to_message_id
            or (not args.reply_in_thread and card_message.thread_id is not None)
            or (args.reply_in_thread and (
                not card_message.thread_id or card_message.root_id != target_root_id
                or (target_thread_id is not None and card_message.thread_id != target_thread_id)
            ))
        ):
            result["error"] = "initial card send did not confirm one exact message"
            return result
        result.update(message_id=message_id, send_success=True)
        print(json.dumps({
            "phase": "initial_card_sent", "message_id": message_id,
            "delay_seconds": args.delay_seconds,
        }), file=sys.stderr, flush=True)
        await asyncio.sleep(args.delay_seconds)
        phase = "completion card update"
        updated = await channel.update_card(message_id, completed.card)
        if getattr(updated, "success", None) is not True:
            result["error"] = "completion card update was not confirmed"
            return result
        result["completion_update_success"] = True
        phase = "completion mention send"
        mention, mention_opts = _mention(args, message_id)
        mentioned = await channel.send(args.chat_id, mention, mention_opts)
        notification = validate_topic_message(mentioned, args.chat_id)
        if (
            notification.message_id == message_id or not notification.thread_id
            or not notification.root_id or not notification.parent_id
            or (card_message.thread_id is None and notification.parent_id != message_id)
            or (card_message.thread_id is not None and notification.thread_id != card_message.thread_id)
        ):
            result["error"] = "completion mention did not confirm the exact card topic"
            return result
        result.update(
            completion_mention_success=True,
            completion_mention_message_id=notification.message_id,
        )
        return result
    except Exception:
        # Do not print raw SDK errors, response bodies, or credentials.
        result["error"] = f"{phase} failed; inspect local configuration and target permissions"
        return result
    finally:
        await asyncio.to_thread(channel.stop)


def main() -> int:
    args = _parse_args()
    try:
        result = asyncio.run(_probe(args))
    except Exception:
        result = {
            "error": "probe failed; check local configuration and connectivity",
            "client_notification_verified": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("dry_run") or result.get("completion_mention_success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
