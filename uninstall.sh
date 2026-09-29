#!/bin/sh
# Compatibility tombstone: never select Python, touch an instance, or download.
printf '%s\n' \
    'The legacy release uninstaller has been retired.' \
    'Use: netizen remove --root PATH  (instance data is preserved by default).' \
    'Before uninstalling the program, remove or transfer every associated service.' \
    'Then use the original package manager, for example: python -m pip uninstall netizen-cli' \
    'Do not use --purge when transferring an instance to another environment.' \
    'Existing legacy installations require manual conversion. No files or services were changed.' >&2
exit 2
