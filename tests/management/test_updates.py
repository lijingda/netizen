from __future__ import annotations

import asyncio
import copy
from email.message import Message
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from netizen.management.updates import (
    InstalledRelease, UpdateError, UpdateService, installed_release, parse_release,
    fetch_latest_release, LATEST_API, MAX_RELEASE_BYTES,
)
from netizen.deployment.update_executor import UpdateDispatchUnknown, UpdateExecutorError
from netizen.deployment.update_protocol import (
    acquire_install_lock, advance_operation, new_operation, read_operation, write_operation,
    new_restart_operation,
)


TARGET = {"version": "0.5.0", "releaseId": 125, "installerSha256": "a" * 64,
          "archiveSha256": "b" * 64}


def release_response() -> dict:
    return {"id": 125, "tag_name": "v0.5.0", "draft": False, "prerelease": False,
            "immutable": True, "body": "A <script> is displayed as text.", "assets": [
                {"name": name, "state": "uploaded", "digest": "sha256:" + digest,
                 "browser_download_url": "https://github.com/lijingda/netizen/releases/download/v0.5.0/" + name}
                for name, digest in (("install.sh", "a" * 64), ("netizen-v0.5.0.tar.gz", "b" * 64))]}


class ReleaseTests(unittest.TestCase):
    def test_release_binds_official_assets_and_preserves_plain_notes(self):
        latest = parse_release(release_response())
        self.assertEqual({key: latest[key] for key in TARGET}, TARGET)
        self.assertEqual(latest["notes"], "A <script> is displayed as text.")
        self.assertEqual(latest["url"], "https://github.com/lijingda/netizen/releases/tag/v0.5.0")

    def test_rejects_incomplete_mutable_or_unofficial_releases(self):
        mutations = [
            lambda r: r.update(draft=True), lambda r: r.update(prerelease=True),
            lambda r: r.update(immutable=False), lambda r: r.update(id=True),
            lambda r: r.update(tag_name="v0.5.0; touch /tmp/injected"),
            lambda r: r.update(tag_name="v0.5.0-rc.1"),
            lambda r: r["assets"].pop(),
            lambda r: r["assets"].append(copy.deepcopy(r["assets"][0])),
            lambda r: r["assets"][0].update(digest=None),
            lambda r: r["assets"][0].update(browser_download_url="https://evil.example/install.sh"),
            lambda r: r.update(body={"unexpected": "object"}),
        ]
        for mutate in mutations:
            response = release_response()
            mutate(response)
            with self.subTest(response=response), self.assertRaises((ValueError, RuntimeError)):
                parse_release(response)

    def test_running_release_identity_uses_interpreter_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            root = home / ".netizen/releases" / ("c" * 64)
            (root / "source").mkdir(parents=True)
            prefix = root / "venv"
            prefix.mkdir()
            manifest = {"schema": 1, "qualification": "github-release", "version": "0.4.6",
                        "commit": "d" * 40, "sourceDigest": "e" * 64, "requirementsDigest": "f" * 64}
            meta = {"gateSchema": 1, "qualification": "published", "digest": root.name,
                    "sourceDigest": manifest["sourceDigest"], "publishedRelease": {
                        "version": "0.4.6", "commit": manifest["commit"],
                        "requirementsDigest": manifest["requirementsDigest"]}}
            (root / "source/.netizen-release.json").write_text(json.dumps(manifest))
            (root / ".release.json").write_text(json.dumps(meta))
            with patch("netizen.management.updates.sys.prefix", str(prefix)), \
                    patch("netizen.management.updates.__file__", str(prefix / "lib/netizen/management/updates.py")), \
                    patch("netizen.management.updates.importlib.metadata.version", return_value="0.4.6"):
                self.assertEqual(installed_release(home), InstalledRelease("0.4.6", "published", root))
                meta["qualification"] = "source"
                (root / ".release.json").write_text(json.dumps(meta))
                self.assertEqual(installed_release(home).source, "source")
                meta["qualification"] = "published"
                meta["publishedRelease"]["commit"] = "0" * 40
                (root / ".release.json").write_text(json.dumps(meta))
                self.assertEqual(installed_release(home).source, "unmanaged")

                real_open = os.open
                def nonblocking_open(path, flags, *args, **kwargs):
                    # Fail the regression safely instead of opening a blocking FIFO.
                    self.assertTrue(flags & os.O_NONBLOCK)
                    return real_open(path, flags, *args, **kwargs)
                metadata_path = root / ".release.json"
                metadata_path.unlink()
                os.mkfifo(metadata_path, 0o600)
                with patch("netizen.management.updates.os.open", side_effect=nonblocking_open):
                    self.assertEqual(installed_release(home).source, "unmanaged")


class FakeExecutor:
    def __init__(self):
        self.launched = []
        self.cleaned = []
        self.active = True
        self.error = None
        self.cleanup_error = None
        self.on_launch = None

    def launch(self, operation_id, release_root):
        self.launched.append((operation_id, release_root))
        if self.on_launch:
            self.on_launch()
        if self.error:
            raise self.error

    def is_active(self, operation_id):
        if isinstance(self.active, Exception):
            raise self.active
        return self.active

    def cleanup(self, operation_id):
        self.cleaned.append(operation_id)
        if self.cleanup_error:
            raise self.cleanup_error


class UpdateServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / ".netizen"
        (self.root / "state").mkdir(parents=True, mode=0o700)
        self.release = self.root / "releases" / ("c" * 64)
        self.release.mkdir(parents=True)
        (self.root / "current").symlink_to(self.release)
        self.executor = FakeExecutor()
        self.fetches = 0
        self.now = 2_000_000_000
        self.service = UpdateService(home=self.home, current=InstalledRelease("0.4.6", "published", self.release),
                                     executor=self.executor, fetch=self.fetch,
                                     clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    def fetch(self):
        self.fetches += 1
        return parse_release(release_response())

    async def test_check_is_explicit_cached_and_start_uses_exact_target(self):
        self.assertFalse((await self.service.status())["available"])
        self.assertEqual(self.fetches, 0)
        self.assertTrue((await self.service.check())["available"])
        await self.service.check()
        self.assertEqual(self.fetches, 1)
        def assert_locked():
            with self.assertRaises(BlockingIOError):
                acquire_install_lock(self.root)
        self.executor.on_launch = assert_locked
        operation = await self.service.start(target=TARGET)
        self.assertEqual(operation["target"], TARGET)
        self.assertEqual(read_operation(self.root), operation)
        self.assertEqual(self.executor.launched, [(operation["operationId"], self.release)])
        self.now = operation["createdAt"]
        self.assertFalse((await self.service.status())["available"])
        with self.assertRaises(UpdateError) as error:
            await self.service.start(target=TARGET)
        self.assertEqual(error.exception.code, "update_already_submitted")
        self.assertEqual(len(self.executor.launched), 1)

    async def test_source_and_unmanaged_never_fetch_or_launch(self):
        for source in ("source", "unmanaged"):
            self.service._current = InstalledRelease("0.4.6", source, self.release)
            status = await self.service.check()
            self.assertFalse(status["supported"])
            self.assertEqual(status["current"]["source"], source)
            self.assertNotIn("message", status)
            with self.assertRaises(UpdateError) as error:
                await self.service.start(target=TARGET)
            self.assertEqual(error.exception.code, "update_unsupported")
        self.assertEqual(self.fetches, 0)
        self.assertEqual(self.executor.launched, [])

    async def test_restart_needs_no_release_check_and_supports_managed_source(self):
        for source in ("published", "source"):
            with self.subTest(source=source):
                self.service._current = InstalledRelease("0.4.6", source, self.release)
                self.assertTrue((await self.service.status())["restartAvailable"])
                operation = await self.service.restart(release_digest=self.release.name)
                self.assertEqual(operation["kind"], "restart")
                self.assertEqual(operation["target"], {
                    "version": "0.4.6", "releaseDigest": self.release.name,
                })
                self.assertEqual(read_operation(self.root), operation)
                advance_operation(self.root, operation["operationId"], "succeeded")
        self.assertEqual(self.fetches, 0)
        self.assertEqual(len(self.executor.launched), 2)

    async def test_restart_is_unavailable_for_unmanaged_or_changed_installation(self):
        for source, digest in (("unmanaged", self.release.name), ("published", "a" * 64)):
            self.service._current = InstalledRelease("0.4.6", source, self.release)
            with self.assertRaises(UpdateError):
                await self.service.restart(release_digest=digest)
        self.assertEqual(self.executor.launched, [])
        self.assertIsNone(read_operation(self.root))
        with patch.object(self.service, "_restart_supported", side_effect=[True, False]), \
                self.assertRaises(UpdateError) as error:
            await self.service.restart(release_digest=self.release.name)
        self.assertEqual(error.exception.code, "update_installation_changed")

    async def test_restart_and_upgrade_block_each_other_and_recovery(self):
        await self.service.check()
        for restart in (False, True):
            for phase in ("accepted", "restarting", "recovery_required"):
                with self.subTest(restart=restart, phase=phase):
                    operation = (new_restart_operation("0.4.6", self.release.name) if restart
                                 else new_operation(TARGET, self.release.name))
                    write_operation(self.root, operation)
                    advance_operation(self.root, operation["operationId"], phase)
                    self.now = operation["createdAt"]
                    descriptor = acquire_install_lock(self.root)
                    try:
                        status = await self.service.status()
                        self.assertFalse(status["available"])
                        self.assertFalse(status["restartAvailable"])
                    finally:
                        os.close(descriptor)
                    for submit in (lambda: self.service.start(target=TARGET),
                                   lambda: self.service.restart(release_digest=self.release.name)):
                        with self.assertRaises(UpdateError):
                            await submit()
        self.assertEqual(self.executor.launched, [])

    async def test_restart_respects_install_lock_and_interrupted_activation(self):
        descriptor = acquire_install_lock(self.root)
        try:
            with self.assertRaises(UpdateError) as error:
                await self.service.restart(release_digest=self.release.name)
            self.assertEqual(error.exception.code, "update_busy")
        finally:
            os.close(descriptor)
        # Even a dangling intent symlink must fail closed before dispatch.
        (self.root / "state/.activation-intent.json").symlink_to("missing")
        self.assertFalse((await self.service.status())["restartAvailable"])
        with self.assertRaises(UpdateError) as error:
            await self.service.restart(release_digest=self.release.name)
        self.assertEqual(error.exception.code, "update_recovery_required")
        self.assertIsNone(read_operation(self.root))
        self.assertEqual(self.executor.launched, [])

    async def test_ambiguous_restart_and_lost_worker_never_resubmit(self):
        self.executor.error = UpdateDispatchUnknown("SECRET")
        operation = await self.service.restart(release_digest=self.release.name)
        self.assertEqual(operation["phase"], "accepted")
        self.now = operation["createdAt"]
        self.assertFalse((await self.service.status())["restartAvailable"])
        with self.assertRaises(UpdateError):
            await self.service.restart(release_digest=self.release.name)
        self.executor.active = False
        status = await self.service.status()
        self.assertEqual(status["operation"]["phase"], "recovery_required")
        self.assertEqual(status["operation"]["kind"], "restart")
        self.assertFalse(status["restartAvailable"])
        self.assertEqual(len(self.executor.launched), 1)

    async def test_target_tampering_and_nonforward_versions_fail_before_dispatch(self):
        await self.service.check()
        for value in ({**TARGET, "installerSha256": "0" * 64},
                      {**TARGET, "url": "https://evil.example"},
                      {**TARGET, "version": "0.4.6"}):
            with self.assertRaises(UpdateError):
                await self.service.start(target=value)
        self.assertEqual(self.executor.launched, [])
        self.assertIsNone(read_operation(self.root))

    async def test_compare_versions_numerically(self):
        self.service._current = InstalledRelease("0.10.0", "published", self.release)
        self.assertFalse((await self.service.check())["available"])

    async def test_candidate_is_pinned_even_if_upstream_changes_after_check(self):
        await self.service.check()
        self.service._fetch = lambda: {**TARGET, "version": "0.6.0"}
        operation = await self.service.start(target=TARGET)
        self.assertEqual(operation["target"]["version"], "0.5.0")
        self.assertEqual(self.fetches, 1)

    async def test_cli_lock_conflict_never_creates_a_second_operation(self):
        await self.service.check()
        descriptor = acquire_install_lock(self.root)
        try:
            with self.assertRaises(UpdateError) as error:
                await self.service.start(target=TARGET)
            self.assertEqual(error.exception.code, "update_busy")
            self.assertIsNone(read_operation(self.root))
        finally:
            os.close(descriptor)

    async def test_changed_current_is_rejected(self):
        await self.service.check()
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(self.root / "releases" / ("d" * 64))
        with self.assertRaises(UpdateError):
            await self.service.start(target=TARGET)
        self.assertEqual(self.executor.launched, [])

    async def test_changed_current_after_lock_is_a_distinct_failure(self):
        await self.service.check()
        with patch.object(self.service, "_supported", side_effect=[True, False]), \
                self.assertRaises(UpdateError) as error:
            await self.service.start(target=TARGET)
        self.assertEqual(error.exception.code, "update_installation_changed")
        self.assertIsNone(read_operation(self.root))
        self.assertEqual(self.executor.launched, [])

    async def test_lock_failure_and_submission_unknown_have_distinct_codes(self):
        await self.service.check()
        with patch("netizen.management.updates.acquire_install_lock", side_effect=OSError("SECRET lock")), \
                self.assertRaises(UpdateError) as error:
            await self.service.start(target=TARGET)
        self.assertEqual(error.exception.code, "update_lock_unavailable")
        with patch("netizen.management.updates.write_operation", side_effect=OSError("SECRET state")), \
                self.assertRaises(UpdateError) as error:
            await self.service.start(target=TARGET)
        self.assertEqual(error.exception.code, "update_submission_unknown")
        self.assertIsNone(read_operation(self.root))
        self.assertEqual(self.executor.launched, [])

    async def test_ambiguous_dispatch_retains_exact_attempt_without_retry(self):
        await self.service.check()
        self.executor.error = UpdateDispatchUnknown("ambiguous")
        operation = await self.service.start(target=TARGET)
        self.assertEqual(operation["phase"], "accepted")
        with self.assertRaises(UpdateError):
            await self.service.start(target=TARGET)
        self.assertEqual(len(self.executor.launched), 1)

    async def test_definite_dispatch_failure_is_durable_and_retryable(self):
        await self.service.check()
        self.executor.error = UpdateExecutorError("SECRET must not enter result")
        operation = await self.service.start(target=TARGET)
        self.assertEqual(operation["phase"], "failed")
        self.assertNotIn("SECRET", json.dumps(operation))
        self.executor.error = None
        retry = await self.service.start(target=TARGET)
        self.assertNotEqual(retry["operationId"], operation["operationId"])

    async def test_dead_worker_is_unknown_not_success_even_when_new_service_is_ready(self):
        operation = new_operation(TARGET, self.release.name)
        write_operation(self.root, operation)
        advance_operation(self.root, operation["operationId"], "restarting")
        (self.root / "state/service.ready").write_text("netizen service ready\n")
        status = await self.service.status()
        self.assertEqual(status["operation"]["phase"], "recovery_required")
        self.assertEqual(status["operation"]["code"], "worker_lost")

    async def test_active_worker_state_is_not_inferred_from_a_status_timeout(self):
        operation = new_operation(TARGET, self.release.name)
        write_operation(self.root, operation)
        self.executor.active = UpdateExecutorError("temporary observation timeout")
        with self.assertRaises(UpdateError) as error:
            await self.service.status()
        self.assertEqual(error.exception.code, "update_state_unavailable")
        self.assertEqual(read_operation(self.root)["phase"], "accepted")
        self.assertEqual(self.executor.cleaned, [])

    async def test_worker_holding_install_lock_is_not_reconciled_or_cleaned(self):
        operation = new_operation(TARGET, self.release.name)
        write_operation(self.root, operation)
        advance_operation(self.root, operation["operationId"], "succeeded")
        descriptor = acquire_install_lock(self.root)
        try:
            self.assertEqual((await self.service.status())["operation"]["phase"], "succeeded")
            self.assertEqual(self.executor.cleaned, [])
        finally:
            os.close(descriptor)
        self.executor.cleanup_error = UpdateExecutorError("cleanup unavailable")
        self.assertEqual((await self.service.status())["operation"]["phase"], "succeeded")

    async def test_requires_action_allows_explicit_retry(self):
        await self.service.check()
        previous = new_operation(TARGET, self.release.name)
        write_operation(self.root, previous)
        advance_operation(self.root, previous["operationId"], "requires_action", "permissions_required")
        self.assertTrue((await self.service.status())["available"])
        operation = await self.service.start(target=TARGET)
        self.assertNotEqual(operation["operationId"], previous["operationId"])

    async def test_cleanup_failure_preserves_previous_result_without_dispatch(self):
        await self.service.check()
        previous = new_operation(TARGET, self.release.name)
        write_operation(self.root, previous)
        previous = advance_operation(self.root, previous["operationId"], "failed", "dispatch_failed")
        self.executor.cleanup_error = UpdateExecutorError("SECRET manager detail")
        with self.assertRaises(UpdateError) as error:
            await self.service.start(target=TARGET)
        self.assertEqual(error.exception.code, "update_cleanup_unavailable")
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(read_operation(self.root), previous)
        self.assertEqual(self.executor.launched, [])

    async def test_cli_recovered_operation_allows_a_new_explicit_update(self):
        await self.service.check()
        previous = new_operation(TARGET, self.release.name)
        write_operation(self.root, previous)
        advance_operation(self.root, previous["operationId"], "recovered", "manual_recovery")
        self.assertTrue((await self.service.status())["available"])
        self.assertEqual((await self.service.status())["operation"]["phase"], "recovered")
        operation = await self.service.start(target=TARGET)
        self.assertNotEqual(operation["operationId"], previous["operationId"])

    async def test_optional_updater_does_not_probe_manager_during_service_construction(self):
        with patch("netizen.management.updates.UpdateExecutor", side_effect=UpdateExecutorError("unavailable")):
            service = UpdateService(home=self.home, current=InstalledRelease("0.4.6", "unmanaged"))
            try:
                self.assertFalse((await service.status())["supported"])
            finally:
                await service.close()

    async def test_new_admin_process_reads_prior_installer_result(self):
        operation = new_operation(TARGET, self.release.name)
        write_operation(self.root, operation)
        advance_operation(self.root, operation["operationId"], "rolled_back", "activation_failed")
        other = UpdateService(home=self.home, current=self.service._current, executor=self.executor)
        try:
            self.assertEqual((await other.status())["operation"]["phase"], "rolled_back")
        finally:
            await other.close()

    async def test_network_failure_is_visible_and_does_not_modify_installation(self):
        def fail():
            raise OSError("SECRET remote text")
        self.service._fetch = fail
        status = await self.service.check()
        self.assertFalse(status["available"])
        self.assertEqual(status["checkingErrorCode"], "release_network_error")
        self.assertNotIn("checkingError", status)
        self.assertNotIn("SECRET", json.dumps(status))
        self.assertIsNone(read_operation(self.root))


    async def test_release_failures_are_classified_cached_and_recover_without_dispatch(self):
        def http_error(code, headers=None, body=b'{"message":"SECRET remote text"}'):
            message = Message()
            for key, value in (headers or {}).items():
                message[key] = value
            return HTTPError(LATEST_API, code, "SECRET reason", message, io.BytesIO(body))

        cases = [
            (http_error(403, {"X-RateLimit-Remaining": "0"}), "release_rate_limited"),
            (http_error(403, {"Retry-After": "120"}), "release_rate_limited"),
            (http_error(403, {"X-RateLimit-Remaining": "5"},
                        b'{"message":"You have exceeded a secondary rate limit. SECRET"}'),
             "release_rate_limited"),
            (http_error(429), "release_rate_limited"),
            (http_error(403, {"X-RateLimit-Remaining": "5"}), "release_access_denied"),
            (http_error(403, body=b"<html>SECRET forbidden</html>"), "release_access_denied"),
            (http_error(403, body=b'{"message":"' + b"x" * 8192 + b'rate limit"}'),
             "release_access_denied"),
            (http_error(404), "release_not_found"),
            (http_error(502), "release_service_unavailable"),
            (http_error(401), "release_http_error"),
            (URLError("SECRET network"), "release_network_error"),
            (TimeoutError("SECRET timeout"), "release_check_timeout"),
            (URLError(TimeoutError("SECRET connect timeout")), "release_check_timeout"),
            (IncompleteRead(b"SECRET truncated"), "release_network_error"),
        ]
        for failure, expected in cases:
            with self.subTest(failure=failure, expected=expected):
                self.service._fetch = self.fetch
                self.assertTrue((await self.service.check())["available"])
                self.now += 60
                self.service._fetch = fetch_latest_release
                with patch("netizen.management.updates.urllib.request.urlopen",
                           side_effect=failure) as opener:
                    status = await self.service.check()
                    self.assertEqual(status["checkingErrorCode"], expected)
                    self.assertIsNone(status["latest"])
                    self.assertFalse(status["available"])
                    self.assertTrue(status["restartAvailable"])
                    self.assertNotIn("SECRET", json.dumps(status))
                    self.assertEqual(await self.service.status(), status)
                    self.now += 59
                    self.assertEqual(await self.service.check(), status)
                    self.assertEqual(opener.call_count, 1)
                    with self.assertRaises(UpdateError) as rejected:
                        await self.service.start(target=TARGET)
                    self.assertEqual(rejected.exception.code, "update_target_changed")
                if isinstance(failure, HTTPError):
                    self.assertTrue(failure.closed)
                self.service._fetch = self.fetch
                self.now += 1
                recovered = await self.service.check()
                self.assertTrue(recovered["available"])
                self.assertIsNone(recovered["checkingErrorCode"])
        self.assertEqual(self.executor.launched, [])
        self.assertIsNone(read_operation(self.root))

    async def test_invalid_release_responses_are_distinct_from_network_failures(self):
        incomplete = release_response()
        incomplete["assets"].pop()
        invalid_target = release_response()
        invalid_target["id"] = True
        for payload, url in (
            (b"SECRET invalid JSON", LATEST_API),
            (b"x" * (MAX_RELEASE_BYTES + 1), LATEST_API),
            (json.dumps(incomplete).encode(), LATEST_API),
            (json.dumps(invalid_target).encode(), LATEST_API),
            (json.dumps(release_response()).encode(), "https://example.com/SECRET"),
        ):
            with self.subTest(url=url, size=len(payload)):
                response = MagicMock()
                response.__enter__.return_value = response
                response.geturl.return_value = url
                response.read.return_value = payload
                self.service._fetch = fetch_latest_release
                self.now += 60
                with patch("netizen.management.updates.urllib.request.urlopen", return_value=response):
                    status = await self.service.check()
                self.assertEqual(status["checkingErrorCode"], "release_invalid_response")
                self.assertFalse(status["available"])
                self.assertIsNone(status["latest"])
                self.assertNotIn("SECRET", json.dumps(status))
                self.assertTrue(response.__exit__.called)


if __name__ == "__main__":
    unittest.main()
