"""Small YAML configuration and protected Lark application profile loading."""

from __future__ import annotations

import os
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .lark_app import LARK_APP_RELATIVE_PATH, LarkAppConfigError, load_lark_app
from .admin.port_config import ConfigFileSnapshot
from .sessions.policy import SessionAccessPolicy, SessionPolicyError
from .worktrees.settings import WorktreeSettings, WorktreeSettingsError
from .progress_card_settings import ProgressCardSettings


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdminWebSettings:
    host: str = "0.0.0.0"
    port: int | None = None
    access_host: str | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    app_id: str
    app_secret: str = field(repr=False)
    project_root: Path
    data_dir: Path
    projects: dict[str, Path] = field(default_factory=dict)
    security_mode: str = "audit"
    admin_web: AdminWebSettings = AdminWebSettings()
    session_tools: SessionAccessPolicy = SessionAccessPolicy()
    worktrees: WorktreeSettings = WorktreeSettings()
    progress_card: ProgressCardSettings = ProgressCardSettings()
    config_path: Path | None = None
    config_snapshot: ConfigFileSnapshot | None = field(default=None, repr=False)

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> "Settings":
        env = os.environ if environment is None else environment
        config_path = Path(path).expanduser().resolve(strict=True)
        config_snapshot = ConfigFileSnapshot.read(config_path)
        try:
            loaded = yaml.safe_load(config_snapshot.content)
        except yaml.YAMLError as error:
            raise SettingsError(f"invalid YAML: {error}") from error
        if not isinstance(loaded, dict):
            raise SettingsError("configuration root must be a mapping")
        if "access" in loaded:
            raise SettingsError(
                "access allowlists are not supported; configure availability "
                "in the Feishu app console"
            )

        instance = _mapping(loaded, "instance")
        channel = _optional_mapping(loaded, "channel")
        admin_web = _admin_web_settings(loaded)
        try:
            session_tools = SessionAccessPolicy.from_mapping(loaded.get("sessionTools", {}))
        except SessionPolicyError as error:
            raise SettingsError(str(error)) from error
        try:
            worktrees = WorktreeSettings.from_mapping(loaded.get("worktrees", {}))
        except WorktreeSettingsError as error:
            raise SettingsError(str(error)) from error
        try:
            progress_card = (ProgressCardSettings.from_dict(loaded["progressCard"])
                             if "progressCard" in loaded else ProgressCardSettings())
        except ValueError as error:
            raise SettingsError(str(error)) from error
        raw_projects = loaded.get("projects", {})
        if not isinstance(raw_projects, dict):
            raise SettingsError("projects must map aliases to absolute paths")

        if env.get("FEISHU_APP_SECRET") or env.get("FEISHU_APP_SECRET_FILE"):
            raise SettingsError(
                "Feishu secret environment sources are no longer supported; use NETIZEN_LARK_APP_CONFIG"
            )
        credential_path = env.get("NETIZEN_LARK_APP_CONFIG", "").strip()
        try:
            credentials = load_lark_app(
                Path(credential_path) if credential_path
                else config_path.parent / LARK_APP_RELATIVE_PATH
            )
        except LarkAppConfigError as error:
            raise SettingsError(str(error)) from None
        data_dir = _absolute_path(instance, "dataDir")
        project_root = _absolute_path(instance, "projectRoot")
        projects: dict[str, Path] = {}
        for alias, value in raw_projects.items():
            if not isinstance(alias, str) or not isinstance(value, str):
                raise SettingsError("projects must map string aliases to string paths")
            project_path = Path(value).expanduser()
            if not project_path.is_absolute():
                raise SettingsError(f"Project {alias} path must be absolute")
            projects[alias] = project_path

        security_mode = channel.get("securityMode", "audit")
        if not isinstance(security_mode, str) or security_mode not in {
            "compat",
            "audit",
            "strict",
        }:
            raise SettingsError("channel.securityMode must be compat, audit, or strict")

        return cls(
            app_id=credentials.app_id,
            app_secret=credentials.app_secret,
            data_dir=data_dir,
            project_root=project_root,
            projects=projects,
            security_mode=security_mode,
            admin_web=admin_web,
            session_tools=session_tools,
            worktrees=worktrees,
            progress_card=progress_card,
            config_path=config_path,
            config_snapshot=config_snapshot,
        )


def _mapping(parent: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise SettingsError(f"{name} must be a mapping")
    return value


def _optional_mapping(parent: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if name not in parent:
        return {}
    return _mapping(parent, name)


def _string(parent: Mapping[str, Any], name: str) -> str:
    value = parent.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{name} must be a non-empty string")
    return value.strip()


def _absolute_path(parent: Mapping[str, Any], name: str) -> Path:
    value = Path(_string(parent, name)).expanduser()
    if not value.is_absolute():
        raise SettingsError(f"{name} must be an absolute path")
    return value


def _admin_web_settings(
    loaded: Mapping[str, Any],
) -> AdminWebSettings:
    values = _optional_mapping(loaded, "adminWeb")
    unknown = values.keys() - {"host", "port", "accessHost"}
    if unknown:
        raise SettingsError("adminWeb contains unsupported settings")
    host = values.get("host", "0.0.0.0")
    if (
        not isinstance(host, str)
        or not host
        or host.strip() != host
        or any(ord(character) < 0x20 for character in host)
    ):
        raise SettingsError("adminWeb.host must be a non-empty host")
    port = values.get("port")
    if "port" in values and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise SettingsError("adminWeb.port must be an integer from 1 to 65535")
    access_host = validate_access_host(values["accessHost"]) if "accessHost" in values else None
    return AdminWebSettings(
        host=host,
        port=port,
        access_host=access_host,
    )


def validate_access_host(value: object) -> str:
    """A hostname or bare IP only; never a URL, port, or wildcard listener."""

    message = "adminWeb.accessHost must be a hostname or IP without scheme, port, path, or wildcard"
    if not isinstance(value, str) or not value or value.strip() != value:
        raise SettingsError(message)
    if any(character in value for character in "/\\@?#[]%"):
        raise SettingsError(message)
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            hostname = value.removesuffix(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise SettingsError(message) from None
        if len(hostname) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in hostname.split(".")
        ):
            raise SettingsError(message)
        return hostname
    if address.is_unspecified:
        raise SettingsError(message)
    return address.compressed
