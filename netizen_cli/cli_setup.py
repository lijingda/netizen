"""Explicit, per-instance setup; package installation has no setup side effects."""
from __future__ import annotations

import logging
import getpass
import contextlib
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, IO

from .backends import DEFAULT_BACKEND, instance_backend
from .lark_app import encode_lark_app, load_lark_app


logger = logging.getLogger(__name__)


class SetupError(RuntimeError):
    pass


def _write_atomic(path: Path, content: bytes, *, mode: int) -> None:
    """Save private setup files without importing the retired installer."""
    parent = path.parent
    if parent.is_symlink():
        raise SetupError(f"configuration parent must not be a symlink: {parent}")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _private_file(path: Path) -> None:
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise SetupError(f"expected a private current-user regular file: {path}")


def _helper(module: str, arguments: list[str], *, timeout: float,
            runner: Callable = subprocess.run, progress: bool = False) -> dict:
    """Capture credentials privately; never include stdout in an exception."""
    try:
        result = runner(
            [os.path.abspath(sys.executable), "-E", "-P", "-B", "-u", "-m", module, *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=None if progress else subprocess.PIPE,
            text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise SetupError("setup helper failed or timed out; rerun netizen setup") from None
    if result.returncode == 130:
        raise KeyboardInterrupt
    if result.returncode != 0:
        raise SetupError("setup helper did not complete; rerun netizen setup")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise SetupError("setup helper returned an invalid result") from None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SetupError("setup helper returned an invalid result")
    return value


def require_codex_login(*, runner: Callable = subprocess.run) -> None:
    """Check the selected account context without executing shell startup code.

    Login profiles run only inside the managed service boundary. An explicitly
    selected CODEX_HOME is the same lexical absolute path used by registration.
    """
    account = pwd.getpwuid(os.geteuid())
    environment = dict(os.environ)
    selected_codex_home = environment.get("CODEX_HOME")
    codex_home = (Path(selected_codex_home).expanduser().absolute()
                  if selected_codex_home else Path(account.pw_dir) / ".codex")
    environment.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name,
                       SHELL=account.pw_shell, CODEX_HOME=str(codex_home))
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        environment.pop(name, None)
    try:
        result = runner(
            [os.path.abspath(sys.executable), "-E", "-P", "-c",
             "import os; from codex_cli_bin import bundled_codex_path; "
             "p = os.fspath(bundled_codex_path()); os.execv(p, [p, 'login', 'status'])"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
            timeout=30, env=environment, cwd=account.pw_dir,
        )
    except (OSError, subprocess.SubprocessError):
        raise SetupError("could not check the bundled Codex login") from None
    if result.returncode != 0:
        raise SetupError("sign in to Codex for this account, then rerun netizen setup")


def _register(root: Path, app_id: str | None, *, backend: str = DEFAULT_BACKEND, runner: Callable) -> None:
    arguments = ["--backend", backend]
    if app_id:
        arguments.extend(["--app-id", app_id])
    value = _helper("netizen_cli.feishu_app_onboarding", arguments,
                    timeout=660, runner=runner, progress=True)
    if (set(value) != {"version", "appId", "appSecret"}
            or not isinstance(value["appId"], str)
            or not isinstance(value["appSecret"], str)
            or not value["appId"] or not value["appSecret"]
            or (app_id is not None and value["appId"] != app_id)):
        raise SetupError("browser setup returned invalid or mismatched app credentials")
    encoded = encode_lark_app(value["appId"], value["appSecret"])
    _write_atomic(root / "lark-app/config.json", encoded, mode=0o600)


def _manual_credentials(root: Path, app_id: str | None, *, source: IO[str],
                        secret_prompt: Callable[[str], str]) -> None:
    if not app_id:
        print("Feishu App ID: ", end="", file=sys.stderr, flush=True)
        app_id = source.readline().strip()
    secret = secret_prompt("Feishu App Secret (not echoed): ").strip()
    if not app_id or not secret:
        raise SetupError("App ID and App Secret must not be empty")
    _write_atomic(root / "lark-app/config.json", encode_lark_app(app_id, secret), mode=0o600)


def _browser_or_manual(root: Path, app_id: str | None, *, backend: str, runner: Callable,
                       source: IO[str], secret_prompt: Callable[[str], str]) -> None:
    try:
        _register(root, app_id, backend=backend, runner=runner)
    except SetupError:
        if not source.isatty():
            raise
        # Only one browser attempt. Never echo helper output or retry in a loop.
        print("Browser setup did not complete. Enter credentials manually, or press Ctrl-C to cancel.",
              file=sys.stderr)
        _manual_credentials(root, app_id, source=source, secret_prompt=secret_prompt)


def _missing_permissions(root: Path, *, runner: Callable) -> dict[str, list[str]]:
    from .feishu_app_onboarding import REQUIRED_TENANT_SCOPES, REQUIRED_USER_SCOPES

    try:
        value = _helper("netizen_cli.feishu_app_permissions",
                        ["--lark-app-config", str(root / "lark-app/config.json")],
                        timeout=90, runner=runner)
    except SetupError:
        raise SetupError(
            "could not verify saved app credentials/permissions; check network and app status. "
            "If the App Secret is invalid, clear only appSecret in this instance's "
            "lark-app/config.json (keep appId), then rerun netizen setup for exact-App repair"
        ) from None
    missing = value.get("missingScopes")
    required = {"tenant": REQUIRED_TENANT_SCOPES, "user": REQUIRED_USER_SCOPES}
    if (set(value) != {"version", "missingScopes"} or not isinstance(missing, dict)
            or set(missing) != set(required)
            or any(not isinstance(missing[kind], list)
                   or any(not isinstance(scope, str) for scope in missing[kind])
                   or missing[kind] != [scope for scope in scopes if scope in missing[kind]]
                   for kind, scopes in required.items())):
        raise SetupError("permission helper returned an invalid result")
    return missing


def admin_binding_ready(root: Path) -> bool:
    """Read-only binding check shared by explicit setup and startup admission."""
    from .admin.users import AdminUsers

    profile = root / "lark-app/config.json"
    if not profile.exists() and not profile.is_symlink():
        return False
    credentials = load_lark_app(profile, allow_incomplete=True)
    if not credentials.app_id or not credentials.app_secret:
        return False
    return AdminUsers(root, credentials.app_id).has_admins


def require_admin_binding(root: Path) -> None:
    """Background starts never initiate an authorization flow."""
    if not admin_binding_ready(root):
        raise SetupError(
            f"Feishu app and administrator configuration is required; run netizen setup --root {root} "
            "explicitly to authorize, or supply --admin-open-id for the current app"
        )


def _bind_admin(root: Path, *, open_id: str | None, source: IO[str]) -> None:
    from .admin.users import AdminUsers

    credentials = load_lark_app(root / "lark-app/config.json")
    users = AdminUsers(root, credentials.app_id)
    if users.has_admins:
        return
    name = ""
    avatar_url = None
    if open_id is None and source.isatty():
        print("First administrator: [1] Feishu login (default), [2] enter this app's Open ID", file=sys.stderr)
        choice = source.readline().strip()
        if choice not in {"", "1", "2"}:
            raise SetupError("choose 1 or 2 and rerun setup")
        if choice == "2":
            print(f"Administrator Open ID for {credentials.app_id}: ", end="", file=sys.stderr, flush=True)
            open_id = source.readline().strip()
    if open_id is None:
        from .admin.feishu import run_admin_binding

        def verification(url: str) -> None:
            print(f"Administrator binding for {credentials.app_id} in {root}:", file=sys.stderr)
            print(url, file=sys.stderr)
            print("Open this Feishu/Lark URL to sign in. Agents: relay the URL to the user and "
                  "keep this setup process running until confirmation completes.", file=sys.stderr, flush=True)

        try:
            identity = run_admin_binding(credentials.app_id, credentials.app_secret, verification)
        except Exception as diagnostic_error:
            logger.warning(
                "administrator binding authorization failed",
                extra={"error_type": type(diagnostic_error).__name__},
            )
            if not source.isatty():
                raise SetupError("administrator authorization did not complete; rerun setup or supply --admin-open-id") from None
            print("Administrator authorization did not complete. Enter this app's Open ID manually, "
                  "or press Ctrl-C to cancel.", file=sys.stderr)
            print(f"Administrator Open ID for {credentials.app_id}: ", end="", file=sys.stderr, flush=True)
            open_id = source.readline().strip()
        else:
            open_id, name, avatar_url = identity.open_id, identity.name, identity.avatar_url
    users.add_initial(open_id, name=name, avatar_url=avatar_url)


def read_setup_config(path: Path) -> dict[str, Any] | None:
    """Read the instance config even when interrupted setup has no credentials yet."""
    import yaml

    try:
        _private_file(path)
    except FileNotFoundError:
        return None
    try:
        config = yaml.safe_load(path.read_bytes())
    except (ValueError, yaml.YAMLError):
        raise SetupError("existing configuration is invalid; repair it before retrying setup") from None
    if not isinstance(config, dict) or not isinstance(config.get("instance"), dict):
        raise SetupError("existing configuration must contain an instance mapping")
    return config


def setup_backend(config: dict[str, Any] | None, requested: str | None, *, root: Path) -> str:
    if config is None:
        from .cli_data import has_retained_setup_data
        if requested is None and has_retained_setup_data(root):
            raise SetupError("Retained instance data has no config.yaml. Specify its original --backend to repair setup.")
        return instance_backend({} if requested is None else {"backend": requested})
    saved = instance_backend(config["instance"])
    if requested is not None and requested != saved:
        raise SetupError("--backend differs from existing configuration. The backend is fixed at setup. "
                         "A different backend needs a new root.")
    return saved


def resolve_setup_backend(root: Path, requested: str | None, *, already_registered: bool = False) -> str | None:
    try:
        config = read_setup_config(root / "config.yaml")
        if config is None and already_registered:
            if requested is not None:
                raise SetupError("cannot verify --backend without the registered instance configuration")
            return None
        return setup_backend(config, requested, root=root)
    except (OSError, ValueError, SetupError):
        # Optional metadata must never prevent an idempotent setup result.
        if already_registered and requested is None:
            return None
        raise


def verify_setup_backend(backend: str | None, bound_python: Path | None = None) -> None:
    if backend != "claude":
        return
    from .claude_installation import REPAIR, verified_claude_cli

    if bound_python is None or bound_python == Path(sys.executable):
        verified_claude_cli()
        return
    # Bindings keep their original interpreter, including its venv symlink.
    try:
        subprocess.run(
            [str(bound_python), "-I", "-B", "-c",
             "from netizen_cli.claude_installation import verified_claude_cli; verified_claude_cli()"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(REPAIR) from exc


def check_unbound_start_backend(root: Path) -> None:
    try:
        config = read_setup_config(root / "config.yaml")
    except (OSError, ValueError, SetupError):
        # Service startup handles unreadable configuration after registration.
        return
    if config is not None:
        verify_setup_backend(instance_backend(config["instance"]))


def prepare_configuration(root: Path, *, backend: str | None = None, admin_port: int | None = None,
                          admin_open_id: str | None = None, bound_python: Path | None = None,
                          runner: Callable = subprocess.run,
                          input_stream: IO[str] | None = None,
                          secret_prompt: Callable[[str], str] = getpass.getpass) -> str:
    """Root/maintenance/lifetime locks belong to the caller; no service is started."""
    from .settings import Settings
    import yaml

    source = sys.stdin if input_stream is None else input_stream
    config_file = root / "config.yaml"
    existing = read_setup_config(config_file)
    effective_backend = setup_backend(existing, backend, root=root)
    verify_setup_backend(effective_backend, bound_python)
    if existing is None:
        config = {
            "instance": {"backend": effective_backend, "dataDir": str(root / "state"),
                         "projectRoot": str(Path(pwd.getpwuid(os.geteuid()).pw_dir) / "projects")},
            "projects": {}, "channel": {"securityMode": "audit"},
            "adminWeb": {"host": "0.0.0.0"},
        }
        if admin_port is not None:
            config["adminWeb"]["port"] = admin_port
        _write_atomic(config_file, yaml.safe_dump(config, sort_keys=False).encode(), mode=0o600)
    elif admin_port is not None:
        admin = existing.get("adminWeb")
        port = admin.get("port") if isinstance(admin, dict) else None
        if type(port) is not int or port != admin_port:
            raise SetupError("--admin-port differs from existing configuration; edit the config explicitly")
    profile = root / "lark-app/config.json"
    if not profile.exists() and not profile.is_symlink():
        _write_atomic(profile, encode_lark_app("", ""), mode=0o600)
    credentials = load_lark_app(profile, allow_incomplete=True)
    started_flow = False
    if not (credentials.app_id and credentials.app_secret):
        manual = False
        if source.isatty():
            print("Feishu setup: [1] browser (default), [2] manual credentials", file=sys.stderr)
            choice = source.readline().strip()
            if choice not in {"", "1", "2"}:
                raise SetupError("choose 1 or 2 and rerun setup")
            manual = choice == "2"
        if manual:
            _manual_credentials(root, credentials.app_id or None,
                                source=source, secret_prompt=secret_prompt)
        else:
            started_flow = True
            _browser_or_manual(root, credentials.app_id or None, backend=effective_backend, runner=runner,
                               source=source, secret_prompt=secret_prompt)
    missing = _missing_permissions(root, runner=runner)
    if any(missing.values()) and not started_flow:
        _browser_or_manual(root, load_lark_app(profile).app_id, backend=effective_backend, runner=runner,
                           source=source, secret_prompt=secret_prompt)
        missing = _missing_permissions(root, runner=runner)
    if any(missing.values()):
        raise SetupError("finish tenant approval, publish/install the app, then rerun setup; missing scopes: "
                         + ", ".join(f"{kind}:{scope}" for kind, scopes in missing.items() for scope in scopes))
    settings = Settings.from_file(config_file, environment={
        "NETIZEN_LARK_APP_CONFIG": str(profile),
    })
    if settings.data_dir.resolve() != root / "state":
        raise SetupError("instance.dataDir must be this instance's state directory")
    _bind_admin(root, open_id=admin_open_id, source=source)
    return settings.backend
