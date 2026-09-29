"""Explicit, per-instance setup; package installation has no setup side effects."""
from __future__ import annotations

import getpass
import contextlib
import json
import os
import pwd
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, IO

from .lark_app import encode_lark_app, load_lark_app


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


def _register(root: Path, app_id: str | None, *, runner: Callable) -> None:
    arguments = ["--app-id", app_id] if app_id else []
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


def _browser_or_manual(root: Path, app_id: str | None, *, runner: Callable,
                       source: IO[str], secret_prompt: Callable[[str], str]) -> None:
    try:
        _register(root, app_id, runner=runner)
    except SetupError:
        if not source.isatty():
            raise
        # Only one browser attempt. Never echo helper output or retry in a loop.
        print("Browser setup did not complete. Enter credentials manually, or press Ctrl-C to cancel.",
              file=sys.stderr)
        _manual_credentials(root, app_id, source=source, secret_prompt=secret_prompt)


def _missing_permissions(root: Path, *, runner: Callable) -> list[str]:
    from .feishu_app_onboarding import REQUIRED_TENANT_SCOPES

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
    if (set(value) != {"version", "missingScopes"} or not isinstance(missing, list)
            or any(not isinstance(scope, str) for scope in missing)
            or missing != [scope for scope in REQUIRED_TENANT_SCOPES if scope in missing]):
        raise SetupError("permission helper returned an invalid result")
    return missing


def prepare_configuration(root: Path, *, admin_port: int | None = None,
                          runner: Callable = subprocess.run,
                          input_stream: IO[str] | None = None,
                          secret_prompt: Callable[[str], str] = getpass.getpass) -> None:
    """Root/maintenance/lifetime locks belong to the caller; no service is started."""
    from .admin.auth import load_credential_snapshot
    from .settings import Settings
    import yaml

    source = sys.stdin if input_stream is None else input_stream
    config_file = root / "config.yaml"
    if not config_file.exists() and not config_file.is_symlink():
        config = {
            "instance": {"dataDir": str(root / "state"),
                         "projectRoot": str(Path(pwd.getpwuid(os.geteuid()).pw_dir) / "projects")},
            "projects": {}, "channel": {"securityMode": "audit"},
            "adminWeb": {"enabled": True, "host": "0.0.0.0"},
        }
        if admin_port is not None:
            config["adminWeb"]["port"] = admin_port
        _write_atomic(config_file, yaml.safe_dump(config, sort_keys=False).encode(), mode=0o600)
    else:
        _private_file(config_file)
        if admin_port is not None:
            try:
                existing = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            except (UnicodeError, yaml.YAMLError):
                raise SetupError("existing configuration is invalid; repair it before retrying setup") from None
            admin = existing.get("adminWeb") if isinstance(existing, dict) else None
            port = admin.get("port") if isinstance(admin, dict) else None
            if type(port) is not int or port != admin_port:
                raise SetupError("--admin-port differs from existing configuration; edit the config explicitly")
    credential = root / "credentials/admin-web-secret"
    if not credential.exists() and not credential.is_symlink():
        _write_atomic(credential, secrets.token_urlsafe(32).encode("ascii"), mode=0o600)
    _private_file(credential)
    load_credential_snapshot(credential)
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
            _browser_or_manual(root, credentials.app_id or None, runner=runner,
                               source=source, secret_prompt=secret_prompt)
    missing = _missing_permissions(root, runner=runner)
    if missing and not started_flow:
        _browser_or_manual(root, load_lark_app(profile).app_id, runner=runner,
                           source=source, secret_prompt=secret_prompt)
        missing = _missing_permissions(root, runner=runner)
    if missing:
        raise SetupError("finish tenant approval, publish/install the app, then rerun setup; missing scopes: "
                         + ", ".join(missing))
    settings = Settings.from_file(config_file, environment={
        "NETIZEN_ADMIN_SECRET_FILE": str(credential),
        "NETIZEN_LARK_APP_CONFIG": str(profile),
    })
    if settings.data_dir.resolve() != root / "state":
        raise SetupError("instance.dataDir must be this instance's state directory")
