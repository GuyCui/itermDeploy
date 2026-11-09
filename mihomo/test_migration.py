#!/usr/bin/env python3
"""Offline migration/install regression tests; never run real system commands."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import migrate_groups as migration


BASE = '''secret: "test-private-secret"
proxy-providers:
  Airport_01:
    type: file
    path: nodes.yaml
proxy-groups:
  # unrelated heading
  - name: Proxy
    type: select
    proxies:
      - Proxy-Auto
  - name: Proxy-Auto
    type: url-test
    use:
      - Airport_01
    filter: "美国|香港"
    exclude-filter: "过期"
    url: https://www.google.com/generate_204
    expected-status: 204
    interval: 120
    timeout: 5000
    tolerance: 150
    lazy: false
  # following group comment
  - name: Other
    <<: *Other-Template
rules:
  - MATCH,Proxy
'''
DOCKER = '''  - name: 'Docker-Auto'
    type: url-test
    use:
      - Airport_01
    url: https://registry-1.docker.io/v2/
    expected-status: 401
    interval: 180
'''


class TransformTests(unittest.TestCase):
    def test_normal_both_groups_preserves_unrelated_content(self):
        config = BASE.replace('rules:\n', DOCKER + 'rules:\n')
        result, names = migration.transform(config)
        self.assertEqual(names, ['Proxy-Auto', 'Docker-Auto'])
        self.assertEqual(result.count('    type: select\n'), 3)
        self.assertEqual(result.count('    empty-fallback: REJECT\n'), 2)
        self.assertEqual(result.count('    proxies:\n      - REJECT\n'), 2)
        self.assertEqual(result.count('    use:\n      - Airport_01\n'), 2)
        self.assertIn('    filter: "美国|香港"\n    exclude-filter: "过期"\n', result)
        self.assertIn('  - name: Other\n    <<: *Other-Template\n', result)
        self.assertIn('  # following group comment\n', result)
        self.assertTrue(result.startswith('secret: "test-private-secret"\n'))
        for key in migration.AUTO_KEYS:
            self.assertNotIn('    ' + key + ':', result)

    def test_quoted_names_and_type(self):
        for name in ("'Proxy-Auto'", '"Proxy-Auto"', '"Proxy\\u002dAuto"'):
            with self.subTest(name=name):
                config = BASE.replace('- name: Proxy-Auto', '- name: ' + name)
                config = config.replace('type: url-test', "type: 'url-test' # measurement")
                result, names = migration.transform(config)
                self.assertEqual(names, ['Proxy-Auto'])
                self.assertIn('type: select # measurement', result)

    def test_idempotent_and_crlf(self):
        first, _ = migration.transform(BASE.replace('\n', '\r\n'))
        second, _ = migration.transform(first)
        self.assertEqual(first, second)
        self.assertEqual(first.count('\n'), first.count('\r\n'))

    def test_preserves_existing_explicit_nodes_after_reject(self):
        config = BASE.replace('    filter:', '''    proxies:
      # manually named node remains available
      - 'REJECT'
      - DirectNode # node comment
      - "REJECT"
    filter:''')
        result, _ = migration.transform(config)
        self.assertIn('    proxies:\n      - REJECT\n', result)
        self.assertIn('      - DirectNode # node comment\n', result)
        self.assertNotIn("- 'REJECT'", result)
        self.assertEqual(migration.transform(result)[0], result)

    def test_simple_reject_flow_list_is_safe(self):
        for proxies in ('[]', '[REJECT]', '["REJECT"] # placeholder'):
            with self.subTest(proxies=proxies):
                config = BASE.replace('    filter:', '    proxies: ' + proxies + '\n    filter:')
                self.assertIn('      - REJECT\n', migration.transform(config)[0])

    def test_refuses_complex_managed_blocks(self):
        cases = [
            BASE.replace('- name: Proxy-Auto', '- name: &managed Proxy-Auto'),
            BASE.replace('    type: url-test', '    <<: *automatic\n    type: url-test'),
            BASE.replace('    type: url-test', '    type: *automatic'),
            BASE.replace('    filter:', '    proxies: [Node-A, Node-B]\n    filter:'),
            BASE.replace('    filter:', '    proxies:\n      - {name: Node-A}\n    filter:'),
            BASE.replace('    filter:', '    proxies:\n      - Proxy\n    filter:'),
            BASE.replace('  - name: Proxy-Auto\n    type: url-test', '  - {name: Proxy-Auto, type: url-test}'),
            BASE.replace('    type: url-test', '    type: url-test\n    type: select'),
            BASE.replace('    type: url-test', '    type: fallback'),
            BASE.replace('    type: url-test', '\t type: url-test'),
        ]
        for config in cases:
            with self.subTest(config=config.splitlines()[13:18]):
                with self.assertRaises(migration.MigrationError):
                    migration.transform(config)

    def test_preserves_removed_fields_inline_comments(self):
        config = BASE.replace('    interval: 120', '    interval: 120 # avoid repeated automatic switches')
        config = config.replace('    url: https://www.google.com/generate_204',
                                '    url: "https://www.google.com/generate_204#inside" # health target')
        result, _ = migration.transform(config)
        self.assertIn('    # avoid repeated automatic switches\n', result)
        self.assertIn('    # health target\n', result)
        self.assertNotIn('    #inside', result)
        self.assertEqual(migration.transform(result)[0], result)

    def test_requires_proxy_auto_and_provider(self):
        with self.assertRaises(migration.MigrationError):
            migration.transform(BASE.replace('name: Proxy-Auto', 'name: Unmanaged'))
        with self.assertRaises(migration.MigrationError):
            migration.transform(BASE.replace('    use:\n      - Airport_01\n', ''))


class LiveShapeTests(unittest.TestCase):
    def test_requires_selector_and_reject_as_first_item(self):
        cases = [({'type': 'Selector', 'emptyFallback': 'REJECT', 'all': ['REJECT', 'node']}, True),
                 ({'type': 'URLTest', 'emptyFallback': 'REJECT', 'all': ['REJECT', 'node']}, False),
                 ({'type': 'Selector', 'emptyFallback': 'REJECT', 'all': ['node', 'REJECT']}, False),
                 ({'type': 'Selector', 'emptyFallback': 'REJECT', 'all': []}, False),
                 ({'type': 'Selector', 'emptyFallback': 'REJECT'}, False),
                 ({'type': 'Selector', 'all': ['REJECT', 'node']}, False),
                 ({'type': 'Selector', 'emptyFallback': 'DIRECT', 'all': ['REJECT', 'node']}, False),
                 (None, False)]
        for group, expected in cases:
            with self.subTest(group=group):
                api = mock.Mock()
                api.group.return_value = group
                with mock.patch.object(migration.time, 'monotonic', side_effect=[0, 0, 2]):
                    with mock.patch.object(migration.time, 'sleep'):
                        self.assertEqual(migration.verify_selectors(api, ['Proxy-Auto'], timeout=1), expected)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text(BASE)
        self.config.chmod(0o640)
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, command, timeout=45):
        self.calls.append(command)
        return True

    def test_validation_candidate_same_directory_and_permissions(self):
        seen = []
        def runner(command, timeout=45):
            self.calls.append(command)
            if command[0] == 'fake-mihomo':
                candidate = Path(command[-1])
                self.assertEqual(candidate.parent, self.root.resolve())
                self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o640)
                self.assertIn('type: select', candidate.read_text())
                seen.append(candidate)
            return True
        result = migration.migrate(self.config, mihomo='fake-mihomo', runner=runner,
                                   verifier=lambda api, names: True)
        self.assertTrue(result['changed'])
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        backup = Path(result['backup'])
        self.assertEqual(backup.read_text(), BASE)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertTrue(all(not candidate.exists() for candidate in seen))
        self.assertEqual(self.calls[1], ['systemctl', 'reload', 'mihomo.service'])

    def test_failed_validation_leaves_original_and_no_reload(self):
        with self.assertRaises(migration.MigrationError):
            migration.migrate(self.config, runner=lambda command: False,
                              verifier=lambda api, names: True)
        self.assertEqual(self.config.read_text(), BASE)
        self.assertEqual(list(self.root.glob('.guard-*')), [])

    def test_failed_reload_restores_original(self):
        reload_count = 0
        def runner(command, timeout=45):
            nonlocal reload_count
            self.calls.append(command)
            if command[0] == 'systemctl':
                reload_count += 1
                return reload_count > 1
            return True
        with self.assertRaises(migration.MigrationError) as caught:
            migration.migrate(self.config, runner=runner, verifier=lambda api, names: True)
        self.assertEqual(self.config.read_text(), BASE)
        self.assertEqual(reload_count, 2)
        self.assertNotIn('test-private-secret', str(caught.exception))

    def test_wrong_live_type_restores_original(self):
        with self.assertRaises(migration.MigrationError):
            migration.migrate(self.config, runner=self.runner, verifier=lambda api, names: False)
        self.assertEqual(self.config.read_text(), BASE)
        self.assertEqual(self.calls.count(['systemctl', 'reload', 'mihomo.service']), 2)

    def test_unchanged_does_not_write_or_backup(self):
        selected = migration.transform(BASE)[0]
        self.config.write_text(selected)
        result = migration.migrate(self.config, runner=self.runner, verifier=lambda api, names: True)
        self.assertFalse(result['changed'])
        self.assertIsNone(result['backup'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.config.read_text(), selected)

    def test_check_and_output_never_run_commands_or_modify_original(self):
        out = self.root / 'candidate.yaml'
        with mock.patch.object(migration, 'quiet_run', side_effect=AssertionError('command called')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(migration.main(['--config', str(self.config), '--check']), 0)
                self.assertEqual(migration.main(['--config', str(self.config), '--output', str(out)]), 0)
        self.assertEqual(self.config.read_text(), BASE)
        self.assertEqual(out.read_text(), migration.transform(BASE)[0])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / 'package'
        self.source.mkdir()
        self.config = self.root / 'config.yaml'
        self.config.write_text(BASE)
        self.config.chmod(0o640)
        self.destinations = {name: self.root / 'installed' / name
                             for name in ('mihomo_health_guard.py',) + migration.UNITS}
        for name in self.destinations:
            (self.source / name).write_text('# v2\n' if name.endswith('.py') else '[Unit]\n# v2\n')
        self.calls = []
        self.enabled = False
        self.active = False
        self.fail_command = None
        self.patches = [mock.patch.object(migration, 'BACKUP_ROOT', self.root / 'snapshots'),
                        mock.patch.object(migration, 'installed_destinations', return_value=self.destinations),
                        mock.patch.object(migration.os, 'geteuid', return_value=0),
                        mock.patch.object(migration.os, 'chown'),
                        mock.patch.object(migration, 'quiet_run', side_effect=self.runner),
                        mock.patch.object(migration.subprocess, 'run'),
                        mock.patch.object(migration, 'service_exit_status', return_value=2),
                        mock.patch.object(migration, 'migrate', side_effect=self.fake_migrate)]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(self.patches)])
        self.addCleanup(self.directory.cleanup)

    def runner(self, command, timeout=45):
        self.calls.append(command)
        if command == self.fail_command:
            self.fail_command = None
            return False
        if command[1] == 'is-enabled':
            return self.enabled
        if command[1] == 'is-active':
            return self.active
        return True

    def fake_migrate(self, config, api, mihomo, backup):
        self.calls.append(['MIGRATE'])
        Path(config).write_text(migration.transform(Path(config).read_text())[0])
        return {'changed': True}

    def snapshot(self):
        return next((self.root / 'snapshots').glob('upgrade-*'))

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return migration.install(self.source, self.config, 'http://127.0.0.1:9093', 'fake-mihomo')

    def test_first_install_saves_private_snapshot_and_rollback_disables_new_timer(self):
        self.assertEqual(self.install(), 0)
        snapshot = self.snapshot()
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((snapshot / 'manifest.json').stat().st_mode), 0o600)
        self.assertEqual(json.loads((snapshot / 'manifest.json').read_text())['files'],
                         {name: None for name in self.destinations})
        self.calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            migration.rollback(snapshot, runner=self.runner)
        self.assertEqual(self.config.read_text(), BASE)
        self.assertFalse(any(path.exists() for path in self.destinations.values()))
        self.assertIn(['systemctl', 'disable', migration.UNITS[1]], self.calls)
        self.assertNotIn(['systemctl', 'start', migration.UNITS[1]], self.calls)
        self.assertLess(self.calls.index(['systemctl', 'disable', migration.UNITS[1]]),
                        self.calls.index(['systemctl', 'daemon-reload']))

    def test_upgrade_stops_old_guard_then_restores_files_and_timer(self):
        for path in self.destinations.values():
            path.parent.mkdir(exist_ok=True)
            path.write_text('# v1\n')
            path.chmod(0o640)
        self.enabled = self.active = True
        self.assertEqual(self.install(), 0)
        self.assertLess(self.calls.index(['systemctl', 'stop', migration.UNITS[0]]),
                        self.calls.index(['MIGRATE']))
        self.calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            migration.rollback(self.snapshot(), runner=self.runner)
        self.assertEqual(self.config.read_text(), BASE)
        for path in self.destinations.values():
            self.assertEqual(path.read_text(), '# v1\n')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertIn(['systemctl', 'enable', migration.UNITS[1]], self.calls)
        self.assertIn(['systemctl', 'start', migration.UNITS[1]], self.calls)

    def test_failed_enable_restores_original_and_removes_new_files(self):
        self.fail_command = ['systemctl', 'enable', '--now', migration.UNITS[1]]
        with self.assertRaises(migration.MigrationError):
            self.install()
        self.assertEqual(self.config.read_text(), BASE)
        self.assertFalse(any(path.exists() for path in self.destinations.values()))
        self.assertIn(['systemctl', 'disable', migration.UNITS[1]], self.calls)
        self.assertNotIn(['systemctl', 'start', migration.UNITS[1]], self.calls)

    def test_failed_first_check_restores_full_previous_installation(self):
        for path in self.destinations.values():
            path.parent.mkdir(exist_ok=True)
            path.write_text('# v1\n')
        self.enabled = self.active = True
        self.fail_command = ['systemctl', 'start', migration.UNITS[0]]
        with self.assertRaises(migration.MigrationError):
            self.install()
        self.assertEqual(self.config.read_text(), BASE)
        for path in self.destinations.values():
            self.assertEqual(path.read_text(), '# v1\n')
        self.assertIn(['systemctl', 'enable', migration.UNITS[1]], self.calls)
        self.assertIn(['systemctl', 'start', migration.UNITS[1]], self.calls)

    def test_no_healthy_nodes_keeps_v2_timer_for_retry(self):
        self.fail_command = ['systemctl', 'start', migration.UNITS[0]]
        with mock.patch.object(migration, 'service_exit_status', return_value=1):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.install(), 1)
        self.assertEqual(self.config.read_text(), migration.transform(BASE)[0])
        self.assertTrue(all(path.exists() for path in self.destinations.values()))
        self.assertNotIn(['systemctl', 'disable', migration.UNITS[1]], self.calls)

    def test_failed_stop_restores_old_timer_state(self):
        for path in self.destinations.values():
            path.parent.mkdir(exist_ok=True)
            path.write_text('# v1\n')
        self.enabled = self.active = True
        self.fail_command = ['systemctl', 'stop', migration.UNITS[0]]
        with self.assertRaises(migration.MigrationError):
            self.install()
        self.assertEqual(self.config.read_text(), BASE)
        self.assertNotIn(['MIGRATE'], self.calls)
        self.assertIn(['systemctl', 'start', migration.UNITS[1]], self.calls)

    def test_invalid_manifest_fails_before_changes(self):
        self.install()
        snapshot = self.snapshot()
        manifest_file = snapshot / 'manifest.json'
        manifest = json.loads(manifest_file.read_text())
        manifest['config_metadata']['mode'] = 'bad'
        manifest_file.write_text(json.dumps(manifest))
        current = self.config.read_text()
        self.calls.clear()
        with self.assertRaises(migration.MigrationError):
            migration.rollback(snapshot, runner=self.runner)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config.read_text(), current)


if __name__ == '__main__':
    unittest.main()
