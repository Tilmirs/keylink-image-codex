#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

supported_python() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if [ -n "${KEYLINK_PYTHON:-}" ]; then
    if ! supported_python "$KEYLINK_PYTHON"; then
        printf '%s\n' 'KEYLINK_PYTHON must point to a working Python 3.11 or newer interpreter.' >&2
        exit 1
    fi
    exec "$KEYLINK_PYTHON" -X utf8 "$script_dir/keylink_image.py" "$@"
fi

for candidate in "$script_dir/../.venv/bin/python3" python3 python3.14 python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if supported_python "$candidate"; then
        exec "$candidate" -X utf8 "$script_dir/keylink_image.py" "$@"
    fi
done

printf '%s\n' 'Python 3.11 or newer is required. Install it or set KEYLINK_PYTHON to its executable path.' >&2
exit 1
