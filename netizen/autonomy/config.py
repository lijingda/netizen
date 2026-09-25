"""Admin-owned, atomic private configuration; no file watching or secret projection."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .models import AutonomyError, DecisionConfig


DEFAULTS = {
    "jev": ("https://api.typesafe.ai", "jev-1.13.0", 32000),
    "laya": ("http://127.0.0.1:8000", "multilingual", 1024),
}
LAYA_MODELS = {"english", "multilingual", "typed-decisions", "convaiinnovations/laya-multilingual", "convaiinnovations/laya-typed-decisions"}
CONFIG_LIMIT = 16384


def parse_config(payload: dict, previous: DecisionConfig | None = None) -> DecisionConfig:
    if not isinstance(payload, dict):
        raise AutonomyError("invalid decision configuration")
    provider = payload.get("provider", previous.provider if previous else "jev")
    if not isinstance(provider, str) or provider not in DEFAULTS:
        raise AutonomyError("unsupported decision provider")
    base, model, budget = DEFAULTS[provider]
    same = previous is not None and previous.provider == provider
    base = payload.get("base_url", previous.base_url if same else base)
    model = payload.get("model", previous.model if same else model)
    if not isinstance(base, str) or not base or len(base) > 2048 or any(char.isspace() for char in base):
        raise AutonomyError("invalid decision service URL")
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError:
        raise AutonomyError("invalid decision service URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or port == 0:
        raise AutonomyError("decision service URL must use HTTP(S) without credentials or query")
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(char) < 32 for char in model):
        raise AutonomyError("invalid decision model")
    if provider == "laya" and model not in LAYA_MODELS:
        raise AutonomyError("unsupported Laya checkpoint; choose english, multilingual or typed-decisions")
    key = payload.get("api_key")
    if key is not None and not isinstance(key, str):
        raise AutonomyError("invalid decision credential")
    # Never silently forward an existing secret to a newly supplied destination.
    if not key:
        key = previous.api_key if same and previous.base_url == base.rstrip("/") else ""
    if payload.get("clear_api_key") is True:
        key = ""
    if len(key) > 8192 or any(not 32 <= ord(char) <= 126 for char in key):
        raise AutonomyError("invalid decision credential")
    if provider == "jev" and not key:
        raise AutonomyError("Jev requires an API key; re-enter it when changing the service URL")
    timeout = payload.get("timeout_seconds", previous.timeout_seconds if same else 10)
    ceiling = 512 if provider == "laya" and model == "english" else budget
    requested_budget = payload.get("input_budget", min(previous.input_budget, ceiling) if same else ceiling)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.1 <= timeout <= 60:
        raise AutonomyError("decision timeout must be between 0.1 and 60 seconds")
    if isinstance(requested_budget, bool) or not isinstance(requested_budget, int) or not 512 <= requested_budget <= ceiling:
        raise AutonomyError("input budget exceeds the selected model's supported range")
    return DecisionConfig(provider, base.rstrip("/"), model, key, float(timeout), requested_budget)


def _safe_parent(path: Path) -> None:
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise AutonomyError("decision configuration cannot use symlink directories")


def load_config(path: Path) -> tuple[int, DecisionConfig | None]:
    _safe_parent(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return 0, None
    except OSError:
        raise AutonomyError("cannot read private decision configuration") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise AutonomyError("decision configuration must be a private owner-readable file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(CONFIG_LIMIT + 1)
        if len(raw) > CONFIG_LIMIT:
            raise AutonomyError("decision configuration is too large")
        data = json.loads(raw)
        revision = data["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError
        config = parse_config(data["config"]) if data["config"] is not None else None
        return revision, config
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AutonomyError("invalid private decision configuration") from None
    except OSError:
        raise AutonomyError("cannot read private decision configuration") from None
    finally:
        if fd != -1:
            os.close(fd)


def save_config(path: Path, revision: int, config: DecisionConfig | None) -> None:
    _safe_parent(path)
    if path.is_symlink():
        raise AutonomyError("decision configuration cannot be a symlink")
    data = json.dumps({"revision": revision, "config": asdict(config) if config else None}, ensure_ascii=False).encode()
    if len(data) > CONFIG_LIMIT:
        raise AutonomyError("decision configuration is too large")
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".decision-", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise AutonomyError("could not save private decision configuration") from None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def public_config(config: DecisionConfig | None) -> dict | None:
    if config is None:
        return None
    return {"provider": config.provider, "base_url": config.base_url, "model": config.model,
            "has_api_key": bool(config.api_key), "timeout_seconds": config.timeout_seconds,
            "input_budget": config.input_budget}
