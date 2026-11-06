"""Turn file card actions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from lark_channel import (
    MediaSource,
    OutboundFile,
    OutboundImage,
    SendOpts,
)

from .base import CHANNEL_LOGGER_NAME
from ..cards import (
    CardActionError,
    decode_turn_file_action,
    turn_files_card_from_manifest,
    turn_progress_card_from_manifest,
    reply_card_from_manifest,
)
from ..domain import (
    TurnFileActionIntent,
    TurnFileActionName,
)
from ..turn_files import (
    TurnFile,
    TurnFileError,
    require_turn_file_path,
)

from .app_values import (
    _turn_file_action_uuid,
    _validate_turn_file_reply,
)
from .session_cards import SessionCardChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class TurnFileChannel(SessionCardChannel):
    """Turn file card actions."""

    async def _handle_turn_file_card_action(self, event: Any) -> None:
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        operator = getattr(event, "operator", None)
        sender_id = str(getattr(operator, "open_id", "") or "")
        action = getattr(event, "action", None)
        intent: TurnFileActionIntent | None = None
        try:
            if action is None:
                raise CardActionError("本轮文件回调缺少 action。")
            intent = decode_turn_file_action(
                app_id=self._app_id,
                message_id=message_id,
                callback_chat_id=chat_id,
                sender_id=sender_id,
                tag=str(getattr(action, "tag", "") or ""),
                value=getattr(action, "value", None),
                form_value=getattr(action, "form_value", None),
            )
            await self._turn_file_action(intent)
        except (CardActionError, TurnFileError) as error:
            await self._safe_turn_file_feedback(
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                action=getattr(action, "value", None),
                message=str(error),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "Turn file card interaction failed",
                extra={
                    "error_type": type(error).__name__,
                    "binding_id": getattr(intent, "binding_id", None),
                    "turn_id": getattr(intent, "turn_id", None),
                },
            )
            await self._safe_turn_file_feedback(
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                action=getattr(action, "value", None),
                message="本轮文件操作失败，请重新执行任务后再试。",
            )

    async def _turn_file_action(self, intent: TurnFileActionIntent) -> None:
        if intent.name is TurnFileActionName.PAGE:
            assert intent.page is not None
            assert intent.answer is not None
            if intent.reply is not None:
                card = reply_card_from_manifest(
                    scope=intent.scope,
                    binding_id=intent.binding_id,
                    turn_id=intent.turn_id,
                    manifest=intent.files,
                    reply=intent.reply,
                    page=intent.page,
                    additions=intent.additions,
                    deletions=intent.deletions,
                )
            elif intent.progress is None:
                card = turn_files_card_from_manifest(
                    scope=intent.scope,
                    binding_id=intent.binding_id,
                    turn_id=intent.turn_id,
                    final_response=intent.answer,
                    manifest=intent.files,
                    page=intent.page,
                    additions=intent.additions,
                    deletions=intent.deletions,
                )
            else:
                card = turn_progress_card_from_manifest(
                    scope=intent.scope,
                    binding_id=intent.binding_id,
                    turn_id=intent.turn_id,
                    final_response=intent.answer,
                    manifest=intent.files,
                    progress=intent.progress,
                    page=intent.page,
                    additions=intent.additions,
                    deletions=intent.deletions,
                )
            updated = await self._safe_update_card(intent.source_id, card)
            if not updated:
                raise TurnFileError("飞书未确认本轮文件卡片翻页成功。")
            return

        assert intent.name is TurnFileActionName.SEND
        assert intent.path is not None
        turn_file = require_turn_file_path(intent.path)
        await self._send_turn_file(intent, turn_file)

    async def _send_turn_file(
        self,
        intent: TurnFileActionIntent,
        turn_file: TurnFile,
    ) -> None:
        source = MediaSource(kind="file", path=str(turn_file.resolved_path))
        content: OutboundFile | OutboundImage
        if turn_file.media_kind == "image":
            content = OutboundImage(source=source)
        else:
            content = OutboundFile(
                source=source,
                file_name=turn_file.resolved_path.name,
            )
        sent = await self._channel.send(
            intent.scope.chat_id,
            content,
            SendOpts(
                receive_id_type="chat_id",
                reply_to=intent.source_id,
                reply_in_thread=True,
                reply_target_gone="fail",
                uuid=_turn_file_action_uuid("turn-file-send-", intent),
            ),
        )
        _validate_turn_file_reply(sent, intent=intent)

    async def _safe_turn_file_feedback(
        self,
        *,
        message_id: str,
        chat_id: str,
        sender_id: str,
        action: object,
        message: str,
    ) -> None:
        if not message_id or not chat_id:
            logger.error("cannot report Turn file failure without card identity")
            return
        digest_input = repr((message_id, sender_id, action)).encode("utf-8")
        uuid = "turn-file-error-" + hashlib.sha256(digest_input).hexdigest()[:32]
        try:
            result = await self._channel.send(
                chat_id,
                f"⚠️ 本轮文件操作未完成：{message[:500]}",
                SendOpts(
                    receive_id_type="chat_id",
                    reply_to=message_id,
                    reply_in_thread=True,
                    reply_target_gone="fail",
                    uuid=uuid,
                ),
            )
        except Exception:
            logger.exception("failed to send Turn file callback feedback")
            return
        if not result.success:
            logger.error("Turn file callback feedback was not confirmed")
