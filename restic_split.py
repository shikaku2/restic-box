"""Auto-split big backup sources into stable, independently resumable units.

Why: restic writes a snapshot only when a run finishes, and it picks a parent
snapshot only when host + path set match. One giant `restic backup /pool` that
dies on day 9 leaves nothing to resume from. Split into units that each finish
(and snapshot) on their own, and every re-run skips finished units cheaply.

Rules (T = threshold):
  * A directory under T is one unit, untouched. Existing snapshots of it stay
    valid parents.
  * A directory >= T is split. Each child directory >= T is split recursively.
    Each loose file >= T becomes its own unit (restic accepts file targets).
  * Everything else in that directory (small children, small loose files,
    anything created later) goes into one "rest" unit: the directory itself with
    the big children/files excluded. Coverage is guaranteed by restic's own
    excludes, not by the scan, so a stale plan can never drop files.

Sizes are allocated bytes (st_blocks * 512, like `du`), so sparse images count
for what they actually hold.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

GIB = 1024 ** 3

LogFn = Optional[Callable[[str], None]]


@dataclass
class Unit:
    path: str
    excludes: list[str]
    size: float  # allocated bytes at scan time (progress weighting only)
    kind: str    # "dir" | "rest" | "file" | "group"
    paths: list[str] = field(default_factory=list)  # backup targets; empty -> [path]

    def targets(self) -> list[str]:
        return self.paths or [self.path]


def escape_pattern(path: str) -> str:
    """Escape restic glob metacharacters so an exclude matches this path only.

    Unescaped, a dir named "[Judas] Show" is treated as a character class and
    silently fails to exclude (verified on restic 0.16.4).
    """
    return re.sub(r"([\\\[\]*?])", r"\\\1", path)


def _matcher(patterns: list[str]) -> Callable[[str, str], bool]:
    """Approximate restic excludes. Only used to skip work while sizing, never
    to decide what gets backed up, so over/under-matching just skews sizes."""
    anchored = [p.rstrip("/") for p in patterns if p.startswith("/")]
    loose = [p.rstrip("/") for p in patterns if p and not p.startswith("/")]

    def match(path: str, name: str) -> bool:
        for p in anchored:
            if path == p or fnmatch.fnmatchcase(path, p):
                return True
        for p in loose:
            if "/" in p:
                if fnmatch.fnmatchcase(path, "*/" + p):
                    return True
            elif fnmatch.fnmatchcase(name, p):
                return True
        return False

    return match


def _listdir(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def scan(root: str, excludes: list[str], threshold: int, log: LogFn = None):
    """Iterative post-order walk. Returns (size, children, bigfiles) dicts keyed
    by directory path. Never follows symlinks. Always skips `.zfs` (touching it
    auto-mounts snapshots)."""
    match = _matcher(excludes)
    size: dict[str, int] = {}
    children: dict[str, list[str]] = {}
    bigfiles: dict[str, list[tuple[str, int]]] = {}
    seen_inodes: set[tuple[int, int]] = set()
    n_entries = 0
    last_log = time.monotonic()

    # frame: [path, entries, index, bytes, child_dirs, big_files]
    frames = [[root, _listdir(root), 0, 0, [], []]]
    while frames:
        f = frames[-1]
        if f[2] >= len(f[1]):
            frames.pop()
            size[f[0]], children[f[0]], bigfiles[f[0]] = f[3], f[4], f[5]
            if frames:
                frames[-1][3] += f[3]
                frames[-1][4].append(f[0])
            continue
        e = f[1][f[2]]
        f[2] += 1
        n_entries += 1
        if log and n_entries % 5000 == 0 and time.monotonic() - last_log > 10:
            last_log = time.monotonic()
            log(f"Scanning {root}: {n_entries} entries, "
                f"{(f[3] + sum(fr[3] for fr in frames[:-1])) / GIB:.0f} GiB so far")
        if e.name == ".zfs" or match(e.path, e.name):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                frames.append([e.path, _listdir(e.path), 0, 0, [], []])
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
        if stat.S_ISREG(st.st_mode) and sz >= threshold:
            f[5].append((e.path, sz))
    return size, children, bigfiles


def build_plan(root: str, excludes: list[str], threshold: int, log: LogFn = None) -> list[Unit]:
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return [Unit(root, list(excludes), 0.0, "dir")]
    size, children, bigfiles = scan(root, excludes, threshold, log)
    return plan_from_scan(root, excludes, threshold, size, children, bigfiles, log)


def plan_from_scan(root: str, excludes: list[str], threshold: int,
                   size: dict, children: dict, bigfiles: dict, log: LogFn = None) -> list[Unit]:
    """Pure planning step (no filesystem access), split out so it can be tested."""
    base = list(excludes)

    def pack(paths: list[str]) -> list[list[str]]:
        """First-fit-decreasing into bins of at most `threshold` bytes."""
        bins: list[list] = []  # [used, [paths]]
        for c in sorted(paths, key=lambda c: (-size[c], c)):
            for b in bins:
                if b[0] + size[c] <= threshold:
                    b[0] += size[c]
                    b[1].append(c)
                    break
            else:
                bins.append([size[c], [c]])
        return [sorted(b[1]) for b in bins]

    def split(d: str) -> list[Unit]:
        if size[d] < threshold:
            return [Unit(d, list(base), float(size[d]), "dir")]
        big_dirs = sorted(c for c in children[d] if size[c] >= threshold)
        small_dirs = [c for c in children[d] if size[c] < threshold]
        big_files = bigfiles[d]
        subs: list[Unit] = []
        rest_ex = list(base)
        carved = 0
        for c in big_dirs:
            subs += split(c)
            rest_ex.append(escape_pattern(c))
            carved += size[c]
        for fp, fs in big_files:
            subs.append(Unit(fp, list(base), float(fs), "file"))
            rest_ex.append(escape_pattern(fp))
            carved += fs
        # If the small children alone would still make one huge "rest" unit,
        # pack them into groups of at most T (one multi-path snapshot each).
        if sum(size[c] for c in small_dirs) >= threshold:
            for grp in pack(small_dirs):
                subs.append(Unit(grp[0], list(base), float(sum(size[c] for c in grp)),
                                 "group", paths=grp))
                for c in grp:
                    rest_ex.append(escape_pattern(c))
                    carved += size[c]
        if not subs:  # big only because of a pile of small loose files
            return [Unit(d, list(base), float(size[d]), "dir")]
        subs.sort(key=lambda u: u.size)
        # "rest" first: quick snapshot of the small stuff, then smallest-to-largest.
        return [Unit(d, rest_ex, float(max(size[d] - carved, 0)), "rest")] + subs

    units = split(root)
    if log:
        over = [u for u in units if u.size >= threshold and u.kind in ("dir", "group")]
        log(f"Split plan for {root}: {len(units)} unit(s)"
            + (f"; {len(over)} unsplittable unit(s) still >= threshold" if over else ""))
    return units


@dataclass
class Target:
    """What the backup loop runs: duck-types restic_config.Directory plus `paths`."""
    path: str
    excludes: list[str]
    cached_size: float = 0.0
    paths: Optional[list[str]] = None
    origin: object = None  # the configured Directory this unit came from


def expand(directory, threshold_gib: int, log: LogFn = None) -> list:
    """Configured Directory -> list of things to back up, in order.

    Unsplit sources come back as the original object, so behaviour (and
    cached_size bookkeeping) is identical to before for anything under T.
    """
    try:
        if threshold_gib <= 0:
            plan = [Unit(directory.path, list(directory.excludes), 0.0, "dir")]
        else:
            plan = build_plan(directory.path, directory.excludes, threshold_gib * GIB, log)
    except Exception as exc:  # never let sizing break a backup
        if log:
            log(f"Split planning failed for {directory.path} ({exc}); backing up whole.")
        return [directory]
    if len(plan) == 1 and plan[0].kind == "dir" and not plan[0].paths:
        return [directory]
    return [Target(u.path, u.excludes, u.size, u.paths or None, directory) for u in plan]
