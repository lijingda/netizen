#!/usr/bin/env python3
"""Verify ADR 0056 with a real spawned child in a disposable Git directory.

Requires explicit --live and the service account's logged-in Codex environment.
Creates three root Turns and child work, consumes model usage, and archives its
exact root Thread once; App Server owns descendant shutdown and archive. A lost
root-start response or failed archive can leave native test history unarchived.
Temporary files and native-added trust for this exact fixture are removed;
existing and unrelated user configuration is preserved. The probe sends no
Feishu messages and does not change service state.
Use an outer process timeout as the final bound for an unresponsive SDK/server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import openai_codex
from openai_codex import AsyncCodex, AsyncThread
from openai_codex.generated.v2_all import (
    CollabAgentToolCallThreadItem,
    SubAgentActivityThreadItem,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen.turn_files import turn_patch_summary  # noqa: E402
from netizen.turn_patch_children import (  # noqa: E402
    _has_parent,
    collect_turn_patch_children,
)
from netizen.sdk_gap_adapter import facade_migration_requirements  # noqa: E402
from scripts.probe_python_sdk import (  # noqa: E402
    _public_terminal_turn,
    _status_value,
)
from scripts.probe_scheduled_tasks import (  # noqa: E402
    _read_config,
    _remove_fixture_trust,
    _user_config_path,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _events(items: Any) -> list[dict[str, Any]]:
    """Keep typed identities and categories, never prompts or tool output."""
    events = []
    for wrapped in items:
        item = wrapped.root
        if type(item) is CollabAgentToolCallThreadItem:
            events.append({
                "item_id": item.id,
                "tool": item.tool.value,
                "status": item.status.value,
                "sender": item.sender_thread_id,
                "receivers": item.receiver_thread_ids,
            })
        elif type(item) is SubAgentActivityThreadItem:
            events.append({
                "item_id": item.id,
                "kind": item.kind.value,
                "receiver": item.agent_thread_id,
            })
    return events


def _has_reference(events: list[dict[str, Any]], receiver: str) -> bool:
    return any(
        (
            event.get("tool") in {"sendInput", "sendMessage", "followupTask"}
            and event.get("status") == "completed"
            and receiver in event.get("receivers", [])
        )
        or (event.get("kind") == "interacted" and event.get("receiver") == receiver)
        for event in events
    )


def _counts(summary: Any, cwd: Path) -> dict[str, tuple[int | None, int | None]]:
    return {
        str(Path(item.path).relative_to(cwd)): (item.additions, item.deletions)
        for item in summary.files
    }


def _verify_child_history(
    child: Any, root_id: str, root_turn_ids: set[str], children: Any,
) -> list[str]:
    _require(
        child.forked_from_id == root_id and _has_parent(child, root_id),
        "child did not retain exact native fork and parent provenance",
    )
    _require(
        all(batch.turn_id not in root_turn_ids for batch in children.batches),
        "inherited parent Turn leaked into child patch evidence",
    )
    # V2 can inherit model context while exposing only child-owned UI Turns.
    # Record that view without claiming to inspect the model's full context.
    return sorted(root_turn_ids.intersection(turn.id for turn in child.turns))


async def _turn(thread: AsyncThread, prompt: str, args: argparse.Namespace) -> Any:
    async with asyncio.timeout(args.timeout):
        handle = await thread.turn(prompt, model=args.model)
        turn = await _public_terminal_turn(thread, handle.id, timeout=args.timeout)
        _require(_status_value(turn) == "completed", "root Turn did not complete")
        _require(turn.items_view.value == "full", "root Turn items are incomplete")
        # Public read is the evidence authority; then drain this one handle.
        await handle.run()
        return turn


async def _scenario(
    codex: AsyncCodex,
    cwd: Path,
    args: argparse.Namespace,
    evidence: dict[str, Any],
    owned: set[str],
) -> None:
    async with asyncio.timeout(args.timeout):
        root = await codex.thread_start(cwd=str(cwd))
    owned.add(root.id)
    evidence["root_thread_id"] = root.id
    print("[probe] child-files/seed: started", file=sys.stderr, flush=True)
    seed = await _turn(
        root,
        "Use apply_patch to create inherited.txt containing exactly 'inherited-only' "
        "and one trailing newline. Do not delegate or modify another file. Reply SEED.",
        args,
    )
    evidence["seed_turn_id"] = seed.id
    _require(
        _counts(turn_patch_summary(seed.items, cwd), cwd) == {"inherited.txt": (1, 0)},
        "seed did not produce the expected successful patch",
    )

    print("[probe] child-files/spawn: started", file=sys.stderr, flush=True)
    current = await _turn(
        root,
        "This is an explicit request to spawn exactly one native sub-agent named "
        "files_worker, with full conversation history (fork_turns=all if available). "
        "Give it this task: use apply_patch to create child.txt with exactly two lines "
        "'child-first' and 'child-second', each ending in newline; send one native "
        "collaboration message back to the root agent (send_message or send_input), "
        "then finish. The child must not delegate. Wait until that child finishes. "
        "Then YOU use apply_patch to replace only child-second with parent-second "
        "in child.txt and create parent.txt with exactly 'parent-only' and a newline. "
        "Do not touch inherited.txt or other files. Do not close the child; keep it "
        "available for a later follow-up. Reply PATCHES-DONE after all work completes.",
        args,
    )
    evidence["current_turn_id"] = current.id
    root_events = _events(current.items)
    spawned = {
        receiver for event in root_events
        if event.get("tool") == "spawnAgent" and event.get("status") == "completed"
        and event.get("sender") == root.id
        for receiver in event.get("receivers", []) if receiver
    } | {
        event["receiver"] for event in root_events
        if event.get("kind") == "started" and event.get("receiver")
    }
    owned.update(spawned)
    _require(len(spawned) == 1 and root.id not in spawned, "expected exactly one new child")
    children = await collect_turn_patch_children(
        codex, thread_id=root.id, turn_id=current.id, items=current.items
    )
    _require(children.complete, "new child provenance or patch snapshot is incomplete")
    child_ids = {batch.thread_id for batch in children.batches}
    _require(child_ids == spawned, "expected patches from exactly the one spawned child")
    child_id = next(iter(child_ids))
    owned.add(child_id)
    evidence["child_thread_id"] = child_id
    child = (await AsyncThread(codex, child_id).read(include_turns=True)).thread
    _require(child.id == child_id, "child read identity mismatch")
    child_turn_ids = {turn.id for turn in child.turns}
    inherited_turn_ids = _verify_child_history(
        child, root.id, {seed.id, current.id}, children
    )
    child_events = _events([
        item for turn in child.turns if turn.id not in {seed.id, current.id}
        for item in turn.items
    ])
    _require(_has_reference(child_events, root.id), "missing real child-to-root message")
    summary = turn_patch_summary(current.items, cwd, children=children)
    counts = _counts(summary, cwd)
    _require(
        counts == {"child.txt": (3, 1), "parent.txt": (1, 0)}
        and (summary.additions, summary.deletions) == (4, 1),
        "parent/child successful patches were not accumulated exactly once",
    )
    evidence["new_child"] = {
        "parent_thread_id": child.parent_thread_id,
        "source": child.source.model_dump(mode="json"),
        "forked_from_id": child.forked_from_id,
        "inherited_parent_turn_ids_in_public_view": inherited_turn_ids,
        "seed_patch_excluded": True,
        "root_events": root_events,
        "child_events": child_events,
        "patch_identities": [
            {"thread_id": batch.thread_id, "turn_id": batch.turn_id,
             "item_ids": [item.id for item in batch.items]}
            for batch in children.batches
        ],
        "file_counts": counts,
        "total": [summary.additions, summary.deletions],
    }

    print("[probe] child-files/old-child: started", file=sys.stderr, flush=True)
    followup = await _turn(
        root,
        f"Give the existing native child {child_id} a follow-up task using "
        "followup_task or send_input: use apply_patch to create followup.txt "
        "containing exactly 'followup-only' and a newline, then finish. Do not "
        "spawn a new agent. Wait for it to finish. Then YOU use apply_patch to "
        "create current-only.txt containing exactly 'current-only' and a newline. "
        "Do not modify any other files. Reply FOLLOWUP-DONE.",
        args,
    )
    followup_events = _events(followup.items)
    _require(_has_reference(followup_events, child_id), "missing real old-child reference")
    related = await collect_turn_patch_children(
        codex, thread_id=root.id, turn_id=followup.id, items=followup.items
    )
    _require(not related.complete and not related.batches, "old child history was imported")
    latest_child = (await AsyncThread(codex, child_id).read(include_turns=True)).thread
    _require(latest_child.id == child_id, "follow-up child read identity mismatch")
    new_turns = [turn for turn in latest_child.turns if turn.id not in child_turn_ids]
    _require(
        len(new_turns) == 1 and _status_value(new_turns[0]) == "completed"
        and _counts(turn_patch_summary(new_turns[0].items, cwd), cwd)
        == {"followup.txt": (1, 0)},
        "old child did not produce an exact completed follow-up patch",
    )
    partial = turn_patch_summary(followup.items, cwd, children=related)
    _require(
        _counts(partial, cwd) == {"current-only.txt": (1, 0)}
        and partial.additions is None and partial.deletions is None,
        "old-child reference did not preserve known root counts with an unknown total",
    )
    evidence["old_child"] = {
        "root_turn_id": followup.id,
        "child_turn_id": new_turns[0].id,
        "root_events": followup_events,
        "child_history_imported": False,
        "file_counts": _counts(partial, cwd),
        "total": None,
    }


async def probe(args: argparse.Namespace) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "openai_codex_version": openai_codex.__version__,
        "model_override": args.model,
        "status": "failed",
    }
    migrations = facade_migration_requirements()
    _require(not migrations, "SDK facade migration is required before the live probe")
    evidence["sdk_gap_facade_migrations"] = list(migrations)
    config_path = _user_config_path()
    before = _read_config(config_path)
    with tempfile.TemporaryDirectory(prefix="netizen-child-files-") as directory:
        cwd = Path(directory).resolve()
        owned: set[str] = set()
        evidence["cleanup"] = []
        try:
            subprocess.run(["git", "-C", str(cwd), "init", "--quiet"], check=True)
            async with AsyncCodex() as codex:
                try:
                    await _scenario(codex, cwd, args, evidence, owned)
                    evidence["status"] = "passed"
                finally:
                    # Native root archive shuts down and archives the spawn subtree.
                    # Do not archive children again after that cascade, or retry a
                    # response whose side effects are unknown.
                    thread_id = evidence.get("root_thread_id")
                    if thread_id in owned:
                        try:
                            async with asyncio.timeout(10):
                                await codex.thread_archive(thread_id)
                            evidence["cleanup"].append({
                                "thread_id": thread_id,
                                "status": "archive_acknowledged",
                                "descendants": "native_spawn_subtree",
                            })
                        except Exception as error:
                            evidence["cleanup"].append({
                                "thread_id": thread_id, "status": "unknown",
                                "error_type": type(error).__name__,
                            })
                            evidence["status"] = "failed"
        except Exception as error:
            evidence["status"] = "failed"
            evidence["error"] = {"type": type(error).__name__}
            if isinstance(error, AssertionError):
                evidence["error"]["message"] = str(error)
        finally:
            # Cover startup, shutdown and cancellation as well as scenario errors.
            # The shared helper removes only newly added exact fixture trust;
            # concurrent unrelated edits and pre-existing settings remain intact.
            try:
                evidence["fixture_trust_cleaned"] = _remove_fixture_trust(
                    config_path, before, {str(cwd)}
                )
            except Exception as error:
                evidence["status"] = "failed"
                evidence["fixture_trust_cleanup_error"] = {"type": type(error).__name__}
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="authorize real native Turns")
    parser.add_argument("--model", help="optional native model for these disposable Turns")
    parser.add_argument("--timeout", type=float, default=240, help="per-Turn seconds (240)")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this probe creates real Threads and consumes usage")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    result = asyncio.run(probe(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
