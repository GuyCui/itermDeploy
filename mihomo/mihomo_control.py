#!/usr/bin/env python3
"""One entry point to inspect the installed guard/cache and request a sync.

check is read-only apart from downloading the subscription into memory. No
private URLs, names, credentials, raw API errors, or command output are printed.
sync delegates to the existing installed systemd unit, then checks the result.
"""
import argparse
from collections import Counter
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys


FILTER_WORDS = ('拒绝', '直连', '群', '邀请', '返利', '循环', '官网', '客服',
                '网站', '获取', '订阅', '流量', '到期', '机场', '过期', '已用',
                '联系', '邮箱', '工单', '通知', '倒卖', '地址', '频道', '说明',
                '教程', '更新', 'TOTAL', 'EXPIRE', 'Panel', 'Channel', 'traffic')


def name_filter_summary(rows, provider):
    """Explain the installed simple keyword filter without exposing node names.

    Deliberately recognize only the known literal negative-lookahead form. The
    core uses a different regex engine, so arbitrary patterns are not simulated.
    This does not assess whether a filtered entry is usable or a notice.
    """
    pattern = provider.get('filter', '')
    prefix, suffix = '^(?!.*(', ')).*'
    if not isinstance(pattern, str) or not pattern.startswith(prefix) or not pattern.endswith(suffix):
        return {'supported': False, 'reason': 'filter_requires_core_regex_evaluation'}
    words = pattern[len(prefix):-len(suffix)].split('|')
    if not words or any(word not in FILTER_WORDS for word in words):
        return {'supported': False, 'reason': 'filter_requires_core_regex_evaluation'}
    if any('\n' in row.get('name', '') for row in rows):
        return {'supported': False, 'reason': 'multiline_name_requires_core_regex_evaluation'}
    excluded = []
    for index, row in enumerate(rows, 1):
        name = row.get('name', '')
        matched = [word for word in words if word in name]
        if matched:
            excluded.append({'entry': index, 'matched_words': matched})
    return {'supported': True, 'scope': 'name_keyword_filter_only',
            'excluded': len(excluded), 'remaining': len(rows) - len(excluded),
            'other_exclusions_present': bool(provider.get('exclude-filter') or provider.get('exclude-type')),
            'entries': excluded}


def read_yaml(path):
    import yaml
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            return None
        value = yaml.safe_load(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def provider_summary(row):
    if not isinstance(row, dict):
        return {'ok': False}
    proxies = row.get('proxies')
    if not isinstance(proxies, list):
        return {'ok': False}
    return {'ok': True, 'nodes': len(proxies),
            'vehicle': row.get('vehicleType'),
            'updated_at': row.get('updatedAt'),
            'types': dict(Counter(str(p.get('type', 'Unknown'))
                                  for p in proxies if isinstance(p, dict)))}


def classify(report):
    findings = []
    main = report.get('main_provider', {})
    source = report.get('source', {})
    cache = report.get('cache', {})
    counts = report.get('configuration', {})
    if main.get('ok') and main.get('nodes') == 0:
        findings.append('MAIN_PROVIDER_EMPTY')
    if source.get('ok') and cache.get('ok'):
        if source.get('normalized_sha256') != cache.get('normalized_sha256'):
            findings.append('SOURCE_AND_CACHE_DIFFER')
        else:
            findings.append('SOURCE_AND_CACHE_IDENTICAL')
    if cache.get('ok') and main.get('ok'):
        if main.get('nodes', 0) < cache.get('nodes', 0):
            filtered = report.get('name_filter', {})
            if (filtered.get('supported') and not filtered.get('other_exclusions_present')
                    and filtered.get('remaining') == main['nodes']):
                findings.append('CACHE_MAIN_COUNT_GAP_MATCHES_NAME_FILTER')
            else:
                findings.append('CHECK_PROVIDER_FILTERING_OR_RUNTIME_RELOAD')
        elif main.get('nodes') > cache.get('nodes', 0):
            findings.append('MAIN_PROVIDER_MAY_BE_OLDER_THAN_CACHE')
        else:
            findings.append('CACHE_AND_MAIN_COUNTS_MATCH')
    if (source.get('ok') and cache.get('ok') and main.get('ok')
            and source['nodes'] == cache['nodes'] == main['nodes']):
        findings.append('SOURCE_CACHE_MAIN_COUNTS_MATCH')
    if not report.get('settings', {}).get('ok'):
        findings.append('INSTALLED_CACHE_SETTINGS_UNAVAILABLE')
    if not main.get('ok'):
        findings.append('MAIN_PROVIDER_UNAVAILABLE')
    if report.get('configuration', {}).get('override_matches_settings') is False:
        findings.append('MAIN_OVERRIDE_DIFFERS_FROM_GUARD_SETTINGS')
    if report.get('configuration', {}).get('cache_path_matches_settings') is False:
        findings.append('MAIN_CACHE_PATH_DIFFERS_FROM_GUARD_SETTINGS')
    if counts.get('filter_matches_settings') is False:
        findings.append('MAIN_FILTER_DIFFERS_FROM_GUARD_SETTINGS')
    if report.get('main_sidecar_nodes_match') is False:
        findings.append('MAIN_AND_SIDECAR_NODE_SETS_DIFFER')
    return findings


def inspect(config_path, settings_path, fetch_source=True):
    # Load installed modules, not a possibly older copy shipped in this bundle.
    installed = Path('/usr/local/lib/mihomo-health-guard')
    if installed.is_dir():
        sys.path.insert(0, str(installed))
    from mihomo_health_guard import API
    from provider_cache_sync import load_settings, parse_export, download_export

    report = {'time': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
              'read_only': True, 'source_download_requested': fetch_source}
    config = read_yaml(config_path)
    try:
        settings = load_settings(settings_path)
        report['settings'] = {'ok': True,
                              'recorded_cache_nodes': settings.get('cache_node_count'),
                              'cache_updated_at': settings.get('cache_updated_at')}
    except Exception:
        settings = {}
        report['settings'] = {'ok': False}
    name = settings.get('provider_name', 'Airport_01')
    main_row = None
    provider = {}
    if config is not None:
        provider = config.get('proxy-providers', {}).get(name, {})
        groups = config.get('proxy-groups', [])
        config_cache_path = provider.get('path')
        if config_cache_path:
            config_cache_path = Path(config_cache_path)
            if not config_cache_path.is_absolute():
                config_cache_path = Path(config_path).parent / config_cache_path
        report['configuration'] = {
            'yaml_read': True, 'groups': len(groups),
            'groups_plus_direct_reject': len(groups) + 2,
            'provider_type': provider.get('type'),
            'cache_path_matches_settings': (str(config_cache_path) == settings.get('cache_path'))
                                          if settings else None,
            'filter_present': bool(provider.get('filter')),
            'exclude_filter_present': bool(provider.get('exclude-filter')),
            'exclude_type_present': bool(provider.get('exclude-type')),
            'override_matches_settings': (provider.get('override', {}) == settings.get('provider_override', {}))
                                         if settings else None,
            'filter_matches_settings': (provider.get('filter', '') == settings.get('provider_filter', ''))
                                       if settings else None,
        }
        try:
            api = API(settings.get('api_url', 'http://127.0.0.1:9093'),
                      str(config.get('secret') or ''))
            main_row = api.provider(name)
            report['main_provider'] = provider_summary(main_row)
            report['groups'] = {}
            for group_name in ('Proxy', 'Proxy-Auto', 'Docker-Auto'):
                try:
                    row = api.group(group_name)
                    report['groups'][group_name] = {
                        'type': row.get('type'),
                        'items_including_builtins': len(row.get('all', [])),
                        'selected_reject': row.get('now') in {'REJECT', 'REJECT-DROP'},
                    }
                except Exception:
                    report['groups'][group_name] = {'ok': False}
        except Exception:
            report['main_provider'] = {'ok': False}
    else:
        report['configuration'] = {'yaml_read': False}
        report['main_provider'] = {'ok': False}

    cache_path = provider.get('path') or settings.get('cache_path')
    if cache_path and config is not None:
        cache_path = Path(cache_path)
        if not cache_path.is_absolute():
            cache_path = Path(config_path).parent / cache_path
        try:
            normalized, count = parse_export(cache_path.read_bytes())
            report['cache'] = {
                'ok': True, 'nodes': count,
                'normalized_sha256': hashlib.sha256(normalized).hexdigest(),
                'mtime': datetime.datetime.fromtimestamp(cache_path.stat().st_mtime).astimezone().isoformat(timespec='seconds')}
            import yaml
            report['name_filter'] = name_filter_summary(yaml.safe_load(normalized)['proxies'], provider)
        except Exception:
            report['cache'] = {'ok': False}
    else:
        report['cache'] = {'ok': False}

    if settings and fetch_source:
        try:
            normalized, count = parse_export(download_export(settings))
            report['source'] = {'ok': True, 'nodes': count,
                                'normalized_sha256': hashlib.sha256(normalized).hexdigest()}
        except Exception:
            report['source'] = {'ok': False, 'reason': 'download_or_export_validation_failed'}
    else:
        report['source'] = {'ok': False, 'reason': 'not_requested_or_no_settings'}

    if settings.get('sidecar_secret'):
        try:
            side = API(settings.get('sidecar_api', 'http://127.0.0.1:19094'),
                       settings['sidecar_secret'])
            row = side.provider(name)
            report['sidecar_provider'] = provider_summary(row)
            if isinstance(main_row, dict):
                main_names = {(p.get('name'), p.get('type')) for p in main_row.get('proxies', []) if isinstance(p, dict)}
                side_names = {(p.get('name'), p.get('type')) for p in row.get('proxies', []) if isinstance(p, dict)}
                report['main_sidecar_nodes_match'] = main_names == side_names
                report['main_only_nodes'] = len(main_names - side_names)
                report['sidecar_only_nodes'] = len(side_names - main_names)
        except Exception:
            report['sidecar_provider'] = {'ok': False}
    else:
        report['sidecar_provider'] = {'ok': False, 'reason': 'no_private_sidecar_settings'}

    units = {}
    for unit in ('mihomo.service', 'mihomo-provider-cache.timer',
                 'mihomo-speed-guard.timer', 'mihomo-speed-probe.service'):
        try:
            result = subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            units[unit] = result.returncode == 0
        except Exception:
            units[unit] = None
    report['units_active'] = units
    report['findings'] = classify(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('check', 'sync'), default='check')
    parser.add_argument('--config', default='/etc/mihomo/config.yaml')
    parser.add_argument('--settings', default='/var/lib/mihomo-speed-guard/settings.json')
    parser.add_argument('--no-source', action='store_true', help='skip the up-to-120s export fetch')
    args = parser.parse_args()
    sync_ok = None
    if args.command == 'sync':
        try:
            completed = subprocess.run(['systemctl', 'start', 'mihomo-provider-cache.service'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=190)
            sync_ok = completed.returncode == 0
        except Exception:
            sync_ok = False
    try:
        report = inspect(args.config, args.settings, fetch_source=not args.no_source)
        if args.command == 'sync':
            report['read_only'] = False
            report['existing_cache_service_requested'] = True
            report['sync_service_succeeded'] = sync_ok
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if sync_ok is False else 0
    except Exception:
        # Do not print exception strings: third-party parsing may expose secrets.
        print(json.dumps({'check_failed': True,
                          'reason': 'installed_guard_or_python_yaml_unavailable'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
