#!/usr/bin/env python3
"""Send and round-trip one real v5 composed Reply Card at file capacity."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from lark_channel import FeishuChannel, LogLevel, SendOpts

from netizen.cards import (
    decode_turn_file_action,
    reply_card,
    reply_card_from_manifest,
)
from netizen.domain import (
    FeishuScope,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    ScopeKind,
)
from netizen.settings import Settings
from netizen.turn_files import TurnFile


def _next_page_value(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        if value.get("type") == "callback":
            candidate = value.get("value")
            if (
                isinstance(candidate, dict)
                and candidate.get("intent") == "turn-file.page"
                and candidate.get("page") == 1
            ):
                return candidate
        for child in value.values():
            found = _next_page_value(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _next_page_value(child)
            if found is not None:
                return found
    return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--count", type=int, default=400)
    args = parser.parse_args()
    if not args.chat_id.startswith("oc_"):
        parser.error("--chat-id must be a Feishu chat ID")
    if not 9 <= args.count <= 400:
        parser.error("--count must be between 9 and 400")
    return args


async def _probe(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings.from_file(args.config)
    channel = FeishuChannel(
        app_id=settings.app_id,
        app_secret=settings.app_secret,
        log_level=LogLevel.WARNING,
    )
    scope = FeishuScope(settings.app_id, args.chat_id, ScopeKind.DIRECT)
    files = tuple(
        TurnFile(
            display_path=f"capacity/file-{index:04}.txt",
            resolved_path=Path(f"/tmp/netizen-card-capacity/file-{index:04}.txt"),
            size=index,
            media_kind="file",
            additions=12,
            deletions=3,
        )
        for index in range(args.count)
    )
    card = reply_card(
        ReplyCardProjection(
            scope=scope,
            result=ReplyCardResultModule(
                "**Netizen 部署验收卡，可忽略。**\n"
                f"正在验证 {args.count} 个文件的完整分页与重启后自包含恢复能力。"
            ),
            files=ReplyCardFilesModule(
                binding_id="capacity-probe",
                turn_id="capacity-probe",
                items=tuple(
                    ReplyCardFileItem(
                        path=str(item.resolved_path),
                        label=item.display_path,
                        size=item.size,
                        media_kind=item.media_kind,
                        additions=item.additions,
                        deletions=item.deletions,
                    )
                    for item in files
                ),
                additions=args.count * 12,
                deletions=args.count * 3,
            ),
        )
    )
    try:
        sent = await channel.send(
            args.chat_id,
            card,
            SendOpts(receive_id_type="chat_id"),
        )
        if not sent.success or not sent.message_id:
            raise RuntimeError(f"capacity card send failed: {sent.raw!r}")
        page_value = _next_page_value(card.card)
        if page_value is None:
            raise RuntimeError("capacity card is missing its next-page callback")
        page_intent = decode_turn_file_action(
            app_id=settings.app_id,
            message_id=sent.message_id,
            callback_chat_id=args.chat_id,
            sender_id="capacity-probe",
            tag="button",
            value=page_value,
        )
        if page_intent.reply is None:
            raise RuntimeError("capacity card page callback omitted its Reply manifest")
        if (
            page_intent.additions != args.count * 12
            or page_intent.deletions != args.count * 3
            or len(page_intent.files) != args.count
            or any(
                item.additions != 12 or item.deletions != 3
                for item in page_intent.files
            )
        ):
            raise RuntimeError(
                "capacity card page callback lost exact line statistics"
            )
        updated = reply_card_from_manifest(
            scope=scope,
            binding_id="capacity-probe",
            turn_id="capacity-probe",
            manifest=page_intent.files,
            reply=page_intent.reply,
            page=1,
            additions=page_intent.additions,
            deletions=page_intent.deletions,
        )
        update_result = await channel.update_card(sent.message_id, updated.card)
        if not update_result.success:
            raise RuntimeError(f"capacity card update failed: {update_result.raw!r}")
        refetched = await channel.fetch_message(sent.message_id)
        data = refetched.get("data") if isinstance(refetched, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if (
            not isinstance(items, list)
            or not items
            or not isinstance(items[0], dict)
            or items[0].get("message_id") != sent.message_id
            or items[0].get("chat_id") != args.chat_id
        ):
            raise RuntimeError("updated capacity card could not be refetched exactly")
        return {
            "count": args.count,
            "card_json_bytes": len(
                json.dumps(card.card, ensure_ascii=False).encode("utf-8")
            ),
            "send_success": True,
            "self_contained_callback_success": True,
            "line_statistics_round_trip_success": True,
            "full_card_update_success": True,
            "updated_message_refetch_success": True,
        }
    finally:
        await asyncio.to_thread(channel.stop)


def main() -> int:
    args = _parse_args()
    result = asyncio.run(_probe(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
