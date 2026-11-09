"""Offline tests for the v3 configuration and private rollback transaction."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml
import upgrade_speed as upgrade
import speed_network

SOURCE = '''PProviders: &P
  type: http
  interval: 10800
proxy-providers:
  Airport_01:
    <<: *P
    url: http://127.0.0.1:8000/private
    override:
      additional-prefix: 'Main:'
  Other:
    type: file
    path: other.yaml
proxy-groups:
  - name: Proxy-Auto
    type: select
    use:
      - Airport_01
rules:
  - MATCH,Proxy-Auto
'''


class ConfigurationTests(unittest.TestCase):
    def test_only_provider_block_replaced_and_main_prefix_preserved(self):
        original = yaml.safe_load(SOURCE)['proxy-providers']['Airport_01']
        provider = upgrade.file_provider(original)
        revised = upgrade.replace_provider(SOURCE, provider)
        parsed = yaml.safe_load(revised)
        self.assertEqual(parsed['proxy-providers']['Airport_01']['type'], 'file')
        self.assertEqual(parsed['proxy-providers']['Airport_01']['override']['additional-prefix'], 'Main:')
        self.assertNotIn('url', parsed['proxy-providers']['Airport_01'])
        self.assertEqual(parsed['proxy-providers']['Other'], {'type': 'file', 'path': 'other.yaml'})
        self.assertEqual(revised.split('proxy-groups:')[1], SOURCE.split('proxy-groups:')[1])
        self.assertTrue(revised.startswith('PProviders: &P\n'))

    def test_quoted_provider_and_final_provider_work(self):
        text = SOURCE.replace('  Airport_01:', '  "Airport_01":')
        text = text.replace('  Other:\n    type: file\n    path: other.yaml\n', '')
        parsed = yaml.safe_load(upgrade.replace_provider(text, {'type': 'file', 'path': '/cache'}))
        self.assertEqual(parsed['proxy-providers'], {'Airport_01': {'type': 'file', 'path': '/cache'}})

    def test_refuses_ambiguous_inline_provider_without_mutation(self):
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.replace_provider(SOURCE.replace('  Airport_01:', '  Airport_01: {type: http}'), {})

    def test_sidecar_uses_loopback_no_tun_and_own_mark(self):
        config = upgrade.make_sidecar({'type': 'file', 'path': '/private/cache',
                                       'override': {'additional-prefix': 'Main:'}}, 'test-secret')
        self.assertFalse(config['allow-lan'])
        self.assertFalse(config['tun']['enable'])
        self.assertFalse(config['dns']['enable'])
        self.assertEqual(config['bind-address'], '127.0.0.1')
        self.assertEqual(config['external-controller'], '127.0.0.1:19094')
        self.assertEqual(config['routing-mark'], speed_network.MARK)
        self.assertEqual(config['interface-name'], 'eth0')
        self.assertEqual(config['proxy-providers']['Airport_01']['override']['additional-prefix'], 'Main:')
        self.assertEqual(config['proxy-groups'][0]['proxies'], ['REJECT'])

    def test_mark_rule_rejects_other_rules(self):
        good = {'priority': 8990, 'fwmark': '0x4d48', 'table': 'main'}
        self.assertTrue(speed_network.matches(good))
        for row in (dict(good, fwmark='0x4d49'), dict(good, table=2022),
                    dict(good, priority=9000), dict(good, fwmask='0xffff'), {}):
            self.assertFalse(speed_network.matches(row))

    def test_wait_provider_does_not_accept_old_http_nodes(self):
        api = mock.Mock()
        api.provider.side_effect = [
            {'vehicleType': 'HTTP', 'proxies': [1] * 15},
            {'vehicleType': 'File', 'proxies': [1] * 299}]
        with mock.patch.object(upgrade.time, 'sleep'):
            self.assertEqual(upgrade.wait_file_provider(api), 299)
        self.assertEqual(api.provider.call_count, 2)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_restores_bytes_permissions_and_unit_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing, absent = root / 'config.yaml', root / 'new.py'
            existing.write_text('private-original')
            existing.chmod(0o640)
            units = {name: {'enabled': name == 'mihomo-health-guard.timer',
                            'active': name == 'mihomo-health-guard.timer'}
                     for name in upgrade.TIMERS + ('mihomo-speed-probe.service',)}
            def fake_run(cmd, **kwargs):
                if 'is-enabled' in cmd:
                    return units[cmd[-1]]['enabled']
                if 'is-active' in cmd:
                    return units[cmd[-1]]['active']
                return True
            with mock.patch.object(upgrade, 'STATE', root), \
                 mock.patch.object(upgrade, 'destinations', return_value=[existing, absent]), \
                 mock.patch.object(upgrade, 'run', side_effect=fake_run) as runner:
                folder = upgrade.snapshot()
                manifest = json.loads((folder / 'manifest.json').read_text())
                self.assertEqual(manifest['units'], units)
                self.assertEqual((folder / 'manifest.json').stat().st_mode & 0o777, 0o600)
                existing.write_text('changed')
                existing.chmod(0o600)
                absent.write_text('new install')
                upgrade.rollback(folder)
                self.assertEqual(existing.read_text(), 'private-original')
                self.assertEqual(existing.stat().st_mode & 0o777, 0o640)
                self.assertFalse(absent.exists())
                self.assertTrue(any(call.args[0] == ['systemctl', 'start', 'mihomo-health-guard.timer']
                                    for call in runner.call_args_list))

    def test_bad_manifest_is_rejected_before_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'manifest.json').write_text(json.dumps({'version': 3, 'files': [], 'units': {}}))
            with mock.patch.object(upgrade, 'run') as runner:
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.rollback(root)
                runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
