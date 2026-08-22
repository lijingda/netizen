"""Small YAML configuration and protected Feishu secret loading."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdminWebSettings:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8787
    credential_path: Path | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Settings:
    app_id: str
    app_secret: str = field(repr=False)
    data_dir: Path = Path(".netizen-data")
    default_cwd: Path = Path.cwd()
    project_root: Path = Path.cwd()
    projects: dict[str, Path] = field(default_factory=dict)
    security_mode: str = "audit"
    admin_web: AdminWebSettings = AdminWebSettings()

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> "Settings":
        env = os.environ if environment is None else environment
        config_path = Path(path).expanduser().resolve(strict=True)
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
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
        admin_web = _admin_web_settings(loaded, env)
        raw_projects = loaded.get("projects", {})
        if not isinstance(raw_projects, dict):
            raise SettingsError("projects must map aliases to absolute paths")

        app_id = _string(instance, "appId")
        if not app_id.startswith("cli_"):
            raise SettingsError("instance.appId must be a Feishu cli_ identifier")
        data_dir = _absolute_path(instance, "dataDir")
        default_cwd = _absolute_path(instance, "defaultCwd")
        project_root = (
            _absolute_path(instance, "projectRoot")
            if "projectRoot" in instance
            else default_cwd.parent
        )
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
            app_id=app_id,
            app_secret=_load_secret(env),
            data_dir=data_dir,
            default_cwd=default_cwd,
            project_root=project_root,
            projects=projects,
            security_mode=security_mode,
            admin_web=admin_web,
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
    environment: Mapping[str, str],
) -> AdminWebSettings:
    raw_secret = environment.get("NETIZEN_ADMIN_SECRET", "")
    if raw_secret:
        raise SettingsError(
            "NETIZEN_ADMIN_SECRET is not supported; use "
            "NETIZEN_ADMIN_SECRET_FILE"
        )
    values = _optional_mapping(loaded, "adminWeb")
    unknown = values.keys() - {"enabled", "host", "port"}
    if unknown:
        raise SettingsError("adminWeb contains unsupported settings")
    enabled = values.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SettingsError("adminWeb.enabled must be a boolean")
    host = values.get("host", "0.0.0.0")
    if (
        not isinstance(host, str)
        or not host
        or host.strip() != host
        or any(ord(character) < 0x20 for character in host)
    ):
        raise SettingsError("adminWeb.host must be a non-empty host")
    port = values.get("port", 8787)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SettingsError("adminWeb.port must be an integer from 1 to 65535")
    raw_path = environment.get("NETIZEN_ADMIN_SECRET_FILE", "").strip()
    credential_path: Path | None = None
    if raw_path:
        credential_path = Path(raw_path)
        if not credential_path.is_absolute():
            raise SettingsError("NETIZEN_ADMIN_SECRET_FILE must be an absolute path")
    elif enabled:
        raise SettingsError(
            "NETIZEN_ADMIN_SECRET_FILE is required when Admin Web is enabled"
        )
    return AdminWebSettings(
        enabled=enabled,
        host=host,
        port=port,
        credential_path=credential_path,
    )


def _load_secret(environment: Mapping[str, str]) -> str:
    direct = environment.get("FEISHU_APP_SECRET", "").strip()
    file_value = environment.get("FEISHU_APP_SECRET_FILE", "").strip()
    if direct and file_value:
        raise SettingsError("configure only one Feishu secret source")
    if direct:
        return direct
    if not file_value:
        raise SettingsError("FEISHU_APP_SECRET_FILE or FEISHU_APP_SECRET is required")
    path = Path(file_value)
    if not path.is_absolute():
        raise SettingsError("FEISHU_APP_SECRET_FILE must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SettingsError("Feishu secret file cannot be read") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SettingsError("Feishu secret must be a regular non-symlink file")
    if metadata.st_mode & 0o077:
        raise SettingsError("Feishu secret file permissions must be 0600 or stricter")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SettingsError("Feishu secret file cannot be read") from error
    if not secret:
        raise SettingsError("Feishu secret file must not be empty")
    return secret
