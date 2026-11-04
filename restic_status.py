"""Status and log windows for restic-box."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Log window
# ---------------------------------------------------------------------------
class LogWindow(Gtk.Window):
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
        buf.insert(buf.get_end_iter(), text + "\n")
        if self._pinned:
            GLib.idle_add(self._scroll_to_bottom)


