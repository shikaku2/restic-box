"""Configuration-identity bookkeeping tests (fix #1, restic-box-fixes2.md).

"Backed up" and "checked" are statements about a specific repository *and* a
specific set of source directories. These tests pin the rule that an
operation's results are credited only to the configuration identity it ran
against:

* pointing the settings at repository B mid-backup leaves B as "never backed
  up" — A's success waits in history and is restored if the settings ever
  point back at A;
* changing the source directories has the same effect (what was backed up
  changed);
* check attempts/success and the subset rotation follow the same rule;
* history survives the JSON round trip and cannot grow without bound.

No network, no ssh, no real repositories: everything here is pure config
math plus a redirected config file in a temp dir.
"""
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg  # noqa: E402


def cfg_a(**kw):
    """Repository A with two sources — the 'was backed up' configuration."""
    base = dict(
        backend="sftp", ssh_host="hostA", repo_path="/repo-a",
        directories=[cfg.Directory("/rb-src-a"), cfg.Directory("/rb-src-b")],
    )
    base.update(kw)
    return cfg.Config(**base)


def cfg_b(**kw):
    """Repository B — a different repository, same source paths."""
    base = dict(
        backend="sftp", ssh_host="hostB", repo_path="/repo-b",
        directories=[cfg.Directory("/rb-src-a"), cfg.Directory("/rb-src-b")],
    )
    base.update(kw)
    return cfg.Config(**base)


class IdentityDefinitionTest(unittest.TestCase):
    """What does and does not change "the thing that was backed up"."""

    def test_different_repository_is_a_different_identity(self):
        self.assertNotEqual(cfg.config_identity(cfg_a()),
                            cfg.config_identity(cfg_b()))
        # host, port, user, key, rclone remote, repo path and password file
        # all select the repository restic actually writes into.
        for field, value in (("ssh_host", "other"), ("ssh_port", 2222),
                             ("ssh_user", "someone"), ("ssh_key", "/tmp/k"),
                             ("repo_path", "/other-repo"),
                             ("password_file", "/tmp/pw"),
                             ("rclone_remote", "remote:repo"),
                             ("backend", "local")):
            changed = dataclasses.replace(cfg_a(), **{field: value})
            if getattr(cfg_a(), field) != value:
                self.assertNotEqual(cfg.config_identity(cfg_a()),
                                    cfg.config_identity(changed),
                                    f"{field} must change the identity")

    def test_source_changes_are_a_different_identity(self):
        one_dir = dataclasses.replace(cfg_a(),
                                      directories=[cfg.Directory("/rb-src-a")])
        self.assertNotEqual(cfg.config_identity(cfg_a()),
                            cfg.config_identity(one_dir))

        disabled = dataclasses.replace(cfg_a(), directories=[
            cfg.Directory("/rb-src-a"),
            cfg.Directory("/rb-src-b", enabled=False)])
        self.assertNotEqual(cfg.config_identity(cfg_a()),
                            cfg.config_identity(disabled))

        excluded = dataclasses.replace(cfg_a(), directories=[
            cfg.Directory("/rb-src-a"),
            cfg.Directory("/rb-src-b", excludes=["*.tmp"])])
        self.assertNotEqual(cfg.config_identity(cfg_a()),
                            cfg.config_identity(excluded))

    def test_source_order_does_not_change_the_identity(self):
        swapped = dataclasses.replace(cfg_a(), directories=[
            cfg.Directory("/rb-src-b"), cfg.Directory("/rb-src-a")])
        self.assertEqual(cfg.config_identity(cfg_a()),
                         cfg.config_identity(swapped))

    def test_scheduling_retention_split_settings_do_not(self):
        """Tweaking when/what-compression/what-retention must not make a
        repository look never backed up."""
        tweaked = dataclasses.replace(
            cfg_a(),
            backup_schedule="interval", backup_interval_hours=12,
            check_interval="weekly", check_day=4,
            retention_enabled=True, retention_days=30,
            compression="auto", split_threshold_gib=10,
            inhibit_sleep=False, ignore_inode=True)
        self.assertEqual(cfg.config_identity(cfg_a()),
                         cfg.config_identity(tweaked))

    def test_identity_is_stable_and_short(self):
        self.assertEqual(cfg.config_identity(cfg_a()),
                         cfg.config_identity(cfg_a()))
        self.assertRegex(cfg.config_identity(cfg_a()), r"^[0-9a-f]{16}$")


class RecordBackupTest(unittest.TestCase):
    """A backup outcome is credited only to the identity that produced it."""

    def test_matching_identity_publishes_the_result(self):
        live = cfg_a()
        out = cfg.record_backup(live, cfg.config_identity(live),
                                ts=5000.0, ok=True, partial=False,
                                total_backup_size=900.0,
                                compressed_backup_size=300.0)
        self.assertEqual(out.last_backup_ts, 5000.0)
        self.assertTrue(out.last_backup_ok)
        self.assertEqual(out.total_backup_size, 900.0)
        self.assertEqual(out.compressed_backup_size, 300.0)

    def test_stale_identity_is_filed_not_published(self):
        """A finished backup of A while the live settings describe B: B is
        NOT marked backed up; A's outcome waits in history."""
        live = cfg_b()  # settings moved to B while A's backup was running
        identity_a = cfg.config_identity(cfg_a())
        out = cfg.record_backup(live, identity_a, ts=5000.0, ok=True,
                                partial=False, total_backup_size=900.0,
                                compressed_backup_size=300.0)
        self.assertEqual(out.last_backup_ts, 0.0)   # B: never backed up
        self.assertEqual(out.total_backup_size, 0.0)
        record = out.backup_history[identity_a]
        self.assertEqual(record.ts, 5000.0)
        self.assertTrue(record.ok)
        self.assertEqual(record.total_backup_size, 900.0)

    def test_stale_record_never_borrows_live_sizes(self):
        """A failed run passes total_backup_size=None; the fallback must use
        the identity's own numbers, not repository B's."""
        live = dataclasses.replace(cfg_b(), total_backup_size=42000.0,
                                   compressed_backup_size=7777.0)
        identity_a = cfg.config_identity(cfg_a())
        out = cfg.record_backup(live, identity_a, ts=6000.0, ok=False,
                                partial=False)
        record = out.backup_history[identity_a]
        self.assertEqual(record.total_backup_size, 0.0)
        self.assertEqual(record.compressed_backup_size, 0.0)
        # and repository B's own bookkeeping stays untouched
        self.assertEqual(out.total_backup_size, 42000.0)
        self.assertEqual(out.compressed_backup_size, 7777.0)

    def test_partial_and_failure_are_not_success(self):
        live = cfg_a()
        identity = cfg.config_identity(live)
        out = cfg.record_backup(live, identity, ts=7000.0, ok=False,
                                partial=True)
        self.assertTrue(out.last_backup_partial)
        self.assertFalse(out.last_backup_ok)
        out2 = cfg.record_backup(live, identity, ts=8000.0, ok=False,
                                 partial=False)
        self.assertFalse(out2.last_backup_partial)
        self.assertFalse(out2.last_backup_ok)


class RecordCheckTest(unittest.TestCase):
    def test_stale_check_does_not_verify_another_repository(self):
        live = cfg_b()
        identity_a = cfg.config_identity(cfg_a())
        out = cfg.record_check(live, identity_a, ts=5000.0, ok_ts=5000.0,
                               subset_current=7)
        self.assertEqual(out.last_check_ts, 0.0)
        self.assertEqual(out.last_check_ok_ts, 0.0)
        self.assertEqual(out.check_subset_current, 1)  # B starts its own rotation
        record = out.check_history[identity_a]
        self.assertEqual(record.ts, 5000.0)
        self.assertEqual(record.ok_ts, 5000.0)
        self.assertEqual(record.subset_current, 7)

    def test_failed_check_keeps_only_the_attempt(self):
        live = cfg_a()
        identity = cfg.config_identity(live)
        out = cfg.record_check(live, identity, ts=5000.0, ok_ts=0.0,
                               subset_current=1)
        self.assertEqual(out.last_check_ts, 5000.0)
        self.assertEqual(out.last_check_ok_ts, 0.0)


class MergeSettingsRebindTest(unittest.TestCase):
    """Switching repositories through the settings dialog."""

    def setUp(self):
        self.backed_up_a = dataclasses.replace(
            cfg_a(),
            last_backup_ts=5000.0, last_backup_ok=True,
            total_backup_size=900.0, compressed_backup_size=300.0,
            last_check_ts=4000.0, last_check_ok_ts=4000.0,
            check_subset_current=3)

    def test_switch_to_never_backed_up_repository_clears_the_ledger(self):
        merged = cfg.merge_settings(self.backed_up_a, cfg_b())
        self.assertEqual(merged.repo_path, "/repo-b")
        self.assertEqual(merged.last_backup_ts, 0.0,
                         "B must not inherit A's success timestamp")
        self.assertEqual(merged.last_check_ts, 0.0)
        self.assertEqual(merged.last_check_ok_ts, 0.0)
        self.assertEqual(merged.check_subset_current, 1)
        self.assertEqual(merged.total_backup_size, 0.0)
        self.assertEqual(merged.compressed_backup_size, 0.0)

    def test_history_survives_the_switch(self):
        merged = cfg.merge_settings(self.backed_up_a, cfg_b())
        identity_a = cfg.config_identity(self.backed_up_a)
        self.assertIn(identity_a, merged.backup_history)
        self.assertIn(identity_a, merged.check_history)
        self.assertEqual(merged.backup_history[identity_a].ts, 5000.0)
        self.assertEqual(merged.check_history[identity_a].subset_current, 3)

    def test_switching_back_restores_the_ledger(self):
        merged = cfg.merge_settings(cfg.merge_settings(self.backed_up_a, cfg_b()),
                                    cfg_a())
        self.assertEqual(merged.repo_path, "/repo-a")
        self.assertEqual(merged.last_backup_ts, 5000.0)
        self.assertTrue(merged.last_backup_ok)
        self.assertEqual(merged.total_backup_size, 900.0)
        self.assertEqual(merged.check_subset_current, 3)

    def test_same_identity_edit_keeps_bookkeeping(self):
        """The issue #4 guarantee still holds: editing the schedule on the
        same repository/sources keeps its timestamps."""
        edited = dataclasses.replace(cfg_a(), backup_interval_hours=12)
        merged = cfg.merge_settings(self.backed_up_a, edited)
        self.assertEqual(merged.backup_interval_hours, 12)
        self.assertEqual(merged.last_backup_ts, 5000.0)
        self.assertEqual(merged.last_check_ok_ts, 4000.0)
        self.assertEqual(merged.check_subset_current, 3)

    def test_directory_path_edit_rebinds_like_a_new_source_set(self):
        renamed = dataclasses.replace(
            cfg_a(), directories=[cfg.Directory("/rb-src-a"),
                                  cfg.Directory("/rb-src-renamed")])
        merged = cfg.merge_settings(self.backed_up_a, renamed)
        self.assertEqual(merged.last_backup_ts, 0.0,
                         "the renamed source set was never backed up")
        identity_a = cfg.config_identity(self.backed_up_a)
        self.assertIn(identity_a, merged.backup_history)


class PersistenceTest(unittest.TestCase):
    """backup_history/check_history ride through the JSON round trip."""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp(prefix="rb-identity-"))
        self._real_dir = cfg.CONFIG_DIR
        self._real_file = cfg.CONFIG_FILE
        cfg.CONFIG_DIR = self._dir
        cfg.CONFIG_FILE = self._dir / "config.json"

    def tearDown(self):
        cfg.CONFIG_DIR = self._real_dir
        cfg.CONFIG_FILE = self._real_file
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_round_trip_keeps_history(self):
        live = cfg.merge_settings(self_identity_config(), cfg_b())
        cfg.save_config(live)
        raw = json.loads(cfg.CONFIG_FILE.read_text())
        self.assertIn("backup_history", raw)
        self.assertIn("check_history", raw)
        loaded = cfg.load_config()
        self.assertEqual(loaded.backup_history, live.backup_history)
        self.assertEqual(loaded.check_history, live.check_history)
        for record in loaded.backup_history.values():
            self.assertIsInstance(record, cfg.BackupRecord)
        for record in loaded.check_history.values():
            self.assertIsInstance(record, cfg.CheckRecord)

    def test_old_config_file_without_history_loads(self):
        raw = dataclasses.asdict(cfg_a())
        raw.pop("backup_history")
        raw.pop("check_history")
        cfg.CONFIG_FILE.write_text(json.dumps(raw))
        loaded = cfg.load_config()
        self.assertEqual(loaded.backup_history, {})
        self.assertEqual(loaded.check_history, {})

    def test_invalid_history_is_rejected_not_silently_accepted(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.validate_config(cfg.Config(backup_history={"x": "not a record"}))

        bad = dataclasses.asdict(cfg_a())
        bad["backup_history"] = {"x": {"ts": -5}}
        with self.assertRaises(cfg.ConfigError):
            cfg.Config.from_dict(bad)


def self_identity_config():
    live = cfg_a(last_backup_ts=5000.0, last_backup_ok=True,
                 total_backup_size=900.0, compressed_backup_size=300.0,
                 last_check_ts=4000.0, last_check_ok_ts=4000.0,
                 check_subset_current=3)
    # publish the live bookkeeping into history first, exactly as an
    # operation completion or a settings save would have done
    return cfg.stash_bookkeeping(live)


class HistoryBoundTest(unittest.TestCase):
    """Trying many repositories must not grow the config file forever."""

    def test_history_is_pruned_to_the_most_recent(self):
        live = cfg.Config()
        for i in range(cfg.MAX_HISTORY_ENTRIES + 6):
            repo = cfg.Config(repo_path=f"/repo-{i}")
            live = cfg.record_backup(live, cfg.config_identity(repo),
                                     ts=1000.0 + i, ok=True, partial=False)
        self.assertLessEqual(len(live.backup_history), cfg.MAX_HISTORY_ENTRIES)
        # the kept entries are the most recent ones
        newest = cfg.config_identity(cfg.Config(repo_path=f"/repo-{cfg.MAX_HISTORY_ENTRIES + 5}"))
        self.assertIn(newest, live.backup_history)
        oldest = cfg.config_identity(cfg.Config(repo_path="/repo-0"))
        self.assertNotIn(oldest, live.backup_history)

    def test_pruning_keeps_both_histories_independent(self):
        live = cfg.Config()
        for i in range(cfg.MAX_HISTORY_ENTRIES + 2):
            repo = cfg.Config(repo_path=f"/repo-{i}")
            live = cfg.record_check(live, cfg.config_identity(repo),
                                    ts=2000.0 + i, ok_ts=2000.0 + i,
                                    subset_current=1)
        self.assertLessEqual(len(live.check_history), cfg.MAX_HISTORY_ENTRIES)


class IndicatorStaleResultTest(unittest.TestCase):
    """End-to-end through the indicator's merge path: the mid-run repository
    switch is logged and the result is filed, not published."""

    def _make_indicator(self, cfg_obj):
        import restic_indicator
        ind = object.__new__(restic_indicator.ResticIndicator)
        ind._cfg = cfg_obj
        ind._log = lambda msg: self.logs.append(msg)
        ind._cfg_lock = threading.Lock()
        ind._saved = []
        ind._save_cfg = lambda: self.saved.append(dataclasses.replace(ind._cfg))
        return ind, restic_indicator

    def setUp(self):
        self.logs = []
        self.saved = []

    def test_backup_result_for_stale_identity_is_filed(self):
        ind, restic_indicator = self._make_indicator(cfg_a())
        identity_a = cfg.config_identity(ind._cfg)
        # the user moves the settings to repository B while the run is live
        with ind._cfg_lock:
            ind._cfg = cfg_b()
        ind._merge_backup_result(identity_a, ts=5000.0, ok=True,
                                 partial=False, total_backup_size=900.0,
                                 compressed_backup_size=300.0)
        self.assertEqual(ind._cfg.last_backup_ts, 0.0)
        self.assertIn(identity_a, ind._cfg.backup_history)
        self.assertTrue(any("not the ones now selected" in m for m in self.logs))
        self.assertTrue(self.saved, "the filed result must be persisted")

    def test_check_result_for_stale_identity_is_filed(self):
        ind, restic_indicator = self._make_indicator(cfg_a())
        identity_a = cfg.config_identity(ind._cfg)
        with ind._cfg_lock:
            ind._cfg = cfg_b()
        ind._merge_check_result(identity_a, ts=5000.0, ok_ts=5000.0,
                                subset_current=4)
        self.assertEqual(ind._cfg.last_check_ts, 0.0)
        self.assertEqual(ind._cfg.check_subset_current, 1)
        self.assertEqual(ind._cfg.check_history[identity_a].subset_current, 4)
        self.assertTrue(any("not the one now selected" in m for m in self.logs))

    def test_matching_identity_has_no_stale_warning(self):
        ind, _ = self._make_indicator(cfg_a())
        identity_a = cfg.config_identity(ind._cfg)
        ind._merge_backup_result(identity_a, ts=5000.0, ok=True,
                                 partial=False)
        self.assertEqual(ind._cfg.last_backup_ts, 5000.0)
        self.assertFalse(any("Settings changed" in m for m in self.logs))


if __name__ == "__main__":
    unittest.main()
