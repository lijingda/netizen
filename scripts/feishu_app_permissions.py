#!/usr/bin/env python3
"""Check the effective tenant permission contract for the configured app."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, IO


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.feishu_app_onboarding import (  # noqa: E402
    REQUIRED_TENANT_SCOPES,
    TENANT_SCOPE_ALTERNATIVES,
)
from netizen.lark_app import load_lark_app  # noqa: E402


class PermissionCheckError(RuntimeError):
    """The platform did not return a trustworthy tenant permission state."""


ScopeLister = Callable[[], Any]


def _missing_required_scopes(response: Any) -> tuple[str, ...]:
    success = getattr(response, "success", None)
    if not callable(success) or not success():
        raise PermissionCheckError("the tenant permission query failed")
    data = getattr(response, "data", None)
    scopes = getattr(data, "scopes", None)
    if not isinstance(scopes, list):
        raise PermissionCheckError("the tenant permission query returned invalid data")

    granted: set[str] = set()
    for scope in scopes:
        name = getattr(scope, "scope_name", None)
        status = getattr(scope, "grant_status", None)
        scope_type = getattr(scope, "scope_type", None)
        if (
            not isinstance(name, str)
            or not name
            or isinstance(status, bool)
            or not isinstance(status, int)
            or scope_type not in {"tenant", "user"}
        ):
            raise PermissionCheckError(
                "the tenant permission query returned an invalid scope"
            )
        if scope_type == "tenant" and status == 1:
            granted.add(name)
    return tuple(
        scope
        for scope in REQUIRED_TENANT_SCOPES
        if not (TENANT_SCOPE_ALTERNATIVES.get(scope, frozenset((scope,))) & granted)
    )


def run_permission_check(
    *,
    list_scopes: ScopeLister,
    stdout: IO[str] | None = None,
) -> None:
    output = sys.stdout if stdout is None else stdout
    missing = _missing_required_scopes(list_scopes())
    print(
        json.dumps(
            {"version": 1, "missingScopes": list(missing)},
            separators=(",", ":"),
        ),
        file=output,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lark-app-config", type=Path, required=True)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str, str], Any] | None = None,
) -> int:
    args = parse_args(argv)
    try:
        credentials = load_lark_app(args.lark_app_config)
        if client_factory is None:
            import lark_oapi as lark
            from lark_oapi.api.application.v6 import ListScopeRequest

            client = (
                lark.Client.builder()
                .app_id(credentials.app_id)
                .app_secret(credentials.app_secret)
                .log_level(lark.LogLevel.ERROR)
                .timeout(15)
                .build()
            )
        else:
            client = client_factory(credentials.app_id, credentials.app_secret)

        def list_scopes() -> Any:
            if client_factory is None:
                request = ListScopeRequest.builder().build()
            else:
                request = None
            return client.application.v6.scope.list(request)

        run_permission_check(list_scopes=list_scopes)
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Feishu/Lark permission verification did not complete.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
