#!/usr/bin/env python3
"""Return temporary Netizen bot credentials as JSON for a local caller to capture."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys


def credentials() -> dict[str, str]:
    if sys.platform == "darwin":
        import truststore

        truststore.inject_into_ssl()

    import yaml
    import lark_oapi as lark
    from lark_oapi.api.auth.v3 import (
        InternalTenantAccessTokenRequest,
        InternalTenantAccessTokenRequestBody,
    )

    root = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".netizen"
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    app_id = config["instance"]["appId"]
    if not isinstance(app_id, str) or not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id):
        raise ValueError("invalid App ID")
    descriptor = os.open(
        root / "credentials/feishu-app-secret", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise ValueError("unprotected credential")
        content = stream.read(4097)
    secret = content.decode("utf-8").strip()
    if not secret or len(content) > 4096:
        raise ValueError("invalid credential")

    client = (lark.Client.builder().app_id(app_id).app_secret(secret)
              .timeout(20).log_level(lark.LogLevel.ERROR).build())
    request = InternalTenantAccessTokenRequest.builder().request_body(
        InternalTenantAccessTokenRequestBody.builder()
        .app_id(app_id).app_secret(secret).build()
    ).build()
    response = client.auth.v3.tenant_access_token.internal(request)
    if not response.success():
        raise ValueError("bot authentication failed")
    # The official auth endpoint returns its token at the response root.
    token = json.loads(response.raw.content).get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("missing bot token")
    return {"app_id": app_id, "tenant_access_token": token}


def main() -> int:
    try:
        result = credentials()
    except Exception:
        # SDK/configuration errors may contain secrets; never echo raw diagnostics.
        print("无法获取 Netizen 机器人凭据；请检查本机安装配置、凭据文件权限和网络，"
              "并使用 current/venv/bin/python 执行。", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
