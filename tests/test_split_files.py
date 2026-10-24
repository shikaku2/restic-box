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


if __name__ == "__main__":
    unittest.main()
