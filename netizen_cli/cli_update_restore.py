"""Fresh-install, one-shot update restore under an inherited exact root lock.

Not a public lifecycle command: unlike ``start``, this cannot register or change
a binding. Keeping the updater's lock avoids a release/reacquire window between
package validation and readiness, and serializes existing Admin/CLI maintenance.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Sequence

from .deployment.update_protocol import validate_inherited_lock
from .instance import require_instance_root_marker


def restore(root: Path, descriptor: int, *, expected_prefix: Path,
            expected_python: Path, manager: Any = None) -> None:
    if root != root.resolve() or not root.is_absolute():
        raise ValueError("Update restoration requires the recorded canonical root")
    require_instance_root_marker(root)
    validate_inherited_lock(root, descriptor)
    if manager is None:
        from .cli_services import ServiceManager
        manager = ServiceManager()
    status = manager.inspect(root)
    if (status is None or status.binding.prefix != expected_prefix
            or status.binding.python != expected_python):
        raise RuntimeError("Service binding changed; update restoration will not register or rebind it")
    ready = manager.start(root)
    if not ready.ready:
        raise RuntimeError("Instance did not confirm readiness")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private Netizen update restoration entry")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--expected-prefix", type=Path, required=True)
    parser.add_argument("--expected-python", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        restore(args.root, args.lock_fd, expected_prefix=args.expected_prefix,
                expected_python=args.expected_python)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Instance restoration failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
