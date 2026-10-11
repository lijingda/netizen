#!/usr/bin/env python3
"""Prove process-local Skill execution with disposable, read-only fixtures.

Requires --live and the service account's authenticated Codex environment. No
Feishu calls, service changes, global Skill installation or user config writes.
Each phase is independent and can be rerun with --phase. Run serially under an
EXTERNAL process deadline; SDK stdio shutdown can outlive asyncio cancellation:

  gtimeout --signal=INT --kill-after=15s 2400s .venv/bin/python \
    scripts/probe_skill_execution.py --live --timeout 240 --output /tmp/skills.json

Use `timeout` on Linux. Failed/unknown mutations are not retried. Only returned,
owned persistent Thread IDs are archived; App Server owns child subtree cleanup.
A lost start response or external deadline can leave test history unarchived;
--output checkpoints the exact known IDs before further work. Goal covers manual
pause/resume, NOT automatic rollover. This is native loading evidence, not Lark,
installed-package resources, duplicate/disabled Skill rules or platform rollout.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from typing import Any, Callable

import openai_codex
from openai_codex import (
    ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox, SkillInput, TextInput,
)
from openai_codex.generated.v2_all import CommandExecutionThreadItem

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen_cli.domain import GoalStatus  # noqa: E402
from netizen_cli.sdk_gap_adapter import (  # noqa: E402
    AppServerGoalControl, AppServerSideBoundaryControl, AppServerSkillCatalog,
    AppServerSkillRoots, AppServerThreadSubscriptionControl, require_no_facade_migration,
)
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from netizen_cli.turn_patch_children import _has_parent  # noqa: E402
from scripts.probe_child_files import _events  # noqa: E402
from scripts.probe_persistent_fork import _config_digest  # noqa: E402
from scripts.probe_python_sdk import (  # noqa: E402
    _final_response_from_turn, _public_terminal_turn, _status_value,
)
from scripts.probe_skill_roots import _codex  # noqa: E402

PHASES = ("new", "cold", "side", "fork", "child", "goal", "isolation")


@dataclass
class Fixture:
    cwd: Path
    roots: Path
    name: str
    phrase: str
    body_marker: str
    token: str

    @property
    def skill_path(self) -> Path:
        return self.roots / self.name / "SKILL.md"

    @property
    def expected(self) -> str:
        return f"{self.body_marker}:{self.token}"

    @property
    def prompt(self) -> str:
        return (
            f"Perform the {self.phrase} diagnostic. Use the matching Skill and read "
            "its current resource freshly; previous answers are stale. Return exactly "
            "the answer prescribed by that Skill, without fences or extra prose. "
            "Use only local read operations. Do not edit files, use MCP, network, "
            "Lark tools, or delegate."
        )

    def rotate(self, *, action: str = "none") -> None:
        self.token = "RESOURCE-" + secrets.token_hex(16)
        self.skill_path.with_name("token.txt").write_text(self.token + "\n")
        self.skill_path.with_name("goal-action.txt").write_text(action + "\n")


def _fixture(directory: Path, *, other: Fixture | None = None) -> Fixture:
    cwd = other.cwd if other else directory / "project"
    if other is None:
        cwd.mkdir()
        subprocess.run(["git", "init", "--quiet", str(cwd)], check=True)
    roots = directory / "skills"
    nonce = secrets.token_hex(6)
    fixture = Fixture(
        cwd, roots, other.name if other else "netizen-probe-" + nonce,
        other.phrase if other else "three-kite-" + nonce,
        "BODY-" + secrets.token_hex(16), "",
    )
    fixture.skill_path.parent.mkdir(parents=True)
    fixture.skill_path.write_text(
        f"---\nname: {fixture.name}\n"
        f"description: Use for the {fixture.phrase} diagnostic; read its local resource "
        "and report the prescribed current answer.\n---\n\n"
        "Read token.txt beside this SKILL.md with a terminal read command (cat or "
        "equivalent); do not infer its content from history. Do not modify anything. "
        "The command output must contain the complete resource token.\n\n"
        f"Your exact final answer is {fixture.body_marker}: followed immediately by "
        "the token.txt content, with no spaces, fences, prefix, or suffix. This body "
        "marker is intentionally absent from the catalog description and user request.\n\n"
        "Only when working on an active Goal whose user objective explicitly authorizes "
        "fixture status updates: also read goal-action.txt beside this file. 'pause' "
        "means call the native Goal update tool to pause; 'complete' means mark the "
        "Goal complete. Do that after the resource read and before your final answer. "
        "Never infer the action from an earlier Turn. Otherwise ignore this action file.\n",
        encoding="utf-8",
    )
    fixture.rotate()
    return fixture


def _config(fixture: Fixture, model: str | None) -> CodexConfig:
    # Native override keys split on dots; quoting a dotted key does not quote the
    # path. An inline TOML value preserves the exact cwd (including its dots).
    overrides = [
        'projects={' + json.dumps(str(fixture.cwd)) + '={trust_level="trusted"}}',
        "allow_login_shell=false",
    ]
    if model:
        overrides.append("model=" + json.dumps(model))
    return CodexConfig(cwd=str(fixture.cwd), config_overrides=tuple(overrides))


@asynccontextmanager
async def _server(fixture: Fixture, args: argparse.Namespace):
    async with _codex(_config(fixture, args.model)) as codex:
        await AppServerSkillRoots(codex).set_roots((fixture.roots,))
        snapshot = await AppServerSkillCatalog(codex).list(fixture.cwd, force_reload=True)
        matches = [skill for skill in snapshot.skills if skill.name == fixture.name]
        if snapshot.errors or len(matches) != 1 or not matches[0].enabled:
            raise AssertionError("fixture catalog is missing, disabled or ambiguous")
        if Path(matches[0].path) != fixture.skill_path:
            raise AssertionError("fixture catalog selected another root")
        yield codex


def _verify(turn: Any, fixture: Fixture) -> dict[str, Any]:
    if _status_value(turn) != "completed":
        raise AssertionError("Skill Turn did not complete")
    if _final_response_from_turn(turn) != fixture.expected:
        raise AssertionError("final does not match the Skill body and fresh resource markers")
    reads = [
        wrapped.root.id for wrapped in turn.items
        if type(wrapped.root) is CommandExecutionThreadItem
        and _status_value(wrapped.root) == "completed"
        and wrapped.root.exit_code == 0
        and fixture.token in (wrapped.root.aggregated_output or "")
    ]
    if not reads:
        raise AssertionError("no successful command in this Turn read the resource marker")
    return {
        "turn_id": turn.id, "resource_read_item_ids": reads,
        "expected_answer": fixture.expected,
        "body_marker_and_fresh_resource_match": True,
    }


async def _turn(thread: AsyncThread, prompt: Any, args: argparse.Namespace) -> Any:
    handle = await thread.turn(prompt, model=args.model)
    turn = await _public_terminal_turn(thread, handle.id, timeout=args.timeout)
    await handle.run()  # Exactly one consumer; public read remains evidence authority.
    return turn


async def _start(
    codex: AsyncCodex, fixture: Fixture, evidence: dict[str, Any], checkpoint: Callable[[], None],
) -> AsyncThread:
    thread = await codex.thread_start(
        cwd=str(fixture.cwd), sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all,
    )
    evidence["owned_threads"][thread.id] = "created"
    checkpoint()
    return thread


async def _goal(
    codex: AsyncCodex, thread: AsyncThread, fixture: Fixture,
    args: argparse.Namespace, evidence: dict[str, Any],
) -> None:
    control = AppServerGoalControl(codex)
    fixture.rotate(action="pause")
    handles = []
    try:
        handle = await control.start(
            thread.id, fixture.prompt + " This Goal explicitly authorizes reading the "
            "Skill's goal-action.txt and pausing or completing this Goal as that current "
            "file requests. Pause is an explicit user request. After manual resume, "
            "repeat with fresh resource and action reads. End each Turn with the exact "
            "Skill answer, after the authorized Goal status update.",
        )
        handles.append(handle)
        first = await handle.wait_terminal()
        first_turn = await _public_terminal_turn(
            thread, first.final_physical_turn_id, timeout=args.timeout,
        )
        evidence["first"] = _verify(first_turn, fixture)
        paused = await control.get(thread.id)
        if paused is None or paused.status is not GoalStatus.PAUSED:
            raise AssertionError("fixture did not pause its Goal")
        fixture.rotate(action="complete")
        resumed = await control.resume(thread.id)
        handles.append(resumed)
        terminal = await resumed.wait_terminal()
        if terminal.final_physical_turn_id == first.final_physical_turn_id:
            raise AssertionError("Goal resume reused its first physical Turn")
        turn = await _public_terminal_turn(
            thread, terminal.final_physical_turn_id, timeout=args.timeout,
        )
        evidence["resumed"] = _verify(turn, fixture)
        completed = await control.get(thread.id)
        if completed is None or completed.status is not GoalStatus.COMPLETE:
            raise AssertionError("fixture did not complete its resumed Goal")
        if not await control.clear(thread.id) or await control.get(thread.id) is not None:
            raise AssertionError("completed fixture Goal was not cleared")
        evidence["automatic_rollover_verified"] = False
    finally:
        for handle in handles:
            await handle.aclose()


async def _scenario(
    phase: str, fixture: Fixture, args: argparse.Namespace,
    evidence: dict[str, Any], checkpoint: Callable[[], None],
) -> None:
    async with _server(fixture, args) as codex:
        thread = await _start(codex, fixture, evidence, checkpoint)
        if phase in {"new", "cold"}:
            evidence["first"] = _verify(await _turn(thread, fixture.prompt, args), fixture)
        elif phase == "goal":
            await _goal(codex, thread, fixture, args, evidence)
        elif phase == "isolation":
            other = _fixture(fixture.roots.parent / "other", other=fixture)
            # Both servers are alive, share HOME/CODEX_HOME and cwd, and register
            # the same Skill name before either model runs. Only roots differ.
            async with _server(other, args) as second:
                other_thread = await _start(second, other, evidence, checkpoint)
                evidence["first"] = _verify(await _turn(thread, fixture.prompt, args), fixture)
                evidence["second"] = _verify(await _turn(other_thread, other.prompt, args), other)
                evidence["same_home_cwd_name_distinct_roots"] = True
        else:
            seed = await _turn(thread, "Reply exactly READY. Do not use tools.", args)
            if _status_value(seed) != "completed" or _final_response_from_turn(seed) != "READY":
                raise AssertionError("parent seed failed")
            if phase in {"side", "fork"}:
                fork = await codex.thread_fork(
                    thread.id, ephemeral=phase == "side", include_turns=False,
                )
                if fork.id == thread.id:
                    raise AssertionError("fork reused parent identity")
                if phase == "fork":
                    evidence["owned_threads"][fork.id] = "created"
                else:
                    evidence["side_thread_id"] = fork.id
                checkpoint()
                try:
                    native = (await fork.read(include_turns=False)).thread
                    if native.id != fork.id:
                        raise AssertionError("fork read returned a different Thread identity")
                    if native.ephemeral is not (phase == "side"):
                        raise AssertionError("fork ephemeral identity changed")
                    if native.forked_from_id is not None and native.forked_from_id != thread.id:
                        raise AssertionError("fork lost exact native parent identity")
                    evidence["fork_parent_field"] = (
                        "missing" if native.forked_from_id is None else "matched"
                    )
                    checkpoint()
                    if phase == "fork":
                        evidence["fork"] = _verify(await _turn(fork, fixture.prompt, args), fixture)
                    else:
                        await AppServerSideBoundaryControl(codex).inject_boundary(fork.id)
                        handle = await fork.turn([
                            TextInput(f"${fixture.name}\n{fixture.prompt}"),
                            SkillInput(name=fixture.name, path=str(fixture.skill_path)),
                        ], model=args.model)
                        evidence["side"] = _verify(await handle.run(), fixture)
                finally:
                    if phase == "side":
                        try:
                            await PinnedExperimentalTerminalCleanup(codex).clean_thread(fork.id)
                        finally:
                            status = await AppServerThreadSubscriptionControl(codex).unsubscribe(fork.id)
                            evidence["side_unsubscribe"] = status.value
                            checkpoint()
            elif phase == "child":
                parent = await _turn(
                    thread, "Explicitly spawn exactly one native sub-agent with this task: "
                    + fixture.prompt + " The child must do that task itself. You must not "
                    "read the Skill or resource yourself. Wait for the child to finish, "
                    "then reply exactly CHILD-DONE. Do not close the child. No other work.", args,
                )
                events = _events(parent.items)
                children = {
                    receiver for event in events
                    if event.get("tool") == "spawnAgent" and event.get("status") == "completed"
                    and event.get("sender") == thread.id
                    for receiver in event.get("receivers", []) if receiver
                } | {
                    event["receiver"] for event in events
                    if event.get("kind") == "started" and event.get("receiver")
                }
                if len(children) != 1 or thread.id in children:
                    raise AssertionError("expected exactly one native child")
                child_id = children.pop()
                child = (await AsyncThread(codex, child_id).read(include_turns=True)).thread
                if child.id != child_id or not _has_parent(child, thread.id):
                    raise AssertionError("child lacks exact native parent provenance")
                parent_ids = {turn.id for turn in (await thread.read(include_turns=True)).thread.turns}
                own_turns = [turn for turn in child.turns if turn.id not in parent_ids]
                if not own_turns:
                    raise AssertionError("child has no own Turn; parent relay is not proof")
                evidence["child_thread_id"] = child_id
                evidence["child"] = _verify(own_turns[-1], fixture)
        checkpoint()
    if phase == "cold":
        # A real process restart, not another connection to the same server.
        fixture.rotate()
        async with _server(fixture, args) as codex:
            resumed = await codex.thread_resume(thread.id, include_turns=False)
            if resumed.id != thread.id:
                raise AssertionError("cold resume changed Thread identity")
            evidence["cold"] = _verify(await _turn(resumed, fixture.prompt, args), fixture)
            if evidence["cold"]["turn_id"] == evidence["first"]["turn_id"]:
                raise AssertionError("cold resume reused its first Turn")
            checkpoint()


async def _cleanup(
    fixture: Fixture, args: argparse.Namespace,
    evidence: dict[str, Any], checkpoint: Callable[[], None],
) -> None:
    if not evidence["owned_threads"]:
        return
    # Reverse order archives a persistent fork before its source. Never enumerate
    # or mutate unrelated history; native root archive handles native descendants.
    async with _codex(_config(fixture, args.model)) as codex:
        for thread_id in reversed(evidence["owned_threads"]):
            if evidence["owned_threads"][thread_id] != "created":
                continue
            evidence["owned_threads"][thread_id] = "archive_unknown"
            checkpoint()
            try:
                async with asyncio.timeout(20):
                    await codex.thread_archive(thread_id)
            except Exception as error:
                evidence.setdefault("cleanup_errors", []).append(type(error).__name__)
            else:
                evidence["owned_threads"][thread_id] = "archive_acknowledged"
            checkpoint()


async def probe(args: argparse.Namespace, report: dict[str, Any], checkpoint: Callable[[], None]) -> bool:
    require_no_facade_migration()
    before = _config_digest()
    for phase in args.phase or PHASES:
        evidence: dict[str, Any] = {"status": "running", "owned_threads": {}}
        report["phases"][phase] = evidence
        checkpoint()
        print(f"[probe] skill-execution/{phase}: started", file=sys.stderr, flush=True)
        with tempfile.TemporaryDirectory(prefix="netizen-skill-execution-") as directory:
            fixture = _fixture(Path(directory).resolve())
            try:
                async with asyncio.timeout(args.timeout):
                    await _scenario(phase, fixture, args, evidence, checkpoint)
                evidence["status"] = "passed"
            except Exception as error:
                evidence["status"] = "failed"
                evidence["error"] = {"type": type(error).__name__, "message": str(error)}
            finally:
                try:
                    async with asyncio.timeout(60):
                        await _cleanup(fixture, args, evidence, checkpoint)
                except Exception as error:
                    evidence.setdefault("cleanup_errors", []).append(type(error).__name__)
                evidence["config_unchanged"] = _config_digest() == before
                if evidence.get("cleanup_errors") or not evidence["config_unchanged"]:
                    evidence["status"] = "failed"
                checkpoint()
        print(json.dumps({"phase": phase, **evidence}, ensure_ascii=False), flush=True)
        if evidence["status"] != "passed":
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="authorize disposable real-model execution")
    parser.add_argument("--phase", action="append", choices=PHASES, help="repeatable; default: all")
    parser.add_argument("--model", help="optional public process-level model override")
    parser.add_argument("--timeout", type=float, default=240, help="per-phase work deadline, seconds")
    parser.add_argument("--output", type=Path, help="checkpoint JSON after each acknowledged mutation")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this probe consumes real model usage")
    if args.timeout <= 0 or (args.phase and len(args.phase) != len(set(args.phase))):
        parser.error("timeout must be positive and phase selections must not repeat")
    report: dict[str, Any] = {
        "openai_codex_version": openai_codex.__version__, "model_override": args.model,
        "phases": {}, "automatic_goal_rollover_verified": False,
        "lark_or_platform_rollout_verified": False,
    }

    def checkpoint() -> None:
        if args.output:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    checkpoint()
    passed = asyncio.run(probe(args, report, checkpoint))
    report["passed"] = passed
    checkpoint()
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
