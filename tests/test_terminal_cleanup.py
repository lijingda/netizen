from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openai_codex
from openai_codex import AsyncCodex, CodexConfig

from netizen import terminal_cleanup
from netizen.terminal_cleanup import (
    PinnedExperimentalTerminalCleanup,
    UnsupportedCleanupSdk,
)


_FAKE_SERVER = r"""
import json
import sys

log_path = sys.argv[1]
for line in sys.stdin:
    message = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, sort_keys=True) + "\n")
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "userAgent": "netizen-cleanup-contract/1",
            "serverInfo": {"name": "fake", "version": "1"},
        }
        response = {"id": request_id, "result": result}
    elif method == "thread/backgroundTerminals/clean":
        response = {"id": request_id, "result": {}}
    elif method == "thread/backgroundTerminals/list":
        thread_id = message.get("params", {}).get("threadId")
        result = {"data": []}
        if thread_id == "thread-running":
            result["data"] = [{"processId": "process-1"}]
        elif thread_id == "thread-cursor":
            result["nextCursor"] = "unexpected-more"
        response = {"id": request_id, "result": result}
    else:
        response = {
            "id": request_id,
            "error": {"code": -32601, "message": "unexpected method"},
        }
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
"""


def _config(log_path: Path, *, experimental_api: bool = True) -> CodexConfig:
    return CodexConfig(
        launch_args_override=(
            sys.executable,
            "-u",
            "-c",
            _FAKE_SERVER,
            str(log_path),
        ),
        experimental_api=experimental_api,
    )


def _messages(log_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _close_probe_pipes(process: object) -> None:
    # Pinned SDK 0.147.0 terminates its probe subprocess but leaves the local
    # stdout/stderr wrappers for GC. Close those test-only handles explicitly
    # so ResourceWarning does not obscure the contract result.
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is not None:
            stream.close()


class ExperimentalTerminalCleanupContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_capability_method_and_params_are_sent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path)) as codex:
                process = codex._client._sync._proc
                cleanup = PinnedExperimentalTerminalCleanup(codex)
                self.assertFalse(hasattr(cleanup, "request"))
                await cleanup.clean_thread("thread-exact")
                self.assertFalse(await cleanup.has_running("thread-empty"))
                self.assertTrue(await cleanup.has_running("thread-running"))
                self.assertTrue(await cleanup.has_running("thread-cursor"))

            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        initialize = next(item for item in messages if item.get("method") == "initialize")
        self.assertEqual(
            initialize["params"]["capabilities"],  # type: ignore[index]
            {"experimentalApi": True},
        )
        cleanup_request = next(
            item
            for item in messages
            if item.get("method") == "thread/backgroundTerminals/clean"
        )
        self.assertEqual(cleanup_request["params"], {"threadId": "thread-exact"})
        list_requests = [
            item
            for item in messages
            if item.get("method") == "thread/backgroundTerminals/list"
        ]
        self.assertEqual(
            [item["params"] for item in list_requests],
            [
                {"threadId": "thread-empty", "limit": 1},
                {"threadId": "thread-running", "limit": 1},
                {"threadId": "thread-cursor", "limit": 1},
            ],
        )
        request_methods = [item.get("method") for item in messages if "id" in item]
        self.assertEqual(
            request_methods,
            [
                "initialize",
                "thread/backgroundTerminals/clean",
                "thread/backgroundTerminals/list",
                "thread/backgroundTerminals/list",
                "thread/backgroundTerminals/list",
            ],
        )

    async def test_disabled_experimental_capability_fails_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, experimental_api=False)) as codex:
                process = codex._client._sync._proc
                with self.assertRaisesRegex(
                    UnsupportedCleanupSdk,
                    "experimentalApi capability is not enabled",
                ):
                    PinnedExperimentalTerminalCleanup(codex)

            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        self.assertEqual(
            [item.get("method") for item in messages if "id" in item],
            ["initialize"],
        )
        self.assertEqual(
            messages[0]["params"]["capabilities"],  # type: ignore[index]
            {"experimentalApi": False},
        )

    async def test_version_and_source_fingerprint_mismatches_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path)) as codex:
                process = codex._client._sync._proc
                with patch.object(openai_codex, "__version__", "0.147.1"):
                    with self.assertRaisesRegex(
                        UnsupportedCleanupSdk,
                        "supports only openai-codex==0.147.0",
                    ):
                        PinnedExperimentalTerminalCleanup(codex)

                with patch.object(
                    terminal_cleanup,
                    "_PACKAGE_SOURCE_FINGERPRINT",
                    "0" * 64,
                ):
                    with self.assertRaisesRegex(
                        UnsupportedCleanupSdk,
                        "package source fingerprint changed",
                    ):
                        PinnedExperimentalTerminalCleanup(codex)

            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        self.assertEqual(
            [item.get("method") for item in messages if "id" in item],
            ["initialize"],
        )


if __name__ == "__main__":
    unittest.main()
