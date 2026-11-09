#!/usr/bin/env python3
"""Keep mihomo Selector groups on a node that passes real HTTPS targets.

One invocation performs one bounded round. Only Python's standard library is used.
The provider healthcheck API changes delay history even with --dry-run; --dry-run
never sends a group PUT. No real target is contacted directly by this process.
"""

import argparse
import concurrent.futures
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


TARGETS = (
    ("google", "https://www.google.com/generate_204", 204),
    ("github", "https://github.com/", 200),
    ("docker", "https://registry-1.docker.io/v2/", 401),
)
BLOCKED_TYPES = {"direct", "reject", "rejectdrop", "pass", "compatible",
                 "selector", "urltest", "fallback", "loadbalance", "relay"}
BLOCKED_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
BACKUP_MAX_AGE = 300
PROBE_QUERY = "mihomo-health-guard=v2"


class GuardError(Exception):
    pass


class APIError(GuardError):
    def __init__(self, status=None):
        self.status = status
        super().__init__("API HTTP " + str(status) if status else "API connection failed")


def log(event, **fields):
    row = {"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "event": event}
    row.update(fields)
    print(json.dumps(row, ensure_ascii=False), flush=True)


def _tail_is_comment(tail):
    tail = tail.strip()
    return not tail or tail.startswith("#")


def parse_secret_scalar(value):
    """Decode a normal, single-line YAML scalar without loading YAML as code.

    Supports plain scalars, YAML single quote doubling, double quote escapes and
    a trailing YAML comment. Block scalars, anchors and tags are rejected.
    """
    value = value.strip()
    if not value or value.startswith("#"):
        return ""
    if value[0] == "'":
        out, index = [], 1
        while index < len(value):
            if value[index] != "'":
                out.append(value[index])
                index += 1
            elif index + 1 < len(value) and value[index + 1] == "'":
                out.append("'")
                index += 2
            else:
                if not _tail_is_comment(value[index + 1:]):
                    raise GuardError("secret has unsupported trailing YAML content")
                return "".join(out)
        raise GuardError("secret has an unclosed single quote")
    if value[0] == '"':
        escapes = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n",
                   "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ",
                   '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
                   "L": "\u2028", "P": "\u2029"}
        out, index = [], 1
        while index < len(value):
            char = value[index]
            if char == '"':
                if not _tail_is_comment(value[index + 1:]):
                    raise GuardError("secret has unsupported trailing YAML content")
                return "".join(out)
            if char != "\\":
                out.append(char)
                index += 1
                continue
            index += 1
            if index >= len(value):
                break
            escaped = value[index]
            if escaped in escapes:
                out.append(escapes[escaped])
                index += 1
            elif escaped in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[escaped]
                digits = value[index + 1:index + 1 + width]
                if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise GuardError("secret has an invalid YAML hexadecimal escape")
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    raise GuardError("secret has an invalid YAML Unicode escape") from None
                index += width + 1
            else:
                raise GuardError("secret has an unsupported YAML escape")
        raise GuardError("secret has an unclosed double quote")
    if value[0] in "!&*|>{[":
        raise GuardError("secret uses complex YAML; supply SECRET through a protected environment file")
    # In a plain scalar, # starts a comment only after whitespace.
    value = re.split(r"[ \t]+#", value, maxsplit=1)[0].rstrip()
    if value in ("~", "null", "Null", "NULL"):
        return ""
    return value


def load_secret(config):
    if "SECRET" in os.environ:
        secret = os.environ["SECRET"]
    else:
        try:
            content = Path(config).read_text(encoding="utf-8-sig")
        except OSError:
            raise GuardError("cannot read mihomo config; check --config and file permissions") from None
        matches = re.findall(r"^secret[ \t]*:[ \t]*(.*)$", content, flags=re.MULTILINE)
        if len(matches) != 1:
            raise GuardError("expected one top-level secret; alternatively set SECRET securely")
        secret = parse_secret_scalar(matches[0])
    if any(char in secret for char in ("\r", "\n", "\0")):
        raise GuardError("secret cannot contain a newline or NUL")
    return secret


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base, secret, timeout_ms=4000):
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise GuardError("invalid --api URL; credentials must come from SECRET/config")
        self.base = base.rstrip("/")
        self.timeout_ms = timeout_ms
        self.headers = {"Accept": "application/json"}
        if secret:
            self.headers["Authorization"] = "Bearer " + secret
        # Never inherit HTTP_PROXY/HTTPS_PROXY or follow an auth-bearing redirect.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        headers = dict(self.headers)
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout_ms / 1000 + 3) as response:
                raw = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise APIError(status) from None
        except (OSError, urllib.error.URLError, ValueError):
            raise APIError() from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise GuardError("API returned invalid JSON") from None

    def group(self, name):
        return self.request("GET", "/proxies/" + urllib.parse.quote(name, safe=""))

    def provider(self, name):
        return self.request("GET", "/providers/proxies/" + urllib.parse.quote(name, safe=""))

    def probe(self, provider, node, target):
        label, url, expected = target
        # A fixed private URL keeps the guard's status history separate from
        # provider background checks. Never use a random URL: mihomo retains an
        # extra history map entry for every distinct tested URL.
        url += ("&" if "?" in url else "?") + PROBE_QUERY
        query = urllib.parse.urlencode({"url": url, "timeout": self.timeout_ms,
                                       "expected": expected})
        node_path = ("/providers/proxies/" + urllib.parse.quote(provider, safe="") + "/"
                     + urllib.parse.quote(node, safe=""))
        try:
            before = self._probe_history(self.request("GET", node_path), url)
            result = self.request("GET", node_path + "/healthcheck?" + query)
            delay = result.get("delay")
            after = self._probe_history(self.request("GET", node_path), url)
            # At least the 478af2a core can return a positive API delay when the
            # HTTP status mismatches `expected`; only extra[url] records that
            # failure reliably. Require a new successful history entry as well
            # so a previous success cannot validate the current request.
            if (isinstance(delay, int) and not isinstance(delay, bool) and delay >= 0
                    and after is not None and after["alive"] is True
                    and after["delay"] > 0
                    and (before is None or after["time"] != before["time"])):
                return {"target": label, "ok": True, "delay_ms": delay}
            return {"target": label, "ok": False,
                    "error": "missing fresh successful status history"}
        except APIError as exc:
            return {"target": label, "ok": False,
                    "error": "HTTP " + str(exc.status) if exc.status else "API connection failed"}
        except GuardError:
            return {"target": label, "ok": False, "error": "invalid API response"}

    @staticmethod
    def _probe_history(node, url):
        if not isinstance(node, dict):
            return None
        extra = node.get("extra")
        row = extra.get(url) if isinstance(extra, dict) else None
        history = row.get("history") if isinstance(row, dict) else None
        entry = history[-1] if isinstance(history, list) and history else None
        if not isinstance(entry, dict):
            return None
        stamp, delay = entry.get("time"), entry.get("delay")
        if (not isinstance(stamp, str) or not isinstance(delay, int)
                or isinstance(delay, bool)):
            return None
        try:
            parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return {"time": stamp, "delay": delay, "alive": row.get("alive")}

    def fix(self, group, node):
        return self.request("PUT", "/proxies/" + urllib.parse.quote(group, safe=""), {"name": node})


def history_delay(node):
    for entry in reversed(node.get("history") or []):
        delay = entry.get("delay") if isinstance(entry, dict) else None
        if isinstance(delay, (int, float)) and delay > 0:
            return delay
    return float("inf")


class Guard:
    def __init__(self, api, groups, provider, state, dry_run=False,
                 max_candidates=12, retry_delay=1.0, candidate_workers=3,
                 warmup_candidates=2):
        self.api = api
        self.group_names = groups
        self.provider_name = provider
        self.state = state
        self.dry_run = dry_run
        self.max_candidates = max_candidates
        self.retry_delay = retry_delay
        self.candidate_workers = candidate_workers
        self.warmup_candidates = warmup_candidates
        self.state_lock = threading.Lock()
        if not isinstance(self.state.get("health"), dict):
            self.state["health"] = {}

    def test_round(self, node):
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(TARGETS)) as executor:
            results = list(executor.map(
                lambda target: self.api.probe(self.provider_name, node, target), TARGETS))
        passed = all(result["ok"] for result in results)
        log("probe", node=node, passed=passed, results=results)
        return passed

    def pause(self):
        if self.retry_delay:
            time.sleep(self.retry_delay)

    def record(self, node, healthy):
        with self.state_lock:
            health = self.state["health"]
            row = health.get(node)
            if not isinstance(row, dict):
                row = {}
                health[node] = row
            now = time.time()
            row["checked_at"] = now
            if healthy:
                row["verified_at"] = now
                row["last_success_at"] = now
                row["failures"] = 0
            else:
                row["verified_at"] = 0
                row["last_failure_at"] = now
                failures = row.get("failures", 0)
                row["failures"] = (failures if isinstance(failures, int) else 0) + 1

    def verified_at(self, node):
        row = self.state["health"].get(node, {})
        if not isinstance(row, dict):
            return 0
        value = row.get("verified_at", 0)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    def confirmed(self, node):
        passed = self.test_round(node)
        if passed:
            self.pause()
            passed = self.test_round(node)
        self.record(node, passed)
        return passed

    def batch(self, names):
        # Only this batch is submitted. At most candidate_workers * 3 target
        # probes run concurrently; the rest of the pool is never pre-submitted.
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.candidate_workers) as executor:
            return list(executor.map(self.confirmed, names))

    def align(self, groups, node, reason):
        changed = []
        for name, group in groups.items():
            if group.get("now") == node:
                continue
            if self.dry_run:
                log("would_select", group=name, node=node, reason=reason)
            else:
                self.api.fix(name, node)
                log("selected", group=name, node=node, reason=reason)
            changed.append(name)
        self.state.update(last_node=node, last_result="dry_run" if self.dry_run else reason,
                          checked_at=datetime.datetime.now().astimezone().isoformat(),
                          failed_cycle_nodes=[])
        log("round_complete", node=node, result=reason, changed_groups=changed,
            dry_run=self.dry_run)

    def fail_closed(self, groups, reason):
        # A stable first REJECT is also required in YAML: if the provider drops a
        # selected name, mihomo Selector falls back to its first available proxy.
        for name, group in groups.items():
            if "REJECT" not in group["all"] or group.get("now") == "REJECT":
                continue
            if self.dry_run:
                log("would_select_reject", group=name, reason=reason)
            else:
                self.api.fix(name, "REJECT")
                log("selected_reject", group=name, reason=reason)

    def rotation(self, nodes, current):
        ordered = sorted((name for name in nodes if name != current),
                         key=lambda name: (history_delay(nodes[name]), name))
        previous = self.state.get("rotation", [])
        if not isinstance(previous, list):
            previous = []
        rotation = []
        for name in previous + ordered:
            if isinstance(name, str) and name in nodes and name != current and name not in rotation:
                rotation.append(name)
        self.state["rotation"] = rotation
        return rotation

    @staticmethod
    def rotate_used(rotation, names):
        for name in names:
            if name in rotation:
                rotation.remove(name)
                rotation.append(name)

    def warm_backups(self, nodes, current, rotation):
        fresh = {name for name in nodes if name != current
                 and 0 <= time.time() - self.verified_at(name) <= BACKUP_MAX_AGE}
        budget = min(self.warmup_candidates, max(0, 2 - len(fresh)))
        candidates = [name for name in rotation if name not in fresh][:budget]
        if not candidates:
            log("backup_pool", fresh=len(fresh), warmed=0)
            return
        for offset in range(0, len(candidates), self.candidate_workers):
            batch = candidates[offset:offset + self.candidate_workers]
            self.batch(batch)
            self.rotate_used(rotation, batch)
        fresh_after = sum(name != current and
                          0 <= time.time() - self.verified_at(name) <= BACKUP_MAX_AGE
                          for name in nodes)
        log("backup_pool", fresh=fresh_after, warmed=len(candidates))

    def run(self):
        provider = self.api.provider(self.provider_name)
        groups = {}
        for index, name in enumerate(self.group_names):
            try:
                group = self.api.group(name)
            except APIError as exc:
                if index and exc.status == 404:
                    log("optional_group_missing", group=name)
                    continue
                raise
            if group.get("type") != "Selector":
                raise GuardError("managed groups must be Selector groups; migrate type to select first")
            if not isinstance(group.get("all"), list):
                raise GuardError("managed group has no valid node list")
            if not group["all"] or group["all"][0] != "REJECT":
                raise GuardError("managed Selector groups must keep REJECT as the first available proxy")
            if group.get("emptyFallback") != "REJECT":
                raise GuardError("managed Selector groups must set empty-fallback to REJECT")
            groups[name] = group

        allowed = set.intersection(*(set(group["all"]) for group in groups.values()))
        provider_nodes = provider.get("proxies", [])
        if not isinstance(provider_nodes, list):
            raise GuardError("provider has no valid node list")
        nodes = {}
        for node in provider_nodes:
            name = node.get("name")
            kind = str(node.get("type", "")).replace("-", "").lower()
            if (isinstance(name, str) and name in allowed and name.upper() not in BLOCKED_NAMES
                    and kind not in BLOCKED_TYPES):
                nodes[name] = node
        if not nodes:
            reason = "empty_provider" if not provider_nodes else "empty_shared_pool"
            self.fail_closed(groups, reason)
            self.state.update(last_result=reason,
                              checked_at=datetime.datetime.now().astimezone().isoformat())
            log(reason, provider=self.provider_name, provider_nodes=len(provider_nodes),
                result="no_qualified_node", dry_run=self.dry_run)
            return 1

        # Removed subscription entries cannot remain in the standby cache.
        self.state["health"] = {name: row for name, row in self.state["health"].items()
                                if name in nodes and isinstance(row, dict)}
        current = groups[self.group_names[0]].get("now")
        rotation = self.rotation(nodes, current)
        previous_failed = self.state.get("failed_cycle_nodes", [])
        if not isinstance(previous_failed, list):
            previous_failed = []
        failed_cycle = set(name for name in previous_failed if name in nodes)
        if current in nodes:
            healthy = self.test_round(current)
            if not healthy:
                self.pause()
                healthy = self.test_round(current)
            self.record(current, healthy)
            if healthy:
                self.align(groups, current, "current_healthy")
                self.warm_backups(nodes, current, rotation)
                return 0
            failed_cycle.add(current)
        else:
            log("current_not_in_provider_pool", node=current)

        # A cached success is ordering information only: every replacement still
        # needs two fresh rounds on all targets before it can be selected.
        backup_order = sorted((name for name in nodes if name != current and self.verified_at(name) > 0),
                              key=lambda name: -self.verified_at(name))
        candidates = list(dict.fromkeys(backup_order + rotation))[:self.max_candidates]
        tested = []
        for offset in range(0, len(candidates), self.candidate_workers):
            batch = candidates[offset:offset + self.candidate_workers]
            passed = self.batch(batch)
            tested.extend(batch)
            self.rotate_used(rotation, batch)
            for name, ok in zip(batch, passed):
                if not ok:
                    failed_cycle.add(name)
            for name, ok in zip(batch, passed):
                if ok:
                    self.align(groups, name, "replacement_verified")
                    return 0

        all_failed = set(nodes).issubset(failed_cycle)
        self.state.update(last_result="pool_cycle_exhausted" if all_failed else "scan_budget_exhausted",
                          checked_at=datetime.datetime.now().astimezone().isoformat(),
                          failed_cycle_nodes=[] if all_failed else sorted(failed_cycle))
        # A shared target can itself be unavailable. Never turn a failed scan
        # into a global REJECT or DIRECT selection when real nodes still exist.
        log("no_qualified_node", kept=current,
            tested=len(tested), pool_size=len(nodes),
            failed_in_cycle=len(failed_cycle), pool_cycle_exhausted=all_failed,
            dry_run=self.dry_run)
        return 1


@contextlib.contextmanager
def locked_state(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError("another guard round is already running") from None
        yield
    finally:
        os.close(fd)


def read_state(path):
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(result, dict):
            return result
    except (OSError, ValueError):
        pass
    log("state_reset", reason="unreadable or invalid state")
    return {}


def write_state(path, state):
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:9093")
    parser.add_argument("--config", default="/etc/mihomo/config.yaml")
    parser.add_argument("--state", default="/var/lib/mihomo-health-guard/state.json")
    parser.add_argument("--provider", default="Airport_01")
    parser.add_argument("--groups", default="Proxy-Auto,Docker-Auto",
                        help="comma-separated Selector groups; only the first is required")
    parser.add_argument("--max-candidates", type=int, default=12)
    parser.add_argument("--candidate-workers", type=int, default=3)
    parser.add_argument("--warmup-candidates", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=4000)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="probe and report, but never PUT a group selection")
    args = parser.parse_args(argv)
    args.groups = list(dict.fromkeys(name.strip() for name in args.groups.split(",") if name.strip()))
    if not args.groups:
        parser.error("--groups must contain at least one group")
    if not 1 <= args.max_candidates <= 12:
        parser.error("--max-candidates must be between 1 and 12")
    if not 1 <= args.candidate_workers <= 3:
        parser.error("--candidate-workers must be between 1 and 3")
    if not 0 <= args.warmup_candidates <= 2:
        parser.error("--warmup-candidates must be between 0 and 2")
    if not 100 <= args.timeout_ms <= 30000:
        parser.error("--timeout-ms must be between 100 and 30000")
    if not 0 <= args.retry_delay <= 10:
        parser.error("--retry-delay must be between 0 and 10")
    return args


def main(argv=None):
    args = arguments(argv)
    path = Path(args.state).expanduser()
    try:
        with locked_state(path):
            api = API(args.api, load_secret(args.config), args.timeout_ms)
            state = read_state(path)
            try:
                result = Guard(api, args.groups, args.provider, state, args.dry_run,
                               args.max_candidates, args.retry_delay, args.candidate_workers,
                               args.warmup_candidates).run()
            finally:
                write_state(path, state)
            return result
    except (GuardError, OSError) as exc:
        log("error", message=str(exc) if isinstance(exc, GuardError)
            else "local file access failed; check state/config permissions")
        return 2


if __name__ == "__main__":
    sys.exit(main())
