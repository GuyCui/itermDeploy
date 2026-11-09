#!/usr/bin/env python3
"""Install the public mihomoctl read-only control entry.

This installer assumes that the v3 guard/cache modules are already installed.
It only installs mihomo_control.py and a small wrapper; it does not restart
Mihomo or change the main configuration.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import stat


MODULE = "mihomo_control.py"


class InstallError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def trusted_metadata(info):
    return info.st_uid == 0 and not info.st_mode & 0o022


def require_directory(path, label):
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink() or not item.is_dir() or not trusted_metadata(item.stat()):
            raise InstallError(f"{label} is not a root-owned protected directory: {item}")


def atomic_write(path, data, mode):
    path = Path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.install-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise InstallError(f"staging file already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(source, lib, config, target, wrapper, backup_root):
    if os.geteuid() != 0:
        raise InstallError("请在 Debian root 终端运行此脚本")
    source = Path(source).resolve(strict=True)
    lib = Path(lib)
    config = Path(config)
    target = Path(target)
    wrapper = Path(wrapper)
    backup_root = Path(backup_root)
    source_module = source / MODULE
    if not source_module.is_file() or source_module.is_symlink():
        raise InstallError(f"缺少 {source_module}")
    if not config.is_file() or config.is_symlink():
        raise InstallError(f"缺少主配置 {config}")
    if not (lib / "provider_cache_sync.py").is_file():
        raise InstallError(f"未找到已安装的 v3 模块: {lib}")
    require_directory(lib, "核心模块目录")
    require_directory(target.parent, "安装目录父目录")
    require_directory(wrapper.parent, "命令目录父目录")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise InstallError(f"安装目录异常: {target}")
    if target.exists():
        require_directory(target, "现有安装目录")
    compile(source_module.read_bytes(), str(source_module), "exec")
    if backup_root.is_symlink() or (backup_root.exists() and not backup_root.is_dir()):
        raise InstallError(f"备份目录异常: {backup_root}")
    ancestor = backup_root
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    require_directory(ancestor, "备份目录父目录")
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_directory(backup_root, "备份目录")

    before = sha256(config)
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_root / f"control-install-{stamp}"
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup.chmod(0o700)
    previous = {}
    previous_metadata = {}
    for path in (target / MODULE, wrapper):
        if path.is_symlink():
            raise InstallError(f"安装目标不能是符号链接: {path}")
        saved = None
        if path.exists():
            if not path.is_file():
                raise InstallError(f"安装目标不是普通文件: {path}")
            saved = backup / (path.name + ".previous")
            previous_metadata[str(path)] = {
                "mode": stat.S_IMODE(path.stat().st_mode),
                "uid": path.stat().st_uid, "gid": path.stat().st_gid,
            }
            shutil.copy2(path, saved)
            saved.chmod(0o600)
        previous[str(path)] = str(saved) if saved else None
    metadata = {
        "config_sha256_before": before,
        "source_sha256": sha256(source_module),
        "previous_files": previous,
        "previous_metadata": previous_metadata,
        "installed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (backup / "installation.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (backup / "installation.json").chmod(0o600)
    written = []
    try:
        target.mkdir(mode=0o755, exist_ok=True)
        if target.is_symlink() or not target.is_dir():
            raise InstallError(f"安装目录异常: {target}")
        target.chmod(0o755)
        destination = target / MODULE
        if destination.is_symlink():
            raise InstallError(f"安装文件目标是符号链接: {destination}")
        atomic_write(destination, source_module.read_bytes(), 0o644)
        written.append(destination)
        wrapper_text = ("#!/bin/sh\nexec /usr/bin/python3 "
                        + shlex.quote(str(target / MODULE)) + ' "$@"\n')
        atomic_write(wrapper, wrapper_text.encode(), 0o755)
        written.append(wrapper)
        if sha256(config) != before:
            raise InstallError("主配置在安装过程中发生变化")
    except Exception:
        for path in reversed(written):
            saved = previous[str(path)]
            if saved:
                shutil.copy2(saved, path)
                info = previous_metadata[str(path)]
                path.chmod(info["mode"])
                if path.stat().st_uid != info["uid"] or path.stat().st_gid != info["gid"]:
                    os.chown(path, info["uid"], info["gid"])
            else:
                path.unlink(missing_ok=True)
        raise
    return {
        "installed": True,
        "module": str(target / MODULE),
        "wrapper": str(wrapper),
        "backup": str(backup),
        "main_config_unchanged": True,
        "module_sha256": sha256(target / MODULE),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--lib", default="/usr/local/lib/mihomo-health-guard")
    parser.add_argument("--config", default="/etc/mihomo/config.yaml")
    parser.add_argument("--target", default="/opt/mihomo-control")
    parser.add_argument("--wrapper", default="/usr/local/sbin/mihomoctl")
    parser.add_argument("--backup-root", default="/var/lib/mihomo-health-guard")
    args = parser.parse_args(argv)
    result = install(args.source, args.lib, args.config, args.target,
                     args.wrapper, args.backup_root)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as error:
        raise SystemExit(str(error))
