#!/usr/bin/env python3
"""Launch Netizen with the effective user's exported shell environment."""

from __future__ import annotations

import contextlib
import hashlib
import os
import pwd
import select
import shlex
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path


PROFILE_CAPTURE_TIMEOUT_SECONDS = 10.0
PROFILE_CAPTURE_MAX_BYTES = 4 * 1024 * 1024
_POSIX_PROFILE_SHELLS = frozenset({"bash", "dash", "ksh", "mksh", "sh", "zsh"})
_SCRUBBED_PYTHON_ENVIRONMENT = (
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "__PYVENV_LAUNCHER__",
)


class ServiceLaunchError(RuntimeError):
    """The service cannot safely acquire its shell environment or start."""


class _ProfileOutputTooLarge(RuntimeError):
    pass


def _profile_shell_argv(shell: Path, command: str) -> list[str]:
    name = shell.name
    if name in _POSIX_PROFILE_SHELLS:
        return [str(shell), "-lic", command]
    if name == "fish":
        return [str(shell), "--login", "--interactive", "--command", command]
    raise ServiceLaunchError(
        f"unsupported account login shell {shell}; supported shells are "
        "bash, dash, fish, ksh, mksh, sh, and zsh"
    )


def _environment_probe_command(
    python_executable: Path,
    start_token: str,
    end_token: str,
) -> str:
    code = (
        "import hashlib,os,sys; "
        "out=sys.stdout.buffer; "
        "start=os.fsencode(sys.argv[1]); end=os.fsencode(sys.argv[2]); "
        "payload=b''.join(key+b'='+value+b'\\0' for key,value in os.environb.items()); "
        "digest=hashlib.sha256(payload).hexdigest().encode(); "
        "out.write(b'\\0'+start+b'\\0'+str(len(payload)).encode()+b'\\0'"
        "+digest+b'\\0'+payload+b'\\0'+end+b'\\0')"
    )
    # Replacing the login shell prevents Bash/Zsh logout hooks from running.
    # Those hooks belong to an ending terminal session, not to starting a
    # long-lived service with the account's startup environment.
    return "exec " + shlex.join(
        (
            str(python_executable),
            "-E",
            "-B",
            "-u",
            "-c",
            code,
            start_token,
            end_token,
        )
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _read_profile_snapshot(
    read_fd: int,
    process: subprocess.Popen[bytes],
    *,
    end_token: str,
    timeout: float,
) -> bytes:
    deadline = time.monotonic() + timeout
    end_marker = b"\0" + os.fsencode(end_token) + b"\0"
    output = bytearray()
    while end_marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        readable, _, _ = select.select((read_fd,), (), (), remaining)
        if not readable:
            raise subprocess.TimeoutExpired(process.args, timeout)
        chunk = os.read(read_fd, 64 * 1024)
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > PROFILE_CAPTURE_MAX_BYTES:
            raise _ProfileOutputTooLarge

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(process.args, timeout)
    process.wait(timeout=remaining)
    return bytes(output)


def _parse_environment_dump(
    output: bytes,
    *,
    start_token: str,
    end_token: str,
) -> dict[str, str]:
    start_marker = b"\0" + os.fsencode(start_token) + b"\0"
    start = output.rfind(start_marker)
    if start < 0:
        raise ServiceLaunchError(
            "account shell profile completed without returning an environment snapshot"
        )

    framed = output[start + len(start_marker) :]
    length_text, separator, framed = framed.partition(b"\0")
    digest, digest_separator, framed = framed.partition(b"\0")
    if (
        not separator
        or not digest_separator
        or not length_text.isdigit()
        or len(length_text) > 10
        or len(digest) != 64
    ):
        raise ServiceLaunchError(
            "account shell profile returned an invalid environment snapshot"
        )
    length = int(length_text)
    if length > PROFILE_CAPTURE_MAX_BYTES:
        raise ServiceLaunchError(
            "account shell profile returned an invalid environment snapshot"
        )
    payload = framed[:length]
    trailer = framed[length:]
    end_marker = b"\0" + os.fsencode(end_token) + b"\0"
    if (
        len(payload) != length
        or not trailer.startswith(end_marker)
        or hashlib.sha256(payload).hexdigest().encode() != digest
    ):
        raise ServiceLaunchError(
            "account shell profile environment snapshot failed its integrity check"
        )

    environment: dict[str, str] = {}
    for entry in payload.split(b"\0"):
        if not entry:
            continue
        name, separator, value = entry.partition(b"=")
        if not separator or not name:
            raise ServiceLaunchError(
                "account shell profile returned an invalid environment snapshot"
            )
        environment[os.fsdecode(name)] = os.fsdecode(value)
    return environment


def capture_profile_environment(
    *,
    shell: Path,
    home: Path,
    username: str,
    python_executable: Path,
    base_environment: Mapping[str, str] | None = None,
    timeout: float = PROFILE_CAPTURE_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Run one bounded interactive login shell and return its exported environment."""

    if not shell.is_absolute() or not shell.is_file() or not os.access(shell, os.X_OK):
        raise ServiceLaunchError(f"account login shell is not executable: {shell}")
    if timeout <= 0:
        raise ServiceLaunchError("shell profile capture timeout must be positive")

    source = os.environ if base_environment is None else base_environment
    bootstrap = dict(source)
    bootstrap.update(
        {
            "HOME": str(home),
            "LOGNAME": username,
            "SHELL": str(shell),
            "USER": username,
        }
    )
    bootstrap.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    token = uuid.uuid4().hex
    start_token = f"NETIZEN_ENV_START_{token}"
    end_token = f"NETIZEN_ENV_END_{token}"
    try:
        command = _environment_probe_command(
            python_executable,
            start_token,
            end_token,
        )
        argv = _profile_shell_argv(shell, command)
        process = subprocess.Popen(
            argv,
            cwd=home,
            env=bootstrap,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            umask=0o077,
        )
    except OSError as error:
        raise ServiceLaunchError(f"could not start account login shell {shell}: {error}") from error
    assert process.stdout is not None
    try:
        stdout = _read_profile_snapshot(
            process.stdout.fileno(),
            process,
            end_token=end_token,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise ServiceLaunchError(
            f"account shell profile did not finish within {timeout:g}s: {shell}"
        ) from error
    except _ProfileOutputTooLarge as error:
        _terminate_process_group(process)
        raise ServiceLaunchError(
            "account shell profile output exceeded the 4 MiB safety limit"
        ) from error
    except OSError as error:
        _terminate_process_group(process)
        raise ServiceLaunchError(
            f"could not read account shell profile environment: {error}"
        ) from error
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        process.stdout.close()
    if process.returncode != 0:
        _terminate_process_group(process)
        raise ServiceLaunchError(
            f"account shell profile exited with status {process.returncode}: {shell}"
        )
    try:
        return _parse_environment_dump(
            stdout,
            start_token=start_token,
            end_token=end_token,
        )
    except ServiceLaunchError:
        _terminate_process_group(process)
        raise


def service_environment(
    captured: Mapping[str, str],
    *,
    home: Path,
    username: str,
    shell: Path,
    codex_home: str,
    config_path: str,
    secret_file: str,
    admin_secret_file: str,
) -> dict[str, str]:
    """Preserve the shell snapshot while enforcing Netizen-owned launch values."""

    environment = dict(captured)
    environment.pop("FEISHU_APP_SECRET", None)
    environment.pop("FEISHU_APP_SECRET_FILE", None)
    environment.pop("NETIZEN_ADMIN_SECRET", None)
    environment.pop("NETIZEN_ADMIN_SECRET_FILE", None)
    for name in _SCRUBBED_PYTHON_ENVIRONMENT:
        environment.pop(name, None)
    environment.update(
        {
            "CODEX_HOME": codex_home,
            "FEISHU_APP_SECRET_FILE": secret_file,
            "HOME": str(home),
            "LOGNAME": username,
            "NETIZEN_CONFIG_PATH": config_path,
            "NETIZEN_ADMIN_SECRET_FILE": admin_secret_file,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "SHELL": str(shell),
            "USER": username,
        }
    )
    return environment


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ServiceLaunchError(f"managed service environment is missing {name}")
    return value


def launch() -> None:
    try:
        account = pwd.getpwuid(os.geteuid())
    except KeyError as error:
        raise ServiceLaunchError(
            f"effective uid {os.geteuid()} has no account database entry"
        ) from error
    home = Path(account.pw_dir)
    shell = Path(account.pw_shell)
    captured = capture_profile_environment(
        shell=shell,
        home=home,
        username=account.pw_name,
        python_executable=Path(sys.executable),
    )
    environment = service_environment(
        captured,
        home=home,
        username=account.pw_name,
        shell=shell,
        codex_home=_required_environment("CODEX_HOME"),
        config_path=_required_environment("NETIZEN_CONFIG_PATH"),
        secret_file=_required_environment("FEISHU_APP_SECRET_FILE"),
        admin_secret_file=_required_environment("NETIZEN_ADMIN_SECRET_FILE"),
    )
    os.execve(
        sys.executable,
        [sys.executable, "-E", "-B", "-u", "-m", "netizen.main"],
        environment,
    )


def main(_argv: Sequence[str] | None = None) -> int:
    try:
        launch()
    except (OSError, ServiceLaunchError) as error:
        print(f"netizen: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
