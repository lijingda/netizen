"""Per-user installed-CLI services; definitions are the instance inventory.

Only Netizen's frozen v1 definition shape is writable. Recognition is independent
of the current renderer; a new format needs its own explicitly supported validator.
systemd state comes from machine properties; launchd's documented list columns
provide PID state.
The diagnostic output of ``launchctl print`` is deliberately never parsed.
Callers serialize lifecycle mutations with the instance maintenance lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import pwd
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .instance import launch_agent_label, require_instance_root_marker, systemd_service_name


class ServiceError(RuntimeError):
    """Service identity, manager state, or lifecycle completion is unproven."""


@dataclass(frozen=True)
class ServiceBinding:
    root: Path
    python: Path
    prefix: Path
    codex_home: Path | None = None

    def __post_init__(self) -> None:
        paths = (self.root, self.python, self.prefix)
        if self.codex_home is not None:
            paths += (self.codex_home,)
        for path in paths:
            if not path.is_absolute() or any(ord(c) < 32 for c in str(path)):
                raise ServiceError("service binding paths must be absolute without control characters")
        if self.root != self.root.resolve() or self.prefix != self.prefix.resolve():
            raise ServiceError("instance root and environment prefix must be canonical")
        # Do NOT resolve python: two venvs often point at the same base binary.
        if self.python != Path(os.path.abspath(self.python)):
            raise ServiceError("Python launch path must be normalized without resolving its symlink")


@dataclass(frozen=True)
class ServiceStatus:
    binding: ServiceBinding
    running: bool
    enabled: bool | None
    ready: bool
    loaded: bool = False


_V1_MARKER = "# Netizen CLI service v1 "
_V1_SENTINEL = "io.github.lijingda.netizen/cli-v1"
_READY = b"netizen service ready\n"
_LOG_READ_LIMIT = 1024 * 1024
_SYSTEMD_PROPERTIES = (
    "LoadState", "ActiveState", "SubState", "MainPID", "ControlPID", "FragmentPath",
    "DropInPaths", "NeedDaemonReload", "Transient", "UnitFileState",
)


def _v1_quote(value: str) -> str:
    if any(ord(c) < 32 for c in value):
        raise ServiceError("systemd values cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _v1_environment(home: Path, binding: ServiceBinding) -> dict[str, str]:
    """Frozen v1 environment: no extra overrides or caller PATH are accepted."""
    root = binding.root
    environment = {
        "HOME": str(home), "NETIZEN_ROOT": str(root),
        "NETIZEN_CLI_PREFIX": str(binding.prefix), "NETIZEN_CLI_SERVICE": "1",
        "NETIZEN_CLI_PYTHON": str(binding.python),
        "NETIZEN_CONFIG_PATH": str(root / "config.yaml"),
        "NETIZEN_LARK_APP_CONFIG": str(root / "lark-app/config.json"),
        "NETIZEN_ADMIN_SECRET_FILE": str(root / "credentials/admin-web-secret"),
        "NETIZEN_READY_FILE": str(root / "state/service.ready"),
        "NETIZEN_LIFETIME_LOCK_FILE": str(root / "state/service.lifetime.lock"),
        "NETIZEN_LOG_FILE": str(root / "state/netizen.log"),
        "PATH": ":".join([str(home / ".local/bin"), "/opt/homebrew/bin",
                          "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
    }
    if binding.codex_home is not None:
        environment["CODEX_HOME"] = str(binding.codex_home)
    return environment


def _v1_arguments(binding: ServiceBinding) -> list[str]:
    return [str(binding.python), "-E", "-P", "-m", "netizen_cli", "_serve",
            "--root", str(binding.root)]


def _v1_definition(home: Path, platform: str, binding: ServiceBinding,
                   *, enabled: bool = True) -> bytes:
    """Frozen CLI v1 contract, never expanded to match new rendering defaults.

    Linux accepts this exact serialization; launchd accepts this exact plist
    structure and types. Binding paths and RunAtLoad are the only variable
    fields. Changes require a separately versioned contract, retaining this one
    while v1 is supported. This is not a parser for arbitrary manager overrides.
    """
    env = _v1_environment(home, binding)
    if platform == "darwin":
        env["NETIZEN_MANAGED_LAUNCH_AGENT"] = _V1_SENTINEL
        return plistlib.dumps({
            "Label": launch_agent_label(binding.root),
            "ProgramArguments": _v1_arguments(binding),
            "WorkingDirectory": str(home), "RunAtLoad": enabled,
            "KeepAlive": {"SuccessfulExit": False}, "ExitTimeOut": 75,
            "ThrottleInterval": 3, "Umask": 0o077,
            "AbandonProcessGroup": False, "EnvironmentVariables": env,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": str(binding.root / "state/launchd.stderr.log"),
        }, sort_keys=True)
    metadata = json.dumps({"root": str(binding.root), "python": str(binding.python),
                           "prefix": str(binding.prefix),
                           "codex_home": str(binding.codex_home) if binding.codex_home else None},
                          sort_keys=True)
    lines = [
        _V1_MARKER + metadata, "[Unit]", "Description=Netizen CLI instance",
        "After=network-online.target", "[Service]", "Type=simple",
        "WorkingDirectory=" + _v1_quote(str(home)),
        "ExecStart=:" + " ".join(_v1_quote(arg) for arg in _v1_arguments(binding)),
        "Restart=on-failure", "RestartSec=3", "TimeoutStopSec=75",
        "KillMode=control-group", "UMask=0077",
        *["Environment=" + _v1_quote(f"{key}={value}") for key, value in env.items()],
        "[Install]", "WantedBy=default.target", "",
    ]
    return "\n".join(lines).encode()


def _read_owned(path: Path, uid: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                    or stat.S_IMODE(metadata.st_mode) & 0o022):
                raise ServiceError(f"not a protected current-user regular file: {path}")
            return source.read()
    except OSError as exc:
        raise ServiceError(f"cannot read owned service file {path}: {exc}") from exc


def _lock_available(root: Path, uid: int) -> bool:
    path = root / "state" / "service.lifetime.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ServiceError(f"cannot inspect instance lifetime lock {path}: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ServiceError(f"unsafe instance lifetime lock: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _tail_log(path: Path, *, uid: int, lines: int) -> str | None:
    """Read a bounded tail without following a log file replaced by a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ServiceError(f"cannot read log {path}: {exc}") from exc
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise ServiceError(f"not a protected current-user log file: {path}")
        offset = max(0, metadata.st_size - _LOG_READ_LIMIT)
        source.seek(offset)
        payload = source.read(_LOG_READ_LIMIT)
    if offset:
        # Drop a possibly partial first line, but retain a giant last line's
        # bounded bytes if it contains no newline at all.
        separator = payload.find(b"\n")
        if separator >= 0:
            payload = payload[separator + 1:]
    text = "\n".join(payload.decode("utf-8", errors="replace").splitlines()[-lines:])
    return ("[earlier log bytes omitted]\n" if offset else "") + text


class ServiceManager:
    def __init__(
        self, *, home: Path | None = None, platform: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.uid = os.geteuid()
        self.home = (home or Path(pwd.getpwuid(self.uid).pw_dir)).resolve()
        self.platform = platform or sys.platform
        if self.platform not in {"linux", "darwin"}:
            raise ServiceError("Netizen supports systemd user services and macOS GUI LaunchAgents only")
        self.service_dir = self.home / (
            ".config/systemd/user" if self.platform == "linux" else "Library/LaunchAgents"
        )
        self._runner = runner

    def service_name(self, root: Path) -> str:
        return (systemd_service_name if self.platform == "linux" else launch_agent_label)(root)

    def service_file(self, root: Path) -> Path:
        name = self.service_name(root)
        return self.service_dir / (name if self.platform == "linux" else name + ".plist")

    def _instance_name(self, name: str) -> bool:
        prefix = "netizen-" if self.platform == "linux" else "io.github.lijingda.netizen."
        suffix = ".service" if self.platform == "linux" else ""
        if not name.startswith(prefix) or (suffix and not name.endswith(suffix)):
            return False
        digest = name[len(prefix):len(name) - len(suffix) if suffix else None]
        return len(digest) == 24 and all(c in "0123456789abcdef" for c in digest)

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PYTHON", "LD_", "DYLD_"))}
        env["HOME"] = str(self.home)
        env["LC_ALL"] = "C"
        if self.platform == "linux":
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{self.uid}")
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{self.uid}/bus")
        try:
            result = self._runner(list(args), check=False, capture_output=True, text=True,
                                  env=env, timeout=100)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceError(f"service manager command failed: {args[0]}: {exc}") from exc
        if check and result.returncode:
            raise ServiceError(f"service manager command failed ({result.returncode}): "
                               f"{' '.join(args)}; {result.stderr.strip()}")
        return result

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run("systemctl", "--user", *args, check=check)

    def _launchctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run("launchctl", *args, check=check)

    def _root(self, root: Path) -> Path:
        root = Path(root).resolve()
        if root in {self.home, Path(root.anchor)}:
            raise ServiceError("instance root cannot be the account home or filesystem root")
        return root

    def _environment(self, binding: ServiceBinding) -> dict[str, str]:
        return _v1_environment(self.home, binding)

    def _arguments(self, binding: ServiceBinding) -> list[str]:
        return _v1_arguments(binding)

    def _render(self, binding: ServiceBinding, *, enabled: bool = True) -> bytes:
        return _v1_definition(self.home, self.platform, binding, enabled=enabled)

    def _binding_from_file(self, path: Path) -> ServiceBinding:
        content = _read_owned(path, self.uid)
        try:
            if self.platform == "linux":
                first, *_ = content.decode().splitlines()
                if not first.startswith(_V1_MARKER):
                    raise ValueError("unsupported service definition format; only CLI v1 is supported")
                raw = json.loads(first[len(_V1_MARKER):])
                if set(raw) != {"root", "python", "prefix", "codex_home"}:
                    raise ValueError("unrecognized binding metadata")
                binding = ServiceBinding(*(Path(raw[key]) for key in ("root", "python", "prefix")),
                                         Path(raw["codex_home"]) if raw["codex_home"] is not None else None)
                valid = content == _v1_definition(self.home, self.platform, binding)
            else:
                payload = plistlib.loads(content)
                env = payload["EnvironmentVariables"]
                if not isinstance(env, dict) or env.get("NETIZEN_MANAGED_LAUNCH_AGENT") != _V1_SENTINEL:
                    raise ValueError("unsupported service definition format; only CLI v1 is supported")
                binding = ServiceBinding(Path(env["NETIZEN_ROOT"]),
                                         Path(env["NETIZEN_CLI_PYTHON"]),
                                         Path(env["NETIZEN_CLI_PREFIX"]),
                                         Path(env["CODEX_HOME"]) if "CODEX_HOME" in env else None)
                enabled = payload["RunAtLoad"]
                expected = plistlib.loads(_v1_definition(self.home, self.platform, binding, enabled=enabled))
                # plist serialization retains scalar types (False is not the
                # integer 0), unlike Python's ordinary dictionary equality.
                valid = (isinstance(enabled, bool)
                         and plistlib.dumps(payload, sort_keys=True)
                         == plistlib.dumps(expected, sort_keys=True))
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, UnicodeError) as exc:
            raise ServiceError(f"unrecognized service definition {path}: {exc}") from exc
        if not valid or path != self.service_file(binding.root):
            raise ServiceError(f"modified or mismatched service definition: {path}")
        if binding.root == self.home or binding.root == Path(binding.root.anchor):
            raise ServiceError(f"unsafe root in service definition: {path}")
        return binding

    def _systemd_properties(self, root: Path) -> dict[str, str]:
        # A failed query is never interpreted as an absent service.
        result = self._systemctl("show", self.service_name(root),
                                 "--property=" + ",".join(_SYSTEMD_PROPERTIES), check=False)
        props: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in _SYSTEMD_PROPERTIES or key in props:
                raise ServiceError("unrecognized systemctl show property response")
            props[key] = value
        if set(props) != set(_SYSTEMD_PROPERTIES):
            raise ServiceError("systemd state unavailable or incomplete; no service action performed")
        if result.returncode and props.get("LoadState") != "not-found":
            raise ServiceError(f"systemd service query failed ({result.returncode})")
        return props

    def _launchd_jobs(self) -> dict[str, int | None]:
        self._launchctl("print", f"gui/{self.uid}")  # exit code only
        output = self._launchctl("list").stdout
        lines = output.splitlines()
        if not lines or lines[0].split() != ["PID", "Status", "Label"]:
            raise ServiceError("unsupported launchctl list format; refusing ambiguous service state")
        jobs: dict[str, int | None] = {}
        for line in lines[1:]:
            parts = line.split()
            if len(parts) != 3:
                raise ServiceError("malformed launchctl list row")
            pid, status_code, label = parts
            if (not status_code.lstrip("-").isdigit() or label in jobs
                    or (pid != "-" and (not pid.isdigit() or int(pid) <= 0))):
                raise ServiceError("invalid launchctl list PID/status/label")
            jobs[label] = None if pid == "-" else int(pid)
        return jobs

    def _ready(self, root: Path) -> bool:
        path = root / "state/service.ready"
        if not path.exists() and not path.is_symlink():
            return False
        try:
            return stat.S_IMODE(path.lstat().st_mode) == 0o600 and _read_owned(path, self.uid) == _READY
        except (OSError, ServiceError):
            return False

    def _clear_ready(self, root: Path) -> None:
        path = root / "state/service.ready"
        if not path.exists() and not path.is_symlink():
            return
        _read_owned(path, self.uid)
        path.unlink()

    def inspect(self, root: Path, *, lifetime_descriptor: int | None = None) -> ServiceStatus | None:
        return self._inspect(root, lifetime_descriptor=lifetime_descriptor)

    def _identity_matches(self, binding: ServiceBinding, pid: int, *, starting: bool = False) -> bool:
        path = binding.root / "state/service.identity.json"
        if not path.exists() and not path.is_symlink():
            return False
        try:
            if stat.S_IMODE(path.lstat().st_mode) != 0o600:
                raise ValueError("runtime identity is not private")
            identity = json.loads(_read_owned(path, self.uid))
        except (OSError, ValueError) as exc:
            raise ServiceError(f"invalid runtime service identity: {path}: {exc}") from exc
        expected = {"format": 1, "pid": pid, "python": str(binding.python),
                    "prefix": str(binding.prefix), "root": str(binding.root)}
        if (starting and isinstance(identity, dict) and type(identity.get("pid")) is int
                and identity["pid"] != pid):
            # The new launcher can briefly own the lock before replacing the
            # previous process's evidence. This is not a ready or update proof.
            return False
        if (identity != expected or type(identity.get("format")) is not int
                or type(identity.get("pid")) is not int):
            raise ServiceError("running service identity differs from its registered binding; "
                               "inspect the service definition before controlling it")
        return True

    def _inspect(self, root: Path, *, stopping: bool = False,
                 starting: bool = False,
                 lifetime_descriptor: int | None = None) -> ServiceStatus | None:
        root = self._root(root)
        path = self.service_file(root)
        exists = path.exists() or path.is_symlink()
        binding = self._binding_from_file(path) if exists else None
        if binding is not None and binding.root != root:
            raise ServiceError("service root does not match selected instance")
        if self.platform == "linux":
            props = self._systemd_properties(root)
            if binding is None:
                if props["LoadState"] != "not-found" or props["FragmentPath"]:
                    raise ServiceError("service definition is missing but systemd still knows the target")
                running = False
            else:
                if (props["LoadState"] != "loaded"
                        or props["FragmentPath"] != str(path)
                        or props["DropInPaths"] or props["NeedDaemonReload"] != "no"
                        or props["Transient"] != "no"):
                    raise ServiceError("effective systemd definition differs or is unknown; inspect overrides")
                active = props["ActiveState"]
                if active not in {"active", "activating", "reloading", "deactivating", "inactive", "failed"}:
                    raise ServiceError(f"unrecognized systemd active state: {active}")
                if not props["MainPID"].isdigit() or not props["ControlPID"].isdigit():
                    raise ServiceError("systemd did not report exact process state")
                running = active not in {"inactive", "failed"} or any(
                    int(props[key]) > 0 for key in ("MainPID", "ControlPID")
                )
            unit_state = props["UnitFileState"]
            enabled = (True if unit_state in {"enabled", "enabled-runtime"}
                       else False if unit_state == "disabled" else None)
            loaded = props["LoadState"] == "loaded"
        else:
            jobs = self._launchd_jobs()
            name = self.service_name(root)
            exact = self._launchctl("print", f"gui/{self.uid}/{name}", check=False)
            loaded = name in jobs
            if (exact.returncode == 0) != loaded:
                raise ServiceError("launchctl namespace/state disagrees with the exact GUI target")
            if binding is None and loaded:
                raise ServiceError("LaunchAgent definition missing but target is still loaded")
            running = loaded and jobs[name] is not None
            # print-disabled is diagnostic text, not a machine API. Do not guess.
            enabled = None
        lock_free = _lock_available(root, self.uid)
        if lifetime_descriptor is not None:
            from .cli_data import validate_lifetime_lock

            try:
                validate_lifetime_lock(root, lifetime_descriptor)
            except (OSError, RuntimeError) as exc:
                raise ServiceError(f"caller does not hold this instance's lifetime lock: {exc}") from exc
            if running:
                raise ServiceError("manager reports a running instance while caller owns its lifetime lock")
            lock_free = True  # Exact ownership, not mere observation of a busy inode.
        if not running and not lock_free and not stopping:
            raise ServiceError("instance lifetime lock is held despite a stopped/unbound service")
        if binding is None:
            return None
        identity_proven = True
        if self.platform == "darwin" and running and not stopping:
            identity_proven = not lock_free and self._identity_matches(binding, jobs[name], starting=starting)
            if not identity_proven and not starting:
                raise ServiceError("running LaunchAgent identity is not yet proven by its lifetime lock "
                                   "and runtime identity; retry after startup")
        return ServiceStatus(binding, running, enabled,
                             running and not lock_free and identity_proven and self._ready(root), loaded)

    def list_instances(self, *, prefix: Path | None = None) -> list[ServiceStatus]:
        """Inspect all owned candidates before a caller can begin stopping any."""
        pattern = "netizen*.service" if self.platform == "linux" else "io.github.lijingda.netizen*.plist"
        candidates = {path for path in self.service_dir.glob(pattern)
                      if self._instance_name(path.name if self.platform == "linux" else path.stem)}
        if self.platform == "linux":
            for command, key in (("list-units", "unit"), ("list-unit-files", "unit_file")):
                args = [command, "netizen*.service", "--output=json", "--no-pager"]
                if command == "list-units":
                    args.append("--all")
                try:
                    result = self._systemctl(*args, check=False)
                    rows = json.loads(result.stdout)
                    if not isinstance(rows, list):
                        raise ValueError("expected service array")
                    # systemd 252's list-unit-files returns 1 for no matches.
                    # Only its validated empty JSON array has this meaning;
                    # no diagnostic text or other failed query proves absence.
                    if result.returncode and not (command == "list-unit-files"
                                                  and result.returncode == 1 and not rows
                                                  and not result.stderr.strip()):
                        raise ServiceError(f"systemd inventory query failed ({result.returncode})")
                    for row in rows:
                        name = row[key]
                        if not isinstance(name, str) or Path(name).name != name:
                            raise ValueError("invalid unit name")
                        if self._instance_name(name):
                            candidates.add(self.service_dir / name)
                except (ValueError, TypeError, KeyError) as exc:
                    raise ServiceError("unsupported systemd service inventory response") from exc
        else:
            for name in self._launchd_jobs():
                if self._instance_name(name):
                    candidates.add(self.service_dir / (name + ".plist"))
        statuses = []
        for path in sorted(candidates):
            binding = self._binding_from_file(path)
            if prefix is not None and binding.prefix != prefix.resolve():
                continue
            status = self.inspect(binding.root)
            if status is None:
                raise ServiceError("service inventory changed while being inspected")
            statuses.append(status)
        return statuses

    def register(self, binding: ServiceBinding, *, enabled: bool = True,
                 lifetime_descriptor: int | None = None) -> None:
        self._root(binding.root)
        try:
            require_instance_root_marker(binding.root, uid=self.uid)
        except (OSError, ValueError) as exc:
            raise ServiceError(f"instance must be prepared before service registration: {exc}") from exc
        existing = self.inspect(binding.root, lifetime_descriptor=lifetime_descriptor)
        if existing is not None:
            if existing.binding != binding:
                raise ServiceError("instance already bound to another environment; remove first")
            return
        if not binding.python.is_file() or not binding.prefix.is_dir():
            raise ServiceError("bound Python environment is missing; select an installed environment")
        self.service_dir.mkdir(parents=True, exist_ok=True)
        if self.service_dir.is_symlink():
            raise ServiceError("service directory must not be a symlink")
        metadata = self.service_dir.stat()
        if metadata.st_uid != self.uid or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ServiceError("service directory must be owned by the current user and not group/world writable")
        path = self.service_file(binding.root)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as dest:
                dest.write(self._render(binding, enabled=enabled))
                dest.flush()
                os.fsync(dest.fileno())
        except OSError as exc:
            raise ServiceError(f"could not register service definition {path}: {exc}") from exc
        if self.platform == "linux":
            self._systemctl("daemon-reload")
            self._systemctl("enable" if enabled else "disable", self.service_name(binding.root))
        else:
            self._run("plutil", "-lint", str(path))
            self._launchctl("enable" if enabled else "disable",
                            f"gui/{self.uid}/{self.service_name(binding.root)}")
        self.inspect(binding.root, lifetime_descriptor=lifetime_descriptor)

    def start(self, root: Path, *, timeout: float = 120) -> ServiceStatus:
        root = self._root(root)
        status = self._inspect(root, starting=True)
        if status is None:
            raise ServiceError("instance has no service binding; register it before starting")
        if status.ready:
            return status
        if not status.binding.python.is_file() or not status.binding.prefix.is_dir():
            raise ServiceError("bound Python environment is missing; remove and start from the new environment")
        if not status.running:
            self._clear_ready(root)
            if self.platform == "linux":
                self._systemctl("start", self.service_name(root))
            elif not status.loaded:
                # Do not enable: a pre-existing disable override must remain visible.
                self._launchctl("bootstrap", f"gui/{self.uid}", str(self.service_file(root)))
            else:
                self._launchctl("kickstart", f"gui/{self.uid}/{self.service_name(root)}")
        deadline = time.monotonic() + timeout
        while True:
            current = self._inspect(root, starting=True)
            if current is None:
                raise ServiceError("service was removed while starting")
            if current.ready:
                return current
            if time.monotonic() >= deadline:
                raise ServiceError(f"service did not become ready within {timeout:g}s; "
                                   "inspect status/logs; startup failure does not imply it is stopped")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def stop(self, root: Path, *, timeout: float = 90) -> ServiceStatus:
        root = self._root(root)
        status = self.inspect(root)
        if status is None:
            raise ServiceError("instance has no registered service to stop")
        if self.platform == "linux":
            # stop cancels pending restarts without changing boot-time enable intent.
            self._systemctl("stop", self.service_name(root))
        elif status.loaded:
            self._launchctl("bootout", f"gui/{self.uid}/{self.service_name(root)}")
        deadline = time.monotonic() + timeout
        while True:
            current = self._inspect(root, stopping=True)
            if current is None:
                raise ServiceError("service disappeared during stop confirmation")
            if not current.running and _lock_available(root, self.uid):
                if self.platform == "darwin" and current.loaded:
                    raise ServiceError("launchd target remained loaded after bootout")
                self._clear_ready(root)
                return current
            if time.monotonic() >= deadline:
                raise ServiceError(f"service did not fully exit within {timeout:g}s; no further action performed")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def restart(self, root: Path) -> ServiceStatus:
        self.stop(root)
        return self.start(root)

    def remove(self, root: Path) -> None:
        root = self._root(root)
        if self.inspect(root) is None:
            return
        self.stop(root)
        path = self.service_file(root)
        self._binding_from_file(path)
        if self.platform == "linux":
            self._systemctl("disable", self.service_name(root))
        else:
            self._launchctl("disable", f"gui/{self.uid}/{self.service_name(root)}")
        path.unlink()
        if self.platform == "linux":
            self._systemctl("daemon-reload")
            self._systemctl("reset-failed", self.service_name(root), check=False)
        if self.inspect(root) is not None:
            raise ServiceError("service removal not confirmed")

    def logs(self, root: Path, *, lines: int = 100) -> str:
        root = self._root(root)
        if lines < 1:
            raise ServiceError("log line count must be positive")
        try:
            require_instance_root_marker(root, uid=self.uid)
        except (OSError, ValueError) as exc:
            raise ServiceError(f"cannot identify the requested instance logs: {exc}") from exc
        sections: list[str] = []
        paths = [("Runtime log", root / "state/netizen.log")]
        if self.platform == "darwin":
            paths.append(("LaunchAgent startup stderr", root / "state/launchd.stderr.log"))
        for label, path in paths:
            try:
                content = _tail_log(path, uid=self.uid, lines=lines)
                content = "(not created)" if content is None else content or "(empty)"
            except (OSError, ServiceError) as exc:
                content = f"(unavailable: {exc})"
            sections.append(f"{label} ({path}):\n{content}")
        if self.platform == "linux":
            try:
                result = self._run("journalctl", "--user", "--unit", self.service_name(root),
                                   "--no-pager", "--lines", str(lines), "--output=cat")
                diagnostics = result.stdout.strip() or "(no entries)"
            except ServiceError as exc:
                diagnostics = f"(unavailable: {exc})"
            sections.append("Systemd startup diagnostics:\n" + diagnostics)
        # Text from each sink is only displayed, never parsed as service state.
        return "\n\n".join(sections)
