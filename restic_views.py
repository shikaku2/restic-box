"""Build the friendly mount views (`latest/`, `bydate/`) from restic snapshots.

Both views are stitched out of the read-only `ids/` FUSE tree, and split units
nest: a "rest" unit covers `/src` while a carved unit covers `/src/big`. So one
backup-target path is often an *ancestor* of another. Symlinking the ancestor
would then require creating the deeper symlink *inside* the read-only mount,
which the kernel refuses (EROFS/EPERM) — and that took the whole view build down
with it, leaving `latest/` half-built and `bydate/` never built at all.

So overlapping prefixes become real local directories instead of symlinks, topped
up with the ancestor unit's own entries along the corridor down to each nested
unit. Nothing is ever created inside the mount. A corridor walk stops at any
intermediate path another unit owns, so the most specific unit always supplies
the entries in its own subtree. A covered `/` target becomes the (real) view
root itself, populated from its snapshot.

bydate/ groups folders by *backup run*, not wall-clock minute: every unit of an
operation shares a `restic-box-run=<id>` restic tag, and snapshots carrying the
same tag land in one folder whatever time they finalize at. Snapshots predating
run tags fall back to calendar-minute grouping.

`ls_children` is injected (snapshot short id, absolute path) -> [(name, is_dir)]
so the builders can be tested without a FUSE mount or a repository. A corridor
listing that *fails* aborts the build: unknown contents must never be published
as an empty directory.

Every rebuild is staged in `<view>.building/` and only renamed into place once it
is complete, so a listing that fails partway leaves the previously working view
untouched (review fix #6, restic-box-fixes2.md).
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

LogFn = Optional[Callable[[str], None]]
LsChildren = Callable[[str, str], list]  # (snap_id8, absolute path) -> [(name, is_dir)]

# Every unit snapshot of one backup operation carries this tag plus a per-run
# id, giving bydate/ true run identity (issue #8).
RUN_TAG_PREFIX = "restic-box-run="


def set_tree_readonly(path: Path, readonly: bool) -> None:
    """Recursively chmod directories under path to 0o555 (readonly) or 0o755."""
    if not path.exists():
        return
    mode = 0o555 if readonly else 0o755
    for root, dirs, _ in os.walk(str(path)):
        os.chmod(root, mode)


def staging_path(dest: Path) -> Path:
    """Where a replacement for *dest* is built before it is published (fix #6)."""
    return dest.parent / (dest.name + ".building")


def backup_path(dest: Path) -> Path:
    """Where the outgoing view is parked for the instant a swap takes place."""
    return dest.parent / (dest.name + ".old")


def leftover_paths(dest: Path) -> list[Path]:
    """Scratch trees a published view can leave behind (e.g. after a crash)."""
    return [staging_path(dest), backup_path(dest)]


def rebuild_view(dest: Path, fill) -> int:
    """Build a complete replacement for *dest*, then swap it in (review fix #6).

    `fill(staging)` populates a staging tree and returns its link count. The
    view the user is looking at is only removed once that replacement is
    finished: a failed, truncated, or interrupted rebuild discards the staging
    tree and leaves the previous view exactly as it was. The swap itself is two
    renames, and the old view is put back if the second one fails.

    Safe because everything a view contains is a directory or an *absolute*
    symlink into the FUSE mount — moving the tree elsewhere changes no target.
    """
    staging = staging_path(dest)
    stale_backup = backup_path(dest)
    # Clear scratch first: an earlier build or swap that a crash interrupted
    # leaves 0555 trees behind, and chmod must precede the removal.
    for scratch in (staging, stale_backup):
        set_tree_readonly(scratch, False)
        shutil.rmtree(scratch, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        created = fill(staging)
    except BaseException:
        set_tree_readonly(staging, False)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    set_tree_readonly(staging, True)       # published the same way as the old view

    backup: Path | None = None
    try:
        if dest.exists() or dest.is_symlink():
            set_tree_readonly(dest, False)
            backup = stale_backup
            os.replace(str(dest), str(backup))       # old view parked, out of the way
        os.replace(str(staging), str(dest))
    except OSError:
        if backup is not None and not dest.exists():
            try:
                os.replace(str(backup), str(dest))   # put the user's view back
            except OSError:
                pass
        # Discard the replacement that could not be published. The next rebuild
        # would clear it anyway, but a stray tree in the mount folder is untidy.
        set_tree_readonly(staging, False)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if backup is not None:
            set_tree_readonly(backup, False)
            shutil.rmtree(backup, ignore_errors=True)
    return created


def norm(path: str) -> str:
    """Canonical absolute tree path: no trailing slash, no '.' segments."""
    parts = [c for c in (path or "").split("/") if c and c != "."]
    return "/" + "/".join(parts)


def covered_from_snapshots(snaps: list[dict], newest_wins: bool = True) -> dict[str, str]:
    """Backup-target path -> snapshot short id (8 chars, as the FUSE ids/ view uses).

    newest_wins=True gives each path its newest snapshot; False gives its first
    (used by bydate/, where several runs may claim the same path in one folder).
    """
    covered: dict[str, str] = {}
    for snap in sorted(snaps, key=lambda s: s.get("time", "")):
        sid = str(snap.get("id") or snap.get("short_id") or "")[:8]
        if not sid:
            continue
        for path in snap.get("paths") or []:
            key = norm(path)
            if newest_wins or key not in covered:
                covered[key] = sid
    return covered


def _ancestor_prefixes(covered: dict[str, str]) -> set[str]:
    """Every proper ancestor of every covered path."""
    out: set[str] = set()
    for path in covered:
        parts = [c for c in path.split("/") if c]
        for i in range(1, len(parts)):
            out.add("/" + "/".join(parts[:i]))
    return out


def _own_subtree(unit_path: str, other: str) -> bool:
    """True if `other` is inside `unit_path` (both normalized)."""
    if unit_path == "/":
        return other != "/"
    return other.startswith(unit_path + "/")


def _corridor_nodes(unit_path: str, covered: dict[str, str]) -> list[str]:
    """Directories the ancestor unit must reach through to hand off to nested units.

    The walk stops at any intermediate path that is itself a covered target
    (issue #9): the deeper unit owns its own subtree and fills its own
    corridors, so an outer unit's snapshot can never pre-empt the inner unit's
    entries along the way.
    """
    nodes = {unit_path}
    head = len([c for c in unit_path.split("/") if c])
    for other in covered:
        if not _own_subtree(unit_path, other):
            continue
        parts = [c for c in other.split("/") if c]
        for i in range(head + 1, len(parts)):
            node = "/" + "/".join(parts[:i])
            if node in covered:
                break  # a more specific unit owns this subtree — and everything deeper
            nodes.add(node)
    return sorted(nodes, key=lambda n: (n.count("/"), n))


def place_targets(root: Path, raw_mount: Path, covered: dict[str, str],
                  ls_children: LsChildren = None, log: LogFn = None) -> int:
    """Mirror backup-target paths under `root` as symlinks into `raw_mount/ids/`.

    Returns the number of symlinks created. Deepest-first so corridor directories
    exist as real local dirs before any ancestor tries to place a link there.
    """
    ancestors = _ancestor_prefixes(covered)
    created = 0

    for path in sorted(covered, key=lambda p: (-p.count("/"), p)):
        if path == "/":
            # The view root cannot be a symlink into the mount; it stays a real
            # directory and is populated from its snapshot below (issue #7).
            continue
        rel = path.lstrip("/")
        dst = root / rel
        # Parents are always real local dirs: a component that is a covered path
        # and also an ancestor of another covered path is never symlinked.
        dst.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(dst):  # already a corridor directory, or placed already
            continue
        dst.symlink_to(raw_mount / "ids" / covered[path] / rel)
        created += 1

    for unit_path in sorted(p for p in covered if p in ancestors or p == "/"):
        created += _fill_corridor(root, raw_mount, unit_path, covered[unit_path],
                                  covered, ls_children, log)
    return created


def _fill_corridor(root: Path, raw_mount: Path, unit_path: str, snap_id: str,
                   covered: dict[str, str], ls_children: LsChildren,
                   log: LogFn) -> int:
    """Give an ancestor unit a place to live: real dirs along the corridor, each
    holding that unit's own entries except where a nested unit already lives."""
    created = 0
    for node in _corridor_nodes(unit_path, covered):
        local = root / node.lstrip("/")
        local.mkdir(parents=True, exist_ok=True)
        if ls_children is None:
            continue
        try:
            children = ls_children(snap_id, node)
        except Exception as exc:
            # The contents of this corridor directory are UNKNOWN. Publishing
            # the half-built view would show it as an empty one, so the whole
            # build aborts and the previous view survives (review fix #6).
            if log:
                log(f"[mount] could not list {node} of snapshot {snap_id}: {exc} "
                    f"— view rebuild aborted, previous view kept")
            raise
        for name, _is_dir in children:
            child_path = (node.rstrip("/") if node != "/" else "") + "/" + name
            child_local = local / name
            # Another unit owns that path, or the corridor dir is already there.
            if child_path in covered or os.path.lexists(child_local):
                continue
            child_local.symlink_to(
                raw_mount / "ids" / snap_id / child_path.lstrip("/"))
            created += 1
    return created


def build_latest(latest_path: Path, raw_mount: Path, snaps: list[dict],
                 ls_children: LsChildren = None, log: LogFn = None) -> int:
    """latest/ = every backup target path, taken from that path's newest snapshot.

    The replacement is built complete in `latest.building/` and swapped in only
    afterwards: a build that fails partway leaves the existing latest/ intact
    (review fix #6).
    """
    covered = covered_from_snapshots(snaps)

    def fill(staging: Path) -> int:
        return place_targets(staging, Path(raw_mount), covered, ls_children, log)

    return rebuild_view(latest_path, fill)


def _parse_local_time(t: str) -> tuple[str, str]:
    """(date, HH-MM) folder keys for a snapshot time, in local time."""
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H-%M")
    except Exception:
        return t[:10], t[11:16].replace(":", "-")


def _run_tag(snap: dict) -> str | None:
    """The restic-box run tag this snapshot carries, if any (issue #8)."""
    for tag in snap.get("tags") or []:
        if isinstance(tag, str) and tag.startswith(RUN_TAG_PREFIX):
            return tag
    return None


def _paths_of(snaps: list[dict]) -> list[tuple[str, str]]:
    """[(normalized path, sid), …] for snapshots ordered by finalize time."""
    entry: list[tuple[str, str]] = []
    for snap in snaps:
        sid = str(snap.get("id") or snap.get("short_id") or "")[:8]
        if not sid:
            continue
        for path in snap.get("paths") or []:
            entry.append((norm(path), sid))
    return entry


def build_bydate(bydate_path: Path, raw_mount: Path, snaps: list[dict],
                 ls_children: LsChildren = None, log: LogFn = None) -> int:
    """bydate/<date>/[<HH-MM>/] paths, one folder per backup run.

    Run identity comes from the restic-box-run=<id> tag every unit of one
    backup operation shares (issue #8): a split backup lasting hours lands in
    ONE folder, and two distinct backups finalizing within the same minute stay
    separate folders. Snapshots from before run tags existed fall back to the
    old calendar-minute grouping.

    Like latest/, the whole tree is built in `bydate.building/` and swapped in
    only when complete, so a failed rebuild cannot blank the existing view
    (review fix #6).
    """
    # Collect runs: tagged snapshots group by tag whatever time they finalize
    # at; untagged ones group by (date, minute).
    tagged: dict[str, list[dict]] = {}
    untagged: dict[tuple[str, str], list[dict]] = {}
    for snap in snaps:
        sid = str(snap.get("id") or snap.get("short_id") or "")[:8]
        if not sid:
            continue
        tag = _run_tag(snap)
        if tag is not None:
            tagged.setdefault(tag, []).append(snap)
        else:
            untagged.setdefault(_parse_local_time(snap.get("time", "")), []).append(snap)

    # One entry per run: (date, time_key, [(path, sid), …]). A tagged run is
    # filed under the date/minute of its earliest finalized snapshot.
    runs: list[tuple[str, str, list[tuple[str, str]]]] = []
    for run_snaps in tagged.values():
        ordered = sorted(run_snaps, key=lambda s: s.get("time", ""))
        date, time_key = _parse_local_time(ordered[0].get("time", ""))
        runs.append((date, time_key, _paths_of(ordered)))
    for (date, time_key), run_snaps in untagged.items():
        ordered = sorted(run_snaps, key=lambda s: s.get("time", ""))
        runs.append((date, time_key, _paths_of(ordered)))

    by_date: dict[str, list[tuple[str, str, list[tuple[str, str]]]]] = {}
    for run in runs:
        by_date.setdefault(run[0], []).append(run)

    def fill(staging: Path) -> int:
        created = 0
        for date in sorted(by_date):
            day_runs = sorted(by_date[date], key=lambda r: r[1])
            multi = len(day_runs) > 1
            used_times: dict[str, int] = {}
            for _, time_key, entry in day_runs:
                if multi:
                    # Two different runs in the same minute get _2, _3, … suffixes
                    # (reachable now that grouping is by run, not by unique minute).
                    used_times[time_key] = used_times.get(time_key, 0) + 1
                    subfolder = (time_key if used_times[time_key] == 1
                                 else f"{time_key}_{used_times[time_key]}")
                    base_dir = staging / date / subfolder
                else:
                    base_dir = staging / date
                base_dir.mkdir(parents=True, exist_ok=True)
                covered = covered_from_snapshots(
                    [{"id": sid, "paths": [path]} for path, sid in entry],
                    newest_wins=False)
                created += place_targets(base_dir, Path(raw_mount), covered,
                                         ls_children, log)
        return created

    return rebuild_view(bydate_path, fill)
