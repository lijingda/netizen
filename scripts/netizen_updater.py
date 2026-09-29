#!/usr/bin/env python3
"""Retired entry point: deliberately performs no deployment actions."""
import sys


def main() -> int:
    print("The legacy per-instance updater is retired. Use netizen update for a CLI installation. Existing release layouts require manual migration; see docs/cli.md.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
