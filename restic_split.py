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
  * If the leftovers of that directory (small children and small loose files
    together) still weigh >= T, they are packed into "group" units of at most T
    bytes each — one multi-path snapshot per group. This is what makes a flat
    pile of medium-sized files (a photo archive, a media library) splittable
    instead of one unfinishable unit.
  * Everything left over (including anything created later) goes into one "rest"
    unit: the directory itself with the carved children/files excluded. Coverage
    is guaranteed by restic's own excludes, not by the scan, so a stale plan can
    never drop files — and a directory whose file list is dropped (too many
    entries) simply leaves its files in the rest unit.

Sizes are allocated bytes (st_blocks * 512, like `du`), so sparse images count
for what they actually hold.

Across runs the plan is saved (CONFIG_DIR/split-plans) and reused: restic only
finds a parent snapshot on an exact path-set match, so units keep their path
sets and excludes unless they grow past drift_factor * T (hysteresis in
plan_stable()); new files and dirs are always picked up by their "rest" unit.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from restic_config import CONFIG_DIR

GIB = 1024 ** 3

MAX_LISTED_FILES = 50_000  # per directory; above this the small-file list is dropped
MAX_UNIT_PATHS = 400       # keeps a group unit's argv manageable
PLAN_VERSION = 1           # on-change bump invalidates all saved split plans

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
    """Iterative post-order walk. Returns (size, children, bigfiles, smallfiles)
    dicts keyed by directory path. Never follows symlinks. Always skips `.zfs`
    (touching it auto-mounts snapshots).

    bigfiles[d]  — loose files in d at or above threshold (each becomes its own unit)
    smallfiles[d] — the rest of d's loose files, or None if there were too many to
                    list (those files are then left to the "rest" unit)
    """
    match = _matcher(excludes)
    size: dict[str, int] = {}
    children: dict[str, list[str]] = {}
    bigfiles: dict[str, list[tuple[str, int]]] = {}
    smallfiles: dict[str, Optional[list[tuple[str, int]]]] = {}
    seen_inodes: set[tuple[int, int]] = set()
    n_entries = 0
    last_log = time.monotonic()

    # frame: [path, entries, index, bytes, child_dirs, big_files, small_files]
    frames = [[root, _listdir(root), 0, 0, [], [], []]]
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
        n_entries += 1
        if log and n_entries % 5000 == 0 and time.monotonic() - last_log > 10:
            last_log = time.monotonic()
            log(f"Scanning {root}: {n_entries} entries, "
                f"{(f[3] + sum(fr[3] for fr in frames[:-1])) / GIB:.0f} GiB so far")
        if e.name == ".zfs" or match(e.path, e.name):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                frames.append([e.path, _listdir(e.path), 0, 0, [], [], []])
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
            if len(f[6]) > MAX_LISTED_FILES:
                f[6] = None  # too many to plan individually; the rest unit covers them
    return size, children, bigfiles, smallfiles


def plan_file_path(root: str, threshold_bytes: int, base_excludes: list[str],
                   plan_dir: Path | None = None) -> Path:
    """Stable identity of a plan: (root, threshold, base excludes). Any of those
    changes -> different file -> the saved plan is simply not found."""
    key = hashlib.sha1(
        "\0".join([os.path.normpath(root), str(int(threshold_bytes)),
                   *sorted(base_excludes)]).encode("utf-8", "surrogateescape")
    ).hexdigest()
    return (Path(plan_dir) if plan_dir else CONFIG_DIR / "split-plans") / (key + ".json")


def load_plan(root: str, base_excludes: list[str], threshold_bytes: int,
              plan_dir: Path | None = None) -> Optional[list[Unit]]:
    """Saved plan, or None if missing / corrupt / wrong version."""
    try:
        with open(plan_file_path(root, threshold_bytes, base_excludes, plan_dir)) as f:
            data = json.load(f)
        if data.get("version") != PLAN_VERSION:
            return None
        return [Unit(u["path"], list(u["excludes"]), float(u["size"]), u["kind"],
                     list(u.get("paths") or []))
                for u in data["units"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None  # missing, unparseable JSON, or malformed units -> fresh plan


def save_plan(root: str, base_excludes: list[str], threshold_bytes: int,
              units: list[Unit], plan_dir: Path | None = None) -> None:
    """Atomic write (tmp + os.replace). Best effort: never breaks a backup."""
    payload = {"version": PLAN_VERSION,
               "units": [{"kind": u.kind, "path": u.path, "paths": list(u.paths),
                          "excludes": list(u.excludes), "size": u.size}
                         for u in units]}
    p = plan_file_path(root, threshold_bytes, base_excludes, plan_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, p)
    except OSError:
        pass


def build_plan(root: str, excludes: list[str], threshold: int, log: LogFn = None) -> list[Unit]:
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return [Unit(root, list(excludes), 0.0, "dir")]
    size, children, bigfiles, smallfiles = scan(root, excludes, threshold, log)
    return plan_from_scan(root, excludes, threshold, size, children,
                          bigfiles, smallfiles, log)


def _pack(items: list[tuple[str, int]], threshold: int) -> list[list[str]]:
    """First-fit-decreasing into bins of at most *threshold* bytes and at most
    MAX_UNIT_PATHS paths each (keeps a group unit's argv manageable)."""
    bins: list[list] = []  # [used, [paths]]
    for path, sz in sorted(items, key=lambda it: (-it[1], it[0])):
        for b in bins:
            if b[0] + sz <= threshold and len(b[1]) < MAX_UNIT_PATHS:
                b[0] += sz
                b[1].append(path)
                break
        else:
            bins.append([sz, [path]])
    return [sorted(b[1]) for b in bins]


def _no_carve(_path: str) -> bool:
    return False


def _carve(d: str, base: list[str], threshold: int, size: dict, children: dict,
           bigfiles: dict, smallfiles: dict,
           already_carved: Callable[[str], bool] = _no_carve) -> tuple[list[Unit], list[str], int]:
    """Carve one directory's children and loose files into subunits.
    already_carved(p) skips entries a saved plan already carved out (their own
    units and excludes keep covering them). Returns (subunits, extra exclude
    patterns for d's rest unit, carved bytes)."""
    big_dirs = sorted(c for c in children.get(d, [])
                      if size[c] >= threshold and not already_carved(c))
    small_dirs = [c for c in children.get(d, [])
                  if size[c] < threshold and not already_carved(c)]
    subs: list[Unit] = []
    added_ex: list[str] = []
    carved = 0
    for c in big_dirs:
        subs += _split_dir(c, base, threshold, size, children, bigfiles, smallfiles)
        added_ex.append(escape_pattern(c))
        carved += size[c]
    for fp, fs in bigfiles.get(d, []):
        if already_carved(fp):
            continue
        subs.append(Unit(fp, list(base), float(fs), "file"))
        added_ex.append(escape_pattern(fp))
        carved += fs
    # If what's left would still make one huge "rest" unit, group it: small
    # child dirs and small loose files alike, binned to at most T each. A flat
    # flat directory of medium-sized files is splittable this way.
    leftovers = ([(c, size[c]) for c in small_dirs]
                 + [it for it in (smallfiles.get(d) or []) if not already_carved(it[0])])
    if sum(sz for _, sz in leftovers) >= threshold:
        sz_of = dict(leftovers)
        for grp in _pack(leftovers, threshold):
            subs.append(Unit(grp[0], list(base),
                             float(sum(sz_of[p] for p in grp)), "group", paths=grp))
            for c in grp:
                added_ex.append(escape_pattern(c))
                carved += sz_of[c]
    return subs, added_ex, carved


def _split_dir(d: str, base: list[str], threshold: int, size: dict, children: dict,
               bigfiles: dict, smallfiles: dict) -> list[Unit]:
    """Fresh plan for one directory: either a single unit, or rest + subs."""
    if size[d] < threshold:
        return [Unit(d, list(base), float(size[d]), "dir")]
    subs, added_ex, carved = _carve(d, base, threshold, size, children, bigfiles, smallfiles)
    if not subs:  # nothing carvable: leftovers too small to group, or too many
        return [Unit(d, list(base), float(size[d]), "dir")]
    subs.sort(key=lambda u: u.size)
    # "rest" first: quick snapshot of the small stuff, then smallest-to-largest.
    return [Unit(d, list(base) + added_ex, float(max(size[d] - carved, 0)), "rest")] + subs


def plan_from_scan(root: str, excludes: list[str], threshold: int,
                   size: dict, children: dict, bigfiles: dict,
                   smallfiles: Optional[dict] = None, log: LogFn = None) -> list[Unit]:
    """Pure planning step (no filesystem access), split out so it can be tested."""
    smallfiles = smallfiles if smallfiles is not None else {}
    units = _split_dir(root, list(excludes), threshold, size, children, bigfiles, smallfiles)
    if log:
        over = [u for u in units if u.size >= threshold and u.kind in ("dir", "group")]
        log(f"Split plan for {root}: {len(units)} unit(s)"
            + (f"; {len(over)} unsplittable unit(s) still >= threshold" if over else ""))
    return units


def plan_stable(prev_units: list[Unit], root: str, excludes: list[str], threshold: int,
                drift_factor: float, size: dict, children: dict, bigfiles: dict,
                smallfiles: Optional[dict] = None,
                exists: Callable[[str], bool] = os.path.lexists,
                log: LogFn = None) -> list[Unit]:
    """Re-plan reusing the saved plan (hysteresis): restic only finds a parent
    snapshot when host + path set match exactly, so untouched units must keep
    their exact path set and excludes. Only units that drifted past
    drift_factor * threshold are repacked/resplit/carved; coverage stays
    guaranteed by every split directory's "rest" unit, which automatically
    covers anything new. Pure: only reads the dicts from scan() and exists()."""
    base = list(excludes)
    smallfiles = smallfiles if smallfiles is not None else {}
    drift = drift_factor * threshold
    file_sizes: dict[str, int] = {}
    for items in bigfiles.values():
        for p, sz in items:
            file_sizes[p] = sz
    for items in smallfiles.values():
        if items:
            for p, sz in items:
                file_sizes[p] = sz

    rest_out: list[Unit] = []      # stored order
    other_out: list[Unit] = []    # stored order, stable-sorted by size at the end
    reused = repacked = resplit = carved = dropped = 0

    def member_size(p: str) -> int:
        return size.get(p, file_sizes.get(p, 0))

    def emit(units: list[Unit]) -> None:
        for x in units:
            (rest_out if x.kind == "rest" else other_out).append(x)

    for u0 in prev_units:
        u = Unit(u0.path, list(u0.excludes), u0.size, u0.kind, list(u0.paths))  # never mutate the caller's objects
        if u.kind == "dir":
            if u.path not in size:  # gone (or now skipped by the scan)
                dropped += 1
                continue
            if size[u.path] >= drift:
                emit(_split_dir(u.path, base, threshold, size, children,
                                bigfiles, smallfiles))
                resplit += 1
            else:
                u.size = float(size[u.path])
                other_out.append(u)
                reused += 1
        elif u.kind == "file":
            if not exists(u.path):
                dropped += 1
                continue
            if u.path in file_sizes:
                u.size = float(file_sizes[u.path])
            other_out.append(u)
            reused += 1
        elif u.kind == "group":
            members = [p for p in u.paths if exists(p)]
            if not members:
                dropped += 1
                continue
            pulled = [p for p in members if member_size(p) >= threshold]
            if pulled:  # a member grew into its own territory: plan it standalone
                for p in pulled:
                    if p in size:
                        emit(_split_dir(p, base, threshold, size, children,
                                        bigfiles, smallfiles))
                    else:
                        emit([Unit(p, base, float(file_sizes[p]), "file")])
                resplit += 1
                members = [p for p in members if p not in pulled]
                if not members:
                    continue
            total = sum(member_size(p) for p in members)
            if total >= drift:  # repack only this group's members
                bins = _pack([(p, member_size(p)) for p in members], threshold)
                for grp in bins:
                    emit([Unit(grp[0], list(u.excludes),
                               float(sum(member_size(p) for p in grp)),
                               "group", paths=grp)])
                repacked += 1
            else:
                u.paths = members
                u.size = float(total)
                other_out.append(u)
                reused += 1
        elif u.kind == "rest":
            d = u.path
            if d not in size:
                dropped += 1
                continue
            # Which scan entries are already carved out of this rest unit, and
            # how much unexplained (new or grown) weight piled up under it.
            match = _matcher(u.excludes)
            covered_size = 0
            leftover_size = 0
            for c in children.get(d, []):
                if match(c, os.path.basename(c)):
                    covered_size += size.get(c, 0)
                else:
                    leftover_size += size.get(c, 0)
            for fp, fs in bigfiles.get(d, []):
                if match(fp, os.path.basename(fp)):
                    covered_size += fs
                else:
                    leftover_size += fs
            for fp, fs in (smallfiles.get(d) or []):
                if match(fp, os.path.basename(fp)):
                    covered_size += fs
                else:
                    leftover_size += fs
            if leftover_size >= drift:
                subs, added, extra_carved = _carve(
                    d, base, threshold, size, children, bigfiles, smallfiles,
                    already_carved=lambda p: match(p, os.path.basename(p)))
                if subs:
                    u.excludes.extend(added)
                    emit(subs)
                    covered_size += extra_carved
                    carved += 1
            u.size = float(max(size.get(d, 0) - covered_size, 0))
            rest_out.append(u)
            reused += 1
        # unknown kinds (hand-edited plan file): dropped silently

    other_out.sort(key=lambda u: u.size)  # stable: equal sizes keep their order
    plan = rest_out + other_out
    if log:
        log(f"Split plan for {root}: reused {reused}, repacked {repacked}, "
            f"resplit {resplit}, carved {carved}, dropped {dropped} "
            f"-> {len(plan)} units")
    return plan


@dataclass
class Target:
    """What the backup loop runs: duck-types restic_config.Directory plus `paths`."""
    path: str
    excludes: list[str]
    cached_size: float = 0.0
    paths: Optional[list[str]] = None
    origin: object = None  # the configured Directory this unit came from


def expand(directory, threshold_gib: int, log: LogFn = None,
           drift_factor: float = 2.0) -> list:
    """Configured Directory -> list of things to back up, in order.

    Unsplit sources come back as the original object, so behaviour (and
    cached_size bookkeeping) is identical to before for anything under T.
    The plan for (root, threshold, base excludes) is persisted across runs and
    reused with hysteresis, so unit path sets (and hence restic parents) stay
    stable day to day.
    """
    try:
        if threshold_gib <= 0:
            plan = [Unit(directory.path, list(directory.excludes), 0.0, "dir")]
        else:
            root = os.path.normpath(directory.path)
            threshold = threshold_gib * GIB
            prev = load_plan(root, directory.excludes, threshold)
            if prev is not None and os.path.isdir(root):
                size, children, bigfiles, smallfiles = scan(
                    root, directory.excludes, threshold, log)
                plan = plan_stable(prev, root, list(directory.excludes), threshold,
                                   drift_factor, size, children, bigfiles, smallfiles,
                                   log=log)
                if not plan:  # everything in the saved plan is gone
                    plan = plan_from_scan(root, list(directory.excludes), threshold,
                                          size, children, bigfiles, smallfiles, log)
            else:  # no saved plan (or unreadable root): fresh plan, old behavior
                plan = build_plan(root, directory.excludes, threshold, log)
            save_plan(root, directory.excludes, threshold, plan)
    except Exception as exc:  # never let sizing break a backup
        if log:
            log(f"Split planning failed for {directory.path} ({exc}); backing up whole.")
        return [directory]
    if len(plan) == 1 and plan[0].kind == "dir" and not plan[0].paths:
        # Unsplit source: carry the fresh scan size through so progress has an
        # accurate estimate even on the first run (cached_size was stale/zero).
        if plan[0].size:
            directory.cached_size = float(plan[0].size)
        return [directory]
    return [Target(u.path, u.excludes, u.size, u.paths or None, directory) for u in plan]
