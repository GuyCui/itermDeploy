#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo '请使用 sudo sh install-v3.sh' >&2
    exit 1
fi
if ! /usr/bin/python3 -c 'import yaml' >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3-yaml
fi
speed_upgrade_source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 "$speed_upgrade_source/upgrade_speed.py" --source "$speed_upgrade_source" "$@"
