"""Best-effort previews of the local images explicitly referenced in a Result."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import unquote, urlsplit

import httpx
from lark_channel import FeishuChannelError, FeishuChannelErrorCode, MediaSource, classify_error
from markdown_it.token import Token

from .external_calls import ExternalCallCancelled, external_call
from .markdown_images import image_tokens, parse_markdown, render_markdown, replace_image_with_text
from .turn_files import is_supported_image_header


_MAX_IMAGES = 20
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_CONCURRENT_IMAGES = 3
_PREVIEW_TIMEOUT_SECONDS = 10.0
_UPLOAD_TIMEOUT_SECONDS = 4.0
_IMAGE_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,256}\Z")
_INVALID_IMAGE_KEY = "飞书未返回有效的图片标识"
logger = logging.getLogger(__name__)


class ImageUploadChannel(Protocol):
    async def upload_media(self, source: MediaSource, *, kind: Literal["image"]) -> str: ...


class _ImageProblem(ValueError):
    """A local preview failure with a message safe to show in the reply."""


def _batch_timeout(stage: str) -> str:
    return f"整批图片处理已达 {_PREVIEW_TIMEOUT_SECONDS:g} 秒，{stage}"


@dataclass(slots=True)
class _Preview:
    key: str | None = None
    reason: str = ""


async def prepare_result_images(
    channel: ImageUploadChannel,
    content: str,
    *,
    cwd: Path | None,
) -> str:
    """Upload each admitted path once, then render results in original Markdown order."""
    if "![" not in content:
        return content
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PREVIEW_TIMEOUT_SECONDS
    tokens, env = parse_markdown(content)
    references: dict[str, list[Token]] = {}
    for token in image_tokens(tokens):
        target = token.attrGet("src") or ""
        parts = urlsplit(target)
        if _IMAGE_KEY.fullmatch(target) or parts.scheme not in {"", "file"} or parts.netloc:
            continue
        references.setdefault(target, []).append(token)
    if not references:
        return content

    slots = asyncio.Semaphore(_CONCURRENT_IMAGES)
    by_path: dict[Path, _Preview] = {}
    by_target: dict[str, _Preview] = {}
    batch = asyncio.timeout_at(deadline)
    try:
        async with batch, asyncio.TaskGroup() as group:
            for target in references:
                if loop.time() >= deadline:
                    break
                try:
                    path = await asyncio.to_thread(_local_path, target, cwd)
                except _ImageProblem as exc:
                    by_target[target] = _Preview(reason=str(exc))
                    continue
                if path in by_path:
                    by_target[target] = by_path[path]
                elif len(by_path) >= _MAX_IMAGES:
                    by_target[target] = _Preview(reason=f"每条回复最多预览 {_MAX_IMAGES} 张不同图片，此图已跳过")
                else:
                    preview = _Preview()
                    by_target[target] = by_path[path] = preview
                    group.create_task(_prepare_one(channel, path, preview, slots, batch))
    except TimeoutError:
        if not batch.expired():
            raise
        logger.warning("inline result image preview batch timed out")
    for target, occurrences in references.items():
        preview = by_target.get(target)
        for token in occurrences:
            if preview and preview.key is not None:
                token.attrSet("src", preview.key)
            else:
                reason = (preview and preview.reason) or _batch_timeout("此图尚未开始处理")
                name = Path(unquote(_path_text(target))).name
                replace_image_with_text(token, f"预览暂不可用：{reason}", fallback_label=name)
    return render_markdown(tokens, env)


def _read_failure(exc: Exception) -> str:
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return "图片文件不存在"
    if isinstance(exc, PermissionError):
        return "没有权限读取图片"
    if isinstance(exc, IsADirectoryError):
        return "图片路径指向目录"
    return "读取图片文件失败"


def _upload_failure(exc: Exception) -> str:
    cause: BaseException = exc
    if isinstance(exc, FeishuChannelError):
        code = exc.context.get("raw_code")
        if exc.code == FeishuChannelErrorCode.RATE_LIMITED:
            return "飞书图片上传触发限流"
        if type(code) is int and code > 0:
            # The Open Platform frequency-control guide specifies 99991400.
            if code == 99991400 or classify_error(code).code == FeishuChannelErrorCode.RATE_LIMITED:
                return f"飞书图片上传触发限流（错误码 {code}）"
            reason = {
                234002: "飞书图片上传鉴权失败",
                234006: "图片大小超过飞书限制",
                234011: "飞书无法识别此图片格式",
                234039: "图片分辨率超过飞书限制",
            }.get(code, "飞书拒绝了图片上传")
            return f"{reason}（错误码 {code}）"
        if exc.code == FeishuChannelErrorCode.PERMISSION_DENIED:
            return "没有向飞书上传图片的权限"
        if exc.code == FeishuChannelErrorCode.UPLOAD_FAILED and "data_keys" in exc.context:
            return _INVALID_IMAGE_KEY
        cause = exc.cause or exc
    if isinstance(cause, (TimeoutError, httpx.TimeoutException)):
        return "图片上传请求超时，未收到飞书确认"
    if isinstance(cause, (ConnectionError, httpx.NetworkError)):
        return "图片上传时网络连接失败"
    return "图片上传失败，未取得具体原因"


async def _prepare_one(
    channel: ImageUploadChannel, path: Path, preview: _Preview, slots: asyncio.Semaphore,
    batch: asyncio.Timeout,
) -> None:
    await slots.acquire()
    read: asyncio.Task[bytes] | None = None
    reading = True
    timer = asyncio.timeout(_UPLOAD_TIMEOUT_SECONDS)
    try:
        deadline = batch.when()
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            return
        async with timer:
            preview.reason = _batch_timeout("此图仍在读取")
            read = asyncio.create_task(asyncio.to_thread(_read_image, path, _MAX_IMAGE_BYTES))
            body = await asyncio.shield(read)
            reading = False
            preview.reason = _batch_timeout("尚未收到此图的上传结果")

            key = await external_call(channel.upload_media(MediaSource(kind="buffer", buffer=body), kind="image"))
            if not isinstance(key, str) or not _IMAGE_KEY.fullmatch(key):
                raise _ImageProblem(_INVALID_IMAGE_KEY)
            preview.key = key

    except asyncio.CancelledError:
        # TaskGroup isolates a cancelled child and propagates cancellation of its parent.
        # At the batch deadline keep the recorded stage for the partial reply.
        if not reading and not batch.expired():
            preview.reason = "上传等待已取消，未取得结果"
        raise
    except Exception as exc:
        if timer.expired():
            stage = "读取图片超时" if reading else "等待飞书上传结果超时"
            preview.reason = f"{stage}（单图处理上限 {_UPLOAD_TIMEOUT_SECONDS:g} 秒）"
        elif isinstance(exc, ExternalCallCancelled):
            preview.reason = "上传等待已取消，未取得结果"
        elif isinstance(exc, _ImageProblem):
            preview.reason = str(exc)
        elif reading:
            preview.reason = _read_failure(exc)
        else:
            preview.reason = _upload_failure(exc)
        logger.warning("inline result image preview unavailable: %s", preview.reason)
    finally:
        if read is None or read.done():
            slots.release()
        else:
            # Cancelling an await cannot stop a filesystem read already running in a
            # thread. Keep its slot until it finishes; its bytes will never be uploaded.
            def release_after_read(done: asyncio.Task[bytes]) -> None:
                if not done.cancelled():
                    done.exception()
                slots.release()

            read.add_done_callback(release_after_read)


def _path_text(target: str) -> str:
    parsed = urlsplit(target)
    return parsed.path if parsed.scheme == "file" else target


def _local_path(target: str, cwd: Path | None) -> Path:
    try:
        target = _path_text(target)
        if not target:
            raise _ImageProblem("图片路径为空")
        path = Path(unquote(target, errors="strict")).expanduser()
        if not path.is_absolute():
            if cwd is None:
                raise _ImageProblem("缺少解析相对路径所需的工作目录")
            path = cwd / path
        return path.resolve()
    except _ImageProblem:
        raise
    except PermissionError as exc:
        raise _ImageProblem("没有权限访问图片路径") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise _ImageProblem("无法解析图片路径") from exc


def _read_image(path: Path, limit: int) -> bytes:
    # Nonblocking open rejects a file replaced by a FIFO without hanging a worker.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise _ImageProblem("图片路径未指向普通文件")
        if not metadata.st_size:
            raise _ImageProblem("图片文件为空")
        if metadata.st_size > limit:
            raise _ImageProblem(
                f"图片大小为 {metadata.st_size:,} 字节，超过单图上限 {limit / (1024 * 1024):g} MiB"
            )
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as stream:
            body = stream.read(metadata.st_size)
        if len(body) != metadata.st_size or os.fstat(fd).st_size != metadata.st_size:
            raise _ImageProblem("图片文件在读取时发生变化")
    finally:
        os.close(fd)
    if not is_supported_image_header(body[:12]):
        raise _ImageProblem("不支持此图片格式，仅支持 PNG、JPEG、GIF 和 WebP")
    return body
