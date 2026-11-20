"""Snapshot-browser Forget/Prune are serialised with every other repository
operation (fix #4, restic-box-fixes2.md).

The rule: exactly one holder owns the repository at a time. The indicator
holds an exclusive lease that is RESERVED before any restic process launches
(queued operations too — the worker reserves before dispatching) and every
live process is registered in a central registry, so Stop All terminates all
of them rather than just the newest.

Acceptance coverage:
* Forget and Prune attempted simultaneously → only one starts;
* a destructive browser action while a backup/check holds the lease → refused;
* a queued operation while a browser action runs → it waits, or a Stop All
  cancels it, never a concurrent launch;
* Stop All terminates the browser's process AND a second live process, and
  releases the cancelled lease so the follow-up unlock can run;
* the lease is restored to "free" after success, failure, and cancel.

No network or ssh: every restic entry point here is a fake.
"""
import os
import queue
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_indicator  # noqa: E402


class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def make_indicator(cfg=None):
    """Bare ResticIndicator without touching GTK/AppIndicator plumbing."""
    ind = object.__new__(restic_indicator.ResticIndicator)
    ind._cfg = cfg or cfg_mod.Config(
        backend="local", repo_path="/repo-a",
        directories=[cfg_mod.Directory("/rb-src-a")],
        inhibit_sleep=False, backup_schedule="interval",
        backup_interval_hours=6)
    ind._log = lambda msg: None
    ind._set_state = lambda state, msg, progress=-1.0: None
    ind._save_cfg = lambda: None
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


class FakeDialog:
    """Stands in for Gtk.MessageDialog; records every dialog shown."""

    shown = []
    response = "ok"

    def __init__(self, **kwargs):
        FakeDialog.shown.append(kwargs)

    def format_secondary_text(self, _text):
        pass

    def run(self):
        return FakeDialog.response

    def destroy(self):
        pass


def make_browser(ind, response="ok"):
    """SnapshotBrowser wired to the indicator exactly as _open_snapshot_browser
    does — but without instantiating any GTK widgets."""
    FakeDialog.shown = []
    FakeDialog.response = response
    browser = restic_indicator.SnapshotBrowser.__new__(restic_indicator.SnapshotBrowser)
    browser._cfg = ind._cfg
    browser._on_proc = ind._set_current_proc
    browser._busy = ind._operation_active
    browser._acquire_lease = ind._reserve_browser_op
    browser._release_lease = ind._release_repo
    return browser


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.ind = make_indicator()

    def test_first_holder_wins_and_second_is_refused(self):
        self.assertTrue(self.ind._reserve_repo("forget"))
        self.assertFalse(self.ind._reserve_repo("prune"))
        self.assertFalse(self.ind._reserve_repo("backup"))
        self.ind._release_repo("forget")
        self.assertTrue(self.ind._reserve_repo("prune"))

    def test_release_only_clears_the_owners_own_lease(self):
        self.ind._reserve_repo("forget")
        self.ind._release_repo("prune")        # not the owner: no-op
        self.assertEqual(self.ind._repo_owner, "forget")
        self.ind._release_repo("forget")       # the owner: cleared
        self.assertIsNone(self.ind._repo_owner)

    def test_operation_active_tracks_the_lease(self):
        self.assertFalse(self.ind._operation_active())
        self.ind._reserve_repo("forget")
        self.assertTrue(self.ind._operation_active())


class BrowserRefusalTest(unittest.TestCase):
    """Forget and Prune aimed at the same indicator: only one gets through."""

    def setUp(self):
        self.ind = make_indicator()

    def _refuse(self, browser, op):
        with mock.patch.object(restic_indicator.Gtk, "MessageDialog",
                               new=FakeDialog):
            return browser._refuse_if_busy(op)

    def test_second_destructive_action_is_refused(self):
        forget = make_browser(self.ind)
        prune = make_browser(self.ind)
        self.assertFalse(self._refuse(forget, "forget"))   # claimed
        self.assertTrue(self._refuse(prune, "prune"))      # refused, dialog shown
        self.assertEqual(len(FakeDialog.shown), 1)
        self.assertEqual(self.ind._repo_owner, "forget")

    def test_destructive_action_refused_while_backup_holds_the_repo(self):
        self.ind._reserve_repo("backup")   # the worker would hold this mid-run
        browser = make_browser(self.ind)
        self.assertTrue(self._refuse(browser, "prune"))
        self.assertEqual(self.ind._repo_owner, "backup")  # untouched by refusal

    def test_lease_frees_up_after_the_first_action_completes(self):
        browser = make_browser(self.ind)
        self.assertFalse(self._refuse(browser, "forget"))
        self.ind._release_repo("forget")
        self.assertFalse(self._refuse(browser, "prune"))  # now free

    def test_cancel_at_confirmation_returns_the_lease(self):
        browser = make_browser(self.ind, response="cancel")

        class Model:
            def get_value(self, _it, col):
                return ["abc12345", "abc123", "2026-01-01 00:00:00"][col]

        browser._snap_sel = mock.Mock()
        browser._snap_sel.get_selected.return_value = (Model(), "iter")
        browser._snap_lbl = mock.Mock()
        browser._btn_forget = mock.Mock()

        with mock.patch.object(restic_indicator.Gtk, "MessageDialog",
                               new=FakeDialog):
            browser._on_forget(None)
        # user said Cancel: the action must not keep the repository hostage
        self.assertIsNone(self.ind._repo_owner)
        self.assertFalse(self.ind._operation_active())


class BrowserActionLeaseLifecycleTest(unittest.TestCase):
    """Success, failure and crash all hand the repository back."""

    def setUp(self):
        self.ind = make_indicator()
        self.browser = make_browser(self.ind)

    def test_successful_forget_releases(self):
        self.ind._reserve_repo("forget")
        with mock.patch.object(restic_indicator.runner, "run_forget",
                               new=lambda c, s, **k: (0, "")), \
             mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: None):
            self.browser._do_forget("abc")
        self.assertIsNone(self.ind._repo_owner)

    def test_failing_forget_releases(self):
        self.ind._reserve_repo("forget")
        with mock.patch.object(restic_indicator.runner, "run_forget",
                               new=lambda c, s, **k: (3, "boom")), \
             mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: None):
            self.browser._do_forget("abc")
        self.assertIsNone(self.ind._repo_owner)

    def test_crashing_forget_releases(self):
        self.ind._reserve_repo("forget")

        def explode(*a, **k):
            raise RuntimeError("ssh died")

        with mock.patch.object(restic_indicator.runner, "run_forget",
                               new=explode), \
             mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: None):
            with self.assertRaises(RuntimeError):
                self.browser._do_forget("abc")
        self.assertIsNone(self.ind._repo_owner)

    def test_crashing_prune_releases(self):
        self.ind._reserve_repo("prune")

        def explode(*a, **k):
            raise RuntimeError("sftp died")

        with mock.patch.object(restic_indicator.runner, "run_prune",
                               new=explode):
            with self.assertRaises(RuntimeError):
                self.browser._do_prune()
        self.assertIsNone(self.ind._repo_owner)

    def test_browser_process_is_registered_with_the_indicator(self):
        proc = FakeProc()

        def fake_forget(c, s, prune=True, on_output=None, on_proc=None):
            if on_proc is not None:
                on_proc(proc)          # exactly how run_restic registers it
            return 0, ""

        with mock.patch.object(restic_indicator.runner, "run_forget",
                               new=fake_forget), \
             mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: None):
            self.browser._do_forget("abc")
        # the process went through on_proc into the indicator's registry
        self.assertIn(proc, self.ind._active_procs)
        self.assertIs(self.ind._current_proc, proc)


class StopAllRegistryTest(unittest.TestCase):
    """Stop All must reach EVERY live process, not just the newest slot."""

    def setUp(self):
        self.ind = make_indicator()

    def _stop(self, stopped):
        with mock.patch.object(restic_indicator.runner, "stop_process",
                               new=lambda p, timeout=10.0: stopped.append(p)), \
             mock.patch.object(restic_indicator.runner, "run_unlock",
                               side_effect=lambda c, **k: (0, "")):
            self.ind._stop_and_unlock()

    def test_two_live_processes_are_both_terminated(self):
        forget_proc = FakeProc()      # browser forget launched first
        backup_proc = FakeProc()      # then somehow a second writer appeared
        self.ind._set_current_proc(forget_proc)
        self.ind._set_current_proc(backup_proc)
        stopped = []
        self._stop(stopped)
        self.assertEqual(len(stopped), 2)
        self.assertIn(forget_proc, stopped)
        self.assertIn(backup_proc, stopped)

    def test_dead_processes_are_skipped(self):
        dead = FakeProc(alive=False)
        live = FakeProc()
        self.ind._set_current_proc(dead)
        self.ind._set_current_proc(live)
        stopped = []
        self._stop(stopped)
        self.assertEqual(stopped, [live])

    def test_browser_lease_is_released_so_unlock_can_run(self):
        # a browser prune holds the lease and has a live process
        self.ind._reserve_repo("prune")
        proc = FakeProc()
        self.ind._set_current_proc(proc)
        stopped = []
        self._stop(stopped)
        self.assertEqual(stopped, [proc])
        self.assertIsNone(self.ind._repo_owner)
        self.assertTrue(self.ind._reserve_repo("unlock"))  # free again immediately


class WorkerRespectsTheLeaseTest(unittest.TestCase):
    """A queued backup/check never launches beside a running browser action."""

    def setUp(self):
        self.ind = make_indicator()
        self.launched = []
        self.logs = []
        self.ind._log = self.logs.append
        self.ind._set_state = lambda state, msg, progress=-1.0: None

    def _patches(self, stack):
        def fake_check(c, **k):
            self.launched.append("check")
            return 0, ""
        stack.enter_context(
            mock.patch.object(restic_indicator.GLib, "idle_add",
                              new=lambda fn, *a, **k: fn(*a, **k)))
        stack.enter_context(
            mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                              return_value=(True, "")))
        stack.enter_context(
            mock.patch.object(restic_indicator.runner, "run_check",
                              side_effect=fake_check))

    def test_queued_check_waits_for_the_browser_then_runs(self):
        self.ind._reserve_repo("prune")          # browser owns the repo
        self.ind._op_queue.put(("check", True))
        self.ind._op_queue.put(None)             # sentinel ends the worker after

        def release_later():
            # simulates the browser finishing while the check waits
            import time
            time.sleep(0.3)
            self.ind._release_repo("prune")

        releaser = threading.Thread(target=release_later, daemon=True)
        worker = threading.Thread(target=self.ind._op_worker, daemon=True)
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patches(stack)
            releaser.start()
            worker.start()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.launched, ["check"])
        self.assertIsNone(self.ind._repo_owner)

    def test_stop_while_waiting_for_the_lease_cancels_without_launching(self):
        self.ind._reserve_repo("prune")          # browser keeps owning the repo
        self.ind._op_queue.put(("check", True))
        self.ind._op_queue.put(None)

        def cancel_later():
            # Stop All pressed while the check is parked behind the browser
            import time
            time.sleep(0.3)
            self.ind._stop_requested.set()

        canceller = threading.Thread(target=cancel_later, daemon=True)
        worker = threading.Thread(target=self.ind._op_worker, daemon=True)
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patches(stack)
            canceller.start()
            worker.start()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.launched, [],
                         "a cancelled operation must never launch beside the browser")
        self.assertTrue(any("repository is busy" in m for m in self.logs),
                        self.logs)

    def test_worker_releases_the_lease_after_running(self):
        self.ind._op_queue.put(("check", True))
        self.ind._op_queue.put(None)
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patches(stack)
            self.ind._op_worker()
        self.assertEqual(self.launched, ["check"])
        self.assertIsNone(self.ind._repo_owner)

    def test_crashed_worker_releases_the_lease(self):
        self.ind._op_queue.put(("check", True))
        self.ind._op_queue.put(None)

        def explode(*a, **k):
            raise RuntimeError("restic check exploded")

        with mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(restic_indicator.runner, "ssh_transport_ok",
                               return_value=(True, "")), \
             mock.patch.object(restic_indicator.runner, "run_check",
                               new=explode):
            self.ind._op_worker()
        self.assertIsNone(self.ind._repo_owner)


if __name__ == "__main__":
    unittest.main()
