#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 -E -B -u "$SCRIPT_DIR/scripts/netizen_installer.py" uninstall "$@"
