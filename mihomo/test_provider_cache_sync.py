#!/usr/bin/env python3
"""Offline tests: mock all downloads, mihomo processes and controller calls."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock
import provider_cache_sync as sync

EXPORT = b'''secret: unrelated-export-secret
proxies:
  - {name: first, type: http, server: 192.0.2.1, port: 443, password: private-password}
  - {name: second, type: http, server: 192.0.2.1, port: 443, password: private-password}
proxy-groups: [{name: ignored, type: select, proxies: [first]}]
rules: [MATCH,DIRECT]
'''
NEW_EXPORT = EXPORT.replace(b'first', b'new-first')

class ParseTests(unittest.TestCase):
    def test_only_proxies_retained_and_duplicate_connections_preserved(self):
        payload, count = sync.parse_export(EXPORT)
        data = sync.yaml.safe_load(payload)
        self.assertEqual(set(data), {'proxies'})
        self.assertEqual(count, 2)
        self.assertEqual(data['proxies'][0]['server'], data['proxies'][1]['server'])
        self.assertNotIn(b'unrelated-export-secret', payload)

    def test_empty_bad_shape_identity_and_duplicate_names_rejected(self):
        cases = [b'', b'proxies: []', b'{}', b'proxies: invalid', b'[]',
                 b'proxies: [not-a-mapping]', b'proxies: [{name: x}]',
                 b'proxies: [{name: "", type: http}]',
                 b'proxies: [{name: x, type: http}, {name: x, type: ss}]']
        for value in cases:
            with self.subTest(value=value), self.assertRaises(sync.SyncError):
                sync.parse_export(value)

    def test_unsafe_yaml_invalid_encoding_and_size_limit_are_rejected(self):
        for value in (b'!!python/object/apply:os.system [echo-private-token]', b'\xff\xfe'):
            with self.subTest(value=value), self.assertRaises(sync.SyncError) as raised:
                sync.parse_export(value)
            self.assertNotIn('private-token', str(raised.exception))
        with mock.patch.object(sync, 'MAX_EXPORT_BYTES', 10), self.assertRaises(sync.SyncError):
            sync.parse_export(EXPORT)

    def test_missing_dependency_explained(self):
        with mock.patch.object(sync, 'yaml', None), self.assertRaisesRegex(sync.SyncError, 'python3-yaml'):
            sync.parse_export(EXPORT)

class RequestTests(unittest.TestCase):
    def test_token_only_in_stdin_and_loopback_not_bound_to_eth0(self):
        source = 'http://127.0.0.1:8000/c/?token=private-token&client=clash'
        command, config = sync._curl_request({'source_url': source})
        self.assertNotIn('private-token', ' '.join(command))
        self.assertIn(source.encode(), config)
        self.assertNotIn('--interface', command)
        self.assertNotIn('--location', command)
        self.assertEqual(command[command.index('--connect-timeout') + 1], '5')
        self.assertEqual(command[command.index('--max-time') + 1], '120')

    def test_external_source_bound_and_curl_config_escaped(self):
        command, config = sync._curl_request({'source_url': 'https://example.test/?token=x"\\y'})
        self.assertEqual(command[command.index('--interface') + 1], 'eth0')
        self.assertIn(b'x\\"\\\\y', config)
        self.assertEqual(config.count(b'\n'), 1)

    def test_control_injection_invalid_scheme_and_port_rejected(self):
        for source in ('http://localhost/?token=private-token\noutput=/etc/passwd',
                       'file:///private-token', 'http://a:bad/?token=private-token'):
            with self.subTest(source=source), self.assertRaises(sync.SyncError) as raised:
                sync._curl_request({'source_url': source})
            self.assertNotIn('private-token', str(raised.exception))

    def test_download_exception_text_cannot_leak_token(self):
        with mock.patch.object(sync.subprocess, 'Popen', side_effect=OSError('private-token')):
            with self.assertRaises(sync.SyncError) as raised:
                sync.download_export({'source_url': 'http://localhost/?token=private-token'})
        self.assertNotIn('private-token', str(raised.exception))

class StreamTests(unittest.TestCase):
    class FakeCurl:
        def __init__(self, payload, result=0):
            self.stdin = io.BytesIO()
            reader, writer = os.pipe()
            self.stdout = os.fdopen(reader, 'rb', buffering=0)
            self.result = result
            self.killed = False
            def write():
                try:
                    offset = 0
                    while offset < len(payload):
                        offset += os.write(writer, payload[offset:])
                except BrokenPipeError:
                    pass
                finally:
                    os.close(writer)
            self.thread = threading.Thread(target=write)
            self.thread.start()
        def poll(self): return self.result
        def wait(self, timeout=None): return self.result
        def kill(self): self.killed = True

    def test_stream_download_returns_bytes_and_closes_handles(self):
        process = self.FakeCurl(EXPORT)
        with mock.patch.object(sync.subprocess, 'Popen', return_value=process):
            result = sync.download_export({'source_url': 'http://127.0.0.1/?token=private-token'})
        process.thread.join()
        self.assertEqual(result, EXPORT)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_stream_limit_applies_when_content_length_is_missing(self):
        process = self.FakeCurl(b'x' * 17)
        with mock.patch.object(sync, 'MAX_EXPORT_BYTES', 16), mock.patch.object(sync.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(sync.SyncError, '8 MiB'):
                sync.download_export({'source_url': 'http://127.0.0.1/?token=private-token'})
        process.thread.join()
        self.assertTrue(process.stdout.closed)

    def test_curl_failure_codes_have_sanitized_messages(self):
        for code, pattern in ((28, 'time limit'), (63, '8 MiB'), (22, 'curl exit 22')):
            process = self.FakeCurl(b'private-token', result=code)
            with self.subTest(code=code), mock.patch.object(sync.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(sync.SyncError, pattern) as raised:
                    sync.download_export({'source_url': 'http://127.0.0.1/?token=private-token'})
            process.thread.join()
            self.assertNotIn('private-token', str(raised.exception))

    def test_python_deadline_stops_download_even_if_curl_stalls(self):
        process = self.FakeCurl(b'')
        with mock.patch.object(sync.subprocess, 'Popen', return_value=process), mock.patch.object(sync.time, 'monotonic', side_effect=[0, 1000]):
            with self.assertRaisesRegex(sync.SyncError, 'time limit'):
                sync.download_export({'source_url': 'http://127.0.0.1/?token=private-token'})
        process.thread.join()

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'settings.json'
        self.cache = self.root / 'cache' / 'Airport_01.yaml'
        self.settings = {'settings_path': str(self.path),
                         'source_url': 'http://127.0.0.1:8000/c/?token=private-token',
                         'cache_path': str(self.cache), 'mihomo': '/usr/bin/mihomo',
                         'interface': 'eth0', 'provider_name': 'Airport_01',
                         'provider_override': {'additional-prefix': 'Main:', 'udp': True},
                         'provider_filter': 'good', 'cache_uid': os.geteuid(),
                         'cache_gid': os.getegid(), 'cache_mode': 0o640,
                         'generation': 'old', 'other': {'preserve': True}}
        self.write_settings()
        self.download = mock.patch.object(sync, 'download_export', return_value=EXPORT).start()
        self.validation = mock.patch.object(sync, 'validate_candidate').start()
        self.addCleanup(mock.patch.stopall)

    def write_settings(self):
        self.path.write_text(json.dumps({k:v for k,v in self.settings.items() if k != 'settings_path'}))
        self.path.chmod(0o600)

    def seed(self):
        self.cache.parent.mkdir(mode=0o750)
        self.cache.write_bytes(sync.parse_export(EXPORT)[0])
        self.cache.chmod(0o640)

    def test_first_publish_updates_generation_and_permissions(self):
        result = sync.sync_once(self.settings)
        payload, _ = sync.parse_export(EXPORT)
        self.assertTrue(result['changed'])
        self.assertEqual(result['node_count'], 2)
        self.assertEqual(result['generation'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.cache.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.cache.parent.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['generation'], result['generation'])
        self.assertEqual(stored['other'], {'preserve': True})
        self.assertEqual(stored['source_url'], self.settings['source_url'])
        self.validation.assert_called_once()
        self.assertFalse(result['api_refreshed'])

    def test_new_export_preserves_previous_valid_cache(self):
        self.seed()
        old = self.cache.read_bytes()
        self.download.return_value = NEW_EXPORT
        self.assertTrue(sync.sync_once(self.settings)['changed'])
        backup = Path(str(self.cache) + '.previous')
        self.assertEqual(backup.read_bytes(), old)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)

    def test_identical_export_keeps_inode_and_has_stable_generation(self):
        self.seed()
        inode = self.cache.stat().st_ino
        result = sync.sync_once(self.settings)
        self.assertFalse(result['changed'])
        self.assertEqual(self.cache.stat().st_ino, inode)
        self.assertEqual(result['generation'], hashlib.sha256(self.cache.read_bytes()).hexdigest())

    def test_download_empty_or_validation_failure_keeps_cache_and_generation(self):
        self.seed()
        before = self.cache.read_bytes(), self.path.read_bytes()
        for failure in ('download', 'empty', 'validation'):
            self.download.side_effect = None
            self.download.return_value = NEW_EXPORT
            self.validation.side_effect = None
            if failure == 'download': self.download.side_effect = sync.SyncError('download failed')
            if failure == 'empty': self.download.return_value = b'proxies: []'
            if failure == 'validation': self.validation.side_effect = sync.SyncError('invalid provider')
            with self.subTest(failure=failure), self.assertRaises(sync.SyncError):
                sync.sync_once(self.settings)
            self.assertEqual(before, (self.cache.read_bytes(), self.path.read_bytes()))
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_dry_run_validates_without_publication_or_refresh(self):
        self.seed()
        self.settings['refresh_main'] = True
        self.write_settings()
        self.download.return_value = NEW_EXPORT
        before = self.cache.read_bytes(), self.path.read_bytes()
        with mock.patch.object(sync, 'refresh_main') as refresh:
            result = sync.sync_once(self.settings, dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertTrue(result['changed'])
        self.assertEqual(before, (self.cache.read_bytes(), self.path.read_bytes()))
        refresh.assert_not_called()

    def test_settings_commit_failure_restores_cache_even_on_first_install(self):
        for existing in (False, True):
            if existing: self.seed()
            before = self.cache.read_bytes() if existing else None
            settings_before = self.path.read_bytes()
            original_replace = os.replace
            def replace(source, destination):
                if Path(destination) == self.path: raise OSError('private-token')
                return original_replace(source, destination)
            self.download.return_value = NEW_EXPORT
            with self.subTest(existing=existing), mock.patch.object(sync.os, 'replace', side_effect=replace):
                with self.assertRaises(sync.SyncError) as raised:
                    sync.sync_once(self.settings)
            self.assertEqual(self.cache.read_bytes() if self.cache.exists() else None, before)
            self.assertEqual(self.path.read_bytes(), settings_before)
            self.assertNotIn('private-token', str(raised.exception))
            if not existing and self.cache.parent.exists(): self.cache.parent.rmdir()

    def test_settings_changed_during_download_are_preserved(self):
        self.seed()
        old = self.cache.read_bytes()
        def changed(settings, candidate):
            current = json.loads(self.path.read_text())
            current['source_url'] = 'http://127.0.0.1/new'
            self.path.write_text(json.dumps(current))
        self.validation.side_effect = changed
        with self.assertRaisesRegex(sync.SyncError, 'settings changed'):
            sync.sync_once(self.settings)
        self.assertEqual(self.cache.read_bytes(), old)
        self.assertEqual(json.loads(self.path.read_text())['source_url'], 'http://127.0.0.1/new')

    def test_optional_refresh_runs_after_generation_and_cache_publish(self):
        self.settings['refresh_main'] = True
        self.write_settings()
        def refresh(settings):
            self.assertTrue(self.cache.exists())
            self.assertEqual(json.loads(self.path.read_text())['generation'], settings['generation'])
            return False
        with mock.patch.object(sync, 'refresh_main', side_effect=refresh) as request:
            result = sync.sync_once(self.settings)
        request.assert_called_once()
        self.assertFalse(result['api_refreshed'])
        self.assertTrue(self.cache.exists())

    def test_symlink_refused_and_lock_prevents_overlap(self):
        with sync._sync_lock(self.path), self.assertRaisesRegex(sync.SyncError, 'already running'):
            sync.sync_once(self.settings)
        self.cache.parent.mkdir()
        target = self.root / 'target'
        target.write_bytes(b'untouched')
        self.cache.symlink_to(target)
        with self.assertRaises(sync.SyncError): sync.sync_once(self.settings)
        self.assertEqual(target.read_bytes(), b'untouched')
        self.download.assert_not_called()

    def test_settings_need_private_permissions(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(sync.SyncError, '0600'): sync.load_settings(self.path)

class ValidationTests(unittest.TestCase):
    def test_isolated_config_preserves_prefix_filters_and_disables_network_features(self):
        config = sync._validation_config({'provider_override': {'additional-prefix': 'Main:'},
                                          'provider_filter': 'good', 'provider_exclude_filter': 'bad',
                                          'provider_exclude_type': 'Direct'}, Path('/safe/provider.yaml'))
        self.assertEqual(config['rules'], ['MATCH,REJECT'])
        self.assertFalse(config['tun']['enable'])
        self.assertFalse(config['dns']['enable'])
        provider = config['proxy-providers']['Airport_01']
        self.assertEqual(provider['type'], 'file')
        self.assertNotIn('url', provider)
        self.assertEqual(provider['override']['additional-prefix'], 'Main:')
        self.assertEqual(provider['filter'], 'good')
        self.assertEqual(provider['exclude-filter'], 'bad')
        self.assertEqual(provider['exclude-type'], 'Direct')

    def test_validator_uses_private_json_and_suppresses_sensitive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'candidate.yaml'
            candidate.write_bytes(EXPORT)
            def runner(command, **kwargs):
                self.assertEqual(command[:4], ['/usr/bin/mihomo', '-t', '-d', str(candidate.parent)])
                config_path = Path(command[-1])
                self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
                self.assertEqual(json.loads(config_path.read_text())['proxy-providers']['Airport_01']['path'], str(candidate))
                self.assertEqual(kwargs['stdout'], sync.subprocess.DEVNULL)
                self.assertEqual(kwargs['stderr'], sync.subprocess.DEVNULL)
                return mock.Mock(returncode=1)
            with mock.patch.object(sync.subprocess, 'run', side_effect=runner), self.assertRaises(sync.SyncError):
                sync.validate_candidate({}, candidate)
            self.assertEqual(list(Path(directory).iterdir()), [candidate])

    def test_refresh_error_is_nonfatal_and_never_prints_exception_token(self):
        with mock.patch('mihomo_health_guard.load_secret', side_effect=Exception('private-token')):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertFalse(sync.refresh_main({}))
        self.assertIn('provider_refresh_pending', out.getvalue())
        self.assertNotIn('private-token', out.getvalue())

if __name__ == '__main__': unittest.main()
