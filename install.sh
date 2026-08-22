#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "usage: ./install.sh" >&2
    exit 2
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 -E -B -u "$SCRIPT_DIR/scripts/netizen_installer.py" install
