#!/usr/bin/env python3
"""Serial known regression set; never an SDK upgrade approval.

The registry is the only inventory. --list shows commands, deadlines and evidence
rules without running probes. Account/native phases require explicit --live.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.source_evidence import source_evidence  # noqa: E402

FOOTER = "此结果只覆盖已知回归集，本次版本的新增行为仍须按升级审查第 2–4 步判断并补针对性验证。"


@dataclass(frozen=True)
class Step:
    name: str
    group: str
    script: str
    arguments: tuple[str, ...] = ()
    deadline: float = 420
    grace: float = 10
    live: bool = False
    needs_model: bool = False
    evidence: str = "passed"
    evidence_source: str = "stdout"
    case_count: int | None = None
    weakness: str | None = None


# Order and budgets follow deployment.md; supplementary budgets follow the
# 0.167.0 serial run. Admin sessions: 2*90+180 internal seconds + shutdown margin;
# input attachments: 90 internal seconds + shutdown margin.
REGISTRY = (
    *(Step(f"sdk-{phase}", "native-phases", "probe_python_sdk.py",
           ("--cwd", "{cwd}", "--phase", phase), deadline=deadline,
           live=True, evidence="native")
      for phase, deadline in (
          *((name, 420) for name in (
              "models", "turn-settings", "smoke", "usage", "steer", "plan", "polling",
              "compact", "concurrency", "interrupt", "skills", "lifecycle", "side", "release")),
          ("config", 660), ("goal", 660), ("sandbox", 300))),
    Step("account-rate-limits", "account", "probe_account_rate_limits.py", ("--live",),
         deadline=120, live=True, evidence="quota"),
    Step("persistent-fork", "account", "probe_persistent_fork.py", live=True),
    Step("thread-naming", "account", "probe_thread_naming.py", live=True,
         evidence="exit-only", weakness="仅退出码：JSON 无整体通过标记；断言在探针内执行"),
    Step("child-files", "account", "probe_child_files.py", ("--live",),
         deadline=900, live=True, evidence="status"),
    # Grouped with account acceptance even though this discovery uses no account.
    Step("skill-roots", "account", "probe_skill_roots.py",
         ("--source-root", "{root}", "--timeout", "15"), deadline=60, grace=5,
         evidence="skill-roots"),
    Step("skill-execution", "account", "probe_skill_execution.py",
         ("--live", "--timeout", "240", "--output", "{json}"), deadline=2400, grace=15,
         live=True, evidence="skills", evidence_source="json"),
    *(Step(f"scheduled-{phase}", "account", "probe_scheduled_tasks.py",
           ("--phase", phase, "--model", "{model}", "--output", "{json}"),
           live=True, needs_model=True, evidence_source="json")
      for phase in ("mcp", "mcp-recovery", "dispatch", "manual", "binding")),
    Step("project-delete", "account", "probe_project_delete.py", live=True,
         evidence="project-delete"),
    Step("provider-admission", "isolated", "probe_model_provider_admission.py",
         ("--timeout", "60", "--output", "{json}"), deadline=90, grace=5, evidence_source="json"),
    Step("thread-restore", "isolated", "probe_thread_restore.py",
         ("--output", "{json}"), deadline=90, grace=5, evidence_source="json"),
    Step("native-boundaries", "isolated", "probe_sdk_native_boundaries.py",
         ("--output", "{json}"), deadline=180, evidence="cases", evidence_source="json", case_count=7),
    Step("partial-answers", "isolated", "probe_sdk_partial_answers.py",
         ("--output", "{json}"), deadline=150, evidence="cases", evidence_source="json", case_count=6),
    Step("session-collaboration", "isolated", "probe_session_collaboration.py",
         ("--output", "{json}"), deadline=240, evidence="cases", evidence_source="json", case_count=8),
    Step("cancellation", "isolated", "probe_sdk_cancellation.py",
         ("--output", "{json}"), deadline=180, evidence="cases", evidence_source="json", case_count=3),
    *(Step(f"recovery-{case}", "isolated", "probe_sdk_response_recovery.py",
           ("--case", case, "--output", "{json}"), deadline=500,
           evidence="cases", evidence_source="json", case_count=1)
      for case in ("incomplete_max_output_tokens", "incomplete_unknown", "incomplete_content_filter",
                   "incomplete_max_output_tokens_retries", "incomplete_unknown_retries",
                   "interrupted_continuation", "cold_permissions")),
    Step("thread-history", "isolated", "probe_thread_history.py", deadline=210,
         evidence="exit-only", weakness="仅退出码：stdout 为多个 JSON，无整体通过标记；断言在探针内执行"),
    Step("admin-sessions", "isolated", "probe_admin_sessions.py",
         ("--output", "{json}"), deadline=420, evidence="admin", evidence_source="json", case_count=2),
    Step("input-attachments", "isolated", "probe_input_attachments.py",
         ("--output", "{json}"), deadline=120, evidence_source="json"),
    Step("scheduled-worktree", "isolated", "probe_scheduled_tasks.py",
         ("--phase", "worktree", "--timeout", "100", "--output", "{json}"),
         deadline=120, grace=5, evidence_source="json"),
    Step("scheduled-auto-archive", "isolated", "probe_scheduled_auto_archive.py",
         ("--timeout", "100", "--output", "{json}"), deadline=120, grace=5,
         evidence="scenarios", evidence_source="json", case_count=5),
    Step("worktree-cwd", "isolated", "probe_worktree_cwd.py", ("--output", "{json}"),
         deadline=90, grace=5, evidence_source="json"),
)
EXCLUDED_PROBES = {
    "probe_sdk_turn_plan.py": "check_sdk.py / make check 固定 synthetic 门禁",
    "probe_sdk_completion_race.py": "check_sdk.py / make check 固定 synthetic 门禁",
    "probe_feishu_completion_mention.py": "需另行授权真实飞书测试群与客户端验收",
    "probe_feishu_partial_answers.py": "需另行授权真实飞书测试群与客户端验收",
    "probe_feishu_turn_file_card.py": "需另行授权真实飞书测试群与客户端验收",
    "probe_admin_update_executor.py": "独立部署平台验收；不是 SDK 回归，make check 有行为覆盖",
}


def evidence_check(step: Step, payload: Any, stderr: str, sdk_version: str) -> None:
    """Require the probe's actual success shape, including nonempty case sets."""
    if not isinstance(payload, dict):
        raise ValueError("证据必须是 JSON object")
    kind = step.evidence
    if kind == "native":
        phase = step.arguments[step.arguments.index("--phase") + 1]
        ok = (f"[probe] {phase}: passed" in stderr.splitlines()
              and payload.get("openai_codex_version") == sdk_version
              and payload.get("sdk_gap_facade_migrations") == [])
    elif kind == "passed":
        ok = payload.get("passed") is True
    elif kind in {"cases", "admin", "scenarios"}:
        cases = payload.get("scenarios" if kind == "scenarios" else "cases")
        ok = (isinstance(cases, list) and len(cases) == step.case_count and bool(cases)
              and all(isinstance(case, dict) and case.get("passed") is True for case in cases))
        if kind == "scenarios":
            ok = ok and payload.get("passed") is True
        if kind == "admin":
            ok = ok and payload.get("sparse_search", {}).get("passed") is True
    elif kind == "project-delete":
        ok = all(isinstance(payload.get(key), dict) and payload[key].get("passed") is True
                 for key in ("mixed_sessions", "orphan_side"))
    elif kind == "status":
        ok = payload.get("status") == "passed"
    elif kind == "quota":
        ok = payload.get("result") == "read-success" and payload.get("live_account") is True
    elif kind == "skill-roots":
        ok = all(payload.get(key) is True for key in (
            "process_local", "native_user_skills_preserved", "config_unchanged"))
        ok = ok and isinstance(payload.get("discovered"), list) and bool(payload["discovered"])
    elif kind == "skills":
        phases = payload.get("phases")
        ok = (payload.get("passed") is True and isinstance(phases, dict)
              and set(phases) == {"new", "cold", "side", "fork", "child", "goal", "isolation"}
              and all(isinstance(case, dict) and case.get("status") == "passed" for case in phases.values()))
    else:
        raise ValueError(f"未知证据判定方式：{kind}")
    if not ok:
        raise ValueError(f"通过证据不成立或字段缺失：{kind}")


def signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


@contextmanager
def interruption_signals():
    state = {"signal": None, "running": False}
    def interrupt(signum, _frame):
        # Repeated signals must not cut short the bounded cleanup/summary path.
        if state["signal"] is None:
            state["signal"] = signum
            if state["running"]:
                raise KeyboardInterrupt
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        for sig in original:
            signal.signal(sig, interrupt)
        yield state
    finally:
        for sig, handler in original.items():
            signal.signal(sig, handler)


def sanitize_commandline(command: str) -> str:
    command = re.sub(r"(?i)(--?(?:api[-_]key|access[-_]token|token|password|secret)|authorization)\s*(?:=|:|\s)\s*\S+",
                     r"\1=<redacted>", command)
    command = re.sub(r"(?:https?|ssh|git|wss?)://[^\s\"']+", "<url>", command)
    command = re.sub(r"/[^\s\"'\[\]{};]+", "<path>", command)
    command = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[0-9a-f]{16,}\b", "<id>", command, flags=re.I)
    command = re.sub(r"--keep=fetch-pack \S+ on \S+", "--keep=fetch-pack <pid> on <host>", command)
    return " ".join(command.split())[:1000]


def live_group_members(pid: int) -> list[dict[str, str]]:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return []
    # ps supplies process states on both Linux and macOS. killpg alone also
    # reports groups containing only zombies waiting for an external reaper.
    snapshot = subprocess.run(["ps", "-ax", "-o", "pgid=,stat=,comm=,args="],
                              check=True, capture_output=True, text=True, timeout=3)
    members = []
    for line in snapshot.stdout.splitlines():
        fields = line.split(None, 3)
        if not fields or fields[0] != str(pid):
            continue
        if len(fields) < 3 or not re.fullmatch(r"[A-Z][A-Za-z0-9+<=>-]*", fields[1]):
            raise ValueError("无法确定自有进程组成员状态")
        if not fields[1].startswith("Z"):
            members.append({"comm": Path(fields[2]).name,
                            "commandline": sanitize_commandline(fields[3] if len(fields) == 4 else "")})
    return members


def wait_group_empty(pid: int, grace: float) -> bool:
    end = time.monotonic() + grace
    waiter = threading.Event()
    while True:
        if not live_group_members(pid):
            return True
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        waiter.wait(min(0.05, remaining))


def stop_group(process: subprocess.Popen, grace: float) -> bool:
    signal_group(process.pid, signal.SIGINT)
    end = time.monotonic() + grace
    remaining = grace
    while True:
        try:
            process.wait(timeout=remaining)
            if wait_group_empty(process.pid, max(0, end - time.monotonic())):
                return True
            break
        except subprocess.TimeoutExpired:
            break
        except KeyboardInterrupt:
            # A first signal can arrive during deadline cleanup. Finish that
            # cleanup; the suite's signal state still stops subsequent steps.
            remaining = max(0, end - time.monotonic())
            if not remaining:
                break
    signal_group(process.pid, signal.SIGKILL)
    end = time.monotonic() + 5
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return wait_group_empty(process.pid, max(0, end - time.monotonic()))


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    timed_out: bool = False
    residual_processes: tuple[dict[str, str], ...] = ()
    cleanup_failed: bool = False


def run_command(command: list[str], stdout: Path, stderr: Path, deadline: float, grace: float) -> CommandResult:
    with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
        process = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err, start_new_session=True)
        try:
            code = process.wait(timeout=deadline)
            residual = () if wait_group_empty(process.pid, grace) else tuple(live_group_members(process.pid))
            cleaned = stop_group(process, grace) if residual else True
            return CommandResult(code, residual_processes=residual, cleanup_failed=not cleaned)
        except subprocess.TimeoutExpired:
            cleaned = stop_group(process, grace)
            return CommandResult(process.returncode, timed_out=True, cleanup_failed=not cleaned)
        except KeyboardInterrupt:
            stop_group(process, grace)
            raise
        except BaseException:
            signal_group(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise


def run_step(step: Step, output_dir: Path, cwd: Path, model: str | None, sdk_version: str) -> dict[str, Any]:
    paths = {kind: output_dir / f"{step.name}.{suffix}" for kind, suffix in (
        ("stdout", "stdout"), ("stderr", "stderr"), ("json", "json"))}
    values = {"root": str(ROOT), "cwd": str(cwd), "model": model or "", "json": str(paths["json"])}
    command = [sys.executable, str(ROOT / "scripts" / step.script),
               *(arg.format_map(values) for arg in step.arguments)]
    result: dict[str, Any] = {"name": step.name, "group": step.group, "status": "failed",
        "command": command, "deadline": step.deadline, "grace": step.grace,
        "evidence_rule": step.evidence, "weakness": step.weakness,
        "files": {key: path.name for key, path in paths.items()}, "exit_code": None}
    started = time.monotonic()
    try:
        if step.needs_model and not model:
            raise ValueError("前置条件失败：需要 --model 或已通过 sdk-models 的唯一默认条目的 model slug")
        command_result = run_command(command, paths["stdout"], paths["stderr"], step.deadline, step.grace)
        result.update(exit_code=command_result.exit_code, timed_out=command_result.timed_out,
                      residual_group=bool(command_result.residual_processes), cleanup_failed=command_result.cleanup_failed)
        result["warnings"] = ([{"reason": "探针退出后仍有非僵尸成员",
                                "processes": list(command_result.residual_processes),
                                "cleanup": "failed" if command_result.cleanup_failed else "terminated"}]
                              if command_result.residual_processes else [])
        if command_result.cleanup_failed:
            raise ValueError("SIGKILL 后自有进程组仍有非僵尸成员，或未能在有界等待内确认结束")
        if command_result.timed_out:
            raise ValueError("超过 deadline；已执行有界清理（SIGINT，必要时 SIGKILL）")
        if command_result.exit_code != 0:
            raise ValueError(f"探针非零退出：{command_result.exit_code}")
        if step.evidence != "exit-only":
            payload = json.loads(paths[step.evidence_source].read_text(encoding="utf-8"))
            evidence_check(step, payload, paths["stderr"].read_text(encoding="utf-8"), sdk_version)
            if step.evidence_source == "stdout":
                paths["json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if step.name == "sdk-models":
                entries = payload.get("models", {}).get("models")
                if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
                    raise ValueError("models.models 缺失或无效")
                defaults = [entry for entry in entries if entry.get("is_default") is True]
                if len(defaults) != 1 or any(not isinstance(defaults[0].get(key), str)
                                             or not defaults[0][key].strip() for key in ("id", "model")):
                    raise ValueError("models.models 必须有唯一默认条目，且 id 与 model 均为非空字符串")
                result.update(default_model_id=defaults[0]["id"], default_model=defaults[0]["model"])
        result["status"] = "passed"
    except KeyboardInterrupt:
        result.update(status="interrupted", reason="运行被中断；已等待探针有界清理，停止后续步骤")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        result["reason"] = f"{type(error).__name__}: {error}；需要人工检查证据"
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def exit_code(results: list[dict[str, Any]]) -> int:
    return int(not any(row["status"] not in {"not_selected", "skipped", "not_run"} for row in results)
               or any(row["status"] not in {"passed", "not_selected", "skipped"}
                   or (row["status"] == "skipped" and not row.get("reason", "").strip()) for row in results))


def run_suite(steps: tuple[Step, ...], *, output_dir: Path, cwd: Path, sdk_version: str,
              live: bool = False, groups: set[str] | None = None, only: set[str] | None = None,
              skips: dict[str, str] | None = None, model: str | None = None,
              interruption: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    if interruption is not None:
        return _run_suite(steps, output_dir=output_dir, cwd=cwd, sdk_version=sdk_version, live=live,
                          groups=groups, only=only, skips=skips, model=model, interruption=interruption)
    with interruption_signals() as interruption:
        return _run_suite(steps, output_dir=output_dir, cwd=cwd, sdk_version=sdk_version, live=live,
                          groups=groups, only=only, skips=skips, model=model, interruption=interruption)


def _run_suite(steps, *, output_dir, cwd, sdk_version, live, groups, only, skips, model, interruption):
    rows = []
    interrupted = False
    for step in steps:
        reason = None
        if (groups and step.group not in groups) or (only and step.name not in only):
            reason = "未在 --group/--only 选择范围"
        elif step.live and not live and not (skips and step.name in skips):
            reason = "真实账号步骤需要显式 --live"
        if interrupted or interruption["signal"] is not None:
            row = {"name": step.name, "group": step.group, "status": "not_run", "reason": "先前步骤被中断"}
        elif reason:
            row = {"name": step.name, "group": step.group, "status": "not_selected", "reason": reason}
        elif skips and step.name in skips:
            row = {"name": step.name, "group": step.group, "status": "skipped", "reason": skips[step.name]}
        else:
            print(f"[已知回归集] {step.name}: started", flush=True)
            interruption["running"] = True
            try:
                row = run_step(step, output_dir, cwd, model, sdk_version)
            finally:
                interruption["running"] = False
            if interruption["signal"] is not None:
                row.update(status="interrupted", reason=f"收到信号 {interruption['signal']}；已完成有界清理，停止后续步骤")
            interrupted = row["status"] == "interrupted"
            if not model and row["status"] == "passed":
                model = row.get("default_model")
            print(f"[已知回归集] {step.name}: {row['status']} {row.get('reason', '')}", flush=True)
        # Runner metadata is separate from the probe's success evidence. Even a
        # skipped, precondition-failed or exit-only step gets a JSON receipt;
        # never fabricate a missing probe JSON or a passed field.
        row["result_file"] = f"{step.name}.result.json"
        (output_dir / row["result_file"]).write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rows.append(row)
    return rows, model


def regression_source_evidence() -> dict[str, Any]:
    return {**source_evidence(), "tracked_diff_stat": subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--stat", "HEAD", "--"], check=True,
        capture_output=True, timeout=10).stdout.decode()}


def write_summary(output_dir: Path, report: dict[str, Any]) -> None:
    counts = Counter(row["status"] for row in report["steps"])
    report["counts"] = {key: counts[key] for key in ("passed", "failed", "skipped", "not_selected", "interrupted", "not_run")}
    report["exit_code"] = int(bool(exit_code(report["steps"]) or report.get("interruption", {}).get("signal")))
    report["notice"] = FOOTER
    report["source_changed_during_run"] = any(report.get(key) != report["source_after"].get(key)
                                               for key in ("source_commit", "source_diff_sha256"))
    report["executed_steps"] = sum(row["status"] not in {"not_selected", "skipped", "not_run"} for row in report["steps"])
    report["warnings"] = []
    report["residual_warnings"] = [{"step": row["name"], **warning}
                                   for row in report["steps"] for warning in row.get("warnings", [])]
    for warning in report["residual_warnings"]:
        report["warnings"].append(f"残留进程警告：{warning['step']}；清理结果 {warning['cleanup']}")
    if report["source_changed_during_run"]:
        report["warnings"].append("运行期间源码变化，不能作为 exact commit 证据")
    if not report["executed_steps"]:
        report["warnings"].append("没有执行任何步骤")
    (output_dir / "SUMMARY.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["已知回归集", ""]
    for warning in report["warnings"]:
        lines += [f"**{warning}**", ""]
    for warning in report["residual_warnings"]:
        for process in warning["processes"]:
            lines.append(f"- {warning['step']}: {process['comm']} — {process['commandline']}")
    if report["residual_warnings"]:
        lines.append("")
    if report["source_dirty"] or report["source_after"]["source_dirty"]:
        lines += ["**DIRTY：不能作为 exact commit 证据**", ""]
    lines += [f"commit: {report['source_commit']}", f"SDK: {report['sdk_version']} / CLI: {report['cli_version']}",
              f"model: {report['model'] or '未取得'}", json.dumps(report["counts"], ensure_ascii=False), ""]
    for row in report["steps"]:
        lines.append(f"- {row['name']}: {row['status']} {row.get('reason', '')} {row.get('weakness') or ''}".rstrip())
    lines += ["", FOOTER]
    readable = "\n".join(lines) + "\n"
    (output_dir / "SUMMARY.md").write_text(readable, encoding="utf-8")
    print(readable, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--group", action="append", choices=sorted({step.group for step in REGISTRY}))
    parser.add_argument("--only", help="NAME[,NAME]; intersects --group when both are given")
    parser.add_argument("--skip", action="append", default=[], metavar="NAME=原因")
    parser.add_argument("--model")
    parser.add_argument("--output-dir", type=Path, help="new or empty evidence directory (never reuse old evidence)")
    parser.add_argument("--list", action="store_true", help="print the registry and exclusions without running probes")
    args = parser.parse_args(argv)
    names = {step.name for step in REGISTRY}
    only = set(args.only.split(",")) if args.only is not None else None
    if only is not None and not only <= names:
        parser.error(f"未知步骤：{sorted(only - names)}")
    skips: dict[str, str] = {}
    for value in args.skip:
        name, separator, reason = value.partition("=")
        if not separator or name not in names or not reason.strip() or name in skips:
            parser.error("--skip 必须为唯一的已知 NAME=非空原因")
        if (only and name not in only) or (args.group and next(s.group for s in REGISTRY if s.name == name) not in args.group):
            parser.error(f"--skip {name} 不在选择范围")
        skips[name] = reason.strip()
    if args.model is not None and not args.model.strip():
        parser.error("--model 不得为空")
    if args.list:
        print(json.dumps({"steps": [asdict(step) for step in REGISTRY], "excluded_probes": EXCLUDED_PROBES}, ensure_ascii=False, indent=2))
        return 0
    unauthorized = [step.name for step in REGISTRY if step.live and step.name not in skips
                    and (not only or step.name in only) and (not args.group or step.group in args.group)]
    if not args.live and (only is not None or args.group) and unauthorized:
        parser.error(f"显式选择的真实账号步骤需要 --live：{', '.join(unauthorized)}")
    try:
        with interruption_signals() as interruption:
            sdk = importlib.metadata.version("openai-codex")
            cli = importlib.metadata.version("openai-codex-cli-bin")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            output_dir = (args.output_dir or ROOT / "docs/research/compatibility" / f"upgrade-regression-{sdk}-{stamp}").resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            if any(output_dir.iterdir()):
                raise ValueError("--output-dir 必须为空，避免旧证据被误判")
            report = {**regression_source_evidence(), "sdk_version": sdk, "cli_version": cli,
                      "platform": platform.platform(), "python": platform.python_version(), "started_utc": stamp,
                      "selection": {"live": args.live, "groups": args.group, "only": sorted(only) if only else None, "skips": skips}}
            with tempfile.TemporaryDirectory(prefix="netizen-upgrade-regression-") as raw:
                cwd = Path(raw).resolve()
                subprocess.run(["git", "-C", str(cwd), "init", "--quiet"], check=True, capture_output=True, timeout=10)
                rows, model = run_suite(REGISTRY, output_dir=output_dir, cwd=cwd, sdk_version=sdk,
                                        live=args.live, groups=set(args.group or ()), only=only, skips=skips, model=args.model, interruption=interruption)
            report.update(steps=rows, model=model, interruption=interruption, source_after=regression_source_evidence(),
                          disposable_cwd_removed=not cwd.exists(), finished_utc=datetime.now(timezone.utc).isoformat())
            write_summary(output_dir, report)
            return report["exit_code"]
    except (OSError, ValueError, subprocess.SubprocessError, importlib.metadata.PackageNotFoundError) as error:
        print(f"已知回归集无法执行：{error}；需要人工处理", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
