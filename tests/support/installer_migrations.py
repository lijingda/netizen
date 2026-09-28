"""Isolated installer scenarios with persistent fake service-manager state.

The fake never starts Netizen or contacts Feishu. Database transactions, recovery
files, admission evidence, release links, process death and flock are real.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from netizen import database_migrations as migrations
from netizen.deployment import activation_recovery
from netizen.deployment.installer_support import _capture_file, _restore_file
from netizen.deployment.service_backend import READY_MARKER_CONTENT
from netizen.migrations.v14 import validate as validate_v14
from scripts import netizen_installer as installer


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OLD = "1" * 64
CANDIDATE = "2" * 64
PREVIOUS = "3" * 64
OTHER = "4" * 64


def release(layout: installer.Layout, digest: str) -> installer.Release:
    root = layout.releases / digest
    return installer.Release(digest, root, root / "source", root / "venv")


@dataclass
class Scenario:
    root: Path
    layout: installer.Layout

    @classmethod
    def open(cls, root: Path) -> Scenario:
        home = root / "home"
        home.mkdir(parents=True, exist_ok=True)
        layout = installer.resolve_layout(
            environ={}, account_home=home, uid=os.geteuid(),
            username="test-user", platform_name="linux",
        )
        return cls(root, layout)

    @classmethod
    def create(cls, root: Path, *, running: bool = False) -> Scenario:
        scenario = cls.open(root)
        layout = scenario.layout
        installer.prepare_directories(layout)
        for digest in (OLD, CANDIDATE, PREVIOUS, OTHER):
            candidate = release(layout, digest)
            candidate.source.mkdir(parents=True)
            candidate.venv.mkdir()
        installer._set_release_link(layout.current, release(layout, OLD).root, layout)
        installer._set_release_link(layout.previous, release(layout, PREVIOUS).root, layout)
        installer._write_atomic(layout.service_file, b"old service definition\n", mode=0o600)
        with sqlite3.connect(scenario.database) as connection:
            connection.executescript((FIXTURES / "channel_v14.sql").read_text())
            connection.executescript((FIXTURES / "channel_v14_data.sql").read_text())
        scenario.manager_file.write_text(json.dumps({"loaded": running, "enabled": running}))
        return scenario

    @property
    def database(self) -> Path:
        return self.layout.state_dir / "channel.sqlite3"

    @property
    def manager_file(self) -> Path:
        return self.root / "fake-manager.json"

    @property
    def candidate(self) -> installer.Release:
        return release(self.layout, CANDIDATE)

    def recovery(self) -> activation_recovery.Recovery:
        intent = installer._read_activation_intent(self.layout)
        assert intent is not None and intent.recovery is not None
        return activation_recovery.load_recovery(self.layout, intent.recovery)

    def rows(self, query: str) -> list[tuple]:
        with sqlite3.connect(self.database) as connection:
            return connection.execute(query).fetchall()


@dataclass
class Fault:
    point: str = ""
    kill: bool = False
    used: bool = False

    def hit(self, point: str) -> None:
        if point != self.point or self.used:
            return
        self.used = True
        if self.kill:
            os.kill(os.getpid(), signal.SIGKILL)
            raise AssertionError("SIGKILL returned")
        raise installer.InstallError(f"injected {point} failure")


class StatefulBackend:
    def __init__(self, scenario: Scenario, fault: Fault) -> None:
        self.scenario = scenario
        self.layout = scenario.layout
        self.fault = fault
        self.events: list[str] = []

    def _set_state(self, **changes: bool) -> None:
        state = json.loads(self.scenario.manager_file.read_text())
        self.scenario.manager_file.write_text(json.dumps(state | changes))

    def inspect_state(self) -> installer.ServiceState:
        state = json.loads(self.scenario.manager_file.read_text())
        return installer.ServiceState(state["loaded"], state["enabled"])

    def capture_definition(self) -> installer.FileSnapshot:
        return _capture_file(self.layout.service_file)

    def render_definition(self, candidate: installer.Release) -> bytes:
        return f"candidate {candidate.digest}\n".encode()

    def stop_and_confirm(self, **_kwargs: object) -> None:
        self.events.append("stop")
        self._set_state(loaded=False)
        self.layout.ready_file.unlink(missing_ok=True)

    def publish_definition(self, content: bytes, *, should_enable: bool) -> None:
        self.events.append("publish")
        installer._write_atomic(self.layout.service_file, content, mode=0o600)
        self._set_state(enabled=should_enable)
        if self.fault.point in {"rollback_current_published", "rollback_main_restored"}:
            raise installer.InstallError("injected publish failure before interrupted rollback")
        self.fault.hit("publish")

    def restore_definition(self, snapshot: installer.FileSnapshot, *, should_enable: bool) -> None:
        self.events.append("restore")
        _restore_file(self.layout.service_file, snapshot)
        self._set_state(enabled=should_enable)

    def start_and_wait(self, *, timeout: float) -> None:
        current = installer._read_release_link(self.layout.current, self.layout)
        self.events.append(f"start:{current.name}")
        self._set_state(loaded=True)
        if current == self.scenario.candidate.root.resolve():
            self.fault.hit("before_admission")
            activation_recovery.mark_candidate_admission(self.layout.ready_file, current)
            # Represents a durable input written after the runtime admission
            # boundary; retries must preserve it and must not replay migration.
            with sqlite3.connect(self.scenario.database) as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO dedup_keys VALUES ('accepted-message', 2100000000)"
                )
            self.fault.hit("after_admission")
        installer._write_atomic(self.layout.ready_file, READY_MARKER_CONTENT, mode=0o600)
        if current == self.scenario.candidate.root.resolve():
            self.fault.hit("ready")
        else:
            self.fault.hit("old_service_ready")


@contextlib.contextmanager
def activation_environment(
    scenario: Scenario, *, point: str = "", kill: bool = False, target: int = 16,
    missing_path: bool = False,
) -> Iterator[StatefulBackend]:
    """Register future migrations only inside an isolated test's lifetime."""
    fault = Fault(point, kill)
    backend = StatefulBackend(scenario, fault)

    def to_fifteen(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE projects SET cwd = cwd || '/migrated'")
        fault.hit("migration_apply")

    def to_sixteen(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE schedule_plans SET name = name || ' (migrated)'")

    def validate_fifteen(connection: sqlite3.Connection) -> None:
        validate_v14(connection)
        if connection.execute("SELECT version FROM schema_version").fetchone()[0] != 15:
            raise RuntimeError("expected test schema v15")

    def validate_sixteen(connection: sqlite3.Connection) -> None:
        validate_v14(connection)
        if connection.execute("SELECT version FROM schema_version").fetchone()[0] != 16:
            raise RuntimeError("expected test schema v16")

    steps = (
        migrations.Migration(14, 15, to_fifteen, validate_fifteen),
        migrations.Migration(15, 16, to_sixteen, validate_sixteen),
    )
    selected = tuple(step for step in steps if step.to_version <= target)
    if missing_path:
        selected = selected[:1]
    migrate = installer.migrate_channel_database
    set_link = installer._set_release_link
    save_recovery = activation_recovery.Recovery.save
    copy_file = installer.shutil.copy2

    def migrate_and_fault(*args: object, **kwargs: object) -> dict:
        result = migrate(*args, **kwargs)
        fault.hit("migration_committed")
        return result

    def set_link_and_fault(link: Path, target_path: Path | None, layout: installer.Layout) -> None:
        set_link(link, target_path, layout)
        if link == layout.current and target_path == scenario.candidate.root:
            fault.hit("current_published")
        if link == layout.current and target_path == release(layout, OLD).root:
            fault.hit("rollback_current_published")

    def save_recovery_and_fault(recovery: activation_recovery.Recovery, **changes: object) -> None:
        save_recovery(recovery, **changes)
        if changes.get("phase") == "restoring_source":
            fault.hit("restoring_source")

    def copy_and_fault(source: Path, destination: Path, **kwargs: object) -> object:
        result = copy_file(source, destination, **kwargs)
        if Path(destination) == scenario.database:
            fault.hit("rollback_main_restored")
        return result

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(migrations, "SCHEMA_VERSION", target))
        stack.enter_context(patch.object(migrations, "MIGRATIONS", selected))
        stack.enter_context(patch.object(installer, "SCHEMA_VERSION", target))
        stack.enter_context(patch.object(installer, "_service_backend", return_value=backend))
        stack.enter_context(patch.object(installer, "migrate_channel_database", side_effect=migrate_and_fault))
        stack.enter_context(patch.object(installer, "_set_release_link", side_effect=set_link_and_fault))
        stack.enter_context(patch.object(activation_recovery.Recovery, "save", save_recovery_and_fault))
        stack.enter_context(patch.object(installer.shutil, "copy2", side_effect=copy_and_fault))
        yield backend


def activate(scenario: Scenario, *, digest: str = CANDIDATE) -> None:
    with installer.installation_lock(scenario.layout):
        installer.activate_release(
            release(scenario.layout, digest), scenario.layout, interactive=False,
            data_dir=scenario.layout.state_dir,
        )


if __name__ == "__main__":
    scenario = Scenario.open(Path(sys.argv[1]))
    with activation_environment(scenario, point=sys.argv[2], kill=True):
        activate(scenario)
