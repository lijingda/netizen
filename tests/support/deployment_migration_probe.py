#!/usr/bin/env python3
"""Opt-in real user-service probe for the installation migration boundary.

This runs a minimal service fixture, not the Netizen runtime or SDK. It never
reads application credentials or sends messages. Each instance is confined to
a new temporary workspace; only its unique user-service definition lives outside
that workspace. Run with the current account's real LaunchAgent/systemd manager.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import time
import traceback


SERVICE_FIXTURE = r'''
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0, SOURCE_ROOT)
from scripts.netizen_service_launcher import acquire_lifetime_lock, clear_ready_marker
from netizen.deployment.activation_recovery import mark_candidate_admission
from netizen.deployment.installer_support import _write_atomic
from netizen.deployment.service_backend import READY_MARKER_CONTENT

release = Path(__file__).resolve().parents[2]
root = Path(os.environ["NETIZEN_ROOT"])
ready = root / "state/service.ready"
descriptor = acquire_lifetime_lock(root / "state/service.lifetime.lock")
clear_ready_marker(ready)
mode = (root / "state" / f"fixture-mode-{release.name}").read_text().strip()
if mode == "before-admission":
    raise SystemExit(0)
if mode == "first-install":
    from netizen.bindings import BindingStore
    BindingStore(root / "state/channel.sqlite3").close()
mark_candidate_admission(ready, release)
if mode == "after-admission":
    with sqlite3.connect(root / "state/channel.sqlite3") as connection:
        connection.execute(
            "INSERT OR REPLACE INTO dedup_keys VALUES (?, ?)",
            ("probe-after-admission", 9999999999),
        )
    raise SystemExit(0)
_write_atomic(ready, READY_MARKER_CONTENT, mode=0o600)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    while True:
        time.sleep(0.1)
finally:
    clear_ready_marker(ready)
    os.close(descriptor)
'''


def _snapshot_source(source: Path, destination: Path) -> str:
    destination.mkdir(mode=0o700)
    for name in ("netizen", "scripts", "deploy"):
        shutil.copytree(
            source / name, destination / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    fixtures = destination / "tests/fixtures"
    fixtures.mkdir(parents=True)
    for name in ("channel_v14.sql", "channel_v14_data.sql"):
        shutil.copyfile(source / "tests/fixtures" / name, fixtures / name)
    digest = hashlib.sha256()
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(destination).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def run(workspace: Path, source: Path) -> dict[str, object]:
    if workspace.exists() or not workspace.name.startswith("netizen-migration-probe-"):
        raise ValueError("workspace must be a new directory named netizen-migration-probe-*")
    workspace.mkdir(mode=0o700)
    workspace = workspace.resolve()
    frozen_source = workspace / "source"
    digest = _snapshot_source(source, frozen_source)
    sys.path.insert(0, str(frozen_source))

    from scripts import netizen_installer as installer
    from netizen import database_migrations as migrations
    from netizen.deployment.installer_support import Release
    from netizen.deployment.service_backend import _lifetime_lock_available, _ready_marker_present
    from netizen.migrations.v14 import validate as validate_v14

    def migrate_fixture(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE dedup_keys SET dedup_key = ? WHERE dedup_key = ?",
            ("probe-migrated", "probe-before"),
        )

    def validate_v15(connection: sqlite3.Connection) -> None:
        validate_v14(connection)
        if connection.execute("SELECT version FROM schema_version").fetchone()[0] != 15:
            raise RuntimeError("fixture target schema is not v15")
        if connection.execute(
            "SELECT 1 FROM dedup_keys WHERE dedup_key = 'probe-migrated'",
        ).fetchone() is None:
            raise RuntimeError("fixture migration did not preserve and transform its seed")

    migrations.SCHEMA_VERSION = 15
    migrations.MIGRATIONS = (
        migrations.Migration(14, 15, migrate_fixture, validate_v15),
    )
    installer.SCHEMA_VERSION = 15

    report: dict[str, object] = {
        "kind": "minimal-service-platform-probe",
        "platform": sys.platform,
        "source_sha256": digest,
        "source_schema": 14,
        "target_schema": 15,
        "coverage": [
            "production activation and migration engine",
            "real current-user service manager and generated service definition",
            "production lifetime lock and candidate admission marker",
            "private readiness bytes and atomic file publication",
            "first-install rollback before service definition publication and retry",
        ],
        "excluded": [
            "full Netizen runtime startup and SDK/Feishu integration",
            "artifact download, dependency preparation, public installer entrypoint",
            "process crash injection during installer instructions",
        ],
        "scenarios": [],
    }
    scenarios: list[dict[str, object]] = []
    report["scenarios"] = scenarios

    def database_state(layout: object) -> dict[str, object]:
        with sqlite3.connect(layout.state_dir / "channel.sqlite3") as connection:
            return {
                "schema": connection.execute("SELECT version FROM schema_version").fetchone()[0],
                "keys": [row[0] for row in connection.execute(
                    "SELECT dedup_key FROM dedup_keys WHERE dedup_key LIKE 'probe-%' ORDER BY dedup_key",
                )],
                "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
            }

    def preserved_data(layout: object) -> str:
        """Prove all historical fixture values outside the explicit probe delta."""
        with sqlite3.connect(layout.state_dir / "channel.sqlite3") as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_version' ORDER BY name",
            )]
            rows = {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                suffix = " WHERE dedup_key NOT LIKE 'probe-%'" if table == "dedup_keys" else ""
                rows[table] = sorted(
                    [list(row) for row in connection.execute(f"SELECT * FROM {quoted}{suffix}")],
                    key=repr,
                )
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    def release_fixture(layout: object, symbol: str, mode: str) -> Release:
        release_root = layout.releases / (symbol * 64)
        release_source = release_root / "source"
        (release_source / "scripts").mkdir(parents=True)
        (release_source / "deploy").mkdir()
        venv = release_root / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to(Path(sys.executable).resolve())
        (release_source / "scripts/netizen_service_launcher.py").write_text(
            f"SOURCE_ROOT = {str(frozen_source)!r}\n" + SERVICE_FIXTURE,
        )
        shutil.copyfile(
            frozen_source / "deploy/netizen.service", release_source / "deploy/netizen.service",
        )
        (layout.state_dir / f"fixture-mode-{release_root.name}").write_text(mode)
        return Release(symbol * 64, release_root, release_source, venv)

    def first_install_probe(layout: object, backend: object, scenario: dict[str, object]) -> None:
        # No synthetic schema v15 is involved in a fresh installation: the
        # fixture uses BindingStore's current production schema creation.
        migrations.SCHEMA_VERSION = 14
        migrations.MIGRATIONS = ()
        installer.SCHEMA_VERSION = 14
        scenario["source_schema"] = None
        scenario["target_schema"] = 14
        try:
            candidate = release_fixture(layout, "b", "first-install")
            assert not layout.current.exists() and not layout.current.is_symlink()
            assert not layout.service_file.exists()
            assert not (layout.state_dir / "channel.sqlite3").exists()
            if sys.platform == "linux":
                from netizen.deployment.systemd import systemctl_user

                result = systemctl_user(
                    layout, "show", layout.service_name,
                    "--property=LoadState", "--property=ActiveState",
                    check=False, capture_output=True,
                )
                scenario["missing_unit_show"] = {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
                assert sorted(result.stdout.splitlines()) == [
                    "ActiveState=inactive", "LoadState=not-found",
                ]
            # A real occupied loopback port causes a deterministic failure
            # after the durable intent is written and before unit publication.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
                held.bind(("127.0.0.1", 0))
                held.listen()
                binding = installer.AdminBind(True, "127.0.0.1", held.getsockname()[1])
                try:
                    installer.activate_release(
                        candidate, layout, interactive=False,
                        ready_timeout=3, admin_bind=binding,
                    )
                except installer.InstallError as error:
                    scenario["expected_error"] = str(error)
                    assert "was rolled back" in str(error), str(error)
                    assert f"127.0.0.1:{binding.port}" in str(error), str(error)
                else:
                    raise AssertionError("occupied first-install port was accepted")
            assert installer._read_activation_intent(layout) is None
            assert not layout.current.exists() and not layout.current.is_symlink()
            assert not layout.previous.exists() and not layout.previous.is_symlink()
            assert not layout.service_file.exists()
            assert not (layout.state_dir / "channel.sqlite3").exists()
            assert not backend.inspect_state().loaded
            assert _lifetime_lock_available(layout)
            scenario["prepublish_failure_rolled_back"] = True
            installer.activate_release(
                candidate, layout, interactive=False, ready_timeout=15,
            )
            assert layout.current.resolve() == candidate.root
            assert backend.inspect_state().loaded
            assert _ready_marker_present(layout)
            assert installer._read_activation_intent(layout) is None
            assert database_state(layout) == {"schema": 14, "keys": [], "integrity": "ok"}
            migrations.plan_channel_database(layout.state_dir / "channel.sqlite3")
            scenario["database"] = database_state(layout)
            scenario["first_install_retry_succeeded"] = True
        finally:
            migrations.SCHEMA_VERSION = 15
            migrations.MIGRATIONS = (
                migrations.Migration(14, 15, migrate_fixture, validate_v15),
            )
            installer.SCHEMA_VERSION = 15

    for scenario_name in (
        "active", "stopped", "before-admission", "after-admission",
        "first-install-before-publish",
    ):
        scenario: dict[str, object] = {"name": scenario_name, "passed": False, "cleanup": False}
        scenarios.append(scenario)
        layout = installer.resolve_layout(root=workspace / f"instance-{scenario_name}")
        backend = installer._service_backend(layout)
        scenario["service"] = layout.service_name if sys.platform == "linux" else layout.service_label
        started = time.monotonic()
        try:
            installer.prepare_directories(layout)
            backend.preflight()
            with installer.installation_lock(layout):
                if scenario_name == "first-install-before-publish":
                    first_install_probe(layout, backend, scenario)
                    scenario["passed"] = True
                    continue
                old = release_fixture(layout, "a", "ready")
                candidate_mode = "ready" if scenario_name in {"active", "stopped"} else scenario_name
                candidate = release_fixture(layout, "b", candidate_mode)
                with sqlite3.connect(layout.state_dir / "channel.sqlite3") as connection:
                    connection.executescript((frozen_source / "tests/fixtures/channel_v14.sql").read_text())
                    connection.executescript((frozen_source / "tests/fixtures/channel_v14_data.sql").read_text())
                    connection.execute("INSERT INTO dedup_keys VALUES ('probe-before', 9999999999)")
                original_data = preserved_data(layout)
                installer._set_release_link(layout.current, old.root, layout)
                backend.publish_definition(backend.render_definition(old), should_enable=True)
                if scenario_name != "stopped":
                    backend.start_and_wait(timeout=15)
                    assert not _lifetime_lock_available(layout), "service failed to retain lifetime lock"
                try:
                    installer.activate_release(candidate, layout, interactive=False, ready_timeout=3)
                except installer.InstallError as error:
                    scenario["expected_error"] = str(error)
                    if scenario_name == "before-admission":
                        assert "was rolled back" in str(error), str(error)
                        assert database_state(layout) == {
                            "schema": 14, "keys": ["probe-before"], "integrity": "ok",
                        }
                        assert layout.current.resolve() == old.root
                        assert _ready_marker_present(layout)
                        assert installer._read_activation_intent(layout) is None
                    elif scenario_name == "after-admission":
                        assert "rollback incomplete" in str(error), str(error)
                        assert database_state(layout) == {
                            "schema": 15,
                            "keys": ["probe-after-admission", "probe-migrated"],
                            "integrity": "ok",
                        }
                        intent = installer._read_activation_intent(layout)
                        assert intent is not None and intent.recovery is not None
                        recovery = installer.load_recovery(layout, intent.recovery)
                        assert recovery.admission_observed()
                        snapshot_hash = hashlib.sha256(
                            (recovery.root / "database/channel.sqlite3").read_bytes(),
                        ).hexdigest()
                        other = release_fixture(layout, "c", "ready")
                        try:
                            installer.activate_release(other, layout, interactive=False, ready_timeout=3)
                        except installer.InstallError as mismatch:
                            assert "exact release" in str(mismatch), str(mismatch)
                            scenario["other_release_rejected"] = True
                        else:
                            raise AssertionError("different candidate was accepted after admission")
                        assert hashlib.sha256(
                            (recovery.root / "database/channel.sqlite3").read_bytes(),
                        ).hexdigest() == snapshot_hash
                        (layout.state_dir / f"fixture-mode-{candidate.digest}").write_text("ready")
                        installer.activate_release(candidate, layout, interactive=False, ready_timeout=15)
                        assert database_state(layout) == {
                            "schema": 15,
                            "keys": ["probe-after-admission", "probe-migrated"],
                            "integrity": "ok",
                        }
                        assert layout.current.resolve() == candidate.root
                        assert _ready_marker_present(layout)
                        assert installer._read_activation_intent(layout) is None
                        scenario["exact_candidate_recovered"] = True
                    else:
                        raise
                else:
                    assert scenario_name in {"active", "stopped"}, "fault fixture unexpectedly became ready"
                    assert database_state(layout) == {
                        "schema": 15, "keys": ["probe-migrated"], "integrity": "ok",
                    }
                    assert layout.current.resolve() == candidate.root
                    assert backend.inspect_state().loaded is (scenario_name == "active")
                    assert _ready_marker_present(layout) is (scenario_name == "active")
                    assert installer._read_activation_intent(layout) is None
                scenario["database"] = database_state(layout)
                assert preserved_data(layout) == original_data, "historical fixture values changed"
                scenario["preserved_fixture_sha256"] = original_data
                scenario["passed"] = True
        except BaseException:
            scenario["failure"] = traceback.format_exc()
        finally:
            try:
                backend.uninstall_definition()
                assert not backend.inspect_state().loaded
                assert not layout.service_file.exists()
                assert _lifetime_lock_available(layout)
                scenario["cleanup"] = True
            except BaseException:
                scenario["cleanup_failure"] = traceback.format_exc()
            scenario["seconds"] = round(time.monotonic() - started, 2)
            print(json.dumps(scenario, sort_keys=True), flush=True)
    report["passed"] = all(row["passed"] and row["cleanup"] for row in scenarios)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    report = run(args.workspace, args.source.resolve())
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
