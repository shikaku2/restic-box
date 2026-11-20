"""Cancellation and retry-scheduling consistency tests (fix #3,
restic-box-fixes2.md).

The shared cancellation token must cover the WHOLE operation lifecycle —
including the connection preflight — and failed or cancelled automatic
operations must not re-fire on the next 60-second scheduler tick. They wait
out a retry backoff instead: exponential for failed preflight, fixed for a
user Stop. Manual triggers are never gated.

Acceptance coverage:
* stop during SSH preflight → no backup/check command launches;
* stop during a check → the registered process is terminated and nothing is
  re-queued;
* cancel a scheduled backup → the next tick does not re-queue it;
* failed preflight → exponential backoff; success clears it;
* the retention/stats gates (already covered in test_indicator_ops) remain.

No network or ssh: the probe itself is always a fake here.
"""
import contextlib
import os
import queue
import sys
import threading
import time
import unittest
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


class WorkerBase(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.states = []
        self.launches = []
        self.cfg = cfg_mod.Config(
            backend="local", repo_path="/repo-a",
            directories=[cfg_mod.Directory("/rb-src-a")],
            inhibit_sleep=False, backup_schedule="interval",
            backup_interval_hours=6,
        )
        self.ind = make_indicator(self.cfg)
        self.ind._log = self.logs.append
        self.ind._set_state = lambda state, msg, progress=-1.0: \
            self.states.append((state, msg))
        self.ind._save_cfg = lambda: None

    def _idle(self):
        return mock.patch.object(restic_indicator.GLib, "idle_add",
                                 new=lambda fn, *a, **k: fn(*a, **k))

    def _no_launch(self):
        """Every real command must explode if the worker reaches one."""
        def boom(*a, **k):
            raise AssertionError("a restic command launched after Stop")
        return [mock.patch.object(restic_indicator.runner, name, side_effect=boom)
                for name in ("run_backup", "run_check", "run_check_full",
                             "run_retention_forget", "run_raw_data_size",
                             "run_unlock")]

    def _run_worker(self, patches):
        stack = contextlib.ExitStack()
        try:
            for p in patches:
                stack.enter_context(p)
            self.ind._op_queue.put(None)
            self.ind._op_worker()
        finally:
            stack.close()


class StopDuringPreflightTest(WorkerBase):
    def test_stop_during_preflight_prevents_every_command(self):
        def probe(_cfg):
            # behaves like Stop All pressed while the ssh probe is mid-flight
            self.ind._stop_requested.set()
            return True, ""
        self.ind._op_queue.put(("backup", True))
        patches = [mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                                     side_effect=probe)] + self._no_launch()
        self._run_worker([self._idle()] + patches)

        self.assertEqual(self.launches, [])
        self.assertTrue(any("nothing was launched" in m for m in self.logs),
                        self.logs)
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.IDLE)
        # and the cancelled backup is not re-queued on the next tick
        self.assertFalse(self._tick_requeues("backup"))

    def test_stop_before_reachable_preflight_also_blocks_the_launch(self):
        def probe(_cfg):
            self.ind._stop_requested.set()
            return False, "host down"  # stop AND failure: stop wins
        self.ind._op_queue.put(("check", True))
        patches = [mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                                     side_effect=probe)] + self._no_launch()
        self._run_worker([self._idle()] + patches)
        self.assertFalse(self._tick_requeues("check"))

    def _tick_requeues(self, op):
        """Run the scheduler exactly like the 60s timer would and look for a
        freshly queued operation."""
        due = {"backup": self.ind._backup_due, "check": self.ind._check_due}[op]
        sched = {"backup": self.ind._schedule_backup,
                 "check": self.ind._schedule_check}[op]
        before = due()
        sched()
        with self.ind._queue_lock:
            queued = op in self.ind._queued
        return before and queued


class PreflightFailureBackoffTest(WorkerBase):
    def _fail_preflight(self, why="ssh: connect: Connection timed out"):
        self.ind._op_queue.put(("backup", True))
        with self._idle(), mock.patch.object(
                restic_indicator.runner, "ssh_transport_ok",
                side_effect=lambda c: (False, why)):
            self.ind._op_queue.put(None)
            self.ind._op_worker()

    def test_failed_preflight_does_not_requeue_on_the_next_tick(self):
        self._fail_preflight()
        self.assertTrue(self.ind._backup_due())  # still due by schedule
        self.ind._schedule_backup()              # the 60s tick
        with self.ind._queue_lock:
            self.assertNotIn("backup", self.ind._queued,
                             "a failed preflight must not re-fire this tick")

    def test_consecutive_failures_back_off_exponentially(self):
        delays = []
        base = time.time()
        for _ in range(4):
            self.ind._apply_retry_backoff("backup", "preflight")
            delays.append(self.ind._backoff_until["backup"] - base)
        self.assertGreaterEqual(delays[0], 60)
        self.assertGreater(delays[1], delays[0] * 1.5)
        self.assertGreater(delays[2], delays[1] * 1.5)
        self.assertLessEqual(max(delays), restic_indicator.BACKOFF_CAP_SECONDS + 1)

    def test_check_and_full_check_share_the_backoff_group(self):
        self.ind._apply_retry_backoff("check_full", "preflight")
        self.assertTrue(self.ind._backoff_active("check"))
        self.assertFalse(self.ind._backoff_active("backup"))

    def test_unreachable_host_shows_an_honest_error_state(self):
        self._fail_preflight()
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.ERROR)
        self.assertIn("unreachable", self.states[-1][1].lower())

    def test_getting_through_preflight_clears_the_backoff(self):
        self._fail_preflight()
        self.assertTrue(self.ind._backoff_active("backup"))
        # a later manual run reaches the actual command: streak broken
        def fake_backup(c, d, **kw):
            self.launches.append(d.path)
            return 0, "", 10
        self.ind._op_queue.put(("backup", False))
        with self._idle(), mock.patch.object(
                restic_indicator.runner, "ssh_transport_ok",
                return_value=(True, "")), \
             mock.patch.object(restic_indicator.runner, "run_backup",
                               side_effect=fake_backup), \
             mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                               return_value=1.0):
            self.ind._op_queue.put(None)
            self.ind._op_worker()
        self.assertFalse(self.ind._backoff_active("backup"))
        self.assertEqual(self.launches, ["/rb-src-a"])


class CancelledOperationNotRequeuedTest(WorkerBase):
    def test_stopped_backup_is_not_requeued_by_the_next_tick(self):
        # a scheduled backup is queued and in flight
        self.ind._op_queue.put(("backup", True))
        with self.ind._queue_lock:
            self.ind._queued.add("backup")
        self.ind._active_op = "backup"

        stopped = []

        class FakeProc:
            def poll(self):
                return None

        def fake_stop(proc):
            stopped.append(proc)

        with mock.patch.object(restic_indicator.runner, "stop_process",
                               new=fake_stop), \
             mock.patch.object(restic_indicator.runner, "run_unlock",
                               side_effect=lambda c, **k: (0, "")):
            self.ind._current_proc = FakeProc()
            self.ind._stop_and_unlock()

        self.assertEqual(len(stopped), 1, "the running process must be terminated")
        self.assertTrue(self.ind._backup_due())
        self.ind._schedule_backup()  # the next 60s tick
        with self.ind._queue_lock:
            self.assertNotIn("backup", self.ind._queued,
                             "a cancelled backup must not re-queue immediately")

    def test_stopped_check_is_terminated_and_paused(self):
        self.ind._op_queue.put(("check", True))
        with self.ind._queue_lock:
            self.ind._queued.add("check")
        self.ind._active_op = "check"

        stopped = []

        class FakeProc:
            def poll(self):
                return None

        with mock.patch.object(restic_indicator.runner, "stop_process",
                               new=lambda p: stopped.append(p)), \
             mock.patch.object(restic_indicator.runner, "run_unlock",
                               side_effect=lambda c, **k: (0, "")):
            self.ind._current_proc = FakeProc()
            self.ind._stop_and_unlock()

        self.assertEqual(len(stopped), 1)
        self.ind._schedule_check()
        with self.ind._queue_lock:
            self.assertNotIn("check", self.ind._queued)

    def test_manual_trigger_is_never_gated_by_backoff(self):
        self.ind._apply_retry_backoff("backup", "preflight")
        self.assertTrue(self.ind._backoff_active("backup"))
        self.ind._trigger_backup()  # user picked "Backup now" from the menu
        with self.ind._queue_lock:
            self.assertIn("backup", self.ind._queued)

    def test_queued_duplicates_are_cancelled_too(self):
        self.ind._op_queue.put(("backup", True))
        self.ind._op_queue.put(("check", True))
        with self.ind._queue_lock:
            self.ind._queued.update({"backup", "check"})
        with mock.patch.object(restic_indicator.runner, "run_unlock",
                               side_effect=lambda c, **k: (0, "")):
            self.ind._stop_and_unlock()
        # both drained operations are paused, not just the running one
        self.assertTrue(self.ind._backoff_active("backup"))
        self.assertTrue(self.ind._backoff_active("check"))
        with self.ind._queue_lock:
            self.assertEqual(self.ind._queued, {"unlock"},
                             "only the unlock survives a Stop All")


if __name__ == "__main__":
    unittest.main()
