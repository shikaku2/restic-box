"""Snapshot-browser stale-response tests (issue #12).

Selecting snapshot A then B starts two listings. If A finishes last it must
NOT overwrite B's tree. The same generation guard covers snapshot-list
refreshes and cleared selections: only the response matching the current
generation may touch the UI.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_indicator  # noqa: E402


class FakeStore:
    """Stands in for Gtk.ListStore / Gtk.TreeStore append/clear."""

    def __init__(self):
        self.rows = []

    def clear(self):
        self.rows.clear()

    def append(self, parent, values=None):
        if values is None:  # ListStore-style append(values)
            parent, values = None, parent
        self.rows.append(values)
        return f"iter-{len(self.rows)}"  # any opaque TreeIter stand-in


class FakeLabel:
    def __init__(self, text=""):
        self.text = text

    def set_text(self, t):
        self.text = t


class FakeButton:
    def __init__(self):
        self.sensitive = True

    def set_sensitive(self, v):
        self.sensitive = v


class FakeModel:
    def __init__(self, snap_id):
        self._snap_id = snap_id

    def get_value(self, it, col):
        return self._snap_id


class CapturedThread:
    """Thread stand-in: records the work instead of running it."""

    def __init__(self, target, args, kwargs):
        self.target, self.args, self.kwargs = target, args, kwargs

    def start(self):
        pass  # captured: the test invokes target manually in chosen order


class FakeSelection:
    def __init__(self, snap_id):
        self._snap_id = snap_id

    def get_selected(self):
        if self._snap_id is None:
            return None, None
        return FakeModel(self._snap_id), "iter-0"


def make_browser():
    """SnapshotBrowser without GTK init: just the state the load paths touch."""
    browser = restic_indicator.SnapshotBrowser.__new__(restic_indicator.SnapshotBrowser)
    browser._cfg = cfg_mod.Config(backend="local", repo_path="/repo-x")
    browser._snap_store = FakeStore()
    browser._file_store = FakeStore()
    browser._snap_lbl = FakeLabel()
    browser._file_lbl = FakeLabel()
    browser._btn_forget = FakeButton()
    browser._nodes = {}
    browser._nid = 0
    browser._snap_gen = 0
    browser._files_gen = 0
    return browser


def entry(path):
    return {"path": path, "type": "file", "struct_type": "node", "size": 1}


def start_selection(browser, snap_id, threads):
    """Simulate a selection change, capturing (not running) the load thread."""

    def fake_thread(target=None, args=(), daemon=False, **kwargs):
        threads.append((target, args))
        return CapturedThread(target, args, kwargs)

    with mock.patch.object(restic_indicator.threading, "Thread", new=fake_thread):
        browser._on_snap_sel_changed(FakeSelection(snap_id))
    return threads[-1] if snap_id is not None else None


class StaleFilesTest(unittest.TestCase):
    def test_out_of_order_completion_shows_newest_selection(self):
        browser = make_browser()
        pending = []

        def idle_add(fn, *args, **kwargs):
            pending.append((fn, args, kwargs))

        def fake_ls(cfg, snap_id):
            return [entry(f"/from-{snap_id}.txt")]

        threads = []
        load_a = start_selection(browser, "snapA", threads)
        load_b = start_selection(browser, "snapB", threads)

        with mock.patch.object(restic_indicator.runner, "run_ls_json",
                               new=fake_ls), \
             mock.patch.object(restic_indicator.GLib, "idle_add", new=idle_add):
            load_b[0](*load_b[1])
            load_a[0](*load_a[1])
            # flush in arrival order: B's fill, then A's (stale) fill
            for fn, args, kwargs in pending:
                fn(*args, **kwargs)

        names = [row[0] for row in browser._file_store.rows]
        self.assertEqual(names, ["from-snapB.txt"])
        self.assertIn("1 entries", browser._file_lbl.text)

    def test_empty_selection_clears_tree_and_invalidates_in_flight(self):
        browser = make_browser()
        pending = []

        def idle_add(fn, *args, **kwargs):
            pending.append((fn, args, kwargs))

        threads = []
        load_a = start_selection(browser, "snapA", threads)

        # User deselects while A's listing is still running.
        def fake_thread(target=None, args=(), daemon=False, **kwargs):
            threads.append((target, args))
            return CapturedThread(target, args, kwargs)

        with mock.patch.object(restic_indicator.threading, "Thread", new=fake_thread):
            browser._on_snap_sel_changed(FakeSelection(None))

        self.assertEqual(browser._file_store.rows, [], "tree cleared on deselect")
        self.assertIn("Select a snapshot", browser._file_lbl.text)
        self.assertFalse(browser._btn_forget.sensitive)

        with mock.patch.object(restic_indicator.runner, "run_ls_json",
                               new=lambda cfg, snap_id: [entry("/from-snapA.txt")]), \
             mock.patch.object(restic_indicator.GLib, "idle_add", new=idle_add):
            load_a[0](*load_a[1])  # A completes late
        for fn, args, kwargs in pending:
            fn(*args, **kwargs)

        self.assertEqual(browser._file_store.rows, [],
                         "the stale listing must not repopulate after deselect")
        self.assertIn("Select a snapshot", browser._file_lbl.text)

    def test_ls_failure_reports_only_for_current_generation(self):
        browser = make_browser()
        pending = []

        def idle_add(fn, *args, **kwargs):
            pending.append((fn, args, kwargs))

        threads = []
        load_a = start_selection(browser, "snapA", threads)
        start_selection(browser, "snapB", threads)  # bumps generation past A

        def boom(cfg, snap_id):
            raise RuntimeError("sftp exploded")

        with mock.patch.object(restic_indicator.runner, "run_ls_json", new=boom), \
             mock.patch.object(restic_indicator.GLib, "idle_add", new=idle_add):
            load_a[0](*load_a[1])  # A's failure arrives late
        for fn, args, kwargs in pending:
            fn(*args, **kwargs)

        self.assertNotIn("sftp exploded", browser._file_lbl.text,
                         "a stale failure must not label the current selection")


class StaleSnapshotsTest(unittest.TestCase):
    def _start_refresh(self, browser, threads):
        def fake_thread(target=None, args=(), daemon=False, **kwargs):
            threads.append((target, args))
            return CapturedThread(target, args, kwargs)

        with mock.patch.object(restic_indicator.threading, "Thread", new=fake_thread):
            browser._load_snapshots()
        return threads[-1]

    def test_stale_refresh_response_is_ignored(self):
        browser = make_browser()
        pending = []

        def idle_add(fn, *args, **kwargs):
            pending.append((fn, args, kwargs))

        threads = []
        first = self._start_refresh(browser, threads)   # snap_gen == 1
        second = self._start_refresh(browser, threads)  # snap_gen == 2

        responses = iter([
            [{"id": "oldid1234", "short_id": "oldid1234",
              "time": "2026-04-30T09:00:00Z", "paths": ["/old"]}],
            [{"id": "newid1234", "short_id": "newid1234",
              "time": "2026-05-01T10:00:00Z", "paths": ["/new"]}],
        ])
        # worker 1 (the first refresh) answers with the OLD list, worker 2
        # with the new one; the old response arrives last (worst case).
        with mock.patch.object(restic_indicator.runner, "run_snapshots_json",
                               side_effect=lambda cfg: next(responses)), \
             mock.patch.object(restic_indicator.GLib, "idle_add", new=idle_add):
            first[0](*first[1])    # queues _fill_snapshots(old, gen 1)
            second[0](*second[1])  # queues _fill_snapshots(new, gen 2)
            for fn, args, kwargs in pending:
                fn(*args, **kwargs)

        ids = [row[0] for row in browser._snap_store.rows]
        self.assertEqual(ids, ["newid1234"],
                         "only the newest refresh may populate the snapshot list")

    def test_stale_error_does_not_overwrite_label(self):
        browser = make_browser()
        threads = []
        self._start_refresh(browser, threads)   # gen 1
        self._start_refresh(browser, threads)   # gen 2

        # Emulate refresh 1 failing late, after refresh 2 has started: the
        # worker queues _load_snaps_error(gen 1), but the current generation
        # is now 2.
        browser._snap_lbl.set_text("Loading…")  # what refresh 2 set
        browser._load_snaps_error(RuntimeError("repo lock"), 1)  # late error for gen 1
        self.assertNotIn("repo lock", browser._snap_lbl.text)

        # The current generation's own error still reports normally.
        browser._load_snaps_error(RuntimeError("still broken"), 2)
        self.assertIn("still broken", browser._snap_lbl.text)


if __name__ == "__main__":
    unittest.main()
