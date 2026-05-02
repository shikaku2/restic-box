"""Status and log windows for restic-box."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore[attr-defined]


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

    def append(self, text: str) -> None:
        buf = self._view.get_buffer()
        adj = self._view.get_vadjustment()
        at_bottom = adj and (adj.get_value() >= adj.get_upper() - adj.get_page_size() - 1)
        buf.insert(buf.get_end_iter(), text + "\n")
        if at_bottom and adj:
            adj.set_value(adj.get_upper())


