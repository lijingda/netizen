"""Identify the optional SDK's bundled executable in this Python installation."""
from importlib import metadata
import os
from pathlib import Path


SDK_VERSION = "0.2.186"
CLI_VERSION = "2.1.329"
REPAIR = f"请在此实例绑定的 Python 环境安装或修复 netizen-cli[claude]（SDK {SDK_VERSION}，CLI {CLI_VERSION}），然后重试。"


def verified_claude_cli() -> Path:
    try:
        if metadata.version("claude-agent-sdk") != SDK_VERSION:
            raise ValueError("SDK version")
        dist = metadata.distribution("claude-agent-sdk")
        expected = {"claude_agent_sdk/_bundled/claude", "claude_agent_sdk/_bundled/claude.version"}
        records = [item for item in dist.files or () if str(item) in expected]
        if len(records) != 2 or {str(item) for item in records} != expected:
            raise ValueError("bundled resources")
        paths = {item.name: Path(str(dist.locate_file(item))) for item in records}
        binary, marker = paths["claude"], paths["claude.version"]
        if (marker.is_symlink() or marker.read_text(encoding="ascii").strip() != f"{CLI_VERSION} (Claude Code)"
                or binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK)):
            raise ValueError("bundled executable/version")
        from claude_agent_sdk import ClaudeSDKClient

        if not callable(ClaudeSDKClient):
            raise ValueError("SDK client")
        return binary
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError(REPAIR) from exc
