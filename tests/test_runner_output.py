"""Bounded process-output retention tests (issue #14, review fix #6).

A backup can run for days and stream hundreds of thousands of JSON progress
records. They are consumed for the progress bar and must NOT accumulate in
memory. Retained diagnostic text is trimmed oldest-first, whole lines only —
callers json-parse individual lines, so trimming drops whole records.

Review fix #6 covers the hole that left: a SINGLE record bigger than the total
cap used to be kept whole ("the newest line always survives"), so one
exceptional line could retain an arbitrary amount of memory. Now every retained
line is also bounded per line, the excerpt says what it is, and the full record
is written to the rotating diagnostic log file, which has its own disk bound
(see tests/test_diag_log.py). run_backup additionally re-emits the summary's
authoritative total_bytes_processed as its final progress event.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_diag_log as diag_log  # noqa: E402
from tests import DIAG_LOG_DIR  # noqa: E402
import restic_runner as runner  # noqa: E402
from restic_config import Config, Directory  # noqa: E402

_TMP = None


def setUpModule():
    """Diagnostics must land in a throwaway directory: the suite has no business
    writing into the user's real ~/.config/restic-box/logs."""
    global _TMP
    _TMP = Path(tempfile.mkdtemp(prefix="rb-out-logs-"))
    diag_log.set_log_dir(_TMP)


def tearDownModule():
    diag_log.active().close()
    # Restore the suite-wide throwaway directory, never the user's real config dir.
    diag_log.set_log_dir(DIAG_LOG_DIR)
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


from tests.fakeproc import FakeProc, fake_popen  # noqa: E402


def cfg():
    return Config(backend="local", repo_path="/repo-test")


def status_line(pct, done, total):
    return json.dumps({"message_type": "status", "percent_done": pct,
                       "bytes_done": done, "total_bytes": total})


def summary_line(data_added=42, files_new=3, processed=900):
    return json.dumps({"message_type": "summary", "data_added": data_added,
                       "files_new": files_new, "total_bytes_processed": processed})


class LineTailTest(unittest.TestCase):
    def test_keeps_newest_lines_and_reports_drops(self):
        tail = runner._LineTail(30)
        for i in range(10):
            tail.append(f"line-{i:02}")
        text = tail.text()
        self.assertTrue(text.startswith("[restic-box: dropped"))
        kept = text.splitlines()[1:]
        self.assertTrue(all(len(line) <= 30 for line in kept))
        self.assertEqual(kept[-1], "line-09", "newest line must survive")
        self.assertEqual(tail.dropped, 10 - len(kept))

    def test_oversized_record_is_excerpted_with_a_visible_note(self):
        """Review fix #6: one record must not get an exemption from the cap."""
        tail = runner._LineTail(1000)
        big = "HEAD" + "x" * 5000 + "TAIL"
        note = tail.append(big)

        text = tail.text()
        self.assertLessEqual(len(text), 1000, "the cap is a hard bound now")
        self.assertTrue(text.startswith("HEAD"), "the head of the record is what a human can act on")
        self.assertNotIn("TAIL", text)
        self.assertIn("more characters", text, "an excerpt must announce itself")
        self.assertIsNotNone(note)
        self.assertIn("more characters", note)
        self.assertEqual(tail.truncated, 1)

    def test_per_line_limit_bounds_a_single_gigantic_line(self):
        tail = runner._LineTail(4000, max_line=200)
        tail.append("y" * 400_000)
        text = tail.text()
        self.assertLessEqual(len(text), 200, "max_line is the effective bound here")
        self.assertEqual(tail.truncated, 1)

    def test_full_detail_of_an_excerpted_record_reaches_the_log_file(self):
        """Bounded memory is only acceptable if the detail still exists: it goes
        to the rotating diagnostic log file, which the note points at."""
        tail = runner._LineTail(500)
        big = "unique-head-" + "z" * 9000 + "-unique-tail"
        note = tail.append(big)

        logged = diag_log.active().contents()
        self.assertIn("unique-head-" + "z" * 9000, logged, "the full record is on disk")
        self.assertIn(f"{len(big)} characters total", logged,
                      "the log states how large the record really was")
        self.assertIn(str(diag_log.log_dir()), note, "the note names the log file")
        self.assertIn("-unique-tail", logged, "even the tail of the record survives on disk")

    def test_second_line_still_evicts_the_first(self):
        tail = runner._LineTail(10)
        tail.append(json.dumps({"id": "x" * 100}))  # oversized newest
        tail.append("short")
        text = tail.text()
        self.assertNotIn("x" * 100, text, "the oversized line yields once anything newer exists")
        self.assertIn("short", text)

    def test_no_marker_when_under_limit(self):
        tail = runner._LineTail(1000)
        tail.append("hello")
        self.assertEqual(tail.text(), "hello")
        self.assertNotIn("dropped", tail.text())

    def test_parsed_records_under_the_cap_stay_whole(self):
        """snapshots --json really is one line; an ordinary record must never be
        cut, or json-parsing callers would break."""
        record = json.dumps({"id": "a" * 500, "paths": ["/mnt/share/x"]})
        tail = runner._LineTail(runner.MAX_QUERY_OUTPUT_CHARS, runner.MAX_QUERY_LINE_CHARS)
        self.assertIsNone(tail.append(record))
        self.assertEqual(json.loads(tail.text())["id"], "a" * 500)


class RunBackupOutputTest(unittest.TestCase):
    def run_with(self, lines, returncode=0, **kw):
        out_events, progress_events = [], []
        directory = Directory("/src")
        with mock.patch.object(runner.subprocess, "Popen", fake_popen(lines, returncode)):
            rc, out, data_added = runner.run_backup(
                cfg(), directory,
                on_output=out_events.append,
                on_progress=lambda p, d, t: progress_events.append((p, d, t)),
                **kw,
            )
        return rc, out, data_added, out_events, progress_events

    def test_status_records_are_streamed_but_never_retained(self):
        lines = [status_line(i / 10, i * 100, 1000) for i in range(5)]
        lines.append("restic: warning: permission denied")
        lines.append(summary_line())

        rc, out, data_added, out_events, progress = self.run_with(lines)

        self.assertEqual(rc, 0)
        self.assertIn("permission denied", out, "diagnostics must be retained")
        self.assertIn('"message_type": "summary"', out, "the summary is the final diagnostic")
        self.assertNotIn('"message_type": "status"', out,
                         "progress records must never be retained (issue #14)")
        self.assertNotIn('"message_type": "status"', "\n".join(out_events))
        self.assertEqual(len(progress), 6, "5 status events + 1 final summary event")
        self.assertEqual(data_added, 42)

    def test_summary_total_overrides_the_last_intermediate_progress(self):
        """Size accounting oversight: callers record the LAST progress total.
        The summary's total_bytes_processed must be that final value."""
        lines = [status_line(0.9, 810, 900)]          # last status says total 900...
        lines.append(summary_line(processed=4242))    # ...but the true volume is 4242

        _rc, _out, _da, _oe, progress = self.run_with(lines)

        self.assertEqual(progress[-1], (1.0, 4242, 4242),
                         "the authoritative processed total must be the last event")

    def test_summary_without_processed_field_adds_no_event(self):
        lines = [status_line(0.5, 5, 10), json.dumps({"message_type": "summary", "data_added": 1})]
        _rc, _out, _da, _oe, progress = self.run_with(lines)
        self.assertEqual(len(progress), 1, "no fabricated 100% event without real data")

    def test_retained_diagnostics_are_bounded_and_whole(self):
        lines = [f"restic: diagnostic line {i:03d} " + "x" * 40 for i in range(200)]
        lines.append(summary_line())
        with mock.patch.object(runner, "MAX_BACKUP_OUTPUT_CHARS", 4000):
            _rc, out, _da, _oe, _pr = self.run_with(lines)

        self.assertLessEqual(len(out), 4000 + 200)
        first, *kept = out.splitlines()
        self.assertIn("dropped", first)
        self.assertEqual(kept[-1], summary_line(), "newest retained line survives intact")
        self.assertTrue(all(line in lines or "dropped" in line for line in [first] + kept),
                        "retention must keep whole original lines only")

    def test_cap_constants_are_pinned(self):
        self.assertEqual(runner.MAX_BACKUP_OUTPUT_CHARS, 256 * 1024)
        self.assertGreaterEqual(runner.MAX_QUERY_OUTPUT_CHARS, 1024 * 1024,
                                "query cap is a generous backstop, not a working limit")
        # Review fix #6: per-line bounds, sized by what the caller does with the
        # text (parsed records may use the whole budget; human-facing backup
        # diagnostics get a tight one).
        self.assertEqual(runner.MAX_QUERY_LINE_CHARS, runner.MAX_QUERY_OUTPUT_CHARS)
        self.assertEqual(runner.MAX_BACKUP_LINE_CHARS, 64 * 1024)
        self.assertLess(runner.MAX_BACKUP_LINE_CHARS, runner.MAX_BACKUP_OUTPUT_CHARS)

    def test_one_gigantic_diagnostic_line_cannot_defeat_the_cap(self):
        """The exact hole review fix #6 is about: retention used to exempt the
        newest line, however large, from the cap."""
        lines = [status_line(0.5, 5, 10), "corrupt-" + "q" * 300_000, summary_line()]
        with mock.patch.object(runner.subprocess, "Popen", fake_popen(lines)):
            _rc, out, _da, _oe, _pr = self.run_with(lines)

        self.assertLess(len(out), runner.MAX_BACKUP_OUTPUT_CHARS + 200,
                        "retention is bounded even when one line is megabytes long")
        self.assertIn("corrupt-", out, "the head of the record survives")
        self.assertIn("more characters", out, "and it announces that it was excerpted")

    def test_oversized_record_is_written_to_the_diagnostic_log(self):
        lines = ["blob-" + "w" * 120_000 + "-end"]
        _rc, out, _da, out_events, _pr = self.run_with(lines)

        logged = diag_log.active().contents()
        self.assertIn("blob-" + "w" * 120_000 + "-end", logged,
                      "the full record must survive somewhere, not just exist in RAM")
        self.assertTrue(any("more characters" in line for line in out_events),
                        "the user sees that this line was excerpted")

    def test_progress_records_reach_neither_memory_nor_the_log_file(self):
        """'Routine progress JSON should be parsed for progress and then
        discarded': streaming it to disk instead would just move the problem."""
        lines = [status_line(i / 2000, i * 100, 200_000) for i in range(2000)]
        _rc, out, _da, _oe, progress = self.run_with(lines)

        self.assertEqual(len(progress), 2000)
        self.assertNotIn('"message_type": "status"', out)
        self.assertNotIn('"message_type": "status"', diag_log.active().contents(),
                         "2000 progress records must not accumulate on disk either")


class RunResticOutputTest(unittest.TestCase):
    def test_query_output_is_bounded_whole_and_streamed(self):
        lines = [json.dumps({"message_type": "stats", "i": i, "pad": "p" * 60})
                 for i in range(150)]
        streamed = []
        with mock.patch.object(runner, "MAX_QUERY_OUTPUT_CHARS", 3000), \
             mock.patch.object(runner.subprocess, "Popen", fake_popen(lines)):
            rc, out = runner.run_restic(["stats"], cfg(), on_output=streamed.append)

        self.assertEqual(rc, 0)
        self.assertEqual(len(streamed), 150, "streaming is unaffected by trimming")
        self.assertLessEqual(len(out), 3000 + 200)
        body = out.splitlines()[1:]  # drop the marker line
        self.assertEqual(body[-1], lines[-1], "the newest record must survive whole")
        for line in body:
            json.loads(line)  # every retained line still parses as a whole record

    def test_small_output_returned_verbatim(self):
        lines = ["snapshot abc123", "restic-box: nothing to do"]
        with mock.patch.object(runner.subprocess, "Popen", fake_popen(lines)):
            _rc, out = runner.run_restic(["foo"], cfg())
        self.assertEqual(out, "\n".join(lines))
        self.assertNotIn("dropped", out)

    def test_parsed_query_records_stay_whole_within_the_cap(self):
        """snapshots --json really is one line: an ordinary record must never be
        excerpted, or every parsed view in the app would break."""
        record = json.dumps([{"id": f"{i:08d}", "paths": ["/mnt/share"]} for i in range(50)])
        with mock.patch.object(runner.subprocess, "Popen", fake_popen([record])):
            _rc, out = runner.run_restic(["snapshots", "--json"], cfg())
        self.assertEqual(out, record)
        self.assertEqual(json.loads(out)[49]["id"], "00000049")
        self.assertEqual(diag_log.active().errors, 0)

    def test_one_oversized_query_record_cannot_defeat_the_cap(self):
        huge = json.dumps({"pad": "s" * 60_000})      # far beyond the patched cap
        streamed = []
        with mock.patch.object(runner, "MAX_QUERY_OUTPUT_CHARS", 4000), \
             mock.patch.object(runner.subprocess, "Popen", fake_popen([huge])):
            _rc, out = runner.run_restic(["snapshots", "--json"], cfg(),
                                         on_output=streamed.append)

        self.assertLessEqual(len(out), 4000, "the query cap is now a hard bound")
        self.assertIn("more characters", out, "an excerpt must be visible in the retained text")
        self.assertTrue(any("more characters" in s for s in streamed),
                        "and it is streamed to the log too")
        self.assertIn(huge[:20_000], diag_log.active().contents(),
                      "the full record goes to the diagnostic log file")


class PipeLifecycleTest(unittest.TestCase):
    """The runner must close the output pipe it creates (review fix #6 audit).

    run_restic is called by the scheduler, the views, the browser and the status
    checks: several times a minute, for years. A pipe left to the garbage
    collector is a ResourceWarning per call and, in a long-lived tray process, a
    slow creep toward the file-descriptor limit.
    """

    def _capture_popen(self, lines, returncode=0):
        captured = {}

        def _open(*args, **kwargs):
            captured["proc"] = FakeProc(lines, returncode)
            return captured["proc"]
        return _open, captured

    def test_run_restic_closes_its_stdout_pipe(self):
        popen, captured = self._capture_popen(["restic: done", "snapshot abc"])
        with mock.patch.object(runner.subprocess, "Popen", popen):
            _rc, _out = runner.run_restic(["snapshots"], cfg())
        self.assertTrue(captured["proc"].stdout.closed,
                        "run_restic leaked the pipe it opened")

    def test_run_restic_closes_the_pipe_even_when_the_child_failed(self):
        popen, captured = self._capture_popen(["repository not found"], returncode=1)
        with mock.patch.object(runner.subprocess, "Popen", popen):
            rc, _out = runner.run_restic(["snapshots"], cfg())
        self.assertEqual(rc, 1)
        self.assertTrue(captured["proc"].stdout.closed,
                        "a failing restic run leaks just as surely as a working one")

    def test_run_backup_closes_its_stdout_pipe(self):
        popen, captured = self._capture_popen([status_line(0.5, 1, 2), summary_line()])
        with mock.patch.object(runner.subprocess, "Popen", popen):
            runner.run_backup(cfg(), Directory("/src"))
        self.assertTrue(captured["proc"].stdout.closed,
                        "a days-long backup opens the most pipes of any code path")


if __name__ == "__main__":
    unittest.main()
