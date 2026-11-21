"""Measure one explicitly requested managed Claude service crash. Emit JSONL only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
from stat import S_ISREG
import sys
import time
from uuid import UUID

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from netizen_cli.backends import instance_backend
from netizen_cli.claude_installation import verified_claude_cli
from netizen_cli.cli_services import ServiceManager
from netizen_cli.cli_setup import read_setup_config
from netizen_cli.instance import require_instance_root_marker, resolve_instance_root


GRACE = 2.0
POLL = 0.1
READY_LINE = re.compile(rb"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO (?:__main__|netizen_cli\.main) netizen service ready(?: projects=\d+)?\r?\n$")

GRACE_LINE = re.compile(rb"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO netizen_cli\.runtime\.claude_ordinary "
                        rb"Claude admission grace started pid=(\d+) monotonic=([\d.e+-]+) deadline=([\d.e+-]+)\r?\n$")
SLEEP_ARGS = ["python3", "-c", "import time; time.sleep(40)"]


class EvidenceError(Exception):
    """Only fixed diagnostic codes from the helper may be printed."""


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def main_pid(manager, root):
    if manager.platform == "darwin":
        return manager._launchd_jobs().get(manager.service_name(root))
    return int(manager._systemd_properties(root)["MainPID"]) or None


def verified_main(manager, root):
    status = manager.inspect(root)
    pid = main_pid(manager, root)
    if not (status and status.binding.root == root and status.ready and pid and pid > 1
            and manager._identity_matches(status.binding, pid)):
        raise EvidenceError("unconfirmed_service_identity")
    return psutil.Process(pid)


def live(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def descendants(process, platform, proc=Path("/proc")):
    if platform == "darwin":
        return process.children(recursive=True)
    # /proc/<pid>/task/<tid>/children contains only that thread's direct children.
    found = set()
    pending = [process]
    while pending:
        parent = pending.pop()
        with suppress(FileNotFoundError):
            for task in (proc / str(parent.pid) / "task").iterdir():
                try:
                    children = (task / "children").read_text().split()
                except FileNotFoundError:
                    if task.exists():
                        raise EvidenceError("thread_children_unavailable") from None
                    continue
                for pid in map(int, children):
                    with suppress(psutil.NoSuchProcess):
                        child = psutil.Process(pid)
                        if child not in found and child.ppid() == parent.pid:
                            found.add(child)
                            pending.append(child)
    return sorted(found, key=lambda p: p.pid)


def snapshot(main, platform, cli):
    tree = {}
    for child in descendants(main, platform):
        with suppress(psutil.NoSuchProcess):
            executable = Path(child.exe())
            if executable.resolve() == cli:
                role = "cli"
            elif executable.name in {"sh", "bash", "zsh", "dash", "fish", "ksh"}:
                role = "shell"
            else:
                role = "tool"
            tree[child] = role
    return tree


def cli_session_id(cli):
    ids = [arg.partition("=")[2] for arg in cli.cmdline() if arg.startswith(("--resume=", "--session-id="))]
    if len(ids) != 1 or str(UUID(ids[0])) != ids[0]:
        raise EvidenceError("missing_session_identity")
    return ids[0]


def session_transcripts(clis, config_dir):
    from claude_agent_sdk import get_session_info, project_key_for_directory

    active = set()
    for cli in clis:
        session_id = cli_session_id(cli)
        projects = config_dir / "projects"
        cwd = cli.cwd()
        expected = projects / project_key_for_directory(cwd) / f"{session_id}.jsonl"
        # A very long native project name can have a platform-specific hash.
        # An existing exact session file identifies its directory in that case.
        if not expected.exists():
            matches = list(projects.glob(f"*/{session_id}.jsonl"))
            if len(matches) > 1:
                raise EvidenceError("ambiguous_session_transcript")
            if matches:
                info = get_session_info(session_id, directory=cwd)
                if info is None or info.session_id != session_id or not info.cwd or Path(info.cwd).resolve() != Path(cwd).resolve():
                    raise EvidenceError("unconfirmed_transcript_project")
                expected = matches[0]
        active.add(expected)
    return active


@contextmanager
def open_evidence(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not S_ISREG(os.fstat(fd).st_mode):
            raise EvidenceError("evidence_is_not_a_regular_file")
        yield source


@dataclass(frozen=True)
class Cursor:
    device: int
    inode: int
    offset: int


def files(directories):
    # Include nested native subagent transcripts. Propagate unreadable directories.
    def unreadable(error):
        raise error
    for project in sorted(directories):
        if not project.exists():
            continue  # A first execution may not have written its transcript yet.
        for parent, _, names in os.walk(project, onerror=unreadable):
            for name in sorted(names):
                if name.endswith(".jsonl"):
                    yield Path(parent) / name


def baseline(directories):
    cursors = {}
    for path in files(directories):
        with open_evidence(path) as source:
            stat = os.fstat(source.fileno())
            if stat.st_size:
                source.seek(stat.st_size - 1)
                if source.read(1) != b"\n":
                    raise EvidenceError("incomplete_transcript_before_kill")
            cursors[path] = Cursor(stat.st_dev, stat.st_ino, stat.st_size)
    return cursors


def transcript_delta(active, before, killed_wall):
    directories = {path.parent for path in active}
    growth = []
    after_kill = {"api-request": False, "assistant": False}
    seen = set()
    for path in files(directories):
        seen.add(path)
        with open_evidence(path) as source:
            stat = os.fstat(source.fileno())
            cursor = before.get(path)
            if cursor is not None:
                if ((stat.st_dev, stat.st_ino) != (cursor.device, cursor.inode) or stat.st_size < cursor.offset):
                    raise EvidenceError("replaced_or_truncated_transcript")
                source.seek(cursor.offset)
            lines = 0
            for raw in source:
                if not raw.endswith(b"\n"):
                    raise EvidenceError("incomplete_transcript_after_kill")
                record = json.loads(raw)
                kind = record["type"]
                if kind == "system" and record.get("subtype") == "api-request":
                    kind = "api-request"
                if kind in after_kill and after_kill[kind] is not True:
                    try:
                        stamp = datetime.fromisoformat(record.get("timestamp", ""))
                        if stamp.tzinfo is None:
                            raise ValueError
                    except (TypeError, ValueError):
                        after_kill[kind] = None  # An append can be a delayed pre-crash write.
                    else:
                        # Native time has millisecond precision. Include the boundary millisecond.
                        if stamp.timestamp() >= math.floor(killed_wall * 1000) / 1000:
                            after_kill[kind] = True
                lines += 1
            growth.append(lines)
    if before.keys() - seen:
        raise EvidenceError("missing_baseline_transcript")
    if active - seen:
        raise EvidenceError("unobserved_active_transcript")
    return growth, after_kill


def ready_since(path, cursor):
    with open_evidence(path) as source:
        stat = os.fstat(source.fileno())
        # A rotated log begins at byte zero. Never reread old ready lines in the same log.
        if (stat.st_dev, stat.st_ino) == (cursor.device, cursor.inode):
            if stat.st_size < cursor.offset:
                raise EvidenceError("truncated_service_log")
            source.seek(cursor.offset)
        ready = False
        grace = []
        offset = source.tell()
        for line in source:
            if not line.endswith(b"\n"):
                break
            ready |= READY_LINE.fullmatch(line) is not None
            if match := GRACE_LINE.fullmatch(line):
                grace.append((int(match[1]), float(match[2]), float(match[3])))
            offset += len(line)
        return ready, grace, Cursor(stat.st_dev, stat.st_ino, offset)


def pending_foreground_call(cli):
    from claude_agent_sdk import get_session_messages
    session_id = cli_session_id(cli)
    pending = set()
    for msg in get_session_messages(session_id, directory=cli.cwd()):
        if msg.session_id != session_id or not isinstance(msg.message, dict):
            continue
        blocks = msg.message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                pending.discard(block.get("tool_use_id"))
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                args = block.get("input", {})
                if isinstance(args, dict) and args.get("run_in_background", False) is False and block.get("id"):
                    if not isinstance(args.get("command"), str):
                        continue
                    try:
                        command = shlex.split(args["command"])
                    except ValueError:
                        continue
                    if command == SLEEP_ARGS:
                        pending.add(block["id"])
    return len(pending) == 1


def foreground_target(clis, platform):
    for cli in clis:
        with suppress(psutil.NoSuchProcess):
            matches = []
            for child in descendants(cli, platform):
                with suppress(psutil.NoSuchProcess):
                    argv = child.cmdline()
                    if (len(argv) == 3 and Path(argv[0]).name == SLEEP_ARGS[0] and argv[1:] == SLEEP_ARGS[1:]
                            and live(child)):
                        matches.append(child)
            if len(matches) == 1 and pending_foreground_call(cli):
                return cli, matches[0]
    return None


def crash_main(manager, root, expected, executable, clis, foreground):
    if manager.platform == "linux":
        if not callable(getattr(os, "pidfd_open", None)) or not callable(getattr(signal, "pidfd_send_signal", None)):
            raise EvidenceError("stable_signal_target_unavailable")
        try:
            fd = os.pidfd_open(expected.pid)
        except OSError:
            raise EvidenceError("stable_signal_target_unavailable") from None
    elif manager.platform == "darwin":
        fd = None
    else:
        raise EvidenceError("stable_signal_target_unavailable")
    try:
        if verified_main(manager, root) != expected:
            raise EvidenceError("service_changed_before_kill")
        tree = snapshot(expected, manager.platform, executable)
        if not clis or {p for p, role in tree.items() if role == "cli"} != clis:
            raise EvidenceError("execution_changed_before_kill")
        if (not foreground or foreground[0] not in clis or foreground[1] not in tree
                or foreground_target({foreground[0]}, manager.platform) != foreground):
            raise EvidenceError("foreground_task_not_covered")
        if main_pid(manager, root) != expected.pid or not live(expected):
            raise EvidenceError("service_changed_before_kill")
        mono, wall = time.monotonic(), time.time()
        if fd is None:
            manager._launchctl("kill", "SIGKILL", f"gui/{manager.uid}/{manager.service_name(root)}")
        else:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        return mono, wall, tree
    finally:
        if fd is not None:
            os.close(fd)


def verdict(cli_exits, admission, observed, activity):
    if any(activity.values()):
        return "post_kill_model_activity"
    if any(activity.get(kind) is not False for kind in ("api-request", "assistant")):
        return "unconfirmed_transcript_time"
    if admission is None or observed < admission:
        return "admission_not_observed"
    # An exit first observed at or after admission cannot prove the old CLI had already exited.
    if not cli_exits or any(t is None or t >= admission for t in cli_exits):
        return "cli_exit_before_admission_unproven"
    return "pass"


def observe(manager, root, old, tree, log, cursor, killed, window):
    exits = {}
    replacement = None
    stable = True
    ready = False
    windows = []
    while True:
        for child, role in tree.items():
            if child not in exits and not live(child):
                exits[child] = time.monotonic() - killed
                emit("exited", pid=child.pid, role=role, after_kill=round(exits[child], 3))
        pid = main_pid(manager, root)
        if replacement is not None and pid != replacement:
            stable = False
        if pid and pid != old.pid and replacement is None:
            replacement = pid
            emit("new_main", pid=pid, after_kill=round(time.monotonic() - killed, 3))
        line_ready, grace, cursor = ready_since(log, cursor)
        ready |= line_ready
        windows.extend(record for record in grace if record[1] >= killed)
        elapsed = time.monotonic() - killed
        if elapsed >= window:
            break
        time.sleep(min(POLL, window - elapsed))
    survivors = [{"pid": child.pid, "role": role} for child, role in tree.items() if child not in exits]
    emit("survivors", cli_pids=[p["pid"] for p in survivors if p["role"] == "cli"],
         tool_processes=[p for p in survivors if p["role"] != "cli"])
    cli_exits = [exits.get(child) for child, role in tree.items() if role == "cli"]
    admission = None
    if stable and ready and len(windows) == 1:
        pid, started, deadline = windows[0]
        if pid == replacement and math.isfinite(deadline) and deadline == started + GRACE:
            admission = deadline - killed
            emit("ready", grace_started_after_kill=round(started - killed, 3), admission_after_kill=round(admission, 3))
    return cli_exits, admission, elapsed, replacement if stable else None


def run(args):
    root = resolve_instance_root(args.root)
    require_instance_root_marker(root)
    config = read_setup_config(root / "config.yaml")
    if not config or instance_backend(config["instance"]) != "claude":
        raise EvidenceError("not_a_claude_instance")
    manager = ServiceManager()
    old = verified_main(manager, root)
    executable = verified_claude_cli().resolve()
    emit("waiting_for_execution", platform=manager.platform, main=old.pid)
    deadline = time.monotonic() + args.wait_timeout
    while live(old):
        initial = {p for p, role in snapshot(old, manager.platform, executable).items() if role == "cli"}
        if initial:
            break
        if time.monotonic() >= deadline:
            raise EvidenceError("no_execution_seen")
        time.sleep(POLL)
    else:
        raise EvidenceError("original_service_exited")
    emit("execution_seen", cli_pids=sorted(p.pid for p in initial))
    time.sleep(args.delay)
    tree = snapshot(old, manager.platform, executable)
    clis = {p for p, role in tree.items() if role == "cli"}
    if not initial.intersection(clis):
        raise EvidenceError("execution_ended_before_kill")
    while not (foreground := foreground_target(clis, manager.platform)):
        if time.monotonic() >= deadline or not live(old) or not any(live(cli) for cli in clis):
            raise EvidenceError("foreground_task_not_covered")
        time.sleep(POLL)
    emit("foreground_task_verified", cli=foreground[0].pid, task=foreground[1].pid)
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or manager.home / ".claude")
    active_transcripts = session_transcripts(clis, config_dir)
    before = baseline({path.parent for path in active_transcripts})
    log = root / "state/netizen.log"
    with open_evidence(log) as source:
        stat = os.fstat(source.fileno())
        cursor = Cursor(stat.st_dev, stat.st_ino, stat.st_size)
    killed, wall, tree = crash_main(manager, root, old, executable, clis, foreground)
    emit("kill", main=old.pid, after_kill=0, descendants=[{"pid": p.pid, "role": r} for p, r in tree.items()])
    exits, admission, observed, replacement = observe(manager, root, old, tree, log, cursor, killed, args.observe_seconds)
    growth, activity = transcript_delta(active_transcripts, before, wall)
    emit("transcripts", line_growth=growth, api_request_after_kill=activity["api-request"],
         assistant_after_kill=activity["assistant"])
    if replacement is None or verified_main(manager, root).pid != replacement:
        raise EvidenceError("replacement_service_unconfirmed")
    return verdict(exits, admission, observed, activity)


def seconds(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("expected finite nonnegative seconds")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", required=True, help="explicit root of the disposable Claude instance")
    parser.add_argument("--yes-crash", required=True, action="store_true", help="SIGKILL its verified service main PID")
    parser.add_argument("--delay", type=seconds, default=8.0, help="seconds after seeing the CLI (default 8)")
    parser.add_argument("--wait-timeout", type=seconds, default=600.0, help="seconds to wait for a CLI (default 600)")
    parser.add_argument("--observe-seconds", type=seconds, default=45.0, help="post-crash observation window (default 45)")
    args = parser.parse_args(argv)
    try:
        reason = run(args)
    except EvidenceError as exc:
        reason = str(exc)
    except Exception as exc:
        # Manager errors, filesystem exceptions and parse errors can include private data.
        reason = type(exc).__name__
    label = "PASS" if reason == "pass" else "FAIL"
    if reason in {"no_execution_seen", "execution_ended_before_kill", "foreground_task_not_covered"}:
        label = "NOT_COVERED"
    emit("verdict", verdict=label, reason=reason)
    return 0 if reason == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
