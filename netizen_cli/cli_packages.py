"""Finite, read-only package update preparation.

This module never installs packages. It produces a private, serializable plan for
the external update worker. Native pip report v1 and uv exit statuses are used;
installer diagnostics are never parsed. Private uv-tool receipt identity and
cache-layout refusal rules are tested against uv 0.12.20; ordinary uv-pip uses
the documented public commands and non-mutating capability checks.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from typing import Any, Mapping


DISTRIBUTION = "netizen-cli"
SUPPORTED_UV_TOOL_VERSIONS = frozenset({"0.12.20"})


class PackageUpdateError(RuntimeError):
    """Preflight could not safely identify or resolve this installation."""

    def __init__(self, message: str, *, diagnostic: str = "") -> None:
        super().__init__(message)
        self.diagnostic = diagnostic  # Private diagnostics, never used for decisions.


# Executed in a new isolated interpreter, not imported from the installation that
# the worker is replacing. importlib.metadata and sysconfig are public APIs.
_PROBE_BODY = r'''
import importlib, importlib.metadata as m, importlib.util, json, os, pathlib, sys, sysconfig
P = pathlib.Path
distributions = list(m.distributions(name="netizen-cli"))
if len(distributions) != 1:
    raise RuntimeError("installation lacks a unique netizen-cli distribution")
d = distributions[0]
files = list(d.files or ())
metadata = [P(d.locate_file(f)).resolve() for f in files
            if f.name == "METADATA" and f.parent.name.endswith(".dist-info")]
if len(metadata) != 1:
    raise RuntimeError("installation lacks unique wheel metadata")
spec = importlib.util.find_spec("netizen_cli")
if spec is None or spec.origin is None:
    raise RuntimeError("netizen_cli module is missing")
entries = [e.value for e in d.entry_points if e.group == "console_scripts" and e.name == "netizen"]
if entries != ["netizen_cli.cli:main"]:
    raise RuntimeError("netizen entry point does not match the installed CLI")
prefix = P(sys.prefix).resolve()
package = P(spec.origin).resolve().parent
info = metadata[0].parent
scripts = P(sysconfig.get_path("scripts")).resolve()
sites = {P(sysconfig.get_path(k)).resolve() for k in ("purelib", "platlib")}
if not all(p.is_relative_to(prefix) for p in (*sites, scripts)):
    raise RuntimeError("installation uses external site-packages")
if not any(package.parent == s and info.parent == s for s in sites):
    raise RuntimeError("package or metadata is outside this environment's site-packages")
raw_url = d.read_text("direct_url.json")
if raw_url:
    raise RuntimeError("direct URL, local source and editable installs require manual maintenance")
if not isinstance(d.version, str) or not d.version.strip():
    raise RuntimeError("installed distribution version is missing")
pip_spec = importlib.util.find_spec("pip")
local_pip = (pip_spec is not None and pip_spec.origin is not None
             and P(pip_spec.origin).resolve().is_relative_to(prefix))
result = {"environment_python": os.path.abspath(sys.executable), "prefix": str(prefix),
          "base_prefix": str(P(sys.base_prefix).resolve()), "package_dir": str(package),
          "dist_info": str(info), "version": d.version,
          "installer": (d.read_text("INSTALLER") or "").strip(),
          "pip_available": local_pip,
          "scripts_dir": str(scripts),
          "externally_managed": (P(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists()}
if len(sys.argv) > 1:
    expected = json.loads(sys.argv[1])
    for key, value in expected.items():
        if result.get(key) != value:
            raise RuntimeError("installation identity changed: " + key)
if len(sys.argv) > 2:
    for module in ("netizen_cli.cli", "netizen_cli.cli_services", "netizen_cli.package_resources"):
        importlib.import_module(module)
print(json.dumps(result))
'''
_PROBE = ("try:\n" + "".join("    " + line + "\n" for line in _PROBE_BODY.splitlines())
          + "except Exception as error:\n"
            "    import json\n"
            "    print(json.dumps({'error': str(error)}))\n"
            "    raise SystemExit(1)\n")


def _run(
    argv: list[str], *, env: Mapping[str, str], cwd: Path,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, env=dict(env), cwd=cwd, text=True, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageUpdateError(
            f"Package preflight could not execute {Path(argv[0]).name}: {exc}. "
            "No services have been stopped; repair the tool or update manually."
        ) from exc


def _require_success(result: subprocess.CompletedProcess[str], operation: str) -> None:
    if result.returncode:
        # Tool output is diagnostic, not a machine protocol. In particular a
        # nonzero uv --check must not be interpreted as a successful plan.
        raise PackageUpdateError(
            f"{operation} failed (exit {result.returncode}). No services have been stopped. "
            "Run the same package manager manually to diagnose its resolution or installation error.",
            diagnostic=result.stderr or result.stdout,
        )


def _object(text: str, operation: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise PackageUpdateError(f"{operation} returned an invalid machine response.") from exc
    if not isinstance(data, dict):
        raise PackageUpdateError(f"{operation} returned an invalid machine response.")
    return data


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PackageUpdateError(f"Cannot read installation evidence {path}: {exc}") from exc


def _identity(path: Path) -> list[int]:
    try:
        st = path.stat()
    except OSError as exc:
        raise PackageUpdateError(f"Cannot identify installation path {path}: {exc}") from exc
    return [st.st_dev, st.st_ino]


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _check_environment_options(backend: str, env: Mapping[str, str]) -> None:
    if backend == "pip":
        denied_values = (
            "PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_PYTHON",
            "PIP_REQUIREMENT", "PIP_EDITABLE", "PIP_REPORT",
        )
        denied_flags = (
            "PIP_USER", "PIP_BREAK_SYSTEM_PACKAGES", "PIP_DRY_RUN",
            "PIP_IGNORE_INSTALLED", "PIP_FORCE_REINSTALL",
            "PIP_NO_DEPS", "PIP_IGNORE_REQUIRES_PYTHON",
        )
    else:
        denied_values = (
            "UV_TARGET", "UV_PREFIX",
            "UV_PYTHON", "UV_PROJECT_ENVIRONMENT", "UV_WORKING_DIR", "UV_PROJECT",
            "UV_REINSTALL_PACKAGE",
        )
        denied_flags = (
            "UV_SYSTEM_PYTHON", "UV_BREAK_SYSTEM_PACKAGES", "UV_NO_DEPS",
            "UV_EXACT", "UV_REINSTALL",
        )
    for key in (*denied_values, *denied_flags):
        value = env.get(key, "")
        # A path or package literally named "false" is still a redirect. Only
        # boolean options may interpret such a value as disabling the option.
        if value and (key in denied_values or value.lower() not in ("0", "false", "no")):
            raise PackageUpdateError(
                f"{key} changes the package update target or contract; clear it or update manually."
            )
    for key in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR"):
        if env.get(key) and not Path(env[key]).is_absolute():
            raise PackageUpdateError(f"{key} must be absolute for an unambiguous update target.")
    # The worker runs outside the package environment. Never reinterpret a user's
    # relative constraint/index path against that unrelated working directory.
    for key in ("PIP_CONSTRAINT", "PIP_FIND_LINKS", "UV_CONSTRAINT", "UV_OVERRIDE", "UV_FIND_LINKS"):
        if not env.get(key):
            continue
        try:
            paths = shlex.split(env[key])
        except ValueError as exc:
            raise PackageUpdateError(f"Cannot interpret {key}; update manually.") from exc
        if any("://" not in p and not Path(p).is_absolute() for p in paths):
            raise PackageUpdateError(
                f"{key} contains a relative path. Use absolute paths or update manually."
            )


def _check_environment_layout(info: dict[str, Any], env: Mapping[str, str]) -> list[Path]:
    prefix = Path(info["prefix"])
    _reject_uv_cache_environment(prefix)
    evidence: list[Path] = []
    if prefix == Path(info["base_prefix"]) and info["externally_managed"]:
        raise PackageUpdateError("This Python is externally managed; use its owning package manager.")
    if not os.access(Path(info["dist_info"]).parent, os.W_OK):
        raise PackageUpdateError("This installation is not writable; update it manually without Netizen privilege escalation.")
    cfg = prefix / "pyvenv.cfg"
    if cfg.exists():
        evidence.append(cfg)
        try:
            settings = dict(line.split("=", 1) for line in cfg.read_text().splitlines() if "=" in line)
        except OSError as exc:
            raise PackageUpdateError(f"Cannot inspect {cfg}: {exc}") from exc
        if any(k.strip().lower() == "include-system-site-packages" and v.strip().lower() == "true"
               for k, v in settings.items()):
            raise PackageUpdateError("Shared system-site-packages environments require manual update.")
    for marker in (prefix / "pipx_metadata.json", prefix / "conda-meta"):
        if marker.exists():
            raise PackageUpdateError(f"Environment is managed by another tool ({marker.name}); update it manually.")
    if (prefix.parent / "uv.lock").exists() or (prefix.parent / "poetry.lock").exists():
        raise PackageUpdateError("A project-managed environment must be updated through its project manager.")
    if env.get("UV_PROJECT_ENVIRONMENT"):
        raise PackageUpdateError("UV_PROJECT_ENVIRONMENT makes ownership ambiguous; use the project manager.")
    return evidence


def _uv_executable(env: Mapping[str, str]) -> str:
    selected = shutil.which("uv", path=env.get("PATH", os.defpath))
    if not selected:
        raise PackageUpdateError("uv is not available; install/manage uv yourself or update manually.")
    # Unlike Python, resolving the uv binary does not erase venv identity.
    return str(Path(selected).resolve())


def _uv_directory(uv: str, kind: str, env: Mapping[str, str], cwd: Path) -> Path:
    result = _run([uv, kind, "dir"], env=env, cwd=cwd)
    _require_success(result, f"uv {kind}-directory query")
    raw = result.stdout.strip()
    if not raw or "\n" in raw or not Path(raw).is_absolute():
        raise PackageUpdateError(f"uv {kind} dir returned an invalid absolute directory.")
    return Path(raw).resolve()


def _reject_uv_cache_environment(prefix: Path, cache_root: Path | None = None) -> None:
    # The public cache directory covers current and future buckets. The one
    # observed archive bucket is also refused when UV_CACHE_DIR changed, the
    # original configuration is lost, or `uvx --no-cache` uses a temporary root.
    # This is a refusal rule, never permission to write any cache layout.
    if ((cache_root is not None and prefix.is_relative_to(cache_root))
            or prefix.parent.name == "archive-v0"):
        raise PackageUpdateError(
            "This Python belongs to a disposable uv cache/tool-run environment; "
            "install netizen-cli in a persistent environment before updating. "
            "No services have been stopped."
        )


def _uv_context(uv: str, env: Mapping[str, str], cwd: Path) -> tuple[str, Path, Path]:
    result = _run([uv, "self", "version", "--output-format", "json"], env=env, cwd=cwd)
    _require_success(result, "uv version query")
    version = _object(result.stdout, "uv version query").get("version")
    if not isinstance(version, str) or not version.strip():
        raise PackageUpdateError("uv version query returned an invalid version.")
    return (version, _uv_directory(uv, "tool", env, cwd),
            _uv_directory(uv, "cache", env, cwd))


def _require_uv_tool_version(version: str) -> None:
    # Reading private receipts is version-gated independently of ordinary pip
    # maintenance, which proves its public capabilities during preflight.
    if version not in SUPPORTED_UV_TOOL_VERSIONS:
        raise PackageUpdateError(
            f"uv {version!r} is outside the tested uv-tool layout adapter versions "
            f"({', '.join(sorted(SUPPORTED_UV_TOOL_VERSIONS))}); use uv manually. Normal Netizen operation is unaffected."
        )


def _tool_receipt(prefix: Path, info: dict[str, Any], env: Mapping[str, str]) -> tuple[Path, Path]:
    receipt = prefix / "uv-receipt.toml"
    try:
        if receipt.is_symlink():
            raise ValueError("receipt symlink is not a supported ownership marker")
        document = tomllib.loads(receipt.read_text())
        tool = document["tool"]
        requirement = tool["requirements"][0]
        # Only the tested modern name object is accepted, not a PEP 508 parser.
        if not isinstance(requirement, dict) or _canonical_name(requirement["name"]) != DISTRIBUTION:
            raise ValueError("wrong requirement identity")
        entries = [e for e in tool["entrypoints"] if e.get("name") == "netizen"]
        if len(entries) != 1 or _canonical_name(entries[0].get("from", DISTRIBUTION)) != DISTRIBUTION:
            raise ValueError("wrong command identity")
        destination = Path(entries[0]["install-path"])
        installed_script = Path(info["scripts_dir"]) / "netizen"
        if not installed_script.resolve().is_relative_to(prefix):
            raise ValueError("environment command points outside the environment")
        if not destination.is_absolute() or not destination.samefile(installed_script):
            raise ValueError("command destination does not refer to this environment")
        requested_bin = env.get("UV_TOOL_BIN_DIR")
        if requested_bin and Path(requested_bin).resolve() != destination.parent.resolve():
            raise ValueError("UV_TOOL_BIN_DIR differs from the installed command location")
    except (OSError, KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise PackageUpdateError(
            f"uv tool identity is missing, damaged or unsupported at {receipt}; "
            "repair the tool installation or update it manually."
        ) from exc
    return receipt, destination.parent.resolve()


def _pip_changes(command: list[str], env: Mapping[str, str], cwd: Path) -> bool:
    report = cwd / "pip-preflight-report.json"
    if report.exists():
        raise PackageUpdateError(f"Preflight report already exists at {report}; retry with a fresh work directory.")
    result = _run([*command, "--dry-run", "--report", str(report)], env=env, cwd=cwd)
    _require_success(result, "pip dependency preflight")
    try:
        data = _object(report.read_text(), "pip installation report")
    except OSError as exc:
        raise PackageUpdateError("pip did not produce its documented installation report.") from exc
    if data.get("version") != "1" or not isinstance(data.get("install"), list):
        raise PackageUpdateError("Unsupported pip installation-report version or install list; update manually.")
    for item in data["install"]:
        if (not isinstance(item, dict) or not isinstance(item.get("metadata"), dict)
                or not isinstance(item["metadata"].get("name"), str)
                or not isinstance(item["metadata"].get("version"), str)):
            raise PackageUpdateError("pip returned an incomplete installation plan; update manually.")
    return bool(data["install"])


def _uv_pip_changes(command: list[str], env: Mapping[str, str], cwd: Path) -> bool:
    check = _run([*command, "--check"], env=env, cwd=cwd)
    if check.returncode == 0:
        return False
    if check.returncode != 1:
        _require_success(check, "uv pip no-change preflight")
    # Exit 1 includes resolution failure. Require a successful *subsequent*
    # resolution before stopping anything. No state is inferred from log text.
    resolved = _run([*command, "--dry-run"], env=env, cwd=cwd)
    _require_success(resolved, "uv pip dependency preflight")
    return True


def prepare_update(
    *, work_dir: Path, backend: str | None = None, python: str | None = None,
) -> dict[str, Any]:
    """Identify this installation and resolve a non-mutating update plan.

    ``backend`` is explicit maintenance intent for an ordinary self-managed
    environment, never permission to override conflicting ownership. INSTALLER
    is only the default choice between pip and uv-pip in such an environment.
    The returned command must only run after all affected services have stopped.
    This function does not create ``work_dir``; it must be private, fresh and
    outside the installation. Environment values are inherited, not serialized;
    only nonsecret target-fixing overrides are included in the plan.
    """
    if backend not in (None, "pip", "uv-pip", "uv-tool"):
        raise PackageUpdateError("Unsupported package backend; choose pip, uv-pip or uv-tool.")
    cwd = Path(work_dir).resolve()
    if not cwd.is_dir():
        raise PackageUpdateError("Package preflight requires an existing private work directory.")
    executable = os.path.abspath(python or sys.executable)  # Never realpath a venv interpreter.
    env = dict(os.environ)
    probe = _run([executable, "-I", "-c", _PROBE, "{}", "import"], env=env, cwd=cwd)
    if probe.returncode:
        detail = _object(probe.stdout, "Installed Python/package identity check").get("error")
        if isinstance(detail, str):
            raise PackageUpdateError(
                f"Installed Python/package identity check failed: {detail}. "
                "No services have been stopped; repair this installation or update manually.",
                diagnostic=probe.stderr,
            )
    _require_success(probe, "Installed Python/package identity check")
    info = _object(probe.stdout, "Installed Python/package identity check")
    required = ("prefix", "base_prefix", "package_dir", "dist_info", "version", "installer", "scripts_dir")
    if any(not isinstance(info.get(k), str) for k in required):
        raise PackageUpdateError("Incomplete installed package identity; update manually.")
    prefix = Path(info["prefix"])
    if cwd.is_relative_to(prefix):
        raise PackageUpdateError("Update work directory must be outside the replaced Python environment.")
    # The default entry must really be the installed module, not a checkout
    # shadowing another installed distribution via cwd/PYTHONPATH.
    if python is None and (Path(sys.prefix).resolve() != prefix
                           or Path(__file__).resolve().parent != Path(info["package_dir"])):
        raise PackageUpdateError("The running CLI does not match its installed distribution; use the installed CLI, not a source checkout.")
    evidence = _check_environment_layout(info, env)
    info_dir = Path(info["dist_info"])
    evidence.extend([info_dir / "METADATA", info_dir / "entry_points.txt"])
    if (info_dir / "INSTALLER").exists():
        evidence.append(info_dir / "INSTALLER")
    overrides: dict[str, str] = {}
    paths = {str(prefix): _identity(prefix), executable: _identity(Path(executable)),
             str(Path(executable).parent): _identity(Path(executable).parent)}
    uv: str | None = None
    uv_version: str | None = None
    tool_root: Path | None = None
    receipt_exists = (prefix / "uv-receipt.toml").exists()
    if info["installer"] == "uv" or receipt_exists or backend in ("uv-pip", "uv-tool"):
        uv = _uv_executable(env)
        uv_version, tool_root, cache_root = _uv_context(uv, env, cwd)
        _reject_uv_cache_environment(prefix, cache_root)
    tool_matches = tool_root is not None and (tool_root / DISTRIBUTION).resolve() == prefix
    if (not tool_matches and not receipt_exists and info["installer"] == "uv"
            and prefix.name == DISTRIBUTION):
        raise PackageUpdateError("This tool-shaped environment has no verifiable uv receipt or tool root; repair its ownership metadata or update manually.")
    if receipt_exists or tool_matches:
        if not tool_matches:
            raise PackageUpdateError("This looks like a uv tool environment but UV_TOOL_DIR does not match it; specify the original tool directory or update manually.")
        if backend not in (None, "uv-tool"):
            raise PackageUpdateError("This environment belongs to uv tool; another backend cannot take it over.")
        if (tool_root / DISTRIBUTION).is_symlink():
            raise PackageUpdateError("A symlinked whole uv tool environment is not a tested update layout; use uv manually.")
        assert uv_version is not None
        _require_uv_tool_version(uv_version)
        receipt, bin_dir = _tool_receipt(prefix, info, env)
        evidence.append(receipt)
        evidence.append(Path(info["scripts_dir"]) / "netizen")
        paths[str(tool_root)] = _identity(tool_root)
        overrides.update({"UV_TOOL_DIR": str(tool_root), "UV_TOOL_BIN_DIR": str(bin_dir)})
        selected = "uv-tool"
    else:
        if backend == "uv-tool":
            raise PackageUpdateError("The selected uv tool directory does not own this Python environment.")
        known = {"pip": "pip", "uv": "uv-pip"}.get(info["installer"])
        if info["installer"] and known is None:
            raise PackageUpdateError(f"Installer {info['installer']!r} is unsupported; use its owning package manager.")
        selected = backend or known
        if selected is None:
            raise PackageUpdateError("Installer identity is absent. Explicitly choose --via pip or --via uv-pip for a self-managed environment, or update manually.")
    _check_environment_options(selected, env)
    if selected == "pip":
        if not info.get("pip_available"):
            raise PackageUpdateError("This environment has no pip. Netizen will not install one or choose an unrelated pip; update manually.")
        overrides.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        command = [executable, "-I", "-m", "pip", "install", "--upgrade", "--no-input", DISTRIBUTION]
    elif selected == "uv-pip":
        assert uv is not None
        command = [uv, "--no-python-downloads", "pip", "install", "--python", executable,
                   "--upgrade", DISTRIBUTION]
    else:
        assert uv is not None
        command = [uv, "--no-python-downloads", "tool", "upgrade", DISTRIBUTION]
    env.update(overrides)
    if uv is not None:
        paths[uv] = _identity(Path(uv))
    fingerprints = {str(p): _digest(p) for p in evidence}
    changes = (_pip_changes(command, env, cwd) if selected == "pip" else
               _uv_pip_changes(command, env, cwd) if selected == "uv-pip" else True)
    if any(_digest(Path(p)) != digest for p, digest in fingerprints.items()):
        raise PackageUpdateError("Installation metadata changed during preflight; retry after external maintenance finishes.")
    if any(_identity(Path(p)) != identity for p, identity in paths.items()):
        raise PackageUpdateError("Installation target changed during preflight; retry after external maintenance finishes.")
    expected = {"prefix": str(prefix), "package_dir": info["package_dir"]}
    before_expected = {**expected, "version": info["version"]}
    return {
        "environment_python": executable, "prefix": str(prefix), "backend": selected,
        "before_version": info["version"], "command": command,
        "command_env": overrides, "command_cwd": str(cwd), "changes_required": changes,
        "revalidation_files": fingerprints,
        "revalidation_paths": paths,
        "revalidation_command": [executable, "-I", "-c", _PROBE, json.dumps(before_expected), "import"],
        "validation_command": [executable, "-I", "-c", _PROBE, json.dumps(expected), "import"],
        "uv_version": uv_version,
        "notice": ("uv tool has no equivalent dry-run; originally running instances may restart even without a package change."
                   if selected == "uv-tool" else "Preflight does not reserve candidates against external package/index changes."),
    }
