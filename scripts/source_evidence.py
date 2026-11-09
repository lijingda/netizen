"""Shared checkout identity for review tools and disposable probes."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess


def source_evidence() -> dict[str, object]:
    """Identify the checkout without exposing its path, remote or patch content."""
    root = Path(__file__).resolve().parents[1]

    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(root), *args],
                              check=True, capture_output=True, timeout=10).stdout

    patch = git("diff", "--binary", "HEAD", "--")
    return {"source_commit": git("rev-parse", "HEAD").decode().strip(),
            "source_dirty": bool(git("status", "--porcelain")),
            "source_diff_sha256": hashlib.sha256(patch).hexdigest()}
