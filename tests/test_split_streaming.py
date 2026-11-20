"""Directory scanning: memory, coverage, and honest errors (fix #8, restic-box-fixes2.md).

The bug: `_listdir()` did `list(os.scandir(path))`. A directory with a million
entries became a million `os.DirEntry` objects — each holding its own path and
name strings — all alive at once, held by the walk frame for the whole time the
walk spent inside that directory's children. On a big archive that is hundreds
of megabytes of temporary RAM for a tool that only ever needs one entry at a
time, and the currency it is measured in is *path characters*: an archive's
files sit under long directory chains, so 200,000 entries can carry 46 MB of
path text on their own, held for the entire time the walk is busy elsewhere.
The same helper answered `[]` for a directory it could not read, so a permission
problem was indistinguishable from an empty directory: the sizes it reported,
and the plan built from them, silently claimed nothing was there.

The fix streams (`restic_split._DirStream`), closes every directory handle it
opens — including the ones open when a Stop arrives — and reports an unreadable
directory through the log and a caller-visible `errors` dict. The planner's
file-list cap is kept, because it was never a limit on what gets backed up:
files the planner stops remembering stay in their directory's "rest" unit.

What this module proves, with no network and only throwaway repositories:

  * the walk holds one entry at a time: a child directory is opened after its
    parent's *first* entry, not after its whole listing;
  * post-order size accounting, child lists, big/small file classification,
    excludes and hardlink dedup are what the materializing walk produced
    (compared against a verbatim copy of the pre-fix algorithm);
  * peak memory and scan duration, measured in fresh child processes against
    short-named and long-named trees alike: 7.7 MiB against 98 MiB for a
    200k-entry directory with short names, 17.6 MiB against 41.6 MiB for eight
    55,000-entry directories whose paths are 228 characters long, and not slower.
    A streamed walk's peak is set by `MAX_LISTED_FILES` times the path length, not
    by how many entries exist: measured 17.9 MiB at 1M past-cap entries and
    18.1 MiB at 10M (restic-box-scan-memory.md);
  * how much memory the planner's own file lists still cost, since fix #8 adds
    no new caps: 378 B per file at 228-character paths, so 2M files is 774 MiB —
    measured rather than left vague, and the reason prefix-sharing is logged as a
    follow-up instead of quietly folded into this fix;
  * a directory past the planning cap is still covered end to end, verified by
    running real restic and listing the resulting snapshots;
  * an unreadable directory produces visible errors instead of looking empty;
  * cancelling a scan releases every outstanding directory handle.
"""
import atexit
import gc
import json
import os
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_split as split  # noqa: E402

_REAL_SCANDIR = os.scandir  # captured before any test patches os.scandir
MIB = 1024 ** 2


# --------------------------------------------------------------------------
# Reference implementation: the pre-fix walk, kept verbatim for comparison.
# The app no longer uses it; it exists so a test can ask "did streaming change
# anything the planner sees?" and get a real answer.
# --------------------------------------------------------------------------

def _materialized_scan(root: str, excludes: list, threshold: int):
    match = split._matcher(excludes)
    size: dict[str, int] = {}
    children: dict[str, list] = {}
    bigfiles: dict[str, list] = {}
    smallfiles: dict[str, object] = {}
    seen_inodes: set = set()

    def listdir(path):
        try:
            with os.scandir(path) as it:
                return list(it)
        except OSError:
            return []      # the old behaviour: an unreadable directory looked empty

    frames = [[root, listdir(root), 0, 0, [], [], []]]
    while frames:
        f = frames[-1]
        if f[2] >= len(f[1]):
            frames.pop()
            size[f[0]], children[f[0]] = f[3], f[4]
            bigfiles[f[0]], smallfiles[f[0]] = f[5], f[6]
            if frames:
                frames[-1][3] += f[3]
                frames[-1][4].append(f[0])
            continue
        e = f[1][f[2]]
        f[2] += 1
        if e.name == ".zfs" or match(e.path, e.name):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                frames.append([e.path, listdir(e.path), 0, 0, [], [], []])
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        sz = st.st_blocks * 512
        if st.st_nlink > 1:
            key = (st.st_dev, st.st_ino)
            if key in seen_inodes:
                sz = 0
            else:
                seen_inodes.add(key)
        f[3] += sz
        if not stat.S_ISREG(st.st_mode):
            continue
        if sz >= threshold:
            f[5].append((e.path, sz))
        elif f[6] is not None:
            f[6].append((e.path, sz))
            if len(f[6]) > split.MAX_LISTED_FILES:
                f[6] = None
    return size, children, bigfiles, smallfiles


# --------------------------------------------------------------------------
# Watching the walk's directory handles (real filesystem)
# --------------------------------------------------------------------------

class _DirHandle:
    """Wraps a real scandir iterator so a test can see when it opened and closed."""

    __slots__ = ("path", "it", "audit", "delivered", "closed")

    def __init__(self, path, it, audit):
        self.path = path
        self.it = it
        self.audit = audit
        self.delivered = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            entry = next(self.it)
        except StopIteration:
            self.close()
            raise
        except OSError:
            self.close()
            raise
        self.delivered += 1
        return entry

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.audit.closed_handles += 1
        self.audit.live -= 1
        self.it.close()


class HandleAudit:
    """os.scandir replacement that records every handle the app opens.

    Only the openings are journalled (with how many entries each already-open
    directory had yielded at that moment): journalling every delivered entry
    would make the *audit* the biggest memory user in the test.
    """

    def __init__(self):
        self.handles: list = []
        self.opens: list = []           # (path, {path: entries delivered}, count)
        self.live = 0
        self.peak_live = 0
        self.closed_handles = 0

    def _record_open(self, handle):
        snapshot = {other.path: other.delivered for other in self.handles}
        self.handles.append(handle)
        self.opens.append((handle.path, snapshot, len(self.handles)))
        self.live += 1
        self.peak_live = max(self.peak_live, self.live)

    @property
    def opened(self) -> int:
        return len(self.handles)

    def delivered_when_opened(self, path: str, ancestor: str) -> int:
        """How many entries `ancestor` had yielded when `path` was opened."""
        for opened_path, snapshot, _count in self.opens:
            if opened_path == path:
                return snapshot.get(ancestor, 0)
        raise AssertionError(f"{path} was never opened")

    def scandir(self, path):
        handle = _DirHandle(path, _REAL_SCANDIR(path), self)
        self._record_open(handle)
        return handle


# --------------------------------------------------------------------------
# A synthetic filesystem: directories far too large to build for real, with
# entries produced lazily so the only DirEntry objects alive are the app's.
# --------------------------------------------------------------------------

class FakeEntry:
    """Duck-types os.DirEntry for the synthetic walk."""

    __slots__ = ("name", "path", "_dir", "_st")

    def __init__(self, path: str, name: str, st, is_directory: bool = False):
        self.path = path
        self.name = name
        self._st = st
        self._dir = is_directory

    def is_dir(self, follow_symlinks=False):
        return self._dir

    def is_symlink(self):
        return False

    def stat(self, follow_symlinks=False):
        return self._st


class FailingEntry(FakeEntry):
    """An entry whose stat() fails the way a permission or I/O error does."""

    def __init__(self, path: str, detail: str = "Permission denied", errno: int = 13):
        super().__init__(path, os.path.basename(path), None)
        self.detail = detail
        self.errno = errno

    def stat(self, follow_symlinks=False):
        raise OSError(self.errno, self.detail, self.path)


def fake_stat(size: int, mode: int = stat.S_IFREG | 0o644, nlink: int = 1,
              dev: int = 42, ino: int = 1):
    return types.SimpleNamespace(st_blocks=max(size, 0) // 512, st_nlink=nlink,
                                 st_mode=mode, st_dev=dev, st_ino=ino)


def _fail_after(entries, limit: int, path: str):
    for index, entry in enumerate(entries):
        if index == limit:
            raise OSError(5, "I/O error", path)
        yield entry


class SyntheticStream:
    """A lazy directory listing that records its own open/close order."""

    __slots__ = ("path", "entries", "audit", "delivered", "closed")

    def __init__(self, path, entries, audit):
        self.path = path
        self.entries = entries
        self.audit = audit
        self.delivered = 0
        self.closed = False

    def __iter__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __next__(self):
        entry = next(self.entries)      # StopIteration propagates to the caller
        self.delivered += 1
        return entry

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.audit.closed_handles += 1
        self.audit.live -= 1


class SyntheticAudit(HandleAudit):
    """spec: directory path -> zero-argument callable returning a fresh entry iterator."""

    def __init__(self, spec: dict, fail_at: dict | None = None):
        super().__init__()
        self.spec = spec
        self.fail_at = dict(fail_at or {})

    def scandir(self, path):
        if path not in self.spec:
            raise FileNotFoundError(2, "No such file or directory", path)
        entries = self.spec[path]()
        limit = self.fail_at.get(path)
        if limit is not None:
            entries = _fail_after(entries, limit, path)
        handle = SyntheticStream(path, entries, self)
        self._record_open(handle)
        return handle


def _long_name(kind: str, index: int, width: int) -> str:
    """A directory or file name exactly `width` characters long."""
    stem = f"{kind}{index:04d}-"
    return (stem + "abcdefghij" * width)[:width]


def pool_spec(root: str, n_files: int, file_bytes: int, child_files: int = 1_000) -> dict:
    """A flat directory of n_files loosely hanging files with short names."""
    child = f"{root}/sub"

    def loose():
        yield FakeEntry(child, "sub", fake_stat(0, mode=stat.S_IFDIR | 0o755), True)
        for i in range(n_files):
            yield FakeEntry(f"{root}/f{i:07d}.bin", f"f{i:07d}.bin",
                            fake_stat(file_bytes, ino=i + 1))

    def nested():
        for i in range(child_files):
            yield FakeEntry(f"{child}/g{i}.bin", f"g{i}.bin",
                            fake_stat(file_bytes, ino=10_000 + i))

    return {root: loose, child: nested}


def archive_spec(root: str, n_dirs: int, files_per_dir: int,
                 dir_len: int = 75, file_len: int = 60) -> dict:
    """A tree like a real archive: long directory chains above long file names.

    `root/<mid>/<leaf>/<file>`, with exactly `n_dirs` leaf directories holding
    `files_per_dir` files each. The point is not the entry count but the length of
    each entry's full path — `ARCHIVE_PATH_LEN` characters — because a materialized
    listing is charged in path characters: every DirEntry keeps its own name *and*
    its full path string. Short-named fixtures understate what the old walk cost,
    so both shapes are measured.
    """
    mids = max(1, min(10, n_dirs))
    per_mid = max(1, round(n_dirs / mids))
    n_dirs = mids * per_mid                       # what this shape really contains
    spec: dict = {}

    def top():
        for mid in range(mids):
            name = _long_name("migration-", mid, dir_len)
            yield FakeEntry(f"{root}/{name}", name,
                            fake_stat(0, mode=stat.S_IFDIR | 0o755), True)

    spec[root] = top
    for mid in range(mids):
        mid_path = f"{root}/{_long_name('migration-', mid, dir_len)}"

        def leaves(mid=mid, mid_path=mid_path):
            for offset in range(per_mid):
                name = _long_name("year-2019-", mid * per_mid + offset, dir_len)
                yield FakeEntry(f"{mid_path}/{name}", name,
                                fake_stat(0, mode=stat.S_IFDIR | 0o755), True)

        spec[mid_path] = leaves
        for offset in range(per_mid):
            leaf_path = f"{mid_path}/{_long_name('year-2019-', mid * per_mid + offset, dir_len)}"

            def files(leaf_path=leaf_path, first=(mid * per_mid + offset) * files_per_dir):
                for i in range(files_per_dir):
                    name = _long_name("invoice-", first + i, file_len)
                    yield FakeEntry(f"{leaf_path}/{name}", name,
                                    fake_stat(POOL_BYTES, ino=first + i + 1))

            spec[leaf_path] = files
    return spec


POOL_FILES = 200_000
POOL_BYTES = 4096

ARCHIVE_DIRS = 200
ARCHIVE_FILES_PER_DIR = POOL_FILES // ARCHIVE_DIRS       # 1000 files per directory
DEEP_DIRS = 8
DEEP_FILES_PER_DIR = 55_000       # every one of these directories is past the cap
#: Characters in one archived file's full path: two long directory names and a
#: long file name, under a short root. This is the unit peak memory is charged in:
#: a directory's listing holds one of these per entry, as a string.
ARCHIVE_PATH_LEN = len("/synthetic/pool") + 75 * 2 + 60 + 3

#: Bytes of Python per entry beyond the path text itself: the (path, size) tuple,
#: its list slot, and allocator slack. Measured 140 B/file at 2M files
#: (restic-box-scan-memory.md); 240 is the same number with room, so the bound
#: below is a ceiling the walk is expected to stay well under, not an estimate it
#: is expected to match.
ENTRY_OVERHEAD_BYTES = 240


def cap_bound_kib(path_len: int) -> int:
    """Peak KiB a streamed walk can reach in a directory past the planning cap.

    Before `scan()` discovers that a directory has too many files to plan
    individually it appends up to `MAX_LISTED_FILES` `(path, size)` tuples, then
    drops them. That partial list is the entire cost of the walk, so the ceiling is
    set by the cap and by how long the paths are — never by how many entries the
    directory has. Measured: 7.7 MiB for short names, 17.6 MiB at 228 characters,
    and 17.9 MiB against 18.1 MiB for 1M against 10M past-cap entries.
    """
    return split.MAX_LISTED_FILES * (path_len + ENTRY_OVERHEAD_BYTES) // 1024


# --------------------------------------------------------------------------
# 1. Streaming semantics
# --------------------------------------------------------------------------

class StreamsInsteadOfListsTest(unittest.TestCase):
    """A 200k-entry directory must cost one entry at a time."""

    root = "/synthetic/pool"

    def audit_scan(self, threshold: int = 1 * MIB, **kwargs):
        audit = SyntheticAudit(pool_spec(self.root, POOL_FILES, POOL_BYTES))
        with mock.patch.object(os, "scandir", audit.scandir):
            result = split.scan(self.root, [], threshold, **kwargs)
        return audit, result

    def test_a_child_is_opened_after_one_parent_entry_not_after_all_of_them(self):
        audit, _result = self.audit_scan()
        child = f"{self.root}/sub"
        parent_delivered = audit.delivered_when_opened(child, self.root)
        self.assertEqual(parent_delivered, 1,
                         "the walk descended into a child after reading 1 of its "
                         f"parent's {POOL_FILES} entries; materializing the whole "
                         "listing first is exactly what fix #8 removed")
        self.assertGreater(audit.peak_live, 1,
                           "streaming a nested tree keeps ancestor handles open; "
                           "the old walk never had two open at once")

    def test_every_entry_of_a_huge_directory_is_accounted(self):
        audit, (size, children, bigfiles, smallfiles) = self.audit_scan()
        self.assertEqual(size[self.root], (POOL_FILES + 1_000) * POOL_BYTES)
        self.assertEqual(size[f"{self.root}/sub"], 1_000 * POOL_BYTES)
        self.assertEqual(children[self.root], [f"{self.root}/sub"])
        self.assertEqual(audit.closed_handles, audit.opened,
                         "the walk left directory handles open")
        self.assertEqual(audit.live, 0)

    def test_the_file_list_cap_drops_planning_detail_not_coverage(self):
        """MAX_LISTED_FILES is about what the planner remembers, not what restic sends."""
        _audit, (size, children, bigfiles, smallfiles) = self.audit_scan()
        self.assertIsNone(smallfiles[self.root],
                          "above the cap the small-file list is dropped")
        self.assertEqual(size[self.root], (POOL_FILES + 1_000) * POOL_BYTES,
                         "dropping the list must not drop the bytes")
        units = split.plan_from_scan(self.root, [], 1 * MIB, size, children,
                                     bigfiles, smallfiles)
        rest = next(u for u in units if u.kind == "rest")
        carved = [pattern for pattern in rest.excludes if "/f" in pattern]
        self.assertEqual(carved, [],
                         "loose files the planner stopped listing must not be carved "
                         f"out of the rest unit: {carved[:3]}")
        self.assertIn(split.escape_pattern(f"{self.root}/sub"), rest.excludes,
                      "the child directory that is its own unit is excluded")
        self.assertGreaterEqual(sum(u.size for u in units), size[self.root])

    def test_a_deep_archive_holds_one_handle_per_level_not_one_per_directory(self):
        """Handles stay along the path down the tree, never across the whole tree.

        The archive shape a user actually has: 200 long-named directories under
        two levels of long-named parents. A streaming walk needs three live
        directory handles (root, mid, leaf) no matter how many files hang off
        them; anything larger means listings are being collected, not consumed.
        """
        audit = SyntheticAudit(archive_spec(self.root, ARCHIVE_DIRS, ARCHIVE_FILES_PER_DIR))
        with mock.patch.object(os, "scandir", audit.scandir):
            size, _children, _big, _small = split.scan(self.root, [], 1 * MIB)
        self.assertEqual(audit.peak_live, 3,
                         f"the walk held {audit.peak_live} directory handles at once "
                         "in a three-level tree; one per level is all streaming needs")
        self.assertEqual(audit.closed_handles, audit.opened)
        self.assertEqual(len(size), 1 + ARCHIVE_DIRS // 20 + ARCHIVE_DIRS)

    def test_the_archived_paths_are_long_enough_to_be_worth_measuring(self):
        """Guard the fixture: a short-named tree understates the old walk's cost.

        The pre-fix walk's peak memory is charged in path characters — one full
        path string per entry, held for the whole time the directory is being
        walked — so an archive fixture with 25-character paths measures nothing
        useful. This checks the tree really is the long-path shape the memory
        tests claim to be measuring.
        """
        spec = archive_spec(self.root, ARCHIVE_DIRS, ARCHIVE_FILES_PER_DIR)
        leaves = [p for p in spec if p.count("/") == self.root.count("/") + 2]
        self.assertEqual(len(leaves), ARCHIVE_DIRS)
        entry = next(iter(spec[leaves[0]]()))
        self.assertEqual(len(entry.path), ARCHIVE_PATH_LEN,
                         f"an archived file's path is {len(entry.path)} characters, "
                         f"not the {ARCHIVE_PATH_LEN} the memory accounting assumes")
        self.assertGreater(ARCHIVE_PATH_LEN, 200,
                           "the fixture's paths must be long enough that holding one "
                           "per entry is a real cost")
        # 55k entries in one directory: the widest listing the old walk had to hold
        widest = DEEP_FILES_PER_DIR * ARCHIVE_PATH_LEN
        self.assertGreater(widest, 10 * 1024 * 1024,
                           f"the widest deep directory carries only {widest // 1024} "
                           "KiB of path text: too small to tell the two walks apart")


# --------------------------------------------------------------------------
# 2. Accounting is unchanged
# --------------------------------------------------------------------------

def _comparable(dicts):
    """List contents compared as sets: readdir order is not part of the contract."""
    return [{key: sorted(value) if isinstance(value, list) else value
             for key, value in d.items()} for d in dicts]


class AccountingUnchangedTest(unittest.TestCase):
    """Streaming must produce exactly what the materializing walk produced."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-stream-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "pool"
        (self.root / "deep" / "nested").mkdir(parents=True)
        (self.root / "small").mkdir()
        (self.root / "big").mkdir()
        for i in range(40):
            (self.root / f"f{i}.bin").write_bytes(b"x" * 2048)
        for i in range(10):
            (self.root / "small" / f"s{i}.bin").write_bytes(b"y" * 1024)
        for i in range(5):
            (self.root / "deep" / "nested" / f"n{i}.bin").write_bytes(b"z" * 8192)
        (self.root / "big" / "data.bin").write_bytes(b"b" * 6000)
        # shapes the walk has to keep handling exactly as before
        (self.root / ".zfs").mkdir()
        (self.root / ".zfs" / "snap.bin").write_bytes(b"s" * 1024)
        (self.root / "skipme").mkdir()
        (self.root / "skipme" / "a.bin").write_bytes(b"a" * 1024)
        os.symlink("/definitely-not-followed", self.root / "link.bin")
        os.mkfifo(self.root / "pipe")
        os.link(self.root / "f0.bin", self.root / "hard.bin")

    def _compare(self, excludes, threshold):
        new = split.scan(str(self.root), excludes, threshold)
        old = _materialized_scan(str(self.root), excludes, threshold)
        for index, name in ((0, "size"), (1, "children"), (2, "bigfiles"),
                            (3, "smallfiles")):
            self.assertEqual(_comparable([new[index]])[0], _comparable([old[index]])[0],
                             f"{name} changed when the walk started streaming")
        return new

    def test_scan_results_are_identical_to_the_materialized_walk(self):
        size, children, bigfiles, smallfiles = self._compare([], 4096)
        self.assertIn(str(self.root / "deep"), children[str(self.root)])
        self.assertTrue(any(p.endswith("data.bin")
                            for p, _sz in bigfiles[str(self.root / "big")]))

    def test_excludes_and_thresholds_are_still_honoured(self):
        self._compare(["/skipme", "*.bin"], 1024)
        self._compare([], 10 * MIB)

    def test_zfs_snapshots_are_still_never_touched(self):
        _size, children, _big, _small = split.scan(str(self.root), [], 4096)
        names = [Path(child).name for child in children[str(self.root)]]
        self.assertNotIn(".zfs", names)

    def test_plan_from_a_streamed_scan_builds_the_same_units(self):
        size, children, bigfiles, smallfiles = split.scan(str(self.root), [], 4096)
        units = split.plan_from_scan(str(self.root), [], 4096, size, children,
                                     bigfiles, smallfiles)
        self.assertIn("rest", {u.kind for u in units})
        owned = set()
        for unit in units:
            owned.update(unit.paths or [unit.path])
        self.assertTrue(any(p.endswith("data.bin") for p in owned),
                        f"data.bin is owned by no unit: {sorted(owned)[:6]}")


# --------------------------------------------------------------------------
# 3. An unreadable directory is reported, not mistaken for an empty one
# --------------------------------------------------------------------------

class UnreadableDirectoryTest(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a mode-000 directory")
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-unread-"))
        self.root = self.tmp / "pool"
        self.locked = self.root / "locked"
        self.locked.mkdir(parents=True)
        (self.locked / "secret.bin").write_bytes(b"x" * 4096)
        (self.root / "open.bin").write_bytes(b"y" * 4096)
        self.logs: list[str] = []

    def tearDown(self):
        for path in [self.locked] + [self.root / f"locked{i}" for i in range(20)]:
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lock(self, path: Path) -> None:
        """Make a directory unreadable, or skip the test if this system won't."""
        os.chmod(path, 0o000)
        if os.access(path, os.R_OK):
            os.chmod(path, 0o755)
            self.skipTest("a mode-000 directory is still readable here")

    def test_an_unreadable_directory_is_reported_not_treated_as_empty(self):
        self.lock(self.locked)
        errors: dict[str, str] = {}
        split.scan(str(self.root), [], 1024, self.logs.append, errors=errors)
        self.assertIn(str(self.locked), errors,
                      "an unreadable directory must be named, not counted as empty")
        self.assertTrue(any("Cannot read directory" in line and str(self.locked) in line
                           for line in self.logs), self.logs)
        self.assertTrue(any("could not be read" in line for line in self.logs),
                        "the scan must end with a summary the user can see")

    def test_the_error_is_visible_even_without_the_optional_dict(self):
        self.lock(self.locked)
        split.scan(str(self.root), [], 1024, self.logs.append)
        self.assertTrue(any("Cannot read directory" in line for line in self.logs),
                        self.logs)

    def test_many_unreadable_directories_are_counted_even_when_loud_lines_are_capped(self):
        hidden = []
        for i in range(split.MAX_NOTED_ERRORS + 3):
            directory = self.root / f"locked{i}"
            directory.mkdir()
            (directory / "f.bin").write_bytes(b"x" * 512)
            os.chmod(directory, 0o000)
            if os.access(directory, os.R_OK):
                self.skipTest("a mode-000 directory is still readable here")
            hidden.append(directory)
        errors: dict[str, str] = {}
        split.scan(str(self.root), [], 1024, self.logs.append, errors=errors)
        loud = [line for line in self.logs if "Cannot read directory" in line]
        self.assertLessEqual(len(loud), split.MAX_NOTED_ERRORS,
                             "a tree with many problems must not flood the log")
        self.assertEqual(len(errors), len(hidden),
                         "capping the log lines must not cap what is recorded")
        summary = [line for line in self.logs if "could not be read" in line]
        self.assertTrue(summary, self.logs)
        self.assertIn(str(len(hidden)), summary[-1])

    def test_a_readdir_failure_partway_through_a_listing_is_reported(self):
        """The listing opened fine and broke later — also not "empty"."""
        root = "/synthetic/pool"
        entries = [FakeEntry(f"{root}/f{i}.bin", f"f{i}.bin", fake_stat(4096, ino=i))
                   for i in range(50)]
        audit = SyntheticAudit({root: lambda: iter(list(entries))}, fail_at={root: 20})
        errors: dict[str, str] = {}
        with mock.patch.object(os, "scandir", audit.scandir):
            size, _children, _big, _small = split.scan(root, [], 1 * MIB,
                                                        self.logs.append, errors=errors)
        self.assertIn(root, errors)
        self.assertTrue(any("Cannot read directory" in line for line in self.logs),
                        self.logs)
        self.assertEqual(size[root], 20 * 4096,
                         "what was read before the failure is still counted")
        self.assertEqual(audit.closed_handles, audit.opened)

    def test_an_unreadable_entry_is_counted_not_skipped_in_silence(self):
        root = "/synthetic/pool"
        broken = FailingEntry(f"{root}/broken.bin")
        entries = [FakeEntry(f"{root}/ok.bin", "ok.bin", fake_stat(4096, ino=1)), broken]
        audit = SyntheticAudit({root: lambda: iter(list(entries))})
        with mock.patch.object(os, "scandir", audit.scandir):
            split.scan(root, [], 1 * MIB, self.logs.append)
        self.assertTrue(any("Cannot read" in line and "broken.bin" in line
                           for line in self.logs), self.logs)


# --------------------------------------------------------------------------
# 4. Cancellation releases every outstanding directory handle
# --------------------------------------------------------------------------

class CancellationReleasesHandlesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-cancel-handles-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in range(6):
            sub = self.tmp / f"d{d}"
            sub.mkdir()
            for f in range(20):
                (sub / f"f{f}.bin").write_bytes(b"x" * 512)

    def _audit_scan(self, should_stop):
        audit = HandleAudit()
        with mock.patch.object(os, "scandir", audit.scandir):
            with self.assertRaises(split.SplitCancelled):
                split.scan(str(self.tmp), [], 1024, None, should_stop)
        return audit

    def test_cancelling_mid_walk_closes_every_open_directory(self):
        seen = []

        def should_stop():
            seen.append(1)
            return len(seen) > 4            # stop while the walk is partway down

        audit = self._audit_scan(should_stop)
        self.assertGreater(audit.opened, 1, "the walk should have descended")
        self.assertTrue(all(handle.closed for handle in audit.handles),
                        "a cancelled scan left directory handles open")
        self.assertEqual(audit.live, 0)

    def test_cancelling_before_a_newly_opened_directory_is_read(self):
        """A handle opened moments before the Stop must be released too.

        This is the case a generator-based stream gets wrong: closing a
        generator that never started runs no body, so the scandir iterator it
        captured leaks until garbage collection.
        """
        audit = HandleAudit()
        with mock.patch.object(os, "scandir", audit.scandir):
            # Stop once the walk has opened a child directory it has not read.
            with self.assertRaises(split.SplitCancelled):
                split.scan(str(self.tmp), [], 1024, None, lambda: audit.opened > 1)
        self.assertGreater(audit.opened, 1)
        self.assertEqual([handle.path for handle in audit.handles if not handle.closed],
                         [], "unclosed handles after cancellation")
        self.assertEqual(audit.live, 0)

    def test_cancellation_raises_no_resource_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            self._audit_scan(lambda: True)
            gc.collect()

    def test_a_plain_scan_closes_its_handles_too(self):
        audit = HandleAudit()
        with mock.patch.object(os, "scandir", audit.scandir):
            split.scan(str(self.tmp), [], 1024)
        self.assertEqual(audit.closed_handles, audit.opened)
        self.assertEqual(audit.live, 0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            gc.collect()


# --------------------------------------------------------------------------
# 5. Peak RSS and duration, measured in child processes
# --------------------------------------------------------------------------

def _peak_rss_kib() -> int:
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _spec_for(shape: str, root: str) -> dict | None:
    """The synthetic trees. "real" returns None: that shape walks a real directory."""
    if shape == "flat":
        return pool_spec(root, POOL_FILES, POOL_BYTES)
    if shape == "deep-capped":
        # 200 directories of 1000 files each: every one under the planning cap,
        # so both walks keep their small-file lists. The realistic archive.
        return archive_spec(root, ARCHIVE_DIRS, ARCHIVE_FILES_PER_DIR)
    if shape == "deep-uncapped":
        # 8 directories of 55,000 files each: every one past the cap, so neither
        # walk keeps a per-file list and only the listing is left to charge for.
        return archive_spec(root, DEEP_DIRS, DEEP_FILES_PER_DIR)
    return None


def _bench(strategy: str, shape: str, root: str, threshold: int) -> dict:
    """Runs in a child process so VmHWM measures exactly one walk.

    strategy: "streamed" (the app) or "materialized" (the pre-fix reference).
    shape: "flat", "deep-capped", "deep-uncapped" — lazily generated trees with
    no filesystem, so the only DirEntry-shaped objects alive are the walk's own —
    or "real", a directory that actually exists, with real os.DirEntry objects.
    In every pair the only thing that varies is streaming versus materializing.
    """
    floor = _peak_rss_kib()
    started = time.monotonic()
    materializing = strategy == "materialized"
    spec = _spec_for(shape, root)
    handles = (0, 0)
    if spec is None:
        size, _children, _big, smallfiles = _walk(materializing, root, threshold)
    else:
        audit = SyntheticAudit(spec)
        with mock.patch.object(os, "scandir", audit.scandir):
            size, _children, _big, smallfiles = _walk(materializing, root, threshold)
        handles = (audit.opened, audit.closed_handles)
    lists = list(smallfiles.values())
    return {"strategy": strategy, "shape": shape,
            "seconds": round(time.monotonic() - started, 3),
            "peak_kib": _peak_rss_kib(), "floor_kib": floor,
            "total_bytes": size[root], "directories": len(size),
            "handles_open": handles[0], "handles_closed": handles[1],
            "lists_dropped": sum(1 for listing in lists if listing is None),
            "small_entries_retained": sum(len(listing) for listing in lists
                                          if listing is not None)}


def _walk(materializing: bool, root: str, threshold: int):
    if materializing:
        return _materialized_scan(root, [], threshold)
    return split.scan(root, [], threshold)


_BENCH_CACHE: dict[tuple, dict] = {}


def run_bench(strategy: str, shape: str, root: str = "/synthetic/pool") -> dict:
    """One bench walk in a fresh interpreter, memoized per test process."""
    key = (strategy, shape, root)
    if key not in _BENCH_CACHE:
        proc = subprocess.run(
            [sys.executable, "-m", "tests.test_split_streaming", "bench", strategy,
             shape, root, str(1 * MIB)],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"bench {strategy}/{shape} failed: {proc.stderr}")
        _BENCH_CACHE[key] = json.loads(proc.stdout)
    return _BENCH_CACHE[key]


def overhead(result: dict) -> int:
    """Peak RSS the walk itself added, in KiB: peak minus the process floor."""
    return result["peak_kib"] - result["floor_kib"]


def mib(kib: int) -> str:
    return f"{kib // 1024} MiB"


def assert_walks_agree(tc: unittest.TestCase, streamed: dict, materialized: dict) -> None:
    """Cheap must not mean different: both walks must agree on the tree."""
    label = f"{streamed['shape']} ({streamed['directories']} directories)"
    tc.assertEqual(streamed["total_bytes"], materialized["total_bytes"],
                   f"{label}: the streamed walk counted different bytes")
    tc.assertEqual(streamed["directories"], materialized["directories"],
                   f"{label}: the streamed walk found a different number of "
                   "directories")
    tc.assertLess(streamed["seconds"], materialized["seconds"] * 1.5 + 0.5,
                  f"{label}: streamed {streamed['seconds']}s against "
                  f"materialized {materialized['seconds']}s — streaming must not "
                  "make the walk meaningfully slower")


class MemoryMeasurementTest(unittest.TestCase):
    """Peak RSS and duration, each pair measured in fresh child processes:
    streamed (the app) against materialized (the pre-fix reference), the same
    tree both times, so what is reported is what fix #8 changed.

    What peak memory means for each walk, which is why the pairs are shaped as
    they are:

      * the old walk's peak is one directory's whole listing — every DirEntry
        with its own name and full path strings — held for as long as that
        directory and everything below it are being walked, with every ancestor's
        listing held alongside it. Its peak is therefore set by the widest
        directory on the way down, and by how long the paths inside it are;
      * the streamed walk holds one entry at a time, so what it still keeps is
        the planner's own per-directory small-file list, which exists only where
        a directory stayed under MAX_LISTED_FILES.
    """

    root = "/synthetic/pool"

    def test_streaming_costs_a_fraction_of_the_peak_memory_it_replaced(self):
        """200k loose files in one short-named directory, past the planning cap."""
        streamed = run_bench("streamed", "flat", self.root)
        materialized = run_bench("materialized", "flat", self.root)
        assert_walks_agree(self, streamed, materialized)
        self.assertEqual(streamed["lists_dropped"], 1)
        self.assertEqual(streamed["small_entries_retained"], 1_000,
                         "the 200k-file pool's own list is dropped, and only that "
                         "one: the 1000-file child directory under it stays small "
                         "enough to plan individually, so its list survives")
        self.assertGreater(overhead(materialized), 40 * 1024,
                           f"materializing {POOL_FILES} entries added only "
                           f"{mib(overhead(materialized))}: the fixture is not holding "
                           "a materialized listing")
        self.assertLess(overhead(streamed), overhead(materialized) // 4,
                        f"streamed scan added {mib(overhead(streamed))} against "
                        f"{mib(overhead(materialized))} for materializing the same "
                        f"{POOL_FILES} entries")

    def test_a_huge_directory_costs_bounded_memory_and_releases_every_handle(self):
        streamed = run_bench("streamed", "flat", self.root)
        self.assertEqual(streamed["handles_closed"], streamed["handles_open"])
        bound = cap_bound_kib(len(f"{self.root}/f0000000.bin"))
        self.assertLess(overhead(streamed), bound,
                        f"scanning {POOL_FILES} entries cost "
                        f"{mib(overhead(streamed))} of peak RSS, over the "
                        f"{mib(bound)} a walk that holds at most "
                        f"{split.MAX_LISTED_FILES} entries is allowed")

    def test_long_paths_do_not_change_what_a_streaming_walk_holds(self):
        """The same entry count under long directory chains: 8 x 55,000 files.

        Every directory here is past the planning cap, so neither walk keeps a
        per-file list, and the only cost left is the listing itself. Each entry's
        full path is ARCHIVE_PATH_LEN characters: the widest directory alone
        carries ~12 MiB of path text, which is what the old walk had to hold — as
        DirEntry objects, each with its own strings — while the new one holds a
        bounded prefix of the listing and then drops it.
        """
        streamed = run_bench("streamed", "deep-uncapped", self.root)
        materialized = run_bench("materialized", "deep-uncapped", self.root)
        assert_walks_agree(self, streamed, materialized)
        self.assertEqual(streamed["lists_dropped"], DEEP_DIRS)
        self.assertEqual(streamed["small_entries_retained"], 0)
        self.assertEqual(streamed["directories"], 1 + 2 * DEEP_DIRS,
                         "the root, one long-named directory per leaf, and the "
                         f"{DEEP_DIRS} leaf directories that hold the files")

        widest_path_text = DEEP_FILES_PER_DIR * ARCHIVE_PATH_LEN
        self.assertGreater(overhead(materialized), widest_path_text // 1024,
                           f"materializing a directory whose entries carry "
                           f"{mib(widest_path_text // 1024)} of path text added only "
                           f"{mib(overhead(materialized))}: the listing is not held")
        bound = cap_bound_kib(ARCHIVE_PATH_LEN)
        self.assertLess(overhead(streamed), bound,
                        f"streaming {DEEP_DIRS} directories of {DEEP_FILES_PER_DIR} "
                        f"long-named entries cost {mib(overhead(streamed))} of peak "
                        f"RSS, over the {mib(bound)} the planning cap allows: path "
                        "length must not be what the walk is charged for")
        self.assertLess(overhead(streamed), overhead(materialized) * 3 // 4,
                        f"streamed {mib(overhead(streamed))} against materialized "
                        f"{mib(overhead(materialized))} on the same long-named tree")

    def test_a_deep_archive_under_the_cap_costs_the_same_either_way(self):
        """200 directories of 1000 long-named files: none of them past the cap.

        This is the honest half of fix #8. Both walks end up holding 200k
        (path, size) tuples, because a directory small enough to plan
        individually has to be *remembered* individually: streaming removes the
        listing, not the planning data. So the saving here is a few hundred KiB,
        not an order of magnitude, and the test says so instead of pretending —
        what it requires is that streaming never costs *more*, and that the cost
        that remains is the planner's own retention (which fix #8's brief says not
        to cap). Reducing that is prefix-sharing, deliberately deferred; see
        restic-box-scan-memory.md.
        """
        streamed = run_bench("streamed", "deep-capped", self.root)
        materialized = run_bench("materialized", "deep-capped", self.root)
        assert_walks_agree(self, streamed, materialized)
        self.assertEqual(streamed["lists_dropped"], 0)
        self.assertEqual(streamed["small_entries_retained"],
                         ARCHIVE_DIRS * ARCHIVE_FILES_PER_DIR)
        self.assertLess(overhead(streamed), overhead(materialized) + 4 * 1024,
                        f"streamed {mib(overhead(streamed))} against materialized "
                        f"{mib(overhead(materialized))}: streaming must not be the "
                        "more expensive way to walk a tree the planner fully remembers")
        saved = overhead(materialized) - overhead(streamed)
        self.assertLess(saved, overhead(materialized) // 4,
                        f"streaming a capped {ARCHIVE_DIRS}-directory archive saved "
                        f"{mib(saved)} of {mib(overhead(materialized))}: a saving this "
                        "large here would mean the planner stopped remembering files")

    def test_how_much_memory_the_planners_own_lists_cost(self):
        """The cost fix #8 deliberately leaves in place, as a measured number.

        MAX_LISTED_FILES keeps one (path, size) tuple per small file per
        directory, and those tuples carry full paths too. Streaming cannot make
        that free: it is planning data the walk hands the planner, and the brief
        for fix #8 says not to add new caps. Measured on this shape, and scaled up
        outside the suite (restic-box-scan-memory.md): 378 B per file at 228
        characters, 72 MiB for 200k files, 372 MiB for 1M, 774 MiB for 2M — two
        thirds of it path text. These thresholds pin that order of magnitude so a
        future change to the cap, or the prefix-sharing and file-list-cache work,
        has something to be compared against.
        """
        streamed = run_bench("streamed", "deep-capped", self.root)
        tuples = ARCHIVE_DIRS * ARCHIVE_FILES_PER_DIR
        path_text = tuples * ARCHIVE_PATH_LEN
        self.assertEqual(streamed["small_entries_retained"], tuples)
        self.assertGreater(overhead(streamed), path_text // 1024 // 2,
                           f"{tuples} retained (path, size) tuples cost only "
                           f"{mib(overhead(streamed))}, less than half of the "
                           f"{mib(path_text // 1024)} of path text inside them")
        self.assertLess(overhead(streamed), 250 * 1024,
                        f"the planner's retained lists alone cost "
                        f"{mib(overhead(streamed))} for {tuples} files: worth "
                        "revisiting, not silently growing")


@unittest.skipUnless(sys.platform.startswith("linux"), "/proc/self/status for peak RSS")
class RealDirectoryMemoryTest(unittest.TestCase):
    """The same comparison on a directory that really exists, with real DirEntry
    objects and real long paths: the shape the planner actually meets."""

    def setUp(self):
        self.root = str(huge_directory())

    def test_a_real_huge_directory_costs_less_to_walk(self):
        streamed = run_bench("streamed", "real", self.root)
        materialized = run_bench("materialized", "real", self.root)
        assert_walks_agree(self, streamed, materialized)
        saved = overhead(materialized) - overhead(streamed)
        self.assertGreater(saved, 8 * 1024,
                           f"streaming {HUGE_DIR_FILES} entries saved {mib(saved)} "
                           f"of peak RSS (streamed {mib(overhead(streamed))} vs "
                           f"materialized {mib(overhead(materialized))}): the walk is "
                           "not streaming")
        widest_path_text = HUGE_DIR_FILES * huge_directory_path_len()
        self.assertGreater(overhead(materialized), widest_path_text // 1024,
                           f"materializing {HUGE_DIR_FILES} entries whose paths total "
                           f"{mib(widest_path_text // 1024)} added only "
                           f"{mib(overhead(materialized))}")
        bound = cap_bound_kib(huge_directory_path_len())
        self.assertLess(overhead(streamed), bound,
                        f"the streamed walk over a real directory of {HUGE_DIR_FILES} "
                        f"long-named files cost {mib(overhead(streamed))}, over the "
                        f"{mib(bound)} the planning cap allows")
        self.assertEqual(streamed["lists_dropped"], 1,
                         "the fixture's pool directory should be past the planning cap")




# --------------------------------------------------------------------------
# 6. Coverage end to end: the planning cap must never cost a file
# --------------------------------------------------------------------------

# The planner measures a file as allocated bytes (`st_blocks * 512`), so these
# fixtures only mean something on a filesystem that accounts blocks honestly. The
# approved scratch directory is a ZFS dataset with `compression=on`, which does
# not: a freshly written 4 MiB file reported 140 KiB there. So the default home for
# a fixture whose *sizes* matter is tmpfs. Override with RESTIC_BOX_TEST_TMPDIR —
# `FixtureFilesystemTest` fails loudly if the override lands somewhere that lies
# about blocks.
_SCRATCH = "/tmp"
_FIXTURES: dict[str, Path] = {}

HUGE_DIR_FILES = 60_000           # > MAX_LISTED_FILES (50_000)
HUGE_DIR_LEVELS = 3               # long-named directory levels above the loose files
HUGE_DIR_NAME_LEN = 75
HUGE_FILE_NAME_LEN = 60


def fixture_base() -> Path:
    """Scratch space for the big fixtures, created on demand and removed on exit.

    A few hundred megabytes of throwaway files on tmpfs (override with
    RESTIC_BOX_TEST_TMPDIR), chosen because the planner measures sizes as
    `st_blocks * 512` and tmpfs accounts blocks truthfully. Peak RSS is read from
    the walking process, so where the fixture lives does not enter the memory
    measurement — only what the walk itself holds does.
    """
    if "base" not in _FIXTURES:
        wanted = Path(os.environ.get("RESTIC_BOX_TEST_TMPDIR") or _SCRATCH)
        base = wanted / f"restic-box-scan-tests-{os.getpid()}"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:                       # scratch unusable: fall back to a temp dir
            base = Path(tempfile.mkdtemp(prefix="rb-fixture-"))
        atexit.register(shutil.rmtree, base, True)
        _FIXTURES["base"] = base
    return _FIXTURES["base"]


class FixtureFilesystemTest(unittest.TestCase):
    """Every size expectation in this module is an *allocated bytes* expectation,
    because that is what `restic_split` measures (`st_blocks * 512`). On a
    filesystem that does not account blocks honestly — the ZFS scratch dataset,
    `compression=on`, which reported 140 KiB for a freshly written 4 MiB file — the
    split planner would see a different tree than the fixture claims to be, and the
    coverage and carve-out assertions below would be testing nothing. This checks
    the ground before trusting it.
    """

    def test_the_fixture_filesystem_accounts_for_written_bytes(self):
        probe = fixture_base() / f"block-probe-{os.getpid()}.bin"
        payload = (b"compressible" * (4 * MIB // 12 + 1))[:4 * MIB]
        probe.write_bytes(payload)
        st = os.stat(probe)
        probe.unlink(missing_ok=True)
        self.assertEqual(st.st_size, 4 * MIB)
        self.assertGreaterEqual(
            st.st_blocks * 512, 4 * MIB,
            f"{probe.parent} reported {st.st_blocks * 512} allocated bytes for a "
            f"{st.st_size} byte file: the split planner sizes files as "
            "st_blocks * 512, so a compressing filesystem makes every size "
            "assertion in this module measure a tree that is not there. Run the "
            "tests with RESTIC_BOX_TEST_TMPDIR on a filesystem that does not "
            "compress (tmpfs, or btrfs with incompressible content).")


def huge_directory() -> Path:
    """A real directory past the planner's list cap, built once, with long paths.

    Three long-named directory levels, 60,000 loose files with 60-character names
    in the deepest one, and a child big enough to be carved out of the "rest"
    unit. Both halves of the point are here: the path text a listing of this
    directory has to hold, and a directory whose list the planner really drops.
    """
    if "huge" not in _FIXTURES:
        root = fixture_base() / "pool"
        for level in range(HUGE_DIR_LEVELS):
            root = root / _long_name("archive-migration-", level, HUGE_DIR_NAME_LEN)
        (root / "archive").mkdir(parents=True)
        (root / "archive" / "data.bin").write_bytes(b"b" * 4 * MIB)
        for i in range(HUGE_DIR_FILES):
            (root / _long_name("invoice-", i, HUGE_FILE_NAME_LEN)).write_bytes(b"x" * 512)
        _FIXTURES["huge"] = root
    return _FIXTURES["huge"]


def huge_directory_path_len() -> int:
    """Characters in one of the pool directory's file paths, measured for real."""
    return len(str(huge_directory())) + 1 + HUGE_FILE_NAME_LEN


@unittest.skipUnless(shutil.which("restic"), "restic not installed")
class HugeDirectoryCoverageTest(unittest.TestCase):
    def setUp(self):
        self.root = huge_directory()
        self.logs: list[str] = []
        self._real_config = split.CONFIG_DIR
        split.CONFIG_DIR = fixture_base() / "config"

    def tearDown(self):
        split.CONFIG_DIR = self._real_config

    def test_every_file_lands_in_the_repository_even_past_the_list_cap(self):
        root = os.path.normpath(str(self.root))
        units = split.build_plan(root, [], 1 * MIB, self.logs.append)
        repo = str(fixture_base() / "repo")
        env = dict(os.environ, RESTIC_PASSWORD="x", RESTIC_REPOSITORY=repo)
        subprocess.run(["restic", "init", "-q"], env=env, check=True)
        for unit in units:
            args = ["restic", "backup", "-q", *unit.targets()]
            for pattern in unit.excludes:
                args += ["--exclude", pattern]
            subprocess.run(args, env=env, check=True)

        snaps = json.loads(subprocess.run(["restic", "snapshots", "--json"], env=env,
                                          capture_output=True, text=True,
                                          check=True).stdout)
        self.assertTrue(snaps, "the plan produced no snapshots")
        seen: set[str] = set()
        for snap in snaps:
            listing = subprocess.run(["restic", "ls", "--json", snap["id"]], env=env,
                                     capture_output=True, text=True, check=True)
            for line in listing.stdout.splitlines():
                record = json.loads(line)
                if record.get("type") == "file":
                    seen.add(record["path"])

        expected = set()
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                expected.add(os.path.join(dirpath, name))
        self.assertGreater(len(expected), split.MAX_LISTED_FILES,
                           "the fixture should be past the planning cap")
        missing = expected - seen
        self.assertFalse(missing,
                         f"{len(missing)} of {len(expected)} files were never backed up "
                         f"although the only thing 'wrong' with them is that their "
                         f"directory's list was dropped: {sorted(missing)[:5]}")
        self.assertEqual(seen, expected)

    def test_the_dropped_list_does_not_change_the_plans_ownership_of_carved_dirs(self):
        root = os.path.normpath(str(self.root))
        units = split.build_plan(root, [], 1 * MIB, self.logs.append)
        kinds = [unit.kind for unit in units]
        self.assertIn("rest", kinds)
        rest = next(u for u in units if u.kind == "rest")
        self.assertIn(split.escape_pattern(os.path.join(root, "archive")), rest.excludes,
                      "the carved child must be excluded from the rest unit")
        owned = {u.path for u in units if u.kind in ("dir", "rest", "file", "group")}
        self.assertIn(os.path.join(root, "archive"), owned,
                      "an excluded path must be owned by a surviving unit")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "bench":
        print(json.dumps(_bench(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))))
    else:
        unittest.main()
