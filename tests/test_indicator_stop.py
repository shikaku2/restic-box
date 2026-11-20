"""Stop All reliability tests (issue #6).

Cancellation is a shared token checked at every step of an operation: planning,
before each subprocess launch, and before the destructive retention/stats steps.
Every restic process the app starts — including snapshot-browser forget/prune —
is registered so "Stop All" can terminate it, and termination is bounded
(terminate → wait → kill).
"""
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_indicator  # noqa: E402
import restic_split  # noqa: E402


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
    ind._op_queue = queue.Queue()
    ind._queued = set()
    ind._queue_lock = threading.Lock()
    ind._backup_dirs = []
    ind._backup_item_index = 0
    ind._backup_item_count = 0
    ind._current_backup_done = 0.0
    ind._current_backup_total = 0.0
    ind._run_scanned_so_far = 0.0
    ind._run_total_size_estimate = 0.0
    ind._current_unit_scan_size = 0.0
    ind._last_progress_ts = 0.0
    return ind


class FakeProc:
    """Stand-in for a live subprocess.Popen."""

    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False


class StopDuringOperationTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.states = []
        self.saved = []
        self.backup_units = []
        self.retention_calls = []
        self.stats_calls = []
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-stop-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("a.bin", "b.bin"):
            (self.tmp / name).write_bytes(b"x" * 64)

    def _indicator(self, dirs, threshold_gib=0):
        cfg = cfg_mod.Config(
            inhibit_sleep=False,
            retention_enabled=True, retention_years=1,
            split_threshold_gib=threshold_gib,
            directories=[cfg_mod.Directory(d) for d in dirs],
        )
        ind = make_indicator(cfg)
        ind._log = lambda msg: self.logs.append(msg)
        ind._set_state = lambda state, msg, progress=-1.0: self.states.append((state, msg))
        return ind

    def _run(self, ind, rcs, mutate=None):
        calls = []

        def fake_backup(c, d, on_output=None, on_progress=None, on_proc=None,
                        tag=None):
            calls.append(d.path)
            if mutate:
                mutate(len(calls), ind)
            return rcs[len(calls) - 1], "", 10

        def fake_retention(c, on_output=None, on_proc=None):
            self.retention_calls.append(c)
            return 0, ""

        def fake_stats(c, on_output=None, on_proc=None):
            self.stats_calls.append(c)
            return 1.0

        self.backup_units = calls
        patches = (
            mock.patch.object(restic_indicator.GLib, "idle_add",
                              new=lambda fn, *a, **k: fn(*a, **k)),
            mock.patch.object(restic_indicator.runner, "run_backup",
                              side_effect=fake_backup),
            mock.patch.object(restic_indicator.runner, "run_retention_forget",
                              side_effect=fake_retention),
            mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                              side_effect=fake_stats),
            mock.patch.object(restic_indicator.cfg_mod, "save_config",
                              side_effect=lambda c: self.saved.append(c)),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            ind._run_backup()

    def test_stop_requested_before_launch_skips_all_units(self):
        """The pre-launch gate: even with no unit ever having run, a Stop that
        arrived before the loop prevents the first restic process."""
        ind = self._indicator([str(self.tmp)])
        ind._stop_requested.set()
        self._run(ind, [0])
        self.assertEqual(self.backup_units, [])
        self.assertEqual(self.states[-1][0], restic_indicator.BackupState.IDLE)
        self.assertEqual(self.states[-1][1], "Stopped")

    def test_stop_during_planning_starts_no_backup(self):
        """A Stop while split scanning/planning aborts before any launch."""
        ind = self._indicator([str(self.tmp)], threshold_gib=1)
        ind._stop_requested.set()
        with mock.patch.object(restic_split, "CONFIG_DIR", self.tmp / "plans"):
            self._run(ind, [0])
        self.assertEqual(self.backup_units, [])
        self.assertTrue(any("planning" in m.lower() for m in self.logs),
                        f"logs: {self.logs}")
        self.assertEqual(self.states[-1][1], "Stopped")
        self.assertFalse(any("retention" in m.lower() for m in self.logs))

    def test_stop_mid_run_skips_remaining_units_retention_and_stats(self):
        ind = self._indicator([str(self.tmp) + "-a", str(self.tmp) + "-b"])

        def stop_after_first(count, indicator):
            if count == 1:
                indicator._stop_requested.set()

        # unit 1 is killed by Stop All → nonzero rc
        self._run(ind, [1], mutate=stop_after_first)
        self.assertEqual(self.backup_units, [str(self.tmp) + "-a"])
        self.assertEqual(self.retention_calls, [])      # forget --prune NEVER follows an abort
        self.assertEqual(self.stats_calls, [])
        self.assertEqual(self.saved, [])                # no completion bookkeeping
        self.assertEqual(self.states[-1][1], "Stopped")
        self.assertTrue(any("interrupted by Stop" in m for m in self.logs),
                        f"logs: {self.logs}")

    def test_stop_after_ok_unit_skips_next_unit(self):
        ind = self._indicator([str(self.tmp) + "-a", str(self.tmp) + "-b"])

        def stop_after_first(count, indicator):
            if count == 1:
                indicator._stop_requested.set()

        self._run(ind, [0, 0], mutate=stop_after_first)
        self.assertEqual(self.backup_units, [str(self.tmp) + "-a"])
        self.assertEqual(self.retention_calls, [])
        self.assertEqual(self.states[-1][1], "Stopped")


class StopAllProcessHandlingTest(unittest.TestCase):
    def _indicator(self):
        cfg = cfg_mod.Config(inhibit_sleep=False)
        ind = make_indicator(cfg)
        ind._log = lambda msg: None
        return ind

    def test_operation_active_tracks_process_and_queue(self):
        ind = self._indicator()
        self.assertFalse(ind._operation_active())
        ind._current_proc = FakeProc(alive=False)
        self.assertFalse(ind._operation_active())
        ind._current_proc = FakeProc(alive=True)
        self.assertTrue(ind._operation_active())
        ind._current_proc = None
        ind._queued.add("check")
        self.assertTrue(ind._operation_active())

    def test_stop_and_unlock_terminates_with_bounded_escalation(self):
        ind = self._indicator()
        proc = FakeProc(alive=True)
        ind._current_proc = proc
        stopped = []
        with mock.patch.object(restic_indicator.runner, "stop_process",
                               new=lambda p, timeout=10.0: stopped.append(p)):
            ind._stop_and_unlock()
        self.assertTrue(ind._stop_requested.is_set())
        self.assertEqual(stopped, [proc])   # bounded terminate→wait→kill, not bare terminate()
        self.assertFalse(proc.terminated)   # raw terminate() was never used
        self.assertIn("unlock", ind._queued)
        self.assertEqual(ind._op_queue.get_nowait(), ("unlock", False))

    def test_stop_and_unlock_drops_queued_operations(self):
        ind = self._indicator()
        ind._enqueue("backup")
        ind._current_proc = None
        with mock.patch.object(restic_indicator.runner, "stop_process"):
            ind._stop_and_unlock()
        # queue was cleared, then only the unlock remains
        items = []
        while True:
            try:
                items.append(ind._op_queue.get_nowait())
            except queue.Empty:
                break
        self.assertEqual(items, [("unlock", False)])

    def test_quit_sets_cancellation_token(self):
        ind = self._indicator()
        proc = FakeProc(alive=True)
        ind._current_proc = proc
        ind._enqueue("backup")
        stopped = []
        with mock.patch.object(restic_indicator.Gtk, "main_quit", new=lambda: None), \
             mock.patch.object(restic_indicator.runner, "stop_process",
                               new=lambda p, timeout=10.0: stopped.append(p)):
            ind._quit(None)
        self.assertTrue(ind._stop_requested.is_set())   # quit stops, it does not merely exit
        self.assertEqual(stopped, [proc])
        self.assertIsNone(ind._op_queue.get_nowait())   # worker sentinel
        self.assertFalse(ind._queued)                   # queued work abandoned


class SnapshotBrowserRegistrationTest(unittest.TestCase):
    """forget/prune run on their own threads but must register their process
    with the indicator, and refuse to start while an operation owns the repo."""

    def _browser(self, busy=False):
        # SnapshotBrowser derives from Gtk.Window, so object.__new__ is refused;
        # __new__ gives an instance we can drive without real GTK widgets.
        browser = restic_indicator.SnapshotBrowser.__new__(restic_indicator.SnapshotBrowser)
        browser._cfg = cfg_mod.Config(inhibit_sleep=False)
        registered = []
        browser._on_proc = registered.append
        browser._busy = (lambda: busy) if busy is not None else None
        # No indicator attached: acquire/release stay None, which selects the
        # legacy busy-probe path in _refuse_if_busy and makes _release a no-op.
        browser._acquire_lease = None
        browser._release_lease = None
        return browser, registered

    def test_forget_registers_its_process(self):
        browser, registered = self._browser()
        seen = {}

        def fake_forget(cfg, snap_id, prune=True, on_output=None, on_proc=None):
            seen["on_proc"] = on_proc
            seen["prune"] = prune
            return 0, ""

        with mock.patch.object(restic_indicator.runner, "run_forget",
                               new=fake_forget), \
             mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: None):
            browser._do_forget("abc123")
        self.assertEqual(seen["on_proc"], browser._on_proc)  # same registration as backups
        self.assertTrue(seen["prune"])

    def test_prune_registers_its_process(self):
        browser, registered = self._browser()
        seen = {}

        def fake_prune(cfg, on_output=None, on_proc=None):
            seen["on_proc"] = on_proc
            return 0, ""

        class Lbl:
            def set_text(self, _t):
                pass

        browser._snap_lbl = Lbl()
        with mock.patch.object(restic_indicator.runner, "run_prune",
                               new=fake_prune), \
             mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            browser._do_prune()
        self.assertEqual(seen["on_proc"], browser._on_proc)

    def test_busy_guard_refuses_destructive_actions(self):
        browser, _ = self._browser(busy=True)
        dialogs = []

        class FakeDialog:
            def __init__(self, **kwargs):
                dialogs.append(kwargs)

            def format_secondary_text(self, _text):
                pass

            def run(self):
                return "ok"

            def destroy(self):
                pass

        with mock.patch.object(restic_indicator.Gtk, "MessageDialog", new=FakeDialog):
            self.assertTrue(browser._refuse_if_busy())
        self.assertEqual(len(dialogs), 1)

    def test_idle_allows_destructive_actions(self):
        browser, _ = self._browser(busy=False)
        with mock.patch.object(restic_indicator.Gtk, "MessageDialog") as dlg:
            self.assertFalse(browser._refuse_if_busy())
        dlg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
