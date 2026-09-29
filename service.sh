#!/bin/sh
# Compatibility tombstone: never select Python, touch an instance, or download.
printf '%s\n' \
    'The legacy release service wrapper has been retired.' \
    'Use the installed CLI: netizen status|start|stop|restart --root PATH' \
    'Existing service bindings retain their selected Python environment.' \
    'Existing legacy installations require manual conversion. No files or services were changed.' >&2
exit 2
