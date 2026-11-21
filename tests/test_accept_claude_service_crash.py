"""Acceptance-helper tests use only synthetic process identities and temporary files."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from netizen_cli.cli_services import ServiceManager
from netizen_cli.instance import INSTANCE_ROOT_MARKER_CONTENT
from scripts import accept_claude_service_crash as kit


READY = b"2026-01-01 01:02:03,456 INFO __main__ netizen service ready\n"


class CrashKitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.tmp).resolve()
        self.root = self.home / "instance"
        self.root.mkdir()
        (self.root / "state").mkdir()

    def test_both_explicit_flags_required_and_no_service_discovery_on_refusal(self):
        with patch.object(kit, "ServiceManager") as manager, contextlib.redirect_stderr(io.StringIO()):
            for args in ([], ["--yes-crash"], ["--root", str(self.root)], ["--root", str(self.root), "--yes"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as refused:
                    kit.main(args)
                self.assertEqual(refused.exception.code, 2)
            manager.assert_not_called()

    def test_codex_root_refused_before_manager_or_process_lookup(self):
        for name, content in ((".netizen-root", INSTANCE_ROOT_MARKER_CONTENT), ("config.yaml", b"instance: {}\n")):
            path = self.root / name
            path.write_bytes(content)
            path.chmod(0o600)
        out = io.StringIO()
        with patch.object(kit, "ServiceManager") as manager, contextlib.redirect_stdout(out):
            self.assertEqual(kit.main(["--root", str(self.root), "--yes-crash"]), 1)
        manager.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["reason"], "not_a_claude_instance")

    def test_linux_follows_children_of_every_thread_and_recurses(self):
        procs = {pid: Mock(pid=pid) for pid in (10, 20, 30, 40)}
        for parent, tid, kids in ((10, 10, ""), (10, 11, "20 30"), (20, 21, "40")):
            task = self.home / str(parent) / "task" / str(tid)
            task.mkdir(parents=True)
            (task / "children").write_text(kids)
            for child in kids.split():
                procs[int(child)].ppid.return_value = parent
        with patch.object(kit.psutil, "Process", side_effect=procs.__getitem__):
            self.assertEqual(kit.descendants(procs[10], "linux", self.home), [procs[i] for i in (20, 30, 40)])

    def test_mac_uses_psutil_and_recognizes_the_exact_bundled_executable(self):
        main, cli, shell, other = (Mock() for _ in range(4))
        binary = self.home / "bundled/claude"
        main.children.return_value = [cli, shell, other]
        cli.exe.return_value, shell.exe.return_value, other.exe.return_value = str(binary), "/bin/sh", "/elsewhere/claude"
        self.assertEqual(kit.snapshot(main, "darwin", binary), {cli: "cli", shell: "shell", other: "tool"})
        main.children.assert_called_once_with(recursive=True)

    def test_signal_refused_for_wrong_root_identity_recycled_pid_or_finished_cli(self):
        main = Mock(pid=1001)
        root = self.root
        manager = Mock(platform="darwin")
        name = manager.service_name.return_value = "selected"
        manager._launchd_jobs.return_value = {name: main.pid, "unrelated": 1002}
        for case in ("wrong_root", "identity", "recycled_pid", "finished_cli"):
            with self.subTest(case=case):
                manager.inspect.return_value = SimpleNamespace(ready=True, binding=SimpleNamespace(
                    root=root.parent if case == "wrong_root" else root))
                manager._identity_matches.return_value = case != "identity"
                current = Mock(pid=main.pid) if case == "recycled_pid" else main
                with (patch.object(kit.psutil, "Process", return_value=current),
                      patch.object(kit, "snapshot", return_value={}), self.assertRaises(kit.EvidenceError)):
                    kit.crash_main(manager, root, main, Path("/fixture/claude"), {Mock()})
        main.kill.assert_not_called()

    def test_transcript_growth_accounts_for_timestamps_new_files_and_nested_records(self):
        path = self.home / "native.jsonl"
        path.write_bytes(b'{"type":"assistant","content":"private baseline"}\n')
        before = kit.baseline([self.home])
        with path.open("ab") as stream:
            stream.write(b'{"type":"assistant","timestamp":"1970-01-01T00:00:09Z"}\n')
            stream.write(b'{"type":"user","content":"assistant api-request"}\n')
        self.assertEqual(kit.transcript_delta({path}, before, 10), ([2], {"api-request": False, "assistant": False}))
        nested = self.home / "session/subagents/agent.jsonl"
        nested.parent.mkdir(parents=True)
        nested.write_text('{"type":"system","subtype":"api-request","timestamp":"1970-01-01T00:00:11Z"}\n'
                          '{"type":"assistant","timestamp":"1970-01-01T00:00:12Z"}\n')
        self.assertEqual(kit.transcript_delta({path}, before, 10), ([2, 2], {"api-request": True, "assistant": True}))
        path.unlink()
        with self.assertRaisesRegex(kit.EvidenceError, "missing_baseline"):
            kit.transcript_delta({path}, before, 10)

    def test_initially_empty_project_needs_positive_evidence_afterward(self):
        project = self.home / "new-project"
        active = project / "active.jsonl"
        before = kit.baseline([project])
        self.assertEqual(before, {})
        with self.assertRaisesRegex(kit.EvidenceError, "unobserved_active"):
            kit.transcript_delta({active}, before, 10)
        project.mkdir()
        active.write_text('{"type":"api-request"}\n')
        growth, activity = kit.transcript_delta({active}, before, 10)
        self.assertEqual(growth, [1])
        self.assertIsNone(activity["api-request"])
        self.assertEqual(kit.verdict([.1], 1, 1.1, 5, activity), "unconfirmed_transcript_time")

    def test_fresh_complete_ready_record_required_even_across_log_rotation(self):
        log = self.root / "state/netizen.log"
        log.write_bytes(READY)
        stat = log.stat()
        cursor = kit.Cursor(stat.st_dev, stat.st_ino, stat.st_size)
        self.assertFalse(kit.ready_since(log, cursor)[0])
        log.rename(log.with_suffix(".old"))
        log.write_bytes(b"unrelated netizen service ready\n" + READY[:-1])
        seen, cursor = kit.ready_since(log, cursor)
        self.assertFalse(seen)
        with log.open("ab") as source:
            source.write(b"\n")
        self.assertTrue(kit.ready_since(log, cursor)[0])

    def test_verdict_requires_all_cli_exits_and_both_kinds_of_transcript_evidence(self):
        clean = {"api-request": False, "assistant": False}
        self.assertEqual(kit.verdict([.5, 2.9], 1, 1.1, 4, clean), "pass")
        for exits in ([], [None], [3.05], [.4, 3]):
            self.assertNotEqual(kit.verdict(exits, 1, 1.1, 4, clean), "pass")
        self.assertNotEqual(kit.verdict([.1], None, None, 45, clean), "pass")
        self.assertNotEqual(kit.verdict([.1], 1, 1.1, 1.5, clean), "pass")
        for kind in clean:
            self.assertEqual(kit.verdict([.5], 1, 1.1, 4, clean | {kind: True}), "post_kill_model_activity")

    def test_complete_run_on_both_managers_only_signals_original_main(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                self.simulate(platform)

    def simulate(self, platform):
        config = self.root / "config.yaml"
        config.write_text("instance:\n  backend: claude\n")
        marker = self.root / ".netizen-root"
        marker.write_bytes(INSTANCE_ROOT_MARKER_CONTENT)
        for private in (marker, config):
            private.chmod(0o600)
        log = self.root / "state/netizen.log"
        log.write_bytes(READY)
        session = str(uuid4())
        config_dir = self.home / platform
        from claude_agent_sdk import project_key_for_directory
        cwd = self.home / "private-project"
        transcript = config_dir / "projects" / project_key_for_directory(cwd) / (session + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(b'{"type":"user","content":"private body"}\n')

        manager = ServiceManager(home=self.home, platform=platform)
        service_name = manager.service_name(self.root)
        old, new, cli, tool = (Mock(pid=n) for n in (7001, 7002, 7003, 7004))
        cli.cmdline.return_value = ["claude", "--resume=" + session]
        cli.cwd.return_value = str(cwd)
        clock = SimpleNamespace(elapsed=0., killed=False)
        def tick(seconds):
            previous = clock.elapsed
            clock.elapsed += seconds
            if previous < 1 <= clock.elapsed:
                with log.open("ab") as out:
                    out.write(READY)
                with transcript.open("ab") as out:
                    out.write(b'{"type":"system","content":"private marker"}\n')
        def kill(*_args):
            self.assertFalse(clock.killed)
            clock.killed = True
        def pid():
            return old.pid if not clock.killed else (None if clock.elapsed < .3 else new.pid)
        def alive(process):
            if process is cli:
                return clock.elapsed < .6
            return True
        stdout = io.StringIO()
        with (patch.object(kit, "ServiceManager", return_value=manager),
              patch.object(manager, "inspect", return_value=SimpleNamespace(ready=True, binding=SimpleNamespace(root=self.root))),
              patch.object(manager, "_identity_matches", return_value=True),
              patch.object(manager, "_systemd_properties", side_effect=lambda root: {"MainPID": str(pid() or 0)}) as systemd,
              patch.object(manager, "_launchd_jobs", side_effect=lambda: {service_name: pid(), "other": 8888}) as launchd,
              patch.object(kit.psutil, "Process", side_effect={old.pid: old, new.pid: new}.__getitem__),
              patch.object(kit, "verified_claude_cli", return_value=self.home / "bundled/claude"),
              patch.object(kit, "snapshot", return_value={cli: "cli", tool: "tool"}),
              patch.object(kit, "live", side_effect=alive),
              patch.object(kit, "time", SimpleNamespace(monotonic=lambda: clock.elapsed, time=lambda: 10., sleep=tick)),
              patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}),
              patch.object(kit.os, "pidfd_open", side_effect=lambda p: os.open(os.devnull, os.O_RDONLY), create=True) as fdopen,
              patch.object(kit.signal, "pidfd_send_signal", side_effect=kill, create=True) as send_signal,
              patch.object(old, "kill", side_effect=kill) as kill_old,
              contextlib.redirect_stdout(stdout)):
            self.assertEqual(kit.main(["--root", str(self.root), "--yes-crash", "--delay", "0",
                                       "--observe-seconds", "4"]), 0, stdout.getvalue())
        if platform == "linux":
            fdopen.assert_called_once_with(old.pid)
            send_signal.assert_called_once()
            self.assertEqual(send_signal.call_args.args[1], signal.SIGKILL)
            kill_old.assert_not_called()
            launchd.assert_not_called()
            self.assertTrue(systemd.called)
        else:
            kill_old.assert_called_once()
            systemd.assert_not_called()
            fdopen.assert_not_called()
            self.assertTrue(launchd.called)
        cli.kill.assert_not_called()
        tool.kill.assert_not_called()
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        by_event = {row["event"]: row for row in events}
        self.assertEqual(by_event["kill"]["main"], old.pid)
        self.assertEqual(by_event["new_main"]["pid"], new.pid)
        self.assertEqual(by_event["exited"]["pid"], cli.pid)
        self.assertEqual(by_event["survivors"]["tool_processes"], [{"pid": tool.pid, "role": "tool"}])
        self.assertEqual(by_event["transcripts"]["line_growth"], [1])
        self.assertEqual(by_event["ready"]["admission_after_kill"], by_event["ready"]["after_kill"] + 2)
        self.assertEqual(events[-1]["verdict"], "PASS")
        for private in (str(self.root), session, "private body", "private marker", "private-project"):
            self.assertNotIn(private, stdout.getvalue())
