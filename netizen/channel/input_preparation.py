"""Prepare native input from exact current, quoted and supplemental messages.

The caller captures admission before preparation and owns submission, context
cursor commits and receipts. This component only reads messages and images;
prepared context anchors are data, not evidence of native acceptance.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from ..domain import FeishuScope, MessageContextAnchor, ScopeKind
from ..image_inputs import (
    ImageDownloadChannel,
    ImageInputError,
    ImageInputUnavailable,
    ImagePromptReferences,
    ImageReference,
    compose_multimodal_input,
    image_prompt_references,
    image_references,
    localize_image_markers,
    prepare_images,
)
from ..message_history import (
    MessageHistoryError,
    MessageHistoryReader,
    MessageHistoryRef,
    MessageHistoryUnavailable,
)
from ..message_preparation import MessagePreparationError, prepare_message_content
from ..message_projection import (
    HistoricalMessageError,
    HistoricalMessageProjection,
    SupplementalContextStats,
    SupplementalMessageOmission,
    compose_message_context_prompt,
    historical_message_deleted,
    normalized_historical_message_type,
    project_quoted_message,
    project_supplemental_message,
    select_supplemental_messages,
)
from ..prompt_projection import (
    CardAnswerProjection,
    CurrentMessageProjection,
    MessageInputProjection,
    PromptProjectionError,
    ScheduledInputProjection,
    project_current_content,
    render_plain_prompt,
)
from ..quoted_context import (
    QuotedMessageUnavailable,
    compose_quoted_prompt,
    validate_quoted_message,
)


logger = logging.getLogger(__name__)

_QUOTE_FETCH_TIMEOUT_SECONDS = 10.0
_CONTEXT_PREPARATION_TIMEOUT_SECONDS = 60.0
_CONTEXT_FETCH_TIMEOUT_SECONDS = 10.0
_CONTEXT_FETCH_CONCURRENCY = 4
_CONTEXT_MESSAGE_LIMIT = 50
_CONTEXT_TEXT_LIMIT = 64_000


class MessageInputChannel(ImageDownloadChannel, Protocol):
    """Only the public read operations required to prepare a prompt."""

    async def fetch_inbound_message(self, message_id: str) -> Any: ...

    async def fetch_quoted_context(self, message_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class PreparedInput:
    """Transient input and optional catch-up facts, without submission authority."""

    native_input: str | list[Any]
    context_anchor: MessageContextAnchor | None = None
    context_stats: SupplementalContextStats | None = None


class MessageInputPreparer:
    """Own message preparation while callers retain operation identity and locks."""

    def __init__(self, *, channel: MessageInputChannel) -> None:
        self._channel = channel

    async def prepare(
        self,
        *,
        source_message: Any,
        quoted_target_id: str | None,
        current: MessageInputProjection,
        current_images: tuple[ImageReference, ...] = (),
    ) -> PreparedInput:
        """Prepare current-only input, including its optional explicit quote."""

        self._validate_anchor_material(current, quoted_target_id, current_images)
        if isinstance(current, (ScheduledInputProjection, CardAnswerProjection)):
            return PreparedInput(native_input=render_plain_prompt(current))

        try:
            current_fallback = await self._prepare_message_content(
                source_message, source="current",
            )
            quoted = None
            fallback_text = None
            quoted_images: tuple[ImageReference, ...] = ()
            if quoted_target_id is not None:
                async with asyncio.timeout(_QUOTE_FETCH_TIMEOUT_SECONDS):
                    quoted = await self._channel.fetch_inbound_message(
                        quoted_target_id
                    )
                if quoted is None:
                    raise QuotedMessageUnavailable(
                        "无法读取被引用的消息；它可能已撤回，"
                        "或应用缺少消息读取权限。本条消息未执行。"
                    )
                validate_quoted_message(
                    quoted,
                    expected_message_id=quoted_target_id,
                    expected_chat_id=str(source_message.conversation.chat_id),
                )
                quoted_images = image_references(
                    quoted,
                    source="quoted_message",
                )

                fallback_text = await self._prepare_message_content(
                    quoted, source="quoted",
                )

            image_references_to_prepare = quoted_images + current_images
            prepared_images = await prepare_images(
                self._channel,
                image_references_to_prepare,
            )
            quoted_read_keys = tuple(
                image.reference.file_key
                for image in prepared_images
                if image.reference.source == "quoted_message"
            )
            prompt_image_refs = image_prompt_references(prepared_images)
            rendered_current = self._render_current_content(
                source_message,
                current,
                fallback_text=current_fallback,
                images=prepared_images,
                image_prompt_refs=prompt_image_refs,
            )
            prompt_text = render_plain_prompt(rendered_current)
            if quoted is not None:
                assert isinstance(rendered_current, CurrentMessageProjection)
                prompt_text = compose_quoted_prompt(
                    quoted,
                    rendered_current,
                    interactive_fallback_text=fallback_text,
                    read_image_keys=quoted_read_keys,
                    image_prompt_refs=prompt_image_refs,
                )

            return PreparedInput(
                native_input=compose_multimodal_input(
                    prompt_text,
                    images=prepared_images,
                    image_prompt_refs=prompt_image_refs,
                ),
            )
        except (HistoricalMessageError, ImageInputError, PromptProjectionError):
            raise
        except TimeoutError as error:
            raise QuotedMessageUnavailable(
                "读取被引用消息超时，本条消息未执行；"
                "请重新发送。"
            ) from error
        except Exception as error:
            logger.warning(
                "prompt context preparation failed",
                extra={"error_type": type(error).__name__},
            )
            if quoted_target_id is not None:
                raise QuotedMessageUnavailable(
                    "无法读取被引用的消息；它可能已撤回，"
                    "或应用缺少消息读取权限。本条消息未执行。"
                ) from error
            raise ImageInputUnavailable(
                "无法处理消息中的图片，本条消息未执行；请重新发送。"
            ) from error

    async def prepare_catch_up(
        self,
        *,
        source_message: Any,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper_id: str,
        quoted_target_id: str | None,
        current: MessageInputProjection,
        current_images: tuple[ImageReference, ...] = (),
        message_history: MessageHistoryReader | None,
    ) -> PreparedInput:
        """Prepare a bounded history window and return its exact upper anchor."""

        self._validate_anchor_material(current, quoted_target_id, current_images)
        if isinstance(current, (ScheduledInputProjection, CardAnswerProjection)) and upper_id != current.message_id:
            raise PromptProjectionError(
                "本次输入的上下文边界与反馈消息不一致，本条消息未执行。"
            )
        reader = message_history
        if reader is None:
            raise MessageHistoryUnavailable(
                "群聊上下文读取能力尚不可用，本条消息未执行；请联系维护者。"
            )

        try:
            async with asyncio.timeout(_CONTEXT_PREPARATION_TIMEOUT_SECONDS):
                current_fallback = (
                    await self._prepare_message_content(source_message, source="current")
                    if isinstance(current, CurrentMessageProjection)
                    else None
                )
                window = await reader.read_window(scope, lower, upper_id)
                fetched = await self._fetch_history_candidates(
                    scope,
                    window.candidates,
                )
                by_id = {
                    reference.message_id: value
                    for reference, value in zip(window.candidates, fetched)
                }
                attribution_names = {
                    reference.message_id: reference.sender_name
                    for reference in window.candidates
                }

                quoted_input: tuple[Any, str | None] | None = None
                quoted_projection: HistoricalMessageProjection | None = None
                if quoted_target_id is not None:
                    quoted_input = by_id.get(quoted_target_id)
                    if quoted_input is None:
                        quoted_input = await self._fetch_normalized_history_message(
                            quoted_target_id,
                            validate=lambda message: validate_quoted_message(
                                message,
                                expected_message_id=quoted_target_id,
                                expected_chat_id=scope.chat_id,
                            ),
                        )
                    quoted_message, quoted_fallback = quoted_input
                    validate_quoted_message(
                        quoted_message,
                        expected_message_id=quoted_target_id,
                        expected_chat_id=scope.chat_id,
                    )
                    quoted_projection = project_quoted_message(
                        quoted_message,
                        interactive_fallback_text=quoted_fallback,
                    )

                eligible_inputs: list[
                    tuple[Any, str | None, HistoricalMessageProjection]
                ] = []
                projection_omissions: list[SupplementalMessageOmission] = []
                for message, fallback in fetched:
                    projection = project_supplemental_message(
                        message,
                        interactive_fallback_text=fallback,
                        attribution_name=attribution_names.get(_message_id(message)),
                    )
                    if isinstance(projection, SupplementalMessageOmission):
                        projection_omissions.append(projection)
                    else:
                        eligible_inputs.append((message, fallback, projection))

                supplemental_stats = SupplementalContextStats(
                    scanned_count=window.stats.raw_messages_scanned,
                    omitted_count=(
                        window.stats.omitted_messages + len(projection_omissions)
                    ),
                    unsupported_omitted_count=sum(
                        omission.reason == "unsupported_message_type"
                        for omission in projection_omissions
                    ),
                    truncated_before=window.stats.truncated_before,
                    message_limit_reached=window.stats.scan_limit_hit,
                )
                selection = select_supplemental_messages(
                    tuple(item[2] for item in eligible_inputs),
                    quoted_message_id=(
                        quoted_projection.message_id
                        if quoted_projection is not None
                        else None
                    ),
                    supplemental_stats=supplemental_stats,
                    max_supplemental_messages=_CONTEXT_MESSAGE_LIMIT,
                    max_supplemental_text=_CONTEXT_TEXT_LIMIT,
                )
                image_eligible_ids = frozenset(
                    projection.message_id for projection in selection.messages
                )

                supplemental_image_references: list[ImageReference] = []
                for message, _, projection in eligible_inputs:
                    if projection.message_id not in image_eligible_ids:
                        continue
                    supplemental_image_references.extend(
                        image_references(
                            message,
                            source="supplemental_message",
                        )
                    )
                quoted_image_references: tuple[ImageReference, ...] = ()
                if quoted_input is not None:
                    quoted_image_references = image_references(
                        quoted_input[0],
                        source="quoted_message",
                    )
        except (MessageHistoryError, HistoricalMessageError, ImageInputError):
            raise
        except TimeoutError as error:
            raise MessageHistoryUnavailable(
                "读取并整理群聊上下文超过时间限制，本条消息未执行；请重试。"
            ) from error
        except Exception as error:
            logger.warning(
                "catch-up context preparation failed",
                extra={"error_type": type(error).__name__},
            )
            raise MessageHistoryUnavailable(
                "无法安全整理群聊上下文，本条消息未执行；请重试。"
            ) from error

        prepared_images = await prepare_images(
            self._channel,
            tuple(supplemental_image_references)
            + quoted_image_references
            + current_images,
        )

        selected_inputs = {
            projection.message_id: (message, fallback)
            for message, fallback, projection in eligible_inputs
            if projection.message_id in image_eligible_ids
        }
        supplemental_projections: list[HistoricalMessageProjection] = []
        for selected in selection.messages:
            assert selected.message_id is not None
            message, fallback = selected_inputs[selected.message_id]
            projection = project_supplemental_message(
                message,
                interactive_fallback_text=fallback,
                read_image_keys=self._prepared_image_keys(
                    prepared_images,
                    source="supplemental_message",
                    message_id=_message_id(message),
                ),
                attribution_name=attribution_names.get(_message_id(message)),
            )
            if isinstance(projection, SupplementalMessageOmission):
                raise MessageHistoryUnavailable(
                    "补充上下文消息在整理期间发生变化，本条消息未执行；请重试。"
                )
            supplemental_projections.append(projection)
        selection = selection.reproject(supplemental_projections)
        final_supplemental_ids = frozenset(
            projection.message_id for projection in selection.messages
        )
        prepared_images = tuple(
            image
            for image in prepared_images
            if image.reference.source != "supplemental_message"
            or image.reference.message_id in final_supplemental_ids
        )

        final_quoted_projection = None
        if quoted_input is not None:
            quoted_message, quoted_fallback = quoted_input
            final_quoted_projection = project_quoted_message(
                quoted_message,
                interactive_fallback_text=quoted_fallback,
                read_image_keys=self._prepared_image_keys(
                    prepared_images,
                    source="quoted_message",
                    message_id=_message_id(quoted_message),
                ),
            )

        prompt_image_refs = image_prompt_references(prepared_images)
        rendered_current = self._render_current_content(
            source_message,
            current,
            fallback_text=current_fallback,
            images=prepared_images,
            image_prompt_refs=prompt_image_refs,
        )
        context = compose_message_context_prompt(
            supplemental_selection=selection,
            quoted_message=final_quoted_projection,
            current=rendered_current,
            image_prompt_refs=prompt_image_refs,
        )
        return PreparedInput(
            native_input=compose_multimodal_input(
                context.text,
                images=prepared_images,
                image_prompt_refs=prompt_image_refs,
            ),
            context_anchor=window.upper,
            context_stats=context.stats,
        )

    async def _fetch_history_candidates(
        self,
        scope: FeishuScope,
        references: tuple[MessageHistoryRef, ...],
    ) -> tuple[tuple[Any, str | None], ...]:
        semaphore = asyncio.Semaphore(_CONTEXT_FETCH_CONCURRENCY)

        async def fetch(
            reference: MessageHistoryRef,
        ) -> tuple[Any, str | None]:
            async with semaphore:
                value = await self._fetch_normalized_history_message(
                    reference.message_id,
                    validate=lambda message: self._validate_history_candidate(
                        scope, reference, message,
                    ),
                )
                return value

        tasks = tuple(asyncio.create_task(fetch(reference)) for reference in references)
        if not tasks:
            return ()
        try:
            return tuple(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_normalized_history_message(
        self,
        message_id: str,
        *,
        validate: Callable[[Any], None] | None = None,
    ) -> tuple[Any, str | None]:
        try:
            async with asyncio.timeout(_CONTEXT_FETCH_TIMEOUT_SECONDS):
                message = await self._channel.fetch_inbound_message(message_id)
        except TimeoutError as error:
            raise MessageHistoryUnavailable(
                "读取补充上下文消息超时，本条消息未执行；请重试。"
            ) from error
        except Exception as error:
            raise MessageHistoryUnavailable(
                "无法读取补充上下文消息，本条消息未执行；请重试。"
            ) from error
        if message is None:
            raise MessageHistoryUnavailable(
                "补充上下文消息已不可读取，本条消息未执行；请重试。"
            )

        if _message_id(message) != message_id:
            raise MessageHistoryUnavailable(
                "补充上下文 exact message ID 不一致，本条消息未执行。"
            )
        if validate is not None:
            validate(message)
        fallback_text = await self._prepare_message_content(message, source="historical")
        return message, fallback_text

    async def _prepare_message_content(
        self,
        message: Any,
        *,
        source: Literal["current", "quoted", "historical"],
    ) -> str | None:
        """One preparation path; only user-facing failure attribution varies."""

        try:
            return await prepare_message_content(
                self._channel,
                message,
                timeout_seconds=(
                    _CONTEXT_FETCH_TIMEOUT_SECONDS
                    if source == "historical" else _QUOTE_FETCH_TIMEOUT_SECONDS
                ),
            )
        except MessagePreparationError as error:
            if source == "historical":
                labels = {
                    "timeout": "读取历史应用消息可见内容超时",
                    "unavailable": "无法读取历史应用消息可见内容",
                    "identity": "历史应用消息没有可验证的可见内容",
                }
                raise MessageHistoryUnavailable(
                    labels[error.reason] + "，本条消息未执行；请重试。"
                ) from error
            if source == "quoted":
                if error.reason == "identity":
                    raise QuotedMessageUnavailable(
                        "被引用的应用消息没有可验证的可见内容，"
                        "请复制内容后重试。本条消息未执行。"
                    ) from error
                if error.reason == "timeout":
                    raise QuotedMessageUnavailable(
                        "读取被引用消息超时，本条消息未执行；请重新发送。"
                    ) from error
                raise QuotedMessageUnavailable(
                    "无法读取被引用的消息；它可能已撤回，"
                    "或应用缺少消息读取权限。本条消息未执行。"
                ) from error
            raise PromptProjectionError(
                "无法读取当前卡片的可见内容，本条消息未执行；"
                "请复制内容或重新发送。"
            ) from error

    @staticmethod
    def _validate_history_candidate(
        scope: FeishuScope,
        reference: MessageHistoryRef,
        message: Any,
    ) -> None:
        if _message_id(message) != reference.message_id:
            raise MessageHistoryUnavailable(
                "补充上下文 exact message ID 不一致，本条消息未执行。"
            )
        conversation = getattr(message, "conversation", None)
        if str(getattr(conversation, "chat_id", "") or "") != scope.chat_id:
            raise MessageHistoryUnavailable(
                "补充上下文消息不属于当前会话，本条消息未执行。"
            )
        thread_id = str(getattr(conversation, "thread_id", "") or "") or None
        if scope.kind is ScopeKind.GROUP and thread_id is not None:
            raise MessageHistoryUnavailable(
                "补充上下文消息不属于当前群聊主线，本条消息未执行。"
            )
        if scope.kind is ScopeKind.TOPIC and thread_id != scope.topic_id:
            raise MessageHistoryUnavailable(
                "补充上下文消息不属于当前话题，本条消息未执行。"
            )
        create_time = getattr(message, "create_time", None)
        if isinstance(create_time, bool):
            actual_create_time = None
        elif isinstance(create_time, int):
            actual_create_time = create_time
        elif isinstance(create_time, str) and create_time.isdigit():
            actual_create_time = int(create_time)
        else:
            actual_create_time = None
        if actual_create_time != reference.create_time_ms:
            raise MessageHistoryUnavailable(
                "补充上下文消息时间与历史索引不一致，本条消息未执行；请重试。"
            )
        if historical_message_deleted(message):
            return
        sender = getattr(message, "sender", None)
        sender_id = str(getattr(sender, "open_id", "") or "")
        if (
            sender_id != reference.sender_id
            or bool(getattr(sender, "is_bot", False))
            or str(getattr(sender, "sender_type", "user") or "") != "user"
        ):
            raise MessageHistoryUnavailable(
                "补充上下文消息发送者与历史索引不一致，本条消息未执行；请重试。"
            )
        actual_type = normalized_historical_message_type(message)
        expected_type = "media" if reference.message_type == "video" else reference.message_type
        if actual_type != expected_type:
            raise MessageHistoryUnavailable(
                "补充上下文消息类型与历史索引不一致，本条消息未执行；请重试。"
            )

    def _render_current_content(
        self,
        message: Any,
        current: MessageInputProjection,
        *,
        fallback_text: str | None,
        images: Sequence[Any],
        image_prompt_refs: ImagePromptReferences,
    ) -> MessageInputProjection:
        if isinstance(current, (ScheduledInputProjection, CardAnswerProjection)):
            return current
        current = project_current_content(
            message,
            current,
            interactive_fallback_text=fallback_text,
            read_image_keys=self._prepared_image_keys(
                images,
                source="current_message",
                message_id=current.message_id,
            ),
        )
        return replace(
            current,
            request_text=localize_image_markers(
                current.request_text,
                source="current_message",
                message_id=current.message_id,
                image_prompt_refs=image_prompt_refs,
            ),
        )

    @staticmethod
    def _validate_anchor_material(
        current: MessageInputProjection,
        quoted_target_id: str | None,
        current_images: tuple[ImageReference, ...],
    ) -> None:
        if isinstance(current, (ScheduledInputProjection, CardAnswerProjection)) and (
            quoted_target_id is not None or current_images
        ):
            raise PromptProjectionError(
                "本次输入不能把反馈消息的引用或图片当作任务材料，本条消息未执行。"
            )

    @staticmethod
    def _prepared_image_keys(
        images: Sequence[Any],
        *,
        source: str,
        message_id: str,
    ) -> tuple[str, ...]:
        return tuple(
            image.reference.file_key
            for image in images
            if image.reference.source == source
            and image.reference.message_id == message_id
        )


def _message_id(message: Any) -> str:
    return str(getattr(message, "message_id", None) or getattr(message, "id", ""))
