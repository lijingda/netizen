#!/usr/bin/env python3
"""Retired entry point: deliberately performs no deployment actions."""
import sys


def main() -> int:
    print("The legacy release artifact builder is retired. Use scripts/build_cli_distribution.py for wheel/sdist artifacts.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
