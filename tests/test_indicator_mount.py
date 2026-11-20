"""Mount stdout-draining tests (issue #14 mount hazard).

While a restic mount serves a repository it keeps writing request/log lines to
stdout. Building the friendly views runs repository and ls commands *through
that mount*, which takes time. If view building ran between two stdout reads,
the 64K pipe buffer would fill, the child would block on a full pipe, and the
parent would block waiting for the child — the deadlock Python documents for
subprocess pipes. _run_mount() must therefore drain stdout continuously on its
own thread and run post-mount work elsewhere.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_indicator  # noqa: E402

CHILD_LINES = 40          # lines the mount child produces while we build views
CHILD_LINE_DELAY = 0.010  # ms between child writes (a serving mount is chatty)
BUILD_BLOCK = 0.250       # how long view building takes in this scenario


class FakeMountProc:
    def __init__(self, state):
        self._state = state
        self.returncode = 0

    def __iter__(self):
        return iter(self._lines())

    # subprocess.Popen exposes stdout as a file object: iterate it
    @property
    def stdout(self):
        return self._lines()

    def _lines(self):
        state = self._state
        yield "restic rest starting\n"
        yield "serving on http://localhost:8000/repo\n"
        for i in range(CHILD_LINES):
            # A real mount blocks when its stdout pipe is full: it can only
            # write this line if the parent has been keeping up.
            time.sleep(CHILD_LINE_DELAY)
            state["consumed"].append((i, time.monotonic()))
            yield f"GET /repo/data/chunk{i:03d} 200\n"

    def wait(self):
        return self.returncode


class FakeItem:
    def __init__(self):
        self.label = None
        self.sensitive = None

    def set_label(self, text):
        self.label = text

    def set_sensitive(self, value):
        self.sensitive = value


def make_indicator(state, idle_calls):
    ind = object.__new__(restic_indicator.ResticIndicator)
    ind._cfg = None
    ind._mount_proc = None
    ind._item_mount = FakeItem()
    ind._cleanup_stale_mount = lambda: None
    ind._stop_mount_spinner = lambda: None

    log_lock = threading.Lock()

    def _log(text):
        with log_lock:
            state["log"].append((text, threading.current_thread().name))

    ind._log = _log
    return ind


class MountDrainTest(unittest.TestCase):
    def _run(self):
        state = {"consumed": [], "log": [], "drain_thread": None,
                 "build_threads": [], "consumed_during_build": None,
                 "build_span": None}
        idle_calls = []
        ind = make_indicator(state, idle_calls)

        real_ready = None

        def fake_mount_ready(_mount_cfg=None):
            """Stands in for _build_friendly_views + UI updates: a slow job."""
            state["build_threads"].append(threading.current_thread().name)
            start = time.monotonic()
            time.sleep(BUILD_BLOCK)
            end = time.monotonic()
            state["build_span"] = (start, end)
            state["consumed_during_build"] = sum(
                1 for _i, ts in state["consumed"] if start <= ts <= end
            )

        ind._mount_ready = fake_mount_ready

        def start_mount(cfg, mountpoint):
            proc = FakeMountProc(state)
            # remember which thread the drain loop iterates the child's stdout on
            orig_lines = proc._lines
            def traced_lines():
                state["drain_thread"] = threading.current_thread().name
                return orig_lines()
            proc._lines = traced_lines
            return proc

        with mock.patch.object(restic_indicator.runner, "start_mount", new=start_mount), \
             mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: idle_calls.append((fn, a, k))):
            ind._run_mount(cfg_mod.Config(backend="local", repo_path="/repo-mount"))

        return state, idle_calls, ind

    def test_child_stdout_is_drained_while_views_are_built(self):
        state, _idle, _ind = self._run()

        # The whole point: while view building is busy, mount output keeps
        # flowing. Before the fix the build ran inline between two reads, so
        # nothing was consumed during it and the pipe could fill up.
        self.assertIsNotNone(state["consumed_during_build"])
        self.assertGreaterEqual(
            state["consumed_during_build"], 10,
            "the drain loop must keep reading while post-mount work runs "
            f"(only {state['consumed_during_build']} of {CHILD_LINES} lines "
            "were consumed during the build window)")
        self.assertEqual(len(state["consumed"]), CHILD_LINES,
                         "every child line was read; none left in the pipe")

    def test_post_mount_work_does_not_run_on_the_drain_thread(self):
        state, _idle, _ind = self._run()
        self.assertIsNotNone(state["drain_thread"])
        self.assertEqual(len(state["build_threads"]), 1)
        self.assertNotEqual(state["build_threads"][0], state["drain_thread"],
                            "_mount_ready must not block the stdout drain loop")

    def test_build_runs_once_even_if_serving_is_logged_twice(self):
        state, _idle, _ind = self._run()
        self.assertEqual(len(state["build_threads"]), 1,
                         "duplicate 'serving' lines must not retrigger view building")

    def test_no_line_is_lost_and_all_reach_the_log(self):
        state, _idle, _ind = self._run()
        logged = [text for text, _ in state["log"]]
        mount_lines = [ln for ln in logged if ln.startswith("[mount]")]
        self.assertEqual(len(mount_lines), CHILD_LINES + 2)
        self.assertEqual(logged[-1], "Unmounted.",
                         "the mount teardown must be reported after the drained output")

    def test_ui_state_is_restored_after_mount_ends(self):
        _state, idle_calls, ind = self._run()
        for fn, args, _kwargs in idle_calls:
            fn(*args)
        self.assertEqual(ind._item_mount.label, "Mount Backup")
        self.assertTrue(ind._item_mount.sensitive)


if __name__ == "__main__":
    unittest.main()
