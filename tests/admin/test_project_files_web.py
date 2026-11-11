from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.file_previews import PreviewUnavailable, render_markdown
from netizen_cli.admin.users import AdminIdentity, AdminUsers
from netizen_cli.admin.web import AdminWebRunner
from netizen_cli.management.blocking_io import (
    BlockingIOExecutorClosed,
    BlockingIOExecutorSaturated,
    BlockingIOResultUnknown,
)
from netizen_cli.project_files import MAX_TEXT_BYTES
from tests.support.channel_fixtures import channel_fixture


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV4sAAAAASUVORK5CYII="
)


class ProjectFilesWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.project = self.fixture.project.resolve()
        self.management = self.fixture.management
        self.store = self.fixture.store
        instance = self.fixture.project_root / "instance"
        instance.mkdir()
        self.users = AdminUsers(instance, "cli_test")
        member = self.users.add_initial("ou_admin")
        self.auth = AdminAuth(self.users)
        self.runner = AdminWebRunner(host="127.0.0.1", port=0, users=self.users,
            auth=self.auth, management=self.management,
            feishu_client=SimpleNamespace(app_id="cli_test", aclose=AsyncMock()))
        with patch("netizen_cli.admin.web.accepted_authorities",
            side_effect=lambda _host, _port, addresses: (f"127.0.0.1:{addresses[0][1]}",)):
            await self.runner.bind()
        self.addAsyncCleanup(self.close_runner)
        self.port = self.runner.addresses[0][1]
        self.authority = f"127.0.0.1:{self.port}"
        self.runner.open_admission()
        self.session = self.auth.issue_session(AdminIdentity("cli_test", "ou_admin"),
            source_ip="127.0.0.1", expected_add_revision=member.add_revision).token
        (self.project / "hello.py").write_text('print("你好")\n', encoding="utf-8")
        (self.project / "pixel.png").write_bytes(_PNG)

    async def close_runner(self):
        try:
            await self.runner.drain(asyncio.get_running_loop().time() + 5)
        finally:
            self.runner.close_auth()

    async def request(self, path, *, authenticated=True, method="GET", body=b""):
        async with asyncio.timeout(15):
            return await self._request(path, authenticated=authenticated, method=method, body=body)

    async def _request(self, path, *, authenticated, method, body):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        headers = [("Host", self.authority), ("Origin", f"http://{self.authority}")]
        if authenticated:
            headers.append(("Cookie", f"netizen_admin_session={self.session}"))
        if body:
            headers += [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        writer.write((f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in headers)
            + "\r\n").encode("ascii") + body)
        await writer.drain()
        try:
            status = int((await reader.readline()).split()[1])
            response_headers = {}
            while (line := await reader.readline()) != b"\r\n":
                if not line:
                    raise AssertionError("response ended before its headers")
                key, value = line.decode("latin1").split(":", 1)
                response_headers[key.lower()] = value.strip()
            content = await reader.readexactly(int(response_headers.get("content-length", "0")))
            return status, response_headers, content
        finally:
            writer.close()
            await writer.wait_closed()

    def target(self, operation, **query):
        return f"/api/v1/project-files/{operation}?" + urlencode({"project": "test", **query})

    async def get(self, operation, **query):
        status, headers, body = await self.request(self.target(operation, **query))
        return status, headers, json.loads(body)

    async def root(self):
        status, _, root = await self.get("root")
        self.assertEqual(status, 200, root)
        return root

    def assert_private_response(self, headers):
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertIn("object-src 'none'", headers["content-security-policy"])
        self.assertNotIn("attachment", headers.get("content-disposition", ""))

    async def test_queries_images_and_vendor_assets_all_require_authentication(self):
        paths = [self.target(operation) for operation in ("root", "directory", "content", "image", "changes", "diff")]
        assets = ("content-preview.js", "content-preview.css", "file-browser.js", "project-files.js", "attachment-files.js", "vendor/ace/ace.js",
            "vendor/ace/mode-python.js", "vendor/ace/ext-diff.js")
        for path in [*paths, *("/static/" + asset for asset in assets)]:
            with self.subTest(path=path):
                status, headers, body = await self.request(path, authenticated=False)
                self.assertEqual(status, 401, body)
                self.assert_private_response(headers)
        for asset in assets:
            status, headers, body = await self.request("/static/" + asset)
            self.assertEqual(status, 200, asset)
            self.assertTrue(body)
            self.assert_private_response(headers)
        status, _, _ = await self.request("/static/vendor/ace/worker-javascript.js")
        self.assertEqual(status, 404)

    async def test_git_routes_queries_deadlines_and_unavailable_response(self):
        root = await self.root()
        for operation in ("changes", "diff"):
            with self.subTest(operation=operation):
                for query in ({}, {"identity": root["identity"], "path": "../escape"},
                              {"identity": root["identity"], "extra": "unexpected"}):
                    status, _, _ = await self.get(operation, **query)
                    self.assertEqual(status, 400)
                before = asyncio.get_running_loop().time()
                with patch.object(self.management.files, operation, new_callable=AsyncMock,
                                  return_value={"git": "unavailable", "message": "busy"}) as read:
                    status, headers, data = await self.get(operation, identity=root["identity"], path="hello.py")
                self.assertEqual(status, 200)
                self.assertEqual(data["git"], "unavailable")
                self.assert_private_response(headers)
                self.assertGreater(read.call_args.kwargs["deadline"], before + 9)
        status, _, data = await self.get("changes", identity=root["identity"])
        self.assertEqual(status, 200)
        self.assertEqual(data["git"], "not_repository")
        status, _, _ = await self.get("diff", identity=root["identity"])
        self.assertEqual(status, 400)

    async def test_directory_text_markdown_and_image_share_authenticated_selected_root(self):
        (self.project / "docs").mkdir()
        markdown = ('# 标题\n\n<script>alert(1)</script>\n\n'
            '[source](../hello.py)\n\n![local](../pixel.png)\n\n'
            '![remote](https://example.invalid/track.png)\n')
        (self.project / "docs" / "readme.md").write_text(markdown, encoding="utf-8")
        root = await self.root()
        self.assertEqual(root["rootId"], "project")
        self.assertEqual(root["projectAlias"], "test")
        self.assertEqual(root["cwd"], str(self.project))
        status, headers, directory = await self.get("directory", identity=root["identity"], path="")
        self.assertEqual(status, 200, directory)
        self.assertEqual([entry["path"] for entry in directory["entries"]],
            ["docs", "hello.py", "pixel.png"])
        self.assertFalse(directory["truncated"])
        self.assert_private_response(headers)
        status, headers, text = await self.get("content", identity=root["identity"], path="hello.py")
        self.assertEqual(status, 200, text)
        self.assertEqual((text["kind"], text["language"], text["text"]),
            ("text", "python", 'print("你好")\n'))
        self.assert_private_response(headers)
        status, headers, result = await self.get("content", identity=root["identity"], path="docs/readme.md")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["text"], markdown)
        rendered = result["markdownHtml"]
        self.assertIn("<h1>标题</h1>", rendered)
        self.assertNotIn("<script", rendered)
        self.assertNotIn('src="https://', rendered)
        self.assertIn('data-file-browser-path="hello.py"', rendered)
        local_image = html.unescape(re.search(r'<img[^>]+src="([^"]+)"', rendered).group(1))
        status, headers, image = await self.request(local_image)
        self.assertEqual(status, 200, image)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(image, _PNG)
        self.assert_private_response(headers)

    async def test_image_content_url_pins_version_and_rejects_replaced_image(self):
        root = await self.root()
        status, _, content = await self.get("content", identity=root["identity"], path="pixel.png")
        self.assertEqual(status, 200, content)
        self.assertEqual(content["kind"], "image")
        self.assertNotIn("text", content)
        self.assertIn("version=" + content["version"], content["imageUrl"])
        status, _, body = await self.request(content["imageUrl"])
        self.assertEqual((status, body), (200, _PNG))
        (self.project / "pixel.png").write_bytes(_PNG + b"\n")
        status, headers, body = await self.request(content["imageUrl"])
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["code"], "file_changed")
        self.assert_private_response(headers)
        status, _, current = await self.get("content", identity=root["identity"], path="pixel.png")
        self.assertEqual(status, 200, current)
        self.assertNotEqual(current["version"], content["version"])

    async def test_image_endpoint_never_serves_text_html_or_svg_as_active_content(self):
        (self.project / "active.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>')
        (self.project / "active.html").write_text("<script>alert(1)</script>")
        root = await self.root()
        for path in ("hello.py", "active.svg", "active.html"):
            with self.subTest(path=path):
                status, headers, body = await self.request(self.target("image", identity=root["identity"], path=path))
                self.assertEqual(status, 415, body)
                self.assertEqual(json.loads(body)["code"], "unsupported_file")
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assert_private_response(headers)

    async def test_query_shape_is_closed_before_filesystem_access(self):
        root = await self.root()
        for operation, query in (
            ("root", "project=test&project=test"),
            ("root", "project=test&cwd=%2Ftmp"),
            ("root", "project=test&identity=unexpected"),
            ("root", "project=test&worktree=x&worktree=y"),
            ("root", "project="),
            ("directory", "project=test&path="),
            ("directory", "project=test&identity=x&path=a&path=b"),
            ("content", "project=test&identity=x&identity=y&path=hello.py"),
            ("content", "project=test&identity=x&path=hello.py&download=true"),
            ("image", "project=test&identity=x&path=pixel.png&version=x&version=y"),
        ):
            with self.subTest(operation=operation, query=query), \
                    patch.object(self.management.files, "root", new_callable=AsyncMock) as roots, \
                    patch.object(self.management.files, "directory", new_callable=AsyncMock) as directories, \
                    patch.object(self.management.files, "content", new_callable=AsyncMock) as contents:
                status, _, body = await self.request(f"/api/v1/project-files/{operation}?{query}")
                self.assertEqual(status, 400, body)
                roots.assert_not_awaited()
                directories.assert_not_awaited()
                contents.assert_not_awaited()
        for path in ("../hello.py", "/etc/passwd", "docs/../../hello.py", "a\\b", ".git/config", "a\x00b"):
            with self.subTest(path=path):
                status, _, error = await self.get("content", identity=root["identity"], path=path)
                self.assertEqual(status, 400, error)
                self.assertEqual(error["code"], "invalid_path")

    async def test_download_and_file_mutation_routes_do_not_exist(self):
        for method, operation in (("GET", "download"), ("POST", "content"),
            ("POST", "directory"), ("DELETE", "content")):
            status, _, body = await self.request(self.target(operation), method=method,
                body=b"{}" if method == "POST" else b"")
            self.assertEqual(status, 405 if method == "DELETE" else 404, body)
        self.assertEqual((self.project / "hello.py").read_text(encoding="utf-8"), 'print("你好")\n')

    async def test_preview_limits_and_unsupported_files_have_safe_explicit_errors(self):
        with (self.project / "large.txt").open("wb") as stream:
            stream.truncate(MAX_TEXT_BYTES + 1)
        (self.project / "binary.bin").write_bytes(b"\x00\x01\x02")
        (self.project / "link.txt").symlink_to(self.project / "hello.py")
        root = await self.root()
        for path, expected_status, code in (("large.txt", 413, "preview_too_large"),
            ("binary.bin", 415, "unsupported_file"), ("link.txt", 403, "unsafe_path"),
            ("missing.txt", 404, "not_found")):
            with self.subTest(path=path):
                status, headers, error = await self.get("content", identity=root["identity"], path=path)
                self.assertEqual(status, expected_status, error)
                self.assertEqual(error["code"], code)
                self.assert_private_response(headers)

    async def test_expired_root_identity_is_a_conflict_and_never_returns_content(self):
        root = await self.root()
        self.project.rename(self.project.with_name("old-project"))
        self.project.mkdir()
        (self.project / "hello.py").write_text("private replacement")
        status, _, error = await self.get("content", identity=root["identity"], path="hello.py")
        self.assertEqual(status, 409, error)
        self.assertEqual(error["code"], "root_changed")
        self.assertNotIn("private replacement", json.dumps(error))

    async def test_logout_during_read_prevents_file_response(self):
        root = await self.root()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.management.files.content

        async def blocked(**kwargs):
            result = await original(**kwargs)
            entered.set()
            await release.wait()
            return result

        with patch.object(self.management.files, "content", blocked):
            request = asyncio.create_task(self.get("content", identity=root["identity"], path="hello.py"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.auth.logout(self.session)
            finally:
                release.set()
            status, _, error = await request
        self.assertEqual(status, 401, error)
        self.assertNotIn("text", error)

    async def test_registration_change_during_markdown_render_discards_response(self):
        (self.project / "readme.md").write_text("# previous registration\n", encoding="utf-8")
        for preview_available in (True, False):
            with self.subTest(preview_available=preview_available):
                root = await self.root()
                entered, release = threading.Event(), threading.Event()

                def blocked(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test did not release Markdown rendering")
                    if not preview_available:
                        raise PreviewUnavailable("Markdown 预览超限，请查看源码。")
                    return render_markdown(*args, **kwargs)

                with patch("netizen_cli.admin.web.render_markdown", blocked):
                    request = asyncio.create_task(self.get("content", identity=root["identity"], path="readme.md"))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        snapshot = (await self.store.preview_project_delete("test"))
                        reserved = self.store.begin_project_delete(alias="test",
                            expected_revision=snapshot.project.revision,
                            expected_inventory_fingerprint=snapshot.fingerprint)
                        self.store.finish_project_delete(alias="test",
                            expected_revision=reserved.project.revision,
                            expected_inventory_fingerprint=reserved.fingerprint)
                        self.store.register_project(alias="test", cwd=str(self.project))
                    finally:
                        release.set()
                        status, _, result = await request
                self.assertEqual((status, result["code"]), (409, "root_changed"))
                self.assertNotIn("previous registration", json.dumps(result))
                self.assertNotIn("text", result)
                self.assertNotIn("markdownHtml", result)

    async def test_read_timeouts_are_not_reported_as_unknown_mutations(self):
        errors = ((TimeoutError(), 504, "read_timeout"),
            (BlockingIOResultUnknown(deadline=asyncio.get_running_loop().time()), 504, "read_timeout"),
            (BlockingIOExecutorClosed(), 503, "read_unavailable"),
            (BlockingIOExecutorSaturated(), 429, "busy"))
        for error, expected_status, code in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.object(self.management.files, "root", new_callable=AsyncMock, side_effect=error):
                status, headers, result = await self.get("root")
                self.assertEqual(status, expected_status, result)
                self.assertEqual(result["code"], code)
                self.assertNotIn("unknown", json.dumps(result))
                self.assert_private_response(headers)
