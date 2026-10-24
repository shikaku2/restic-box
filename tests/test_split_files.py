"""Regression test: big loose files become their own units and coverage is complete.

Run from the project root:  python -m unittest tests.test_split_files -v
The restic coverage check is skipped automatically if `restic` is not installed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import restic_split as split  # noqa: E402

MIB = 1024 * 1024
THRESHOLD = 10 * MIB


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))


class SplitFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-split-")
        self.root = os.path.join(self.tmp, "tree")
        _write(os.path.join(self.root, "mixed", "huge1.bin"), 12 * MIB)
        _write(os.path.join(self.root, "mixed", "huge2[1].bin"), 11 * MIB)  # glob chars in name
        _write(os.path.join(self.root, "mixed", "tiny.txt"), 1 * MIB)
        _write(os.path.join(self.root, "small", "a"), 2 * MIB)
        _write(os.path.join(self.root, "top_huge.img"), 13 * MIB)
        _write(os.path.join(self.root, "top_small"), 1 * MIB)
        self.units = split.build_plan(self.root, [".zfs"], THRESHOLD)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_big_files_get_own_units(self):
        files = {u.path for u in self.units if u.kind == "file"}
        self.assertEqual(files, {
            os.path.join(self.root, "mixed", "huge1.bin"),
            os.path.join(self.root, "mixed", "huge2[1].bin"),
            os.path.join(self.root, "top_huge.img"),
        })

    def test_small_stuff_stays_in_rest_units(self):
        rest = [u for u in self.units if u.kind == "rest"]
        self.assertTrue(rest)
        for u in rest:
            self.assertLess(u.size, THRESHOLD)

    @unittest.skipUnless(shutil.which("restic"), "restic not installed")
    def test_union_of_snapshots_covers_every_file(self):
        env = dict(os.environ, RESTIC_PASSWORD="x", RESTIC_REPOSITORY=os.path.join(self.tmp, "repo"))
        subprocess.run(["restic", "init", "-q"], env=env, check=True)
        for u in self.units:
            args = ["restic", "backup", "-q", *u.targets()]
            for e in u.excludes:
                args += ["--exclude", e]
            subprocess.run(args, env=env, check=True)
        out = subprocess.run(["restic", "ls", "--json", "latest"], env=env, capture_output=True, text=True)
        # collect files from every snapshot
        snaps = subprocess.run(["restic", "snapshots", "--json"], env=env, capture_output=True, text=True, check=True)
        import json
        ids = [s["id"] for s in json.loads(snaps.stdout)]
        seen = set()
        for sid in ids:
            ls = subprocess.run(["restic", "ls", "--json", sid], env=env, capture_output=True, text=True, check=True)
            for line in ls.stdout.splitlines():
                d = json.loads(line)
                if d.get("type") == "file":
                    seen.add(d["path"])
        expected = set()
        for dp, _dn, fn in os.walk(self.root):
            for f in fn:
                expected.add(os.path.join(dp, f))
        self.assertEqual(seen, expected)


class DisabledModeTest(unittest.TestCase):
    """Ensure threshold=0 is a no-op and returns the original directory."""

    def test_threshold_zero_returns_original_dir(self):
        tmp = tempfile.mkdtemp(prefix="rb-split-zero-")
        try:
            root = os.path.join(tmp, "data")
            _write(root, 50 * MIB)
            units = split.expand(_make_dir(root), 0)
            self.assertEqual(len(units), 1)
            self.assertIs(units[0].path, root)
            # Should NOT be a Target — original dir passes through unchanged
            self.assertNotIsInstance(units[0], split.Target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_threshold_negative_returns_original_dir(self):
        tmp = tempfile.mkdtemp(prefix="rb-split-neg-")
        try:
            root = os.path.join(tmp, "data")
            _write(root, 50 * MIB)
            units = split.expand(_make_dir(root), -5)
            self.assertEqual(len(units), 1)
            self.assertNotIsInstance(units[0], split.Target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CacheTest(unittest.TestCase):
    """Verify split plans are freshly computed each time."""

    def test_different_threshold_different_plan(self):
        tmp = tempfile.mkdtemp(prefix="rb-split-cache-")
        try:
            root = os.path.join(tmp, "tree")
            os.makedirs(root)
            # Create 3 subdirs, each 400MB = 1.2GB total
            # threshold 1GiB -> will split (root >= 1GB, big dirs carved out)
            for i in range(3):
                _write(os.path.join(root, f"dir{i}", "data.bin"), 400 * MIB)
            units1 = split.build_plan(root, [], 1 * split.GIB)
            self.assertGreater(len(units1), 1, "Should split with 1GiB threshold")
            units2 = split.build_plan(root, [], 2 * split.GIB)
            self.assertNotEqual(len(units1), len(units2),
                                "Different threshold should produce different unit count")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HardlinkTest(unittest.TestCase):
    """Verify hard links are counted only once."""

    def test_hardlink_size_counted_once(self):
        tmp = tempfile.mkdtemp(prefix="rb-split-hlink-")
        try:
            root = os.path.join(tmp, "data")
            os.makedirs(root)
            src = os.path.join(root, "original.bin")
            _write(src, 20 * MIB)
            # Hard link
            os.link(src, os.path.join(root, "link.bin"))
            # Scan should count 20MB total, not 40MB
            size, children, bigfiles = split.scan(root, [], 10 * MIB)
            self.assertEqual(size[root], 20 * MIB, "Hard links should be deduplicated")
            # Only one unit (the original file), not two
            units = split.build_plan(root, [], 5 * MIB)
            file_units = [u for u in units if u.kind == "file"]
            self.assertEqual(len(file_units), 1, "Hard links produce one unit")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class EdgeCaseTest(unittest.TestCase):
    """Edge cases for plan_from_scan."""

    def test_nonexistent_root_returns_single_unit(self):
        units = split.build_plan("/definitely/does/not/exist/path", [], 100 * split.GIB)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].kind, "dir")
        self.assertEqual(units[0].size, 0.0)

    def test_empty_directory(self):
        tmp = tempfile.mkdtemp(prefix="rb-split-empty-")
        try:
            root = os.path.join(tmp, "empty")
            os.makedirs(root)
            units = split.build_plan(root, [], 10 * MIB)
            self.assertEqual(len(units), 1)
            self.assertEqual(units[0].kind, "dir")
            self.assertEqual(units[0].size, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _make_dir(path):
    """Helper to create a minimal Directory-like object."""
    class Dir:
        def __init__(self, p):
            self.path = p
            self.excludes = []
            self.cached_size = 0.0
            self.enabled = True
    return Dir(path)


if __name__ == "__main__":
    unittest.main()
