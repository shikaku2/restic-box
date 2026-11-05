"""Build the friendly mount views (`latest/`, `bydate/`) from restic snapshots.

Both views are stitched out of the read-only `ids/` FUSE tree, and split units
nest: a "rest" unit covers `/src` while a carved unit covers `/src/big`. So one
backup-target path is often an *ancestor* of another. Symlinking the ancestor
would then require creating the deeper symlink *inside* the read-only mount,
which the kernel refuses (EROFS/EPERM) — and that took the whole view build down
with it, leaving `latest/` half-built and `bydate/` never built at all.

So overlapping prefixes become real local directories instead of symlinks, topped
up with the ancestor unit's own entries along the corridor down to each nested
unit. Nothing is ever created inside the mount.

`ls_children` is injected (snapshot short id, absolute path) -> [(name, is_dir)]
so the builders can be tested without a FUSE mount or a repository.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

LogFn = Optional[Callable[[str], None]]
LsChildren = Callable[[str, str], list]  # (snap_id8, absolute path) -> [(name, is_dir)]


def set_tree_readonly(path: Path, readonly: bool) -> None:
    """Recursively chmod directories under path to 0o555 (readonly) or 0o755."""
    if not path.exists():
        return
    mode = 0o555 if readonly else 0o755
    for root, dirs, _ in os.walk(str(path)):
        os.chmod(root, mode)


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
    """Directories the ancestor unit must reach through to hand off to nested units."""
    nodes = {unit_path}
    head = len([c for c in unit_path.split("/") if c])
    for other in covered:
        if not _own_subtree(unit_path, other):
            continue
        parts = [c for c in other.split("/") if c]
        for i in range(head + 1, len(parts)):
            nodes.add("/" + "/".join(parts[:i]))
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

    for unit_path in sorted(p for p in covered if p in ancestors):
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
        except Exception as exc:  # a bad listing must not kill the whole view
            if log:
                log(f"[mount] could not list {node} of snapshot {snap_id}: {exc}")
            continue
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
    """latest/ = every backup target path, taken from that path's newest snapshot."""
    set_tree_readonly(latest_path, False)
    shutil.rmtree(latest_path, ignore_errors=True)
    latest_path.mkdir(parents=True, exist_ok=True)
    created = place_targets(latest_path, Path(raw_mount),
                            covered_from_snapshots(snaps), ls_children, log)
    set_tree_readonly(latest_path, True)
    return created


def build_bydate(bydate_path: Path, raw_mount: Path, snaps: list[dict],
                 ls_children: LsChildren = None, log: LogFn = None) -> int:
    """bydate/<date>/[<HH-MM>/] paths, one folder per run (units of one run merged)."""
    set_tree_readonly(bydate_path, False)
    shutil.rmtree(bydate_path, ignore_errors=True)
    bydate_path.mkdir(parents=True, exist_ok=True)

    minute_groups: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for snap in snaps:
        t = snap.get("time", "")
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone()
            date, time_key = dt.strftime("%Y-%m-%d"), dt.strftime("%H-%M")
        except Exception:
            date, time_key = t[:10], t[11:16].replace(":", "-")
        sid = str(snap.get("id") or snap.get("short_id") or "")[:8]
        if not sid:
            continue
        entry = minute_groups.setdefault(date, {}).setdefault(time_key, [])
        for path in snap.get("paths") or []:
            entry.append((norm(path), sid))

    created = 0
    for date in sorted(minute_groups):
        runs = minute_groups[date]
        multi = len(runs) > 1
        used_times: dict[str, int] = {}
        for time_key in sorted(runs):
            if multi:
                # Handle same-minute collisions with _2, _3, … suffix
                if time_key in used_times:
                    used_times[time_key] += 1
                    subfolder = f"{time_key}_{used_times[time_key]}"
                else:
                    used_times[time_key] = 1
                    subfolder = time_key
                base_dir = bydate_path / date / subfolder
            else:
                base_dir = bydate_path / date
            base_dir.mkdir(parents=True, exist_ok=True)
            covered = covered_from_snapshots(
                [{"id": sid, "paths": [path]} for path, sid in runs[time_key]],
                newest_wins=False)
            created += place_targets(base_dir, Path(raw_mount), covered, ls_children, log)

    set_tree_readonly(bydate_path, True)
    return created
