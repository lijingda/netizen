"""The instance's protected Lark application credentials, in CLI profile format.

This module uses only the standard library so installation and the service share
the same file contract without depending on the optional Lark CLI.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


LARK_APP_PROFILE = "netizen"
LARK_APP_RELATIVE_PATH = Path("lark-app/config.json")
_MAX_CONFIG_BYTES = 65_536
_APP_ID = re.compile(r"cli_[A-Za-z0-9_-]+")


class LarkAppConfigError(ValueError):
    """A credential configuration failure that is safe to display."""


@dataclass(frozen=True, slots=True)
class LarkAppCredentials:
    app_id: str
    app_secret: str = field(repr=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LarkAppConfigError("Lark app configuration has duplicate JSON fields")
        result[key] = value
    return result


def _validate_credentials(
    app_id: Any, app_secret: Any, *, allow_incomplete: bool,
) -> LarkAppCredentials:
    if not isinstance(app_id, str) or (
        not _APP_ID.fullmatch(app_id) and not (allow_incomplete and app_id == "")
    ):
        raise LarkAppConfigError("Lark app profile netizen requires a valid cli_ App ID")
    if (
        not isinstance(app_secret, str)
        or app_secret.strip() != app_secret
        or any(ord(character) < 0x20 for character in app_secret)
        or (not app_secret and not allow_incomplete)
    ):
        raise LarkAppConfigError("Lark app profile netizen requires a non-empty App Secret string")
    return LarkAppCredentials(app_id=app_id, app_secret=app_secret)


def parse_lark_app(
    content: bytes, *, allow_incomplete: bool = False,
) -> LarkAppCredentials:
    if len(content) > _MAX_CONFIG_BYTES:
        raise LarkAppConfigError("Lark app configuration exceeds the size limit")
    try:
        config = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        raise LarkAppConfigError("Lark app configuration must contain valid, unambiguous JSON") from None
    if not isinstance(config, dict) or not isinstance(config.get("apps"), list):
        raise LarkAppConfigError("Lark app configuration requires an apps array")
    profiles = [
        app for app in config["apps"]
        if isinstance(app, dict) and app.get("name") == LARK_APP_PROFILE
    ]
    if len(profiles) != 1:
        raise LarkAppConfigError("Lark app configuration requires exactly one profile named netizen")
    profile = profiles[0]
    if profile.get("brand") != "feishu":
        raise LarkAppConfigError("Lark app profile netizen requires brand feishu")
    return _validate_credentials(
        profile.get("appId"), profile.get("appSecret"), allow_incomplete=allow_incomplete,
    )


def load_lark_app(
    path: str | Path, *, allow_incomplete: bool = False,
) -> LarkAppCredentials:
    path = Path(path)
    if not path.is_absolute():
        raise LarkAppConfigError("Lark app configuration path must be absolute")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                raise LarkAppConfigError(
                    "Lark app configuration must be an owned regular file with permissions 0600 or stricter"
                )
            content = stream.read(_MAX_CONFIG_BYTES + 1)
    except OSError:
        raise LarkAppConfigError("Lark app configuration cannot be read; check its path and file permissions") from None
    return parse_lark_app(content, allow_incomplete=allow_incomplete)


def encode_lark_app(app_id: str, app_secret: str) -> bytes:
    credentials = _validate_credentials(app_id, app_secret, allow_incomplete=True)
    config = {
        "currentApp": LARK_APP_PROFILE,
        "apps": [{
            "name": LARK_APP_PROFILE,
            "appId": credentials.app_id,
            "appSecret": credentials.app_secret,
            "brand": "feishu",
            "defaultAs": "bot",
            "users": [],
        }],
    }
    content = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(content) > _MAX_CONFIG_BYTES:
        raise LarkAppConfigError("Lark app configuration exceeds the size limit")
    return content
