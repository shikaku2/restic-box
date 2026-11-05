"""Tests for the friendly mount views (latest/, bydate/).

The bug these lock down: split units nest — a "rest" unit covers /mnt/storage
while a carved unit covers /mnt/storage/user/games — so one backup-target path
is an *ancestor* of another. The old builder symlinked the ancestor and then had
to create the deeper symlink *inside* the read-only FUSE tree, which fails
(EROFS/EPERM) and aborted the whole view build, leaving latest/ half-built and
bydate/ missing.

The fake mount mimics `restic mount`'s ids/<8-char-id>/<absolute tree path>
layout and is chmod'ed read-only during the build, so any attempt to write into
it fails exactly like the real one.

Note the placement rule these tests assume: a unit target becomes one symlink at
its own path. When that path is a directory, its contents are read *through* the
link; only overlapping prefixes get real local directories.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import restic_runner as runner  # noqa: E402
import restic_views as views  # noqa: E402
from restic_config import Config  # noqa: E402

STORAGE = "/mnt/storage"
NESTED = f"{STORAGE}/user/games/collection"          # a carved unit, nested in the rest unit
LOOSE = f"{STORAGE}/user/games/collection/game001.dat"


def _snap(snap_id: str, when: str, paths: list[str]) -> dict:
    """A snapshot entry as `restic snapshots --json` reports it (ids are 8 chars)."""
    return {"id": snap_id, "short_id": snap_id, "time": when, "paths": paths}


def _local_keys(*times: str) -> list[tuple[str, str]]:
    """(date, HH-MM) folder keys a snapshot lands in — bydate/ uses local time."""
    out = []
    for t in times:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone()
        out.append((dt.strftime("%Y-%m-%d"), dt.strftime("%H-%M")))
    return out


def _listing(root: Path) -> set[str]:
    out = set()
    for dirpath, dirs, files in os.walk(str(root)):
        for name in dirs + files:
            out.add(os.path.relpath(os.path.join(dirpath, name), str(root)))
    return out


class FakeMount:
    """Read-only stand-in for restic's FUSE mount: ids/<id>/<absolute tree path>."""

    def __init__(self, root: Path, spec: dict[str, list[str]]):
        self.root = root
        self.trees: dict[str, dict[str, bool]] = {}
        for sid, files in spec.items():
            entries: dict[str, bool] = {}
            for path in files:
                parts = [c for c in path.split("/") if c]
                for i in range(1, len(parts)):
                    entries["/" + "/".join(parts[:i])] = True
                entries[path] = False
            self.trees[sid] = entries
            for path, is_dir in entries.items():
                target = self.root / "ids" / sid / path.lstrip("/")
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("data")

    def ls_children(self, snap_id: str, node: str) -> list[tuple[str, bool]]:
        node = "/" + node.strip("/")
        children: dict[str, bool] = {}
        for path, is_dir in self.trees.get(snap_id, {}).items():
            if path == node or not path.startswith(node + "/"):
                continue
            rest = path[len(node) + 1:]
            if "/" in rest:
                continue
            children[rest] = is_dir
        return sorted(children.items())

    def _chmod_all(self, mode: int) -> None:
        for dirpath, _dirs, _files in os.walk(str(self.root)):
            os.chmod(dirpath, mode)

    def read_only(self) -> None:
        self._chmod_all(0o555)

    def writable(self) -> None:
        self._chmod_all(0o755)

    def build_in(self, view: Path, kind: str, snaps: list[dict]) -> int:
        """Build a view with the mount held read-only, then hand it back."""
        self.read_only()
        try:
            if kind == "latest":
                return views.build_latest(view, self.root, snaps, ls_children=self.ls_children)
            return views.build_bydate(view, self.root, snaps, ls_children=self.ls_children)
        finally:
            self.writable()


class NestedUnitsTest(unittest.TestCase):
    """A rest unit and a carved unit nested underneath it must both appear."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="rb-views-")
        self.tmp = Path(self.tmp_dir)
        self.mount = FakeMount(self.tmp / "snapshots", {
            "rest0001": [f"{STORAGE}/top.txt", f"{STORAGE}/user/notes.txt"],
            "carved02": [f"{NESTED}/game001.dat", f"{NESTED}/game002.dat"],
        })
        self.snaps = [
            _snap("rest0001", "2026-05-01T10:00:00Z", [STORAGE]),
            _snap("carved02", "2026-05-01T10:05:00Z", [NESTED]),
        ]
        self.latest = self.tmp / "latest"
        self.bydate = self.tmp / "bydate"

    def tearDown(self):
        for view in (self.latest, self.bydate):
            if view.exists():
                views.set_tree_readonly(view, False)
        self.mount.writable()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_latest_builds_without_touching_the_mount(self):
        before = _listing(self.mount.root)
        created = self.mount.build_in(self.latest, "latest", self.snaps)
        self.assertEqual(_listing(self.mount.root), before, "nothing may be created in the mount")
        self.assertGreater(created, 0, "the view should place some links")

    def test_nested_unit_is_linked_at_its_own_path(self):
        self.mount.build_in(self.latest, "latest", self.snaps)
        unit_link = self.latest / NESTED.lstrip("/")
        self.assertTrue(unit_link.is_symlink(), "the carved unit is one link at its target path")
        self.assertIn("carved02", os.readlink(str(unit_link)))
        for name in ("game001.dat", "game002.dat"):
            self.assertTrue((unit_link / name).exists(), f"{name} must resolve through the link")

    def test_overlap_becomes_a_real_directory_and_keeps_the_ancestors_entries(self):
        self.mount.build_in(self.latest, "latest", self.snaps)
        games = self.latest / f"{STORAGE}/user/games".lstrip("/")
        self.assertTrue(games.is_dir() and not games.is_symlink(),
                        "the corridor to a nested unit must be a real local directory")
        for link_path, owner in ((f"{STORAGE}/top.txt", "rest0001"),
                                 (f"{STORAGE}/user/notes.txt", "rest0001")):
            link = self.latest / link_path.lstrip("/")
            self.assertTrue(link.is_symlink(), f"{link_path} should come from the rest unit")
            self.assertIn(owner, os.readlink(str(link)))

    def test_bydate_builds_without_touching_the_mount(self):
        before = _listing(self.mount.root)
        snaps = [
            _snap("rest0001", "2026-05-01T10:00:00Z", [STORAGE]),
            _snap("carved02", "2026-05-01T10:00:30Z", [NESTED]),  # same run, same minute
        ]
        created = self.mount.build_in(self.bydate, "bydate", snaps)
        self.assertEqual(_listing(self.mount.root), before, "nothing may be created in the mount")
        self.assertGreater(created, 0)

        # One run on this date -> flat bydate/<date>/ layout (no HH-MM subfolder).
        dates = sorted(d for d in self.bydate.iterdir() if d.is_dir())
        self.assertEqual(len(dates), 1, "one run must produce one date folder")
        run_dir = dates[0]
        self.assertTrue((run_dir / NESTED.lstrip("/") / "game001.dat").exists(),
                        "the nested unit's files must be reachable in the run folder")
        self.assertTrue((run_dir / STORAGE.lstrip("/") / "top.txt").is_symlink(),
                        "the rest unit's own entries must be present too")

    def test_two_runs_on_one_day_get_their_own_folders(self):
        mount = FakeMount(self.tmp / "snapshots", {
            "rest0001": [f"{STORAGE}/top.txt"],
            "carved02": [f"{NESTED}/game001.dat"],
            "rest0003": [f"{STORAGE}/later.txt"],
        })
        snaps = [
            _snap("rest0001", "2026-05-01T10:00:00Z", [STORAGE]),
            _snap("carved02", "2026-05-01T10:00:40Z", [NESTED]),
            _snap("rest0003", "2026-05-01T23:30:00Z", [STORAGE]),
        ]
        mount.build_in(self.bydate, "bydate", snaps)

        morning_date, morning_key = _local_keys("2026-05-01T10:00:00Z")[0]
        evening_date, evening_key = _local_keys("2026-05-01T23:30:00Z")[0]
        self.assertEqual(morning_date, evening_date, "both runs must share one date folder")
        morning = self.bydate / morning_date / morning_key
        evening = self.bydate / evening_date / evening_key
        self.assertTrue((morning / NESTED.lstrip("/")).is_symlink())
        self.assertTrue((morning / STORAGE.lstrip("/") / "top.txt").is_symlink())
        self.assertIn("rest0003", os.readlink(str(evening / STORAGE.lstrip("/"))),
                      "the lone unit of the second run links at its own target path")
        self.assertTrue((evening / STORAGE.lstrip("/") / "later.txt").exists())


class GroupUnitsTest(unittest.TestCase):
    """Group units back up many loose file paths — every one must be reachable.

    Group units are multi-target: the snapshot's paths are individual files, so
    each one is placed as its own link, and the directories along the way must be
    real local dirs (the mount is read-only, so it can never hold them).
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="rb-views-grp-")
        self.tmp = Path(self.tmp_dir)
        self.files = [f"{NESTED}/game{i:03d}.dat" for i in range(40)]
        self.mount = FakeMount(self.tmp / "snapshots", {
            "rest0001": [f"{STORAGE}/readme.txt"],
            "group001": self.files[:20],
            "group002": self.files[20:],
        })
        self.snaps = [
            _snap("rest0001", "2026-05-01T10:00:00Z", [STORAGE]),
            _snap("group001", "2026-05-01T10:10:00Z", self.files[:20]),
            _snap("group002", "2026-05-01T10:20:00Z", self.files[20:]),
        ]
        self.latest = self.tmp / "latest"

    def tearDown(self):
        if self.latest.exists():
            views.set_tree_readonly(self.latest, False)
        self.mount.writable()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_every_loose_file_target_is_linked(self):
        before = _listing(self.mount.root)
        self.mount.build_in(self.latest, "latest", self.snaps)
        self.assertEqual(_listing(self.mount.root), before, "nothing may be created in the mount")

        for sid, chunk in (("group001", self.files[:20]), ("group002", self.files[20:])):
            for path in chunk:
                link = self.latest / path.lstrip("/")
                self.assertTrue(link.is_symlink(), f"missing link for {path}")
                self.assertTrue(link.exists(), f"{path} does not resolve")
                self.assertIn(sid, os.readlink(str(link)))

        rest_link = self.latest / f"{STORAGE}/readme.txt".lstrip("/")
        self.assertTrue(rest_link.is_symlink(), "the rest unit must contribute through the corridor")
        self.assertIn("rest0001", os.readlink(str(rest_link)))
        collection = self.latest / NESTED.lstrip("/")
        self.assertTrue(collection.is_dir() and not collection.is_symlink(),
                        "the ancestor unit must not block the grouped files beneath it")


class NewestWinsTest(unittest.TestCase):
    def test_newest_snapshot_wins_per_target_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="rb-views-new-")
        tmp = Path(tmp_dir)
        latest = tmp / "latest"
        try:
            mount = FakeMount(tmp / "snapshots", {
                "old00001": [f"{STORAGE}/keep.txt"],
                "new00001": [f"{STORAGE}/keep.txt", f"{STORAGE}/fresh.txt"],
            })
            snaps = [
                _snap("old00001", "2026-05-01T10:00:00Z", [STORAGE]),
                _snap("new00001", "2026-05-02T10:00:00Z", [STORAGE]),
            ]
            mount.build_in(latest, "latest", snaps)
            unit_link = latest / STORAGE.lstrip("/")
            self.assertIn("new00001", os.readlink(str(unit_link)),
                          "each target path must come from its newest snapshot")
            self.assertTrue((unit_link / "keep.txt").exists())
            self.assertTrue((unit_link / "fresh.txt").exists())
        finally:
            if latest.exists():
                views.set_tree_readonly(latest, False)
            shutil.rmtree(tmp_dir, ignore_errors=True)


class LsChildrenParsingTest(unittest.TestCase):
    """run_ls_children must pick direct children out of restic's flat JSON listing."""

    # Shapes taken from `restic 0.19.1 ls --json <snap> <dir>`
    CANNED = "\n".join([
        '{"time":"2026-05-01T10:00:00Z","paths":["/mnt/storage"],"hostname":"host",'
        '"message_type":"snapshot","struct_type":"snapshot"}',
        '{"name":"storage","type":"dir","path":"/mnt/storage",'
        '"message_type":"node","struct_type":"node"}',
        '{"name":"user","type":"dir","path":"/mnt/storage/user",'
        '"message_type":"node","struct_type":"node"}',
        '{"name":"lost+found","type":"dir","path":"/mnt/storage/lost+found",'
        '"message_type":"node","struct_type":"node"}',
        '{"name":"readme.txt","type":"file","path":"/mnt/storage/readme.txt",'
        '"size":12,"message_type":"node","struct_type":"node"}',
        # deeper entries must be filtered out
        '{"name":"games","type":"dir","path":"/mnt/storage/user/games",'
        '"message_type":"node","struct_type":"node"}',
        '{"name":"game001.dat","type":"file","path":"/mnt/storage/user/games/game001.dat",'
        '"size":7,"message_type":"node","struct_type":"node"}',
        "ssh: warning: something unrelated",
    ])

    def _call(self, canned: str, path: str) -> tuple[list, list]:
        calls: list[list] = []
        original = runner.run_restic

        def fake(args, cfg, on_output=None, on_proc=None):
            calls.append(args)
            return 0, canned

        runner.run_restic = fake
        try:
            cfg = Config(backend="local", repo_path="/tmp/unused")
            children = runner.run_ls_children(cfg, "abcd1234", path)
            return calls[0], children
        finally:
            runner.run_restic = original

    def test_only_direct_children_are_returned(self):
        args, children = self._call(self.CANNED, STORAGE)
        self.assertEqual(args, ["ls", "--json", "abcd1234", STORAGE])
        self.assertEqual(children, [("user", True), ("lost+found", True), ("readme.txt", False)])

    def test_root_path_is_handled(self):
        canned = ('{"name":"mnt","type":"dir","path":"/mnt",'
                  '"message_type":"node","struct_type":"node"}')
        _args, children = self._call(canned, "/")
        self.assertEqual(children, [("mnt", True)])


if __name__ == "__main__":
    unittest.main()
