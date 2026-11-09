#!/usr/bin/env python3
"""Measure bounded downloads through an isolated mihomo and select a faster healthy node.

The sidecar's Selector is the only group changed during measurements. Business
Selectors change only after an actually faster candidate passes two fresh health
rounds. Python's standard library and curl are sufficient on Debian 12.
"""

import argparse
import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
import time
import urllib.parse

from mihomo_health_guard import (
    API, APIError, BLOCKED_NAMES, BLOCKED_TYPES, Guard, GuardError,
    history_delay, load_secret, locked_state, log, read_state, write_state,
)

MIB = 1024 * 1024
DEFAULTS = {
    "version": 1,
    "provider": "Airport_01",
    "groups": ["Proxy-Auto", "Docker-Auto"],
    "sidecar_api": "http://127.0.0.1:19094",
    "sidecar_proxy": "http://127.0.0.1:17892",
    "sidecar_group": "Speed-Probe",
    "interval_seconds": 1800,
    "daily_budget_bytes": 1024 * MIB,
    "max_candidates": 3,
    "force_candidates": 12,
    "sample_bytes": 4 * MIB,
    "cooldown_seconds": 300,
    "gain_ratio": 1.25,
    "gain_mib_s": 0.5,
    "slow_mib_s": 0.5,
}


def loopback_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if (parsed.scheme not in ("http", "https") or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback and 1 <= parsed.port <= 65535
    except (ValueError, TypeError):
        return False


def load_settings(path):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
    except (OSError, ValueError, UnicodeError):
        raise GuardError("cannot read valid speed settings; check protected settings.json") from None
    config = dict(DEFAULTS)
    config.update(data)
    if config.get("version") != 1:
        raise GuardError("unsupported speed settings version")
    for key in ("provider", "sidecar_group", "generation"):
        if not isinstance(config.get(key), str) or not config[key] or len(config[key]) > 1024:
            raise GuardError("missing or invalid speed setting: " + key)
    if not isinstance(config.get("sidecar_secret"), str) or any(
            c in config["sidecar_secret"] for c in ("\0", "\n", "\r")):
        raise GuardError("missing or invalid protected sidecar_secret")
    if not all(loopback_url(config[key]) for key in ("sidecar_api", "sidecar_proxy")):
        raise GuardError("sidecar API and proxy must use explicit loopback IP URLs")
    groups = config.get("groups")
    if (not isinstance(groups, list) or not groups
            or any(not isinstance(g, str) or not g for g in groups)
            or len(set(groups)) != len(groups)):
        raise GuardError("speed groups must be a nonempty list of unique group names")
    limits = {"interval_seconds": (60, 86400), "daily_budget_bytes": (MIB, 8 * 1024 * MIB),
              "max_candidates": (1, 12), "force_candidates": (1, 12),
              "sample_bytes": (64 * 1024, 16 * MIB), "cooldown_seconds": (0, 86400)}
    for key, (low, high) in limits.items():
        value = config[key]
        if type(value) is not int or not low <= value <= high:
            raise GuardError("invalid numeric speed setting: " + key)
    for key, low, high in (("gain_ratio", 1, 10), ("gain_mib_s", 0, 100),
                           ("slow_mib_s", 0, 100)):
        value = config[key]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not low <= value <= high):
            raise GuardError("invalid numeric speed setting: " + key)
    if config["daily_budget_bytes"] < config["sample_bytes"]:
        raise GuardError("daily speed budget must fit at least one sample")
    return config


class CurlSpeedProbe:
    """One serial sample through the sidecar; no business selector writes."""
    def __init__(self, api, group, proxy, sample_bytes=4 * MIB, curl="/usr/bin/curl",
                 url=None, connect_timeout=3, total_timeout=8, runner=subprocess.run):
        self.api, self.group, self.proxy = api, group, proxy
        self.sample_bytes, self.curl = sample_bytes, curl
        self.url = url or ("https://speed.cloudflare.com/__down?bytes=" + str(sample_bytes)
                           + "&mihomo-speed-guard=v3")
        self.connect_timeout, self.total_timeout = connect_timeout, total_timeout
        self.runner = runner

    @staticmethod
    def _header_metadata(path):
        # Redirects are disabled. Ignore the proxy CONNECT header block and
        # inspect the final origin header block only.
        try:
            data = Path(path).read_bytes().decode("iso-8859-1")
        except OSError:
            return {}
        blocks = re.split(r"\r?\n\r?\n", data)
        candidates = [block for block in blocks if block.startswith("HTTP/")]
        if not candidates:
            return {}
        result = {}
        for line in candidates[-1].splitlines()[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.lower().strip()] = value.strip()
        return result

    def refresh_provider(self, provider):
        path = "/providers/proxies/" + urllib.parse.quote(provider, safe="")
        self.api.request("PUT", path)
        return self.api.provider(provider)

    def measure(self, node):
        started = time.monotonic()
        result = {"node": node, "ok": False, "partial": False, "bytes": 0,
                  "elapsed_s": 0, "mib_s": 0}
        group = self.api.group(self.group)
        if not isinstance(group, dict) or group.get("type") != "Selector":
            raise GuardError("isolated speed group must be Selector")
        if not isinstance(group.get("all"), list) or node not in group["all"]:
            result["error"] = "node missing from isolated speed group"
            return result
        self.api.fix(self.group, node)
        if self.api.group(self.group).get("now") != node:
            result["error"] = "isolated speed selection did not apply"
            return result
        with tempfile.TemporaryDirectory(prefix="mihomo-speed-") as scratch:
            body, headers = Path(scratch) / "body", Path(scratch) / "headers"
            command = [self.curl, "--disable", "--silent", "--show-error", "--proxy", self.proxy,
                       "--noproxy", "", "--connect-timeout", str(self.connect_timeout),
                       "--max-time", str(self.total_timeout), "--max-filesize", str(self.sample_bytes),
                       "--max-redirs", "0", "--header", "Accept-Encoding: identity",
                       "--dump-header", str(headers), "--output", str(body),
                       "--write-out", "%{json}", self.url]
            def child_limits():
                resource.setrlimit(resource.RLIMIT_FSIZE, (self.sample_bytes, self.sample_bytes))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            try:
                completed = self.runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=self.total_timeout + 3, check=False,
                                        preexec_fn=child_limits)
                code = completed.returncode
                try:
                    metadata = json.loads(completed.stdout)
                except (ValueError, UnicodeError):
                    metadata = {}
            except (OSError, subprocess.TimeoutExpired):
                code, metadata = -1, {}
            actual = body.stat().st_size if body.exists() else 0
            result.update(bytes=actual, curl_code=code,
                          elapsed_s=round(time.monotonic() - started, 6))
            origin = self._header_metadata(headers)
            try:
                status = int(metadata.get("http_code", 0))
                transferred = float(metadata.get("size_download", -1))
                elapsed = float(metadata.get("time_total", 0))
                ttfb = float(metadata.get("time_starttransfer", 0))
            except (ValueError, TypeError):
                status, transferred, elapsed, ttfb = 0, -1, 0, 0
            result["http"] = status
            content_length = origin.get("content-length")
            encoding = origin.get("content-encoding", "identity").lower()
            full = code == 0 and actual == self.sample_bytes
            partial = (code == 28 and 0 < actual < self.sample_bytes
                       and content_length == str(self.sample_bytes))
            valid = (status == 200 and encoding == "identity"
                     and transferred == actual and math.isfinite(elapsed) and elapsed > 0
                     and (full or partial))
            # A concurrent refresh cannot silently turn the sidecar group into
            # a different node while a download is in progress.
            if valid and self.api.group(self.group).get("now") != node:
                valid = False
                result["error"] = "isolated speed selection changed during download"
            if valid:
                result.update(ok=True, partial=partial, elapsed_s=elapsed,
                              mib_s=actual / elapsed / MIB,
                              ttfb_ms=max(0, ttfb * 1000))
            elif "error" not in result:
                result["error"] = "download response, size or completion did not validate"
        return result


class SpeedGuard:
    def __init__(self, api, probe, settings, state, health_state=None, force=False,
                 dry_run=False, persist=None, persist_health=None,
                 generation_reader=None, now=time.time):
        self.api, self.probe, self.config, self.state = api, probe, settings, state
        self.health_state = health_state if health_state is not None else {}
        self.force, self.dry_run = force, dry_run
        self.persist = persist or (lambda state: None)
        self.persist_health = persist_health or (lambda state: None)
        self.generation_reader = generation_reader or (lambda: self.config["generation"])
        self.now = now

    def initialize(self):
        if self.state.get("generation") != self.config["generation"]:
            # Invalidate measured scores, but keep exploration and scheduling.
            # A renamed/reordered export must not restart at the same nodes or
            # turn a ten-minute subscription refresh into ten-minute downloads.
            self.state.update(version=1, generation=self.config["generation"],
                              samples={})
            log("speed_generation_reset")
        if not isinstance(self.state.get("samples"), dict):
            self.state["samples"] = {}
        today = datetime.datetime.fromtimestamp(self.now()).astimezone().date().isoformat()
        budget = self.state.get("budget")
        if (not isinstance(budget, dict) or budget.get("day") != today
                or type(budget.get("used")) is not int or budget["used"] < 0):
            self.state["budget"] = {"day": today, "used": 0}

    def available(self):
        return self.config["daily_budget_bytes"] - self.state["budget"]["used"]

    def sample(self, node, baseline=False):
        size = self.config["sample_bytes"]
        if self.available() < size:
            log("speed_budget_exhausted", remaining_bytes=max(0, self.available()),
                daily_budget_bytes=self.config["daily_budget_bytes"])
            return None
        # Reserve before networking and checkpoint. If killed during a sample,
        # conservatively count its full cap instead of forgetting traffic.
        self.state["budget"]["used"] += size
        if baseline:
            # Record the current only when a budgeted baseline actually starts.
            # Merely observing a health-guard change cannot consume its retry.
            self.state["last_round_at"] = self.now()
            self.state["last_probed_current"] = node
        self.persist(self.state)
        try:
            result = self.probe.measure(node)
        except GuardError:
            log("speed_probe_unavailable", node=node)
            return None
        if not isinstance(result, dict):
            log("speed_probe_invalid", node=node)
            return None
        actual = result.get("bytes")
        if type(actual) is int and 0 <= actual <= size:
            self.state["budget"]["used"] += actual - size
        else:
            result.update(ok=False, error="invalid payload byte count")
        result["measured_at"] = self.now()
        result["generation"] = self.config["generation"]
        rate = result.get("mib_s")
        if (result.get("ok") is not True or type(rate) not in (int, float)
                or not math.isfinite(rate) or rate <= 0):
            result["ok"] = False
        self.state["samples"][node] = result
        tested = self.state.setdefault("cycle_tested", [])
        if node not in tested:
            tested.append(node)
        self.persist(self.state)
        log("speed_result", **result)
        return result

    def groups(self):
        found = {}
        for index, name in enumerate(self.config["groups"]):
            try:
                group = self.api.group(name)
            except APIError as exc:
                if index and exc.status == 404:
                    continue
                raise
            if (not isinstance(group, dict) or group.get("type") != "Selector"
                    or not isinstance(group.get("all"), list) or not group["all"]
                    or group["all"][0] != "REJECT" or group.get("emptyFallback") != "REJECT"):
                raise GuardError("business speed groups must be Selector with REJECT first and empty fallback")
            found[name] = group
        return found

    @staticmethod
    def provider_members(provider):
        if not isinstance(provider, dict) or not isinstance(provider.get("proxies"), list):
            raise GuardError("speed provider returned no valid node list")
        members = {}
        for node in provider["proxies"]:
            if not isinstance(node, dict) or not isinstance(node.get("name"), str):
                raise GuardError("speed provider returned invalid node metadata")
            if node["name"] in members:
                raise GuardError("speed provider returned duplicate node names")
            members[node["name"]] = str(node.get("type", "")).lower()
        return members

    def refreshed_provider(self):
        provider = self.config["provider"]
        # Both processes read the validated local file. Explicitly refresh both
        # before measuring, so the same display name cannot refer to an old
        # sidecar server and a newly published business server.
        path = "/providers/proxies/" + urllib.parse.quote(provider, safe="")
        self.api.request("PUT", path)
        main = self.api.provider(provider)
        isolated = self.probe.refresh_provider(provider)
        main_members = self.provider_members(main)
        isolated_members = self.provider_members(isolated)
        if main_members != isolated_members:
            raise GuardError("main and isolated provider node sets do not match after refresh")
        log("speed_provider_refreshed", nodes=len(main_members))
        return main

    @staticmethod
    def protocol(node):
        kind = str(node.get("type", "")).replace("-", "").lower() or "unknown"
        return {"ss": "shadowsocks", "ssr": "shadowsocksr", "hy2": "hysteria2"}.get(kind, kind)

    def candidates(self, nodes, current):
        previous = self.state.get("rotation")
        if not isinstance(previous, list):
            previous = []
        health = self.health_state.get("health", {})
        if not isinstance(health, dict):
            health = {}
        def priority(name):
            row = health.get(name, {})
            checked = row.get("verified_at", 0) if isinstance(row, dict) else 0
            fresh = (type(checked) in (int, float) and 0 <= self.now() - checked <= 300)
            return (not fresh, history_delay(nodes[name]), name)
        ordered = sorted((name for name in nodes if name != current), key=priority)
        rotation = []
        for name in previous + ordered:
            if isinstance(name, str) and name in nodes and name != current and name not in rotation:
                rotation.append(name)
        self.state["rotation"] = rotation
        tested = self.state.get("cycle_tested")
        if not isinstance(tested, list):
            tested = []
        tested = list(dict.fromkeys(name for name in tested if isinstance(name, str) and name in nodes))
        unseen = set(rotation) - set(tested)
        if not unseen and rotation:
            # Finish the full pool before a small protocol's only node takes
            # another slot. New entries join the unfinished cycle immediately.
            tested, unseen = [], set(rotation)
            log("speed_exploration_cycle_reset", candidates=len(rotation))
        self.state["cycle_tested"] = tested
        # Keep each protocol's original node queue, but interleave the queues.
        # A large pool of one protocol must not delay smaller protocol pools for
        # many rounds. Names containing claimed download speeds are not scores.
        queues = {}
        for name in rotation:
            if name in unseen:
                queues.setdefault(self.protocol(nodes[name]), []).append(name)
        available_protocols = {self.protocol(nodes[name]) for name in rotation}
        previous_protocols = self.state.get("protocol_rotation")
        if not isinstance(previous_protocols, list):
            previous_protocols = []
        protocols = []
        for kind in previous_protocols + sorted(available_protocols):
            if isinstance(kind, str) and kind in available_protocols and kind not in protocols:
                protocols.append(kind)
        self.state["protocol_rotation"] = list(protocols)
        protocols = [kind for kind in protocols if kind in queues]
        count = self.config["force_candidates"] if self.force else self.config["max_candidates"]
        selected = []
        while protocols and len(selected) < min(count, len(unseen)):
            kind = protocols.pop(0)
            protocols.append(kind)
            if queues[kind]:
                selected.append(queues[kind].pop(0))
        return selected

    def qualifies(self, candidate_rate, current_rate):
        if current_rate < self.config["slow_mib_s"]:
            return candidate_rate >= self.config["slow_mib_s"] and candidate_rate > current_rate
        return (candidate_rate >= current_rate * self.config["gain_ratio"]
                and candidate_rate - current_rate >= self.config["gain_mib_s"])

    def run(self):
        self.initialize()
        now = self.now()
        groups = self.groups()
        primary = self.config["groups"][0]
        current = groups[primary].get("now")
        last = self.state.get("last_round_at", 0)
        if (not self.force and self.state.get("last_probed_current") == current
                and type(last) in (int, float)
                and 0 <= now - last < self.config["interval_seconds"]):
            log("speed_not_due", next_in_seconds=round(self.config["interval_seconds"] - (now - last)))
            return 0
        try:
            provider = self.refreshed_provider()
        except GuardError:
            log("speed_refresh_failed", reason="main or isolated provider refresh failed or disagreed")
            return 1
        groups = self.groups()
        current = groups[primary].get("now")
        members = set.intersection(*(set(group["all"]) for group in groups.values()))
        if not isinstance(provider, dict) or not isinstance(provider.get("proxies"), list):
            raise GuardError("speed provider returned no valid node list")
        nodes = {}
        for node in provider["proxies"]:
            if not isinstance(node, dict):
                continue
            name = node.get("name")
            kind = str(node.get("type", "")).replace("-", "").lower()
            if (isinstance(name, str) and name in members and name.upper() not in BLOCKED_NAMES
                    and kind not in BLOCKED_TYPES):
                nodes[name] = node
        if current not in nodes:
            log("speed_current_unavailable", action="leave recovery to health guard")
            return 1
        self.state["samples"] = {name: row for name, row in self.state["samples"].items() if name in nodes}
        candidates = self.candidates(nodes, current)
        baseline = self.sample(current, baseline=True)
        if baseline is None:
            log("speed_baseline_failed", kept=current, reason="probe_or_budget_unavailable")
            return 1
        baseline_valid = baseline["ok"]
        if not baseline_valid:
            log("speed_baseline_failed", kept=current, reason="check_other_nodes_before_deciding")
        measured = {current: baseline}
        tested = []
        for name in candidates:
            sample = self.sample(name)
            if sample is None:
                break
            tested.append(name)
            if sample["ok"]:
                measured[name] = sample
        rotation = self.state["rotation"]
        protocols = self.state["protocol_rotation"]
        for name in tested:
            rotation.remove(name)
            rotation.append(name)
            kind = self.protocol(nodes[name])
            protocols.remove(kind)
            protocols.append(kind)
        self.persist(self.state)
        # Recheck the two highest observed results. Only this round's actual
        # samples are compared; old speed names and cached rates never qualify.
        ranked = sorted((name for name in measured if measured[name]["ok"]),
                        key=lambda name: measured[name]["mib_s"], reverse=True)
        scores = {name: row["mib_s"] for name, row in measured.items()}
        if not baseline_valid:
            scores[current] = 0  # Unknown baseline is never reported as a measured zero.
        confirmed_speed = set()
        for name in ranked[:2]:
            if name != current and not self.qualifies(scores[name], scores[current]):
                continue
            repeated = self.sample(name)
            if repeated and repeated["ok"]:
                scores[name] = min(scores[name], repeated["mib_s"])
                confirmed_speed.add(name)
            elif name != current:
                scores.pop(name, None)
        winners = [name for name in scores if name != current and name in confirmed_speed
                   and self.qualifies(scores[name], scores[current])]
        if not winners:
            log("speed_kept", node=current, mib_s=round(scores[current], 3) if baseline_valid else None,
                tested=len(tested), reason="no_confirmed_advantage",
                baseline_unavailable=not baseline_valid)
            return 0 if baseline_valid else 1
        winners.sort(key=lambda name: scores[name], reverse=True)
        winner = winners[0]
        switched = self.state.get("last_switch_at", 0)
        if (scores[current] >= self.config["slow_mib_s"]
                and type(switched) in (int, float)
                and 0 <= self.now() - switched < self.config["cooldown_seconds"]):
            log("speed_kept", node=current, candidate=winner, reason="minimum_dwell")
            return 0
        health = Guard(self.api, list(groups), self.config["provider"], self.health_state,
                       dry_run=self.dry_run, warmup_candidates=0)
        winner = None
        for candidate in winners:
            healthy = health.confirmed(candidate)
            self.persist_health(self.health_state)
            if healthy:
                winner = candidate
                break
            log("speed_candidate_unhealthy", node=candidate, kept=current)
        if winner is None:
            return 1
        live = self.groups()
        if (self.generation_reader() != self.config["generation"] or set(live) != set(groups)
                or any(live[name].get("now") != groups[name].get("now") for name in groups)
                or any(winner not in group["all"] for group in live.values())):
            log("speed_selection_changed", kept=current, reason="settings_or_selection_changed_during_probe")
            return 1
        health.align(live, winner, "speed_verified")
        if not self.dry_run and any(self.api.group(name).get("now") != winner for name in live):
            raise GuardError("speed winner selection did not apply to all managed groups")
        if not self.dry_run:
            self.state["last_switch_at"] = self.now()
            self.state["last_selected"] = winner
            # The winner was just downloaded twice. Do not immediately rerun
            # solely because our own successful selection changed the current.
            self.state["last_probed_current"] = winner
            self.persist_health(self.health_state)
        self.persist(self.state)
        log("speed_winner", node=winner, previous=current, tested=len(tested),
            current_mib_s=round(scores[current], 3) if baseline_valid else None,
            candidate_mib_s=round(scores[winner], 3), baseline_unavailable=not baseline_valid,
            dry_run=self.dry_run, scope="measured candidates this round")
        return 0


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", default="/var/lib/mihomo-speed-guard/settings.json")
    parser.add_argument("--api", default="http://127.0.0.1:9093")
    parser.add_argument("--config", default="/etc/mihomo/config.yaml")
    parser.add_argument("--state", default="/var/lib/mihomo-speed-guard/state.json")
    parser.add_argument("--main-state", default="/var/lib/mihomo-health-guard/state.json")
    parser.add_argument("--force", action="store_true", help="run now and scan up to force_candidates")
    parser.add_argument("--dry-run", action="store_true", help="measure but do not change business selectors")
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    path, main_path = Path(args.state), Path(args.main_state)
    if path.resolve() == main_path.resolve():
        log("error", message="speed and health state paths must be different")
        return 2
    try:
        settings = load_settings(args.settings)
        # Same lock as health v2: speed and ordinary recovery cannot race.
        with locked_state(main_path), locked_state(path):
            api = API(args.api, load_secret(args.config))
            sidecar = API(settings["sidecar_api"], settings["sidecar_secret"])
            probe = CurlSpeedProbe(sidecar, settings["sidecar_group"], settings["sidecar_proxy"],
                                   settings["sample_bytes"])
            state, health_state = read_state(path), read_state(main_path)
            try:
                return SpeedGuard(api, probe, settings, state, health_state, args.force, args.dry_run,
                                  persist=lambda state: write_state(path, state),
                                  persist_health=lambda state: write_state(main_path, state),
                                  generation_reader=lambda: load_settings(args.settings)["generation"]).run()
            finally:
                write_state(path, state)
    except (GuardError, OSError) as exc:
        log("error", message=str(exc) if isinstance(exc, GuardError)
            else "local speed guard file access failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
