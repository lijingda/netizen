#!/bin/sh
# Compatibility tombstone: never select Python, touch an instance, or download.
printf '%s\n' \
    'The legacy per-instance release installer has been retired.' \
    'Install the CLI in your selected Python environment:' \
    '  python -m pip install netizen-cli' \
    'Or, from a source checkout:' \
    '  python -m pip install .' \
    'Then run: netizen setup --root PATH; netizen start --root PATH' \
    'Existing legacy installations require manual conversion. No files or services were changed.' >&2
exit 2
