"""Config persistence + password safety tests (issues #2 and #10)."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import restic_config as cfg  # noqa: E402


class PasswordSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-pw-"))
        self.pwfile = self.tmp / "password"
        self.cfg = cfg.Config(password_file=str(self.pwfile))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_secret_at_0600(self):
        pw = cfg.generate_password(self.cfg)
        self.assertTrue(self.pwfile.exists())
        mode = stat.S_IMODE(os.stat(self.pwfile).st_mode)
        self.assertEqual(mode, 0o600, "secret file must be created mode 0600")
        self.assertEqual(self.pwfile.read_text().strip(), pw)

    def test_refuses_to_overwrite_existing_file(self):
        self.pwfile.write_text("the-only-good-password\n")
        os.chmod(self.pwfile, 0o600)
        with self.assertRaises(FileExistsError):
            cfg.generate_password(self.cfg)
        # the existing credential is byte-for-byte intact and still readable
        self.assertEqual(self.pwfile.read_text(), "the-only-good-password\n")

    def test_explicit_overwrite_replaces_at_0600(self):
        self.pwfile.write_text("old\n")
        pw = cfg.generate_password(self.cfg, overwrite=True)
        self.assertEqual(self.pwfile.read_text().strip(), pw)
        self.assertEqual(stat.S_IMODE(os.stat(self.pwfile).st_mode), 0o600)

    def test_password_file_exists(self):
        self.assertFalse(cfg.password_file_exists(self.cfg))
        self.pwfile.write_text("x\n")
        self.assertTrue(cfg.password_file_exists(self.cfg))


class ConfigPersistenceTest(unittest.TestCase):
    """Issue #10: atomic, serialized writes; corruption does not silently reset."""

    def setUp(self):
        self._real = cfg.CONFIG_DIR
        self._real_file = cfg.CONFIG_FILE
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-cfg-"))
        cfg.CONFIG_DIR = self.tmp
        cfg.CONFIG_FILE = self.tmp / "config.json"

    def tearDown(self):
        cfg.CONFIG_DIR = self._real
        cfg.CONFIG_FILE = self._real_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip(self):
        c = cfg.Config(ssh_host="h", ssh_port=2222, repo_path="/r",
                       directories=[cfg.Directory("/data", ["*.tmp"], True, 1.0)])
        cfg.save_config(c)
        loaded = cfg.load_config()
        self.assertEqual(loaded.ssh_host, "h")
        self.assertEqual(loaded.ssh_port, 2222)
        self.assertEqual(loaded.directories[0].path, "/data")
        self.assertEqual(loaded.directories[0].excludes, ["*.tmp"])

    def test_partial_flag_roundtrip(self):
        # issue #3: an incomplete run persists as partial, not as ok/failed
        cfg.save_config(cfg.Config(last_backup_ok=False, last_backup_partial=True))
        loaded = cfg.load_config()
        self.assertFalse(loaded.last_backup_ok)
        self.assertTrue(loaded.last_backup_partial)

    def test_no_temp_litter_after_save(self):
        cfg.save_config(cfg.Config())
        strays = [p for p in self.tmp.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(strays, [])

    def test_unknown_directory_field_is_tolerated(self):
        # forward-compatible: an unknown per-directory field must not discard the
        # whole config (previously Directory(**rd) raised and load reset defaults)
        raw = {"backend": "local", "repo_path": "/keep",
               "directories": [{"path": "/data", "brand_new_field": 123}]}
        cfg.CONFIG_FILE.write_text(json.dumps(raw))
        loaded = cfg.load_config()
        self.assertEqual(loaded.repo_path, "/keep")
        self.assertEqual(loaded.directories[0].path, "/data")

    def test_parse_failure_is_reported_not_silently_reset(self):
        cfg.CONFIG_FILE.write_text("{ this is not valid json")
        with self.assertRaises(Exception):
            cfg.load_config()

    def test_last_known_good_used_on_corruption(self):
        good = cfg.Config(repo_path="/good", ssh_host="host1")
        cfg.save_config(good)
        backup = cfg.CONFIG_FILE.with_name(cfg.CONFIG_FILE.name + ".bak")
        self.assertTrue(backup.exists(), "a last-known-good copy should be kept")
        cfg.CONFIG_FILE.write_text("corrupt {{{")
        loaded = cfg.load_config()
        self.assertEqual(loaded.repo_path, "/good")
        self.assertEqual(loaded.ssh_host, "host1")


if __name__ == "__main__":
    unittest.main()
