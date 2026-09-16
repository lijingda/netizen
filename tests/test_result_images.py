from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen import result_images
from netizen.result_images import prepare_result_images


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"


class ResultImagesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.channel = SimpleNamespace(upload_media=AsyncMock(return_value="img_v3_preview"))

    def image(self, name: str = "chart.png") -> Path:
        path = self.root / name
        path.write_bytes(PNG)
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
        self.assertIn("预览暂不可用", answer)
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

    async def test_missing_directory_and_non_image_paths_keep_the_answer(self) -> None:
        self.image().write_text("not image bytes")
        answer = await self.prepare("before ![图](chart.png) ![目录](.) ![缺失](missing.png) after")
        self.assertTrue(answer.startswith("before "))
        self.assertTrue(answer.endswith(" after"))
        self.assertEqual(answer.count("预览暂不可用"), 3)
        self.assertNotIn("![", answer)
        self.assertNotIn(str(self.root), answer)
        self.channel.upload_media.assert_not_awaited()

    async def test_failure_is_per_image_and_is_not_retried_for_duplicate_refs(self) -> None:
        self.image("first.png")
        self.image("second.png")
        self.channel.upload_media.side_effect = [RuntimeError("transport failed"), "img_v3_second"]
        answer = await self.prepare(
            "Before ![一](first.png) ![再次](first.png) ![二](second.png) after",
        )
        self.assertTrue(answer.startswith("Before "))
        self.assertTrue(answer.endswith(" after"))
        self.assertEqual(answer.count("预览暂不可用"), 2)
        self.assertIn("![二](img_v3_second)", answer)
        self.assertEqual(self.channel.upload_media.await_count, 2)

    async def test_count_and_byte_budgets_keep_completed_previews(self) -> None:
        self.image("first.png")
        self.image("second.png")
        for limit, value in (("_MAX_TOTAL_BYTES", len(PNG)), ("_MAX_IMAGES", 1)):
            with self.subTest(limit=limit), patch.object(result_images, limit, value):
                self.channel.upload_media.reset_mock()
                answer = await self.prepare("![一](first.png) ![二](second.png)")
                self.assertIn("![一](img_v3_preview)", answer)
                self.assertIn("预览暂不可用", answer)
                self.channel.upload_media.assert_awaited_once()

    async def test_failed_reads_still_consume_the_reserved_byte_budget(self) -> None:
        first = self.image("first.png")
        self.image("second.png")
        first.write_bytes(b"x" * len(PNG))
        with (
            patch.object(result_images, "_MAX_TOTAL_BYTES", len(PNG)),
            patch.object(result_images, "_read_image", wraps=result_images._read_image) as read,
        ):
            answer = await self.prepare("![一](first.png) ![二](second.png)")
        self.assertEqual(answer.count("预览暂不可用"), 2)
        read.assert_called_once_with(first, len(PNG))
        self.channel.upload_media.assert_not_awaited()

    async def test_upload_timeout_falls_back_but_task_cancellation_propagates(self) -> None:
        self.image()

        async def stall(*args, **kwargs):
            await asyncio.Event().wait()

        self.channel.upload_media.side_effect = stall
        with patch.object(result_images, "_UPLOAD_TIMEOUT_SECONDS", 0.01):
            answer = await self.prepare("Before ![图](chart.png) after")
        self.assertIn("预览暂不可用", answer)
        self.channel.upload_media.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.prepare("![图](chart.png)")

    async def test_invalid_uploaded_key_cannot_inject_markdown(self) -> None:
        self.image()
        self.channel.upload_media.return_value = "img_key) <at id=all></at>"
        answer = await self.prepare("![图](chart.png)")
        self.assertIn("预览暂不可用", answer)
        self.assertNotIn("<at", answer)

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
