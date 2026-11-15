#!/usr/bin/env python3
"""Verify provider continuity, cold rejection and fork migration through CodexRuntime.

Uses two independent mock providers, temporary HOME/CODEX_HOME/cwd and the
real bundled App Server; warm and cold forks must use the current default and
carry source assistant history in the model request. No account or Feishu
traffic. Run with an external process deadline, for example:
``timeout --signal=INT --kill-after=5s 120s .venv/bin/python
scripts/probe_model_provider_admission.py --output /tmp/provider-admission.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from openai_codex import AsyncCodex, AsyncThread, CodexConfig

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen_cli.bindings import BindingStore  # noqa: E402
from netizen_cli.codex_runtime import CodexRuntime, ModelProviderRejected  # noqa: E402
from netizen_cli.domain import FeishuScope, ScopeKind  # noqa: E402
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from scripts.probe_sdk_partial_answers import (  # noqa: E402
    isolated_config, message_events, mock_provider, response_events,
)


def _user_config_fingerprints() -> dict[Path, str | None]:
    homes = {Path.home() / ".codex", Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))}
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            for home in homes for path in (home / "config.toml",)}


def _reply(name: str) -> tuple[int, bytes]:
    return 200, response_events(name, *message_events(
        name + "-message", name.upper(), "final_answer",
    ))


async def _scenario(root: Path) -> dict[str, Any]:
    with (
        mock_provider([_reply(name) for name in (
            "seed-a", "after-switch-a", "restored-a",
        )]) as provider_a,
        mock_provider([_reply(name) for name in (
            "fresh-b", "warm-fork-b", "cold-fork-b",
        )]) as provider_b,
    ):
        cwd, env = isolated_config(root, provider_a)
        config_path = Path(env["CODEX_HOME"]) / "config.toml"
        original = config_path.read_text(encoding="utf-8") + (
            '\n[model_providers.second_probe]\nname = "Second disposable provider"\n'
            f'base_url = "http://127.0.0.1:{provider_b.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            'request_max_retries = 0\nstream_max_retries = 0\n'
        )
        config_path.write_text(original, encoding="utf-8")
        switched = original.replace(
            'model_provider = "native_probe"', 'model_provider = "second_probe"', 1,
        )
        start = switched.index("[model_providers.native_probe]")
        end = switched.index("[", start + 1)
        removed = switched[:start] + switched[end:]
        assert "[model_providers.native_probe]" not in removed
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="provider-probe", cwd=str(cwd))
        outcomes: list[Any] = []
        fork_history_evidence: dict[str, Any] = {}

        async def capture(outcome: Any) -> None:
            outcomes.append(outcome)

        def binding(name: str):
            return store.create_channel_binding(
                scope=FeishuScope("probe-app", name, ScopeKind.DIRECT),
                project_alias="provider-probe", creator_id="probe-owner",
            )

        def make_runtime(codex: AsyncCodex) -> CodexRuntime:
            return CodexRuntime(
                codex=codex, bindings=store,
                terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                on_completion=capture, poll_interval_seconds=0.01,
                automatic_thread_naming=False,
            )

        def counts() -> tuple[int, int]:
            return len(provider_a.requests), len(provider_b.requests)

        async def complete(runtime: CodexRuntime, target, expected: str):
            previous_count = len(outcomes)
            submission = await runtime.submit(
                binding=store.get(target.id), cwd=cwd, input="Return the scripted reply.",
                owner_id="probe-owner", origin=None,
            )
            assert submission.release_receipt_attempt is not None
            submission.release_receipt_attempt()
            assert await runtime.wait_idle(timeout=15), "Turn did not finish"
            assert len(outcomes) == previous_count + 1
            outcome = outcomes[-1]
            assert outcome.error is None, type(outcome.error).__name__
            assert outcome.turn_id == submission.turn_id
            assert outcome.result.status.value == "completed", outcome.result.status
            assert outcome.result.final_response == expected, outcome.result.final_response
            return submission

        async def fork_and_complete(runtime: CodexRuntime, codex: AsyncCodex, name: str):
            source = store.get(first.id)
            project = store.get_project(source.project_alias)
            requests_before = counts()
            async with runtime.track_fork_creation(project.alias):
                thread = await runtime.fork_exact(source, expected_project_revision=project.revision)
                created = store.create_fork_binding(
                    scope=FeishuScope("probe-app", name, ScopeKind.DIRECT),
                    source=source, native_thread_id=thread.id, root_message_id=name + "-root",
                    creator_id="probe-owner", expected_project_revision=project.revision,
                    target_chat_kind="p2p",
                )
                assert await runtime.adopt_fork(created.binding, thread)
            assert counts() == requests_before, counts()
            submission = await complete(runtime, created.binding, name.upper())
            assert submission.thread_id == thread.id
            assert thread.id != source.native_thread_id
            assert counts() == (requests_before[0], requests_before[1] + 1), counts()
            fork_native = await AsyncThread(codex, thread.id).read()
            source_native = await AsyncThread(codex, source.native_thread_id).read()
            assert fork_native.thread.model_provider == "second_probe"
            assert source_native.thread.model_provider == "native_probe"
            request = provider_b.requests[requests_before[1]]["body"]
            assistant_texts = [
                part["text"]
                for item in request["input"] if item.get("role") == "assistant"
                for part in item.get("content", []) if part.get("type") == "output_text"
            ]
            expected_history = ["SEED-A", "AFTER-SWITCH-A"]
            assert all(text in assistant_texts for text in expected_history), assistant_texts
            fork_history_evidence[name] = {
                "source_provider": source_native.thread.model_provider,
                "fork_provider": fork_native.thread.model_provider,
                "distinct_thread_id": thread.id != source.native_thread_id,
                "provider_requests_before": requests_before,
                "provider_requests_after": counts(),
                "history_location": "B request body input: assistant content output_text",
                "matched_source_replies": expected_history,
            }

        try:
            first = binding("binding-a")
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                runtime = make_runtime(codex)
                try:
                    seed = await complete(runtime, first, "SEED-A")
                    assert counts() == (1, 0), counts()
                    config_path.write_text(switched, encoding="utf-8")
                    continued = await complete(runtime, first, "AFTER-SWITCH-A")
                    assert continued.thread_id == seed.thread_id
                    assert counts() == (2, 0), counts()
                    assert runtime._accepting
                    fresh = await complete(runtime, binding("binding-b"), "FRESH-B")
                    assert fresh.thread_id != seed.thread_id
                    assert counts() == (2, 1), counts()
                    native = await AsyncThread(codex, seed.thread_id).read()
                    assert native.thread.model_provider == "native_probe"
                    # Adopt the materialized cwd before comparing Binding snapshots.
                    await runtime.native_thread_cwd(first.id)
                    await fork_and_complete(runtime, codex, "warm-fork-b")
                    assert counts() == (2, 2), counts()
                finally:
                    runtime.close_admission()
                    await runtime.cancel_tasks()

            config_path.write_text(removed, encoding="utf-8")
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                runtime = make_runtime(codex)
                try:
                    before = store.get(first.id)
                    requests_before = counts()
                    try:
                        await runtime.submit(
                            binding=before, cwd=cwd, input="This must not reach any model.",
                            owner_id="probe-owner", origin=None,
                        )
                    except ModelProviderRejected as error:
                        assert "native_probe" in str(error)
                        # The retired mismatch variant would also name the new default.
                        assert "second_probe" not in str(error)
                        assert "未发送给模型" in str(error)
                    else:
                        raise AssertionError("missing saved provider was not rejected")
                    assert counts() == requests_before
                    assert runtime._accepting
                    assert first.id not in runtime._active
                    assert store.get(first.id) == before
                    assert len(outcomes) == 4
                    await fork_and_complete(runtime, codex, "cold-fork-b")
                    assert counts() == (2, 3), counts()
                    config_path.write_text(switched, encoding="utf-8")
                    restored = await complete(runtime, first, "RESTORED-A")
                    assert restored.thread_id == seed.thread_id
                    assert counts() == (3, 3), counts()
                    assert not provider_a.errors and not provider_a.replies
                    assert not provider_b.errors and not provider_b.replies
                finally:
                    runtime.close_admission()
                    await runtime.cancel_tasks()
        finally:
            store.close()
    return {
        "passed": True, "sdk_version": importlib.metadata.version("openai-codex"),
        "cli_version": importlib.metadata.version("openai-codex-cli-bin"),
        "transport": "bundled App Server + two independent local mock Responses HTTP SSE providers",
        "real_model_used": False, "feishu_delivery_tested": False,
        "provider_request_counts": {"native_probe": 3, "second_probe": 3},
        "old_binding_kept_saved_provider": True, "new_binding_used_new_default": True,
        "thread_read_saved_provider": "native_probe",
        "cold_resume_missing_provider_rejected": True, "rejected_model_request_count": 0,
        "admission_preserved": True, "binding_preserved": True, "rejected_binding_inactive": True,
        "restored_definition_same_thread_continued": True,
        "warm_fork_used_current_default": True,
        "cold_fork_after_rejection_used_current_default": True,
        "fork_carried_source_history": True, "fork_history_evidence": fork_history_evidence,
        "fork_binding_store_api": "create_fork_binding",
    }


async def probe(timeout: float) -> dict[str, Any]:
    before = _user_config_fingerprints()
    try:
        with tempfile.TemporaryDirectory(prefix="netizen-provider-admission-") as directory:
            async with asyncio.timeout(timeout):
                result = await _scenario(Path(directory))
    finally:
        assert _user_config_fingerprints() == before, "user Codex configuration changed"
    return {**result, "user_config_unchanged": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=100, help="Overall deadline in seconds")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        result = asyncio.run(probe(args.timeout))
    except Exception as error:
        result = {"passed": False, "error_type": type(error).__name__}
        if args.output is not None:
            args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        raise
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
