#!/usr/bin/env python3
"""Upgrade v2 to a validated subscription cache and isolated throughput selection."""
import argparse
import datetime
import grp
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import yaml

from mihomo_health_guard import API, GuardError, load_secret
from migrate_groups import atomic_write, metadata_dict, metadata_from_dict, transform, verify_selectors
from provider_cache_sync import sync_once, SyncError, refresh_main
from speed_network import MARK

LIB = Path('/usr/local/lib/mihomo-health-guard')
STATE = Path('/var/lib/mihomo-speed-guard')
CACHE = Path('/etc/mihomo/provider-cache')
SETTINGS = STATE / 'settings.json'
CONFIG = Path('/etc/mihomo/config.yaml')
UNITS = ('mihomo-speed-probe.service', 'mihomo-speed-guard.service',
         'mihomo-speed-guard.timer', 'mihomo-provider-cache.service',
         'mihomo-provider-cache.timer')
SCRIPTS = ('mihomo_health_guard.py', 'mihomo_speed_guard.py',
           'provider_cache_sync.py', 'speed_network.py', 'upgrade_speed.py', 'migrate_groups.py')
TIMERS = ('mihomo-health-guard.timer', 'mihomo-speed-guard.timer', 'mihomo-provider-cache.timer')
SERVICES = ('mihomo-health-guard.service', 'mihomo-speed-guard.service',
            'mihomo-provider-cache.service', 'mihomo-speed-probe.service')


class UpgradeError(Exception):
    pass


def run(command, timeout=45, check=True, env=None):
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        if check:
            raise UpgradeError('system command did not complete: ' + command[0]) from None
        return False
    if check and completed.returncode:
        raise UpgradeError('system command failed: ' + command[0])
    return completed.returncode == 0


def private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.upgrade-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def replace_provider(text, provider):
    """Only replace Airport_01's block; retain the rest of the hand-written YAML."""
    lines = text.splitlines(keepends=True)
    headings = [i for i, line in enumerate(lines) if re.fullmatch(r'proxy-providers:\s*(?:#.*)?\n?', line)]
    if len(headings) != 1:
        raise UpgradeError('need one ordinary proxy-providers block')
    start = headings[0] + 1
    end = next((i for i in range(start, len(lines)) if lines[i].strip()
                and not lines[i].startswith((' ', '#', '\t'))), len(lines))
    matches = [i for i in range(start, end)
               if re.fullmatch(r"  (?:Airport_01|'Airport_01'|\"Airport_01\"):\s*(?:#.*)?\n?", lines[i])]
    if len(matches) != 1:
        raise UpgradeError('need an ordinary Airport_01 provider block')
    a = matches[0]
    b = next((i for i in range(a + 1, end)
              if lines[i].strip() and not lines[i].lstrip().startswith('#')
              and len(lines[i]) - len(lines[i].lstrip(' ')) <= 2), end)
    rendered = yaml.safe_dump({'Airport_01': provider}, allow_unicode=True, sort_keys=False)
    replacement = ''.join('  ' + row if row.strip() else row for row in rendered.splitlines(keepends=True))
    return ''.join(lines[:a]) + replacement + '\n' + ''.join(lines[b:])


def file_provider(original):
    keys = ('override', 'filter', 'exclude-filter', 'exclude-type', 'health-check')
    result = {'type': 'file', 'path': str(CACHE / 'Airport_01.yaml')}
    result.update({key: original[key] for key in keys if key in original})
    return result


def make_sidecar(provider, secret):
    isolated = dict(provider)
    isolated['health-check'] = {'enable': False}
    return {'mixed-port': 17892, 'allow-lan': False, 'bind-address': '127.0.0.1',
            'external-controller': '127.0.0.1:19094', 'secret': secret,
            'ipv6': False, 'mode': 'rule', 'log-level': 'warning',
            'interface-name': 'eth0', 'routing-mark': MARK,
            'tun': {'enable': False}, 'dns': {'enable': False},
            'profile': {'store-selected': False},
            'proxy-providers': {'Airport_01': isolated},
            'proxy-groups': [{'name': 'Speed-Probe', 'type': 'select',
                              'proxies': ['REJECT'], 'empty-fallback': 'REJECT',
                              'use': ['Airport_01']}], 'rules': ['MATCH,Speed-Probe']}


def destinations():
    return [CONFIG, SETTINGS, CACHE / 'Airport_01.yaml', CACHE / 'Airport_01.yaml.previous',
            STATE / 'sidecar/config.json'] + [LIB / n for n in SCRIPTS] + [
                Path('/etc/systemd/system') / n for n in UNITS]


def snapshot():
    folder = STATE / ('upgrade-v3-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    folder.mkdir(mode=0o700, parents=True)
    files = []
    for i, path in enumerate(destinations()):
        row = {'path': str(path), 'backup': None, 'metadata': None}
        if path.exists():
            backup = folder / ('file-' + str(i))
            shutil.copyfile(path, backup)
            backup.chmod(0o600)
            row.update(backup=backup.name, metadata=metadata_dict(path.stat()))
        files.append(row)
    units = {}
    for name in TIMERS + ('mihomo-speed-probe.service',):
        units[name] = {'enabled': run(['systemctl', 'is-enabled', '--quiet', name], check=False),
                       'active': run(['systemctl', 'is-active', '--quiet', name], check=False)}
    private_json(folder / 'manifest.json', {'version': 3, 'files': files, 'units': units})
    return folder


def rollback(folder):
    folder = Path(folder).resolve(strict=True)
    manifest = json.loads((folder / 'manifest.json').read_text())
    allowed = {str(p) for p in destinations()}
    if (manifest.get('version') != 3 or {r['path'] for r in manifest['files']} != allowed
            or set(manifest['units']) != set(TIMERS + ('mihomo-speed-probe.service',))):
        raise UpgradeError('invalid v3 rollback manifest; no files restored')
    for row in manifest['files']:
        if row['backup'] is not None:
            if not re.fullmatch(r'file-\d+', row['backup']) or not (folder / row['backup']).is_file():
                raise UpgradeError('incomplete v3 rollback snapshot')
            metadata_from_dict(row['metadata'])
    run(['systemctl', 'stop', *TIMERS, *SERVICES], timeout=90, check=False)
    run(['systemctl', 'disable', *TIMERS, 'mihomo-speed-probe.service'], check=False)
    for row in manifest['files']:
        path = Path(row['path'])
        if row['backup'] is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, (folder / row['backup']).read_bytes(), metadata_from_dict(row['metadata']))
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'reload', 'mihomo.service'])
    for unit, status in manifest['units'].items():
        if status['enabled']:
            run(['systemctl', 'enable', unit])
        if status['active']:
            run(['systemctl', 'start', unit])
    print('V3_ROLLBACK_COMPLETE', flush=True)


def wait_sidecar(secret):
    api = API('http://127.0.0.1:19094', secret)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if api.group('Speed-Probe').get('type') == 'Selector':
                return
        except GuardError:
            pass
        time.sleep(1)
    raise UpgradeError('isolated speed process did not become ready')


def wait_file_provider(api):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            row = api.provider('Airport_01')
            if row.get('vehicleType') == 'File' and row.get('proxies'):
                return len(row['proxies'])
        except GuardError:
            pass
        time.sleep(1)
    raise UpgradeError('main mihomo has not loaded the local file provider')


def install(source):
    source = Path(source).resolve(strict=True)
    for name in SCRIPTS + UNITS:
        if not (source / name).is_file():
            raise UpgradeError('upgrade package is missing ' + name)
    text = CONFIG.read_text()
    document = yaml.safe_load(text)
    original = document.get('proxy-providers', {}).get('Airport_01')
    if not isinstance(original, dict) or original.get('type') not in ('http', 'file'):
        raise UpgradeError('Airport_01 must be an existing HTTP/file provider')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    old_settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    source_url = original.get('url') if original.get('type') == 'http' else old_settings.get('source_url')
    if not source_url:
        raise UpgradeError('the original private SublinkPro URL is unavailable')
    backup = snapshot()
    print('V3_PRIVATE_BACKUP ' + str(backup), flush=True)
    try:
        run(['systemctl', 'stop', *TIMERS, *SERVICES], timeout=90, check=False)
        group = grp.getgrnam('mihomo').gr_gid
        CACHE.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(CACHE, 0, group)
        CACHE.chmod(0o750)
        secret = old_settings.get('sidecar_secret') or secrets.token_urlsafe(32)
        settings = dict(old_settings)
        settings.update(version=1, provider='Airport_01', provider_name='Airport_01',
                        groups=['Proxy-Auto', 'Docker-Auto'], source_url=source_url,
                        cache_path=str(CACHE / 'Airport_01.yaml'), cache_uid=0, cache_gid=group,
                        cache_mode=0o640, settings_path=str(SETTINGS), mihomo='/usr/bin/mihomo',
                        interface='eth0', provider_filter=original.get('filter', ''),
                        provider_exclude_filter=original.get('exclude-filter', ''),
                        provider_exclude_type=original.get('exclude-type', ''),
                        provider_override=original.get('override', {}), refresh_main=False,
                        api_url='http://127.0.0.1:9093', mihomo_config=str(CONFIG),
                        sidecar_api='http://127.0.0.1:19094', sidecar_proxy='http://127.0.0.1:17892',
                        sidecar_secret=secret, sidecar_group='Speed-Probe',
                        interval_seconds=1800, daily_budget_bytes=1073741824)
        private_json(SETTINGS, settings)
        for name in SCRIPTS:
            LIB.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copyfile(source / name, LIB / name)
            (LIB / name).chmod(0o644)
        print('FETCHING_VALIDATED_SUBSCRIPTION deadline=120s', flush=True)
        result = sync_once(settings)
        print('CACHE_READY nodes=' + str(result['node_count']), flush=True)
        provider = file_provider(original)
        revised = replace_provider(text, provider)
        revised, groups = transform(revised)
        metadata = CONFIG.stat()
        fd, candidate = tempfile.mkstemp(prefix='.speed-upgrade-', suffix='.yaml', dir=CONFIG.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(revised)
            os.chmod(candidate, stat.S_IMODE(metadata.st_mode))
            os.chown(candidate, metadata.st_uid, metadata.st_gid)
            run(['/usr/bin/mihomo', '-t', '-d', str(CONFIG.parent), '-f', candidate], timeout=60)
        finally:
            Path(candidate).unlink(missing_ok=True)
        sidecar_dir = STATE / 'sidecar'
        sidecar_dir.mkdir(mode=0o700, exist_ok=True)
        private_json(sidecar_dir / 'config.json', make_sidecar(provider, secret))
        sidecar_env = dict(os.environ, SAFE_PATHS=str(CACHE))
        run(['/usr/bin/mihomo', '-t', '-d', str(sidecar_dir), '-f', str(sidecar_dir / 'config.json')], env=sidecar_env)
        atomic_write(CONFIG, revised.encode(), metadata)
        run(['systemctl', 'reload', 'mihomo.service'])
        api = API('http://127.0.0.1:9093', load_secret(CONFIG))
        if not verify_selectors(api, groups):
            raise UpgradeError('managed Selector verification failed')
        count = wait_file_provider(api)
        settings = json.loads(SETTINGS.read_text())
        settings['refresh_main'] = True
        private_json(SETTINGS, settings)
        for name in UNITS:
            shutil.copyfile(source / name, Path('/etc/systemd/system') / name)
            (Path('/etc/systemd/system') / name).chmod(0o644)
        run(['systemctl', 'disable', 'mihomo-health-guard.timer'], check=False)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', '--now', 'mihomo-speed-probe.service'])
        wait_sidecar(secret)
        run(['systemctl', 'enable', '--now', 'mihomo-speed-guard.timer', 'mihomo-provider-cache.timer'])
        print('V3_INSTALLED eligible_nodes=' + str(count), flush=True)
        # Healthy business routing is restored before the longer speed scan.
        healthy = run(['systemctl', 'start', 'mihomo-speed-guard.service'], timeout=650, check=False)
        if not healthy:
            print('INITIAL_CHECK_NEEDS_ATTENTION timer_will_retry', flush=True)
        print('V3_COMPLETE backup=' + str(backup), flush=True)
        return 0
    except BaseException as exc:
        print('V3_INSTALL_FAILED attempting_rollback', flush=True)
        try:
            rollback(backup)
        except BaseException:
            print('V3_ROLLBACK_NEEDS_ATTENTION backup=' + str(backup), flush=True)
        if isinstance(exc, (UpgradeError, GuardError, SyncError)):
            raise UpgradeError(str(exc)) from None
        raise UpgradeError('upgrade did not complete; inspect private backup') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback')
    parser.add_argument('--source', default=str(Path(__file__).resolve().parent))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('use sudo sh install-v3.sh')
    if args.rollback:
        rollback(args.rollback)
        return 0
    return install(args.source)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (UpgradeError, GuardError, SyncError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
