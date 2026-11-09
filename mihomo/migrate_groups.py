#!/usr/bin/env python3
"""Conservatively migrate block YAML groups; validate, reload and roll back."""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from mihomo_health_guard import API, GuardError, load_secret, parse_secret_scalar


MANAGED = ("Proxy-Auto", "Docker-Auto")
AUTO_KEYS = {"url", "expected-status", "interval", "timeout", "tolerance", "lazy", "max-failed-times"}
UNITS = ("mihomo-health-guard.service", "mihomo-health-guard.timer")
BACKUP_ROOT = Path("/var/lib/mihomo-health-guard")


class MigrationError(Exception):
    pass


def scalar(value):
    try:
        return parse_secret_scalar(value)
    except GuardError:
        raise MigrationError("不支持的 YAML 标量格式；配置未修改") from None


def inline_comment(value):
    """Find a trailing YAML comment without interpreting quoted # as comment."""
    value = value.strip()
    if not value:
        return ""
    if value[0] not in "\"'":
        match = re.search(r"(?:^|[ \t]+)(#.*)$", value)
        return match.group(1) if match else ""
    quote, index = value[0], 1
    while index < len(value):
        if quote == '"' and value[index] == "\\":
            index += 2
            continue
        if value[index] == quote:
            if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            tail = value[index + 1:].strip()
            return tail if tail.startswith("#") else ""
        index += 1
    return ""


def old_proxies(lines, indent, group_names):
    """Preserve ordinary block strings/comments; reject ambiguous flow lists."""
    raw = lines[0].split(":", 1)[1].strip()
    comments = lambda rows: [s for s in rows if not s.strip() or s.lstrip().startswith("#")]
    if raw and not raw.startswith("#"):
        if re.fullmatch(r"\[\s*(?:(?:REJECT|'REJECT'|\"REJECT\")\s*)?\](?:\s+#.*)?", raw):
            return comments(lines[1:])
        raise MigrationError("受管组使用复杂的行内 proxies 列表；配置未修改")
    result = []
    for line in lines[1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            result.append(line)
            continue
        match = re.fullmatch(r" {%d}-\s+(.+?)(?:\r?\n)?" % (indent + 2), line)
        if not match:
            raise MigrationError("受管组的 proxies 不是普通字符串列表；配置未修改")
        value = match.group(1)
        if value[0] not in "'\"" and re.search(r":\s", value):
            raise MigrationError("受管组的 proxies 包含复杂映射；配置未修改")
        name = scalar(value)
        if name in group_names:
            raise MigrationError("受管组引用了其他策略组；需要直接引用提供器，配置未修改")
        if name != "REJECT":
            result.append(line)
    return result


def transform(text):
    """Return transformed text and managed names. Never write or parse secrets."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    sections = [i for i, line in enumerate(lines)
                if re.fullmatch(r"proxy-groups:\s*(?:#.*)?(?:\r?\n)?", line)]
    if len(sections) != 1:
        raise MigrationError("需要唯一的普通 block proxy-groups: 段；配置未修改")
    start = sections[0] + 1
    end = next((i for i in range(start, len(lines))
                if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
    entries, group_names, item_indent = [], set(), None
    for i in range(start, end):
        line = lines[i]
        if "\t" in line[:len(line) - len(line.lstrip())]:
            raise MigrationError("proxy-groups 缩进含 TAB，无法安全编辑；配置未修改")
        match = re.match(r"^( *)-\s+name:\s*(.+?)(?:\r?\n)?$", line)
        if match:
            indent = len(match.group(1))
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:
                name = scalar(match.group(2))
                if name in group_names:
                    raise MigrationError("proxy-groups 含重复组名；配置未修改")
                entries.append((i, name))
                group_names.add(name)
        elif item_indent is not None and re.match(r"^ {%d}-\s" % item_indent, line):
            if any(name in line for name in MANAGED):
                raise MigrationError("受管组使用行内或锚点写法；配置未修改")
            entries.append((i, None))
    if MANAGED[0] not in group_names:
        raise MigrationError("未找到普通 block 格式的 Proxy-Auto；配置未修改")
    managed = [name for name in MANAGED if name in group_names]
    replacements = []
    for index, (a, name) in enumerate(entries):
        if name not in MANAGED:
            continue
        b = entries[index + 1][0] if index + 1 < len(entries) else end
        block, indent, fields = lines[a:b], item_indent + 2, []
        for offset, line in enumerate(block[1:], 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            depth = len(line) - len(line.lstrip(" "))
            if depth < indent:
                raise MigrationError("受管组缩进不规则；配置未修改")
            if depth == indent:
                m = re.match(r"^ {%d}([A-Za-z0-9_-]+):(?:\s*(.*?))?(?:\r?\n)?$" % indent, line)
                if not m:
                    raise MigrationError("受管组含合并锚点或复杂字段；配置未修改")
                fields.append((offset, m.group(1), m.group(2) or ""))
        by_key, spans = {}, {}
        for n, (pos, key, value) in enumerate(fields):
            if key in by_key:
                raise MigrationError("受管组包含重复字段；配置未修改")
            by_key[key] = value
            spans[key] = (pos, fields[n + 1][0] if n + 1 < len(fields) else len(block))
        if "type" not in by_key or scalar(by_key["type"]) not in ("url-test", "select"):
            raise MigrationError("受管组必须是 url-test 或 select；配置未修改")
        if "use" not in by_key and scalar(by_key.get("include-all-providers", "false")).lower() != "true":
            raise MigrationError("受管组没有提供器入口；配置未修改")
        proxy_lines = []
        if "proxies" in spans:
            p, q = spans["proxies"]
            proxy_lines = old_proxies(block[p:q], indent, group_names)
        removed, comments = set(), []
        prefix = " " * indent
        for key, (p, q) in sorted(spans.items(), key=lambda row: row[1][0]):
            if key not in AUTO_KEYS | {"type", "empty-fallback", "proxies"}:
                continue
            if key in AUTO_KEYS:
                scalar(by_key[key])  # reject unsafe anchors/block scalars
                if any(row.strip() and not row.lstrip().startswith("#") for row in block[p + 1:q]):
                    raise MigrationError("受管组测速字段使用复杂 block 值；配置未修改")
            removed.update(range(p, q))
            comment = inline_comment(by_key[key])
            if comment and key != "type":
                comments.append(prefix + comment + newline)
            if key != "proxies":
                comments.extend(s for s in block[p + 1:q] if not s.strip() or s.lstrip().startswith("#"))
        type_comment = inline_comment(by_key["type"])
        replacement = [block[0], prefix + "type: select" + (" " + type_comment if type_comment else "") + newline,
                       prefix + "empty-fallback: REJECT" + newline,
                       prefix + "proxies:" + newline, prefix + "  - REJECT" + newline]
        replacement += proxy_lines + comments
        replacement += [line for n, line in enumerate(block[1:], 1) if n not in removed]
        replacements.append((a, b, replacement))
    for a, b, replacement in reversed(replacements):
        lines[a:b] = replacement
    return "".join(lines), managed


def private_copy(source, destination):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("xb") as dst:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(src, dst)


def atomic_write(path, data, metadata):
    fd, temporary = tempfile.mkstemp(prefix=".guard-write-", suffix=".yaml", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        if os.geteuid() == 0:
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def metadata_dict(metadata):
    return {"mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid, "gid": metadata.st_gid}


def metadata_from_dict(row):
    if not isinstance(row, dict) or set(row) != {"mode", "uid", "gid"}:
        raise ValueError("invalid file metadata")
    if any(type(row[key]) is not int or row[key] < 0 for key in row):
        raise ValueError("invalid file metadata")
    if row["mode"] > 0o7777:
        raise ValueError("invalid file mode")
    class Metadata:
        st_mode = row["mode"]
        st_uid = row["uid"]
        st_gid = row["gid"]
    return Metadata()


def installed_destinations():
    return {"mihomo_health_guard.py": Path("/usr/local/lib/mihomo-health-guard/mihomo_health_guard.py"),
            **{name: Path("/etc/systemd/system") / name for name in UNITS}}


def quiet_run(command, timeout=45):
    try:
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def service_exit_status(unit):
    try:
        result = subprocess.run(["systemctl", "show", unit, "--property=ExecMainStatus", "--value"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def verify_selectors(api, names, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            groups = [api.group(name) for name in names]
            if all(isinstance(group, dict) and group.get("type") == "Selector"
                   and group.get("emptyFallback") == "REJECT"
                   and isinstance(group.get("all"), list) and group["all"]
                   and group["all"][0] == "REJECT" for group in groups):
                return True
        except GuardError:
            pass
        time.sleep(0.5)
    return False


def migrate(config, api_base="http://127.0.0.1:9093", mihomo="/usr/bin/mihomo",
            backup_dir=None, runner=quiet_run, verifier=verify_selectors):
    config = Path(config).resolve(strict=True)
    old, metadata = config.read_bytes(), config.stat()
    try:
        revised, names = transform(old.decode("utf-8-sig"))
    except UnicodeError:
        raise MigrationError("配置不是 UTF-8；配置未修改") from None
    new = revised.encode("utf-8")
    fd, candidate = tempfile.mkstemp(prefix=".guard-check-", suffix=".yaml", dir=config.parent)
    candidate = Path(candidate)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(new)
        os.chmod(candidate, stat.S_IMODE(metadata.st_mode))
        if os.geteuid() == 0:
            os.chown(candidate, metadata.st_uid, metadata.st_gid)
        if not runner([mihomo, "-t", "-d", str(config.parent), "-f", str(candidate)]):
            raise MigrationError("mihomo 配置校验失败；原配置未修改（检查依赖文件、权限及 YAML）")
    finally:
        candidate.unlink(missing_ok=True)
    api = API(api_base, load_secret(config))
    if new == old:
        if not verifier(api, names):
            if not runner(["systemctl", "reload", "mihomo.service"]) or not verifier(api, names):
                raise MigrationError("受管组需要 Selector、emptyFallback=REJECT 且首项为 REJECT；检查 filter/exclude-filter/exclude-type")
        return {"changed": False, "groups": names, "backup": None}
    if backup_dir is None:
        backup_dir = config.parent / (".guard-backup-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    backup = backup_dir / "config.yaml"
    private_copy(config, backup)
    atomic_write(config, new, metadata)
    try:
        if not runner(["systemctl", "reload", "mihomo.service"]):
            raise MigrationError("mihomo 重载失败")
        if not verifier(api, names):
            raise MigrationError("受管组需要 Selector、emptyFallback=REJECT 且首项为 REJECT；检查 filter/exclude-filter/exclude-type")
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, MigrationError) else "迁移验证发生异常"
        atomic_write(config, old, metadata)
        restored = runner(["systemctl", "reload", "mihomo.service"])
        message = "已恢复原配置并重载" if restored else "已恢复原配置，但回滚重载失败，请检查 mihomo 服务"
        raise MigrationError(reason + "；" + message + "；备份目录：" + str(backup_dir)) from None
    return {"changed": True, "groups": names, "backup": str(backup)}


def install(source, config, api_base, mihomo):
    source = Path(source).resolve(strict=True)
    required = ("mihomo_health_guard.py",) + UNITS
    for name in required:
        if not (source / name).is_file():
            raise MigrationError("安装包缺少必要文件：" + name)
    try:
        compile((source / "mihomo_health_guard.py").read_text(), "mihomo_health_guard.py", "exec")
    except (SyntaxError, UnicodeError):
        raise MigrationError("交付脚本语法错误；未安装") from None
    if os.geteuid() != 0:
        raise MigrationError("请使用 sudo sh install.sh")
    config = Path(config).resolve(strict=True)
    suffix = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    transaction = BACKUP_ROOT / ("upgrade-" + suffix)
    transaction.mkdir(mode=0o700, parents=True)
    os.chmod(transaction.parent, 0o700)
    config_data, config_metadata = config.read_bytes(), config.stat()
    private_copy(config, transaction / "original-config.yaml")
    destinations = installed_destinations()
    originals = {}
    for name, path in destinations.items():
        originals[name] = (path.read_bytes(), path.stat()) if path.exists() else None
        if path.exists():
            private_copy(path, transaction / name)
    timer_enabled = quiet_run(["systemctl", "is-enabled", "--quiet", UNITS[1]])
    timer_active = quiet_run(["systemctl", "is-active", "--quiet", UNITS[1]])
    manifest = {"version": 1, "config": str(config), "config_metadata": metadata_dict(config_metadata),
                "timer_enabled": timer_enabled, "timer_active": timer_active,
                "files": {name: metadata_dict(original[1]) if original else None
                          for name, original in originals.items()}}
    manifest_path = transaction / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as handle:
        os.chmod(manifest_path, 0o600)
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    try:
        # Missing units are normal on a first install. Stop only old units that
        # exist or are active; the installer must not race a running v1 guard.
        for unit in (UNITS[1], UNITS[0]):
            if originals[unit] is not None or quiet_run(["systemctl", "is-active", "--quiet", unit]):
                if not quiet_run(["systemctl", "stop", unit]):
                    raise MigrationError("无法停止旧检测器；配置未修改")
        migrate(config, api_base, mihomo, transaction / "migrated-config-backup")
        for name, destination in destinations.items():
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            data = (source / name).read_bytes()
            fd, temporary = tempfile.mkstemp(prefix=".guard-install-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.chmod(temporary, 0o644)
                os.chown(temporary, 0, 0)
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        if not quiet_run(["systemctl", "daemon-reload"]):
            raise MigrationError("systemd 单元重载失败")
        if not quiet_run(["systemctl", "enable", "--now", UNITS[1]]):
            raise MigrationError("无法启用新版检测定时器")
        print("私有回滚备份：" + str(transaction), flush=True)
        print("正在执行首次新版检查；故障候选较多时会等待一段时间。", flush=True)
        first_ok = quiet_run(["systemctl", "start", UNITS[0]], timeout=360)
        subprocess.run(["journalctl", "-u", UNITS[0], "-n", "20", "--no-pager"], check=False)
        subprocess.run(["systemctl", "list-timers", UNITS[1], "--no-pager"], check=False)
        if not first_ok and service_exit_status(UNITS[0]) != 1:
            raise MigrationError("首次新版检查无法正常执行")
        print("升级完成：受管组已改为 select，REJECT 为首项；自动检测器已启用。", flush=True)
        if not first_ok:
            print("首次检查没有找到可用节点；保留新版定时器继续重试，请查看日志。", file=sys.stderr)
            return 1
        return 0
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, MigrationError) else "安装过程发生异常"
        quiet_run(["systemctl", "stop", UNITS[1], UNITS[0]])
        # Remove a newly created enable symlink before removing a new unit file.
        quiet_run(["systemctl", "disable", UNITS[1]])
        atomic_write(config, config_data, config_metadata)
        for name, destination in destinations.items():
            original = originals[name]
            if original is None:
                destination.unlink(missing_ok=True)
            else:
                data, metadata = original
                atomic_write(destination, data, metadata)
        quiet_run(["systemctl", "daemon-reload"])
        reloaded = quiet_run(["systemctl", "reload", "mihomo.service"])
        if originals[UNITS[1]] is not None:
            quiet_run(["systemctl", "enable" if timer_enabled else "disable", UNITS[1]])
            if timer_active:
                quiet_run(["systemctl", "start", UNITS[1]])
        message = "已恢复原配置和旧文件" if reloaded else "原文件已恢复，但 mihomo 回滚重载失败"
        raise MigrationError(reason + "；" + message + "；私有备份：" + str(transaction)) from None


def rollback(directory, runner=quiet_run):
    """Restore a complete installation snapshot, stopping the new guard first."""
    if os.geteuid() != 0:
        raise MigrationError("回滚需要 root 权限")
    directory = Path(directory).resolve(strict=True)
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest.get("version") != 1 or set(manifest["files"]) != set(installed_destinations()):
            raise ValueError()
        config = Path(manifest["config"])
        if not config.is_absolute() or not (directory / "original-config.yaml").is_file():
            raise ValueError()
        metadata_from_dict(manifest["config_metadata"])
        if type(manifest["timer_enabled"]) is not bool or type(manifest["timer_active"]) is not bool:
            raise ValueError()
        for name, metadata in manifest["files"].items():
            if metadata is not None:
                metadata_from_dict(metadata)
                if not (directory / name).is_file():
                    raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise MigrationError("回滚目录不完整或 manifest 无效；尚未恢复文件") from None
    if not runner(["systemctl", "stop", UNITS[1], UNITS[0]]):
        raise MigrationError("无法停止检测器；未执行回滚")
    if not runner(["systemctl", "disable", UNITS[1]]):
        raise MigrationError("无法停用新版定时器；未执行回滚")
    atomic_write(config, (directory / "original-config.yaml").read_bytes(),
                 metadata_from_dict(manifest["config_metadata"]))
    for name, destination in installed_destinations().items():
        metadata = manifest["files"][name]
        if metadata is None:
            destination.unlink(missing_ok=True)
        else:
            atomic_write(destination, (directory / name).read_bytes(), metadata_from_dict(metadata))
    loaded = runner(["systemctl", "daemon-reload"])
    reloaded = runner(["systemctl", "reload", "mihomo.service"])
    # A first-time install has no old timer/script: do not resurrect anything.
    old_timer_exists = manifest["files"].get(UNITS[1]) is not None
    enabled = bool(manifest.get("timer_enabled")) and old_timer_exists
    if old_timer_exists:
        runner(["systemctl", "enable" if enabled else "disable", UNITS[1]])
    if old_timer_exists and manifest.get("timer_active"):
        runner(["systemctl", "start", UNITS[1]])
    if not loaded or not reloaded:
        raise MigrationError("原文件已恢复，但 systemd/mihomo 重载失败，请检查服务")
    print("已恢复升级前的配置、脚本、单元和定时器状态。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/mihomo/config.yaml")
    parser.add_argument("--api", default="http://127.0.0.1:9093")
    parser.add_argument("--mihomo", default="/usr/bin/mihomo")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", help="write a private transformed copy; never modify/reload original")
    parser.add_argument("--install-from")
    parser.add_argument("--rollback", help="restore an upgrade snapshot directory")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            if args.install_from or args.check or args.output:
                raise MigrationError("--rollback 不能与安装/检查/输出选项合用")
            return rollback(args.rollback)
        if args.install_from:
            if args.check or args.output:
                raise MigrationError("--install-from 不能与 --check/--output 合用")
            return install(args.install_from, args.config, args.api, args.mihomo)
        if args.check or args.output:
            config = Path(args.config).resolve(strict=True)
            original = config.read_text(encoding="utf-8-sig")
            revised, names = transform(original)
            if args.output:
                destination = Path(args.output)
                if destination.resolve() == config:
                    raise MigrationError("--output 不能覆盖原配置")
                with destination.open("x", encoding="utf-8", newline="") as handle:
                    os.chmod(destination, 0o600)
                    handle.write(revised)
            print(json.dumps({"groups": names, "changed": revised != original,
                              "original_modified": False}, ensure_ascii=False))
            return 0
        print(json.dumps(migrate(args.config, args.api, args.mihomo), ensure_ascii=False))
        return 0
    except (MigrationError, GuardError, OSError) as exc:
        message = str(exc) if isinstance(exc, (MigrationError, GuardError)) else "文件访问失败，请检查路径与权限"
        print("迁移未完成：" + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
