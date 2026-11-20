"""Attempt-vs-success bookkeeping tests (fix #9, restic-box-fixes2.md).

Fix #9 says the tray must be able to answer "what happened last?" honestly:

* a scheduled backup/check that never launched (failed SSH preflight, a busy
  repository, an active mount) must be *visible*, not silently covered by the
  previous success;
* a failure must not erase the time of the last run that really succeeded;
* success, partial, failed, cancelled and skipped must read differently;
* and none of it may move ``last_backup_ts`` / ``last_check_ts``, because those
  are what the schedule waits on — fix #3's backoff owns retrying sooner.

The suite never touches ``~/.config/restic-box``: persistence here goes to a
throwaway directory, and no test connects anywhere.
"""
import copy
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_indicator  # noqa: E402

#: Fixed "yesterday"-style timestamps, so assertions are about *which* stamp a
#: call moved rather than about what the clock happened to read.
T1 = 1_700_000_000.0
T2 = T1 + 3600.0
T3 = T2 + 3600.0


def backed_up_cfg(**over) -> cfg_mod.Config:
    """A configuration whose last backup and last check both fully succeeded."""
    cfg = cfg_mod.Config(
        backend="local", repo_path="/repo-a", ssh_host="", password_file="/tmp/rb-pw",
        directories=[cfg_mod.Directory("/rb-src-a")],
        inhibit_sleep=False, backup_schedule="interval", backup_interval_hours=6,
        last_backup_ts=T1, last_backup_ok=True, last_backup_partial=False,
        last_backup_status=cfg_mod.OUTCOME_SUCCESS,
        last_backup_status_ts=T1, last_backup_ok_ts=T1,
        last_check_ts=T1, last_check_ok_ts=T1,
        last_check_status=cfg_mod.OUTCOME_SUCCESS, last_check_status_ts=T1,
        total_backup_size=1024.0, compressed_backup_size=512.0,
    )
    return replace(cfg, **over) if over else cfg


def make_indicator(cfg):
    """Bare ResticIndicator without touching GTK/AppIndicator plumbing."""
    ind = object.__new__(restic_indicator.ResticIndicator)
    ind._cfg = cfg
    ind._log = lambda msg: None
    ind._mount_proc = None
    ind._mount_cfg = None
    ind._current_proc = None
    ind._stop_requested = threading.Event()
    ind._backoff_until = {}
    ind._backoff_streak = {}
    ind._backoff_lock = threading.Lock()
    ind._active_op = None
    ind._repo_lock = threading.Lock()
    ind._repo_owner = None
    ind._active_procs = []
    ind._cfg_lock = threading.Lock()
    ind._backup_dirs = []
    ind._backup_item_index = 0
    ind._backup_item_count = 0
    ind._current_backup_done = 0.0
    ind._current_backup_total = 0.0
    ind._run_scanned_so_far = 0.0
    ind._run_total_size_estimate = 0.0
    ind._current_unit_scan_size = 0.0
    ind._last_progress_ts = 0.0
    ind._op_queue = queue.Queue()
    ind._queued = set()
    ind._queue_lock = threading.Lock()
    return ind


class TempConfigDir(unittest.TestCase):
    """Point save_config/load_config at a throwaway directory (never the user's)."""

    def setUp(self):
        self._real_dir = cfg_mod.CONFIG_DIR
        self._real_file = cfg_mod.CONFIG_FILE
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-attempt-"))
        cfg_mod.CONFIG_DIR = self.tmp
        cfg_mod.CONFIG_FILE = self.tmp / "config.json"

    def tearDown(self):
        cfg_mod.CONFIG_DIR = self._real_dir
        cfg_mod.CONFIG_FILE = self._real_file
        shutil.rmtree(self.tmp, ignore_errors=True)


def identity_of(cfg) -> str:
    return cfg_mod.config_identity(cfg)


class AttemptRecordTest(unittest.TestCase):
    """restic_config.record_attempt / record_backup / record_check contracts."""

    def setUp(self):
        self.cfg = backed_up_cfg()
        self.ident = identity_of(self.cfg)

    def _skip(self, cfg=None, identity=None, op="backup", ts=T2, reason=None):
        return cfg_mod.record_attempt(
            cfg if cfg is not None else self.cfg,
            identity if identity is not None else self.ident,
            op=op, ts=ts, status=cfg_mod.OUTCOME_SKIPPED,
            reason=reason if reason is not None else "connection unreachable: ssh timed out")

    def test_a_skip_leaves_the_scheduling_stamp_alone(self):
        """last_backup_ts is what the schedule waits on; a run that never reached
        the repository must not push the next attempt a whole interval away."""
        out = self._skip()
        self.assertEqual(out.last_backup_ts, T1)
        self.assertEqual(out.last_check_ts, T1)
        self.assertEqual(out.last_backup_ok_ts, T1)

    def test_a_skip_is_reported_as_this_attempts_outcome(self):
        out = self._skip()
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(out.last_backup_status_ts, T2)
        self.assertIn("unreachable", out.last_backup_reason)
        # the previous success is still the record's success, too
        self.assertEqual(out.backup_history[self.ident].ok_ts, T1)

    def test_a_cancelled_attempt_also_leaves_the_stamps_alone(self):
        out = cfg_mod.record_attempt(self.cfg, self.ident, op="backup", ts=T2,
                                     status=cfg_mod.OUTCOME_CANCELLED,
                                     reason="stopped by user")
        self.assertEqual(out.last_backup_ts, T1)
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_CANCELLED)
        self.assertEqual(out.last_backup_ok_ts, T1)

    def test_a_check_skip_is_filed_with_the_checks(self):
        out = self._skip(op="check_full")
        self.assertEqual(out.last_check_ts, T1)
        self.assertEqual(out.last_check_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(out.last_check_status_ts, T2)
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_SUCCESS)

    def test_a_run_that_reached_the_repository_is_not_an_attempt(self):
        for status in (cfg_mod.OUTCOME_SUCCESS, cfg_mod.OUTCOME_PARTIAL):
            with self.assertRaises(cfg_mod.ConfigError):
                cfg_mod.record_attempt(self.cfg, self.ident, op="backup", ts=T2,
                                       status=status)

    def test_recording_helpers_refuse_the_attempts_that_never_launched(self):
        for status in (cfg_mod.OUTCOME_SKIPPED, cfg_mod.OUTCOME_CANCELLED):
            with self.assertRaises(cfg_mod.ConfigError):
                cfg_mod.record_backup(self.cfg, self.ident, ts=T2, ok=False,
                                      partial=False, status=status)
            with self.assertRaises(cfg_mod.ConfigError):
                cfg_mod.record_check(self.cfg, self.ident, ts=T2, ok_ts=T1,
                                     subset_current=1, status=status)

    def test_unlock_records_nothing(self):
        """Unlock has no outcome worth showing the user."""
        out = cfg_mod.record_attempt(self.cfg, self.ident, op="unlock", ts=T2,
                                     status=cfg_mod.OUTCOME_SKIPPED, reason="x")
        self.assertEqual(out, self.cfg)

    def test_an_attempt_says_nothing_about_a_different_repository(self):
        other = replace(self.cfg, repo_path="/repo-b")
        other_ident = identity_of(other)
        self.assertNotEqual(other_ident, self.ident)
        out = self._skip(identity=other_ident)
        # the live config still describes /repo-a, and its stamps are its own
        self.assertEqual(out.last_backup_ts, T1)
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_SUCCESS)
        # /repo-b gets its own record, with no invented success timestamp
        record = out.backup_history[other_ident]
        self.assertEqual(record.status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(record.ok_ts, 0.0)
        self.assertEqual(record.ts, 0.0)

    def test_a_skip_is_worth_remembering_even_with_no_run_yet(self):
        cfg = replace(backed_up_cfg(last_backup_ts=0.0, last_backup_ok_ts=0.0,
                                    last_backup_status=cfg_mod.OUTCOME_NONE,
                                    last_backup_status_ts=0.0))
        out = self._skip(cfg=cfg)
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertIn(identity_of(cfg), out.backup_history)

    def test_a_failure_keeps_the_last_success(self):
        out = cfg_mod.record_backup(self.cfg, self.ident, ts=T2, ok=False, partial=False,
                                    status=cfg_mod.OUTCOME_FAILED,
                                    reason="1 of 2 backup units failed")
        self.assertEqual(out.last_backup_ts, T2)          # the attempt counts
        self.assertEqual(out.last_backup_ok_ts, T1)       # the success survives
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_FAILED)
        self.assertIn("units failed", out.last_backup_reason)

    def test_a_partial_run_keeps_the_last_success_too(self):
        out = cfg_mod.record_backup(self.cfg, self.ident, ts=T2, ok=False, partial=True,
                                    status=cfg_mod.OUTCOME_PARTIAL,
                                    reason="1 of 2 backup units had unreadable files")
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_PARTIAL)
        self.assertEqual(out.last_backup_ok_ts, T1)

    def test_a_success_moves_attempt_and_success_together(self):
        out = cfg_mod.record_backup(self.cfg, self.ident, ts=T3, ok=True, partial=False,
                                    status=cfg_mod.OUTCOME_SUCCESS,
                                    total_backup_size=2048.0,
                                    compressed_backup_size=1024.0)
        self.assertEqual(out.last_backup_ts, T3)
        self.assertEqual(out.last_backup_ok_ts, T3)
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_SUCCESS)
        self.assertEqual(out.last_backup_reason, "")

    def test_the_old_call_shape_still_classifies_itself(self):
        """Callers that pass ok/partial without a status are not left blank."""
        out = cfg_mod.record_backup(self.cfg, self.ident, ts=T2, ok=False, partial=True)
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_PARTIAL)
        out = cfg_mod.record_backup(self.cfg, self.ident, ts=T2, ok=False, partial=False)
        self.assertEqual(out.last_backup_status, cfg_mod.OUTCOME_FAILED)

    def test_a_failed_check_keeps_its_last_success(self):
        out = cfg_mod.record_check(self.cfg, self.ident, ts=T2, ok_ts=self.cfg.last_check_ok_ts,
                                   subset_current=self.cfg.check_subset_current,
                                   status=cfg_mod.OUTCOME_FAILED,
                                   reason="restic check returned rc=1")
        self.assertEqual(out.last_check_ts, T2)
        self.assertEqual(out.last_check_ok_ts, T1)
        self.assertEqual(out.last_check_status, cfg_mod.OUTCOME_FAILED)
        self.assertIn("rc=1", out.last_check_reason)

    def test_the_history_entry_carries_the_reason_too(self):
        out = self._skip()
        record = out.backup_history[self.ident]
        self.assertEqual(record.status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(record.status_ts, T2)
        self.assertEqual(record.reason, "connection unreachable: ssh timed out")

    def test_an_attempt_survives_switching_repositories_and_back(self):
        """Fix #1's stash/rebind pair must not lose fix #9's warning."""
        skipped = self._skip()
        moved = cfg_mod.merge_settings(skipped, replace(skipped, repo_path="/repo-b"))
        self.assertEqual(moved.last_backup_status, cfg_mod.OUTCOME_NONE)
        self.assertEqual(moved.last_backup_ts, 0.0)
        self.assertEqual(moved.last_backup_status_ts, 0.0)
        back = cfg_mod.merge_settings(moved, replace(moved, repo_path="/repo-a"))
        self.assertEqual(back.last_backup_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(back.last_backup_ts, T1)      # still not "backed up" today
        self.assertEqual(back.last_backup_ok_ts, T1)   # and the real success stands
        self.assertIn("unreachable", back.last_backup_reason)


class LegacyConfigAdoptionTest(unittest.TestCase):
    """A config.json written before fix #9 has no attempt fields at all."""

    def test_a_pre_fix_backup_attempt_is_reinterpreted(self):
        raw = {"backend": "local", "repo_path": "/repo-a",
               "directories": [{"path": "/rb-src-a"}],
               "last_backup_ts": T1, "last_backup_ok": False, "last_backup_partial": True}
        cfg = cfg_mod.Config.from_dict(raw)
        self.assertEqual(cfg.last_backup_status, cfg_mod.OUTCOME_PARTIAL)
        self.assertEqual(cfg.last_backup_status_ts, T1)
        self.assertEqual(cfg.last_backup_ok_ts, 0.0)   # it never claimed a success

    def test_a_pre_fix_success_becomes_a_success_timestamp(self):
        raw = {"backend": "local", "repo_path": "/repo-a",
               "directories": [{"path": "/rb-src-a"}],
               "last_backup_ts": T1, "last_backup_ok": True}
        cfg = cfg_mod.Config.from_dict(raw)
        self.assertEqual(cfg.last_backup_status, cfg_mod.OUTCOME_SUCCESS)
        self.assertEqual(cfg.last_backup_ok_ts, T1)

    def test_a_pre_fix_check_that_failed_stays_failed(self):
        """Old check records already separated attempt from success."""
        raw = {"backend": "local", "repo_path": "/repo-a",
               "directories": [{"path": "/rb-src-a"}],
               "last_check_ts": T2, "last_check_ok_ts": T1}
        cfg = cfg_mod.Config.from_dict(raw)
        self.assertEqual(cfg.last_check_status, cfg_mod.OUTCOME_FAILED)
        self.assertEqual(cfg.last_check_ok_ts, T1)

    def test_pre_fix_history_records_gain_a_status(self):
        ident = cfg_mod.config_identity(cfg_mod.Config(
            backend="local", repo_path="/repo-a", directories=[cfg_mod.Directory("/x")]))
        raw = {"backend": "local", "repo_path": "/repo-a",
               "directories": [{"path": "/x"}],
               "backup_history": {ident: {"ts": T1, "ok": False, "partial": False}},
               "check_history": {ident: {"ts": T1, "ok_ts": T1}}}
        cfg = cfg_mod.Config.from_dict(raw)
        self.assertEqual(cfg.backup_history[ident].status, cfg_mod.OUTCOME_FAILED)
        self.assertEqual(cfg.backup_history[ident].status_ts, T1)
        self.assertEqual(cfg.check_history[ident].status, cfg_mod.OUTCOME_SUCCESS)

    def test_what_a_config_already_stored_is_never_reinterpreted(self):
        raw = {"backend": "local", "repo_path": "/repo-a",
               "directories": [{"path": "/rb-src-a"}],
               "last_backup_ts": T1, "last_backup_ok": True,
               "last_backup_status": cfg_mod.OUTCOME_SKIPPED,
               "last_backup_status_ts": T2, "last_backup_ok_ts": T1,
               "last_backup_reason": "connection unreachable: down"}
        cfg = cfg_mod.Config.from_dict(raw)
        self.assertEqual(cfg.last_backup_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(cfg.last_backup_status_ts, T2)
        self.assertEqual(cfg.last_backup_ts, T1)

    def test_an_unknown_outcome_is_rejected(self):
        for bad in ("succeeded", "Cancelled", 7):
            raw = {"backend": "local", "repo_path": "/repo-a",
                   "directories": [{"path": "/rb-src-a"}],
                   "last_backup_status": bad}
            with self.assertRaises(cfg_mod.ConfigError):
                cfg_mod.Config.from_dict(raw)

    def test_an_unknown_outcome_in_a_history_record_is_rejected(self):
        ident = cfg_mod.config_identity(cfg_mod.Config(
            backend="local", repo_path="/repo-a", directories=[cfg_mod.Directory("/x")]))
        raw = {"backend": "local", "repo_path": "/repo-a",
               "directories": [{"path": "/x"}],
               "backup_history": {ident: {"ts": T1, "status": "half-done"}}}
        with self.assertRaises(cfg_mod.ConfigError):
            cfg_mod.Config.from_dict(raw)

    def test_the_status_timestamp_must_be_sane(self):
        raw = {"backend": "local", "repo_path": "/repo-a",
               "directories": [{"path": "/rb-src-a"}],
               "last_backup_status": cfg_mod.OUTCOME_SKIPPED,
               "last_backup_status_ts": -5}
        with self.assertRaises(cfg_mod.ConfigError):
            cfg_mod.Config.from_dict(raw)

    def test_backup_outcome_understands_a_pre_fix_config(self):
        """In-memory too: the tray must read an old config the same way."""
        legacy = cfg_mod.Config(last_backup_ts=T1, last_backup_ok=False,
                                last_backup_partial=True)
        status, attempt, ok, _reason = cfg_mod.backup_outcome(legacy)
        self.assertEqual(status, cfg_mod.OUTCOME_PARTIAL)
        self.assertEqual(attempt, T1)
        self.assertEqual(ok, 0.0)

    def test_merge_settings_does_not_replay_a_stale_attempt(self):
        """A settings dialog opened before the skip must not write its own
        (older) copy of the outcome back over the live one."""
        live = self._skipped()
        stale = replace(live, last_backup_status=cfg_mod.OUTCOME_SUCCESS,
                        last_backup_status_ts=T1, last_backup_reason="")
        edited = replace(stale, backup_interval_hours=12)
        merged = cfg_mod.merge_settings(live, edited)
        self.assertEqual(merged.backup_interval_hours, 12)     # the setting landed
        self.assertEqual(merged.last_backup_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(merged.last_backup_ts, T1)

    def _skipped(self):
        cfg = backed_up_cfg()
        return cfg_mod.record_attempt(cfg, identity_of(cfg), op="backup", ts=T2,
                                      status=cfg_mod.OUTCOME_SKIPPED,
                                      reason="connection unreachable: down")


class AttemptPersistenceTest(TempConfigDir):
    """The warning has to survive the disk: a restart must not forget it."""

    def _skipped(self):
        cfg = backed_up_cfg()
        return cfg_mod.record_attempt(cfg, identity_of(cfg), op="backup", ts=T2,
                                      status=cfg_mod.OUTCOME_SKIPPED,
                                      reason="connection unreachable: ssh timed out")

    def test_a_skipped_attempt_round_trips(self):
        cfg_mod.save_config(self._skipped())
        loaded = cfg_mod.load_config()
        self.assertEqual(loaded.last_backup_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(loaded.last_backup_status_ts, T2)
        self.assertEqual(loaded.last_backup_ts, T1)
        self.assertEqual(loaded.last_backup_ok_ts, T1)
        self.assertEqual(loaded.last_backup_reason,
                         "connection unreachable: ssh timed out")

    def test_the_history_record_round_trips(self):
        cfg = self._skipped()
        cfg_mod.save_config(cfg)
        loaded = cfg_mod.load_config()
        record = loaded.backup_history[identity_of(cfg)]
        self.assertEqual(record.status, cfg_mod.OUTCOME_SKIPPED)
        self.assertEqual(record.status_ts, T2)
        self.assertEqual(record.ok_ts, T1)
        self.assertEqual(record.ts, T1)


class TrayAttemptDisplayTest(TempConfigDir):
    """The tray: what the bookkeeping says is what the status line says."""

    def setUp(self):
        super().setUp()
        self.logs = []
        self.states = []
        self.saved = []
        self.notifications = []
        self.cfg = backed_up_cfg()
        self.ind = make_indicator(self.cfg)
        self.ind._log = self.logs.append
        self.ind._set_state = lambda state, msg, progress=-1.0: \
            self.states.append((state, msg))
        self.ind._save_cfg = self._save
        self.cfg_file = cfg_mod.CONFIG_FILE

    def _save(self):
        self.saved.append(copy.deepcopy(self.ind._cfg))
        cfg_mod.save_config(self.ind._cfg)

    def _idle(self):
        return mock.patch.object(restic_indicator.GLib, "idle_add",
                                 new=lambda fn, *a, **k: fn(*a, **k))

    def _no_launch(self):
        def boom(*a, **k):
            raise AssertionError("a restic command launched after the preflight failed")
        return [mock.patch.object(restic_indicator.runner, name, side_effect=boom)
                for name in ("run_backup", "run_check", "run_check_full",
                             "run_retention_forget", "run_raw_data_size", "run_unlock")]

    def _fail_preflight(self, why="ssh: connect: Connection timed out", op="backup"):
        self.ind._op_queue.put((op, True))
        self.ind._op_queue.put(None)
        patches = [mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                                     side_effect=lambda c: (False, why))] + self._no_launch()
        with self._idle():
            for p in patches:
                p.start()
            try:
                self.ind._op_worker()
            finally:
                for p in patches:
                    p.stop()

    def _successful_backup(self):
        def fake_run_backup(c, d, on_output=None, on_progress=None, on_proc=None,
                            tag=None):
            return 0, "", 1234
        with self._idle(), \
                mock.patch.object(restic_indicator.runner, "run_backup",
                                  side_effect=fake_run_backup), \
                mock.patch.object(restic_indicator.runner, "run_retention_forget",
                                  side_effect=lambda c, **kw: (0, "")), \
                mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                                  return_value=1234.0):
            self.ind._run_backup(automatic=True, cfg=self.ind._cfg)

    def test_a_skip_after_a_success_is_not_shown_as_that_success(self):
        self._fail_preflight()
        state, msg = self.states[-1]
        self.assertEqual(state, restic_indicator.BackupState.ERROR)
        self.assertIn("skipped", msg.lower())
        self.assertIn("unreachable", msg.lower())
        self.assertIn("last successful backup", msg)   # names the real success
        self.assertNotIn("Last backup OK", msg)

    def test_the_skip_never_moves_the_schedule_stamp(self):
        self._fail_preflight()
        self.assertEqual(self.ind._cfg.last_backup_ts, T1)
        self.assertEqual(self.ind._cfg.last_backup_ok_ts, T1)
        # still due by schedule: fix #3's backoff owns retrying sooner, not this
        self.assertTrue(self.ind._backup_due())

    def test_the_warning_is_persisted(self):
        self._fail_preflight()
        self.assertTrue(self.saved, "a skipped scheduled run must be written down")
        self.assertEqual(self.saved[-1].last_backup_status, cfg_mod.OUTCOME_SKIPPED)

    def test_the_warning_survives_a_bookkeeping_refresh(self):
        self._fail_preflight()
        self.states.clear()
        self.ind._refresh_bookkeeping_display()      # as saving settings does
        state, msg = self.states[-1]
        self.assertEqual(state, restic_indicator.BackupState.ERROR)
        self.assertIn("skipped", msg.lower())

    def test_the_warning_is_what_a_restart_would_show(self):
        self._fail_preflight()
        live_state, live_msg = self.ind._state_from_bookkeeping()
        self.assertTrue(self.cfg_file.exists(), "the warning was never saved")
        reloaded = cfg_mod.load_config()          # from the file just written
        restart = make_indicator(reloaded)
        self.assertEqual(restart._state_from_bookkeeping(), (live_state, live_msg))

    def test_a_later_success_clears_the_warning(self):
        self._fail_preflight()
        self.states.clear()
        self._successful_backup()
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.SUCCESS)
        cfg = self.ind._cfg
        self.assertEqual(cfg.last_backup_status, cfg_mod.OUTCOME_SUCCESS)
        self.assertGreater(cfg.last_backup_ts, T1)
        self.assertEqual(cfg.last_backup_ok_ts, cfg.last_backup_ts)
        self.assertEqual(cfg.last_backup_reason, "")

    def test_the_five_outcomes_read_differently(self):
        cases = [
            (cfg_mod.OUTCOME_SUCCESS, restic_indicator.BackupState.SUCCESS, "OK"),
            (cfg_mod.OUTCOME_PARTIAL, restic_indicator.BackupState.PARTIAL, "INCOMPLETE"),
            (cfg_mod.OUTCOME_FAILED, restic_indicator.BackupState.ERROR, "FAILED"),
            (cfg_mod.OUTCOME_SKIPPED, restic_indicator.BackupState.ERROR, "skipped"),
            (cfg_mod.OUTCOME_CANCELLED, restic_indicator.BackupState.IDLE, "stopped"),
        ]
        seen = set()
        for status, expected_state, word in cases:
            cfg = replace(self.cfg, last_backup_status=status,
                          last_backup_status_ts=T2,
                          last_backup_reason="" if status == cfg_mod.OUTCOME_SUCCESS
                          else f"because {status}")
            state, msg = make_indicator(cfg)._state_from_bookkeeping()
            self.assertEqual(state, expected_state, f"{status}: {msg}")
            self.assertIn(word, msg, f"{status}: {msg}")
            seen.add(word)
        self.assertEqual(len(seen), len(cases), "every outcome must read differently")

    def test_a_stopped_run_still_writes_no_bookkeeping(self):
        def stop_after_first(c, d, on_output=None, on_progress=None, on_proc=None,
                             tag=None):
            self.ind._stop_requested.set()
            return 0, "", 1234
        with self._idle(), \
                mock.patch.object(restic_indicator.runner, "run_backup",
                                  side_effect=stop_after_first), \
                mock.patch.object(restic_indicator.runner, "run_retention_forget",
                                  side_effect=lambda *a, **k: (_ for _ in ()).throw(
                                      AssertionError("retention ran after a Stop"))), \
                mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                                  side_effect=AssertionError("statistics ran after a Stop")):
            self.ind._run_backup(automatic=True, cfg=self.ind._cfg)
        self.assertEqual(self.saved, [], "a stopped run must not save bookkeeping")
        self.assertEqual(self.states[-1], (restic_indicator.BackupState.IDLE, "Stopped"))
        self.assertEqual(self.ind._cfg.last_backup_status, cfg_mod.OUTCOME_CANCELLED)
        self.assertEqual(self.ind._cfg.last_backup_ts, T1)
        self.assertEqual(self.ind._cfg.last_backup_ok_ts, T1)

    def test_a_stopped_run_reads_as_stopped_not_as_the_previous_success(self):
        with self._idle(), mock.patch.object(
                restic_indicator.runner, "run_backup",
                side_effect=lambda *a, **k: self.ind._stop_requested.set() or (143, "", 0)):
            self.ind._run_backup(automatic=True, cfg=self.ind._cfg)
        state, msg = self.ind._state_from_bookkeeping()
        self.assertEqual(state, restic_indicator.BackupState.IDLE)
        self.assertIn("stopped", msg.lower())
        self.assertIn("last successful backup", msg)

    def test_a_check_skip_after_a_success_is_reported(self):
        self._fail_preflight(op="check")
        state, msg = self.states[-1]
        self.assertEqual(state, restic_indicator.BackupState.ERROR)
        self.assertIn("Check skipped", msg)
        self.assertEqual(self.ind._cfg.last_check_ts, T1)
        self.assertEqual(self.ind._cfg.last_check_ok_ts, T1)
        self.assertTrue(self.ind._check_due())

    def test_a_terminated_check_is_stopped_not_failed(self):
        """Stop All terminating restic makes the exit code meaningless: filing it
        as a repository failure would blame the repository for the user's Stop."""
        self.ind._stop_requested.set()
        with self._idle(), mock.patch.object(
                restic_indicator.runner, "run_check",
                side_effect=lambda c, **kw: (143, "")):
            self.ind._run_check(automatic=True, cfg=self.ind._cfg)
        self.assertEqual(self.states[-1], (restic_indicator.BackupState.IDLE, "Stopped"))
        self.assertEqual(self.saved, [])
        cfg = self.ind._cfg
        self.assertEqual(cfg.last_check_status, cfg_mod.OUTCOME_CANCELLED)
        self.assertEqual(cfg.last_check_ts, T1)
        self.assertEqual(cfg.last_check_ok_ts, T1)

    def test_a_mount_blocking_a_scheduled_backup_is_recorded(self):
        class Mount:
            def poll(self):
                return None
        self.ind._mount_proc = Mount()
        with self._idle(), mock.patch.object(restic_indicator.runner, "run_backup",
                                             side_effect=AssertionError(
                                                 "backup launched during a mount")):
            self.ind._run_backup(automatic=True, cfg=self.ind._cfg)
        cfg = self.ind._cfg
        self.assertEqual(cfg.last_backup_status, cfg_mod.OUTCOME_SKIPPED)
        self.assertIn("mount", cfg.last_backup_reason)
        self.assertEqual(cfg.last_backup_ts, T1)
        self.assertTrue(self.saved)
        state, msg = self.ind._state_from_bookkeeping()
        self.assertEqual(state, restic_indicator.BackupState.ERROR)
        self.assertIn("skipped", msg.lower())

    def test_a_manual_attempt_the_mount_refuses_is_not_recorded(self):
        class Mount:
            def poll(self):
                return None
        self.ind._mount_proc = Mount()
        with self._idle(), mock.patch.object(restic_indicator, "_notify",
                                             side_effect=self.notifications.append):
            self.ind._run_backup(automatic=False, cfg=self.ind._cfg)
        self.assertTrue(self.notifications, "the user must be told directly")
        self.assertEqual(self.saved, [])
        self.assertEqual(self.ind._cfg.last_backup_status, cfg_mod.OUTCOME_SUCCESS)

    def test_a_failed_check_is_not_hidden_by_a_backup_success(self):
        cfg = replace(self.cfg, last_check_status=cfg_mod.OUTCOME_FAILED,
                      last_check_ts=T2, last_check_status_ts=T2, last_check_ok_ts=T1,
                      last_check_reason="restic check returned rc=1")
        state, msg = make_indicator(cfg)._state_from_bookkeeping()
        self.assertEqual(state, restic_indicator.BackupState.ERROR)
        self.assertIn("Check FAILED", msg)
        self.assertIn("last successful check", msg)

    def test_a_routine_check_success_does_not_outrank_a_partial_backup(self):
        cfg = replace(self.cfg, last_backup_status=cfg_mod.OUTCOME_PARTIAL,
                      last_backup_ts=T1, last_backup_status_ts=T1, last_backup_ok_ts=T1,
                      last_check_status=cfg_mod.OUTCOME_SUCCESS,
                      last_check_ts=T3, last_check_status_ts=T3, last_check_ok_ts=T3)
        state, msg = make_indicator(cfg)._state_from_bookkeeping()
        self.assertEqual(state, restic_indicator.BackupState.PARTIAL)
        self.assertIn("INCOMPLETE", msg)

    def test_a_config_that_never_attempted_anything_says_so(self):
        state, msg = make_indicator(cfg_mod.Config())._state_from_bookkeeping()
        self.assertEqual(state, restic_indicator.BackupState.IDLE)
        self.assertEqual(msg, "Never backed up")

    def test_a_crashed_operation_is_recorded_as_a_failure(self):
        self.ind._op_queue.put(("backup", False))
        self.ind._op_queue.put(None)
        with self._idle(), \
                mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                                  return_value=(True, "")), \
                mock.patch.object(restic_indicator.runner, "run_backup",
                                  side_effect=RuntimeError("restic exploded")), \
                mock.patch.object(restic_indicator.traceback, "print_exc",
                                  new=lambda: None):
            self.ind._op_worker()
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.ERROR)
        self.assertIn("crashed", self.states[-1][1])
        cfg = self.ind._cfg
        self.assertEqual(cfg.last_backup_status, cfg_mod.OUTCOME_FAILED)
        self.assertEqual(cfg.last_backup_ts, T1)       # crashed: it is not "backed up"
        self.assertEqual(cfg.last_backup_ok_ts, T1)    # and the old success still stands


if __name__ == "__main__":
    unittest.main()
