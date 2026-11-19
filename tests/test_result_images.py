from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import FeishuChannelError, FeishuChannelErrorCode

from netizen_cli import result_images
from netizen_cli.markdown_images import parse_markdown
from netizen_cli.result_images import prepare_result_images


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"


class ResultImagesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.channel = SimpleNamespace(upload_media=AsyncMock(return_value="img_v3_preview"))

    def image(self, name: str = "chart.png", body: bytes = PNG) -> Path:
        path = self.root / name
        path.write_bytes(body)
        return path

    async def prepare(self, text: str) -> str:
        return await prepare_result_images(self.channel, text, cwd=self.root)

    async def test_reads_referenced_local_image_once_without_any_files_manifest(self) -> None:
        chart = self.image("chart (1).png")
        self.image("unused.png")
        text = (
            "# Report\n\nBefore ![图](<chart (1).png>) after.\n"
            f"![再看]({chart.as_uri()})\n"
            "`![example](unused.png)`\n[download](chart.png)\n"
        )
        answer = await self.prepare(text)
        self.assertEqual(answer, (
            "# Report\n\nBefore ![图](img_v3_preview) after.\n"
            "![再看](img_v3_preview)\n"
            "`![example](unused.png)`\n[download](chart.png)"
        ))
        self.channel.upload_media.assert_awaited_once()
        call = self.channel.upload_media.await_args
        self.assertEqual(call.kwargs, {"kind": "image"})
        self.assertEqual((call.args[0].kind, call.args[0].buffer), ("buffer", PNG))

    async def test_absolute_path_does_not_require_a_cwd_and_relative_path_does(self) -> None:
        chart = self.image()
        answer = await prepare_result_images(
            self.channel, f"![图]({chart}) ![relative](chart.png)", cwd=None,
        )
        self.assertIn("![图](img_v3_preview)", answer)
        self.assertIn("relative", answer)
        self.assertIn("目录", answer)
        self.channel.upload_media.assert_awaited_once()

    async def test_remote_images_existing_keys_and_code_are_not_read_or_rewritten(self) -> None:
        for text in (
            "![远程](https://example.com/image.png)\n",
            "![数据](data:image/png;base64,AAAA)\n",
            "![已有](img_v3_existing)\n",
            "![已有](<img_v3_existing>)\n",
            "![已有][image]\n\n[image]: img_v3_existing\n",
            "`![示例](chart.png)`\n",
            "plain text\n\n[download](chart.png)\n",
        ):
            with self.subTest(text=text), patch.object(
                result_images, "_read_image", side_effect=AssertionError("unexpected read"),
            ):
                self.assertEqual(await self.prepare(text), text)
        self.channel.upload_media.assert_not_awaited()

    async def test_file_problems_have_distinct_reasons_without_uploading(self) -> None:
        self.image("bad.png", b"not image bytes")
        self.image("empty.png", b"")
        with self.image("large.png").open("ab") as oversized:
            oversized.truncate(12 * 1024 * 1024)
        answer = await self.prepare(
            "before ![A](bad.png) ![B](.) ![C](missing.png) ![D](empty.png) ![E](large.png) after",
        )
        self.assertTrue(answer.startswith("before "))
        self.assertTrue(answer.endswith(" after"))
        for label, reason in [
            ("A", "格式"), ("B", "普通文件|目录"), ("C", "不存在"), ("D", "空"), ("E", "10 MiB"),
        ]:
            with self.subTest(label=label):
                self.assertRegex(answer, rf"{label}[^〕]*(?:{reason})[^〕]*〕")
        self.assertIn(str(12 * 1024 * 1024), answer.replace(",", ""))
        self.assertNotIn(str(self.root), answer)
        self.channel.upload_media.assert_not_awaited()

    async def test_permission_failure_uses_safe_local_reason(self) -> None:
        with patch.object(result_images, "_read_image", side_effect=PermissionError("private path")):
            answer = await self.prepare("![A](denied.png)")
        self.assertRegex(answer, "权限|无权")
        self.assertNotIn("private path", answer)
        self.channel.upload_media.assert_not_awaited()

    async def test_upload_failure_is_shared_by_duplicates_and_isolated_from_other_images(self) -> None:
        self.image("first.png")
        self.image("second.png", PNG + b"second")

        async def upload(source, *, kind):
            if source.buffer == PNG:
                raise FeishuChannelError(
                    FeishuChannelErrorCode.UPLOAD_FAILED, "private upload details",
                    context={"raw_code": 99991400, "raw_msg": "private server details"},
                )
            return "img_v3_second"

        self.channel.upload_media.side_effect = upload
        answer = await self.prepare("Before ![A](first.png) ![B](./first.png) ![C](second.png) after")
        for label in ("A", "B"):
            self.assertRegex(answer, rf"{label}[^〕]*限流[^〕]*〕")
        self.assertIn("![C](img_v3_second)", answer)
        self.assertNotIn("private", answer)
        self.assertEqual(self.channel.upload_media.await_count, 2)

    async def test_feishu_rejection_preserves_numeric_business_code(self) -> None:
        self.image()
        self.channel.upload_media.side_effect = FeishuChannelError(
            FeishuChannelErrorCode.UPLOAD_FAILED, "private diagnostic",
            context={"raw_code": 234039, "raw_msg": "private server details"},
        )
        answer = await self.prepare("![A](chart.png)")
        self.assertIn("分辨率", answer)
        self.assertIn("234039", answer)
        self.assertNotIn("private", answer)

    async def test_unknown_upload_error_exposes_only_upload_failure(self) -> None:
        self.image()
        self.channel.upload_media.side_effect = RuntimeError("private transport details")
        answer = await self.prepare("![A](chart.png)")
        self.assertIn("上传", answer)
        self.assertNotIn("不存在", answer)
        self.assertNotIn("private", answer)

    async def test_reports_known_transport_failures_without_the_sdk_error_message(self) -> None:
        self.image()
        for cause, reason in [(ConnectionError("private URL"), "连接"), (TimeoutError("private URL"), "请求超时")]:
            with self.subTest(reason=reason):
                self.channel.upload_media.side_effect = FeishuChannelError(
                    FeishuChannelErrorCode.UPLOAD_FAILED, "private SDK details", cause=cause,
                )
                answer = await self.prepare("![图](chart.png)")
                self.assertIn(reason, answer)
                self.assertNotIn("private", answer)

    async def test_empty_labels_fall_back_to_each_reference_basename(self) -> None:
        first = self.image("source.png")
        alias = self.root / "alias [draft](review).png"
        alias.symlink_to(first)
        self.channel.upload_media.side_effect = RuntimeError("upload error")
        named_alias = self.root / "figure#1?final.png"
        named_alias.symlink_to(first)
        answer = await self.prepare(
            f"![]({first.as_uri()}) ![  ]({alias.as_uri()}) ![]({named_alias.name}) "
            f"![👨‍💻]({first.as_uri()})"
        )
        tokens, _ = parse_markdown(answer)
        children = next(token.children for token in tokens if token.type == "inline")
        self.assertEqual([child.type for child in children], ["text"])
        self.assertIn("图片：source.png", children[0].content)
        self.assertIn(f"图片：{alias.name}", children[0].content)
        self.assertIn(f"图片：{named_alias.name}", children[0].content)
        self.assertIn("图片：👨‍💻", children[0].content)
        self.assertNotIn(str(self.root), answer)
        self.channel.upload_media.assert_awaited_once()

    async def test_long_filename_placeholders_preserve_the_distinguishing_suffix(self) -> None:
        prefix = "a" * 220
        first = f"{prefix}-raw.png"
        second = f"{prefix}-json.png"
        oversized = "x" * 1000 + "-end.png"
        answer = await self.prepare(f"![]({first}) ![ ]({second}) ![]({oversized})")
        self.assertIn(first, answer)
        self.assertIn(second, answer)
        self.assertIn("-end.png", answer)
        self.assertLess(len(answer), len(oversized))
        self.channel.upload_media.assert_not_awaited()

    async def test_only_first_twenty_distinct_paths_are_attempted(self) -> None:
        for index in range(21):
            self.image(f"{index}.png")
        refs = [f"![{index}]({index}.png)" for index in range(21)]
        refs.append("![repeat](./0.png)")
        answer = await self.prepare(" ".join(refs))
        self.assertEqual(answer.count("(img_v3_preview)"), 21)
        self.assertIn("![repeat](img_v3_preview)", answer)
        self.assertRegex(answer, r"图片：20[^〕]*20 张")
        self.assertEqual(self.channel.upload_media.await_count, 20)

    async def test_per_image_limit_remains_available_after_fifty_mib(self) -> None:
        body = PNG.ljust(10 * 1024 * 1024, b"0")
        with patch.object(result_images, "_read_image", return_value=body):
            answer = await self.prepare(" ".join(f"![{i}]({i}.png)" for i in range(6)))
        self.assertEqual(answer.count("(img_v3_preview)"), 6)
        self.assertEqual(self.channel.upload_media.await_count, 6)

    async def test_three_uploads_in_flight_and_reverse_completions_keep_markdown_order(self) -> None:
        started = [asyncio.Event() for _ in range(4)]
        release = [asyncio.Event() for _ in range(4)]
        active = peak = 0
        for i in range(4):
            self.image(f"{i}.png", PNG + bytes([i]))

        async def upload(source, *, kind):
            nonlocal active, peak
            i = source.buffer[-1]
            active += 1
            peak = max(peak, active)
            started[i].set()
            try:
                await release[i].wait()
                return f"img_key_{i}"
            finally:
                active -= 1

        self.channel.upload_media.side_effect = upload
        answer_task = asyncio.create_task(self.prepare(" ".join(f"![{i}]({i}.png)" for i in range(4))))
        try:
            async with asyncio.timeout(3):
                await asyncio.gather(*(event.wait() for event in started[:3]))
                self.assertFalse(started[3].is_set())
                release[2].set()
                await started[3].wait()
        finally:
            for event in reversed(release):
                event.set()
            answer = await answer_task
        self.assertEqual(peak, 3)
        self.assertEqual(answer, " ".join(f"![{i}](img_key_{i})" for i in range(4)))

    async def test_deadlines_distinguish_upload_read_and_queue_and_keep_success(self) -> None:
        for batch in (True, False):
            with self.subTest(batch=batch):
                # Move the event-loop clock only after all three slots reach the chosen stage.
                reading, uploading, occupied = (asyncio.Event() for _ in range(3))
                never = asyncio.Event()
                loop = asyncio.get_running_loop()
                clock = loop.time
                shift = 0

                async def read(fn, *args):
                    if fn is not result_images._read_image:
                        return fn(*args)
                    path, _limit = args
                    if path.stem == "r":
                        reading.set()
                        await never.wait()
                    return PNG + path.stem.encode()

                async def upload(source, *, kind):
                    stem = source.buffer[len(PNG):].decode()
                    if stem == "cancelled":
                        raise asyncio.CancelledError
                    if stem in ("u", "v"):
                        (uploading if stem == "u" else occupied).set()
                        await never.wait()
                    return f"img_key_{stem}"

                self.channel.upload_media.side_effect = upload
                with (
                    patch.object(loop, "time", new=lambda: clock() + shift),
                    patch.object(asyncio, "to_thread", new=read),
                    patch.object(result_images, "_UPLOAD_TIMEOUT_SECONDS", 30.0 if batch else 4.0),
                ):
                    refs = " ".join(f"![{i}]({i}.png)" for i in ("cancelled", "ok", "r", "u", "v", "q"))
                    task = asyncio.create_task(self.prepare(refs))
                    try:
                        async with asyncio.timeout(3):
                            await asyncio.gather(reading.wait(), uploading.wait(), occupied.wait())
                        shift = 11 if batch else 5
                        answer = await asyncio.wait_for(task, 2)
                    finally:
                        never.set()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertIn("![ok](img_key_ok)", answer)
                self.assertRegex(answer, r"图片：cancelled[^〕]*取消[^〕]*〕")
                self.assertRegex(answer, r"图片：r[^〕]*读[^〕]*〕")
                self.assertRegex(answer, r"图片：u[^〕]*上传[^〕]*〕")
                if batch:
                    self.assertRegex(answer, r"图片：q[^〕]*(未开始|排队)")
                    self.assertIn("10 秒", answer)
                else:
                    self.assertIn("![q](img_key_q)", answer)
                    self.assertIn("4 秒", answer)

    async def test_timed_out_disk_reads_keep_their_slots_until_the_workers_return(self) -> None:
        loop = asyncio.get_running_loop()
        clock = loop.time
        shift = active = peak = 0
        lock = threading.Lock()
        release = threading.Event()
        all_reading = asyncio.Event()

        def read(path, limit):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
                if active == 3:
                    loop.call_soon_threadsafe(all_reading.set)
            try:
                if path.stem != "fourth":
                    release.wait(10)
                return PNG
            finally:
                with lock:
                    active -= 1

        with (
            patch.object(loop, "time", new=lambda: clock() + shift),
            patch.object(result_images, "_read_image", side_effect=read),
        ):
            refs = " ".join(f"![{s}]({s}.png)" for s in ("a", "b", "c", "fourth"))
            task = asyncio.create_task(self.prepare(refs))
            try:
                await asyncio.wait_for(all_reading.wait(), 3)
                shift = 5  # The 4s per-image deadline has passed while all three reads still block.
                # Give the loop a chance to dispatch work unblocked by those timeouts.
                dispatched = asyncio.Event()
                loop.call_later(0.05, dispatched.set)
                await asyncio.wait_for(dispatched.wait(), 2)
            finally:
                release.set()
                answer = await asyncio.wait_for(task, 3)
        self.assertEqual(peak, 3)
        self.assertIn("![fourth](img_v3_preview)", answer)
        self.assertEqual(answer.count("4 秒"), 3)
        self.channel.upload_media.assert_awaited_once()

    async def test_external_cancellation_propagates_and_cleans_up_active_uploads(self) -> None:
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def stall(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.image()
        self.channel.upload_media.side_effect = stall
        task = asyncio.create_task(self.prepare("![A](chart.png)"))
        try:
            await asyncio.wait_for(started.wait(), 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())

    async def test_upload_cancellation_is_shared_by_duplicates_and_isolated_from_siblings(self) -> None:
        self.image("cancelled.png")
        self.image("ok.png", PNG + b"ok")
        for self_cancel in (False, True):
            with self.subTest(self_cancel=self_cancel):
                cancelled = asyncio.Event()

                async def upload(source, *, kind):
                    if source.buffer == PNG:
                        try:
                            if self_cancel:
                                asyncio.current_task().cancel()
                                await asyncio.Event().wait()
                            raise asyncio.CancelledError
                        finally:
                            cancelled.set()
                    await cancelled.wait()
                    return "img_v3_ok"

                self.channel.upload_media.reset_mock()
                self.channel.upload_media.side_effect = upload
                answer = await self.prepare("![A](cancelled.png) ![B](./cancelled.png) ![C](ok.png)")
                self.assertRegex(answer, r"图片：A[^〕]*取消[^〕]*〕")
                self.assertRegex(answer, r"图片：B[^〕]*取消[^〕]*〕")
                self.assertIn("![C](img_v3_ok)", answer)
                self.assertEqual(self.channel.upload_media.await_count, 2)

    async def test_invalid_uploaded_key_cannot_inject_markdown(self) -> None:
        self.image()
        self.channel.upload_media.return_value = "img_key) <at id=all></at>"
        answer = await self.prepare("![图](chart.png)")
        self.assertRegex(answer, "标识|凭证")
        self.assertNotIn("<at", answer)
        self.channel.upload_media.side_effect = FeishuChannelError(
            FeishuChannelErrorCode.UPLOAD_FAILED, "private response details",
            context={"kind": "image", "data_keys": []},
        )
        answer = await self.prepare("![图](chart.png)")
        self.assertIn("标识", answer)
        self.assertNotIn("private", answer)

    def test_snapshot_read_rejects_symlink_fifo_and_oversized_image(self) -> None:
        chart = self.image()
        with self.assertRaises(ValueError):
            result_images._read_image(chart, len(PNG) - 1)
        link = self.root / "link.png"
        link.symlink_to(chart)
        with self.assertRaises(OSError):
            result_images._read_image(link, 1024)
        fifo = self.root / "fifo.png"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            result_images._read_image(fifo, 1024)

if __name__ == "__main__":
    unittest.main()
