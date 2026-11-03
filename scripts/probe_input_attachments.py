#!/usr/bin/env python3
"""Verify native reads of instance attachments outside the project directory.

Uses the production AttachmentStore, bundled App Server and a loopback mock
Responses provider. All files, HOME and CODEX_HOME state are disposable; no
real model account, existing Thread, user configuration or Feishu call is used.
Run with an external deadline, for example:
``timeout --signal=INT --kill-after=5s 120s .venv/bin/python
scripts/probe_input_attachments.py --output /tmp/input-attachments.json``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any

import openai_codex
from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen_cli.attachments import AttachmentInput, AttachmentStore  # noqa: E402
from scripts.probe_scheduled_tasks import (  # noqa: E402
    _safe_traceback, source_evidence, write_result,
)
from scripts.probe_sdk_partial_answers import (  # noqa: E402
    isolated_config, message_events, mock_provider, response_events,
)


_MARKER = "ATTACHMENT-OUTSIDE-PROJECT-READ-OK"


def fixture_responses(marker: Path) -> list[tuple[int, bytes]]:
    replies = []
    for index in range(3):
        command = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": f"attachment-read-{index}",
                "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": f"cat {shlex.quote(str(marker))}",
                    "login": False,
                    "max_output_tokens": 100,
                }),
            },
        }
        replies.extend([
            (200, response_events(f"read-{index}", command)),
            (200, response_events(
                f"final-{index}",
                *message_events(f"answer-{index}", "FIXTURE-OK", "final_answer"),
            )),
        ])
    return replies


async def run_read(thread: AsyncThread, prompt: str) -> None:
    result = await (await thread.turn(prompt)).run()
    assert result.status.value == "completed"
    assert result.final_response == "FIXTURE-OK"


async def native_case(mode: Sandbox, root: Path) -> dict[str, Any]:
    instance = root / "instance"
    instance.mkdir()
    store = AttachmentStore(instance / "attachments")
    batch = await store.prepare_files((AttachmentInput("marker.txt", _MARKER.encode()),))
    await store.publish_files(batch)
    marker = batch.files[0].path
    assert marker.is_absolute() and marker.read_text(encoding="utf-8") == _MARKER
    with mock_provider(fixture_responses(marker)) as provider:
        cwd, environment = isolated_config(root, provider)
        assert cwd not in marker.parents
        config = CodexConfig(cwd=str(cwd), env=environment)
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(cwd), sandbox=mode, approval_mode=ApprovalMode.deny_all,
            )
            await run_read(thread, "Run the scripted local attachment-read fixture.")
            thread_id = thread.id
        async with AsyncCodex(config) as codex:
            resumed = await codex.thread_resume(thread_id, include_turns=False)
            assert resumed.id == thread_id
            await run_read(resumed, "Run the same read after cold resume.")
            forked = await codex.thread_fork(
                thread_id, ephemeral=False, include_turns=False,
            )
            assert forked.id != thread_id
            await run_read(forked, "Run the same read in the persistent fork.")
        assert not provider.errors and not provider.replies
        assert len(provider.requests) == 6
        for index in range(3):
            followup = provider.requests[index * 2 + 1]["body"]["input"]
            outputs = [
                item for item in followup
                if item.get("type") == "function_call_output"
                and item.get("call_id") == f"attachment-read-{index}"
            ]
            # The final-answer fixture does not prove a read. Check the actual
            # native tool output submitted to the next mock-model request.
            assert outputs and _MARKER in str(outputs[-1].get("output"))
        return {
            "sandbox": mode.value,
            "production_attachment_store": True,
            "fresh_thread_read": True,
            "cold_resume_read_without_overrides": True,
            "persistent_fork_read_without_overrides": True,
            "attachment_outside_cwd": True,
            "native_exec_verified": True,
            "mock_model_requests": len(provider.requests),
        }


async def probe(*, timeout: float) -> dict[str, Any]:
    cases = []
    async with asyncio.timeout(timeout):
        for mode in (Sandbox.read_only, Sandbox.workspace_write):
            with tempfile.TemporaryDirectory(prefix="netizen-input-attachments-probe-") as temporary:
                cases.append(await native_case(mode, Path(temporary)))
    return {
        "passed": True,
        "sdk": openai_codex.__version__,
        "transport": "bundled App Server + loopback mock Responses",
        "cases": cases,
        "real_model_requests": 0,
        "feishu_requests": 0,
        "temporary_state_cleaned": True,
        "custom_read_restrictions_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="save sanitized result and checkout identity as JSON")
    parser.add_argument("--timeout", type=float, default=90, help="total probe deadline in seconds")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 300:
        parser.error("--timeout must be between 10 and 300 seconds")
    logging.disable(logging.CRITICAL)
    evidence = source_evidence()
    try:
        result = asyncio.run(probe(timeout=args.timeout))
    except Exception as error:
        write_result({
            **evidence,
            "passed": False,
            "error_type": type(error).__name__,
            "traceback": _safe_traceback(error),
        }, args.output)
        return 1
    write_result({**evidence, **result}, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
