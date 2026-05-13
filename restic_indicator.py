"""restic-box — GTK3 AppIndicator tray applet for restic backups over SSH."""

from __future__ import annotations

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
from collections import defaultdict
from datetime import date, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Callable

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import AppIndicator3, GLib, Gio, Gtk  # type: ignore[attr-defined]

import restic_config as cfg_mod
import restic_runner as runner
from restic_settings import SettingsDialog
from restic_status import LogWindow


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


def _set_tree_readonly(path: Path, readonly: bool) -> None:
    """Recursively chmod directories under path to 0o555 (readonly) or 0o755."""
    if not path.exists():
        return
    mode = 0o555 if readonly else 0o755
    for root, dirs, _ in os.walk(str(path)):
        os.chmod(root, mode)


class BackupState(Enum):
    IDLE = auto()
    RUNNING = auto()
    SUCCESS = auto()
    ERROR = auto()
    CHECKING = auto()


_STATE_COLORS: dict[BackupState, tuple[float, float, float]] = {
    BackupState.IDLE: (0.50, 0.50, 0.50),
    BackupState.RUNNING: (0.20, 0.55, 1.00),
    BackupState.SUCCESS: (0.18, 0.78, 0.30),
    BackupState.ERROR: (0.90, 0.20, 0.18),
    BackupState.CHECKING: (1.00, 0.62, 0.00),
}


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

    def __init__(self, cfg: cfg_mod.Config) -> None:
        super().__init__(title="restic-box — Snapshot Browser")
        self._cfg = cfg
        self._nodes: dict[int, dict] = {}  # node_id → children dict
        self._nid = 0
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
        self._snap_store.clear()
        self._snap_lbl.set_text("Loading…")
        threading.Thread(target=self._do_load_snaps, daemon=True).start()

    def _do_load_snaps(self) -> None:
        try:
            snaps = runner.run_snapshots_json(self._cfg)
            GLib.idle_add(self._fill_snapshots, snaps)
        except Exception as e:
            traceback.print_exc()
            GLib.idle_add(self._snap_lbl.set_text, f"Error: {e}")

    def _fill_snapshots(self, snaps: list[dict]) -> None:
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
        if not it:
            return
        snap_id = model.get_value(it, 0)
        self._file_store.clear()
        self._nodes.clear()
        self._nid = 0
        self._file_lbl.set_text("Loading files…")
        threading.Thread(target=self._do_load_files, args=(snap_id,), daemon=True).start()

    # ── file tree ───────────────────────────────────────────────────────────

    def _do_load_files(self, snap_id: str) -> None:
        entries = runner.run_ls_json(self._cfg, snap_id)
        GLib.idle_add(self._fill_files, entries)

    def _fill_files(self, entries: list[dict]) -> None:
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

    def _on_forget(self, _btn: Gtk.Button) -> None:
        model, it = self._snap_sel.get_selected()
        if not it:
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

    def _do_forget(self, snap_id: str) -> None:
        rc, _out = runner.run_forget(self._cfg, snap_id, prune=True)
        GLib.idle_add(self._after_forget, rc)

    def _after_forget(self, rc: int) -> None:
        if rc == runner.RC_OK:
            self._file_store.clear()
            self._file_lbl.set_text("Snapshot forgotten.")
            self._load_snapshots()
        else:
            self._snap_lbl.set_text(f"Forget failed (rc={rc})")

    def _on_prune_all(self, _btn: Gtk.Button) -> None:
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

    def _do_prune(self) -> None:
        rc, _out = runner.run_prune(self._cfg)
        GLib.idle_add(
            self._snap_lbl.set_text,
            "Prune complete." if rc == runner.RC_OK else f"Prune failed (rc={rc})",
        )


# ---------------------------------------------------------------------------
# Main indicator
# ---------------------------------------------------------------------------
class ResticIndicator:
    def __init__(self) -> None:
        self._cfg = cfg_mod.load_config()
        if self._cfg.last_backup_ts > 0:
            ts = datetime.fromtimestamp(self._cfg.last_backup_ts).strftime("%Y-%m-%d %H:%M")
            self._state = BackupState.SUCCESS if self._cfg.last_backup_ok else BackupState.ERROR
            self._last_msg = f"Last backup OK at {ts}" if self._cfg.last_backup_ok else f"FAILED at {ts}"
        else:
            self._state = BackupState.IDLE
            self._last_msg = "Never backed up"
        self._op_queue: queue.Queue[tuple[str, bool] | None] = queue.Queue()
        self._queued: set[str] = set()
        self._queue_lock = threading.Lock()
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
        self._mount_proc: subprocess.Popen | None = None
        self._mount_spinner_id: int | None = None
        self._mount_frame: int = 0
        self._stop_requested = threading.Event()

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

    def _backup_progress_msg(self, pct: float, done: float, total: float) -> tuple[str, float]:
        total_pct = self._weighted_total_pct(pct)
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
        self._current_backup_done = float(done)
        self._current_backup_total = float(total)

        now = time.monotonic()
        if now - self._last_progress_ts < 1.0:
            return
        self._last_progress_ts = now

        msg, total_pct = self._backup_progress_msg(pct, done, total)
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
        GLib.idle_add(self._log_window.append, f"[{ts}] {text}")

    def _show_log(self) -> None:
        self._log_window.show_all()
        self._log_window.present()

    def _open_snapshot_browser(self) -> None:
        self._snap_browser = SnapshotBrowser(self._cfg)
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

    def _op_worker(self) -> None:
        while True:
            item = self._op_queue.get()
            if item is None:
                break
            op, automatic = item
            try:
                self._stop_requested.clear()
                if not runner.is_host_reachable(self._cfg):
                    self._log(f"Skipping {op} — host {self._cfg.ssh_host} unreachable.")
                    continue
                if op == "backup":
                    self._run_backup(automatic)
                elif op == "check":
                    self._run_check(automatic)
                elif op == "check_full":
                    self._run_full_check(automatic)
                elif op == "unlock":
                    self._run_unlock()
            except Exception:
                traceback.print_exc()
            finally:
                with self._queue_lock:
                    self._queued.discard(op)

    def _trigger_backup(self, automatic: bool = False) -> None:
        self._enqueue("backup", automatic)

    def _mount_active(self) -> bool:
        return bool(self._mount_proc and self._mount_proc.poll() is None)

    def _skip_if_mounted(self, op: str, automatic: bool) -> bool:
        if not self._mount_active():
            return False
        if automatic:
            self._log(f"Skipped {op} — mount is active.")
        else:
            _notify(f"Cannot {op} while mount is active. Unmount first.")
        return True

    def _run_backup(self, automatic: bool = False) -> None:
        if self._skip_if_mounted("backup", automatic):
            return
        inhibit_fd = _inhibit_sleep()
        try:
            dirs = [d for d in self._cfg.directories if d.enabled]
            if not dirs:
                GLib.idle_add(self._set_state, BackupState.IDLE, "No directories configured")
                self._log("No enabled directories to back up.")
                return

            self._log("=== Backup started ===")
            all_ok = True
            run_total_size = 0.0
            self._last_progress_ts = 0.0
            self._backup_dirs = dirs
            self._backup_item_count = len(dirs)
            msg, total_pct = self._backup_progress_msg(0.0, 0.0, 0.0)
            GLib.idle_add(self._set_state, BackupState.RUNNING, msg, total_pct)
            for index, d in enumerate(dirs):
                self._backup_item_index = index
                self._log(f"Backing up: {d.path}")
                self._current_backup_done = 0.0
                self._current_backup_total = 0.0
                msg, total_pct = self._backup_progress_msg(0.0, 0.0, 0.0)
                GLib.idle_add(self._set_state, BackupState.RUNNING, msg, total_pct)
                rc, _out = runner.run_backup(
                    self._cfg, d,
                    on_output=self._log,
                    on_progress=self._on_progress,
                    on_proc=self._set_current_proc,
                )
                if rc == runner.RC_PARTIAL:
                    self._log(f"WARNING: some files in {d.path} were unreadable and skipped")
                elif rc != runner.RC_OK:
                    all_ok = False
                    self._log(f"ERROR: backup of {d.path} failed (rc={rc})")
                if rc in (runner.RC_OK, runner.RC_PARTIAL):
                    run_total_size += self._current_backup_total
                    if self._current_backup_total > 0:
                        d.cached_size = self._current_backup_total
                if self._stop_requested.is_set():
                    self._log("Backup stopped by user — remaining directories skipped.")
                    GLib.idle_add(self._set_state, BackupState.IDLE, "Stopped")
                    return

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            if all_ok:
                self._log(f"=== Backup complete at {ts} ===")
                ret_dur = runner.retention_duration(self._cfg)
                if self._cfg.retention_enabled and ret_dur:
                    self._log(f"=== Applying retention (--keep-within {ret_dur}) ===")
                    rc_r, _out = runner.run_retention_forget(self._cfg, on_output=self._log)
                    if rc_r not in (runner.RC_OK, runner.RC_PARTIAL):
                        self._log(f"WARNING: retention forget failed (rc={rc_r})")
            else:
                GLib.idle_add(self._set_state, BackupState.ERROR, f"Failed at {ts}")
                self._log(f"=== Backup FAILED at {ts} ===")
            compressed_backup_size = self._cfg.compressed_backup_size
            if all_ok:
                latest_compressed_size = runner.run_raw_data_size(self._cfg)
                if latest_compressed_size is not None:
                    compressed_backup_size = latest_compressed_size
            self._cfg = dataclasses.replace(
                self._cfg,
                last_backup_ts=time.time(),
                last_backup_ok=all_ok,
                total_backup_size=run_total_size if all_ok else self._cfg.total_backup_size,
                compressed_backup_size=compressed_backup_size,
            )
            cfg_mod.save_config(self._cfg)
            if all_ok:
                GLib.idle_add(self._set_state, BackupState.SUCCESS, f"Backup complete at {ts}")
        finally:
            self._backup_item_index = 0
            self._backup_item_count = 0
            self._backup_dirs = []
            if inhibit_fd is not None:
                _release_inhibit(inhibit_fd)

    # --- check ---

    def _trigger_check(self, automatic: bool = False) -> None:
        self._enqueue("check", automatic)

    def _run_check(self, automatic: bool = False) -> None:
        if self._skip_if_mounted("check", automatic):
            return
        GLib.idle_add(self._set_state, BackupState.CHECKING, "Checking repo…")
        if (self._cfg.check_read_data_subset
                and self._cfg.check_subset_current > self._cfg.check_subset_total):
            self._cfg = dataclasses.replace(self._cfg, check_subset_current=1)
            cfg_mod.save_config(self._cfg)
            self._log("Subset index reset to 1 (total was reduced).")
        subset_info = ""
        if self._cfg.check_read_data_subset:
            n, t = self._cfg.check_subset_current, self._cfg.check_subset_total
            subset_info = f" (subset {n}/{t})"
        self._log(f"=== restic check started{subset_info} ===")
        rc, _out = runner.run_check(self._cfg, on_output=self._log, on_proc=self._set_current_proc)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if rc in (runner.RC_OK, runner.RC_PARTIAL):
            GLib.idle_add(self._set_state, BackupState.SUCCESS, f"Check OK at {ts}{subset_info}")
            if self._cfg.check_read_data_subset:
                n = self._cfg.check_subset_current % self._cfg.check_subset_total + 1
            else:
                n = self._cfg.check_subset_current
            self._cfg = dataclasses.replace(self._cfg, check_subset_current=n, last_check_ts=time.time())
            cfg_mod.save_config(self._cfg)
        else:
            GLib.idle_add(self._set_state, BackupState.ERROR, f"Check FAILED at {ts}")
        self._log(f"=== restic check finished (rc={rc}) at {ts} ===")

    def _trigger_full_check(self, automatic: bool = False) -> None:
        self._enqueue("check_full", automatic)

    def _run_full_check(self, automatic: bool = False) -> None:
        if self._skip_if_mounted("full check", automatic):
            return
        GLib.idle_add(self._set_state, BackupState.CHECKING, "Checking repo (all data)…")
        self._log("=== restic check --read-data started ===")
        rc, _out = runner.run_check_full(self._cfg, on_output=self._log, on_proc=self._set_current_proc)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if rc in (runner.RC_OK, runner.RC_PARTIAL):
            GLib.idle_add(self._set_state, BackupState.SUCCESS, f"Full check OK at {ts}")
            self._cfg = dataclasses.replace(self._cfg, check_subset_current=1)
            cfg_mod.save_config(self._cfg)
        else:
            GLib.idle_add(self._set_state, BackupState.ERROR, f"Full check FAILED at {ts}")
        self._log(f"=== restic check --read-data finished (rc={rc}) at {ts} ===")

    # --- unlock / stop all ---

    def _stop_and_unlock(self) -> None:
        self._stop_requested.set()
        self._clear_queue()
        if self._current_proc and self._current_proc.poll() is None:
            self._log("Stopping current restic process…")
            self._current_proc.terminate()
        self._enqueue("unlock")

    def _run_unlock(self) -> None:
        GLib.idle_add(self._set_state, BackupState.CHECKING, "Unlocking repo…")
        self._log("=== restic unlock ===")
        rc, _out = runner.run_unlock(self._cfg, on_output=self._log, on_proc=self._set_current_proc)
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
        _set_tree_readonly(_LATEST_PATH, False)
        shutil.rmtree(_LATEST_PATH, ignore_errors=True)
        _set_tree_readonly(_BYDATE_PATH, False)
        shutil.rmtree(_BYDATE_PATH, ignore_errors=True)

    def _mount_backup(self) -> None:
        if self._current_proc and self._current_proc.poll() is None:
            _notify("Can't mount yet — backup/check in progress. Stop it first to allow mounts.")
            return
        if not shutil.which("fusermount3"):
            _notify("fuse3 is not installed — install it to use the mount feature")
            return
        self._cleanup_stale_mount()
        _RAW_MOUNT_PATH.mkdir(parents=True, exist_ok=True)
        self._mount_frame = 0
        self._item_mount.set_label(_MOUNT_FRAMES[0])
        self._item_mount.set_sensitive(False)
        self._mount_spinner_id = GLib.timeout_add(500, self._tick_mount_label)
        threading.Thread(target=self._run_mount, daemon=True).start()

    def _tick_mount_label(self) -> bool:
        self._mount_frame = (self._mount_frame + 1) % len(_MOUNT_FRAMES)
        self._item_mount.set_label(_MOUNT_FRAMES[self._mount_frame])
        return True

    def _stop_mount_spinner(self) -> None:
        if self._mount_spinner_id is not None:
            GLib.source_remove(self._mount_spinner_id)
            self._mount_spinner_id = None

    def _run_mount(self) -> None:
        try:
            self._mount_proc = runner.start_mount(self._cfg, str(_RAW_MOUNT_PATH))
            assert self._mount_proc.stdout is not None
            for line in self._mount_proc.stdout:
                line = line.rstrip()
                if line:
                    self._log(f"[mount] {line}")
                    if "serving" in line.lower():
                        self._build_friendly_views()
                        GLib.idle_add(self._stop_mount_spinner)
                        GLib.idle_add(self._item_mount.set_label, "Unmount Backup")
                        GLib.idle_add(self._item_mount.set_sensitive, True)
                        subprocess.Popen(["xdg-open", str(_MOUNT_DIR)])
                        self._log(f"Mounted at {_MOUNT_DIR}")
            rc = self._mount_proc.wait()
            if rc != 0:
                self._log(f"Mount exited with rc={rc}")
        except Exception as e:
            self._log(f"Mount failed: {e}")
        finally:
            self._mount_proc = None
            self._cleanup_stale_mount()
            GLib.idle_add(self._stop_mount_spinner)
            GLib.idle_add(self._item_mount.set_label, "Mount Backup")
            GLib.idle_add(self._item_mount.set_sensitive, True)
            self._log("Unmounted.")

    def _unmount_backup(self) -> None:
        if self._mount_proc and self._mount_proc.poll() is None:
            self._mount_proc.terminate()
        subprocess.run(["fusermount3", "-u", str(_RAW_MOUNT_PATH)], capture_output=True)

    def _build_friendly_views(self) -> None:
        try:
            snaps = runner.run_snapshots_json(self._cfg)
            self._build_latest(snaps)
            self._build_bydate(snaps)
        except Exception as e:
            self._log(f"[mount] Could not build friendly views: {e}")

    def _build_latest(self, snaps: list[dict]) -> None:
        _set_tree_readonly(_LATEST_PATH, False)
        shutil.rmtree(_LATEST_PATH, ignore_errors=True)
        _LATEST_PATH.mkdir(parents=True)
        sorted_snaps = sorted(snaps, key=lambda s: s.get("time", ""))
        covered: dict[str, str] = {}  # orig_path → snap_id (newest wins)
        for snap in sorted_snaps:
            for path in snap.get("paths") or []:
                covered[path] = snap.get("id", "")[:8]  # FUSE ids/ uses 8-char short IDs
        for orig_path, snap_id in covered.items():
            rel = orig_path.lstrip("/")
            src = _RAW_MOUNT_PATH / "ids" / snap_id / rel
            dst = _LATEST_PATH / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
        _set_tree_readonly(_LATEST_PATH, True)

    def _build_bydate(self, snaps: list[dict]) -> None:
        _set_tree_readonly(_BYDATE_PATH, False)
        shutil.rmtree(_BYDATE_PATH, ignore_errors=True)
        _BYDATE_PATH.mkdir(parents=True)
        minute_groups: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(dict)
        for snap in snaps:
            t = snap.get("time", "")
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone()
                date = dt.strftime("%Y-%m-%d")
                time_key = dt.strftime("%H-%M")
            except Exception:
                date = t[:10]
                time_key = t[11:16].replace(":", "-")
            entry = minute_groups[date].setdefault(time_key, [])
            for path in snap.get("paths") or []:
                entry.append((path, snap.get("id", "")[:8]))
        # Build folder structure: single run → bydate/<date>/paths
        #                         multiple runs → bydate/<date>/<HH-MM>/paths
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
                    base_dir = _BYDATE_PATH / date / subfolder
                else:
                    base_dir = _BYDATE_PATH / date
                base_dir.mkdir(parents=True, exist_ok=True)
                for orig_path, snap_id in runs[time_key]:
                    rel = orig_path.lstrip("/")
                    src = _RAW_MOUNT_PATH / "ids" / snap_id / rel
                    dst = base_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        dst.symlink_to(src)
        _set_tree_readonly(_BYDATE_PATH, True)

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

    def _schedule_backup(self) -> bool:
        if self._backup_due():
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
        if self._check_due():
            self._log("Scheduled check — running restic check.")
            self._trigger_check(automatic=True)
        return True  # keep repeating

    # --- settings ---

    def _open_settings(self) -> None:
        dlg = SettingsDialog(None, self._cfg)
        response = dlg.run()
        if response == Gtk.ResponseType.OK:
            self._cfg = dlg.collect()
            cfg_mod.save_config(self._cfg)
            self._log("Settings saved.")
        dlg.destroy()

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
        self._current_proc = proc

    def _quit(self, _source: object) -> None:
        self._clear_queue()
        self._op_queue.put(None)  # stop worker thread
        if self._mount_proc and self._mount_proc.poll() is None:
            self._mount_proc.terminate()
            subprocess.run(["fusermount3", "-u", str(_RAW_MOUNT_PATH)], capture_output=True)
        if self._current_proc and self._current_proc.poll() is None:
            self._log("Terminating running restic process…")
            self._current_proc.terminate()
            try:
                self._current_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._current_proc.kill()
        Gtk.main_quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    indicator = ResticIndicator()
    indicator.start()
    Gtk.main()


if __name__ == "__main__":
    main()
