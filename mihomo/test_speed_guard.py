#!/usr/bin/env python3
"""Offline v3 tests. Fake controller and local HTTP download server only."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import mihomo_speed_guard as speed
import mihomo_health_guard as health
from test_guard import Fake as HealthFake, fake_api, all_targets


class Fake(HealthFake):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider_refreshes = 0
        self.provider_refresh_failed = False
    def handler(self):
        original = super().handler()
        owner = self
        class Handler(original):
            def do_PUT(self):
                if self.path == "/providers/proxies/Airport_01":
                    if self.headers.get("Authorization") != "Bearer test-secret":
                        self.send(401)
                        return
                    owner.provider_refreshes += 1
                    self.send(500 if owner.provider_refresh_failed else 204)
                else:
                    super().do_PUT()
        return Handler


def settings(**updates):
    result = dict(speed.DEFAULTS)
    result.update(generation="source-A", sidecar_secret="private-test-secret")
    result.update(updates)
    return result


class Downloader:
    def __init__(self, rates, sample_bytes=4 * speed.MIB, types=None):
        self.rates = {name: list(value) if isinstance(value, list) else [value]
                      for name, value in rates.items()}
        self.sample_bytes, self.calls, self.active, self.max_active = sample_bytes, [], 0, 0
        self.refresh_calls = []
        self.provider_nodes = [{"name": name, "type": (types or {}).get(name, "Http")}
                               for name in rates]
    def refresh_provider(self, provider):
        self.refresh_calls.append(provider)
        return {"proxies": self.provider_nodes}
    def measure(self, node):
        self.calls.append(node)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        rows = self.rates.get(node, [None])
        value = rows.pop(0) if len(rows) > 1 else rows[0]
        self.active -= 1
        if value is None:
            return {"node": node, "ok": False, "bytes": 0, "mib_s": 0}
        if isinstance(value, dict):
            return dict(value, node=node)
        return {"node": node, "ok": True, "partial": False,
                "bytes": self.sample_bytes, "mib_s": value,
                "elapsed_s": self.sample_bytes / speed.MIB / value}


class SpeedPolicyTests(unittest.TestCase):
    def run_policy(self, rates, state=None, config=None, current="current", health_results=None,
                   force=False, dry_run=False, now=10000, generation_reader=None, types=None):
        fake = Fake({name: all_targets(True) for name in rates}, current=current,
                    secondary=current, types=types)
        if health_results:
            fake.health.update(health_results)
        config = config or settings()
        state = state if state is not None else {}
        downloader = Downloader(rates, config["sample_bytes"], types=types)
        health_state, persist_calls = {}, []
        original_guard = speed.Guard
        def guard_factory(*args, **kwargs):
            kwargs["retry_delay"] = 0
            return original_guard(*args, **kwargs)
        with fake_api(fake) as api, contextlib.redirect_stdout(io.StringIO()) as output:
            with mock.patch.object(speed, "Guard", side_effect=guard_factory):
                result = speed.SpeedGuard(api, downloader, config, state, health_state,
                    force=force, dry_run=dry_run, now=lambda: now,
                    persist=lambda row: persist_calls.append(json.loads(json.dumps(row))),
                    generation_reader=generation_reader).run()
        return result, fake, downloader, state, output.getvalue(), persist_calls

    def test_healthy_slow_current_is_replaced_by_measured_faster_candidate(self):
        result, fake, downloader, state, output, persisted = self.run_policy({"current": 1, "fast": [4, 3.5]})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts, [("Proxy-Auto", "fast"), ("Docker-Auto", "fast")])
        self.assertEqual(downloader.calls, ["current", "fast", "fast", "current"])
        self.assertEqual(downloader.max_active, 1)
        self.assertEqual(state["last_selected"], "fast")
        self.assertEqual(fake.provider_refreshes, 1)
        self.assertEqual(downloader.refresh_calls, ["Airport_01"])
        self.assertIn('"event": "speed_winner"', output)
        self.assertEqual(len(fake.calls), 6)
        self.assertGreaterEqual(persisted[1]["budget"]["used"], speed.DEFAULTS["sample_bytes"])

    def test_advertised_speed_and_low_delay_do_not_override_actual_bandwidth(self):
        result, fake, _, _, output, _ = self.run_policy({"current": 4, "Advertised 999MB/s": 1})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts, [])
        self.assertIn('"reason": "no_confirmed_advantage"', output)

    def test_small_gain_and_absolute_gain_threshold_do_not_switch(self):
        for baseline, candidate in ((4, 4.9), (1, 1.4)):
            with self.subTest(baseline=baseline):
                result, fake, *_ = self.run_policy({"current": baseline, "candidate": candidate})
                self.assertEqual(result, 0)
                self.assertEqual(fake.puts, [])

    def test_fast_candidate_must_repeat_download_advantage(self):
        result, fake, downloader, *_ = self.run_policy({"current": 2, "fast": [8, 2.1]})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts, [])
        self.assertEqual(downloader.calls.count("fast"), 2)

    def test_fast_candidate_failing_health_is_never_selected(self):
        bad = all_targets(True)
        bad[health.TARGETS[2][1]] = False
        result, fake, *_ = self.run_policy({"current": 1, "fast": 8}, health_results={"fast": bad})
        self.assertEqual(result, 1)
        self.assertEqual(fake.puts, [])

    def test_candidate_must_pass_second_health_round(self):
        flaky = all_targets(True)
        flaky[health.TARGETS[1][1]] = [True, False]
        result, fake, *_ = self.run_policy({"current": 1, "fast": 8}, health_results={"fast": flaky})
        self.assertEqual(result, 1)
        self.assertEqual(fake.puts, [])
        self.assertEqual(len(fake.calls), 6)

    def test_minimum_dwell_prevents_healthy_switch_but_very_slow_can_escape(self):
        result, fake, *_ = self.run_policy({"current": 1, "fast": 8},
                     state={"generation": "source-A", "last_switch_at": 9990})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts, [])
        result, fake, *_ = self.run_policy({"current": 0.1, "fast": 1},
                     state={"generation": "source-A", "last_switch_at": 9990})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "fast"))

    def test_cached_old_speed_does_not_qualify_for_switch(self):
        state = {"generation": "source-A", "samples": {"fast": {"ok": True, "mib_s": 999,
                 "measured_at": 9999}}, "rotation": ["fast"]}
        result, fake, *_ = self.run_policy({"current": 4, "fast": 1}, state=state)
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts, [])
        self.assertEqual(state["samples"]["fast"]["mib_s"], 1)

    def test_generation_change_only_invalidates_samples_without_forcing_due(self):
        state = {"generation": "old", "last_round_at": 9999,
                 "last_probed_current": "current", "last_switch_at": 9500,
                 "samples": {"removed": {"mib_s": 999}},
                 "rotation": ["slow", "removed"], "protocol_rotation": ["http"],
                 "cycle_tested": ["removed"]}
        result, fake, downloader, state, *_ = self.run_policy({"current": 4, "slow": 1}, state=state)
        self.assertEqual(result, 0)
        self.assertEqual(downloader.calls, [])
        self.assertEqual(fake.provider_refreshes, 0)
        self.assertEqual(state["generation"], "source-A")
        self.assertEqual(state["samples"], {})
        self.assertEqual(state["rotation"], ["slow", "removed"])
        self.assertEqual(state["protocol_rotation"], ["http"])
        self.assertEqual(state["cycle_tested"], ["removed"])
        self.assertEqual(state["last_round_at"], 9999)
        self.assertEqual(state["last_switch_at"], 9500)

    def test_generation_changes_keep_progress_and_reconcile_new_and_removed_nodes_when_due(self):
        rates = {"current": 10, "a": 1, "b": 1, "c": 1}
        _, _, first, state, *_ = self.run_policy(rates, config=settings(max_candidates=1))
        self.assertEqual(first.calls[1], "a")
        # A changed export removes a tested entry and adds a new one. Its new
        # generation cannot put already-tested b ahead of the pending c later.
        rates.pop("a")
        rates["new"] = 1
        _, _, second, state, *_ = self.run_policy(rates, state=state,
            config=settings(max_candidates=1, generation="source-B"), now=12000)
        self.assertEqual(second.calls[1], "b")
        self.assertNotIn("a", state["rotation"])
        self.assertNotIn("a", state["cycle_tested"])
        _, _, third, state, *_ = self.run_policy(rates, state=state,
            config=settings(max_candidates=1, generation="source-C"), now=14000)
        self.assertEqual(third.calls[1], "c")
        _, _, fourth, *_ = self.run_policy(rates, state=state,
            config=settings(max_candidates=1, generation="source-D"), now=16000)
        self.assertEqual(fourth.calls[1], "new")

    def test_not_due_skips_all_downloads(self):
        result, fake, downloader, *_ = self.run_policy({"current": 1, "fast": 8},
                     state={"generation": "source-A", "last_round_at": 9999,
                            "last_probed_current": "current"})
        self.assertEqual(result, 0)
        self.assertEqual(downloader.calls, [])
        self.assertEqual(downloader.refresh_calls, [])
        self.assertEqual(fake.provider_refreshes, 0)
        self.assertEqual(fake.puts, [])

    def test_health_selected_new_current_is_probed_early_using_its_fresh_baseline(self):
        state = {"generation": "source-A", "last_round_at": 9999,
                 "last_probed_current": "old", "last_selected": "old",
                 "samples": {"old": {"ok": True, "mib_s": 99, "measured_at": 9999}}}
        result, fake, downloader, state, *_ = self.run_policy(
            {"current": 0.1, "fast": 1}, state=state)
        self.assertEqual(result, 0)
        self.assertEqual(downloader.calls[0], "current")
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "fast"))
        self.assertEqual(state["last_round_at"], 10000)
        self.assertEqual(state["last_probed_current"], "fast")
        _, _, following, *_ = self.run_policy({"current": 0.1, "fast": 1},
            current="fast", state=state, now=10001)
        self.assertEqual(following.calls, [])

    def test_new_current_baseline_marker_is_checkpointed_before_download_starts(self):
        state, config = {"generation": "source-A", "last_probed_current": "old"}, settings()
        probe, snapshots = mock.Mock(), []
        def measure(node):
            self.assertEqual(snapshots[-1]["last_probed_current"], node)
            self.assertEqual(snapshots[-1]["last_round_at"], 10000)
            return {"node": node, "ok": False, "bytes": 0, "mib_s": 0}
        probe.measure.side_effect = measure
        with contextlib.redirect_stdout(io.StringIO()):
            guard = speed.SpeedGuard(None, probe, config, state, now=lambda: 10000,
                                     persist=lambda row: snapshots.append(json.loads(json.dumps(row))))
            guard.initialize()
            guard.sample("new", baseline=True)
        self.assertEqual(state["last_probed_current"], "new")

    def test_no_budget_does_not_record_an_unstarted_new_current_baseline(self):
        day = speed.datetime.datetime.fromtimestamp(10000).astimezone().date().isoformat()
        state = {"generation": "source-A", "last_round_at": 9999,
                 "last_probed_current": "old", "budget": {"day": day, "used": 8 * speed.MIB}}
        result, fake, downloader, state, *_ = self.run_policy(
            {"current": 4, "candidate": 1}, state=state,
            config=settings(daily_budget_bytes=8 * speed.MIB))
        self.assertEqual(result, 1)
        self.assertEqual(downloader.calls, [])
        self.assertEqual(state["last_round_at"], 9999)
        self.assertEqual(state["last_probed_current"], "old")
        self.assertEqual(state["cycle_tested"], [])
        self.assertEqual(fake.puts, [])

    def test_force_is_bounded_and_rotation_covers_new_candidates(self):
        config, state = settings(max_candidates=1, force_candidates=2), {}
        rates = {"current": 4, "a": 1, "b": 1, "c": 1}
        _, _, downloader, state, *_ = self.run_policy(rates, state=state, config=config, force=True)
        self.assertEqual(downloader.calls[:3], ["current", "a", "b"])
        _, _, downloader2, *_ = self.run_policy(rates, state=state, config=config, now=12000)
        self.assertEqual(downloader2.calls[1], "c")

    def test_normal_rounds_rotate_across_protocols_before_repeating_them(self):
        types = {"current": "Http"}
        for kind in ("Http", "Shadowsocks", "Vmess", "Vless", "Trojan", "Hysteria2", "AnyTLS", "Tuic"):
            types[kind + " one"] = kind
            types[kind + " two"] = kind
        rates = {name: 10 if name == "current" else 1 for name in types}
        state, observed = {}, []
        for round_number in range(3):
            _, fake, downloader, state, *_ = self.run_policy(
                rates, types=types, state=state, now=10000 + round_number * 2000)
            candidates = downloader.calls[1:4]
            protocols = [types[name] for name in candidates]
            self.assertEqual(len(set(protocols)), 3)
            self.assertEqual(fake.puts, [])
            observed.extend(protocols)
        self.assertEqual(len(set(observed[:8])), 8)
        self.assertEqual(set(observed[:8]), set(types.values()))
        self.assertEqual(len(state["protocol_rotation"]), 8)

    def test_force_round_includes_small_low_advertised_protocol_pools(self):
        types = {"current": "Http"}
        for index in range(20):
            types["free 999MB/s " + str(index)] = "Http"
        for kind in ("Shadowsocks", "Vmess", "Vless", "Trojan", "Hysteria2", "AnyTLS", "Tuic"):
            types["private " + kind] = kind
        rates = {name: 10 if name == "current" else 1 for name in types}
        _, _, downloader, state, *_ = self.run_policy(rates, types=types, force=True)
        candidates = downloader.calls[1:13]
        self.assertEqual(len(candidates), 12)
        self.assertEqual(len(set(candidates)), 12)
        self.assertEqual(set(types[name] for name in candidates[:8]), set(types.values()))
        self.assertIn("private Hysteria2", candidates)
        self.assertIn("private AnyTLS", candidates)
        self.assertEqual(len(state["rotation"]), len(rates) - 1)

    def test_legacy_node_rotation_survives_protocol_interleaving(self):
        types = {"current": "Http", "http-a": "Http", "http-b": "Http",
                 "ss-a": "Shadowsocks", "ss-b": "Shadowsocks", "private": "Hysteria2"}
        rates = {name: 10 if name == "current" else 1 for name in types}
        state = {"generation": "source-A", "rotation": ["http-b", "http-a", "ss-b", "ss-a", "private"]}
        observed = []
        for round_number in range(3):
            _, _, downloader, state, *_ = self.run_policy(
                rates, types=types, state=state, config=settings(max_candidates=2),
                now=10000 + round_number * 2000)
            observed.extend(name for name in downloader.calls[1:] if name != "current")
        self.assertEqual(set(observed), set(rates) - {"current"})
        self.assertLess(observed.index("http-b"), observed.index("http-a"))
        self.assertLess(observed.index("ss-b"), observed.index("ss-a"))

    def test_small_protocol_is_not_repeated_until_untested_pool_is_covered(self):
        types = {"current": "Http", "one-off": "AnyTLS"}
        types.update({"ss-" + str(i): "Shadowsocks" for i in range(7)})
        rates = {name: 10 if name == "current" else 1 for name in types}
        state, observed = {}, []
        for round_number in range(3):
            _, _, downloader, state, *_ = self.run_policy(rates, types=types, state=state,
                now=10000 + round_number * 2000)
            observed.extend(name for name in downloader.calls[1:] if name != "current")
        self.assertEqual(len(observed), 8)
        self.assertEqual(len(set(observed)), 8)
        self.assertEqual(set(observed), set(rates) - {"current"})
        self.assertEqual(observed.count("one-off"), 1)
        # Full coverage starts another cycle on the next scheduled round.
        _, _, downloader, state, output, *_ = self.run_policy(
            rates, types=types, state=state, now=16000)
        self.assertIn("one-off", downloader.calls[1:4])
        self.assertIn('"event": "speed_exploration_cycle_reset"', output)

    def test_failed_candidate_counts_as_explored_but_unstarted_candidate_does_not(self):
        rates = {"current": 10, "a-bad": None, "b-pending": 1}
        day = speed.datetime.datetime.fromtimestamp(10000).astimezone().date().isoformat()
        state = {"generation": "source-A", "budget": {"day": day, "used": 0}}
        # One full baseline and two payload-free failures still fit. An actual
        # failed response is explored rather than repeatedly taking this slot.
        _, _, first, state, *_ = self.run_policy(rates, state=state, config=settings(max_candidates=1))
        self.assertEqual(first.calls[1], "a-bad")
        self.assertIn("a-bad", state["cycle_tested"])
        _, _, second, *_ = self.run_policy(rates, state=state,
            config=settings(max_candidates=1), now=12000)
        self.assertEqual(second.calls[1], "b-pending")

    def test_advertised_name_speed_does_not_prioritize_new_candidates(self):
        nodes = {name: {"type": "Http", "history": [{"delay": 10}]}
                 for name in ("current", "a ordinary", "z 999MB/s")}
        state = {}
        guard = speed.SpeedGuard(None, None, settings(max_candidates=1), state)
        self.assertEqual(guard.candidates(nodes, "current"), ["a ordinary"])

    def test_budget_advances_only_protocols_that_were_actually_tested(self):
        types = {"current": "Http", "a": "Http", "b": "Hysteria2", "c": "Shadowsocks"}
        rates = {name: 10 if name == "current" else 1 for name in types}
        _, _, downloader, state, *_ = self.run_policy(
            rates, types=types, config=settings(daily_budget_bytes=8 * speed.MIB))
        self.assertEqual(downloader.calls, ["current", "a"])
        self.assertEqual(state["protocol_rotation"], ["hysteria2", "shadowsocks", "http"])
        self.assertEqual(set(state["cycle_tested"]), {"current", "a"})

    def test_daily_payload_budget_bounds_downloads_and_cannot_skip_confirmation(self):
        result, fake, downloader, state, output, _ = self.run_policy(
            {"current": 1, "fast": 8}, config=settings(daily_budget_bytes=8 * speed.MIB))
        self.assertEqual(result, 0)
        self.assertEqual(downloader.calls, ["current", "fast"])
        self.assertEqual(state["budget"]["used"], 8 * speed.MIB)
        self.assertEqual(fake.puts, [])
        self.assertIn('"event": "speed_budget_exhausted"', output)

    def test_failed_body_bytes_accounted_and_partial_speed_can_be_used(self):
        failure = {"ok": False, "bytes": speed.MIB, "mib_s": 0}
        result, fake, _, state, *_ = self.run_policy({"current": 1, "bad": failure})
        self.assertEqual(result, 0)
        self.assertEqual(state["budget"]["used"], 9 * speed.MIB)
        partial = {"ok": True, "partial": True, "bytes": speed.MIB, "mib_s": 0.125}
        result, fake, *_ = self.run_policy({"current": partial, "fast": 1})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "fast"))

    def test_bad_test_target_keeps_current_when_every_node_fails(self):
        result, fake, downloader, _, output, _ = self.run_policy({"current": None, "candidate": None})
        self.assertEqual(result, 1)
        self.assertEqual(downloader.calls, ["current", "candidate"])
        self.assertEqual(fake.puts, [])
        self.assertIn('"event": "speed_baseline_failed"', output)

    def test_unavailable_current_download_can_escape_to_twice_confirmed_candidate(self):
        result, fake, downloader, _, output, _ = self.run_policy({"current": None, "fast": 8})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "fast"))
        self.assertEqual(downloader.calls, ["current", "fast", "fast"])
        self.assertIn('"baseline_unavailable": true', output)

    def test_second_fastest_is_tried_when_fastest_fails_health(self):
        bad = all_targets(False)
        result, fake, *_ = self.run_policy({"current": 1, "first": 8, "second": 4},
                                          health_results={"first": bad})
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts[-1], ("Docker-Auto", "second"))

    def test_dry_run_measures_and_health_checks_without_business_selection(self):
        result, fake, _, _, output, _ = self.run_policy({"current": 1, "fast": 8}, dry_run=True)
        self.assertEqual(result, 0)
        self.assertEqual(fake.puts, [])
        self.assertEqual(len(fake.calls), 6)
        self.assertIn('"event": "would_select"', output)

    def test_provider_refresh_before_selection_cancels_switch(self):
        result, fake, *_ = self.run_policy({"current": 1, "fast": 8}, generation_reader=lambda: "source-B")
        self.assertEqual(result, 1)
        self.assertEqual(fake.puts, [])

    def test_business_selection_change_during_probing_cancels_speed_switch(self):
        fake = Fake({name: all_targets(True) for name in ("current", "fast", "external")},
                    current="current", secondary="current")
        downloader = Downloader({"current": 1, "fast": 8, "external": 1})
        original_measure = downloader.measure
        def measure_then_external_selection(node):
            result = original_measure(node)
            if node == "fast":
                fake.groups["Proxy-Auto"]["now"] = "external"
            return result
        downloader.measure = measure_then_external_selection
        with fake_api(fake) as api, contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(health.Guard, "pause"):
                result = speed.SpeedGuard(api, downloader, settings(), {}, now=lambda: 10000).run()
        self.assertEqual(result, 1)
        self.assertEqual(fake.puts, [])

    def test_unsafe_business_group_is_refused_before_downloading(self):
        fake = Fake({"current": all_targets(True)}, current="current", secondary="current")
        fake.groups["Proxy-Auto"]["emptyFallback"] = "DIRECT"
        downloader = Downloader({"current": 1})
        with fake_api(fake) as api, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(health.GuardError):
                speed.SpeedGuard(api, downloader, settings(), {}, now=lambda: 10000).run()
        self.assertEqual(downloader.calls, [])

    def test_provider_set_disagreement_blocks_all_downloads_and_selection(self):
        fake = Fake({"current": all_targets(True), "fast": all_targets(True)},
                    current="current", secondary="current")
        downloader = Downloader({"current": 1, "fast": 8})
        downloader.provider_nodes.pop()
        with fake_api(fake) as api, contextlib.redirect_stdout(io.StringIO()) as output:
            result = speed.SpeedGuard(api, downloader, settings(), {}, now=lambda: 10000).run()
        self.assertEqual(result, 1)
        self.assertEqual(downloader.calls, [])
        self.assertEqual(fake.puts, [])
        self.assertIn('"event": "speed_refresh_failed"', output.getvalue())

    def test_failed_local_provider_refresh_keeps_current_and_does_not_download(self):
        fake = Fake({"current": all_targets(True), "fast": all_targets(True)},
                    current="current", secondary="current")
        fake.provider_refresh_failed = True
        downloader = Downloader({"current": 1, "fast": 8})
        with fake_api(fake) as api, contextlib.redirect_stdout(io.StringIO()):
            result = speed.SpeedGuard(api, downloader, settings(), {}, now=lambda: 10000).run()
        self.assertEqual(result, 1)
        self.assertEqual(downloader.calls, [])
        self.assertEqual(downloader.refresh_calls, [])
        self.assertEqual(fake.puts, [])

    def test_probe_error_reserves_traffic_conservatively_without_switch(self):
        state, config, probe = {}, settings(), mock.Mock()
        probe.measure.side_effect = health.GuardError("sidecar failed")
        with contextlib.redirect_stdout(io.StringIO()):
            guard = speed.SpeedGuard(mock.Mock(), probe, config, state, now=lambda: 10000)
            guard.initialize()
            self.assertIsNone(guard.sample("node"))
        self.assertEqual(state["budget"]["used"], config["sample_bytes"])


class Sidecar:
    def __init__(self):
        self.now, self.puts, self.changed_after, self.reads = "old", [], False, 0
    def group(self, name):
        self.reads += 1
        return {"type": "Selector", "all": ["node", "old"],
                "now": "old" if self.changed_after and self.reads >= 3 else self.now}
    def fix(self, group, node):
        self.puts.append((group, node))
        self.now = node


class CurlProbeTests(unittest.TestCase):
    def fake_runner(self, size=4 * speed.MIB, code=0, status=200, length=None, encoding="identity"):
        def run(command, **kwargs):
            body = Path(command[command.index("--output") + 1])
            headers = Path(command[command.index("--dump-header") + 1])
            body.write_bytes(b"a" * size)
            headers.write_text("HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 " + str(status)
                + "\r\nContent-Length: " + str(4 * speed.MIB if length is None else length)
                + "\r\nContent-Encoding: " + encoding + "\r\n\r\n")
            self.command, self.kwargs = command, kwargs
            return subprocess.CompletedProcess(command, code, json.dumps({"http_code": status,
                "size_download": size, "time_total": 2, "time_starttransfer": 0.2}).encode(), b"")
        return run

    def test_measures_real_body_and_only_writes_sidecar_group(self):
        api = Sidecar()
        result = speed.CurlSpeedProbe(api, "Speed-Probe", "http://127.0.0.1:17892",
                                     runner=self.fake_runner()).measure("node")
        self.assertTrue(result["ok"])
        self.assertEqual(result["mib_s"], 2)
        self.assertEqual(api.puts, [("Speed-Probe", "node")])
        self.assertEqual(self.command[self.command.index("--noproxy") + 1], "")
        self.assertEqual(self.command[1], "--disable")
        self.assertIn("preexec_fn", self.kwargs)

    def test_timeout_partial_requires_expected_status_declared_length_and_encoding(self):
        for status, length, encoding, expected in ((200, None, "identity", True),
             (403, None, "identity", False), (200, 123, "identity", False), (200, None, "gzip", False)):
            with self.subTest(status=status, length=length, encoding=encoding):
                result = speed.CurlSpeedProbe(Sidecar(), "Speed-Probe", "http://127.0.0.1:17892",
                    runner=self.fake_runner(size=speed.MIB, code=28, status=status,
                                            length=length, encoding=encoding)).measure("node")
                self.assertEqual(result["ok"], expected)
                if expected:
                    self.assertTrue(result["partial"])

    def test_wrong_status_incomplete_non_timeout_and_oversize_are_not_fast(self):
        for size, code, status in ((4 * speed.MIB, 0, 403), (speed.MIB, 56, 200), (5 * speed.MIB, 0, 200)):
            with self.subTest(size=size, code=code, status=status):
                result = speed.CurlSpeedProbe(Sidecar(), "Speed-Probe", "http://127.0.0.1:17892",
                    runner=self.fake_runner(size=size, code=code, status=status)).measure("node")
                self.assertFalse(result["ok"])
                self.assertEqual(result["bytes"], size)

    def test_sidecar_selection_change_invalidates_sample(self):
        api = Sidecar()
        api.changed_after = True
        result = speed.CurlSpeedProbe(api, "Speed-Probe", "http://127.0.0.1:17892",
                                     runner=self.fake_runner()).measure("node")
        self.assertFalse(result["ok"])

    @unittest.skipUnless(shutil.which("curl"), "curl unavailable")
    def test_actual_curl_local_proxy_download_and_hard_output_limit(self):
        size = 64 * 1024
        class Handler(BaseHTTPRequestHandler):
            oversized = False
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                if not self.oversized:
                    self.send_header("Content-Length", str(size))
                self.end_headers()
                try:
                    self.wfile.write(b"x" * (size * 4 if self.oversized else size))
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            probe = speed.CurlSpeedProbe(Sidecar(), "Speed-Probe", "http://127.0.0.1:" + str(server.server_port),
                sample_bytes=size, curl=shutil.which("curl"), url="http://example.invalid/test")
            result = probe.measure("node")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["bytes"], size)
            Handler.oversized = True
            result = probe.measure("node")
            self.assertFalse(result["ok"])
            self.assertLessEqual(result["bytes"], size)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_rejects_remote_sidecar_secrets_and_bad_numbers(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "settings.json"
            path.write_text(json.dumps(settings()))
            result = speed.load_settings(path)
            self.assertEqual(result["interval_seconds"], 1800)
            self.assertEqual(result["daily_budget_bytes"], 1024 * speed.MIB)
            for key, value in (("sidecar_api", "http://192.168.2.2:19094"),
                ("sidecar_proxy", "http://user:secret@127.0.0.1:17892"),
                ("sidecar_secret", "line\nsecret"), ("sidecar_api", "http://[bad"),
                ("gain_ratio", float("nan")),
                ("max_candidates", True), ("generation", "")):
                with self.subTest(key=key):
                    path.write_text(json.dumps(settings(**{key: value})))
                    with self.assertRaises(health.GuardError):
                        speed.load_settings(path)

    def test_budget_resets_next_day_and_generation_change_keeps_usage(self):
        day = speed.datetime.datetime.fromtimestamp(10000).astimezone().date().isoformat()
        state = {"generation": "source-A", "budget": {"day": day, "used": 123}}
        with contextlib.redirect_stdout(io.StringIO()):
            guard = speed.SpeedGuard(None, None, settings(generation="source-B"), state, now=lambda: 10000)
            guard.initialize()
            self.assertEqual(state["budget"]["used"], 123)
            guard.now = lambda: 200000
            guard.initialize()
            self.assertEqual(state["budget"]["used"], 0)


if __name__ == "__main__":
    unittest.main()
