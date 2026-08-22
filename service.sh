#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: ./service.sh {start|stop|restart|status}" >&2
    exit 2
fi

case "$1" in
    start|stop|restart|status) ;;
    *)
        echo "usage: ./service.sh {start|stop|restart|status}" >&2
        exit 2
        ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 -E -B -u "$SCRIPT_DIR/scripts/netizen_installer.py" service "$1"
