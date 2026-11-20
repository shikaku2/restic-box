"""Mount configuration consistency and lifecycle tests (fix #5,
restic-box-fixes2.md).

A mount must belong to ONE repository: the immutable configuration snapshot
taken when the mount starts. Settings may change to a different repository
mid-mount, but restic mount, the snapshot listing, and friendly-view building
must all keep using the original one. Mounting is also mutually exclusive
with the destructive side of everything else:

* a mount refuses to start while a backup/check is queued or running, or a
  browser Forget/Prune holds the repository lease;
* Forget/Prune are refused while a mount is up (documented: unmount first);
* scheduled backups against a mounted repo keep using the existing, logged
  skip (_skip_if_mounted) — a mount can last for hours, so those operations
  skip rather than block forever.

Cleanup is pinned too: mount failure, unmount, and process exit all release
the process, the config snapshot, the spinner, and the menu label.

No network or ssh: mount children are fakes; no real mountpoint is touched.
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


def cfg_repo(name):
    return cfg_mod.Config(backend="local", repo_path=f"/repo-{name}",
                          password_file=f"/pw-{name}", inhibit_sleep=False)


class FakeStdout(list):
    """stdout stand-in: iterable AND closable, like the real pipe."""

    def close(self):
        pass


class FakeProc:
    def __init__(self, lines=(), alive=True):
        self._lines = list(lines)
        self._alive = alive

    @property
    def stdout(self):
        return FakeStdout(self._lines)

    def poll(self):
        return None if self._alive else 0

    def wait(self):
        return 0


class FakeItem:
    def __init__(self):
        self.label = ""
        self.sensitive = True

    def set_label(self, label):
        self.label = label

    def set_sensitive(self, value):
        self.sensitive = value


def make_indicator():
    ind = object.__new__(restic_indicator.ResticIndicator)
    ind._cfg = cfg_repo("a")
    ind._log = lambda msg: None
    ind._mount_proc = None
    ind._mount_cfg = None
    ind._mount_spinner_id = None
    ind._mount_frame = 0
    ind._item_mount = FakeItem()
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
    return ind


class MountSnapshotTest(unittest.TestCase):
    """The mount's whole life — child, listing, views — uses repo A."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-mount-"))
        for name, value in (("_MOUNT_DIR", self.tmp),
                            ("_RAW_MOUNT_PATH", self.tmp / "snapshots"),
                            ("_LATEST_PATH", self.tmp / "latest"),
                            ("_BYDATE_PATH", self.tmp / "bydate")):
            patcher = mock.patch.object(restic_indicator, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ind = make_indicator()
        self.seen_cfgs = []
        self.notified = []
        self.build_done = threading.Event()

    def _patch_common(self):
        patchers = [
            mock.patch.object(restic_indicator, "_notify",
                              new=self.notified.append),
            mock.patch.object(restic_indicator.subprocess, "run",
                              new=lambda *a, **k: None),
            mock.patch.object(restic_indicator.subprocess, "Popen",
                              new=lambda *a, **k: FakeProc()),
            mock.patch.object(restic_indicator.GLib, "idle_add",
                              new=lambda fn, *a, **k: fn(*a, **k)),
            mock.patch.object(restic_indicator.GLib, "timeout_add",
                              new=lambda *a, **k: 1),
            mock.patch.object(restic_indicator.GLib, "source_remove",
                              new=lambda _id: None),
            mock.patch.object(restic_indicator.shutil, "which",
                              new=lambda _exe: "/usr/bin/fusermount3"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return patchers

    def _patch_runner(self, switch_settings_in_flight):
        def fake_start_mount(cfg, mountpoint):
            self.seen_cfgs.append(("start_mount", cfg.repo_path))
            if switch_settings_in_flight:
                # mid-mount the user saves settings pointing at repo B
                self.ind._cfg = cfg_repo("b")
            return FakeProc(lines=["restic rest starting",
                                    "serving on http://localhost:8000/repo"])

        def fake_snapshots(cfg, **k):
            self.seen_cfgs.append(("run_snapshots_json", cfg.repo_path))
            return []

        def fake_ls(cfg, snap_id, path, **k):
            self.seen_cfgs.append(("run_ls_children", cfg.repo_path))
            return []

        def fake_build(builder_name):
            def build(path, mountpath, snaps, ls_children=None, log=None):
                ls_children("snap1", "/some/dir")   # corridors use it too
                self.build_done.set()
                return 0
            return build

        for target, fake in (
                ("start_mount", fake_start_mount),
                ("run_snapshots_json", fake_snapshots),
                ("run_ls_children", fake_ls)):
            patcher = mock.patch.object(restic_indicator.runner, target,
                                        new=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("build_latest", "build_bydate"):
            patcher = mock.patch.object(restic_indicator.views, name,
                                        new=fake_build(name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _wait_build(self):
        self.assertTrue(self.build_done.wait(10),
                        "friendly-view building never ran")

    def _wait_teardown(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.ind._mount_proc is None and self.ind._mount_cfg is None:
                return True
            time.sleep(0.05)
        return False

    def test_mount_backup_snapshots_settings_before_launching(self):
        self._patch_common()
        self._patch_runner(switch_settings_in_flight=True)
        self.ind._mount_backup()          # snapshots repo A here…
        self._wait_build()
        # …every mount-related command still used repo A afterwards…
        stages = [name for name, _repo in self.seen_cfgs]
        self.assertIn("start_mount", stages)
        self.assertIn("run_snapshots_json", stages)
        self.assertIn("run_ls_children", stages)
        offenders = [(name, repo) for name, repo in self.seen_cfgs
                     if repo != "/repo-a"]
        self.assertEqual(offenders, [],
                         f"mount work drifted onto another repo: {self.seen_cfgs}")
        # …and the live settings really did move on to B during the mount.
        self.assertEqual(self.ind._cfg.repo_path, "/repo-b")
        # the finished mount hands its snapshot and process back.
        self.assertTrue(self._wait_teardown())

    def test_view_building_uses_the_mount_snapshot_not_live_settings(self):
        # direct-drive variant: no thread race — build views with snapshot A
        # while the live config already points at B.
        self._patch_common()
        self._patch_runner(switch_settings_in_flight=False)
        self.ind._cfg = cfg_repo("b")
        self.ind._build_friendly_views(cfg_repo("a"))
        self.assertIn(("run_snapshots_json", "/repo-a"), self.seen_cfgs)
        self.assertIn(("run_ls_children", "/repo-a"), self.seen_cfgs)


class MountRefusalTest(unittest.TestCase):
    """A mount never starts beside another repository owner."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-mount-"))
        patcher = mock.patch.object(restic_indicator, "_MOUNT_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("_RAW_MOUNT_PATH", "_LATEST_PATH", "_BYDATE_PATH"):
            patcher = mock.patch.object(restic_indicator, name, self.tmp / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ind = make_indicator()
        self.notified = []
        self.started = []

    def _attempt_mount(self):
        self.notified.clear()
        self.started.clear()
        patchers = [
            mock.patch.object(restic_indicator, "_notify",
                              new=self.notified.append),
            mock.patch.object(restic_indicator.subprocess, "run",
                              new=lambda *a, **k: None),
            mock.patch.object(restic_indicator.shutil, "which",
                              new=lambda _exe: "/usr/bin/fusermount3"),
            mock.patch.object(restic_indicator.GLib, "timeout_add",
                              new=lambda *a, **k: 1),
            mock.patch.object(restic_indicator.runner, "start_mount",
                              new=lambda cfg, mp: self.started.append(cfg)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.ind._mount_backup()

    def test_queued_backup_blocks_mount(self):
        with self.ind._queue_lock:
            self.ind._queued.add("backup")
        self._attempt_mount()
        self.assertEqual(self.started, [])
        self.assertTrue(any("in progress" in m for m in self.notified),
                        self.notified)

    def test_running_process_blocks_mount(self):
        self.ind._current_proc = FakeProc(alive=True)
        self._attempt_mount()
        self.assertEqual(self.started, [])

    def test_browser_lease_blocks_mount(self):
        # the snapshot browser holds the repo for Forget/Prune (fix #4)
        self.ind._repo_owner = "forget"
        self._attempt_mount()
        self.assertEqual(self.started, [])


class BrowserMountExclusionTest(unittest.TestCase):
    """Forget/Prune are refused while a mount serves the repository."""

    def setUp(self):
        self.ind = make_indicator()

    def test_destructive_browser_actions_refused_while_mounted(self):
        self.ind._mount_proc = FakeProc(alive=True)
        self.assertFalse(self.ind._reserve_browser_op("forget"))
        self.assertIsNone(self.ind._repo_owner,
                         "refusal must not claim the lease")
        self.assertFalse(self.ind._reserve_browser_op("prune"))

    def test_lease_still_works_when_unmounted(self):
        self.ind._mount_proc = FakeProc(alive=False)
        self.assertTrue(self.ind._reserve_browser_op("prune"))
        self.assertEqual(self.ind._repo_owner, "prune")


class MountCleanupTest(unittest.TestCase):
    """Failure, unmount, and normal exit all restore process and UI state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-mount-"))
        for name in ("_MOUNT_DIR", "_RAW_MOUNT_PATH", "_LATEST_PATH",
                     "_BYDATE_PATH"):
            patcher = mock.patch.object(restic_indicator, name, self.tmp / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ind = make_indicator()
        idle_calls = []
        patchers = [
            mock.patch.object(restic_indicator.subprocess, "run",
                              new=lambda *a, **k: None),
            mock.patch.object(restic_indicator.subprocess, "Popen",
                              new=lambda *a, **k: FakeProc()),
            mock.patch.object(restic_indicator.GLib, "idle_add",
                              new=lambda fn, *a, **k: idle_calls.append((fn, a))),
            mock.patch.object(restic_indicator.GLib, "source_remove",
                              new=lambda _id: None),
            mock.patch.object(restic_indicator.GLib, "timeout_add",
                              new=lambda *a, **k: 7),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.idle_calls = idle_calls

    def _flush_idle(self):
        for fn, args in self.idle_calls:
            fn(*args)

    def test_start_failure_cleans_up_everything(self):
        def boom(cfg, mountpoint):
            raise RuntimeError("fuse blew up")

        with mock.patch.object(restic_indicator.runner, "start_mount", new=boom):
            self.ind._run_mount(cfg_repo("a"))
        self._flush_idle()
        self.assertIsNone(self.ind._mount_proc)
        self.assertIsNone(self.ind._mount_cfg)
        self.assertIsNone(self.ind._mount_spinner_id)
        self.assertEqual(self.ind._item_mount.label, "Mount Backup")
        self.assertTrue(self.ind._item_mount.sensitive)

    def test_mount_exit_cleans_up_snapshot_and_ui(self):
        # stdout never says "serving": the mount exits before views are ever
        # attempted — teardown must still clear everything deterministically.
        builds = []

        def fake_build(path, mountpath, snaps, ls_children=None, log=None):
            builds.append(path)
            return 0

        proc = FakeProc()

        with mock.patch.object(restic_indicator.runner, "start_mount",
                               new=lambda cfg, mountpoint: proc), \
             mock.patch.object(restic_indicator.views, "build_latest",
                               new=fake_build), \
             mock.patch.object(restic_indicator.views, "build_bydate",
                               new=fake_build):
            snap_cfg = cfg_repo("a")
            self.ind._mount_cfg = snap_cfg      # as _mount_backup does
            self.ind._run_mount(snap_cfg)
        self._flush_idle()
        self.assertEqual(builds, [],
                         "a mount that never served must not build views")
        self.assertIsNone(self.ind._mount_proc)
        self.assertIsNone(self.ind._mount_cfg)
        self.assertIsNone(self.ind._mount_spinner_id)
        self.assertEqual(self.ind._item_mount.label, "Mount Backup")
        self.assertTrue(self.ind._item_mount.sensitive)

    def test_unmount_terminates_the_mount_process(self):
        stopped = []
        proc = FakeProc(alive=True)
        self.ind._mount_proc = proc
        with mock.patch.object(restic_indicator.runner, "stop_process",
                               new=lambda p, timeout=10.0: stopped.append(p)):
            self.ind._unmount_backup()
        self.assertEqual(stopped, [proc],
                         "unmount must use the bounded stop helper")

    def test_unmount_tolerates_an_already_dead_mount(self):
        stopped = []
        self.ind._mount_proc = FakeProc(alive=False)
        with mock.patch.object(restic_indicator.runner, "stop_process",
                               new=lambda p, timeout=10.0: stopped.append(p)):
            self.ind._unmount_backup()
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()
