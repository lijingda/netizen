#!/usr/bin/env python3
"""Read account quota once without a Thread, model call, or identity output.

Default: disposable unauthenticated state. --live: the invoking service user's
native account. Run with an external process deadline (including SDK shutdown).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from importlib.metadata import version
from pathlib import Path

from openai_codex import AsyncCodex, CodexConfig, InvalidRequestError

from netizen_cli.account_rate_limits import AppServerAccountRateLimits
from netizen_cli.sdk_gap_adapter import require_no_facade_migration


async def probe(*, live: bool) -> dict[str, object]:
    require_no_facade_migration()
    evidence: dict[str, object] = {
        "sdk_version": version("openai-codex"),
        "bundled_cli_version": version("openai-codex-cli-bin"),
        "live_account": live,
    }
    with tempfile.TemporaryDirectory(prefix="netizen-quota-probe-") as directory:
        root = Path(directory)
        env = dict(os.environ)
        if not live:
            home, codex_home = root / "home", root / "codex"
            home.mkdir()
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                'cli_auth_credentials_store = "file"\n', encoding="utf-8",
            )
            env.update(HOME=str(home), CODEX_HOME=str(codex_home))
        async with AsyncCodex(CodexConfig(
            cwd=str(root), env=env, experimental_api=False,
        )) as codex:
            try:
                snapshot = await AppServerAccountRateLimits(codex).read()
            except InvalidRequestError as error:
                if live or error.code != -32600 or error.message not in {
                    "codex account authentication required to read rate limits",
                    "chatgpt authentication required to read rate limits",
                }:
                    raise
                evidence["result"] = "unauthenticated-rejected"
            else:
                if not live:
                    raise RuntimeError("disposable account unexpectedly returned quota")
                evidence.update(
                    result="read-success", buckets=len(snapshot.buckets),
                    windows=sum(
                        int(bucket.primary is not None) + int(bucket.secondary is not None)
                        for bucket in snapshot.buckets
                    ),
                )
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(live=args.live))
    except Exception as error:
        # Neither response data nor backend messages belong in probe evidence.
        print(json.dumps({
            "sdk_version": version("openai-codex"),
            "bundled_cli_version": version("openai-codex-cli-bin"),
            "live_account": args.live,
            "result": "failed", "error_type": type(error).__name__,
        }))
        raise SystemExit(1) from None
    print(json.dumps(result))
