from __future__ import annotations

import asyncio
import base64
import html
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.answer_previews import answer_file_link
from netizen_cli.admin.users import AdminIdentity, AdminUsers
from netizen_cli.admin.web import AdminWebRunner
from netizen_cli.management.blocking_io import (
    BlockingIOExecutorClosed, BlockingIOExecutorSaturated, BlockingIOResultUnknown,
)
from tests.support.channel_fixtures import channel_fixture

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOesAAAAASUVORK5CYII="
)


class PathFilesWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.management = self.fixture.management
        self.root = self.fixture.project.resolve()
        instance = self.fixture.project_root / "instance"
        instance.mkdir()
        users = AdminUsers(instance, "cli_test")
        member = users.add_initial("ou_admin")
        self.auth = AdminAuth(users)
        self.runner = AdminWebRunner(host="127.0.0.1", port=0, users=users,
            auth=self.auth, management=self.management,
            feishu_client=SimpleNamespace(app_id="cli_test", aclose=AsyncMock()))
        with patch("netizen_cli.admin.web.accepted_authorities",
                   side_effect=lambda _host, _port, addresses: (f"127.0.0.1:{addresses[0][1]}",)):
            await self.runner.bind()
        self.addAsyncCleanup(self.close_runner)
        self.port = self.runner.addresses[0][1]
        self.runner.open_admission()
        self.session = self.auth.issue_session(AdminIdentity("cli_test", "ou_admin"),
            source_ip="127.0.0.1", expected_add_revision=member.add_revision).token
        (self.root / "readme.md").write_text("# Hello\n\n![pixel](pixel.png) [source](hello.txt) [absolute](/tmp/file)")
        (self.root / "hello.txt").write_text("hello")
        (self.root / "pixel.png").write_bytes(PNG)

    async def close_runner(self):
        try:
            await self.runner.drain(asyncio.get_running_loop().time() + 5)
        finally:
            self.runner.close_auth()

    async def request(self, target, *, authenticated=True):
        async with asyncio.timeout(10):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            try:
                cookie = f"Cookie: netizen_admin_session={self.session}\r\n" if authenticated else ""
                writer.write(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n{cookie}\r\n".encode("ascii"))
                await writer.drain()
                header = (await reader.readuntil(b"\r\n\r\n")).decode("latin1").split("\r\n")
                status = int(header[0].split()[1])
                headers = dict(line.lower().split(": ", 1) for line in header[1:] if line)
                body = await reader.readexactly(int(headers["content-length"]))
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                return status, headers, body
            finally:
                writer.close()
                await writer.wait_closed()

    def target(self, operation, **query):
        return f"/api/v1/path-files/{operation}?" + urlencode({"root": str(self.root), **query})

    async def get(self, operation, **query):
        status, headers, body = await self.request(self.target(operation, **query))
        return status, headers, json.loads(body)

    async def open(self):
        status, _, opened = await self.get("root")
        self.assertEqual(status, 200, opened)
        return opened

    async def test_authentication_assets_and_closed_query_shapes(self):
        for path in [self.target(op) for op in ("root", "directory", "content", "image")] + ["/static/path-files.js"]:
            with self.subTest(path=path):
                self.assertEqual((await self.request(path, authenticated=False))[0], 401)
        self.assertEqual((await self.request("/static/path-files.js"))[0], 200)
        for operation in ("root", "directory", "content", "image"):
            for query in ({}, {"root": str(self.root), "unknown": "x"}):
                with self.subTest(operation=operation, query=query):
                    status, _, body = await self.request(f"/api/v1/path-files/{operation}?" + urlencode(query))
                    self.assertEqual((status, json.loads(body)["code"]), (400, "invalid_query"))
        for operation, query in (("root", {"identity": "x"}), ("directory", {}),
                                 ("content", {"identity": "x", "path": "../escape"}),
                                 ("image", {"identity": "x", "version": ["a", "b"]})):
            self.assertEqual((await self.get(operation, **query))[0], 400)

    async def test_canonical_root_listing_markdown_and_versioned_image(self):
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        status, _, opened = await self.get("root", root=str(alias))
        self.assertEqual(status, 200)
        self.assertEqual(opened["cwd"], str(self.root))
        query = {"root": opened["cwd"], "identity": opened["identity"]}
        status, _, listing = await self.get("directory", **query)
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["entries"]), 3)
        status, _, content = await self.get("content", path="readme.md", **query)
        self.assertEqual(status, 200, content)
        self.assertIn('data-file-browser-path="hello.txt"', content["markdownHtml"])
        self.assertNotIn("data-answer-file-", content["markdownHtml"])
        image_url = html.unescape(re.search(r'<img[^>]+src="([^"]+)"', content["markdownHtml"])[1])
        self.assertEqual(urlsplit(image_url).path, "/api/v1/path-files/image")
        self.assertEqual(parse_qs(urlsplit(image_url).query)["root"], [str(self.root)])
        status, headers, body = await self.request(image_url)
        self.assertEqual((status, headers["content-type"], body), (200, "image/png", PNG))
        status, _, image = await self.get("content", path="pixel.png", **query)
        self.assertEqual(status, 200)
        self.assertIn("version=" + image["version"], image["imageUrl"])
        self.assertEqual((await self.request(image["imageUrl"]))[0], 200)
        (self.root / "pixel.png").write_bytes(PNG + b"\n")
        status, _, body = await self.request(image["imageUrl"])
        self.assertEqual((status, json.loads(body)["code"]), (409, "file_changed"))
        self.assertEqual((await self.get("image", path="hello.txt", **query))[0], 415)

    async def test_answer_link_opens_a_directory_with_trailing_space(self):
        directory = self.root / "目录 "
        directory.mkdir()
        file = directory / "a.py"
        file.write_text("print('tail space')")
        link = answer_file_link(quote(str(file)))
        self.assertEqual(link["data-answer-file-root"], str(directory))
        status, _, opened = await self.get("root", root=link["data-answer-file-root"])
        self.assertEqual(status, 200, opened)
        self.assertEqual(opened["cwd"], str(directory))
        query = {"root": opened["cwd"], "identity": opened["identity"]}
        status, _, listing = await self.get("directory", **query)
        self.assertEqual(status, 200, listing)
        self.assertEqual(listing["entries"][0]["name"], "a.py")
        status, _, content = await self.get("content", path=link["data-answer-file-name"], **query)
        self.assertEqual(status, 200, content)
        self.assertEqual(content["text"], "print('tail space')")

    async def test_root_query_rejects_invalid_encoding_and_byte_budget(self):
        for value in ("", "/" + "a" * 4096, "/" + "猫" * 1366, "/bad\ud800"):
            with self.subTest(value=value[:20]), patch("netizen_cli.admin.web._optional_one", return_value=value):
                status, _, body = await self.request(self.target("root"))
                self.assertEqual((status, json.loads(body)["code"]),
                                 (400, "invalid_query" if not value else "invalid_path"))

    async def test_read_errors_map_to_timeout_unavailable_and_busy(self):
        for error, expected, code in ((TimeoutError(), 504, "read_timeout"),
                (BlockingIOResultUnknown(deadline=0), 504, "read_timeout"),
                (BlockingIOExecutorClosed(), 503, "read_unavailable"),
                (BlockingIOExecutorSaturated(), 429, "busy")):
            with self.subTest(code=code), patch.object(self.management.path_files, "root", new=AsyncMock(side_effect=error)):
                status, _, response = await self.get("root")
                self.assertEqual((status, response["code"]), (expected, code))

    async def test_revocation_during_content_read_prevents_response(self):
        opened = await self.open()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.management.path_files.content

        async def blocked(**kwargs):
            result = await original(**kwargs)
            entered.set()
            await release.wait()
            return result

        with patch.object(self.management.path_files, "content", blocked):
            pending = asyncio.create_task(self.get("content", identity=opened["identity"], path="hello.txt"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.auth.logout(self.session)
            finally:
                release.set()
            status, _, response = await pending
        self.assertEqual(status, 401)
        self.assertNotIn("text", response)
