#!/bin/sh
set -eu

main() {
    if [ "$#" -ne 0 ]; then
        echo "usage: ./install.sh" >&2
        exit 2
    fi

    command -v curl >/dev/null 2>&1 || {
        echo "Netizen installation failed: curl is required" >&2
        exit 1
    }

    temporary_base=${TMPDIR:-/tmp}
    temporary_directory=$(mktemp -d "$temporary_base/netizen-latest.XXXXXX") || {
        echo "Netizen installation failed: could not create a private temporary directory" >&2
        exit 1
    }
    cleanup() {
        rm -rf -- "$temporary_directory"
    }
    trap cleanup 0
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    exact_installer="$temporary_directory/install.sh"
    latest_url='https://github.com/lijingda/netizen/releases/latest/download/install.sh'
    curl -fL --proto '=https' --tlsv1.2 -o "$exact_installer" "$latest_url" || {
        echo "Netizen installation failed: could not download the latest stable installer" >&2
        exit 1
    }
    /bin/sh -n "$exact_installer" || {
        echo "Netizen installation failed: downloaded installer is incomplete" >&2
        exit 1
    }
    /bin/sh "$exact_installer"
}

# Keep effects behind a fully parsed function so a truncated `curl | sh` does
# not start an installation.
main "$@"
