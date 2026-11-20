"""`.zfs` must never be backed up by accident — but must work on purpose
(fix #2, restic-box-fixes2.md).

The split planner already skipped `.zfs` while sizing; the actual restic
command did not, so a ZFS dataset's .zfs/snapshot tree could be duplicated
into the repository. These tests pin both sides:

* the generated backup command always carries a `.zfs` exclusion for ordinary
  sources (bare pattern: dataset roots and nested datasets alike);
* an exclusion the user typed themselves survives untouched — it stays in the
  settings data (so it shows in the UI) and is used exactly once;
* a user who deliberately points a source AT a .zfs path gets that backup;
* the planner never counts .zfs contents as ordinary source data;
* real restic integration: a visible .zfs/snapshot directory is not stored
  by the automatic exclusion, and is stored when the source asks for it.

No network, no ssh: the restic checks use throwaway local repos in temp
directories, and `.zfs` is just a plain directory there — which is exactly
how restic sees the real thing.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import PurePosixPath
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_runner as runner  # noqa: E402
import restic_split as split  # noqa: E402
from tests.fakeproc import fake_popen  # noqa: E402

MIB = 1024 * 1024
THRESHOLD = 10 * MIB


def cfg_local(repo=None, pw=None):
    return cfg_mod.Config(backend="local",
                          repo_path=repo or "/repo-test",
                          password_file=pw or "/pw-test")


def capture_popen(sink, lines=(), returncode=0):
    """fake_popen that also records the exact argv of every Popen call."""
    base = fake_popen(lines, returncode)

    def _open(cmd, *args, **kwargs):
        sink.append(list(cmd))
        return base(cmd, *args, **kwargs)

    return _open


def zfs_exclusions(argv):
    """Every pattern passed via a --exclude flag."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]


class BackupCommandZfsTest(unittest.TestCase):
    def _argv_for(self, directory):
        sink = []
        with mock_patch_popen(sink):
            runner.run_backup(cfg_local(), directory)
        return sink[0]

    def test_plain_source_gets_a_zfs_exclusion(self):
        argv = self._argv_for(cfg_mod.Directory("/src"))
        self.assertIn(".zfs", zfs_exclusions(argv))

    def test_nested_datasets_are_covered_by_the_bare_pattern(self):
        """A pattern without a slash matches the NAME at any depth — one
        exclusion serves /pool/.zfs, /pool/data/.zfs, … all at once."""
        argv = self._argv_for(cfg_mod.Directory("/pool"))
        zfs_flags = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1)
                     if argv[i] == "--exclude"]
        self.assertEqual([pair for pair in zfs_flags if pair[1] == ".zfs"],
                         [("--exclude", ".zfs")],
                         ".zfs must be excluded by name, not by an absolute path")

    def test_user_typed_zfs_exclude_is_used_once_and_left_alone(self):
        directory = cfg_mod.Directory("/src", excludes=["*.tmp", ".zfs"])
        argv = self._argv_for(directory)
        self.assertEqual(zfs_exclusions(argv).count(".zfs"), 1)
        # the user's own list is untouched — it is what the settings dialog
        # will keep showing
        self.assertEqual(directory.excludes, ["*.tmp", ".zfs"])

    def test_split_rest_unit_with_many_excludes_still_excludes_zfs_once(self):
        many = [f"/src/leak/{i}" for i in range(250)] + [".zfs"]
        directory = cfg_mod.Directory("/src", excludes=many)
        argv = self._argv_for(directory)
        self.assertIn("--exclude-file", argv)
        # the big list went to the file; the flag list must not smuggle a
        # second .zfs past it
        self.assertNotIn(".zfs", zfs_exclusions(argv))

    def test_deliberate_zfs_source_is_backed_up_as_asked(self):
        """Someone who adds a .zfs path to the backup list means it: the
        automatic guard covers traversal, not their chosen target."""
        for target in ("/pool/.zfs", "/pool/.zfs/snapshot/snap1",
                       "/pool/data/.zfs/snap/x"):
            argv = self._argv_for(cfg_mod.Directory(target))
            self.assertNotIn(".zfs", zfs_exclusions(argv),
                             f"{target} must not be neutralized by the guard")
            self.assertIn(target, argv)


def mock_patch_popen(sink, lines=(), returncode=0):
    """Patch runner.subprocess.Popen with an argv-recording fake."""

    @contextmanager
    def _ctx():
        with mock.patch.object(runner.subprocess, "Popen",
                               capture_popen(sink, lines, returncode)):
            yield
    return _ctx()


class SplitUnitZfsTest(unittest.TestCase):
    def test_units_get_the_exclusion_too(self):
        """run_backup also receives split units (multi-path objects)."""
        unit = split.Unit("/src/rest", [".zfs", "/src/big.bin"], 8.0, "rest")
        sink = []
        with mock_patch_popen(sink):
            runner.run_backup(cfg_local(), unit)
        argv = sink[0]
        self.assertIn(".zfs", zfs_exclusions(argv))
        self.assertIn("/src/rest", argv)


class PlannerZfsTest(unittest.TestCase):
    """The planner and the real command must agree: .zfs contents are not
    source data, even a file big enough to become its own unit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-zfs-")
        self.root = os.path.join(self.tmp, "tree")
        _write(os.path.join(self.root, "real.bin"), 12 * MIB)
        _write(os.path.join(self.root, "small.bin"), 1 * MIB)
        # a "dataset" with a visible snapshot tree holding a huge ghost
        _write(os.path.join(self.root, ".zfs", "snapshot", "s1", "ghost.bin"),
               40 * MIB)
        _write(os.path.join(self.root, "data", ".zfs", "snapshot", "s1", "nested_ghost.bin"),
               25 * MIB)
        self.units = split.build_plan(self.root, [], THRESHOLD)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_unit_points_into_zfs(self):
        offenders = [u.path for u in self.units if ".zfs" in _parts(u.path)
                     or any(".zfs" in _parts(p) for p in u.targets())]
        self.assertEqual(offenders, [])

    def test_zfs_contents_are_never_unit_data(self):
        sizes = sum(u.size for u in self.units)
        self.assertLess(sizes, 20 * MIB,
                        "the 65 MiB of .zfs ghosts must not be counted")
        paths = {u.path for u in self.units}
        self.assertNotIn(os.path.join(self.root, ".zfs", "snapshot", "s1",
                                      "ghost.bin"), paths)

    def test_planner_and_runner_agree_on_the_same_tree(self):
        """Whatever the planner plans to send, the command excludes .zfs."""
        for unit in self.units:
            sink = []
            with mock_patch_popen(sink):
                runner.run_backup(cfg_local(), unit)
            self.assertIn(".zfs", zfs_exclusions(sink[0]))


def _parts(path):
    return PurePosixPath(path).parts


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))


@unittest.skipUnless(shutil.which("restic"), "restic not installed")
class RealResticZfsTest(unittest.TestCase):
    """End-to-end against a throwaway local repo. On a real ZFS mount restic
    sees .zfs as a traversable directory exactly like this fake one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-zfs-e2e-")
        self.repo = os.path.join(self.tmp, "repo")
        self.pw = os.path.join(self.tmp, "pw")
        with open(self.pw, "w") as f:
            f.write("testpw\n")
        subprocess.run(["restic", "-q", "-r", self.repo, "--password-file", self.pw,
                        "init"], check=True)
        self.source = os.path.join(self.tmp, "src")
        _write(os.path.join(self.source, "real.bin"), 2048)
        _write(os.path.join(self.source, ".zfs", "snapshot", "s1", "ghost.bin"), 2048)
        os.chmod(os.path.join(self.source, ".zfs"), 0o700)  # like the real one

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self):
        return dict(os.environ, RESTIC_REPOSITORY=self.repo, RESTIC_PASSWORD="testpw")

    def _snapshot_paths(self):
        snaps = subprocess.run(["restic", "-q", "snapshots", "--json"],
                               env=self._env(), capture_output=True, text=True,
                               check=True)
        ids = [s["id"] for s in json.loads(snaps.stdout)]
        seen = set()
        for sid in ids:
            ls = subprocess.run(["restic", "-q", "ls", "--json", sid],
                                env=self._env(), capture_output=True, text=True,
                                check=True)
            for line in ls.stdout.splitlines():
                entry = json.loads(line)
                if entry.get("type") == "file":
                    seen.add(entry["path"])
        return seen

    def test_backup_of_normal_source_never_stores_zfs(self):
        rc, _out, _added = runner.run_backup(
            cfg_local(self.repo, self.pw), cfg_mod.Directory(self.source))
        self.assertEqual(rc, 0)
        paths = self._snapshot_paths()
        self.assertTrue(any(p.endswith("real.bin") for p in paths), paths)
        self.assertFalse(any("ghost.bin" in p or "/.zfs/" in f"/{p}"
                             for p in paths),
                         f".zfs contents leaked into the repo: {paths}")

    def test_deliberate_zfs_source_works(self):
        rc, _out, _added = runner.run_backup(
            cfg_local(self.repo, self.pw),
            cfg_mod.Directory(os.path.join(self.source, ".zfs")))
        self.assertEqual(rc, 0)
        paths = self._snapshot_paths()
        self.assertTrue(any("ghost.bin" in p for p in paths),
                        f"the deliberately requested .zfs backup is missing: {paths}")


if __name__ == "__main__":
    unittest.main()
