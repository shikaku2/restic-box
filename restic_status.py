"""Status and log windows for restic-box."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore[attr-defined]

import restic_diag_log as diag_log


# ---------------------------------------------------------------------------
# Log window
# ---------------------------------------------------------------------------
class LogWindow(Gtk.Window):
    # Issue #14: bound the visible log. A days-long backup streams one line
    # per progress event; without a cap the TextView buffer grows forever.
    MAX_LOG_LINES = 2000
    # Review fix #6: the line COUNT alone is not a bound — one enormous line
    # (restic facing a corrupted or binary-polluted stream) would freeze the
    # TextView whatever the line count says. Each appended line is excerpted to
    # this many characters, and the full record is available in the diagnostic
    # log file (restic_diag_log), which the excerpt points at.
    MAX_LOG_LINE_CHARS = 4096
    TRIM_MARKER = "[restic-box: older log lines trimmed]\n"

    def __init__(self) -> None:
        super().__init__(title="restic-box log")
        self.set_default_size(700, 400)
        self.connect("delete-event", lambda w, e: w.hide() or True)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._view = Gtk.TextView()
        self._view.set_editable(False)
        self._view.set_monospace(True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.add(self._view)
        self.add(scroll)

        # Pinned means the user is at (or has left us at) the bottom. It follows
        # the actual scroll position, so scrolling up stops autoscrolling and
        # scrolling back down re-pins.
        self._pinned = True
        self._marker_dropped = False
        self._clipped = 0
        adj = self._view.get_vadjustment()
        if adj is not None:
            adj.connect("value-changed", self._on_scroll)

    def _at_bottom(self, adj) -> bool:
        return adj.get_value() >= adj.get_upper() - adj.get_page_size() - 1

    def _on_scroll(self, adj) -> None:
        # A programmatic scroll to the bottom re-pins here too (value == bottom).
        self._pinned = self._at_bottom(adj)

    def _scroll_to_bottom(self) -> bool:
        adj = self._view.get_vadjustment()
        if adj is not None:
            # Settle the layout first: get_upper() reflects the just-inserted text
            # only after the next size-allocate, so scroll on the idle tick.
            adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def append(self, text: str) -> None:
        buf = self._view.get_buffer()
        buf.insert(buf.get_end_iter(), self._clip(text) + "\n")
        self._trim_buffer(buf)
        if self._pinned:
            GLib.idle_add(self._scroll_to_bottom)

    def _clip(self, text: str) -> str:
        """Excerpt every physical line of a record to MAX_LOG_LINE_CHARS (fix #6).

        Records normally arrive as single lines from ResticIndicator._log, but a
        captured stdout dump can be multi-line: bounding each physical line keeps
        the buffer bounded by lines × characters instead of lines alone. The
        excerpt states how much is missing and names the diagnostic log file
        that holds the full record.
        """
        limit = self.MAX_LOG_LINE_CHARS
        out = []
        for line in text.split("\n"):
            if len(line) <= limit:
                out.append(line)
                continue
            self._clipped += 1
            where = str(diag_log.log_dir() / diag_log.LOG_FILE_NAME)
            note = diag_log.truncation_note(len(line) - (limit - diag_log.NOTE_RESERVE),
                                            where)
            out.append(line[: limit - len(note)] + note)
        return "\n".join(out)

    def _trim_buffer(self, buf) -> None:
        """Drop the oldest lines once the buffer passes MAX_LOG_LINES (issue #14).

        The newest line (just appended) always survives; once trimming starts a
        single marker line stays at the top so the user can tell history was
        dropped. The marker is not one of the MAX_LOG_LINES content lines.
        """
        limit = self.MAX_LOG_LINES + (1 if self._marker_dropped else 0)
        overflow = buf.get_line_count() - limit
        if overflow <= 0:
            return
        first_content = 1 if self._marker_dropped else 0
        last = buf.get_line_count() - 1  # the line we just added must survive
        cut = min(first_content + overflow, last)
        if cut > first_content:
            buf.delete(buf.get_iter_at_line(first_content), buf.get_iter_at_line(cut))
        if not self._marker_dropped:
            self._marker_dropped = True
            buf.insert(buf.get_start_iter(), self.TRIM_MARKER)


