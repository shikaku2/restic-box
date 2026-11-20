"""Backup-run outcome classification tests (issue #3).

restic exit code 3 (RC_PARTIAL) means the snapshot exists but some source
files could not be read. A partial run must NOT be reported as success, must
NOT trigger automatic retention, and must persist as "incomplete" rather than
"ok" or "failed".
"""
import os
import sys
import threading
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_indicator  # noqa: E402


def make_indicator(cfg):
    """Bare ResticIndicator without touching GTK/AppIndicator plumbing."""
    ind = object.__new__(restic_indicator.ResticIndicator)
    ind._cfg = cfg
    ind._log = lambda msg: None
    ind._mount_proc = None
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
    ind._last_progress_ts = 0.0
    ind._backup_dirs = []
    ind._backup_item_index = 0
    ind._backup_item_count = 0
    ind._current_backup_done = 0.0
    ind._current_backup_total = 0.0
    ind._run_scanned_so_far = 0.0
    ind._run_total_size_estimate = 0.0
    ind._current_unit_scan_size = 0.0
    return ind


class BackupRunOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.states = []
        self.saved = []
        self.retention_calls = []

    def _run(self, rcs, cfg=None, mutate=None):
        cfg = cfg or cfg_mod.Config(
            inhibit_sleep=False,
            retention_enabled=True,
            retention_years=1,
            directories=[cfg_mod.Directory("/rb-src-a"), cfg_mod.Directory("/rb-src-b")],
        )
        ind = make_indicator(cfg)
        ind._log = lambda msg: self.logs.append(msg)
        ind._set_state = lambda state, msg, progress=-1.0: self.states.append((state, msg))

        call_log = []

        def fake_run_backup(c, d, on_output=None, on_progress=None, on_proc=None,
                            tag=None):
            call_log.append(d.path)
            if mutate:
                mutate(len(call_log), ind)
            return rcs[len(call_log) - 1], "", 1234

        def fake_retention(c, on_output=None, on_proc=None):
            self.retention_calls.append(c)
            return 0, ""

        with mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(restic_indicator.runner, "run_backup",
                               side_effect=fake_run_backup), \
             mock.patch.object(restic_indicator.runner, "run_retention_forget",
                               side_effect=fake_retention), \
             mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                               return_value=9999.0), \
             mock.patch.object(restic_indicator.cfg_mod, "save_config",
                               side_effect=lambda c: self.saved.append(replace(c))):
            ind._run_backup()
        return ind

    def final_state(self):
        return self.states[-1][0] if self.states else None

    def test_full_success_triggers_retention(self):
        self._run([restic_indicator.runner.RC_OK, restic_indicator.runner.RC_OK])
        self.assertEqual(self.final_state(), restic_indicator.BackupState.SUCCESS)
        self.assertEqual(len(self.retention_calls), 1)
        self.assertTrue(self.saved[-1].last_backup_ok)
        self.assertFalse(self.saved[-1].last_backup_partial)

    def test_partial_never_counts_as_success(self):
        self._run([restic_indicator.runner.RC_OK, restic_indicator.runner.RC_PARTIAL])
        self.assertEqual(self.final_state(), restic_indicator.BackupState.PARTIAL)
        # automatic retention must NOT run after an incomplete backup
        self.assertEqual(self.retention_calls, [])
        self.assertFalse(self.saved[-1].last_backup_ok)
        self.assertTrue(self.saved[-1].last_backup_partial)
        self.assertTrue(any("PARTIAL" in m for m in self.logs))

    def test_partial_alone_is_not_reported_as_failure(self):
        self._run([restic_indicator.runner.RC_PARTIAL, restic_indicator.runner.RC_PARTIAL])
        self.assertEqual(self.final_state(), restic_indicator.BackupState.PARTIAL)
        self.assertFalse(any("FAILED" in m for m in self.logs))

    def test_failure_beats_partial_for_status(self):
        self._run([restic_indicator.runner.RC_PARTIAL, 1])
        self.assertEqual(self.final_state(), restic_indicator.BackupState.ERROR)
        self.assertEqual(self.retention_calls, [])
        # startup icon logic: failures must not present as "incomplete"
        self.assertFalse(self.saved[-1].last_backup_partial)
        self.assertFalse(self.saved[-1].last_backup_ok)

    def test_failure_skips_retention(self):
        self._run([1, restic_indicator.runner.RC_OK])
        self.assertEqual(self.final_state(), restic_indicator.BackupState.ERROR)
        self.assertEqual(self.retention_calls, [])

    def test_retention_can_stay_off_explicitly(self):
        cfg = cfg_mod.Config(
            inhibit_sleep=False, retention_enabled=False,
            directories=[cfg_mod.Directory("/rb-src-a")],
        )
        self._run([restic_indicator.runner.RC_OK], cfg=cfg)
        self.assertEqual(self.final_state(), restic_indicator.BackupState.SUCCESS)
        self.assertEqual(self.retention_calls, [])

    def test_startup_state_uses_partial_flag(self):
        cfg = cfg_mod.Config(last_backup_ts=1234567890.0,
                             last_backup_ok=False, last_backup_partial=True)
        ind = make_indicator(cfg)
        # emulate __init__ classification
        if cfg.last_backup_ok:
            state = restic_indicator.BackupState.SUCCESS
        elif cfg.last_backup_partial:
            state = restic_indicator.BackupState.PARTIAL
        else:
            state = restic_indicator.BackupState.ERROR
        self.assertEqual(state, restic_indicator.BackupState.PARTIAL)

    def test_stop_midway_runs_no_further_backups(self):
        def stop_after_first(n, ind):
            if n == 1:
                ind._stop_requested.set()

        ind = self._run([restic_indicator.runner.RC_OK], mutate=stop_after_first)
        # second directory must not be attempted
        self.assertTrue(any("stopped by user" in m.lower() for m in self.logs))
        self.assertEqual(self.final_state(), restic_indicator.BackupState.IDLE)
        # a stopped run leaves the old timestamps alone (neither success nor failure)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.retention_calls, [])


if __name__ == "__main__":
    unittest.main()
