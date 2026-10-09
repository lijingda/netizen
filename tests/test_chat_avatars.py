from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.chat_avatars import CHAT_AVATAR_HOSTS, CHAT_AVATAR_IMAGE_SOURCES, ChatAvatarImages, avatar_url
from netizen_cli.management.chat_directory import AvailableChat


URL = "https://p3-lark-file.byteimg.com/img/avatar.jpg"


def chat(chat_id="oc_group", url=URL):
    return AvailableChat(chat_id, "同名群", "group", False, url)


class AvatarUrlTest(unittest.TestCase):
    def test_only_https_platform_cdn_urls_are_used(self):
        for url in (URL, "https://s1-imfile.feishucdn.com/static-resource/v1/avatar?x=1",
                    "https://s3-imfile.feishucdn.com/avatar"):
            self.assertEqual(avatar_url(url), url)
        for url in (None, 42, {}, "", "data:image/png;base64,a", "javascript:alert(1)",
                    "http://p3-lark-file.byteimg.com/a", "https://127.0.0.1/a",
                    "https://example.com/a", "https://evilbyteimg.com/a",
                    "https://other.byteimg.com/a", "https://other.feishucdn.com/a",
                    "https://nested.p3-lark-file.byteimg.com/a",
                    "https://p3-lark-file.byteimg.com.evil.test/a",
                    "https://p3-lark-file.byteimg.com@localhost/a",
                    "https://user@p3-lark-file.byteimg.com/a",
                    "https://p3-lark-file.byteimg.com:8443/a",
                    "https://p3-lark-file.byteimg.com:invalid/a", URL + "#fragment",
                    URL + "\n", URL + "\\path", URL + "x" * 4096):
            with self.subTest(url=url):
                self.assertIsNone(avatar_url(url))

    def test_browser_and_upload_policy_share_exact_supported_hosts(self):
        self.assertEqual(CHAT_AVATAR_IMAGE_SOURCES.split(), [f"https://{host}" for host in CHAT_AVATAR_HOSTS])
        for host in CHAT_AVATAR_HOSTS:
            url = f"https://{host}/avatar.png"
            self.assertEqual(avatar_url(url), url)


class ChatAvatarImagesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = SimpleNamespace(upload_media=AsyncMock(return_value="img_v3_avatar"))
        self.images = ChatAvatarImages(self.channel)

    async def test_duplicate_images_share_keys_and_confirmation_does_not_upload_again(self):
        result = await self.images.prepare((chat(), chat("oc_other"), chat("oc_missing", None)))
        self.assertEqual(result, {"oc_group": "img_v3_avatar", "oc_other": "img_v3_avatar"})
        self.assertEqual(await self.images.prepare((chat(),)), {"oc_group": "img_v3_avatar"})
        self.channel.upload_media.assert_awaited_once()
        call = self.channel.upload_media.await_args
        self.assertEqual((call.args[0].kind, call.args[0].url, call.args[0].path), ("url", URL, None))
        self.assertEqual(call.kwargs, {"kind": "image"})

    async def test_changed_url_and_expired_key_refresh_without_changing_target(self):
        with patch("netizen_cli.chat_avatars.monotonic", return_value=100):
            await self.images.prepare((chat(),))
        with patch("netizen_cli.chat_avatars.monotonic", return_value=101):
            await self.images.prepare((chat(url=URL + "?version=2"),))
        with patch("netizen_cli.chat_avatars.monotonic", return_value=701):
            result = await self.images.prepare((chat(),))
        self.assertEqual(result, {"oc_group": "img_v3_avatar"})
        self.assertEqual(self.channel.upload_media.await_count, 3)

    async def test_failed_and_invalid_uploads_only_omit_the_affected_icons(self):
        self.channel.upload_media.side_effect = [RuntimeError("failure"), "not-a-key", "img_v3_ok"]
        chats = tuple(chat(f"oc_{i}", URL + f"?n={i}") for i in range(3))
        self.assertEqual(await self.images.prepare(chats), {"oc_2": "img_v3_ok"})
        self.channel.upload_media.reset_mock()
        self.assertEqual(await self.images.prepare((chat(url="http://localhost/private"),)), {})
        self.channel.upload_media.assert_not_awaited()

    async def test_key_cache_is_bounded(self):
        with patch("netizen_cli.chat_avatars._MAX_KEYS", 2):
            for number in range(3):
                await self.images.prepare((chat(url=URL + f"?n={number}"),))
            await self.images.prepare((chat(url=URL + "?n=0"),))
        self.assertEqual(self.channel.upload_media.await_count, 4)

    async def test_timeout_retains_ready_images_and_cancels_pending_uploads(self):
        stopped = asyncio.Event()
        async def upload(source, **kwargs):
            if source.url == URL:
                return "img_v3_ready"
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        self.channel.upload_media.side_effect = upload
        images = ChatAvatarImages(self.channel, prepare_seconds=0.05)
        result = await images.prepare((chat(), chat("oc_slow", URL + "?slow")))
        self.assertEqual(result, {"oc_group": "img_v3_ready"})
        self.assertTrue(stopped.is_set())

    async def test_cancellation_drains_uploads_and_concurrency_is_bounded(self):
        full = asyncio.Event()
        active = 0
        peak = 0
        async def upload(source, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                full.set()
            try:
                await asyncio.Future()
            finally:
                active -= 1
        self.channel.upload_media.side_effect = upload
        task = asyncio.create_task(self.images.prepare(tuple(chat(f"oc_{i}", URL + f"?n={i}") for i in range(20))))
        await asyncio.wait_for(full.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((peak, active), (4, 0))
