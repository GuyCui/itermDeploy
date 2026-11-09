#!/usr/bin/env python3
"""Keep only the isolated speed process's marked sockets outside the main TUN."""
import argparse
import json
import os
from pathlib import Path
import subprocess

PRIORITY = 8990
MARK = 19784
STATE = Path('/var/lib/mihomo-speed-guard/network-rule.json')


def matches(row):
    mark = row.get('fwmark')
    try:
        value = int(mark, 0) if isinstance(mark, str) else int(mark)
    except (ValueError, TypeError):
        return False
    return (row.get('priority') == PRIORITY and value == MARK
            and str(row.get('table')) in ('main', '254')
            and row.get('fwmask', '0xffffffff') in ('0xffffffff', 4294967295))


def change(action):
    data = subprocess.run(['ip', '-j', '-4', 'rule', 'show'], check=True,
                          capture_output=True, text=True)
    rules = json.loads(data.stdout)
    at_priority = [row for row in rules if row.get('priority') == PRIORITY]
    if action == 'start':
        if at_priority and (len(at_priority) != 1 or not matches(at_priority[0])):
            raise RuntimeError('speed routing priority is already used; no rule changed')
        if not at_priority:
            subprocess.run(['ip', '-4', 'rule', 'add', 'pref', str(PRIORITY),
                            'fwmark', str(MARK), 'lookup', 'main'], check=True)
        STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        STATE.write_text(json.dumps({'priority': PRIORITY, 'mark': MARK}))
        STATE.chmod(0o600)
    elif STATE.exists():
        if len(at_priority) == 1 and matches(at_priority[0]):
            subprocess.run(['ip', '-4', 'rule', 'del', 'pref', str(PRIORITY),
                            'fwmark', str(MARK), 'lookup', 'main'], check=True)
        STATE.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('requires root')
    change(args.action)
