#!/usr/bin/env python3
"""Local fake-API tests. No real mihomo instance or Internet target is used."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from unittest import mock

import mihomo_health_guard as guard


class Fake:
    def __init__(self, health, current="bad", secondary="bad", types=None, probe_delay=0,
                 statuses=None):
        names = list(health)
        self.nodes = [{"name": name, "type": (types or {}).get(name, "Http"),
                       "history": [{"delay": 50 + index}]}
                      for index, name in enumerate(names)]
        self.health = health
        self.statuses = statuses or {}
        self.skip_history_updates = False
        self.history_counter = 0
        self.groups = {
            "Proxy-Auto": {"type": "Selector", "all": ["REJECT"] + names,
                           "now": current, "emptyFallback": "REJECT"},
            "Docker-Auto": {"type": "Selector", "all": ["REJECT"] + names,
                            "now": secondary, "emptyFallback": "REJECT"},
        }
        self.probe_delay = probe_delay
        self.active_probes = 0
        self.max_active_probes = 0
        self.calls = []
        self.puts = []
        self.lock = threading.Lock()

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, payload=None):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if payload is not None:
                    self.wfile.write(json.dumps(payload).encode())

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer test-secret":
                    self.send(401)
                    return
                parsed = urllib.parse.urlsplit(self.path)
                parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
                if parts == ["providers", "proxies", "Airport_01"]:
                    with owner.lock:
                        result = json.loads(json.dumps({"proxies": owner.nodes}))
                    self.send(200, result)
                elif len(parts) == 4 and parts[:3] == ["providers", "proxies", "Airport_01"]:
                    with owner.lock:
                        node = next((node for node in owner.nodes if node["name"] == parts[3]), None)
                        result = json.loads(json.dumps(node))
                    self.send(200 if node else 404, result)
                elif len(parts) == 2 and parts[0] == "proxies":
                    group = owner.groups.get(parts[1])
                    self.send(200 if group else 404, group)
                elif (len(parts) == 5 and parts[:3] == ["providers", "proxies", "Airport_01"]
                      and parts[4] == "healthcheck"):
                    query = urllib.parse.parse_qs(parsed.query)
                    node, probe_url = parts[3], query.get("url", [""])[0]
                    suffix = "?" + guard.PROBE_QUERY
                    url = probe_url[:-len(suffix)] if probe_url.endswith(suffix) else ""
                    expected = query.get("expected", [""])[0]
                    targets = {target[1]: str(target[2]) for target in guard.TARGETS}
                    if targets.get(url) != expected or query.get("timeout") != ["4000"]:
                        self.send(400)
                        return
                    with owner.lock:
                        owner.calls.append((node, url))
                        outcome = owner.health.get(node, {}).get(url, False)
                        if isinstance(outcome, list):
                            outcome = outcome.pop(0) if outcome else False
                        status = owner.statuses.get(node, {}).get(url, int(expected))
                        if isinstance(status, list):
                            status = status.pop(0) if status else 500
                        owner.active_probes += 1
                        owner.max_active_probes = max(owner.max_active_probes, owner.active_probes)
                    if owner.probe_delay:
                        time.sleep(owner.probe_delay)
                    with owner.lock:
                        owner.active_probes -= 1
                        if not owner.skip_history_updates:
                            record = next(entry for entry in owner.nodes if entry["name"] == node)
                            entry = record.setdefault("extra", {}).setdefault(
                                probe_url, {"alive": False, "history": []})
                            owner.history_counter += 1
                            stamp = (guard.datetime.datetime(2026, 11, 7,
                                      tzinfo=guard.datetime.timezone.utc)
                                     + guard.datetime.timedelta(microseconds=owner.history_counter))
                            passed = bool(outcome) and status == int(expected)
                            entry["alive"] = passed
                            entry["history"].append({"time": stamp.isoformat(),
                                                     "delay": 42 if passed else 0})
                            entry["history"] = entry["history"][-10:]
                            # Match the affected core: status mismatch leaves
                            # global alive and response delay positive.
                            record["alive"] = bool(outcome)
                    self.send(200, {"delay": 42}) if outcome else self.send(504)
                else:
                    self.send(404)

            def do_PUT(self):
                if self.headers.get("Authorization") != "Bearer test-secret":
                    self.send(401)
                    return
                parts = [urllib.parse.unquote(p) for p in self.path.strip("/").split("/")]
                if len(parts) != 2 or parts[0] != "proxies" or parts[1] not in owner.groups:
                    self.send(404)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size))
                node = payload.get("name")
                if node not in owner.groups[parts[1]]["all"]:
                    self.send(400)
                    return
                with owner.lock:
                    owner.puts.append((parts[1], node))
                    owner.groups[parts[1]].update(now=node)
                self.send(204)

        return Handler


@contextlib.contextmanager
def fake_api(fake):
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield guard.API("http://127.0.0.1:" + str(server.server_port), "test-secret")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def all_targets(passed):
    return {url: passed for _, url, _ in guard.TARGETS}


class GuardTests(unittest.TestCase):
    def run_guard(self, fake, state=None, **kwargs):
        kwargs.setdefault("warmup_candidates", 0)
        with fake_api(fake) as api, contextlib.redirect_stdout(io.StringIO()):
            return guard.Guard(api, ["Proxy-Auto", "Docker-Auto"], "Airport_01",
                               state if state is not None else {}, retry_delay=0, **kwargs).run()

    def test_selects_only_candidate_passing_all_targets_twice(self):
        partial = all_targets(True)
        partial[guard.TARGETS[1][1]] = False
        healthy = "Main:HTTP 美国/健康 ♥"
        fake = Fake({"bad": all_targets(False), "partial": partial,
                     healthy: all_targets(True)})
        self.assertEqual(self.run_guard(fake), 0)
        self.assertEqual(fake.puts, [("Proxy-Auto", healthy), ("Docker-Auto", healthy)])
        for _, url, _ in guard.TARGETS:
            self.assertEqual(fake.calls.count((healthy, url)), 2)

    def test_all_fail_keeps_selection_and_never_uses_direct(self):
        fake = Fake({"bad": all_targets(False), "other": all_targets(False),
                     "DIRECT": all_targets(True)}, types={"DIRECT": "Direct"})
        self.assertEqual(self.run_guard(fake), 1)
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.groups["Proxy-Auto"]["now"], "bad")
        self.assertFalse(any(node == "DIRECT" for node, _ in fake.calls))

    def test_current_healthy_is_kept_without_testing_candidates(self):
        fake = Fake({"good": all_targets(True), "other": all_targets(True)},
                    current="good", secondary="good")
        self.assertEqual(self.run_guard(fake), 0)
        self.assertEqual(fake.puts, [])
        self.assertEqual({node for node, _ in fake.calls}, {"good"})

    def test_current_healthy_aligns_secondary(self):
        fake = Fake({"good": all_targets(True), "bad": all_targets(False)},
                    current="good", secondary="bad")
        self.assertEqual(self.run_guard(fake), 0)
        self.assertEqual(fake.puts, [("Docker-Auto", "good")])

    def test_current_transient_failure_retries_without_switch(self):
        health = all_targets(True)
        health[guard.TARGETS[0][1]] = [False, True]
        fake = Fake({"good": health, "other": all_targets(True)},
                    current="good", secondary="good")
        self.assertEqual(self.run_guard(fake), 0)
        self.assertEqual(fake.puts, [])
        self.assertEqual(len(fake.calls), 6)

    def test_new_candidate_must_pass_second_round(self):
        health = all_targets(True)
        health[guard.TARGETS[1][1]] = [True, False]
        fake = Fake({"bad": all_targets(False), "flaky": health})
        self.assertEqual(self.run_guard(fake), 1)
        self.assertEqual(fake.puts, [])

    def test_dry_run_probes_but_never_puts(self):
        fake = Fake({"bad": all_targets(False), "good": all_targets(True)})
        self.assertEqual(self.run_guard(fake, dry_run=True), 0)
        self.assertEqual(fake.puts, [])
        self.assertTrue(fake.calls)

    def test_rotation_eventually_covers_nodes_beyond_round_budget(self):
        health = {"bad": all_targets(False)}
        health.update({"candidate" + str(i): all_targets(False) for i in range(5)})
        health["last-good"] = all_targets(True)
        fake, state = Fake(health), {}
        with fake_api(fake) as api, contextlib.redirect_stdout(io.StringIO()):
            controller = guard.Guard(api, ["Proxy-Auto", "Docker-Auto"], "Airport_01",
                                     state, max_candidates=2, retry_delay=0, candidate_workers=2,
                                     warmup_candidates=0)
            self.assertEqual(controller.run(), 1)
            self.assertEqual(controller.run(), 1)
            self.assertEqual(controller.run(), 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "last-good"))

    def test_secondary_is_optional_but_primary_is_required(self):
        fake = Fake({"good": all_targets(True)}, current="good", secondary="good")
        del fake.groups["Docker-Auto"]
        self.assertEqual(self.run_guard(fake), 0)
        del fake.groups["Proxy-Auto"]
        with self.assertRaises(guard.APIError):
            self.run_guard(fake)

    def test_api_ignores_proxy_environment(self):
        fake = Fake({"good": all_targets(True)}, current="good", secondary="good")
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1",
                                         "HTTP_PROXY": "http://127.0.0.1:1",
                                         "HTTPS_PROXY": "http://127.0.0.1:1",
                                         "NO_PROXY": ""}):
            self.assertEqual(self.run_guard(fake), 0)

    def test_urltest_groups_are_rejected_instead_of_fake_fixed(self):
        fake = Fake({"good": all_targets(True)}, current="good", secondary="good")
        fake.groups["Proxy-Auto"]["type"] = "URLTest"
        with self.assertRaisesRegex(guard.GuardError, "Selector"):
            self.run_guard(fake)
        self.assertEqual(fake.puts, [])

    def test_selector_without_first_reject_is_refused(self):
        fake = Fake({"good": all_targets(True)}, current="good", secondary="good")
        for nodes in (["good", "REJECT"], ["good"], []):
            with self.subTest(nodes=nodes):
                fake.groups["Proxy-Auto"]["all"] = nodes
                with self.assertRaisesRegex(guard.GuardError, "REJECT.*first"):
                    self.run_guard(fake)
                self.assertEqual(fake.puts, [])

    def test_positive_api_delay_does_not_hide_wrong_http_status(self):
        for target, status in ((guard.TARGETS[1], 403), (guard.TARGETS[0], 302)):
            with self.subTest(status=status):
                fake = Fake({"good": all_targets(True)},
                            statuses={"good": {target[1]: status}})
                with fake_api(fake) as api:
                    result = api.probe("Airport_01", "good", target)
                self.assertFalse(result["ok"])
                self.assertTrue(fake.nodes[0]["alive"])
                row = fake.nodes[0]["extra"][target[1] + "?" + guard.PROBE_QUERY]
                self.assertFalse(row["alive"])
                self.assertEqual(row["history"][-1]["delay"], 0)

    def test_selector_with_unsafe_empty_fallback_is_refused(self):
        fake = Fake({"good": all_targets(True)}, current="good", secondary="good")
        fake.groups["Proxy-Auto"]["emptyFallback"] = "COMPATIBLE"
        with self.assertRaisesRegex(guard.GuardError, "empty-fallback"):
            self.run_guard(fake)
        self.assertEqual(fake.puts, [])

    def test_old_successful_history_cannot_validate_a_new_probe(self):
        fake = Fake({"good": all_targets(True)})
        target = guard.TARGETS[0]
        url = target[1] + "?" + guard.PROBE_QUERY
        fake.nodes[0]["extra"] = {url: {"alive": True, "history": [
            {"time": "2026-11-07T00:00:00+00:00", "delay": 42}]}}
        fake.skip_history_updates = True
        with fake_api(fake) as api:
            self.assertFalse(api.probe("Airport_01", "good", target)["ok"])

    def test_missing_status_history_cannot_validate_a_positive_delay(self):
        fake = Fake({"good": all_targets(True)})
        fake.skip_history_updates = True
        with fake_api(fake) as api:
            self.assertFalse(api.probe("Airport_01", "good", guard.TARGETS[0])["ok"])

    def test_probe_history_requires_a_valid_timestamp(self):
        url = guard.TARGETS[0][1]
        for stamp in (None, "", "invalid", "2026-11-07T00:00:00"):
            with self.subTest(stamp=stamp):
                node = {"extra": {url: {"alive": True, "history": [
                    {"time": stamp, "delay": 42}]}}}
                self.assertIsNone(guard.API._probe_history(node, url))

    def test_empty_provider_selects_reject_and_reports_failure(self):
        fake = Fake({}, current="gone", secondary="gone")
        state = {}
        self.assertEqual(self.run_guard(fake, state), 1)
        self.assertEqual(fake.puts, [("Proxy-Auto", "REJECT"), ("Docker-Auto", "REJECT")])
        self.assertEqual(state["last_result"], "empty_provider")
        self.assertEqual(fake.calls, [])

    def test_no_common_real_pool_fails_closed(self):
        fake = Fake({"good": all_targets(True)}, current="good", secondary="good")
        fake.groups["Docker-Auto"]["all"] = ["REJECT"]
        state = {}
        self.assertEqual(self.run_guard(fake, state), 1)
        self.assertEqual(state["last_result"], "empty_shared_pool")
        self.assertEqual(fake.puts, [("Proxy-Auto", "REJECT"), ("Docker-Auto", "REJECT")])

    def test_dry_run_empty_provider_does_not_select_reject(self):
        fake = Fake({}, current="gone", secondary="gone")
        self.assertEqual(self.run_guard(fake, dry_run=True), 1)
        self.assertEqual(fake.puts, [])

    def test_recent_verified_backup_is_retested_before_rotation_candidates(self):
        fake = Fake({"bad": all_targets(False), "cold": all_targets(True),
                     "backup": all_targets(True)})
        state = {"health": {"backup": {"verified_at": time.time() - 10}},
                 "rotation": ["cold", "backup"]}
        self.assertEqual(self.run_guard(fake, state, candidate_workers=1), 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "backup"))
        self.assertFalse(any(node == "cold" for node, _ in fake.calls))
        self.assertEqual(sum(node == "backup" for node, _ in fake.calls), 6)

    def test_cached_backup_that_now_fails_is_not_selected(self):
        fake = Fake({"bad": all_targets(False), "backup": all_targets(False),
                     "fresh": all_targets(True)})
        state = {"health": {"backup": {"verified_at": time.time() - 10}}}
        self.assertEqual(self.run_guard(fake, state, candidate_workers=1), 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "fresh"))
        self.assertEqual(state["health"]["backup"]["verified_at"], 0)
        self.assertGreater(state["health"]["backup"]["failures"], 0)

    def test_current_healthy_warms_two_backups_then_stops_fresh_warmups(self):
        fake = Fake({"good": all_targets(True), "backup1": all_targets(True),
                     "backup2": all_targets(True), "unused": all_targets(True)},
                    current="good", secondary="good")
        state = {}
        self.assertEqual(self.run_guard(fake, state, warmup_candidates=2), 0)
        self.assertEqual(fake.puts, [])
        self.assertEqual({node for node, _ in fake.calls}, {"good", "backup1", "backup2"})
        for name in ("backup1", "backup2"):
            self.assertEqual(sum(node == name for node, _ in fake.calls), 6)
        fake.calls.clear()
        self.assertEqual(self.run_guard(fake, state, warmup_candidates=2), 0)
        self.assertEqual({node for node, _ in fake.calls}, {"good"})

    def test_stale_backups_get_refreshed_without_switching_current(self):
        fake = Fake({"good": all_targets(True), "old": all_targets(True)},
                    current="good", secondary="good")
        state = {"health": {"old": {"verified_at": time.time() - 301}}}
        self.assertEqual(self.run_guard(fake, state, warmup_candidates=2), 0)
        self.assertGreater(state["health"]["old"]["verified_at"], time.time() - 10)
        self.assertEqual(fake.puts, [])

    def test_parallel_batches_are_bounded_and_stop_after_first_successful_batch(self):
        fake = Fake({"bad": all_targets(False), "candidate0": all_targets(False),
                     "candidate1": all_targets(True), "candidate2": all_targets(True),
                     "never-submitted": all_targets(True)}, probe_delay=0.025)
        self.assertEqual(self.run_guard(fake, candidate_workers=3), 0)
        self.assertGreaterEqual(fake.max_active_probes, 6)
        self.assertLessEqual(fake.max_active_probes, 9)
        self.assertFalse(any(node == "never-submitted" for node, _ in fake.calls))
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "candidate1"))

    def test_all_failed_cycle_preserves_current_and_reports_pool_coverage(self):
        fake = Fake({"bad": all_targets(False), "other": all_targets(False)})
        state = {}
        self.assertEqual(self.run_guard(fake, state), 1)
        self.assertEqual(state["last_result"], "pool_cycle_exhausted")
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.groups["Proxy-Auto"]["now"], "bad")

    def test_selector_deleted_current_can_recover_from_reject(self):
        fake = Fake({"replacement": all_targets(True)}, current="REJECT", secondary="REJECT")
        self.assertEqual(self.run_guard(fake), 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "replacement"))

    def test_secret_scalar_quotes_comments_and_yaml_escapes(self):
        cases = {
            '"abc!23" # comment': "abc!23",
            "'a''b#c' # comment": "a'b#c",
            "abc#def # comment": "abc#def",
            '"a\\\\b\\\"c"': 'a\\b"c',
            '"a\\x23b\\u0021"': "a#b!",
            "plain:secret": "plain:secret",
            "''": "",
        }
        for value, expected in cases.items():
            self.assertEqual(guard.parse_secret_scalar(value), expected)
        with self.assertRaises(guard.GuardError):
            guard.parse_secret_scalar('"not closed')

    def test_top_level_secret_and_atomic_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("nested:\n  secret: ignore\nsecret: 'a''b#c' # comment\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(guard.load_secret(config), "a'b#c")
            with mock.patch.dict(os.environ, {"SECRET": "environment-secret"}):
                self.assertEqual(guard.load_secret("/nonexistent"), "environment-secret")
            state = Path(directory) / "state.json"
            with guard.locked_state(state):
                guard.write_state(state, {"rotation": ["one"]})
                self.assertEqual(guard.read_state(state), {"rotation": ["one"]})
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(guard.GuardError):
                    with guard.locked_state(state):
                        pass


if __name__ == "__main__":
    unittest.main()
