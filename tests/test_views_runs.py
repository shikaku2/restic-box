"""Backup-run grouping and view-ownership tests (issues #7, #8, #9).

#8: bydate/ folders are backup *runs*, not calendar minutes. Every unit of one
    operation shares a restic tag `restic-box-run=<id>`; grouping follows that
    tag. Untagged (pre-feature) snapshots keep the legacy minute fallback.
#7: a covered `/` target must populate the view root, not vanish.
#9: corridor filling stops at another covered target, so the most specific
    owning unit supplies the entries in its own subtree.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_indicator  # noqa: E402
import restic_runner as runner  # noqa: E402
import restic_views as views  # noqa: E402

from tests.fakeproc import FakeStdout  # noqa: E402
from tests.test_views import FakeMount, _local_keys, _snap  # noqa: E402

RUN_A = f"{views.RUN_TAG_PREFIX}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUN_B = f"{views.RUN_TAG_PREFIX}bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _tagged(snap_id, when, paths, tag):
    s = _snap(snap_id, when, paths)
    s["tags"] = [tag]
    return s


class ByDateRunGroupingTest(unittest.TestCase):
    """One operation = one folder, whatever its units' finalize times."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="rb-views-run-")
        self.tmp = Path(self.tmp_dir)
        self.bydate = self.tmp / "bydate"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.bydate.exists():
            views.set_tree_readonly(self.bydate, False)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_one_run_spanning_hours_is_one_folder(self):
        mount = FakeMount(self.tmp / "snapshots", {
            "unita001": ["/src/top.txt"],
            "unitb002": ["/src/big/movie.mkv"],
            "unitc003": ["/other/late.txt"],
        })
        # Units of a single split backup that ran 03:00 → 07:20.
        snaps = [
            _tagged("unita001", "2026-05-01T03:00:00Z", ["/src"], RUN_A),
            _tagged("unitb002", "2026-05-01T05:30:00Z", ["/src/big"], RUN_A),
            _tagged("unitc003", "2026-05-01T07:20:00Z", ["/other"], RUN_A),
        ]
        mount.build_in(self.bydate, "bydate", snaps)

        dates = sorted(d for d in self.bydate.iterdir() if d.is_dir())
        self.assertEqual(len(dates), 1, "a multi-hour run must not fragment into minutes")
        run_dir = dates[0]
        self.assertFalse(any((run_dir / sub).is_dir() and not (run_dir / sub).is_symlink()
                             for sub in ["03-00", "05-30", "07-20"]),
                         "no HH-MM subfolders when a day holds exactly one run")
        self.assertIn("unita001", os.readlink(str(run_dir / "src" / "top.txt")),
                      "the ancestor unit fills its own corridor directories")
        self.assertIn("unitb002", os.readlink(str(run_dir / "src" / "big")))
        self.assertTrue((run_dir / "other" / "late.txt").exists())

    def test_two_runs_in_the_same_minute_stay_separate(self):
        mount = FakeMount(self.tmp / "snapshots", {
            "run1snap": ["/src/one.txt"],
            "run2snap": ["/src/two.txt"],
        })
        snaps = [
            _tagged("run1snap", "2026-05-01T10:00:10Z", ["/src/one.txt"], RUN_A),
            _tagged("run2snap", "2026-05-01T10:00:40Z", ["/src/two.txt"], RUN_B),
        ]
        mount.build_in(self.bydate, "bydate", snaps)

        date, time_key = _local_keys("2026-05-01T10:00:10Z")[0]
        first = self.bydate / date / time_key
        second = self.bydate / date / f"{time_key}_2"
        self.assertTrue(first.is_dir() and second.is_dir(),
                        "two same-minute runs must collide onto suffixed folders")
        self.assertIn("run1snap", os.readlink(str(first / "src" / "one.txt")))
        self.assertIn("run2snap", os.readlink(str(second / "src" / "two.txt")))
        self.assertFalse((first / "src" / "two.txt").exists(),
                         "run 2 must not leak into run 1's folder")

    def test_untagged_snapshots_keep_the_minute_fallback(self):
        mount = FakeMount(self.tmp / "snapshots", {
            "legac001": ["/src/a.txt"],
            "legac002": ["/src/b.txt"],
        })
        snaps = [
            _snap("legac001", "2026-05-01T10:00:10Z", ["/src/a.txt"]),
            _snap("legac002", "2026-05-01T10:00:40Z", ["/src/b.txt"]),
        ]
        mount.build_in(self.bydate, "bydate", snaps)
        date, _ = _local_keys("2026-05-01T10:00:10Z")[0]
        run_dirs = sorted(d for d in (self.bydate / date).iterdir())
        self.assertEqual(len(run_dirs), 1,
                         "legacy untagged same-minute snapshots must still merge")
        self.assertTrue((self.bydate / date / "src" / "a.txt").is_symlink())
        self.assertTrue((self.bydate / date / "src" / "b.txt").is_symlink())

    def test_run_tagged_with_no_tags_field(self):
        """`restic snapshots --json` omits the tags field entirely sometimes."""
        mount = FakeMount(self.tmp / "snapshots", {"untagsnp": ["/src/x.txt"]})
        snaps = [_snap("untagsnp", "2026-05-01T10:00:00Z", ["/src/x.txt"])]
        self.assertEqual(views._run_tag(snaps[0]), None)
        self.assertEqual(views._run_tag(_tagged("x", "t", ["/"], RUN_A)), RUN_A)


class RootTargetTest(unittest.TestCase):
    """#7: a snapshot whose target is / must produce a populated view."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="rb-views-root-")
        self.tmp = Path(self.tmp_dir)
        self.latest = self.tmp / "latest"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.latest.exists():
            views.set_tree_readonly(self.latest, False)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_root_only_snapshot_populates_the_view(self):
        mount = FakeMount(self.tmp / "snapshots", {
            "root0001": ["/top.txt", "/src/inner.txt"],
        })
        snaps = [_snap("root0001", "2026-05-01T10:00:00Z", ["/"])]
        created = mount.build_in(self.latest, "latest", snaps)
        self.assertGreater(created, 0, "a / target must not produce an empty view")
        self.assertTrue((self.latest / "top.txt").is_symlink())
        self.assertIn("root0001", os.readlink(str(self.latest / "top.txt")))
        self.assertTrue((self.latest / "src" / "inner.txt").exists(),
                        "the root snapshot's subtree must be reachable")

    def test_root_with_carved_descendant(self):
        mount = FakeMount(self.tmp / "snapshots", {
            "root0001": ["/top.txt", "/src/rootside.txt"],
            "carved02": ["/src/big/movie.mkv"],
        })
        snaps = [
            _snap("root0001", "2026-05-01T10:00:00Z", ["/"]),
            _snap("carved02", "2026-05-01T10:05:00Z", ["/src/big"]),
        ]
        mount.build_in(self.latest, "latest", snaps)

        big = self.latest / "src" / "big"
        self.assertTrue(big.is_symlink(), "the carved unit links at its own path")
        self.assertIn("carved02", os.readlink(str(big)))
        self.assertTrue((self.latest / "top.txt").is_symlink(),
                        "the root snapshot's own entries must survive alongside")
        self.assertTrue((self.latest / "src" / "rootside.txt").is_symlink())
        self.assertIn("root0001", os.readlink(str(self.latest / "src" / "rootside.txt")))

    def test_runner_ls_children_assembles_root_listing(self):
        canned = ('{"name":"tmp","type":"dir","path":"/tmp",'
                  '"message_type":"node","struct_type":"node"}\n'
                  '{"name":"etc","type":"dir","path":"/etc",'
                  '"message_type":"node","struct_type":"node"}')
        calls = []
        original = runner._query_lines

        def fake(args, cfg, on_line, on_output=None, on_proc=None):
            calls.append(args)
            for line in canned.splitlines():
                on_line(line)
            return 0

        runner._query_lines = fake
        try:
            cfg = cfg_mod.Config(backend="local", repo_path="/tmp/unused")
            children = runner.run_ls_children(cfg, "abcd1234", "/")
        finally:
            runner._query_lines = original
        self.assertEqual(calls[0], ["ls", "--json", "abcd1234", "/"])
        self.assertEqual(children, [("tmp", True), ("etc", True)])


class CorridorOwnershipTest(unittest.TestCase):
    """#9: an ancestor unit must not fill corridors below another unit's target."""

    def test_corridor_walk_stops_at_covered_intermediates(self):
        covered = {"/src": "outer001", "/src/a": "middle02", "/src/a/b": "deep0003"}
        self.assertEqual(views._corridor_nodes("/src", covered), ["/src"])
        self.assertEqual(views._corridor_nodes("/src/a", covered), ["/src/a"])
        self.assertEqual(views._corridor_nodes("/src/a/b", covered), ["/src/a/b"])

    def test_corridor_walk_stops_for_root_over_covered_dirs(self):
        covered = {"/": "root0001", "/src/a": "middle02", "/src/a/b": "deep0003"}
        # "/src" is not covered → the root unit corridor-fills it; "/src/a" is
        # covered → its own unit owns it and everything below.
        self.assertEqual(views._corridor_nodes("/", covered), ["/", "/src"])

    def test_deeply_nested_three_levels_show_inner_snapshot_files(self):
        tmp_dir = tempfile.mkdtemp(prefix="rb-views-deep-")
        tmp = Path(tmp_dir)
        latest = tmp / "latest"
        try:
            mount = FakeMount(tmp / "snapshots", {
                # The outer snapshot is from an older plan generation and still
                # contains /src/a/note.txt — it must NOT win that path.
                "outersrc": ["/src/top.txt", "/src/a/note.txt"],
                "midsrca1": ["/src/a/own.txt"],
                "deepsrc1": ["/src/a/b/deep.bin"],
            })
            snaps = [
                _snap("outersrc", "2026-05-01T08:00:00Z", ["/src"]),
                _snap("midsrca1", "2026-05-01T08:10:00Z", ["/src/a"]),
                _snap("deepsrc1", "2026-05-01T08:20:00Z", ["/src/a/b"]),
            ]
            mount.build_in(latest, "latest", snaps)

            self.assertIn("outersrc", os.readlink(str(latest / "src" / "top.txt")))
            own = latest / "src" / "a" / "own.txt"
            self.assertTrue(own.is_symlink(), "the owning unit must supply its own entries")
            self.assertIn("midsrca1", os.readlink(str(own)))
            residue = latest / "src" / "a" / "note.txt"
            self.assertFalse(residue.exists() or os.path.lexists(str(residue)),
                             "the outer snapshot must not place entries in a subtree "
                             "owned by a more specific unit")
            deep = latest / "src" / "a" / "b"
            self.assertTrue(deep.is_symlink())
            self.assertIn("deepsrc1", os.readlink(str(deep)))
        finally:
            if latest.exists():
                views.set_tree_readonly(latest, False)
            shutil.rmtree(tmp_dir, ignore_errors=True)


class RunTagPlumbingTest(unittest.TestCase):
    """The indicator stamps every unit of an operation with one run tag."""

    def _indicator(self, dirs):
        ind = object.__new__(restic_indicator.ResticIndicator)
        ind._cfg = cfg_mod.Config(inhibit_sleep=False, split_threshold_gib=0,
                                  directories=[cfg_mod.Directory(d) for d in dirs])
        ind._log = lambda msg: None
        ind._set_state = lambda state, msg, progress=-1.0: None
        ind._mount_proc = None
        ind._current_proc = None
        ind._stop_requested = __import__("threading").Event()
        ind._cfg_lock = __import__("threading").Lock()
        ind._last_progress_ts = 0.0
        ind._backup_dirs = []
        ind._backup_item_index = 0
        ind._backup_item_count = 0
        ind._current_backup_done = 0.0
        ind._current_backup_total = 0.0
        ind._run_scanned_so_far = 0.0
        ind._current_unit_scan_size = 0.0
        ind._on_progress = lambda *a: None
        ind._set_current_proc = lambda p: None
        ind._backup_progress_msg = lambda done, total, scanned: ("", 0.0)
        return ind

    def _capture_tags(self, ind):
        tags = []

        def fake_backup(c, d, on_output=None, on_progress=None, on_proc=None,
                        tag=None):
            tags.append(tag)
            return 0, "", 10

        with mock.patch.object(restic_indicator.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(restic_indicator.runner, "run_backup",
                               side_effect=fake_backup), \
             mock.patch.object(restic_indicator.runner, "run_retention_forget",
                               side_effect=lambda c, **k: (0, "")), \
             mock.patch.object(restic_indicator.runner, "run_raw_data_size",
                               return_value=1.0), \
             mock.patch.object(restic_indicator.cfg_mod, "save_config",
                               new=lambda c: None):
            ind._run_backup()
        return tags

    def test_all_units_share_one_run_tag(self):
        ind = self._indicator(["/rb-run-a", "/rb-run-b"])
        tags = self._capture_tags(ind)
        self.assertEqual(len(tags), 2)
        self.assertEqual(tags[0], tags[1])
        self.assertTrue(tags[0].startswith(views.RUN_TAG_PREFIX))
        self.assertGreater(len(tags[0]), len(views.RUN_TAG_PREFIX))

    def test_separate_operations_get_different_run_tags(self):
        ind = self._indicator(["/rb-run-a"])
        first = self._capture_tags(ind)
        second = self._capture_tags(ind)
        self.assertNotEqual(first[0], second[0],
                            "two backup operations must not merge into one run")

    def test_runner_forwards_tag_as_restic_option(self):
        cfg = cfg_mod.Config(backend="local", repo_path="/tmp/unused")

        class FakeProc:
            returncode = 0

            def __init__(self):
                self.stdout = FakeStdout(["{}"])

            def wait(self):
                return 0

        seen = {}

        def fake_popen(cmd, **kwargs):
            seen["cmd"] = cmd
            return FakeProc()

        with mock.patch.object(runner.subprocess, "Popen", new=fake_popen):
            runner.run_backup(cfg, cfg_mod.Directory("/rb-src"),
                              tag=views.RUN_TAG_PREFIX + "cafe42")
        cmd = seen["cmd"]
        # the tag is one exact --tag <value> pair; the .zfs guard (fix #2)
        # appends its own exclusion after it, so match by content, not position
        self.assertEqual(cmd.count("--tag"), 1)
        index = cmd.index("--tag")
        self.assertEqual(cmd[index + 1], views.RUN_TAG_PREFIX + "cafe42")

        with mock.patch.object(runner.subprocess, "Popen", new=fake_popen):
            runner.run_backup(cfg, cfg_mod.Directory("/rb-src"))
        self.assertNotIn("--tag", seen["cmd"])


if __name__ == "__main__":
    unittest.main()
