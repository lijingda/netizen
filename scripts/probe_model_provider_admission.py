#!/usr/bin/env python3
"""Verify provider rejection through CodexRuntime with an isolated mock provider.

Uses temporary HOME/CODEX_HOME/cwd and the real bundled App Server; no account
or Feishu traffic. Run with an external process deadline, for example:
``timeout --signal=INT --kill-after=5s 90s .venv/bin/python
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

from openai_codex import AsyncCodex, CodexConfig

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


async def _scenario(root: Path) -> dict[str, Any]:
    replies = [(200, response_events(name, *message_events(
        name + "-message", name.upper(), "final_answer",
    ))) for name in ("seed-a", "seed-b", "new-provider", "original-provider")]
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        config_path = Path(env["CODEX_HOME"]) / "config.toml"
        original = config_path.read_text(encoding="utf-8") + (
            '\n[model_providers.second_probe]\nname = "Second disposable provider"\n'
            f'base_url = "http://127.0.0.1:{provider.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            'request_max_retries = 0\nstream_max_retries = 0\n'
        )
        config_path.write_text(original, encoding="utf-8")
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="provider-probe", cwd=str(cwd))
        outcomes: list[Any] = []

        async def capture(outcome: Any) -> None:
            outcomes.append(outcome)

        def binding(name: str):
            return store.create_channel_binding(
                scope=FeishuScope("probe-app", name, ScopeKind.DIRECT),
                project_alias="provider-probe", creator_id="probe-owner",
            )

        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                runtime = CodexRuntime(
                    codex=codex, bindings=store,
                    terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                    on_completion=capture, poll_interval_seconds=0.01,
                    automatic_thread_naming=False,
                )

                async def complete(target, expected: str):
                    submission = await runtime.submit(
                        binding=store.get(target.id), cwd=cwd, input="Return the scripted reply.",
                        owner_id="probe-owner", origin=None,
                    )
                    assert submission.release_receipt_attempt is not None
                    submission.release_receipt_attempt()
                    assert await runtime.wait_idle(timeout=15), "Turn did not finish"
                    outcome = outcomes[-1]
                    assert outcome.error is None, type(outcome.error).__name__
                    assert outcome.turn_id == submission.turn_id
                    assert outcome.result.status.value == "completed", outcome.result.status
                    assert outcome.result.final_response == expected, outcome.result.final_response
                    return submission

                try:
                    first, second = binding("binding-a"), binding("binding-b")
                    seed_a = await complete(first, "SEED-A")
                    await complete(second, "SEED-B")
                    # Resolve cwd once so the rejected operation cannot be
                    # confused with the independent materialized cwd adoption.
                    await runtime.native_thread_cwd(first.id)
                    before = store.get(first.id)
                    request_count = len(provider.requests)
                    assert request_count == 2
                    config_path.write_text(original.replace(
                        'model_provider = "native_probe"', 'model_provider = "second_probe"', 1,
                    ), encoding="utf-8")
                    try:
                        await runtime.submit(
                            binding=before, cwd=cwd, input="This must not reach the model.",
                            owner_id="probe-owner", origin=None,
                        )
                    except ModelProviderRejected as error:
                        assert "native_probe" in str(error) and "second_probe" in str(error)
                        assert "未发送给模型" in str(error)
                    else:
                        raise AssertionError("provider switch was not rejected")
                    assert runtime._accepting
                    assert first.id not in runtime._active
                    assert store.get(first.id) == before
                    assert len(provider.requests) == request_count
                    fresh = await complete(binding("binding-c"), "NEW-PROVIDER")
                    assert fresh.thread_id != seed_a.thread_id
                    config_path.write_text(original, encoding="utf-8")
                    continued = await complete(first, "ORIGINAL-PROVIDER")
                    assert continued.thread_id == seed_a.thread_id
                    assert len(provider.requests) == 4 and len(outcomes) == 4
                    assert not provider.errors and not provider.replies
                finally:
                    runtime.close_admission()
                    await runtime.cancel_tasks()
        finally:
            store.close()
    return {
        "passed": True, "sdk_version": importlib.metadata.version("openai-codex"),
        "cli_version": importlib.metadata.version("openai-codex-cli-bin"),
        "transport": "bundled App Server + local mock Responses HTTP SSE",
        "real_model_used": False, "feishu_delivery_tested": False,
        "model_request_count": 4, "rejected_model_request_count": 0,
        "admission_preserved": True, "binding_preserved": True,
        "new_binding_completed": True, "original_thread_continued": True,
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
    parser.add_argument("--timeout", type=float, default=60, help="Overall deadline in seconds")
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
