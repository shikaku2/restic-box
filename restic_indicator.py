"""restic-box — GTK3 AppIndicator tray applet for restic backups over SSH."""

from __future__ import annotations

import copy
import dataclasses
import queue
import shutil
import time
import math
import os
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from datetime import date, datetime
from enum import Enum, auto
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import AppIndicator3, GLib, Gio, Gtk  # type: ignore[attr-defined]

import restic_config as cfg_mod
import restic_diag_log as diag_log
import restic_runner as runner
import restic_split as split
import restic_views as views
from restic_settings import SettingsDialog
from restic_status import LogWindow
from restic_views import set_tree_readonly as _set_tree_readonly


def _inhibit_sleep() -> int | None:
    """Acquire a systemd sleep inhibitor. Returns the held fd, or None on failure."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        result, fd_list = bus.call_with_unix_fd_list_sync(
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "Inhibit",
            GLib.Variant("(ssss)", ("sleep", "restic-box", "Backup in progress", "block")),
            GLib.VariantType.new("(h)"),
            Gio.DBusCallFlags.NONE,
            -1, None, None,
        )
        return fd_list.get(result.get_child_value(0).get_handle())
    except Exception as e:
        print(f"[inhibit] could not acquire sleep inhibitor: {e}", flush=True)
        return None


def _release_inhibit(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Icon rendering
# ---------------------------------------------------------------------------
_MOUNT_DIR      = Path.home() / ".cache" / "restic" / "mount"
_RAW_MOUNT_PATH = _MOUNT_DIR / "snapshots"   # actual restic FUSE mount
_LATEST_PATH    = _MOUNT_DIR / "latest"
_BYDATE_PATH    = _MOUNT_DIR / "bydate"
_MOUNT_FRAMES   = ("Mounting ·", "Mounting ··", "Mounting ···")
_ICON_DIR = tempfile.mkdtemp(prefix="restic-box-")
_ICON_SLOT = ("a", "b")

ICON_SIZE = 64


class BackupState(Enum):
    IDLE = auto()
    RUNNING = auto()
    SUCCESS = auto()      # every source fully readable
    PARTIAL = auto()      # backup created, but some source files were unreadable (restic rc=3)
    ERROR = auto()
    CHECKING = auto()


#: What the status line calls each scheduled operation whose outcome it reports.
#: Fix #9 (restic-box-fixes2.md): with skipped and stopped attempts on the line too,
#: the line has to name the operation it is talking about.
_OP_LABELS = {"backup": "Backup", "check": "Check"}

#: Operation names as they travel through the worker, and the outcome record each
#: one belongs to ("full check" is the label the mount guard logs; the record key is
#: the same one _op_worker queues).
_OP_RECORD_KEYS = {"backup": "backup", "check": "check", "check_full": "check_full",
                   "full check": "check_full", "unlock": "unlock"}


_STATE_COLORS: dict[BackupState, tuple[float, float, float]] = {
    BackupState.IDLE: (0.50, 0.50, 0.50),
    BackupState.RUNNING: (0.20, 0.55, 1.00),
    BackupState.SUCCESS: (0.18, 0.78, 0.30),
    BackupState.PARTIAL: (0.95, 0.65, 0.10),
    BackupState.ERROR: (0.90, 0.20, 0.18),
    BackupState.CHECKING: (1.00, 0.62, 0.00),
}

# Fix #3 (restic-box-fixes2.md): automatic operations that fail the connection
# preflight, or that the user cancels, must not be re-launched by the next
# 60-second scheduler tick. Failed preflight backs off exponentially (60 s,
# 120 s, … up to 6 h) so an unreachable host is retried gently instead of
# hammered; a cancelled operation waits a fixed quarter-hour, long enough to
# outlive the tick, short enough not to miss a schedule by a whole day.
# Manual triggers from the menu are never gated by this.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 6 * 3600
BACKOFF_CANCEL_SECONDS = 15 * 60


def _render_icon(state: BackupState, name: str, progress: float = -1.0) -> None:
    """Draw the tray icon.

    progress: 0.0–1.0 to show a progress arc when RUNNING; -1 means no arc.
    """
    s = ICON_SIZE
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, s, s)
    ctx = cairo.Context(surface)
    cx = cy = s / 2.0
    r = s / 2.0 - 2.0

    cr, cg, cb = _STATE_COLORS[state]

    if state == BackupState.RUNNING and 0.0 <= progress <= 1.0:
        # Dark background circle
        ctx.arc(cx, cy, r, 0, 2 * math.pi)
        ctx.set_source_rgba(0.15, 0.15, 0.15, 1.0)
        ctx.fill()

        # Grey track ring
        track_r_outer = r
        track_r_inner = r * 0.62
        ctx.arc(cx, cy, track_r_outer, 0, 2 * math.pi)
        ctx.arc_negative(cx, cy, track_r_inner, 2 * math.pi, 0)
        ctx.set_source_rgba(0.30, 0.30, 0.30, 1.0)
        ctx.fill()

        # Coloured progress arc (clockwise from top)
        if progress > 0:
            start = -math.pi / 2
            end = start + progress * 2 * math.pi
            ctx.arc(cx, cy, track_r_outer, start, end)
            ctx.arc_negative(cx, cy, track_r_inner, end, start)
            ctx.close_path()
            ctx.set_source_rgba(cr, cg, cb, 1.0)
            ctx.fill()
    else:
        # Solid circle for non-progress states
        ctx.arc(cx, cy, r, 0, 2 * math.pi)
        ctx.set_source_rgba(cr, cg, cb, 1.0)
        ctx.fill()

    # Inner white symbol
    ctx.set_source_rgba(1, 1, 1, 0.92)
    ctx.set_line_width(4.0)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)

    if state in (BackupState.RUNNING, BackupState.IDLE):
        # Upload arrow
        ctx.move_to(cx, cy + r * 0.35)
        ctx.line_to(cx, cy - r * 0.30)
        ctx.stroke()
        ctx.move_to(cx - r * 0.28, cy - r * 0.05)
        ctx.line_to(cx, cy - r * 0.38)
        ctx.line_to(cx + r * 0.28, cy - r * 0.05)
        ctx.stroke()
    elif state == BackupState.SUCCESS:
        # Checkmark
        ctx.move_to(cx - r * 0.35, cy)
        ctx.line_to(cx - r * 0.08, cy + r * 0.30)
        ctx.line_to(cx + r * 0.38, cy - r * 0.28)
        ctx.stroke()
    elif state == BackupState.PARTIAL:
        # Exclamation mark
        ctx.move_to(cx, cy - r * 0.36)
        ctx.line_to(cx, cy + r * 0.12)
        ctx.stroke()
        ctx.set_source_rgba(1, 1, 1, 0.92)
        ctx.arc(cx, cy + r * 0.32, r * 0.075, 0, 2 * math.pi)
        ctx.fill()
    elif state == BackupState.ERROR:
        # X
        ctx.move_to(cx - r * 0.32, cy - r * 0.32)
        ctx.line_to(cx + r * 0.32, cy + r * 0.32)
        ctx.stroke()
        ctx.move_to(cx + r * 0.32, cy - r * 0.32)
        ctx.line_to(cx - r * 0.32, cy + r * 0.32)
        ctx.stroke()
    elif state == BackupState.CHECKING:
        # Magnifier
        ctx.arc(cx - r * 0.08, cy - r * 0.08, r * 0.28, 0, 2 * math.pi)
        ctx.stroke()
        ctx.move_to(cx + r * 0.14, cy + r * 0.14)
        ctx.line_to(cx + r * 0.38, cy + r * 0.38)
        ctx.stroke()

    path = os.path.join(_ICON_DIR, f"{name}.png")
    surface.write_to_png(path)


# ---------------------------------------------------------------------------
# Autostart
# ---------------------------------------------------------------------------
_AUTOSTART_DIR = Path.home() / ".config" / "autostart"
_AUTOSTART_FILE = _AUTOSTART_DIR / "restic-box.desktop"

def _exec_command() -> str:
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return appimage
    return f"{sys.executable} {Path(__file__).with_name('main.py').resolve()}"

_DESKTOP = f"""\
[Desktop Entry]
Type=Application
Name=restic-box
Comment=Restic backup tray applet
Exec={_exec_command()}
Terminal=false
StartupNotify=false
Categories=Utility;
"""


def _autostart_enabled() -> bool:
    return _AUTOSTART_FILE.exists()


def _set_autostart(enabled: bool) -> None:
    if enabled:
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        _AUTOSTART_FILE.write_text(_DESKTOP)
    else:
        _AUTOSTART_FILE.unlink(missing_ok=True)


def _notify(msg: str) -> None:
    subprocess.Popen(
        ["notify-send", "-a", "restic-box", "restic-box", msg],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Snapshot browser
# ---------------------------------------------------------------------------
class SnapshotBrowser(Gtk.Window):
    _DUMMY = "\x00"  # sentinel child for unexpanded directories

    def __init__(self, cfg: cfg_mod.Config, on_proc=None, busy=None,
                 acquire=None, release=None) -> None:
        super().__init__(title="restic-box — Snapshot Browser")
        self._cfg = cfg
        # forget/prune are restic processes too: they must be registered with
        # the indicator (on_proc) so "Stop All" can terminate them, and they
        # must not start while a backup/check is mid-flight (busy) — two restic
        # processes against one repo would fight over the lock and Stop could
        # only ever track one of them (issue #6).
        #
        # Fix #4 (restic-box-fixes2.md): busy is only a LOOK — the indicator's
        # acquire() is what actually claims the repository lease atomically,
        # so two destructive actions can never start side by side and no
        # window exists between "looked idle" and "process launched". release()
        # hands the repository back when the action ends (success, failure or
        # user cancel).
        self._on_proc = on_proc
        self._busy = busy
        self._acquire_lease = acquire
        self._release_lease = release
        self._nodes: dict[int, dict] = {}  # node_id → children dict
        self._nid = 0
        # Generation tokens (issue #12): each load request bumps its counter;
        # a response is only applied while its generation is still current,
        # so out-of-order listings can never populate the wrong selection.
        self._snap_gen = 0
        self._files_gen = 0
        self.set_default_size(960, 640)
        self.connect("delete-event", lambda w, e: w.hide() or True)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_position(220)
        self.add(paned)

        # ── top: snapshot list ──────────────────────────────────────────────
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # columns: full_id, short_id, time_str, paths_str, host
        self._snap_store = Gtk.ListStore(str, str, str, str, str)
        snap_tv = Gtk.TreeView(model=self._snap_store)
        for title, col, expand, min_w in [
            ("Date", 2, False, 160), ("Paths", 3, True, 0),
            ("Host", 4, False, 110), ("ID", 1, False, 80),
        ]:
            r = Gtk.CellRendererText()
            c = Gtk.TreeViewColumn(title, r, text=col)
            c.set_resizable(True)
            c.set_expand(expand)
            if min_w:
                c.set_min_width(min_w)
            snap_tv.append_column(c)
        scroll1 = Gtk.ScrolledWindow()
        scroll1.add(snap_tv)
        top.pack_start(scroll1, True, True, 0)

        bar1 = Gtk.Box(spacing=6)
        bar1.set_border_width(6)
        btn_refresh = Gtk.Button(label="Refresh")
        btn_refresh.connect("clicked", lambda _: self._load_snapshots())
        self._btn_forget = Gtk.Button(label="Forget + Prune…")
        self._btn_forget.set_sensitive(False)
        self._btn_forget.connect("clicked", self._on_forget)
        btn_prune = Gtk.Button(label="Prune All…")
        btn_prune.connect("clicked", self._on_prune_all)
        self._snap_lbl = Gtk.Label(label="")
        bar1.pack_start(btn_refresh, False, False, 0)
        bar1.pack_start(self._btn_forget, False, False, 0)
        bar1.pack_start(btn_prune, False, False, 0)
        bar1.pack_end(self._snap_lbl, False, False, 0)
        top.pack_start(bar1, False, False, 0)
        paned.pack1(top, resize=True, shrink=False)

        self._snap_sel = snap_tv.get_selection()
        self._snap_sel.connect("changed", self._on_snap_sel_changed)

        # ── bottom: file tree ───────────────────────────────────────────────
        bot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # columns: name, size_str, mtime, type, path, size_int, node_id
        self._file_store = Gtk.TreeStore(str, str, str, str, str, int, int)
        self._file_tv = Gtk.TreeView(model=self._file_store)
        for title, col, expand in [
            ("Name", 0, True), ("Size", 1, False), ("Modified", 2, False), ("Type", 3, False),
        ]:
            r = Gtk.CellRendererText()
            c = Gtk.TreeViewColumn(title, r, text=col)
            c.set_resizable(True)
            c.set_expand(expand)
            self._file_tv.append_column(c)
        self._file_tv.connect("row-expanded", self._on_row_expanded)
        scroll2 = Gtk.ScrolledWindow()
        scroll2.add(self._file_tv)
        bot.pack_start(scroll2, True, True, 0)

        self._file_lbl = Gtk.Label(label="Select a snapshot above to browse its files.")
        self._file_lbl.set_xalign(0)
        self._file_lbl.set_margin_start(6)
        self._file_lbl.set_margin_bottom(4)
        bot.pack_start(self._file_lbl, False, False, 0)
        paned.pack2(bot, resize=True, shrink=False)

        self._load_snapshots()

    # ── snapshot list ───────────────────────────────────────────────────────

    def _load_snapshots(self) -> None:
        self._snap_gen += 1
        self._snap_store.clear()
        self._snap_lbl.set_text("Loading…")
        threading.Thread(target=self._do_load_snaps, args=(self._snap_gen,),
                         daemon=True).start()

    def _do_load_snaps(self, gen: int) -> None:
        try:
            snaps = runner.run_snapshots_json(self._cfg)
            GLib.idle_add(self._fill_snapshots, snaps, gen)
        except Exception as e:
            traceback.print_exc()
            GLib.idle_add(self._load_snaps_error, e, gen)

    def _load_snaps_error(self, exc: Exception, gen: int) -> None:
        if gen != self._snap_gen:
            return
        self._snap_lbl.set_text(f"Error: {exc}")

    def _fill_snapshots(self, snaps: list[dict], gen: int) -> None:
        if gen != self._snap_gen:
            return  # a newer refresh superseded this response (issue #12)
        self._snap_store.clear()
        for s in reversed(snaps):
            t = s.get("time", "")
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                time_str = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                time_str = t[:19]
            self._snap_store.append([
                s.get("id", ""),
                s.get("short_id", s.get("id", ""))[:8],
                time_str,
                ", ".join(s.get("paths") or []),
                s.get("hostname", ""),
            ])
        self._snap_lbl.set_text(f"{len(snaps)} snapshot(s)")

    def _on_snap_sel_changed(self, sel: Gtk.TreeSelection) -> None:
        model, it = sel.get_selected()
        self._btn_forget.set_sensitive(it is not None)
        # Any selection change invalidates in-flight listings, whether or not
        # something is selected now (issue #12).
        self._files_gen += 1
        if not it:
            self._file_store.clear()
            self._nodes.clear()
            self._nid = 0
            self._file_lbl.set_text("Select a snapshot above to browse its files.")
            return
        snap_id = model.get_value(it, 0)
        self._file_store.clear()
        self._nodes.clear()
        self._nid = 0
        self._file_lbl.set_text("Loading files…")
        threading.Thread(target=self._do_load_files,
                         args=(snap_id, self._files_gen), daemon=True).start()

    # ── file tree ───────────────────────────────────────────────────────────

    def _do_load_files(self, snap_id: str, gen: int) -> None:
        try:
            entries = runner.run_ls_json(self._cfg, snap_id)
        except Exception as e:
            traceback.print_exc()
            GLib.idle_add(self._load_files_error, e, gen)
            return
        GLib.idle_add(self._fill_files, entries, gen)

    def _load_files_error(self, exc: Exception, gen: int) -> None:
        if gen != self._files_gen:
            return
        self._file_lbl.set_text(f"Error listing files: {exc}")

    def _fill_files(self, entries: list[dict], gen: int) -> None:
        if gen != self._files_gen:
            return  # selection changed while this listing ran (issue #12)
        self._file_store.clear()
        self._nodes.clear()
        self._nid = 0

        # Build in-memory tree: {name: {"e": entry|None, "ch": {children}}}
        root: dict = {}
        for e in entries:
            if e.get("struct_type") == "snapshot":
                continue
            parts = [p for p in e.get("path", "").split("/") if p]
            if not parts:
                continue
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {"e": None, "ch": {}})["ch"]
            name = parts[-1]
            if name in node:
                node[name]["e"] = e
            else:
                node[name] = {"e": e, "ch": {}}

        self._add_level(None, root)
        self._file_lbl.set_text(f"{len(entries)} entries")

    def _sort_key(self, name: str, node: dict) -> tuple:
        is_file = (node["e"] or {}).get("type", "dir") == "file"
        return (is_file, name.lower())

    def _next_nid(self) -> int:
        nid = self._nid
        self._nid += 1
        return nid

    def _add_level(self, parent_it: Gtk.TreeIter | None, children: dict) -> None:
        for name in sorted(children, key=lambda n: self._sort_key(n, children[n])):
            child = children[name]
            e = child["e"] or {}
            ftype = e.get("type", "dir")
            size = e.get("size") or 0
            mtime = (e.get("mtime") or "")[:16].replace("T", " ")
            nid = self._next_nid()
            it = self._file_store.append(parent_it, [
                name,
                runner.fmt_bytes(size) if ftype == "file" and size else "",
                mtime, ftype, e.get("path", ""), size, nid,
            ])
            if child["ch"]:
                self._nodes[nid] = child["ch"]
                self._file_store.append(it, [self._DUMMY, "", "", "", "", 0, -1])

    def _on_row_expanded(self, _tv: Gtk.TreeView, it: Gtk.TreeIter, _path: Gtk.TreePath) -> None:
        child_it = self._file_store.iter_children(it)
        if not child_it or self._file_store.get_value(child_it, 0) != self._DUMMY:
            return
        nid = self._file_store.get_value(it, 6)
        children = self._nodes.pop(nid, {})
        self._file_store.remove(child_it)
        self._add_level(it, children)

    # ── forget / prune ──────────────────────────────────────────────────────

    def _refuse_if_busy(self, op: str = "") -> bool:
        """Try to claim the repository for a destructive action.

        True (and explains why) when another operation — a queued/running
        backup/check/unlock or another browser Forget/Prune — already owns it.
        On False the caller HOLDS the lease and must release it when done."""
        if self._acquire_lease is not None:
            if self._acquire_lease(op):
                return False          # lease claimed: safe to launch
        elif not (self._busy and self._busy()):
            return False              # standalone harness: legacy busy probe
        dlg = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="The repository is busy",
        )
        dlg.format_secondary_text(
            "A backup/check, snapshot maintenance, or the backup mount is active.\n"
            "Finish it (or use Stop All in the tray menu; unmount first) before "
            "forgetting or pruning snapshots."
        )
        dlg.run()
        dlg.destroy()
        return True

    def _release(self, op: str) -> None:
        if self._release_lease is not None:
            self._release_lease(op)

    def _on_forget(self, _btn: Gtk.Button) -> None:
        model, it = self._snap_sel.get_selected()
        if not it:
            return
        if self._refuse_if_busy("forget"):
            return
        snap_id = model.get_value(it, 0)
        short_id = model.get_value(it, 1)
        time_str = model.get_value(it, 2)
        dlg = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Forget snapshot {short_id}?",
        )
        dlg.format_secondary_text(
            f"Snapshot from {time_str} will be removed and orphaned data pruned.\n"
            "This cannot be undone."
        )
        if dlg.run() == Gtk.ResponseType.OK:
            dlg.destroy()
            self._snap_lbl.set_text("Forgetting…")
            self._btn_forget.set_sensitive(False)
            threading.Thread(target=self._do_forget, args=(snap_id,), daemon=True).start()
        else:
            dlg.destroy()
            self._release("forget")   # user backed out: hand the lease back

    def _do_forget(self, snap_id: str) -> None:
        try:
            rc, _out = runner.run_forget(self._cfg, snap_id, prune=True,
                                         on_proc=self._on_proc)
        finally:
            self._release("forget")   # fix #4: success, failure or kill — always
        GLib.idle_add(self._after_forget, rc)

    def _after_forget(self, rc: int) -> None:
        if rc == runner.RC_OK:
            self._file_store.clear()
            self._file_lbl.set_text("Snapshot forgotten.")
            self._load_snapshots()
        else:
            self._snap_lbl.set_text(f"Forget failed (rc={rc})")

    def _on_prune_all(self, _btn: Gtk.Button) -> None:
        if self._refuse_if_busy("prune"):
            return
        dlg = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Prune all unreferenced data?",
        )
        dlg.format_secondary_text("Removes orphaned pack files left by interrupted backups. Cannot be undone.")
        if dlg.run() == Gtk.ResponseType.OK:
            dlg.destroy()
            self._snap_lbl.set_text("Pruning…")
            threading.Thread(target=self._do_prune, daemon=True).start()
        else:
            dlg.destroy()
            self._release("prune")    # user backed out: hand the lease back

    def _do_prune(self) -> None:
        try:
            rc, _out = runner.run_prune(self._cfg, on_proc=self._on_proc)
        finally:
            self._release("prune")    # fix #4: success, failure or kill — always
        GLib.idle_add(
            self._snap_lbl.set_text,
            "Prune complete." if rc == runner.RC_OK else f"Prune failed (rc={rc})",
        )


# ---------------------------------------------------------------------------
# Main indicator
# ---------------------------------------------------------------------------
class ResticIndicator:
    def __init__(self, cfg: cfg_mod.Config) -> None:
        self._cfg = cfg
        self._state, self._last_msg = self._state_from_bookkeeping()
        self._op_queue: queue.Queue[tuple[str, bool] | None] = queue.Queue()
        self._queued: set[str] = set()
        self._queue_lock = threading.Lock()
        # Guards swaps of self._cfg: the GTK thread installs new settings, the
        # op worker merges per-operation results. Each operation itself works
        # on a private snapshot and never reads the mutable live config.
        self._cfg_lock = threading.Lock()
        self._log_cooldown: dict[str, float] = {}  # message → last_log_timestamp
        self._worker = threading.Thread(target=self._op_worker, daemon=True)
        self._worker.start()
        self._log_window = LogWindow()
        self._icon_slot = 0
        self._last_progress_ts: float = 0.0
        self._current_proc: subprocess.Popen | None = None
        self._current_backup_total: float = 0.0  # current backup's total size (bytes)
        self._current_backup_done: float = 0.0
        self._backup_item_index: int = 0
        self._backup_item_count: int = 0
        self._backup_dirs: list = []
        self._run_scanned_so_far: float = 0.0
        self._run_total_size_estimate: float = 0.0
        self._current_unit_scan_size: float = 0.0
        self._mount_proc: subprocess.Popen | None = None
        # Fix #5: the immutable configuration snapshot the live mount (and all
        # its snapshot-listing / friendly-view work) belongs to. None unless a
        # mount is up.
        self._mount_cfg: cfg_mod.Config | None = None
        self._mount_spinner_id: int | None = None
        self._mount_frame: int = 0
        self._stop_requested = threading.Event()
        # Fix #3: per-operation-group retry backoff (see BACKOFF_* constants).
        # Runtime state only — a restart starts fresh; the schedule itself is
        # unchanged by it.
        self._backoff_until: dict[str, float] = {}
        self._backoff_streak: dict[str, int] = {}
        self._backoff_lock = threading.Lock()
        self._active_op: str | None = None
        # Fix #4 (restic-box-fixes2.md): one exclusive lease for the repository,
        # plus a registry of every live restic process. The operation queue
        # already serialises backups/checks/unlocks, but snapshot-browser
        # Forget/Prune run on their own threads — a lease that is RESERVED
        # before a process launches is what makes the "only one writer at a
        # time" rule hold across threads, and the registry is what lets Stop
        # All terminate every live process instead of only the newest.
        self._repo_lock = threading.Lock()
        self._repo_owner: str | None = None
        self._active_procs: list = []

        icon_name = self._next_icon_name()
        _render_icon(self._state, icon_name)

        self._indicator = AppIndicator3.Indicator.new(
            "restic-box",
            icon_name,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_icon_theme_path(_ICON_DIR)
        self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._indicator.set_title(f"restic-box: {self._last_msg}")
        self._build_menu()

    # --- menu ---

    def _build_menu(self) -> None:
        menu = Gtk.Menu()

        self._item_size = Gtk.MenuItem(label=self._total_backup_size_label())
        self._item_size.set_sensitive(False)
        menu.append(self._item_size)

        self._item_status = Gtk.MenuItem(label=self._last_msg)
        self._item_status.set_sensitive(False)
        menu.append(self._item_status)

        menu.append(Gtk.SeparatorMenuItem())

        item_backup = Gtk.MenuItem(label="Backup Now")
        item_backup.connect("activate", lambda _: self._trigger_backup())
        menu.append(item_backup)

        item_check = Gtk.MenuItem(label="Run Check Now")
        item_check.connect("activate", lambda _: self._trigger_check())
        menu.append(item_check)

        item_check_full = Gtk.MenuItem(label="Run Check (Verify All Data)…")
        item_check_full.connect("activate", lambda _: self._trigger_full_check())
        menu.append(item_check_full)

        item_unlock = Gtk.MenuItem(label="Unlock Repo and Stop All")
        item_unlock.connect("activate", lambda _: self._stop_and_unlock())
        menu.append(item_unlock)

        item_log = Gtk.MenuItem(label="Show Log…")
        item_log.connect("activate", lambda _: self._show_log())
        menu.append(item_log)

        item_browse = Gtk.MenuItem(label="Browse Snapshots…")
        item_browse.connect("activate", lambda _: self._open_snapshot_browser())
        menu.append(item_browse)

        self._item_mount = Gtk.MenuItem(label="Mount Backup")
        self._item_mount.connect("activate", lambda _: self._toggle_mount())
        menu.append(self._item_mount)

        menu.append(Gtk.SeparatorMenuItem())

        item_settings = Gtk.MenuItem(label="Settings…")
        item_settings.connect("activate", lambda _: self._open_settings())
        menu.append(item_settings)

        item_startup = Gtk.CheckMenuItem(label="Run on startup")
        item_startup.set_active(_autostart_enabled())
        item_startup.connect("toggled", lambda i: _set_autostart(i.get_active()))
        menu.append(item_startup)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._quit)
        menu.append(item_quit)

        menu.show_all()
        self._indicator.set_menu(menu)

    def _total_backup_size_label(self) -> str:
        total_s = (
            runner.fmt_bytes(int(self._cfg.total_backup_size))
            if self._cfg.total_backup_size > 0
            else "--"
        )
        compressed_s = (
            runner.fmt_bytes(int(self._cfg.compressed_backup_size))
            if self._cfg.compressed_backup_size > 0
            else "--"
        )
        return f"Total/compressed backup size: {total_s}/{compressed_s}"

    def _update_idle_size_item(self) -> None:
        self._item_size.set_label(self._total_backup_size_label())

    def _update_status_item(self) -> None:
        self._item_status.set_label(self._last_msg)

    def _weighted_total_pct(self, current_pct: float) -> float:
        dirs = self._backup_dirs
        if not dirs:
            return 0.0
        total_weight = sum(d.cached_size for d in dirs)
        i = self._backup_item_index
        if total_weight == 0:
            return (i + current_pct) / len(dirs)
        done_weight = sum(d.cached_size for d in dirs[:i])
        if i < len(dirs) and dirs[i].cached_size > 0:
            done_weight += dirs[i].cached_size * current_pct
        return done_weight / total_weight

    def _progress_pct(self, pct: float) -> float:
        # Scan-size weighted, including the in-flight fraction of the current
        # unit, so the tray percentage advances during a single long unit too
        # rather than only at unit boundaries.
        total = self._run_total_size_estimate
        if total <= 0:
            return self._weighted_total_pct(pct)
        cur = self._current_unit_scan_size
        return min(1.0, (self._run_scanned_so_far + cur * pct) / total)

    def _backup_progress_msg(
        self, pct: float, done: float, total: float,
        scanned: float = 0.0, total_estimate: float = 0.0,
    ) -> tuple[str, float]:
        total_pct = self._progress_pct(pct)

        done_s = runner.fmt_bytes(int(done))
        total_s = runner.fmt_bytes(int(total)) if total else "?"
        msg = f"Total: {total_pct*100:.0f}% Uploading: {pct*100:.0f}% ({done_s} / {total_s})"
        return msg, total_pct

    def _next_icon_name(self) -> str:
        self._icon_slot = 1 - self._icon_slot
        return f"restic-box-{_ICON_SLOT[self._icon_slot]}"

    # --- state / icon ---

    def _set_state(self, state: BackupState, msg: str, progress: float = -1.0) -> None:
        self._state = state
        self._last_msg = msg
        icon_name = self._next_icon_name()
        _render_icon(state, icon_name, progress)
        self._indicator.set_icon_full(icon_name, "restic-box")
        self._indicator.set_title(f"restic-box: {msg}")
        if state != BackupState.RUNNING:
            self._update_idle_size_item()
        self._update_status_item()

    def _on_progress(self, pct: float, done: int, total: int) -> None:
        """Called from backup thread with JSON progress data — throttled to ~1 Hz."""
        if self._stop_requested.is_set():
            return
        self._current_backup_done = float(done)
        self._current_backup_total = float(total)

        now = time.monotonic()
        if now - self._last_progress_ts < 1.0:
            return
        self._last_progress_ts = now

        msg, total_pct = self._backup_progress_msg(
            pct, done, total,
            scanned=self._run_scanned_so_far,
            total_estimate=self._run_total_size_estimate,
        )
        GLib.idle_add(self._set_state, BackupState.RUNNING, msg, total_pct)

    # --- log ---

    def _log(self, text: str) -> None:
        # Deduplicate scheduler messages (1-hour cooldown)
        should_skip = text.startswith("Scheduled") or text.startswith("Skipped") or "already queued" in text
        if should_skip:
            now = time.time()
            if text in self._log_cooldown:
                if now - self._log_cooldown[text] < 3600:
                    return
            self._log_cooldown = {k: v for k, v in self._log_cooldown.items() if now - v < 3600}
            self._log_cooldown[text] = now
        ts = datetime.now().strftime("%H:%M:%S")
        # Review fix #6: the visible window is bounded in both lines and line
        # size, so what it trims or excerpts has to exist somewhere readable.
        # Every operational line is mirrored to the rotating diagnostic log file
        # (restic_diag_log), which is bounded by size and rotation instead.
        diag_log.note(f"[{ts}] {text}")
        GLib.idle_add(self._log_window.append, f"[{ts}] {text}")

    def _show_log(self) -> None:
        self._log_window.show_all()
        self._log_window.present()

    def _open_snapshot_browser(self) -> None:
        self._snap_browser = SnapshotBrowser(
            self._cfg, on_proc=self._set_current_proc,
            busy=self._operation_active,
            acquire=self._reserve_browser_op, release=self._release_repo)
        self._snap_browser.show_all()

    # --- operation queue & backup ---

    def _enqueue(self, op: str, automatic: bool = False) -> None:
        with self._queue_lock:
            if op in self._queued:
                self._log(f"{op} already queued — skipping duplicate.")
                return
            self._queued.add(op)
        self._op_queue.put((op, automatic))

    def _clear_queue(self) -> None:
        with self._queue_lock:
            while True:
                try:
                    self._op_queue.get_nowait()
                except queue.Empty:
                    break
            self._queued.clear()

    # --- retry backoff (fix #3, restic-box-fixes2.md) ---

    @staticmethod
    def _backoff_group(op: str) -> str:
        """check_full shares the check group: a failed subset check and a failed
        full check both gate the same scheduled rotation."""
        return "check" if op in ("check", "check_full") else op

    def _apply_retry_backoff(self, op: str, reason: str) -> None:
        """The automatic version of *op* just failed (preflight) or was stopped:
        schedule no re-trigger before the cooldown expires. Failed preflight
        escalates exponentially; a user cancel is a fixed pause. The streak is
        only cleared by an operation that actually got through preflight."""
        group = self._backoff_group(op)
        with self._backoff_lock:
            if reason == "cancel":
                delay = BACKOFF_CANCEL_SECONDS
            else:
                streak = self._backoff_streak.get(group, 0) + 1
                self._backoff_streak[group] = streak
                delay = min(BACKOFF_BASE_SECONDS * 2 ** (streak - 1),
                            BACKOFF_CAP_SECONDS)
            until = max(self._backoff_until.get(group, 0.0), time.time() + delay)
            self._backoff_until[group] = until
        minutes = max(1, round(delay / 60))
        self._log(f"Automatic {group} retries paused for ~{minutes} min "
                  f"({reason}).")

    def _backoff_active(self, op: str) -> bool:
        group = self._backoff_group(op)
        with self._backoff_lock:
            return time.time() < self._backoff_until.get(group, 0.0)

    def _clear_retry_backoff(self, op: str) -> None:
        group = self._backoff_group(op)
        with self._backoff_lock:
            self._backoff_until.pop(group, None)
            self._backoff_streak.pop(group, None)

    def _snapshot_cfg(self) -> cfg_mod.Config:
        """Private, internally consistent copy of the live configuration.

        Review fix #5: the copy is taken while holding ``_cfg_lock`` — the same
        lock settings saves (_open_settings) and bookkeeping updates
        (_merge_backup_result / _merge_check_result, _merge_dir_sizes) use. Without it, a save landing mid-copy could produce
        a snapshot with a new repository URL and old credentials, or a half-old
        Directory list: exactly the inconsistent configuration an operation must
        never see. The snapshot belongs to this operation; newer settings apply
        to the NEXT one.
        """
        with self._cfg_lock:
            return copy.deepcopy(self._cfg)

    def _op_worker(self) -> None:
        while True:
            item = self._op_queue.get()
            if item is None:
                break
            op, automatic = item
            cfg: cfg_mod.Config | None = None
            try:
                self._stop_requested.clear()
                self._active_op = op
                # Immutable snapshot for the whole operation: a settings save
                # (possibly pointing at a different repository) mid-operation
                # must not redirect the remaining commands, retention, or stats.
                # New settings take effect on the NEXT operation. Taken under
                # _cfg_lock by _snapshot_cfg() (review fix #5).
                cfg = self._snapshot_cfg()
                # Review fix #4: the pre-flight is an ssh probe with the backup's
                # own transport options (ProxyCommand/ProxyJump work), not a raw
                # TCP connect to host:port.
                reachable, why = runner.ssh_transport_ok(cfg)
                # Fix #3 (restic-box-fixes2.md): cancellation is honored AFTER
                # the preflight too — a Stop All that arrives mid-probe must
                # prevent the backup/check command from ever launching.
                if self._stop_requested.is_set():
                    self._log(f"{op} cancelled by user — nothing was launched.")
                    self._record_attempt(op, cfg, cfg_mod.OUTCOME_CANCELLED,
                                         "stopped by user", persist=False)
                    GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                    self._apply_retry_backoff(op, "cancel")
                    continue
                if not reachable:
                    # Fix #3: an unreachable host must not re-trigger this op
                    # on every scheduler tick; the retry waits for an
                    # exponentially growing backoff instead.
                    self._log(f"Skipping {op} — {why}")
                    # Fix #9 (restic-box-fixes2.md): say so in the bookkeeping too.
                    # Without this the skip left no trace, and the next time the
                    # tray re-derived its label from bookkeeping the previous
                    # success was on screen again as though it were this attempt's
                    # result — while the scheduled backup never happened.
                    self._record_attempt(op, cfg, cfg_mod.OUTCOME_SKIPPED,
                                         f"connection unreachable: {why}")
                    self._show_attempt()
                    self._apply_retry_backoff(op, "preflight")
                    continue
                # Fix #4 (restic-box-fixes2.md): take the exclusive repository
                # lease before launching anything. The only way this can be
                # contended is a snapshot-browser Forget/Prune already running
                # on its own thread — the queued operation then waits for the
                # browser to finish instead of starting a second writer, and
                # a Stop All during the wait cancels it like any other.
                got_lease = self._reserve_repo(op)
                while not got_lease:
                    if self._stop_requested.is_set():
                        self._log(f"{op} cancelled — the repository is busy.")
                        self._record_attempt(op, cfg, cfg_mod.OUTCOME_CANCELLED,
                                             "the repository was busy", persist=False)
                        GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                        break
                    time.sleep(0.5)
                    got_lease = self._reserve_repo(op)
                if got_lease:
                    self._clear_retry_backoff(op)   # got through: streak broken
                    if op == "backup":
                        self._run_backup(automatic, cfg=cfg)
                    elif op == "check":
                        self._run_check(automatic, cfg=cfg)
                    elif op == "check_full":
                        self._run_full_check(automatic, cfg=cfg)
                    elif op == "unlock":
                        self._run_unlock(cfg)
            except Exception:
                traceback.print_exc()
                # A crashed operation must leave an honest tray state, not the
                # last "running/success" one.
                self._record_attempt(op, cfg if cfg is not None else self._cfg,
                                     cfg_mod.OUTCOME_FAILED, "the operation crashed")
                GLib.idle_add(self._set_state, BackupState.ERROR, f"{op} crashed")
            finally:
                self._active_op = None
                self._release_repo(op)   # fix #4: crashed or not, the repo is free again
                with self._queue_lock:
                    self._queued.discard(op)

    def _trigger_backup(self, automatic: bool = False) -> None:
        self._enqueue("backup", automatic)

    def _mount_active(self) -> bool:
        return bool(self._mount_proc and self._mount_proc.poll() is None)

    def _skip_if_mounted(self, op: str, automatic: bool, cfg: cfg_mod.Config) -> bool:
        if not self._mount_active():
            return False
        if automatic:
            self._log(f"Skipped {op} — mount is active.")
            # Fix #9: a scheduled run the mount blocked never happened, and saying
            # so is the difference between "backed up today" and a lie. A manual
            # attempt is not recorded: the user is being told about it directly,
            # and a tray warning that outlives the dialog would be noise.
            self._record_attempt(_OP_RECORD_KEYS[op], cfg, cfg_mod.OUTCOME_SKIPPED,
                                 "a restic mount is holding the repository")
        else:
            _notify(f"Cannot {op} while mount is active. Unmount first.")
        return True

    def _run_backup(self, automatic: bool = False,
                    cfg: cfg_mod.Config | None = None) -> None:
        # cfg: the operation's private snapshot (from _op_worker). A direct call
        # without one takes a fresh snapshot of the live config here.
        if cfg is None:
            cfg = self._snapshot_cfg()   # locked snapshot, review fix #5
        # Fix #1 (restic-box-fixes2.md): this run's results may only be credited
        # to the repository + source configuration it actually used.
        identity = cfg_mod.config_identity(cfg)
        if self._skip_if_mounted("backup", automatic, cfg=cfg):
            return
        inhibit_fd = _inhibit_sleep() if cfg.inhibit_sleep else None
        try:
            dirs = [d for d in cfg.directories if d.enabled]
            if not dirs:
                GLib.idle_add(self._set_state, BackupState.IDLE, "No directories configured")
                self._log("No enabled directories to back up.")
                return

            self._log("=== Backup started ===")
            if cfg.split_threshold_gib > 0:
                GLib.idle_add(self._set_state, BackupState.RUNNING, "Planning split…")
            units: list = []
            try:
                for d in dirs:
                    units += split.expand(d, cfg.split_threshold_gib, self._log,
                                          cfg.split_drift_factor,
                                          should_stop=self._stop_requested.is_set)
            except split.SplitCancelled:
                # Stopped during the scan/planning: nothing was launched yet, so
                # there is no process to terminate and nothing to unlock.
                self._log("Backup stopped by user during planning — no backups started.")
                self._note_stopped(cfg, "stopped during planning")
                GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                return
            dirs = units
            unit_totals: dict[int, float] = {}
            units_ok = units_partial = units_failed = 0
            run_total_size = 0.0
            run_uploaded_size = 0.0
            self._last_progress_ts = 0.0
            self._backup_dirs = dirs
            self._backup_item_count = len(dirs)
            self._current_backup_done = 0.0
            self._current_backup_total = 0.0
            self._run_scanned_so_far = 0.0
            self._current_unit_scan_size = 0.0
            # cached_size here is the scan size (split units carry it from the
            # scan; unsplit sources now get it too), so progress % is accurate.
            scan_sizes = [d.cached_size for d in dirs]
            self._run_total_size_estimate = sum(scan_sizes)
            # One identity for the whole operation (issue #8): every unit's
            # snapshot gets this tag, so bydate/ sees ONE run no matter how
            # long the units take or whether other backups overlap.
            run_tag = f"{views.RUN_TAG_PREFIX}{uuid.uuid4().hex}"
            msg, total_pct = self._backup_progress_msg(0.0, 0.0, 0.0)
            GLib.idle_add(self._set_state, BackupState.RUNNING, msg, total_pct)
            for index, d in enumerate(dirs):
                # Cancellation is checked BEFORE each launch (issue #6): a Stop
                # that arrives while unit N is running must prevent unit N+1
                # from ever starting.
                if self._stop_requested.is_set():
                    self._log("Backup stopped by user — remaining directories skipped.")
                    self._note_stopped(cfg)
                    GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                    return
                self._backup_item_index = index
                self._current_unit_scan_size = scan_sizes[index]
                self._log(f"Backing up [{index + 1}/{len(dirs)}]: " + " + ".join(runner.targets_of(d)))
                self._current_backup_done = 0.0
                self._current_backup_total = 0.0
                msg, total_pct = self._backup_progress_msg(0.0, 0.0, 0.0)
                GLib.idle_add(self._set_state, BackupState.RUNNING, msg, total_pct)
                rc, _out, data_added = runner.run_backup(
                    cfg, d,
                    on_output=self._log,
                    on_progress=self._on_progress,
                    on_proc=self._set_current_proc,
                    tag=run_tag,
                )
                if rc == runner.RC_OK:
                    units_ok += 1
                elif rc == runner.RC_PARTIAL:
                    # restic rc=3: the snapshot was created but some source files
                    # could not be read — the backup is incomplete, not successful.
                    units_partial += 1
                    self._log(f"WARNING: PARTIAL backup of {d.path} (rc=3): "
                              f"some files were unreadable and skipped")
                else:
                    units_failed += 1
                    if self._stop_requested.is_set():
                        # Terminated by Stop All, not a real failure: the tray
                        # ends at "Stopped", not "Failed".
                        self._log(f"backup of {d.path} interrupted by Stop (rc={rc})")
                    else:
                        self._log(f"ERROR: backup of {d.path} failed (rc={rc})")
                if rc in (runner.RC_OK, runner.RC_PARTIAL):
                    run_total_size += self._current_backup_total
                    run_uploaded_size += data_added
                    # Persist the actual stored size, but advance progress by the
                    # scan size so the % matches the estimate.
                    if self._current_backup_total > 0:
                        d.cached_size = self._current_backup_total
                        unit_totals[id(d)] = self._current_backup_total
                    self._run_scanned_so_far += scan_sizes[index]
                remaining = max(self._run_total_size_estimate - self._run_scanned_so_far, 0)
                GLib.idle_add(
                    self._log,
                    f"Totals: {runner.fmt_bytes(self._run_scanned_so_far)} scanned"
                    + (f" / {runner.fmt_bytes(remaining)} remaining" if remaining > 0 else ""),
                )
                if self._stop_requested.is_set():
                    self._log("Backup stopped by user — remaining directories skipped.")
                    self._note_stopped(cfg)
                    GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                    return

            # roll split-unit sizes back up to the configured source they came from
            by_origin: dict[int, tuple] = {}
            for t in dirs:
                if isinstance(t, split.Target):
                    by_origin.setdefault(id(t.origin), (t.origin, []))[1].append(t)
            for origin, ts_units in by_origin.values():
                if all(id(t) in unit_totals for t in ts_units):
                    origin.cached_size = sum(unit_totals[id(t)] for t in ts_units)

            # A stop request must also skip retention and stats: retention runs
            # `forget --prune`, which is destructive and must never follow an
            # interrupted (aborted) backup run.
            if self._stop_requested.is_set():
                self._log("Backup stopped by user — retention and statistics skipped.")
                self._note_stopped(cfg, "stopped before retention")
                GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                return

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            # Only a COMPLETE run (every unit rc=0) counts as success and may
            # feed automatic retention. A partial run (rc=3) left incomplete
            # snapshots; running forget --prune after one would age out the
            # older, still-complete snapshots that are the only copies of the
            # unreadable files.
            full_ok = units_failed == 0 and units_partial == 0
            # Fix #9 (restic-box-fixes2.md): the outcome is recorded as one of the
            # five kinds with a reason, not as a bare ok/partial pair — that is what
            # lets the tray say what happened while the last backup that really
            # completed keeps its own timestamp.
            if full_ok:
                outcome, why = cfg_mod.OUTCOME_SUCCESS, ""
            elif units_failed:
                outcome = cfg_mod.OUTCOME_FAILED
                why = f"{units_failed} of {len(units)} backup units failed"
            else:
                outcome = cfg_mod.OUTCOME_PARTIAL
                why = f"{units_partial} of {len(units)} backup units had unreadable files"
            if full_ok:
                self._log(f"=== Backup complete at {ts} ===")
                ret_dur = runner.retention_duration(cfg)
                if cfg.retention_enabled and ret_dur:
                    self._log(f"=== Applying retention (--keep-within {ret_dur}) ===")
                    rc_r, _out = runner.run_retention_forget(
                        cfg, on_output=self._log, on_proc=self._set_current_proc)
                    if rc_r not in (runner.RC_OK, runner.RC_PARTIAL):
                        self._log(f"WARNING: retention forget failed (rc={rc_r})")
                    # A Stop All that arrived while retention was running must
                    # also stop the follow-on commands: no restic subprocess
                    # (here: repository statistics) is launched once the shared
                    # cancellation token is set, and the run ends as Stopped,
                    # not as success.
                    if self._stop_requested.is_set():
                        self._log("Backup stopped by user — statistics skipped.")
                        self._note_stopped(cfg, "stopped during retention")
                        GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                        return
            elif units_failed:
                GLib.idle_add(self._set_state, BackupState.ERROR, f"Failed at {ts}")
                self._log(f"=== Backup FAILED at {ts} ===")
            else:
                GLib.idle_add(self._set_state, BackupState.PARTIAL,
                              f"Backup INCOMPLETE at {ts} — some files unreadable")
                self._log(f"=== Backup PARTIAL at {ts} — automatic retention skipped ===")
            compressed_backup_size = cfg.compressed_backup_size
            if full_ok:
                # Last cancellation gate (review fix #1): statistics must not
                # launch after a Stop All even when retention was skipped and
                # no unit-level gate was hit in between.
                if self._stop_requested.is_set():
                    self._log("Backup stopped by user — statistics skipped.")
                    self._note_stopped(cfg, "stopped before statistics")
                    GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                    return
                latest_compressed_size = runner.run_raw_data_size(
                    cfg, on_proc=self._set_current_proc)
                if latest_compressed_size is not None:
                    compressed_backup_size = latest_compressed_size
            # Merge only the operation's own results, credited to the identity
            # (repository + sources) the operation ran against (fix #1). If the
            # user moved the settings to another repository mid-run, the outcome
            # is filed in that run's history and the newly selected repository is
            # NOT marked backed up. Sizes merge first so this save persists them.
            self._merge_dir_sizes(cfg.directories)
            self._merge_backup_result(
                identity,
                ts=time.time(),
                ok=full_ok,
                partial=units_partial > 0 and units_failed == 0,
                total_backup_size=run_total_size if full_ok else None,
                compressed_backup_size=compressed_backup_size,
                status=outcome,
                reason=why,
            )
            if full_ok:
                GLib.idle_add(self._set_state, BackupState.SUCCESS, f"Backup complete at {ts}")
        finally:
            # Carry scan/backup size bookkeeping back to the live config's
            # directories (matched by path), even if the run was stopped early.
            self._merge_dir_sizes(cfg.directories)
            self._backup_item_index = 0
            self._backup_item_count = 0
            self._backup_dirs = []
            if inhibit_fd is not None:
                _release_inhibit(inhibit_fd)

    # --- check ---

    def _trigger_check(self, automatic: bool = False) -> None:
        self._enqueue("check", automatic)

    def _run_check(self, automatic: bool = False,
                   cfg: cfg_mod.Config | None = None) -> None:
        if cfg is None:
            cfg = self._snapshot_cfg()   # locked snapshot, review fix #5
        identity = cfg_mod.config_identity(cfg)   # fix #1: results belong to this identity
        if self._skip_if_mounted("check", automatic, cfg=cfg):
            return
        inhibit_fd = _inhibit_sleep() if cfg.inhibit_sleep else None
        try:
            GLib.idle_add(self._set_state, BackupState.CHECKING, "Checking repo…")
            if (cfg.check_read_data_subset
                    and cfg.check_subset_current > cfg.check_subset_total):
                cfg = dataclasses.replace(cfg, check_subset_current=1)
                self._merge_check_result(identity, ts=cfg.last_check_ts,
                                         ok_ts=cfg.last_check_ok_ts, subset_current=1)
                self._log("Subset index reset to 1 (total was reduced).")
            subset_info = ""
            if cfg.check_read_data_subset:
                n, t = cfg.check_subset_current, cfg.check_subset_total
                subset_info = f" (subset {n}/{t})"
            self._log(f"=== restic check started{subset_info} ===")
            rc, _out = runner.run_check(cfg, on_output=self._log, on_proc=self._set_current_proc)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            now = time.time()
            if self._stop_requested.is_set():
                # Fix #9: a check Stop All terminated is not a check that failed.
                # Terminated processes return a nonzero rc, which would otherwise be
                # filed as a repository failure the user never asked for.
                self._log("Check stopped by user.")
                self._record_attempt("check", cfg, cfg_mod.OUTCOME_CANCELLED,
                                     "stopped by user", persist=False)
                GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                return
            if rc in (runner.RC_OK, runner.RC_PARTIAL):
                GLib.idle_add(self._set_state, BackupState.SUCCESS, f"Check OK at {ts}{subset_info}")
                if cfg.check_read_data_subset:
                    n = cfg.check_subset_current % cfg.check_subset_total + 1
                else:
                    n = cfg.check_subset_current
                self._merge_check_result(identity, ts=now, ok_ts=now, subset_current=n,
                                         status=cfg_mod.OUTCOME_SUCCESS)
            else:
                # last_check_ts records the ATTEMPT, not the success: a failed
                # check is due again on the next schedule day, never on every
                # scheduler tick (a permanently failing check used to re-run
                # once per minute forever).
                GLib.idle_add(self._set_state, BackupState.ERROR, f"Check FAILED at {ts}")
                self._merge_check_result(identity, ts=now, ok_ts=cfg.last_check_ok_ts,
                                         subset_current=cfg.check_subset_current,
                                         status=cfg_mod.OUTCOME_FAILED,
                                         reason=f"restic check returned rc={rc}")
            self._log(f"=== restic check finished (rc={rc}) at {ts} ===")
        finally:
            if inhibit_fd is not None:
                _release_inhibit(inhibit_fd)

    def _trigger_full_check(self, automatic: bool = False) -> None:
        self._enqueue("check_full", automatic)

    def _run_full_check(self, automatic: bool = False,
                        cfg: cfg_mod.Config | None = None) -> None:
        if cfg is None:
            cfg = self._snapshot_cfg()   # locked snapshot, review fix #5
        identity = cfg_mod.config_identity(cfg)   # fix #1: results belong to this identity
        if self._skip_if_mounted("full check", automatic, cfg=cfg):
            return
        inhibit_fd = _inhibit_sleep() if cfg.inhibit_sleep else None
        try:
            GLib.idle_add(self._set_state, BackupState.CHECKING, "Checking repo (all data)…")
            self._log("=== restic check --read-data started ===")
            rc, _out = runner.run_check_full(cfg, on_output=self._log, on_proc=self._set_current_proc)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            now = time.time()
            if self._stop_requested.is_set():
                self._log("Full check stopped by user.")
                self._record_attempt("check", cfg, cfg_mod.OUTCOME_CANCELLED,
                                     "stopped by user", persist=False)
                GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                return
            if rc in (runner.RC_OK, runner.RC_PARTIAL):
                GLib.idle_add(self._set_state, BackupState.SUCCESS, f"Full check OK at {ts}")
                # A verified full check covers everything a regular subset check
                # would: it resets the subset rotation AND counts as this
                # schedule slot's check, so a regular check does not immediately
                # follow a full-repository verification.
                self._merge_check_result(identity, ts=now, ok_ts=now, subset_current=1,
                                         status=cfg_mod.OUTCOME_SUCCESS)
            else:
                GLib.idle_add(self._set_state, BackupState.ERROR, f"Full check FAILED at {ts}")
                self._merge_check_result(identity, ts=now, ok_ts=cfg.last_check_ok_ts,
                                         subset_current=cfg.check_subset_current,
                                         status=cfg_mod.OUTCOME_FAILED,
                                         reason=f"restic check --read-data returned rc={rc}")
            self._log(f"=== restic check --read-data finished (rc={rc}) at {ts} ===")
        finally:
            if inhibit_fd is not None:
                _release_inhibit(inhibit_fd)

    # --- repo ownership (browser guard) ---

    def _operation_active(self) -> bool:
        """True while an operation owns the repository: a process is running,
        one is still queued, or a snapshot-browser Forget/Prune holds the
        exclusive lease (fix #4)."""
        with self._queue_lock:
            queued = bool(self._queued)
        proc = self._current_proc
        if queued or (proc is not None and proc.poll() is None):
            return True
        with self._repo_lock:
            return self._repo_owner is not None

    # --- exclusive repository lease (fix #4, restic-box-fixes2.md) ---

    def _reserve_repo(self, op: str) -> bool:
        """Atomically claim exclusive repository access for *op*.

        Returns False when something else already holds the lease — a running
        backup/check/unlock or a snapshot-browser Forget/Prune. The caller must
        run its restic commands only after a successful reservation and call
        _release_repo() when done; the reservation is taken before the process
        starts, so there is no window where two writers believe they own the
        repository.
        """
        with self._repo_lock:
            if self._repo_owner is not None:
                return False
            self._repo_owner = op
            return True

    def _reserve_browser_op(self, op: str) -> bool:
        """Lease reservation for a snapshot-browser Forget/Prune.

        Fix #5 (restic-box-fixes2.md): a live `restic mount` serves this very
        repository, and Forget/Prune is destructive in ways a serving mount
        must not race with — so while a mount is up, browser destructive
        actions are refused outright (documented behavior: unmount first).
        """
        if self._mount_active():
            return False
        return self._reserve_repo(op)

    def _release_repo(self, op: str) -> None:
        """Release the lease if (and only if) *op* still owns it — a Stop All
        may already have force-released it while this operation unwound."""
        with self._repo_lock:
            if self._repo_owner == op:
                self._repo_owner = None

    def _force_release_repo(self) -> None:
        """Stop All: the lease holder's process has just been terminated, so
        its lease must not block the follow-up unlock. The holder's own
        release is left as a harmless no-op by _release_repo's ownership
        check."""
        with self._repo_lock:
            self._repo_owner = None

    def _live_procs(self) -> list:
        """Every live restic process this app launched (registry + current),
        pruning dead ones. Stop All and quit terminate ALL of them — with
        browser forget/prune registering too, a single _current_proc slot
        could miss a second live process (fix #4)."""
        with self._repo_lock:
            procs = [p for p in self._active_procs if p.poll() is None]
            cur = self._current_proc
            if cur is not None and cur.poll() is None and all(cur is not p for p in procs):
                procs.append(cur)
            self._active_procs = procs
            return procs

    # --- unlock / stop all ---

    def _stop_and_unlock(self) -> None:
        # Shared cancellation token: the worker checks it before every unit
        # launch, split planning aborts on it, and retention/stats are skipped
        # once it is set. Then the currently-running process (backup, check,
        # unlock, retention, stats, or a browser forget/prune) is terminated
        # with bounded escalation, and finally the repo is unlocked.
        self._stop_requested.set()
        # Fix #3 (restic-box-fixes2.md): everything this Stop All removes from
        # the pipeline — the running operation and anything still queued — gets
        # a cancel backoff, so a cancelled scheduled backup/check is not
        # immediately re-queued on the next tick.
        with self._queue_lock:
            affected = set(self._queued)
        if self._active_op:
            affected.add(self._active_op)
        self._clear_queue()
        for op in affected:
            if op != "unlock":
                self._apply_retry_backoff(op, "cancel")
        procs = self._live_procs()
        if procs:
            self._log("Stopping current restic process…")
            for proc in procs:
                runner.stop_process(proc)  # bounded terminate→wait→kill
        # Fix #4: the cancelled lease (backup, check, or a browser Forget/
        # Prune whose process was just killed) must not block the unlock that
        # Stop All queues below.
        self._force_release_repo()
        self._enqueue("unlock")

    def _run_unlock(self, cfg: cfg_mod.Config | None = None) -> None:
        if cfg is None:
            cfg = self._snapshot_cfg()   # locked snapshot, review fix #5
        GLib.idle_add(self._set_state, BackupState.CHECKING, "Unlocking repo…")
        self._log("=== restic unlock ===")
        rc, _out = runner.run_unlock(cfg, on_output=self._log, on_proc=self._set_current_proc)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if rc == 0:
            GLib.idle_add(self._set_state, BackupState.IDLE, f"Unlocked at {ts}")
        else:
            GLib.idle_add(self._set_state, BackupState.ERROR, f"Unlock FAILED at {ts}")
        self._log(f"=== restic unlock finished (rc={rc}) ===")

    # --- mount ---

    def _toggle_mount(self) -> None:
        if self._mount_proc and self._mount_proc.poll() is None:
            self._unmount_backup()
        else:
            self._mount_backup()

    def _cleanup_stale_mount(self) -> None:
        """Unmount a stale FUSE endpoint and remove leftover friendly-view trees."""
        subprocess.run(["fusermount3", "-u", str(_RAW_MOUNT_PATH)], capture_output=True)
        for published in (_LATEST_PATH, _BYDATE_PATH):
            # .building/ and .old too: a swap interrupted by a crash leaves one (fix #6).
            for tree in [published, *views.leftover_paths(published)]:
                _set_tree_readonly(tree, False)
                shutil.rmtree(tree, ignore_errors=True)

    def _mount_backup(self) -> None:
        # Fix #5 (restic-box-fixes2.md): the whole mount — restic mount,
        # snapshot listing, friendly-view building — runs against ONE
        # immutable configuration snapshot taken here, on the settings
        # thread. A settings save (even one pointing at a different
        # repository) mid-mount can then never redirect part of the work.
        # The mount also refuses to start while any repository owner is
        # active: a queued/running backup/check/unlock or a browser Forget/
        # Prune lease (fix #4's lease, included in _operation_active()).
        if self._operation_active():
            _notify("Can't mount yet — a backup/check or snapshot maintenance "
                    "is in progress. Stop it first to allow mounts.")
            return
        if not shutil.which("fusermount3"):
            _notify("fuse3 is not installed — install it to use the mount feature")
            return
        self._cleanup_stale_mount()
        mount_cfg = self._snapshot_cfg()
        self._mount_cfg = mount_cfg
        _RAW_MOUNT_PATH.mkdir(parents=True, exist_ok=True)
        self._mount_frame = 0
        self._item_mount.set_label(_MOUNT_FRAMES[0])
        self._item_mount.set_sensitive(False)
        self._mount_spinner_id = GLib.timeout_add(500, self._tick_mount_label)
        threading.Thread(target=self._run_mount, args=(mount_cfg,), daemon=True).start()

    def _tick_mount_label(self) -> bool:
        self._mount_frame = (self._mount_frame + 1) % len(_MOUNT_FRAMES)
        self._item_mount.set_label(_MOUNT_FRAMES[self._mount_frame])
        return True

    def _stop_mount_spinner(self) -> None:
        if self._mount_spinner_id is not None:
            GLib.source_remove(self._mount_spinner_id)
            self._mount_spinner_id = None

    def _run_mount(self, mount_cfg: cfg_mod.Config) -> None:
        # mount_cfg is the immutable snapshot from _mount_backup(): every
        # repository command this mount performs uses it, never the live
        # settings (fix #5).
        try:
            self._mount_proc = runner.start_mount(mount_cfg, str(_RAW_MOUNT_PATH))
            assert self._mount_proc.stdout is not None
            # Issue #14: the mount's stdout must be drained *continuously*.
            # Building the friendly views issues repository/ls work that the
            # mount services while restic restic keeps logging request lines
            # to stdout; if view building ran inline between two reads, the
            # 64K pipe buffer could fill and the child would block on a full
            # pipe while we block on it (documented deadlock). So the drain
            # loop only reads, and post-mount work runs on its own thread.
            built = threading.Event()
            proc = self._mount_proc
            assert proc.stdout is not None

            def drain() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    self._log(f"[mount] {line}")
                    if "serving" in line.lower() and not built.is_set():
                        built.set()
                        threading.Thread(target=self._mount_ready,
                                         args=(mount_cfg,), daemon=True).start()
                proc.stdout.close()      # mount exited: don't leave the pipe to the GC

            drain_thread = threading.Thread(target=drain, daemon=True)
            drain_thread.start()
            rc = self._mount_proc.wait()
            drain_thread.join()  # flush any queued [mount] lines first
            if rc != 0:
                self._log(f"Mount exited with rc={rc}")
        except Exception as e:
            self._log(f"Mount failed: {e}")
        finally:
            self._mount_proc = None
            self._mount_cfg = None   # fix #5: the mount's config snapshot goes with it
            self._cleanup_stale_mount()
            GLib.idle_add(self._stop_mount_spinner)
            GLib.idle_add(self._item_mount.set_label, "Mount Backup")
            GLib.idle_add(self._item_mount.set_sensitive, True)
            self._log("Unmounted.")

    def _mount_ready(self, mount_cfg: cfg_mod.Config) -> None:
        """Post-mount work, off the stdout drain thread (issue #14).

        mount_cfg stays the mount's own snapshot (fix #5): friendly views link
        against the repository that is actually serving, not whatever the
        settings happen to say now."""
        self._build_friendly_views(mount_cfg)
        GLib.idle_add(self._stop_mount_spinner)
        GLib.idle_add(self._item_mount.set_label, "Unmount Backup")
        GLib.idle_add(self._item_mount.set_sensitive, True)
        subprocess.Popen(["xdg-open", str(_MOUNT_DIR)])
        self._log(f"Mounted at {_MOUNT_DIR}")

    def _unmount_backup(self) -> None:
        # Bounded terminate→wait→kill (same helper as Stop All): the child
        # exiting is what fires _run_mount's full cleanup — proc, mount
        # point, friendly-view trees, spinner and menu label.
        if self._mount_proc and self._mount_proc.poll() is None:
            runner.stop_process(self._mount_proc)
        subprocess.run(["fusermount3", "-u", str(_RAW_MOUNT_PATH)], capture_output=True)

    def _build_friendly_views(self, mount_cfg: cfg_mod.Config) -> None:
        # Fix #6 (restic-box-fixes2.md): a snapshot listing that fails or ends
        # early raises QueryError instead of answering "no snapshots", and the
        # builders only publish a replacement once it is complete. So when this
        # fails, whatever the user could browse before is still on disk — the
        # log says exactly that, instead of pretending the view is simply empty.
        try:
            snaps = runner.run_snapshots_json(mount_cfg)
        except runner.QueryError as e:
            self._log(f"[mount] Could not list snapshots ({e}). The mount is "
                      f"still usable through {_RAW_MOUNT_PATH}/ids/; the previous "
                      f"friendly views were left untouched.")
            return
        try:
            def ls_children(snap_id: str, path: str):
                # bound to the mount's snapshot, never the live settings
                return runner.run_ls_children(mount_cfg, snap_id, path)

            latest = views.build_latest(_LATEST_PATH, _RAW_MOUNT_PATH, snaps,
                                        ls_children=ls_children, log=self._log)
            bydate = views.build_bydate(_BYDATE_PATH, _RAW_MOUNT_PATH, snaps,
                                        ls_children=ls_children, log=self._log)
        except Exception as e:
            self._log(f"[mount] Could not build friendly views: {e}. The mount "
                      f"is still usable through {_RAW_MOUNT_PATH}/ids/; the "
                      f"previous friendly views were left untouched.")
            return
        self._log(f"[mount] Built latest/ ({latest} links) and "
                  f"bydate/ ({bydate} links) from {len(snaps)} snapshot(s)")

    # --- backup scheduler ---

    def _backup_due(self) -> bool:
        sched = self._cfg.backup_schedule
        if sched == "disabled":
            return False
        if sched == "interval":
            if self._cfg.last_backup_ts <= 0:
                return True
            return time.time() - self._cfg.last_backup_ts >= self._cfg.backup_interval_hours * 3600
        if sched == "daily":
            if self._cfg.last_backup_ts > 0:
                if date.fromtimestamp(self._cfg.last_backup_ts) >= date.today():
                    return False
            return datetime.now().hour >= self._cfg.backup_daily_hour
        return False

    def _is_queued(self, op: str) -> bool:
        with self._queue_lock:
            return op in self._queued

    def _schedule_backup(self) -> bool:
        # A run already in progress counts as "not due": last_backup_ts is only
        # updated at completion, so during a long backup the scheduler would
        # otherwise re-fire every tick and log a duplicate-skip pair.
        # Fix #3: a just-failed (preflight) or just-cancelled backup waits out
        # its retry backoff instead of re-firing on this tick.
        if self._backoff_active("backup"):
            return True
        if not self._is_queued("backup") and self._backup_due():
            self._log("Scheduled backup triggered.")
            self._trigger_backup(automatic=True)
        return True

    # --- daily check scheduler ---

    def _check_due(self) -> bool:
        today = date.today()
        last = date.fromtimestamp(self._cfg.last_check_ts) if self._cfg.last_check_ts > 0 else None
        if last == today:
            return False
        if datetime.now().hour < self._cfg.check_hour:
            return False
        if self._cfg.check_interval == "weekly":
            return today.weekday() == self._cfg.check_day
        return True  # daily

    def _schedule_check(self) -> bool:
        # Same as _schedule_backup: skip silently when a check is already queued,
        # so the 60s tick doesn't re-log "triggered" + "already queued" each time.
        # Fix #3: a failed preflight or a Stop All pauses re-triggers too.
        if self._backoff_active("check"):
            return True
        if not self._is_queued("check") and self._check_due():
            self._log("Scheduled check — running restic check.")
            self._trigger_check(automatic=True)
        return True  # keep repeating

    # --- settings ---

    def _open_settings(self) -> None:
        dlg = SettingsDialog(None, self._cfg, on_log=self._log)
        while True:
            response = dlg.run()
            if response != Gtk.ResponseType.OK:
                break
            try:
                edited = dlg.collect()
                with self._cfg_lock:
                    # Settings from the dialog; operation bookkeeping (last run
                    # times, subset rotation, sizes) from the live config as it
                    # is NOW, not as it was when the dialog opened. merge_settings
                    # also files the outgoing bookkeeping under the identity it
                    # describes and rebinds the rest to the new identity (fix #1).
                    new_cfg = cfg_mod.merge_settings(self._cfg, edited)
                    cfg_mod.save_config(new_cfg)
                    self._cfg = new_cfg
            except cfg_mod.ConfigError as exc:
                # Keep the dialog open so the user can fix the invalid values;
                # the live config is NOT replaced and nothing is saved.
                err = Gtk.MessageDialog(
                    transient_for=dlg,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Settings are invalid — nothing was saved.",
                )
                err.format_secondary_text(str(exc))
                err.run()
                err.destroy()
                continue
            self._log("Settings saved.")
            # Fix #1: whatever the tray was showing describes the previous
            # repository/sources. Re-derive it from the history of the
            # configuration the user has now selected.
            self._refresh_bookkeeping_display()
            break
        dlg.destroy()

    def _state_from_bookkeeping(self) -> tuple[BackupState, str]:
        """Tray state implied by the bookkeeping of the selected configuration.

        Fix #9 (restic-box-fixes2.md): the *last attempt* and the *last success*
        are different facts, and the old version conflated them — an operation that
        was skipped by the connection preflight recorded nothing, so the previous
        success kept wearing a green light as though it were the result of the
        attempt that just failed to happen.

        One status line can carry one fact, so it reports the most recent thing
        that went *wrong* (a scheduled backup or check that failed, was skipped, or
        was stopped), and only falls back to the routine backup status when nothing
        is outstanding. Whatever it reports also names the last time that operation
        genuinely succeeded, since fix #9 makes sure that timestamp survives the
        failure.
        """
        cfg = self._cfg
        attempts = []
        for kind, outcome in (("backup", cfg_mod.backup_outcome(cfg)),
                              ("check", cfg_mod.check_outcome(cfg))):
            status, attempt_ts, ok_ts, reason = outcome
            if status:
                attempts.append((attempt_ts, kind, status, ok_ts, reason))
        if not attempts:
            return BackupState.IDLE, "Never backed up"

        problems = [a for a in attempts if a[2] != cfg_mod.OUTCOME_SUCCESS]
        if problems:
            # Failures, skips and stops are news; a routine check success must not
            # push the backup status the tray exists to show.
            attempt_ts, kind, status, ok_ts, reason = max(problems)
        else:
            attempt_ts, kind, status, ok_ts, reason = next(
                (a for a in attempts if a[1] == "backup"), max(attempts))

        label = _OP_LABELS[kind]
        stamp = datetime.fromtimestamp(attempt_ts).strftime("%Y-%m-%d %H:%M")
        why = f": {reason}" if reason else ""
        previous = ""
        if status != cfg_mod.OUTCOME_SUCCESS and 0 < ok_ts < attempt_ts:
            last_ok = datetime.fromtimestamp(ok_ts).strftime("%Y-%m-%d %H:%M")
            previous = f" (last successful {label.lower()} {last_ok})"

        if status == cfg_mod.OUTCOME_SUCCESS:
            return BackupState.SUCCESS, f"Last {label.lower()} OK at {stamp}"
        if status == cfg_mod.OUTCOME_PARTIAL:
            return BackupState.PARTIAL, (f"INCOMPLETE at {stamp}"
                                         f" — some files were unreadable{previous}")
        if status == cfg_mod.OUTCOME_FAILED:
            return BackupState.ERROR, f"{label} FAILED at {stamp}{why}{previous}"
        if status == cfg_mod.OUTCOME_SKIPPED:
            return BackupState.ERROR, f"{label} skipped at {stamp}{why}{previous}"
        return BackupState.IDLE, f"{label} stopped at {stamp}{previous}"

    def _refresh_bookkeeping_display(self) -> None:
        """Re-show the state belonging to the currently selected configuration.

        Skipped while an operation owns the repository: that one is mid-flight
        and its own state messages are the accurate ones right now.
        """
        if self._operation_active():
            return
        self._set_state(*self._state_from_bookkeeping())

    def _record_attempt(self, op: str, cfg: cfg_mod.Config, status: str,
                        reason: str = "", *, persist: bool = True) -> None:
        """Write down an operation that was attempted but never reached restic.

        Fix #9 (restic-box-fixes2.md): a scheduled backup skipped by the connection
        preflight, stopped before anything launched, or blocked by a busy repository
        used to leave no record at all, and the tray then had nothing honest left to
        show. The attempt is filed under the identity it was meant for (fix #1's
        rule still applies — repository A's failed attempt says nothing about
        repository B) and the live status fields follow, so the warning survives a
        settings refresh and a restart.

        ``persist=False`` for a cancellation: stopping an operation is the user's
        decision, not an outcome of the repository, and a run stopped midway already
        promises to write no bookkeeping (see _run_backup). Showing it is worth it;
        recording it as though it were a repository result is not.
        """
        identity = cfg_mod.config_identity(cfg)
        op = _OP_RECORD_KEYS.get(op, op)
        with self._cfg_lock:
            self._cfg = cfg_mod.record_attempt(self._cfg, identity, op=op, ts=time.time(),
                                               status=status, reason=reason)
            if persist:
                self._save_cfg()

    def _note_stopped(self, cfg: cfg_mod.Config, reason: str = "stopped by user") -> None:
        """Label an interrupted run as cancelled — fix #9's fifth outcome.

        In memory only (see _record_attempt): a run the user stopped is not an
        outcome of the repository, and the existing promise is that a stopped run
        writes no bookkeeping. It is still distinguished on the status line, which
        is what the review asks for: Stopped is not Success, and the last backup
        that actually completed keeps its own timestamp.
        """
        self._record_attempt("backup", cfg, cfg_mod.OUTCOME_CANCELLED, reason,
                             persist=False)

    def _show_attempt(self) -> None:
        """Put the just-recorded attempt on the status line.

        One source for the wording: the tray says exactly what the bookkeeping now
        says, so the live warning and the one that appears after a restart cannot
        drift apart.
        """
        GLib.idle_add(self._set_state, *self._state_from_bookkeeping())

    def _merge_backup_result(self, identity: str, **outcome) -> None:
        """Credit a finished backup to the identity (repository + sources) that
        produced it, under the config lock.

        Fix #1 (restic-box-fixes2.md): the operation's results used to land on
        whatever the live config happened to describe when the run finished, so a
        backup of repository A could mark repository B as backed up — and the
        scheduler would then wait a full interval before touching B for real.
        The outcome is always filed under the run's own identity; it becomes the
        live bookkeeping only while that identity is still the selected one.
        """
        with self._cfg_lock:
            stale = identity != cfg_mod.config_identity(self._cfg)
            self._cfg = cfg_mod.record_backup(self._cfg, identity, **outcome)
            self._save_cfg()
        if stale:
            self._log("Settings changed during this run: its result was recorded "
                      "for the repository and sources it used, not the ones now selected.")

    def _merge_check_result(self, identity: str, **outcome) -> None:
        """Same rule as _merge_backup_result, for integrity checks (fix #1 asks
        for both): which repository was verified, and how far its subset
        rotation got, belong to that repository."""
        with self._cfg_lock:
            stale = identity != cfg_mod.config_identity(self._cfg)
            self._cfg = cfg_mod.record_check(self._cfg, identity, **outcome)
            self._save_cfg()
        if stale:
            self._log("Settings changed during this check: its result was recorded "
                      "for the repository it verified, not the one now selected.")

    def _merge_dir_sizes(self, op_dirs: list) -> None:
        """Carry size bookkeeping from the operation's config snapshot back to
        the live config's directories, matched by path (split units roll up to
        their origin Directory). No save here: on a completed run the result
        merge follows and persists the sizes together; on a run stopped midway
        the updated sizes simply wait for the next save, as before.
        """
        sizes = {d.path: d.cached_size for d in op_dirs
                 if isinstance(d, cfg_mod.Directory)}
        if not sizes:
            return
        with self._cfg_lock:
            for d in self._cfg.directories:
                new = sizes.get(d.path)
                if new is not None and new > 0 and d.cached_size != new:
                    d.cached_size = new

    def _save_cfg(self) -> None:
        """Worker-side config save; a validation failure is logged, never fatal."""
        try:
            cfg_mod.save_config(self._cfg)
        except cfg_mod.ConfigError as exc:
            self._log(f"ERROR: could not save configuration: {exc}")

    # --- start / quit ---

    def start(self) -> None:
        threading.Thread(target=self._cleanup_stale_mount, daemon=True).start()
        GLib.timeout_add_seconds(60, self._schedule_backup)
        GLib.timeout_add_seconds(60, self._schedule_check)
        if self._cfg.backup_on_startup:
            self._trigger_backup(automatic=True)
        elif self._backup_due():
            self._log("Backup overdue — running.")
            self._trigger_backup(automatic=True)
        if self._check_due():
            self._log("Check overdue — running restic check.")
            self._trigger_check(automatic=True)
        self._log("restic-box started.")

    def _set_current_proc(self, proc: subprocess.Popen) -> None:
        # Register every launched process (fix #4): a multi-unit backup
        # overwrites the single _current_proc slot, and browser forget/prune
        # launch on their own threads — Stop All and quit use the registry to
        # reach whichever process is actually alive.
        self._current_proc = proc
        with self._repo_lock:
            self._active_procs = [p for p in self._active_procs if p.poll() is None]
            self._active_procs.append(proc)

    def _quit(self, _source: object) -> None:
        # Same shared cancellation token as Stop All (issue #6): the worker and
        # any planning in progress abort rather than launching the next unit.
        self._stop_requested.set()
        self._clear_queue()
        self._op_queue.put(None)  # stop worker thread
        if self._mount_proc and self._mount_proc.poll() is None:
            runner.stop_process(self._mount_proc)  # bounded terminate→wait→kill
            subprocess.run(["fusermount3", "-u", str(_RAW_MOUNT_PATH)], capture_output=True)
        for proc in self._live_procs():  # fix #4: every live process, not just the newest
            self._log("Terminating running restic process…")
            runner.stop_process(proc)
        Gtk.main_quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    # A corrupt configuration is reported, never silently replaced: continuing
    # with defaults could later save defaults OVER the user's broken-but-real
    # settings and refresh the .bak copy with them, destroying everything.
    try:
        cfg = cfg_mod.load_config()
    except cfg_mod.ConfigError as exc:
        print(f"restic-box: {exc}", file=sys.stderr)
        try:
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="restic-box could not read its configuration",
            )
            dlg.format_secondary_text(str(exc))
            dlg.run()
            dlg.destroy()
        except Exception:
            pass
        sys.exit(1)
    indicator = ResticIndicator(cfg)
    # Older versions appended a fixed SSH alias to ~/.ssh/config that could
    # never be updated; commands now carry the current settings explicitly, so
    # comment the stale alias out once — the entry stays in the file for the
    # user to re-enable or delete themselves. Cleanup must never be fatal: an
    # unreadable or hand-edited ssh config is not a reason to fail to start.
    try:
        cleaned = runner.disable_legacy_ssh_alias()
    except Exception as exc:
        print(f"restic-box: could not disable the outdated SSH alias: {exc}",
              file=sys.stderr, flush=True)
        cleaned = False
    if cleaned:
        print("restic-box: commented out the outdated restic-storagebox SSH "
              "alias in ~/.ssh/config (it is kept there for reference; "
              "settings are now applied directly).",
              flush=True)
    indicator.start()
    Gtk.main()


if __name__ == "__main__":
    main()
