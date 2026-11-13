"""Stable (hysteresis) split-plan tests: saved unit path sets must survive
small drift so restic keeps finding parent snapshots.

Run from the project root:  python -m unittest tests.test_split_stable -v
Only test_split_stable.ResticaE2ETest needs the `restic` binary.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import restic_split as split  # noqa: E402

MIB = 1024 * 1024
GIB = 1024 ** 3


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path) or path, exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))


def _grow(path: str, extra: int) -> None:
    with open(path, "ab") as f:
        f.write(os.urandom(extra))


def _fp(u: split.Unit) -> tuple:
    """Identity fingerprint of a unit: everything restic parents on, plus kind."""
    return (u.kind, u.path, tuple(u.paths), tuple(u.excludes))


def _scan(root: str, threshold: int):
    return split.scan(root, [], threshold)


def _stable(prev, root, threshold, drift_factor=2.0, excludes=()):
    size, children, bigfiles, smallfiles = _scan(root, threshold)
    return split.plan_stable(prev, root, list(excludes), threshold, drift_factor,
                             size, children, bigfiles, smallfiles)


def _group_trees(units):
    return sorted(tuple(u.paths) for u in units if u.kind == "group")


class FixtureMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-stable-")
        self.root = os.path.join(self.tmp, "tree")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, threshold):
        return split.build_plan(self.root, [], threshold)


# ---------------------------------------------------------------------------
# (a) unchanged scan -> identical units
# ---------------------------------------------------------------------------

class UnchangedTest(FixtureMixin):
    T = 10 * MIB

    def setUp(self):
        super().setUp()
        # A: split dir with two single-member groups; B: split dir with a big
        # file; C: small dir left to the root rest unit.
        _write(os.path.join(self.root, "A", "a1"), 7 * MIB)
        _write(os.path.join(self.root, "A", "a2"), 6 * MIB)
        _write(os.path.join(self.root, "B", "b.bin"), 12 * MIB)
        _write(os.path.join(self.root, "C", "c.dat"), 3 * MIB)
        _write(os.path.join(self.root, "r_small.txt"), 1 * MIB)

    def test_identical_units_rest_first(self):
        prev = self.plan(self.T)
        self.assertGreater(len(prev), 3)
        new = _stable(prev, self.root, self.T)
        # rest units first, in stored order; then others stable by size asc
        expected = ([_fp(u) for u in prev if u.kind == "rest"]
                    + [_fp(u) for u in sorted((u for u in prev if u.kind != "rest"),
                                              key=lambda u: u.size)])
        self.assertEqual([_fp(u) for u in new], expected)
        self.assertEqual([u.size for u in new], [u.size for u in prev])


# ---------------------------------------------------------------------------
# (b) REGRESSION: tiny size drift must NOT reshuffle group path sets, even
#     though a fresh plan_from_scan would
# ---------------------------------------------------------------------------

class GroupDriftRegressionTest(FixtureMixin):
    T = 50 * MIB

    def test_grow_1mib_keeps_path_sets(self):
        found = False
        for seed in range(300):
            shutil.rmtree(self.root, ignore_errors=True)
            rng = random.Random(seed)
            flat = os.path.join(self.root, "flat")
            sizes = {f"f{i}.bin": rng.randint(1, 40) * MIB for i in range(8)}
            for name, sz in sizes.items():
                _write(os.path.join(flat, name), sz)
            prev = self.plan(self.T)
            groups = [u for u in prev if u.kind == "group"]
            big_groups = [g for g in groups if len(g.paths) >= 2]
            if not big_groups:
                continue
            member = big_groups[0].paths[0]
            _grow(member, MIB)
            size, children, bigfiles, smallfiles = _scan(self.root, self.T)
            fresh = split.plan_from_scan(self.root, [], self.T,
                                         size, children, bigfiles, smallfiles)
            if _group_trees(prev) == _group_trees(fresh):
                continue  # seed didn't actually reshuffle; try the next one
            found = True
            new = _stable(prev, self.root, self.T)
            self.assertEqual(_group_trees(new), _group_trees(prev),
                             f"seed {seed}: plan_stable reshuffled group paths")
            self.assertEqual(sorted(_fp(u) for u in new if u.kind != "group"),
                             sorted(_fp(u) for u in prev if u.kind != "group"))
            break
        self.assertTrue(found, "no seeded fixture made the fresh plan reshuffle")


# ---------------------------------------------------------------------------
# (c) delete a group member -> only that group's paths shrink
# ---------------------------------------------------------------------------

class DeleteMemberTest(FixtureMixin):
    T = 10 * MIB

    def setUp(self):
        super().setUp()
        flat = os.path.join(self.root, "flat")
        for i in range(8):
            _write(os.path.join(flat, f"f{i}.bin"), 4 * MIB)
        self.prev = self.plan(self.T)
        self.groups = [u for u in self.prev if u.kind == "group"]
        self.assertTrue(any(len(g.paths) >= 2 for g in self.groups))

    def test_only_that_group_shrinks(self):
        victim = next(g for g in self.groups if len(g.paths) >= 2)
        doomed = victim.paths[0]
        os.remove(doomed)
        new = _stable(self.prev, self.root, self.T)
        ng = {tuple(sorted(u.paths)) for u in new if u.kind == "group"}
        self.assertIn(tuple(sorted(p for p in victim.paths if p != doomed)), ng)
        for old in self.groups:
            if old is victim:
                continue
            self.assertIn(tuple(sorted(old.paths)), ng)
        # every non-group unit keeps its identity
        self.assertEqual(sorted(_fp(u) for u in new if u.kind != "group"),
                         sorted(_fp(u) for u in self.prev if u.kind != "group"))


# ---------------------------------------------------------------------------
# (d) new small dir/file under a split directory -> no unit changes
# ---------------------------------------------------------------------------

class NewSmallStuffTest(FixtureMixin):
    T = 10 * MIB

    def setUp(self):
        super().setUp()
        flat = os.path.join(self.root, "flat")
        for i in range(6):
            _write(os.path.join(flat, f"f{i}.bin"), 4 * MIB)

    def test_nothing_changes(self):
        prev = self.plan(self.T)
        _write(os.path.join(self.root, "newdir", "n1.dat"), MIB)
        _write(os.path.join(self.root, "newfile.txt"), MIB)
        new = _stable(prev, self.root, self.T)
        self.assertEqual([_fp(u) for u in new], [_fp(u) for u in prev])
        # rest unit(s) merely got heavier
        for a, b in zip(prev, new):
            if a.kind == "rest" and a.path == self.root:
                self.assertGreater(b.size, a.size)


# ---------------------------------------------------------------------------
# (e) group grows past drift*T -> only that group repacks, bins <= T, union kept
# ---------------------------------------------------------------------------

class GroupRepackTest(FixtureMixin):
    T = 10 * MIB

    def setUp(self):
        super().setUp()
        flat = os.path.join(self.root, "flat")
        for i in range(8):
            _write(os.path.join(flat, f"f{i}.bin"), 2 * MIB)
        self.prev = self.plan(self.T)
        self.groups = [u for u in self.prev if u.kind == "group"]
        self.victim = next(g for g in self.groups if len(g.paths) >= 4)

    def test_repack_only_that_group(self):
        # grow two members so the group total passes drift*T (20M) without any
        # single member reaching T (10M)
        grown = set(self.victim.paths[:2])
        for p in grown:
            _grow(p, 7 * MIB)  # 2 -> 9 MiB
        new = _stable(self.prev, self.root, self.T)
        ng = [u for u in new if u.kind == "group"]
        sz = lambda p: 9 * MIB if p in grown else 2 * MIB
        victims_members = set(self.victim.paths)
        touched = set()
        for u in ng:
            if victims_members & set(u.paths):
                self.assertLessEqual(sum(sz(p) for p in u.paths), self.T)
                touched |= set(u.paths)
        self.assertEqual(touched, victims_members)
        # untouched groups keep their exact bins
        for old in self.groups:
            if old is self.victim:
                continue
            self.assertIn(tuple(sorted(old.paths)),
                          {tuple(sorted(u.paths)) for u in ng})
        self.assertEqual(sorted(_fp(u) for u in new if u.kind != "group"),
                         sorted(_fp(u) for u in self.prev if u.kind != "group"))


# ---------------------------------------------------------------------------
# (f) dir unit grows past drift*T -> becomes rest + subs; others untouched
# ---------------------------------------------------------------------------

class DirUnitResplitTest(FixtureMixin):
    T = 10 * MIB

    def setUp(self):
        super().setUp()
        # real tree: A is 29M (>= 2*T), carved as x + small leftovers.
        _write(os.path.join(self.root, "A", "x", "x.bin"), 22 * MIB)
        _write(os.path.join(self.root, "A", "y", "y.dat"), 3 * MIB)
        _write(os.path.join(self.root, "A", "a_small"), 4 * MIB)
        # saved plan says A is one unsplittable dir unit (synthetic prev:
        # e.g. A had too many files to list back then)
        self.prev = [
            split.Unit(self.root, [split.escape_pattern(os.path.join(self.root, "A"))],
                       0.0, "rest"),
            split.Unit(os.path.join(self.root, "A"), [], 15 * MIB, "dir"),
        ]

    def test_dir_unit_resplits(self):
        new = _stable(self.prev, self.root, self.T)
        a = os.path.join(self.root, "A")
        a_rest = [u for u in new if u.kind == "rest" and u.path == a]
        self.assertEqual(len(a_rest), 1, "A should have become rest + subs")
        self.assertIn(split.escape_pattern(os.path.join(a, "x")), a_rest[0].excludes)
        subs = [u for u in new if u.path != self.root and u is not a_rest[0]]
        self.assertTrue(subs)
        # the root rest unit is untouched
        root_rest = next(u for u in new if u.kind == "rest" and u.path == self.root)
        self.assertEqual(_fp(root_rest), _fp(self.prev[0]))
        # x (22M >= T) got its own file unit under A's rest
        self.assertIn(os.path.join(a, "x", "x.bin"),
                      [u.path for u in new if u.kind == "file"])


# ---------------------------------------------------------------------------
# (g) rest overflow -> new group units, rest excludes extended, no overlap
# ---------------------------------------------------------------------------

class RestOverflowTest(FixtureMixin):
    T = 10 * MIB

    def setUp(self):
        super().setUp()
        self.flat = os.path.join(self.root, "flat")
        for i in range(8):
            _write(os.path.join(self.flat, f"f{i}.bin"), 4 * MIB)
        self.prev = self.plan(self.T)

    def test_overflow_carves_new_groups(self):
        new_files = [os.path.join(self.flat, f"g{i}.bin") for i in range(12)]
        for p in new_files:
            _write(p, 2 * MIB)  # 24M of new leftovers >= drift*T (20M)
        flat_rest = next(u for u in self.prev if u.kind == "rest" and u.path == self.flat)
        before_ex = list(flat_rest.excludes)
        new = _stable(self.prev, self.root, self.T)
        flat_rest2 = next(u for u in new if u.kind == "rest" and u.path == self.flat)
        self.assertEqual(flat_rest2.excludes[:len(before_ex)], before_ex)
        for p in new_files:
            self.assertIn(split.escape_pattern(p), flat_rest2.excludes)
        # new group units contain exactly the carved new files
        carved = {p for u in new if u.kind == "group"
                  for p in u.paths if p in set(new_files)}
        self.assertEqual(carved, set(new_files))
        # no path lives in two units
        seen = set()
        for u in new:
            for p in u.paths:
                self.assertNotIn(p, seen, f"{p} in two units")
                seen.add(p)
            if u.kind in ("dir", "file"):
                self.assertNotIn(u.path, seen)
                seen.add(u.path)


# ---------------------------------------------------------------------------
# (h) plan persistence: key invalidation + corrupt files
# ---------------------------------------------------------------------------

class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-plan-"))
        self.root = "/some/where/data"
        self.T = 10 * GIB
        self.ex = [".zfs", "cache"]
        self.units = [
            split.Unit(self.root, [".zfs", "cache", split.escape_pattern(self.root + "/big")],
                       50.0, "rest"),
            split.Unit(self.root + "/big", [".zfs", "cache"], 42.0, "file"),
            split.Unit(self.root + "/g1", [".zfs", "cache"], 8.0, "group",
                       paths=[self.root + "/g1", self.root + "/s1"]),
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, ex=None):
        split.save_plan(self.root, ex or self.ex, self.T, self.units, self.tmp)

    def test_roundtrip(self):
        self.save()
        got = split.load_plan(self.root, self.ex, self.T, self.tmp)
        self.assertIsNotNone(got)
        self.assertEqual([_fp(u) for u in got], [_fp(u) for u in self.units])
        self.assertEqual([u.size for u in got], [u.size for u in self.units])
        # atomic write: no temp litter
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])
        payload = json.loads(next(self.tmp.glob("*.json")).read_text())
        self.assertEqual(payload["version"], 1)
        self.assertEqual(set(payload["units"][0]),
                         {"kind", "path", "paths", "excludes", "size"})

    def test_different_key_ignores_saved_plan(self):
        self.save()
        self.assertIsNone(split.load_plan(self.root, self.ex, self.T + 1, self.tmp))
        self.assertIsNone(split.load_plan(self.root, self.ex + ["more"], self.T, self.tmp))
        self.assertIsNone(split.load_plan("/other/root", self.ex, self.T, self.tmp))

    def test_corrupt_or_wrong_version_is_fresh(self):
        self.save()
        p = next(self.tmp.glob("*.json"))
        p.write_text("this is { not json")
        self.assertIsNone(split.load_plan(self.root, self.ex, self.T, self.tmp))
        p.write_text(json.dumps({"version": 99, "units": []}))
        self.assertIsNone(split.load_plan(self.root, self.ex, self.T, self.tmp))
        p.write_text(json.dumps({"version": 1}))  # no units key
        self.assertIsNone(split.load_plan(self.root, self.ex, self.T, self.tmp))

    def test_missing_is_none(self):
        self.assertIsNone(split.load_plan(self.root, self.ex, self.T, self.tmp))


class ExpandPersistenceTest(unittest.TestCase):
    """expand() reuses the saved plan across runs (plan dir redirected)."""

    class _Dir:
        def __init__(self, path):
            self.path, self.excludes, self.cached_size = path, [], 0.0

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-expand-"))
        self._real = split.CONFIG_DIR
        split.CONFIG_DIR = self.tmp / "config"
        self.root = str(self.tmp / "data")
        flat = os.path.join(self.root, "flat")
        for i in range(6):
            _write(os.path.join(flat, f"f{i}.bin"), 4 * MIB)

    def tearDown(self):
        split.CONFIG_DIR = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_expand_loads_saved_plan(self):
        # threshold has to be >= 1 GiB for expand(); make data big enough instead
        big = os.path.join(self.root, "flat2")
        for i in range(3):
            _write(os.path.join(big, f"h{i}.bin"), 600 * MIB)  # 1.8G total
        T = 1
        d1 = self._Dir(self.root)
        u1 = split.expand(d1, T, None, 2.0)
        self.assertGreater(len(u1), 1)
        # mutate: grow one group member by 1 MiB — stable plan must be reused
        groups = [u for u in u1 if u.paths]
        with open(groups[0].paths[0], "ab") as f:
            f.write(os.urandom(MIB))
        u2 = split.expand(self._Dir(self.root), T, None, 2.0)
        self.assertEqual(sorted(tuple(t.paths or []) for t in u2),
                         sorted(tuple(t.paths or []) for t in u1))
        # changed threshold -> different key -> fresh plan file, new count ok
        u3 = split.expand(self._Dir(self.root), 2, None, 2.0)
        self.assertEqual(len(list((self.tmp / "config" / "split-plans").glob("*.json"))), 2)
        self.assertLessEqual(len(u3), len(u1))


# ---------------------------------------------------------------------------
# (i) end-to-end with real restic: stable replan still covers every file
# ---------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("restic"), "restic not installed")
class ResticE2ETest(FixtureMixin):
    T = 10 * MIB

    def setUp(self):
        super().setUp()
        self._real = split.CONFIG_DIR
        split.CONFIG_DIR = Path(self.tmp) / "config"
        # split tree: dir with a glob-named big file + group-packed flat dir
        _write(os.path.join(self.root, "big", "b[1].bin"), 15 * MIB)
        flat = os.path.join(self.root, "flat")
        for i in range(6):
            _write(os.path.join(flat, f"f{i}.bin"), 4 * MIB)
        _write(os.path.join(self.root, "tiny", "t.txt"), 2 * MIB)

    def tearDown(self):
        split.CONFIG_DIR = self._real
        super().tearDown()

    def test_mutate_stable_replan_then_full_union(self):
        units1 = _expand_mib(self.root, self.T)
        self.assertGreater(len(units1), 1)
        # mutate: add, delete, grow
        _write(os.path.join(self.root, "flat", "added.txt"), 3 * MIB)
        _write(os.path.join(self.root, "deep", "nested", "n.bin"), 3 * MIB)
        groups = [u for u in units1 if u.paths]
        os.remove(groups[0].paths[0])
        _grow(groups[1].paths[0], 4 * MIB)
        units2 = _expand_mib(self.root, self.T)  # loads saved plan -> plan_stable
        self.assertTrue(any(u.paths for u in units2))

        repo = os.path.join(self.tmp, "repo")
        env = dict(os.environ, RESTIC_PASSWORD="x", RESTIC_REPOSITORY=repo)
        subprocess.run(["restic", "init", "-q"], env=env, check=True)
        for u in units2:
            args = ["restic", "backup", "-q", *u.targets()]
            for e in u.excludes:
                args += ["--exclude", e]
            subprocess.run(args, env=env, check=True)
        snaps = json.loads(subprocess.run(["restic", "snapshots", "--json"],
                                          env=env, capture_output=True, text=True,
                                          check=True).stdout)
        seen = set()
        for s in snaps:
            ls = subprocess.run(["restic", "ls", "--json", s["id"]], env=env,
                                capture_output=True, text=True, check=True)
            for line in ls.stdout.splitlines():
                d = json.loads(line)
                if d.get("type") == "file":
                    seen.add(d["path"])
        expected = set()
        for dp, _dn, fn in os.walk(self.root):
            for f in fn:
                expected.add(os.path.join(dp, f))
        self.assertEqual(seen, expected)


def _expand_mib(root, threshold_bytes):
    """expand() speaks GiB; call build/plan machinery directly at MiB scale."""
    root_n = os.path.normpath(root)
    prev = split.load_plan(root_n, [], threshold_bytes)
    size, children, bigfiles, smallfiles = split.scan(root_n, [], threshold_bytes)
    if prev is not None:
        plan = split.plan_stable(prev, root_n, [], threshold_bytes, 2.0,
                                 size, children, bigfiles, smallfiles)
    else:
        plan = split.plan_from_scan(root_n, [], threshold_bytes,
                                    size, children, bigfiles, smallfiles)
    split.save_plan(root_n, [], threshold_bytes, plan)
    return plan


if __name__ == "__main__":
    unittest.main()
