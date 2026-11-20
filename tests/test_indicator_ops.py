"""Per-operation config snapshot + check attempt/success tracking tests
(issues #4 and #13).

#4: each backup/check works on a private deep copy of the configuration. A
settings save mid-operation (possibly pointing at another repository) must not
redirect the operation's remaining units, retention, or stats — and the
operation's completion must not write stale snapshot settings back over the
user's newer ones.

#13: last_check_ts records the check ATTEMPT (a failure still gates the
schedule; no per-minute retry storm), last_check_ok_ts records the last
SUCCESS, and a successful full check counts toward the regular schedule and
resets the subset rotation.

Also covers review fix #1 (restic-box-remaining-fixes.md): Stop All gates
every follow-on command, including repository statistics after retention.
"""
import contextlib
import copy
import dataclasses
import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
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


class OpSnapshotTest(unittest.TestCase):
    """Issue #4: settings changed mid-operation."""

    def setUp(self):
        self.logs = []
        self.states = []
        self.saved = []
        self.backup_cfgs = []
        self.backup_units = []
        self.retention_cfgs = []
        self.stats_cfgs = []

        self.cfg = cfg_mod.Config(
            inhibit_sleep=False,
            retention_enabled=True, retention_years=1,
            repo_path="/repo-a", ssh_host="hostA",
            directories=[cfg_mod.Directory("/rb-src-a"), cfg_mod.Directory("/rb-src-b")],
        )
        self.ind = make_indicator(self.cfg)
        self.ind._log = lambda msg: self.logs.append(msg)
        self.ind._set_state = lambda state, msg, progress=-1.0: self.states.append((state, msg))

    def _fake_backup(self, c, d, on_output=None, on_progress=None, on_proc=None,
                     tag=None):
        self.backup_cfgs.append(c)
        self.backup_units.append(d.path)
        # Simulate the GTK thread saving brand-new settings while this unit's
        # restic process is running.
        if len(self.backup_units) == 1:
            with self.ind._cfg_lock:
                self.ind._cfg = dataclasses.replace(
                    self.ind._cfg, repo_path="/repo-b", ssh_host="hostB")
        return 0, "", 1234

    def _run(self):
        self.ind._op_queue.put(("backup", False))
        self.ind._op_queue.put(None)
        with mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                               return_value=(True, "")), \
             mock.patch.object(restic_indicator.runner, "run_backup",
                               side_effect=self._fake_backup), \
             mock.patch.object(restic_indicator.runner, "run_retention_forget",
                               side_effect=self._fake_retention), \
             mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                               side_effect=self._fake_stats), \
             mock.patch.object(restic_indicator.cfg_mod, "save_config",
                               side_effect=lambda c: self.saved.append(dataclasses.replace(c))):
            self.ind._op_worker()  # runs synchronously on this thread

    def _fake_retention(self, c, on_output=None, on_proc=None):
        self.retention_cfgs.append(c)
        return 0, ""

    def _fake_stats(self, c, on_output=None, on_proc=None):
        self.stats_cfgs.append(c)
        return 4242.0

    def test_entire_operation_uses_original_snapshot(self):
        self._run()
        self.assertEqual(self.backup_units, ["/rb-src-a", "/rb-src-b"])
        # every unit, retention, and stats call used the SAME snapshot object
        # captured before the settings save — repository A, not B.
        all_cfgs = self.backup_cfgs + self.retention_cfgs + self.stats_cfgs
        self.assertEqual(len(all_cfgs), 4)
        for c in all_cfgs:
            self.assertIs(c, self.backup_cfgs[0])
            self.assertEqual(c.repo_path, "/repo-a")
            self.assertEqual(c.ssh_host, "hostA")

    def test_completion_credits_the_run_own_repository_not_the_new_one(self):
        """Fix #1 (restic-box-fixes2.md): results belong to the identity the
        operation ran against. Repository B must NOT inherit A's success."""
        self._run()
        saved = self.saved[-1]
        # the user's newer settings survived the backup finishing
        self.assertEqual(saved.repo_path, "/repo-b")
        self.assertEqual(saved.ssh_host, "hostB")
        # ...but B was never touched by this run: it shows as never backed up
        self.assertEqual(saved.last_backup_ts, 0.0)
        self.assertEqual(self.ind._cfg.last_backup_ts, 0.0)
        # A's outcome waits in history under A's identity, ready for a save
        # that points the settings back at A.
        identity_a = cfg_mod.config_identity(self.cfg)
        record = saved.backup_history[identity_a]
        self.assertTrue(record.ok)
        self.assertFalse(record.partial)
        self.assertGreater(record.ts, 0)
        self.assertEqual(record.compressed_backup_size, 4242.0)
        # the tray re-derives from bookkeeping: a never-backed-up B is IDLE
        state, msg = self.ind._state_from_bookkeeping()
        self.assertEqual(state, restic_indicator.BackupState.IDLE)
        self.assertEqual(msg, "Never backed up")

    def test_next_operation_sees_new_settings(self):
        self._run()
        self.backup_cfgs.clear()
        self.backup_units.clear()
        self.retention_cfgs.clear()
        self.stats_cfgs.clear()
        self.ind._op_queue.put(("backup", False))
        self.ind._op_queue.put(None)
        with mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                               side_effect=lambda c: ((c.ssh_host == "hostB"),
                                                      "" if c.ssh_host == "hostB"
                                                      else "SSH transport failed: probe")), \
             mock.patch.object(restic_indicator.runner, "run_backup",
                               side_effect=self._fake_backup2), \
             mock.patch.object(restic_indicator.runner, "run_retention_forget",
                               side_effect=lambda c, **k: (0, "")), \
             mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                               return_value=1.0), \
             mock.patch.object(restic_indicator.cfg_mod, "save_config",
                               side_effect=lambda c: self.saved.append(dataclasses.replace(c))):
            self.ind._op_worker()
        # the second operation ran entirely against repository B
        self.assertEqual(self.backup_units, ["/rb-src-a", "/rb-src-b"])
        for c in self.backup_cfgs:
            self.assertEqual(c.repo_path, "/repo-b")

    def _fake_backup2(self, c, d, on_output=None, on_progress=None, on_proc=None,
                      tag=None):
        self.backup_cfgs.append(c)
        self.backup_units.append(d.path)
        return 0, "", 1

    def test_crashed_operation_sets_error_state(self):
        """An operation exception must not leave the tray at RUNNING/SUCCESS."""
        def boom(*a, **k):
            raise RuntimeError("restic exploded")
        self.ind._op_queue.put(("backup", False))
        self.ind._op_queue.put(None)
        with mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                               return_value=(True, "")), \
             mock.patch.object(restic_indicator.runner, "run_backup", side_effect=boom), \
             mock.patch.object(restic_indicator.traceback, "print_exc", new=lambda: None):
            self.ind._op_worker()
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.ERROR)
        self.assertIn("crashed", self.states[-1][1])


class StopDuringRetentionTest(unittest.TestCase):
    """Review fix #1 (restic-box-remaining-fixes.md): the shared cancellation
    token gates EVERY follow-on command. A Stop All that arrives while
    retention is running must prevent the statistics subprocess from ever
    launching, and the operation must end as Stopped, not as success."""

    def setUp(self):
        self.logs = []
        self.states = []
        self.saved = []
        self.stats_cfgs = []
        self.retention_calls = 0

        self.cfg = cfg_mod.Config(
            inhibit_sleep=False,
            retention_enabled=True, retention_years=1,
            repo_path="/repo-a",
            directories=[cfg_mod.Directory("/rb-src-a")],
        )
        self.ind = make_indicator(self.cfg)
        self.ind._log = lambda msg: self.logs.append(msg)
        self.ind._set_state = lambda state, msg, progress=-1.0: self.states.append((state, msg))

    def _run(self, retention_effect, duration_effect=None):
        def fake_backup(c, d, on_output=None, on_progress=None, on_proc=None, tag=None):
            return 0, "", 100
        def fake_retention(c, on_output=None, on_proc=None):
            self.retention_calls += 1
            return retention_effect(c)
        def fake_stats(c, on_output=None, on_proc=None):
            self.stats_cfgs.append(c)
            return 1.0

        patches = [
            mock.patch.object(restic_indicator.GLib, "idle_add",
                              new=lambda fn, *a, **k: fn(*a, **k)),
            mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                              return_value=(True, "")),
            mock.patch.object(restic_indicator.runner, "run_backup",
                              side_effect=fake_backup),
            mock.patch.object(restic_indicator.runner, "run_retention_forget",
                              side_effect=fake_retention),
            mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                              side_effect=fake_stats),
            mock.patch.object(restic_indicator.cfg_mod, "save_config",
                              side_effect=lambda c: self.saved.append(dataclasses.replace(c))),
        ]
        if duration_effect is not None:
            patches.append(mock.patch.object(restic_indicator.runner,
                                             "retention_duration",
                                             side_effect=duration_effect))

        stack = contextlib.ExitStack()
        self.ind._op_queue.put(("backup", False))
        self.ind._op_queue.put(None)
        try:
            for p in patches:
                stack.enter_context(p)
            self.ind._op_worker()
        finally:
            stack.close()

    def test_stop_during_retention_skips_statistics_and_ends_stopped(self):
        # The retention fake behaves like Stop All mid-forget: it sets the
        # shared token (the real handler also terminates the process).
        def stop_during_retention(_c):
            self.ind._stop_requested.set()
            return 1, ""   # forget was terminated -> nonzero rc
        self._run(stop_during_retention)

        self.assertEqual(self.retention_calls, 1)
        self.assertEqual(self.stats_cfgs, [],
                         "no restic statistics subprocess may launch after Stop All")
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.IDLE)
        self.assertIn("Stopped", self.states[-1][1])
        self.assertNotIn(restic_indicator.BackupState.SUCCESS,
                         [s for s, _ in self.states],
                         "an interrupted run must not claim success")
        self.assertTrue(any("statistics skipped" in m for m in self.logs))
        # bookkeeping is not persisted for an interrupted run (same policy as
        # a stop before retention)
        self.assertEqual(self.saved, [])

    def test_pre_stats_gate_catches_stop_when_retention_skipped(self):
        # Stop arrives between the last unit and the statistics launch, with
        # no retention in between (empty duration): the launch-time gate alone
        # must stop the follow-on.
        def stop_during_duration_lookup(_cfg):
            self.ind._stop_requested.set()
            return ""
        self._run(lambda c: (0, ""), duration_effect=stop_during_duration_lookup)

        self.assertEqual(self.retention_calls, 0)
        self.assertEqual(self.stats_cfgs, [])
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.IDLE)
        self.assertIn("Stopped", self.states[-1][1])

    def test_uninterrupted_run_still_launches_stats_after_retention(self):
        # The gates must not be so strict that a normal run loses its stats.
        self._run(lambda c: (0, ""))
        self.assertEqual(self.retention_calls, 1)
        self.assertEqual(len(self.stats_cfgs), 1)
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.SUCCESS)
        self.assertTrue(self.saved[-1].last_backup_ok)


class ConfigSnapshotLockTest(unittest.TestCase):
    """Review fix #5: a private configuration snapshot must be taken while
    holding _cfg_lock — the lock settings saves and bookkeeping updates use.
    Unlocked, a save landing mid-copy can hand an operation a new repository
    with old credentials, or a half-updated Directory list.
    """

    class TrackingLock:
        """A lock that records whether it is currently held, so a copy can be
        asserted to happen strictly inside the critical section."""

        def __init__(self):
            self._inner = threading.Lock()
            self.held = False
            self.entered = 0

        def __enter__(self):
            self._inner.acquire()
            self.held = True
            self.entered += 1
            return self

        def __exit__(self, *_exc):
            self.held = False
            self._inner.release()
            return False

    def setUp(self):
        # backend="local": the SSH pre-flight gate would otherwise try to probe
        # "hostA" for real (fix #4 made that gate an actual ssh invocation).
        self.cfg = cfg_mod.Config(backend="local", repo_path="/repo-a",
                                  ssh_host="hostA",
                                  directories=[cfg_mod.Directory("/rb-src-a")],
                                  inhibit_sleep=False)
        self.ind = make_indicator(self.cfg)
        self.logs = []
        self.ind._log = self.logs.append
        self.ind._set_state = lambda *a, **k: None
        self.ind._set_current_proc = lambda proc: None
        self.ind._skip_if_mounted = lambda op, automatic=False, cfg=None: False
        self.ind._save_cfg = lambda: None
        self.ind._merge_dir_sizes = lambda dirs: None
        self.lock = self.TrackingLock()
        self.ind._cfg_lock = self.lock

        # An unlocked snapshot is exactly the interleaving fix #5 removes, so the
        # copy itself is made to refuse when the lock is not held.
        real_deepcopy = copy.deepcopy

        def strict(obj):
            if not self.lock.held:
                raise AssertionError("configuration snapshot taken without _cfg_lock")
            return real_deepcopy(obj)

        patcher = mock.patch.object(restic_indicator.copy, "deepcopy", new=strict)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _idle(self):
        patcher = mock.patch.object(restic_indicator.GLib, "idle_add",
                                    new=lambda fn, *a, **k: fn(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_worker_snapshot_holds_the_lock(self):
        self._idle()
        checks = []
        patcher = mock.patch.object(restic_indicator.runner, "run_check",
                                    side_effect=lambda c, **k: (checks.append(c), 0, "")[1:])
        patcher.start()
        self.addCleanup(patcher.stop)

        self.ind._op_queue.put(("check", False))
        self.ind._op_queue.put(None)
        self.ind._op_worker()          # strict copy: raises if the gate is unlocked

        self.assertEqual(len(checks), 1)
        self.assertGreaterEqual(self.lock.entered, 1)
        self.assertFalse(self.lock.held, "the lock must not stay held afterwards")

    def test_direct_call_fallbacks_hold_the_lock(self):
        """_run_check / _run_full_check / _run_unlock called without a worker
        snapshot must lock their own copy too."""
        self._idle()
        for runner_name, method in (("run_check", "_run_check"),
                                    ("run_check_full", "_run_full_check"),
                                    ("run_unlock", "_run_unlock")):
            with mock.patch.object(restic_indicator.runner, runner_name,
                                   return_value=(0, "")):
                getattr(self.ind, method)(cfg=None)
        self.assertGreaterEqual(self.lock.entered, 3)
        self.assertFalse(self.lock.held)

    def test_snapshot_is_a_private_deep_copy(self):
        snap = self.ind._snapshot_cfg()
        self.assertIsNot(snap, self.ind._cfg)
        snap.directories[0].path = "/mutated"
        snap.retention_years = 99
        self.assertEqual(self.ind._cfg.directories[0].path, "/rb-src-a")
        self.assertEqual(self.ind._cfg.retention_years, 0)

    def test_save_racing_an_operation_never_mixes_generations(self):
        """Acceptance check: settings saved concurrently with an operation.

        The live config flips between two complete generations (repository +
        host belong together). Every configuration the operation actually used
        must be one whole generation, never a mixture.
        """
        self._idle()
        used = []
        stop = threading.Event()

        def fake_check(c, **kwargs):
            used.append((c.repo_path, c.ssh_host))
            time.sleep(0.002)
            return 0, ""

        def saver():
            while not stop.is_set():
                with self.ind._cfg_lock:
                    current = (self.ind._cfg.repo_path, self.ind._cfg.ssh_host)
                    other = ("/repo-b", "hostB") if current == ("/repo-a", "hostA") \
                        else ("/repo-a", "hostA")
                    self.ind._cfg = dataclasses.replace(
                        self.ind._cfg, repo_path=other[0], ssh_host=other[1])
                time.sleep(0.001)

        patcher = mock.patch.object(restic_indicator.runner, "run_check",
                                    side_effect=fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)

        for _ in range(12):
            self.ind._op_queue.put(("check", True))
        self.ind._op_queue.put(None)

        thread = threading.Thread(target=self.ind._op_worker, daemon=True)
        thread.start()
        saver_thread = threading.Thread(target=saver, daemon=True)
        saver_thread.start()
        thread.join(timeout=10)
        stop.set()
        saver_thread.join(timeout=5)

        self.assertFalse(thread.is_alive(), "worker did not finish")
        self.assertGreater(len(used), 1, "the save must actually race the operations")
        whole = {("/repo-a", "hostA"), ("/repo-b", "hostB")}
        self.assertEqual([g for g in used if g not in whole], [],
                         "an operation received a mixture of two settings saves")

    def test_no_unlocked_snapshot_path_is_left_behind(self):
        src = (Path(__file__).resolve().parent.parent / "restic_indicator.py").read_text(
            encoding="utf-8")
        copies = [ln.strip() for ln in src.splitlines() if "copy.deepcopy(self._cfg)" in ln]
        self.assertEqual(copies, ["return copy.deepcopy(self._cfg)"],
                         "every config snapshot must go through _snapshot_cfg()")


class MergeSettingsTest(unittest.TestCase):
    """Issue #4, second half: a dialog opened before an operation finished
    must not write its stale bookkeeping values back into the config."""

    def test_stale_operation_fields_are_not_restored(self):
        live = cfg_mod.Config(
            repo_path="/repo-a",
            last_backup_ts=5000.0, last_backup_ok=True, last_backup_partial=False,
            last_check_ts=4000.0, last_check_ok_ts=4000.0,
            check_subset_current=3, total_backup_size=1000.0,
            compressed_backup_size=555.0,
            directories=[cfg_mod.Directory("/rb-data", cached_size=777.0)],
        )
        # what the dialog captured when it opened (all zeros), but the user
        # edited the daily backup hour meanwhile
        opened_copy = dataclasses.replace(cfg_mod.Config(), repo_path="/repo-a",
                                          directories=[cfg_mod.Directory("/rb-data")])
        edited = dataclasses.replace(opened_copy, backup_daily_hour=5)

        merged = cfg_mod.merge_settings(live, edited)
        self.assertEqual(merged.backup_daily_hour, 5)          # setting applied
        self.assertEqual(merged.last_backup_ts, 5000.0)        # bookkeeping preserved
        self.assertTrue(merged.last_backup_ok)
        self.assertEqual(merged.last_check_ok_ts, 4000.0)
        self.assertEqual(merged.check_subset_current, 3)
        self.assertEqual(merged.total_backup_size, 1000.0)
        self.assertEqual(merged.compressed_backup_size, 555.0)
        self.assertEqual(merged.directories[0].cached_size, 777.0)

    def test_new_directory_keeps_dialog_size(self):
        live = cfg_mod.Config(directories=[])
        edited = cfg_mod.Config(directories=[cfg_mod.Directory("/rb-fresh", cached_size=42.0)])
        merged = cfg_mod.merge_settings(live, edited)
        self.assertEqual(merged.directories[0].cached_size, 42.0)


class CheckTrackingTest(unittest.TestCase):
    """Issue #13: attempt vs success tracking."""

    def setUp(self):
        self.logs = []
        self.states = []
        self.saved = []

    def _indicator(self, **kw):
        cfg = cfg_mod.Config(inhibit_sleep=False, check_hour=0,
                             check_interval="daily", **kw)
        ind = make_indicator(cfg)
        ind._log = lambda msg: self.logs.append(msg)
        ind._set_state = lambda state, msg, progress=-1.0: self.states.append((state, msg))
        return ind

    def _patch_common(self):
        return (
            mock.patch.object(restic_indicator.GLib, "idle_add",
                              new=lambda fn, *a, **k: fn(*a, **k)),
            mock.patch.object(restic_indicator.cfg_mod, "save_config",
                              side_effect=lambda c: self.saved.append(dataclasses.replace(c))),
        )

    def test_failed_check_records_attempt_and_stops_retry_storm(self):
        ind = self._indicator()
        p1, p2 = self._patch_common()
        with p1, p2, mock.patch.object(restic_indicator.runner, "run_check",
                                       return_value=(1, "")):
            ind._run_check()
        saved = self.saved[-1]
        self.assertGreater(saved.last_check_ts, 0)   # ATTEMPT recorded
        self.assertEqual(saved.last_check_ok_ts, 0.0)  # no success claimed
        # the rest of the day: not due again on the next 60 s tick
        self.assertFalse(ind._check_due())
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.ERROR)

    def test_successful_check_records_attempt_and_success(self):
        ind = self._indicator(check_read_data_subset=True,
                              check_subset_total=10, check_subset_current=3)
        p1, p2 = self._patch_common()
        with p1, p2, mock.patch.object(restic_indicator.runner, "run_check",
                                       return_value=(restic_indicator.runner.RC_OK, "")):
            ind._run_check()
        saved = self.saved[-1]
        self.assertGreater(saved.last_check_ts, 0)
        self.assertEqual(saved.last_check_ok_ts, saved.last_check_ts)
        self.assertEqual(saved.check_subset_current, 4)  # rotation advances on success
        self.assertFalse(ind._check_due())  # counted for today

    def test_full_check_counts_toward_regular_schedule_and_resets_subset(self):
        ind = self._indicator(check_read_data_subset=True,
                              check_subset_total=10, check_subset_current=4)
        p1, p2 = self._patch_common()
        with p1, p2, mock.patch.object(restic_indicator.runner, "run_check_full",
                                       return_value=(restic_indicator.runner.RC_OK, "")):
            ind._run_full_check()
        saved = self.saved[-1]
        self.assertGreater(saved.last_check_ts, 0)       # counts for this slot
        self.assertGreater(saved.last_check_ok_ts, 0)
        self.assertEqual(saved.check_subset_current, 1)  # rotation reset
        self.assertFalse(ind._check_due())  # no regular check right after a full verify

    def test_subset_index_self_heals_when_total_shrinks(self):
        ind = self._indicator(check_read_data_subset=True,
                              check_subset_total=3, check_subset_current=5)
        p1, p2 = self._patch_common()
        with p1, p2, mock.patch.object(restic_indicator.runner, "run_check",
                                       return_value=(restic_indicator.runner.RC_OK, "")):
            ind._run_check()
        saved = self.saved[-1]
        self.assertEqual(saved.check_subset_current, 2)  # reset to 1, then advance
        self.assertTrue(any("Subset index reset" in m for m in self.logs))

    def test_attempt_gating_survives_time(self):
        # Never attempted → due. An attempt earlier today (even a failed one)
        # blocks re-firing; yesterday's failure does not block today's slot —
        # that is the daily retry cadence instead of a per-minute retry storm.
        ind = self._indicator()
        self.assertTrue(ind._check_due())
        ind._cfg = dataclasses.replace(ind._cfg, last_check_ts=time.time())
        self.assertFalse(ind._check_due())
        yst = time.time() - 86400
        ind._cfg = dataclasses.replace(ind._cfg, last_check_ts=yst)
        self.assertTrue(ind._check_due())


if __name__ == "__main__":
    unittest.main()
