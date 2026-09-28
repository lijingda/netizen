#!/bin/sh
set -eu

main() {
validate_install_arguments() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --root)
                [ "$#" -ge 2 ] && [ -n "$2" ] || return 1
                shift 2
                ;;
            --root=*)
                [ -n "${1#--root=}" ] || return 1
                shift
                ;;
            --admin-port)
                [ "$#" -ge 2 ] || return 1
                candidate_port=$2
                shift 2
                case "$candidate_port" in ''|*[!0-9]*) return 1 ;; esac
                [ "${#candidate_port}" -le 5 ] && [ "$candidate_port" -ge 1 ] && [ "$candidate_port" -le 65535 ] || return 1
                ;;
            --admin-port=*)
                candidate_port=${1#--admin-port=}
                shift
                case "$candidate_port" in ''|*[!0-9]*) return 1 ;; esac
                [ "${#candidate_port}" -le 5 ] && [ "$candidate_port" -ge 1 ] && [ "$candidate_port" -le 65535 ] || return 1
                ;;
            *) return 1 ;;
        esac
    done
}

validate_install_arguments "$@" || {
    echo "usage: install.sh [--root PATH] [--admin-port PORT]" >&2
    exit 2
}

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
    /bin/sh "$exact_installer" "$@"
}

# Keep effects behind a fully parsed function so a truncated `curl | sh` does
# not start an installation.
main "$@"
