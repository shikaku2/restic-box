"""Log-window bounds tests (issue #14, review fix #6).

A days-long backup appends one line per progress event. LogWindow must trim
its oldest lines so the TextView buffer cannot grow without bound: the newest
line always survives, and exactly one marker line records that history was
dropped.

Fix #6 adds the other half of the bound: a single enormous line (restic facing
a corrupted or binary-polluted stream) must also be excerpted, because a
line-count cap alone still lets one record hang the TextView. The excerpt names
the rotating diagnostic log file that holds the full record.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_diag_log as diag_log  # noqa: E402
from tests import DIAG_LOG_DIR  # noqa: E402
import restic_status as status  # noqa: E402

_TMP = None


def setUpModule():
    """The excerpts quote the diagnostic log path, and clipping a record can
    spill it there: keep both inside a throwaway directory."""
    global _TMP
    _TMP = Path(tempfile.mkdtemp(prefix="rb-status-log-"))
    diag_log.set_log_dir(_TMP)


def tearDownModule():
    diag_log.active().close()
    # Restore the suite-wide throwaway directory, never the user's real config dir.
    diag_log.set_log_dir(DIAG_LOG_DIR)
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


class FakeIter:
    def __init__(self, buf, line):
        self.buf = buf
        self.line = line


class FakeBuffer:
    """Models GtkTextBuffer as a list of complete lines (like every line this
    window actually inserts: append() always terminates with \\n)."""

    def __init__(self):
        self.lines = []

    def get_line_count(self):
        return len(self.lines)

    def get_start_iter(self):
        return FakeIter(self, 0)

    def get_end_iter(self):
        return FakeIter(self, len(self.lines))

    def get_iter_at_line(self, n):
        return FakeIter(self, n)

    def insert(self, it, text):
        # Every insert here is one or more newline-terminated lines: append()
        # adds "\n" and TRIM_MARKER ends with "\n".
        new = [ln + "\n" for ln in text.split("\n") if ln]
        self.lines[it.line:it.line] = new

    def delete(self, start, end):
        del self.lines[start.line:end.line]

    # test helpers ---------------------------------------------------------
    def text(self):
        return "".join(self.lines)

    def stripped(self):
        return [ln.rstrip("\n") for ln in self.lines]


class FakeView:
    def __init__(self):
        self._buf = FakeBuffer()

    def get_buffer(self):
        return self._buf


def make_window():
    win = status.LogWindow.__new__(status.LogWindow)
    win._view = FakeView()
    win._pinned = False       # keep GLib out of the test
    win._marker_dropped = False
    win._clipped = 0
    return win


class LogWindowBoundTest(unittest.TestCase):
    def test_cap_constant(self):
        self.assertEqual(status.LogWindow.MAX_LOG_LINES, 2000)
        self.assertTrue(status.LogWindow.TRIM_MARKER.strip())

    def test_no_trimming_below_the_cap(self):
        win = make_window()
        for i in range(status.LogWindow.MAX_LOG_LINES):
            win.append(f"line {i}")
        buf = win._view.get_buffer()
        self.assertEqual(buf.stripped(), [f"line {i}" for i in range(status.LogWindow.MAX_LOG_LINES)])
        self.assertFalse(win._marker_dropped)

    def test_oldest_lines_trimmed_newest_survives(self):
        win = make_window()
        n = status.LogWindow.MAX_LOG_LINES
        for i in range(n + 1):
            win.append(f"line {i}")
        buf = win._view.get_buffer()
        lines = buf.stripped()

        self.assertIn(status.LogWindow.TRIM_MARKER.rstrip("\n"), lines)
        self.assertNotIn("line 0", lines, "the oldest line must be released")
        self.assertIn(f"line {n}", lines, "the just-appended line must survive")
        content = [ln for ln in lines if ln != status.LogWindow.TRIM_MARKER.rstrip("\n")]
        self.assertEqual(len(content), n, "buffer holds exactly MAX_LOG_LINES content lines")
        self.assertEqual(lines[0], status.LogWindow.TRIM_MARKER.rstrip("\n"),
                         "the marker belongs at the top")

    def test_marker_inserted_once_even_after_thousands_of_appends(self):
        win = make_window()
        marker = status.LogWindow.TRIM_MARKER.rstrip("\n")
        for i in range(status.LogWindow.MAX_LOG_LINES + 1500):
            win.append(f"[12:00:00] progress event {i}")
            if i % 500 == 0:
                lines = win._view.get_buffer().stripped()
                self.assertLessEqual(
                    sum(1 for ln in lines if ln == marker), 1,
                    "trimming must not accumulate marker lines")

        lines = win._view.get_buffer().stripped()
        self.assertEqual(sum(1 for ln in lines if ln == marker), 1)
        self.assertEqual(lines[0], marker)
        self.assertEqual(lines[-1], f"[12:00:00] progress event {status.LogWindow.MAX_LOG_LINES + 1499}")
        content = [ln for ln in lines if ln != marker]
        self.assertEqual(len(content), status.LogWindow.MAX_LOG_LINES)

    def test_trimming_follows_append_order_not_content(self):
        """Whatever is oldest gets dropped, regardless of what it says."""
        win = make_window()
        n = status.LogWindow.MAX_LOG_LINES
        win.append("IMPORTANT startup banner")
        for i in range(1, n + 3):
            win.append(f"line {i}")
        lines = win._view.get_buffer().stripped()
        self.assertNotIn("IMPORTANT startup banner", lines)
        self.assertIn(f"line {n + 2}", lines)


class LogWindowLineSizeTest(unittest.TestCase):
    """Review fix #6: a line-count cap is only half a bound.

    One record of several megabytes — restic reading a corrupted repository or a
    binary-polluted stream — used to land in the TextView whole, which is how a
    "bounded" log window still froze the app. Every physical line of every
    appended record is now excerpted to MAX_LOG_LINE_CHARS, visibly, and the
    full record goes to the rotating diagnostic log file.
    """

    def test_cap_constant(self):
        self.assertEqual(status.LogWindow.MAX_LOG_LINE_CHARS, 4096)

    def test_ordinary_lines_are_untouched(self):
        win = make_window()
        win.append("[12:00:00] === restic backup started ===")
        self.assertEqual(win._view.get_buffer().stripped(),
                         ["[12:00:00] === restic backup started ==="])
        self.assertEqual(win._clipped, 0, "clipping must not touch normal output")

    def test_one_huge_line_is_excerpted_to_the_cap(self):
        win = make_window()
        huge = "[12:00:00] restic: catastrophic failure: " + "x" * 300_000
        win.append(huge)

        lines = win._view.get_buffer().stripped()
        self.assertEqual(len(lines), 1, "one record must stay one line")
        self.assertLessEqual(len(lines[0]), status.LogWindow.MAX_LOG_LINE_CHARS,
                            "the TextView can now hold this window at all")
        self.assertTrue(lines[0].startswith("[12:00:00] restic: catastrophic failure: xxx"),
                        "the beginning of the record is what a user can act on")
        self.assertIn("more characters", lines[0], "an excerpt must announce itself")
        self.assertEqual(win._clipped, 1)

    def test_excerpt_names_the_diagnostic_log_holding_the_full_record(self):
        win = make_window()
        win.append("marker-start-" + "y" * 50_000 + "-marker-end")
        shown = win._view.get_buffer().stripped()[0]

        self.assertIn(diag_log.LOG_FILE_NAME, shown,
                      "the window must say where the untruncated text is")
        self.assertNotIn("-marker-end", shown)
        self.assertEqual(diag_log.active().errors, 0,
                         "clipping in the log window is display-only: the app writes the "
                         "same line to the diagnostic file through ResticIndicator._log "
                         "(and the runner spills oversized records; see "
                         "tests/test_runner_output.py)")

    def test_every_line_of_a_multiline_record_is_bounded(self):
        """Captured subprocess output can be multi-line: bounding only the first
        line would leave the hole wide open."""
        win = make_window()
        record = "\n".join(f"line-{i}-" + "z" * 60_000 for i in range(4))
        win.append(record)

        lines = win._view.get_buffer().stripped()
        self.assertEqual(len(lines), 4, "the record keeps its shape")
        for i, line in enumerate(lines):
            self.assertLessEqual(len(line), status.LogWindow.MAX_LOG_LINE_CHARS,
                                 f"physical line {i} of the record is still unbounded")
            self.assertIn(f"line-{i}-", line, "each line keeps its own head")
        self.assertEqual(win._clipped, 4)

    def test_total_visible_size_is_bounded_in_characters(self):
        """lines × chars is the real bound; assert the product."""
        win = make_window()
        n = status.LogWindow.MAX_LOG_LINES + 60
        for i in range(n):
            win.append(f"progress {i}: " + "w" * 50_000)

        buf = win._view.get_buffer()
        content = [ln for ln in buf.stripped()
                   if ln != status.LogWindow.TRIM_MARKER.rstrip("\n")]
        self.assertEqual(len(content), status.LogWindow.MAX_LOG_LINES)
        total = sum(len(ln) for ln in buf.lines)
        self.assertLessEqual(
            total, (status.LogWindow.MAX_LOG_LINES + 1) * status.LogWindow.MAX_LOG_LINE_CHARS,
            "the visible log must have a character bound, not only a line bound")
        self.assertEqual(content[-1].split(":")[0], f"progress {n - 1}",
                         "the newest line always survives")


if __name__ == "__main__":
    unittest.main()
