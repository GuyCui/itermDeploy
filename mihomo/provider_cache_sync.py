#!/usr/bin/env python3
"""Fetch a private SublinkPro export, validate a provider file and publish it.

The source URL stays in a private settings file and curl's standard input. Only
node data is retained; unrelated exported DNS, rules and listeners are ignored.
"""

import argparse
import contextlib
import datetime
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_SETTINGS = Path('/var/lib/mihomo-speed-guard/settings.json')
MAX_EXPORT_BYTES = 8 * 1024 * 1024
DOWNLOAD_SECONDS = 120
VALIDATION_SECONDS = 45


class SyncError(Exception):
    """A sanitized operational error safe to show in logs."""


def log(event, **fields):
    row = {'time': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
           'event': event}
    row.update(fields)
    print(json.dumps(row, ensure_ascii=False), flush=True)


def _source_parts(settings):
    source = settings.get('source_url')
    if not isinstance(source, str) or not source or any(ord(c) < 32 for c in source):
        raise SyncError('invalid source URL in private settings')
    try:
        parts = urllib.parse.urlsplit(source)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.fragment:
            raise ValueError()
        parts.port
    except ValueError:
        raise SyncError('invalid source URL in private settings') from None
    return source, parts


def _curl_request(settings):
    source, parts = _source_parts(settings)
    # curl config quoted strings escape backslashes and double quotes. Rejecting
    # control characters above prevents config-line injection by the source URL.
    escaped = source.replace('\\', '\\\\').replace('"', '\\"')
    config = ('url = "' + escaped + '"\n').encode('utf-8')
    command = ['curl', '--config', '-', '--silent', '--show-error', '--fail',
               '--noproxy', '*', '--connect-timeout', '5',
               '--max-time', str(DOWNLOAD_SECONDS), '--proto', '=http,https',
               '--max-filesize', str(MAX_EXPORT_BYTES)]
    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = parts.hostname.lower() == 'localhost'
    interface = settings.get('interface', 'eth0')
    if not isinstance(interface, str) or not interface or any(c.isspace() for c in interface):
        raise SyncError('invalid download interface in private settings')
    # Binding eth0 for 127.0.0.1 prevents reaching a loopback-only SublinkPro.
    # Non-loopback requests are pinned to the specified physical interface.
    if not loopback:
        command += ['--interface', interface]
    return command, config


def download_export(settings):
    """Read at most 8 MiB with curl enforcing connection and total deadlines."""
    command, config = _curl_request(settings)
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        process.stdin.write(config)
        process.stdin.close()
        data = bytearray()
        deadline = time.monotonic() + DOWNLOAD_SECONDS + 5
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SyncError('subscription download exceeded the time limit')
                events = selector.select(min(remaining, 1))
                if not events:
                    continue
                chunk = os.read(process.stdout.fileno(),
                                min(65536, MAX_EXPORT_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_EXPORT_BYTES:
                    raise SyncError('subscription export exceeds the 8 MiB limit')
        result = process.wait(timeout=3)
        if result != 0:
            if result == 28:
                raise SyncError('subscription download exceeded the time limit')
            if result == 63:
                raise SyncError('subscription export exceeds the 8 MiB limit')
            raise SyncError('subscription download failed (curl exit ' + str(result) + ')')
        if not data:
            raise SyncError('subscription download returned an empty response')
        return bytes(data)
    except SyncError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never print curl stderr or exceptions: either can contain the token.
        raise SyncError('subscription downloader could not complete') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


def parse_export(data):
    if yaml is None:
        raise SyncError('python3-yaml is required')
    if not isinstance(data, bytes) or not data or len(data) > MAX_EXPORT_BYTES:
        raise SyncError('subscription response is empty or exceeds the size limit')
    try:
        document = yaml.safe_load(data.decode('utf-8-sig'))
    except (UnicodeError, yaml.YAMLError, ValueError, RecursionError):
        raise SyncError('subscription response is not safe UTF-8 YAML') from None
    nodes = document.get('proxies') if isinstance(document, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise SyncError('subscription contains no proxy nodes; keeping the previous cache')
    names = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise SyncError('subscription contains a proxy that is not a mapping')
        name, kind = node.get('name'), node.get('type')
        if not isinstance(name, str) or not name.strip() or not isinstance(kind, str) or not kind:
            raise SyncError('subscription contains a proxy without a valid name/type')
        if name in names:
            raise SyncError('subscription contains duplicate proxy names')
        names.add(name)
    try:
        payload = yaml.safe_dump({'proxies': nodes}, allow_unicode=True,
                                 sort_keys=False).encode('utf-8')
    except (yaml.YAMLError, ValueError, RecursionError):
        raise SyncError('subscription nodes cannot be safely serialized') from None
    if len(payload) > MAX_EXPORT_BYTES:
        raise SyncError('normalized provider cache exceeds the 8 MiB limit')
    return payload, len(nodes)


def _owner(settings, cache):
    previous = cache.stat() if cache.exists() else None
    uid = settings.get('cache_uid', previous.st_uid if previous else os.geteuid())
    gid = settings.get('cache_gid', previous.st_gid if previous else None)
    if gid is None:
        try:
            gid = grp.getgrnam('mihomo').gr_gid
        except KeyError:
            if os.geteuid() == 0:
                raise SyncError('mihomo group is missing; configure cache_gid') from None
            gid = os.getegid()
    mode = settings.get('cache_mode', 0o640)
    if (type(uid) is not int or uid < 0 or type(gid) is not int or gid < 0
            or type(mode) is not int or mode != 0o640):
        raise SyncError('invalid cache ownership or mode; cache files require mode 0640')
    return uid, gid, mode


def _prepare_file(path, data, uid, gid, mode):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(handle.fileno(), uid, gid)
            elif (uid, gid) != (os.geteuid(), os.getegid()):
                raise SyncError('cache ownership requires root privileges')
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_file(path, data, uid, gid, mode):
    temporary = _prepare_file(path, data, uid, gid, mode)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validation_config(settings, candidate):
    provider_name = settings.get('provider_name', 'Airport_01')
    if not isinstance(provider_name, str) or not provider_name:
        raise SyncError('invalid provider name in private settings')
    provider = {'type': 'file', 'path': str(candidate)}
    override = settings.get('provider_override', {})
    if not isinstance(override, dict):
        raise SyncError('invalid provider override in private settings')
    provider['override'] = override
    for key in ('filter', 'exclude-filter', 'exclude-type'):
        value = settings.get('provider_' + key.replace('-', '_'), '')
        if not isinstance(value, str):
            raise SyncError('invalid provider filtering in private settings')
        if value:
            provider[key] = value
    return {'mode': 'rule', 'log-level': 'error', 'ipv6': False,
            'interface-name': settings.get('interface', 'eth0'),
            'tun': {'enable': False}, 'dns': {'enable': False},
            'proxy-providers': {provider_name: provider},
            'proxy-groups': [{'name': 'CACHE-VALIDATION', 'type': 'select',
                              'proxies': ['REJECT'], 'empty-fallback': 'REJECT',
                              'use': [provider_name]}],
            'rules': ['MATCH,REJECT']}


def validate_candidate(settings, candidate):
    config = _validation_config(settings, candidate)
    try:
        data = json.dumps(config, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError):
        raise SyncError('provider validation settings cannot be serialized') from None
    validation_path = candidate.parent / '.provider-validation.json'
    temporary = _prepare_file(validation_path, data, os.geteuid(), os.getegid(), 0o600)
    try:
        binary = settings.get('mihomo', '/usr/bin/mihomo')
        if not isinstance(binary, str) or not Path(binary).is_absolute():
            raise SyncError('mihomo validation binary must be an absolute path')
        # No geosite/GEOIP rules, DNS service, TUN, or subscription network fetch.
        result = subprocess.run([binary, '-t', '-d', str(candidate.parent),
                                 '-f', str(temporary)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=VALIDATION_SECONDS)
        if result.returncode:
            raise SyncError('mihomo rejected the downloaded provider; keeping the previous cache')
    except SyncError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise SyncError('mihomo provider validation could not complete') from None
    finally:
        temporary.unlink(missing_ok=True)


def load_settings(path):
    path = Path(path)
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SyncError('private settings must be a regular file with mode 0600')
        result = json.loads(path.read_text(encoding='utf-8'))
    except SyncError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise SyncError('cannot read private cache settings') from None
    if not isinstance(result, dict):
        raise SyncError('private cache settings must contain a JSON object')
    result['settings_path'] = str(path)
    return result


def _prepare_generation(settings, generation, count, updated_at):
    path = Path(settings.get('settings_path', DEFAULT_SETTINGS))
    current = load_settings(path)
    if current.get('source_url') != settings.get('source_url') or current.get('cache_path') != settings.get('cache_path'):
        raise SyncError('private settings changed during download; retrying is required')
    current.pop('settings_path', None)
    current.update(generation=generation, cache_node_count=count, cache_updated_at=updated_at)
    metadata = path.stat()
    pending = _prepare_file(path, (json.dumps(current, ensure_ascii=False, indent=2) + '\n').encode(),
                            metadata.st_uid, metadata.st_gid, 0o600)
    return path, pending


def refresh_main(settings):
    """Ask main mihomo to read the already-published local file; never roll back it."""
    try:
        from mihomo_health_guard import API, GuardError, load_secret
        api = API(settings.get('api_url', 'http://127.0.0.1:9093'),
                  load_secret(settings.get('mihomo_config', '/etc/mihomo/config.yaml')))
        provider = urllib.parse.quote(settings.get('provider_name', 'Airport_01'), safe='')
        api.request('PUT', '/providers/proxies/' + provider)
        return True
    except Exception:
        # Exceptions from HTTP libraries or secret loading must not leak URLs.
        log('provider_refresh_pending', reason='main API refresh failed; valid cache retained')
        return False


@contextlib.contextmanager
def _sync_lock(settings_path):
    lock_path = Path(str(settings_path) + '.sync.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError('another provider-cache synchronization is already running') from None
        yield
    finally:
        os.close(fd)


def sync_once(settings, dry_run=False):
    """Publish one validated export and atomically update the private generation.

    settings.json must already exist. The optional settings_path supports install
    staging. A source/validation failure never replaces the previous cache.
    """
    if not isinstance(settings, dict):
        raise SyncError('cache settings must be an object')
    raw_cache = settings.get('cache_path')
    if not isinstance(raw_cache, str) or not Path(raw_cache).is_absolute():
        raise SyncError('cache_path must be absolute')
    cache = Path(raw_cache)
    if cache.is_symlink():
        raise SyncError('cache_path must not be a symbolic link')
    settings_path = Path(settings.get('settings_path', DEFAULT_SETTINGS))
    with _sync_lock(settings_path):
        payload, count = parse_export(download_export(settings))
        uid, gid, mode = _owner(settings, cache)
        if not cache.parent.exists():
            cache.parent.mkdir(mode=0o750, parents=True)
        os.chmod(cache.parent, 0o750)
        if os.geteuid() == 0:
            os.chown(cache.parent, uid, gid)
        candidate = _prepare_file(cache, payload, uid, gid, mode)
        pending_settings = None
        try:
            validate_candidate(settings, candidate)
            old = cache.read_bytes() if cache.exists() else None
            generation = hashlib.sha256(payload).hexdigest()
            updated_at = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
            changed = payload != old
            result = {'changed': changed, 'node_count': count, 'bytes': len(payload),
                      'generation': generation, 'updated_at': updated_at,
                      'cache_path': str(cache), 'dry_run': bool(dry_run),
                      'api_refreshed': False}
            if dry_run:
                return result
            _, pending_settings = _prepare_generation(settings, generation, count, updated_at)
            if changed and old is not None:
                # The existing file is the previous successful publication. If
                # it was externally damaged, keep any older backup instead.
                try:
                    parse_export(old)
                except SyncError:
                    pass
                else:
                    _atomic_file(Path(str(cache) + '.previous'), old, uid, gid, mode)
            published = False
            try:
                if changed:
                    os.replace(candidate, cache)
                    published = True
                else:
                    os.chmod(cache, mode)
                    if os.geteuid() == 0:
                        os.chown(cache, uid, gid)
                os.replace(pending_settings, settings_path)
            except (OSError, SyncError):
                if published:
                    if old is None:
                        cache.unlink(missing_ok=True)
                    else:
                        _atomic_file(cache, old, uid, gid, mode)
                raise SyncError('cache publication failed; previous cache restored') from None
            settings.update(generation=generation, cache_node_count=count, cache_updated_at=updated_at)
            if settings.get('refresh_main') is True:
                result['api_refreshed'] = refresh_main(settings)
            return result
        except SyncError:
            raise
        except (OSError, ValueError, TypeError):
            raise SyncError('provider-cache operation failed; check local paths and permissions') from None
        finally:
            candidate.unlink(missing_ok=True)
            if pending_settings is not None:
                pending_settings.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', default=str(DEFAULT_SETTINGS))
    parser.add_argument('--dry-run', action='store_true',
                        help='download and validate without publishing or updating generation')
    args = parser.parse_args(argv)
    try:
        result = sync_once(load_settings(args.settings), args.dry_run)
        log('provider_cache_checked' if args.dry_run else 'provider_cache_synced', **result)
        return 0
    except (SyncError, OSError):
        # Only our own sanitized errors are safe for stdout/journal.
        error = sys.exc_info()[1]
        log('provider_cache_failed', reason=str(error) if isinstance(error, SyncError)
            else 'private settings or cache directory access failed')
        return 1


if __name__ == '__main__':
    sys.exit(main())
