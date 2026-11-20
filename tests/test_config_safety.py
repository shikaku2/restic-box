"""Configuration and password-file safety regression tests."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import restic_config as config


class ConfigSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_file = self.root / "config" / "config.json"

        patcher = mock.patch.object(
            config, "CONFIG_FILE", self.config_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_config_returns_defaults(self):
        self.assertEqual(config.load_config(), config.Config())

    def test_round_trip(self):
        cfg = config.Config(
            backend="local",
            repo_path="/example/repository",
            directories=[
                config.Directory(
                    "/example/data",
                    excludes=["cache"],
                    cached_size=123,
                )
            ],
        )

        config.save_config(cfg)
        self.assertEqual(config.load_config(), cfg)

    def test_corrupt_config_is_not_silently_reset(self):
        self.config_file.parent.mkdir(parents=True)
        original = b'{"backend": '
        self.config_file.write_bytes(original)

        with self.assertRaises(config.ConfigError):
            config.load_config()

        self.assertEqual(self.config_file.read_bytes(), original)

    def test_non_object_config_is_rejected(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("[]", encoding="utf-8")

        with self.assertRaises(config.ConfigError):
            config.load_config()

    def test_failed_replace_preserves_previous_file(self):
        old = config.Config(backend="local", repo_path="/old")
        new = config.Config(backend="local", repo_path="/new")
        config.save_config(old)
        original = self.config_file.read_bytes()

        with mock.patch.object(
            config.os, "replace", side_effect=OSError("simulated failure")
        ):
            with self.assertRaises(OSError):
                config.save_config(new)

        self.assertEqual(self.config_file.read_bytes(), original)
        self.assertEqual(
            list(self.config_file.parent.glob("*.tmp")), []
        )

    def test_temporary_file_is_complete_before_replace(self):
        cfg = config.Config(backend="local", repo_path="/complete")
        real_replace = os.replace
        seen = []

        def inspect_then_replace(source, destination):
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            seen.append(payload["repo_path"])
            real_replace(source, destination)

        with mock.patch.object(
            config.os, "replace", side_effect=inspect_then_replace
        ):
            config.save_config(cfg)

        self.assertEqual(seen, ["/complete"])
        self.assertEqual(config.load_config(), cfg)

    def test_unknown_directory_fields_are_ignored(self):
        cfg = config.Config.from_dict({
            "future_top_level_field": True,
            "directories": [{
                "path": "/data",
                "future_directory_field": "ignored",
            }],
        })

        self.assertEqual(cfg.directories, [config.Directory("/data")])

    def test_invalid_subset_total_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.Config.from_dict({"check_subset_total": 0})

    def test_boolean_is_not_accepted_as_integer(self):
        with self.assertRaises(config.ConfigError):
            config.Config.from_dict({"ssh_port": True})

    def test_invalid_directory_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.Config.from_dict({
                "directories": [{"path": "/data", "excludes": "cache"}]
            })

    def test_generated_password_has_private_permissions(self):
        password_path = self.root / "secret" / "password"
        cfg = config.Config(password_file=str(password_path))

        password = config.generate_password(cfg)

        self.assertEqual(len(password), 48)
        self.assertEqual(
            password_path.read_text(encoding="utf-8"), password + "\n"
        )
        self.assertEqual(
            stat.S_IMODE(password_path.stat().st_mode), 0o600
        )

    def test_existing_password_is_never_overwritten(self):
        password_path = self.root / "password"
        password_path.write_text("irreplaceable-secret\n", encoding="utf-8")
        cfg = config.Config(password_file=str(password_path))

        with self.assertRaises(FileExistsError):
            config.generate_password(cfg)

        self.assertEqual(
            password_path.read_text(encoding="utf-8"),
            "irreplaceable-secret\n",
        )

    def test_password_symlink_is_never_followed(self):
        target = self.root / "actual-secret"
        target.write_text("keep-me\n", encoding="utf-8")
        link = self.root / "password-link"
        link.symlink_to(target)
        cfg = config.Config(password_file=str(link))

        with self.assertRaises(FileExistsError):
            config.generate_password(cfg)

        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep-me\n")

    def test_dangling_password_symlink_is_not_replaced(self):
        target = self.root / "missing"
        link = self.root / "dangling"
        link.symlink_to(target)
        cfg = config.Config(password_file=str(link))

        with self.assertRaises(FileExistsError):
            config.generate_password(cfg)

        self.assertTrue(link.is_symlink())
        self.assertFalse(target.exists())

    def test_empty_password_path_is_rejected(self):
        with self.assertRaises(ValueError):
            config.generate_password(config.Config(password_file=" "))


if __name__ == "__main__":
    unittest.main()
