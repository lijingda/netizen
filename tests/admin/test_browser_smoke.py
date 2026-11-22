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
from unittest.mock import AsyncMock, Mock, patch

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


async def run_in_group(args, *, deadline, data=None):
    # Reap descendants even if the version probe or the driver hangs during launch.
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        async with asyncio.timeout_at(deadline):
            out, err = await process.communicate(data)
        return process.returncode, out, err
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def run_driver(node, config):
    now = asyncio.get_running_loop().time()
    info = {"browserPath": config["browser"]}
    try:
        status, out, err = await run_in_group([config["browser"], "--version"], deadline=now + 5)
        version = (out.strip() or err.strip()).decode(errors="replace")[:500]
        info["browserVersion"] = version if status == 0 and version else f"unavailable (exit {status}): {version}"
    except TimeoutError:
        info["browserVersion"] = "unavailable (--version timed out after 5 s)"
    except OSError as error:
        info["browserVersion"] = f"unavailable ({error})"

    try:
        status, out, err = await run_in_group(
            [node, str(_DRIVER)], deadline=now + 120, data=json.dumps({**config, **info}).encode(),
        )
        try:
            return status, json.loads(out)
        except ValueError:
            return 1, {**info, "error": f"Driver exited {status} without JSON: {err.decode(errors='replace')[-1000:]}"}
    except TimeoutError:
        return 1, {**info, "error": "Driver exceeded the 120 s deadline"}


@unittest.skipUnless(os.environ.get("NETIZEN_BROWSER_SMOKE") == "1", "set NETIZEN_BROWSER_SMOKE=1 to run")
class BrowserSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_pages(self):
        await self._admin_pages(owner_only=False)

    async def test_owner_admin_pages(self):
        await self._admin_pages(owner_only=True)

    async def test_manual_rename_without_generator(self):
        await self._admin_pages(owner_only=False, manual_only=True)

    async def _admin_pages(self, *, owner_only, manual_only=False):
        from netizen_cli.backends import BackendPolicy
        from netizen_cli.management import InstanceManagementService, ManagementRuntimePort, ScopeCoordinator
        from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions
        from tests.support.claude_client import FakeClaudeClients
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
        management = fixture.management
        policy = BackendPolicy("codex")
        if owner_only:
            from netizen_cli.domain import FeishuScope, ScopeKind
            lazy_project = fixture.project_root / "pending"
            lazy_project.mkdir()
            fixture.store.bootstrap_project(alias="pending", cwd=str(lazy_project))
            fixture.store.create_channel_binding(scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                                                project_alias="pending", creator_id="ou_admin")
            owner = users.restrict_to_owner()
            policy = BackendPolicy("claude", owner.open_id)
            local = ClaudeOrdinarySessions(bindings=fixture.store, owner_id=owner.open_id, clients=FakeClaudeClients(),
                                          on_completion=AsyncMock(), notice=AsyncMock())
            management = InstanceManagementService(bindings=fixture.store, projects=fixture.projects,
                scope_coordinator=ScopeCoordinator(), runtime=ManagementRuntimePort(None, ordinary=local, local=local))
            management.enable_defaults(app_id="cli_test", chat_info=fixture.channel)
            self.addAsyncCleanup(management.close)
        else:
            from netizen_cli.domain import NativeCapability
            fixture.runtime.available_capabilities = frozenset({NativeCapability.DELETE})
            fixture.runtime.project_side_snapshots = Mock(return_value=())
        if manual_only:
            from netizen_cli.session_providers import SessionNames
            management.names = SessionNames(writer=management.names.writer)
        runner.attach_management(management, group_access=GroupAccessService(), backend_policy=policy,
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
            "paths": ["/admins", "/sessions/ordinary", "/defaults", "/projects", "/maintenance"] if owner_only else
                     ["/sessions/ordinary"] if manual_only else sorted(_ADMIN_PAGE_PATHS),
            "ownerOnly": owner_only, "manualOnly": manual_only,
        })
        output = report(result)
        self.assertEqual(status, 0, output)
        self.assertNotIn("error", result)
        print(output)
