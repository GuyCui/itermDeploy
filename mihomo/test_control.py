import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import mihomo_control as control
import mihomo_health_guard as health
import provider_cache_sync as sync


class ControlTests(unittest.TestCase):
    def test_21_source_19_loaded_report_is_private_and_read_only(self):
        canary = 'PRIVATE-NAME-KEY-TOKEN'
        rows = [{'name': canary + word, 'type': 'hysteria2',
                 'server': 'private.example', 'password': canary}
                for word in ['流量', '到期'] + [str(i) for i in range(19)]]
        payload = yaml.safe_dump({'proxies': rows}, allow_unicode=True).encode()
        loaded = [{'name': 'Main:' + n['name'], 'type': 'Hysteria2'} for n in rows[2:]]
        pattern = '^(?!.*(流量|到期)).*'
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            cache = base / 'nodes.yaml'
            config = base / 'config.yaml'
            settings = base / 'settings.json'
            cache.write_bytes(payload)
            provider = {'type': 'file', 'path': str(cache), 'filter': pattern,
                        'override': {'additional-prefix': 'Main:'}}
            config.write_text(yaml.safe_dump({'secret': canary,
                                             'proxy-providers': {'Airport_01': provider}}))
            settings.write_text(json.dumps({'cache_path': str(cache),
                                            'source_url': 'https://private.example/' + canary,
                                            'sidecar_secret': canary,
                                            'provider_filter': pattern,
                                            'provider_override': provider['override']}))
            settings.chmod(0o600)
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.iterdir()}
            requests = []

            def get_only(api, method, path, body=None):
                requests.append((method, path, body))
                self.assertEqual(method, 'GET')
                self.assertIsNone(body)
                if path.startswith('/providers/'):
                    return {'proxies': loaded, 'vehicleType': 'File'}
                return {'type': 'Selector', 'all': [canary], 'now': canary}

            def status_only(command, **kwargs):
                self.assertEqual(command[:3], ['systemctl', 'is-active', '--quiet'])
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(health.API, 'request', get_only), \
                    mock.patch.object(sync, 'download_export', return_value=payload), \
                    mock.patch.object(control.subprocess, 'run', side_effect=status_only):
                report = control.inspect(config, settings)
            self.assertTrue(requests)
            self.assertEqual(report['source']['nodes'], 21)
            self.assertEqual(report['cache']['nodes'], 21)
            self.assertEqual(report['main_provider']['nodes'], 19)
            self.assertTrue(report['main_sidecar_nodes_match'])
            self.assertEqual(report['name_filter']['excluded'], 2)
            self.assertEqual(report['name_filter']['remaining'], 19)
            self.assertIn('CACHE_MAIN_COUNT_GAP_MATCHES_NAME_FILTER', report['findings'])
            text = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(canary, text)
            self.assertNotIn('private.example', text)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.iterdir()})

    def test_arbitrary_regex_is_not_guessed(self):
        report = control.name_filter_summary([{'name': '流量是真节点'}],
                                             {'filter': '(?i)normal|流量'})
        self.assertFalse(report['supported'])
        self.assertNotIn('entries', report)
        multiline = control.name_filter_summary([{'name': 'normal\n流量'}],
                                                {'filter': '^(?!.*(流量|到期)).*'})
        self.assertFalse(multiline['supported'])

    def test_equal_counts_do_not_imply_small_provider_or_healthy_nodes(self):
        report = {'main_provider': {'ok': True, 'nodes': 81},
                  'cache': {'ok': True, 'nodes': 81, 'normalized_sha256': 'same'},
                  'source': {'ok': True, 'nodes': 81, 'normalized_sha256': 'same'},
                  'settings': {'ok': True}}
        self.assertIn('SOURCE_CACHE_MAIN_COUNTS_MATCH', control.classify(report))
        self.assertNotIn('19_MAY_BE_GROUPS_PLUS_BUILTINS_NOT_NODES', control.classify(report))

    def test_sync_failure_has_nonzero_exit_and_safe_output(self):
        with mock.patch.object(sys, 'argv', ['mihomo_control.py', 'sync', '--no-source']), \
                mock.patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
                mock.patch.object(control, 'inspect', return_value={'read_only': True}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(control.main(), 1)
        self.assertFalse(json.loads(output.getvalue())['sync_service_succeeded'])

    def test_api_sets_mismatch_is_visible_even_at_equal_counts(self):
        self.assertIn('MAIN_AND_SIDECAR_NODE_SETS_DIFFER',
                      control.classify({'settings': {'ok': True},
                                        'main_provider': {'ok': True, 'nodes': 19},
                                        'main_sidecar_nodes_match': False}))


if __name__ == '__main__':
    unittest.main()
