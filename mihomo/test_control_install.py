import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("install_control", HERE / "install-control.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ControlInstallTests(unittest.TestCase):
    def test_installs_wrapper_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root / "source"
            source.mkdir()
            (source / "mihomo_control.py").write_text("print('safe')\n")
            lib = root / "lib"
            lib.mkdir()
            (lib / "provider_cache_sync.py").write_text("# installed\n")
            config = root / "etc" / "config.yaml"
            config.parent.mkdir()
            config.write_text("secret: private\n")
            target = root / "opt" / "mihomo-control"
            target.parent.mkdir()
            wrapper = root / "usr" / "sbin" / "mihomoctl"
            wrapper.parent.mkdir(parents=True)
            backup_root = root / "var" / "lib"
            with mock.patch.object(MODULE.os, "geteuid", return_value=0), \
                    mock.patch.object(MODULE, "trusted_metadata", return_value=True):
                result = MODULE.install(source, lib, config, target, wrapper, backup_root)
            self.assertTrue(result["main_config_unchanged"])
            self.assertEqual((target / "mihomo_control.py").read_text(), "print('safe')\n")
            self.assertIn(str(target / "mihomo_control.py"), wrapper.read_text())
            self.assertTrue(wrapper.read_text().endswith(' "$@"\n'))
            self.assertEqual(config.read_text(), "secret: private\n")
            metadata = json.loads((Path(result["backup"]) / "installation.json").read_text())
            self.assertEqual(metadata["config_sha256_before"], MODULE.sha256(config))
            self.assertEqual((target / "mihomo_control.py").stat().st_mode & 0o777, 0o644)
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o755)

    def test_refuses_symlink_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root / "source"
            source.mkdir()
            (source / "mihomo_control.py").write_text("safe\n")
            lib = root / "lib"
            lib.mkdir()
            (lib / "provider_cache_sync.py").write_text("# installed\n")
            config = root / "config.yaml"
            config.write_text("mode: rule\n")
            parent = root / "usr" / "sbin"
            parent.mkdir(parents=True)
            target = root / "opt" / "mihomo-control"
            target.parent.mkdir()
            target.symlink_to(source, target_is_directory=True)
            with mock.patch.object(MODULE.os, "geteuid", return_value=0), \
                    mock.patch.object(MODULE, "trusted_metadata", return_value=True):
                with self.assertRaises(MODULE.InstallError):
                    MODULE.install(source, lib, config, target,
                                   parent / "mihomoctl", root / "backup")

    def test_wrapper_write_failure_restores_previous_module_and_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source, lib, target = root / "source", root / "lib", root / "target"
            for path in (source, lib, target):
                path.mkdir()
            (source / "mihomo_control.py").write_text("print('new')\n")
            (lib / "provider_cache_sync.py").write_text("# installed\n")
            old = target / "mihomo_control.py"
            old.write_text("print('previous')\n")
            old.chmod(0o640)
            config = root / "config.yaml"
            config.write_text("secret: private\n")
            wrapper = root / "mihomoctl"
            wrapper.write_text("previous wrapper\n")
            wrapper.chmod(0o750)
            real_write = MODULE.atomic_write

            def fail_wrapper(path, *args):
                if path == wrapper:
                    raise OSError("simulated full disk")
                return real_write(path, *args)

            with mock.patch.object(MODULE.os, "geteuid", return_value=0), \
                    mock.patch.object(MODULE, "trusted_metadata", return_value=True), \
                    mock.patch.object(MODULE, "atomic_write", side_effect=fail_wrapper):
                with self.assertRaises(OSError):
                    MODULE.install(source, lib, config, target, wrapper, root / "backup")
            self.assertEqual(old.read_text(), "print('previous')\n")
            self.assertEqual(old.stat().st_mode & 0o777, 0o640)
            self.assertEqual(wrapper.read_text(), "previous wrapper\n")
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o750)
            self.assertEqual(config.read_text(), "secret: private\n")

    def test_nonroot_refused_before_files_created(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with mock.patch.object(MODULE.os, "geteuid", return_value=1000):
                with self.assertRaises(MODULE.InstallError):
                    MODULE.install(root, root, root / "config", root / "target",
                                   root / "wrapper", root / "backup")
            self.assertEqual(list(root.iterdir()), [])

    def test_trust_rejects_nonroot_or_group_writable_directories(self):
        self.assertTrue(MODULE.trusted_metadata(SimpleNamespace(st_uid=0, st_mode=0o40755)))
        self.assertFalse(MODULE.trusted_metadata(SimpleNamespace(st_uid=1000, st_mode=0o40755)))
        self.assertFalse(MODULE.trusted_metadata(SimpleNamespace(st_uid=0, st_mode=0o40775)))
        self.assertFalse(MODULE.trusted_metadata(SimpleNamespace(st_uid=0, st_mode=0o40777)))

    def test_untrusted_destination_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source, lib, target = root / "source", root / "lib", root / "target"
            for path in (source, lib, target):
                path.mkdir()
            (source / "mihomo_control.py").write_text("print('new')\n")
            (lib / "provider_cache_sync.py").write_text("# installed\n")
            config = root / "config.yaml"
            config.write_text("mode: rule\n")
            with mock.patch.object(MODULE.os, "geteuid", return_value=0), \
                    mock.patch.object(MODULE, "trusted_metadata", return_value=False):
                with self.assertRaises(MODULE.InstallError):
                    MODULE.install(source, lib, config, target, root / "wrapper", root / "backup")
            self.assertEqual(list(target.iterdir()), [])
            self.assertFalse((root / "backup").exists())


if __name__ == "__main__":
    unittest.main()
