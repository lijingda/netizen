#!/usr/bin/env python3
"""Retired entry point: deliberately performs no deployment actions."""
import sys


def main() -> int:
    print("The legacy per-instance installer is retired. Install netizen-cli with your Python package manager, then use netizen setup. Existing release layouts require manual migration; see docs/cli.md.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
