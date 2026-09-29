#!/bin/sh
# Compatibility tombstone: never select Python, touch an instance, or download.
printf '%s\n' \
    'The legacy source installer has been retired; Netizen no longer creates per-instance Python environments.' \
    'From this source checkout, select your Python environment and run:' \
    '  python -m pip install -e .' \
    '  netizen setup --root PATH' \
    '  netizen start --root PATH' \
    'Existing legacy installations require manual conversion. No files or services were changed.' >&2
exit 2
