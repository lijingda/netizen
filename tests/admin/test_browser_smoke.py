"""Opt-in end-to-end smoke of the Admin HTTP server and a local Chromium."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

_DRIVER = Path(__file__).with_name("browser_smoke.js")
_BROWSER_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOesAAAAASUVORK5CYII="


def find_browser():
    override = os.environ.get("NETIZEN_TEST_BROWSER")
    if override:
        if not Path(override).is_absolute():
            raise ValueError("NETIZEN_TEST_BROWSER must be an absolute executable path")
        return shutil.which(override)
    return next((path for name in _BROWSER_NAMES if (path := shutil.which(name))), None)


def report(result):
    output = json.dumps(result, ensure_ascii=False)
    if path := os.environ.get("NETIZEN_BROWSER_RESULT"):
        Path(path).write_text(output + "\n", encoding="utf-8")
    return output


async def seed(fixture):
    from netizen_cli.attachments import AttachmentInput, AttachmentStore
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.management.attachment_files import AttachmentFileService
    from netizen_cli.runtime.contracts import (
        NativeAnswerImage, NativeConversationPage, NativeConversationTurn, NativeThreadMetadata,
    )
    from netizen_cli.schedules.models import ScheduleRule
    from netizen_cli.worktrees.policy import WorkspacePolicy
    from netizen_cli.worktrees.settings import WorktreeSettingsService

    project = fixture.project.resolve()
    (project / "readme.md").write_text("# Browser smoke\n\n![Pixel](pixel.png)\n", encoding="utf-8")
    (project / "hello.py").write_text('print("hello smoke")\n', encoding="utf-8")
    (project / "pixel.png").write_bytes(base64.b64decode(_PIXEL))

    now = int(time.time())
    thread = "smoke-thread"
    created = await fixture.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
    fixture.runtime.materialize(created.binding.id, thread, cwd=project)
    fixture.runtime.thread_metadata_values[thread] = NativeThreadMetadata(thread, "Browser smoke", "", now, now)
    image = str(project / "pixel.png")
    turn = NativeConversationTurn(
        "smoke-turn", "completed", latest=True, images=(NativeAnswerImage(image, _PIXEL),),
        answer=f"# Smoke answer\n\n![Pixel](<{image}>) [Source](<{project / 'hello.py'}>)",
    )
    fixture.runtime.thread_conversation = AsyncMock(return_value=NativeConversationPage(thread, (turn,)))

    fixture.store.schedules.create(
        name="Smoke schedule", instructions="Inspect the local fixture.", project_alias="test",
        app_id="cli_test", chat_id="oc_group", workspace=WorkspacePolicy(), request_id="smoke",
        schedule=ScheduleRule("interval", "UTC", every_minutes=60, anchor=now),
    )
    attachments = AttachmentStore(fixture.project_root.resolve() / "attachments")
    attachments.publish(attachments.prepare([AttachmentInput("note.md", b"# Smoke attachment")]))
    fixture.management.attachments = AttachmentFileService(attachments, fixture.management.file_reads)
    fixture.management.enable_worktrees(settings=WorktreeSettingsService(default_root=project.parent / "worktrees"))


async def run_driver(node, config):
    # Chromium inherits the driver's private process group. The parent owns the
    # profile and can reap the group even if Node hangs or dies during startup.
    process = await asyncio.create_subprocess_exec(
        node, str(_DRIVER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        async with asyncio.timeout(120):
            out, err = await process.communicate(json.dumps(config).encode())
        try:
            result = json.loads(out)
        except ValueError:
            result = {"error": f"Driver exited {process.returncode} without JSON: {err.decode(errors='replace')[:1000]}"}
        return process.returncode, result
    except TimeoutError:
        return 1, {"error": "Driver exceeded the 120 s deadline"}
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


@unittest.skipUnless(os.environ.get("NETIZEN_BROWSER_SMOKE") == "1", "set NETIZEN_BROWSER_SMOKE=1 to run")
class BrowserSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_pages(self):
        node = shutil.which("node")
        browser = find_browser()
        if not node or not browser:
            reason = ("Admin browser smoke requires Node.js 22" if not node else
                      "Chrome/Chromium unavailable; set NETIZEN_TEST_BROWSER to its absolute executable path")
            if os.environ.get("CI") == "true":
                self.fail(report({"error": reason}))
            self.skipTest(reason)

        from netizen_cli.admin.auth import AdminAuth
        from netizen_cli.admin.feishu import UserProfile, UserTokens
        from netizen_cli.admin.users import AdminUsers
        from netizen_cli.admin.web import AdminWebRunner, _ADMIN_PAGE_PATHS
        from netizen_cli.group_access import GroupAccessService
        from netizen_cli.sessions.policy import SessionPolicyService
        from tests.support.channel_fixtures import channel_fixture

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = directory / "instance"
        root.mkdir()
        self.enterContext(patch.dict(os.environ, NETIZEN_ROOT=str(root)))
        self.enterContext(patch("netizen_cli.cli_services.ServiceManager.inspect", return_value=None))
        fixture = await self.enterAsyncContext(channel_fixture())
        await seed(fixture)
        users = AdminUsers(root, "cli_test")
        member = users.add_initial("ou_admin", name="Smoke Admin")
        auth = AdminAuth(users)
        feishu = SimpleNamespace(app_id="cli_test", aclose=AsyncMock(),
                                 user_info=AsyncMock(return_value=UserProfile("ou_admin", "Smoke Admin", "")))
        runner = AdminWebRunner(host="127.0.0.1", port=0, users=users, auth=auth, feishu_client=feishu,
                                instance_root=root)
        runner.attach_management(fixture.management, group_access=GroupAccessService(),
                                 session_policy=SessionPolicyService())
        with patch("netizen_cli.admin.web.accepted_authorities",
                   side_effect=lambda host, port, addresses: (f"127.0.0.1:{addresses[0][1]}",)):
            await runner.bind()

        async def close_runner():
            try:
                await runner.drain(asyncio.get_running_loop().time() + 5)
            finally:
                runner.close_auth()

        self.addAsyncCleanup(close_runner)
        runner.open_admission()
        await runner._credentials.save_login(
            member.identity, UserTokens("fixture-token", None, time.time() + 3600, None, ()),
            expected_add_revision=member.add_revision,
        )
        token = auth.issue_session(member.identity, source_ip="127.0.0.1",
                                   expected_add_revision=member.add_revision).token
        status, result = await run_driver(node, {
            "browser": browser, "profile": str(directory / "chrome"),
            "origin": f"http://127.0.0.1:{runner.addresses[0][1]}",
            "cookie": {"name": runner.application._session_cookie, "value": token},
            "paths": sorted(_ADMIN_PAGE_PATHS),
        })
        output = report(result)
        self.assertEqual(status, 0, output)
        self.assertNotIn("error", result)
        print(output)
