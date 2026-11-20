"""Rotating diagnostic log tests (review fix #6).

The memory bounds in restic_runner._LineTail and restic_status.LogWindow are
only safe if what they drop still exists somewhere the user can read — and if
that somewhere has its own hard bound. This module is the on-disk half:

  * operational lines land in <config>/logs/restic-box.log;
  * rotation caps the disk used at MAX_LOG_FILES * MAX_LOG_FILE_BYTES;
  * one record cannot fill a file faster than rotation can follow;
  * writing a diagnostic must never be able to break a backup, including when
    the log directory is unusable;
  * worker threads and the GTK thread share the file without corrupting it.

Every test here points the log at a throwaway directory: the suite never writes
into the user's real ~/.config/restic-box.
"""
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_diag_log as diag_log  # noqa: E402
from tests import DIAG_LOG_DIR  # noqa: E402  suite-wide throwaway log directory

_TMP = None


def setUpModule():
    global _TMP
    _TMP = Path(tempfile.mkdtemp(prefix="rb-diag-"))
    diag_log.set_log_dir(_TMP)


def tearDownModule():
    diag_log.active().close()
    # Restore the suite-wide throwaway directory, never the user's real config dir.
    diag_log.set_log_dir(DIAG_LOG_DIR)
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


class DiagTestCase(unittest.TestCase):
    """Base for tests that build throwaway DiagLog instances: each one gets its
    own directory (so rotation counts are unambiguous) and is closed afterwards
    instead of leaking an open file handle."""

    def setUp(self):
        self._logs = []

    def tearDown(self):
        for log in self._logs:
            log.close()

    def track(self, log):
        self._logs.append(log)
        return log

    def fresh(self, **kw):
        tmp = Path(tempfile.mkdtemp(prefix="rb-diag-one-", dir=_TMP))
        return self.track(diag_log.DiagLog(tmp, **kw)), tmp


class BasicWriteTest(DiagTestCase):
    def test_lines_are_written_to_the_active_file_with_a_timestamp(self):
        log, tmp = self.fresh()
        log.write("backup finished (rc=0)")
        text = (tmp / diag_log.LOG_FILE_NAME).read_text()
        self.assertRegex(text, r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] backup finished \(rc=0\)\n$")
        self.assertEqual(log.errors, 0)

    def test_module_helpers_share_one_log_and_it_follows_set_log_dir(self):
        other = Path(tempfile.mkdtemp(prefix="rb-diag-move-", dir=_TMP))
        try:
            diag_log.set_log_dir(other)
            diag_log.note("moved here")
            self.assertIn("moved here", (other / diag_log.LOG_FILE_NAME).read_text())
            self.assertEqual(diag_log.log_dir(), other)
        finally:
            diag_log.set_log_dir(_TMP)
        diag_log.note("back to the suite directory")
        self.assertIn("back to the suite directory",
                      (diag_log.log_dir() / diag_log.LOG_FILE_NAME).read_text())

    def test_default_directory_is_logs_under_the_config_directory(self):
        """The app writes next to its settings, which is where a user looks."""
        fake_cfg = Path(tempfile.mkdtemp(prefix="rb-diag-cfg-", dir=_TMP))
        try:
            with mock.patch.object(cfg_mod, "CONFIG_DIR", fake_cfg):
                diag_log.set_log_dir(None)
                self.assertEqual(diag_log.log_dir(), fake_cfg / "logs")
                self.assertEqual(diag_log.active().write("via config dir"),
                                 fake_cfg / "logs" / diag_log.LOG_FILE_NAME)
        finally:
            diag_log.active().close()      # the log we just created points at fake_cfg
            diag_log.set_log_dir(_TMP)

    def test_log_directory_is_created_private(self):
        """Diagnostics can quote repository paths: keep the directory 0700."""
        outer = Path(tempfile.mkdtemp(prefix="rb-diag-outer-", dir=_TMP))
        os.chmod(outer, 0o755)
        log = self.track(diag_log.DiagLog(outer / "logs"))
        log.write("x")
        mode = (outer / "logs").stat().st_mode & 0o777
        self.assertEqual(mode, 0o700,
                         "a world-readable log of a user's repository paths is not acceptable")

    def test_indicator_mirrors_its_log_there(self):
        """ResticIndicator._log is the app's single logging path (the settings
        dialog uses it too), so writing there is what makes trimmed window
        content recoverable."""
        src = (Path(__file__).resolve().parent.parent / "restic_indicator.py").read_text(
            encoding="utf-8")
        self.assertIn("import restic_diag_log as diag_log", src)
        body = src[src.index("def _log("): src.index("def _show_log(")]
        self.assertIn("diag_log.note(", body,
                      "the window is bounded in both lines and line size, so the "
                      "untruncated text has to reach the rotating file")


class RotationTest(DiagTestCase):
    def test_rotation_enforces_a_disk_bound(self):
        log, tmp = self.fresh(max_file_bytes=4000, max_files=3)
        for i in range(300):                      # 300 * ~90 bytes ≈ 27 KB
            log.write(f"[{i:04d}] " + "diagnostic line " * 6)

        used = log.total_bytes()
        self.assertLessEqual(used, 3 * 4000 + 4000,
                             "total disk may only overshoot by the record that triggered "
                             "the last rotation")
        self.assertGreater(used, 4000, "and it is a real log, not a truncated stub")

    def test_rotation_keeps_the_newest_and_lets_the_oldest_expire(self):
        log, tmp = self.fresh(max_file_bytes=2000, max_files=3)
        for i in range(200):
            log.write(f"entry-{i:04d} " + "filler " * 40)

        names = [p.name for p in log.files()]
        self.assertEqual(sorted(names),
                         [diag_log.LOG_FILE_NAME, f"{diag_log.LOG_FILE_NAME}.1",
                          f"{diag_log.LOG_FILE_NAME}.2"],
                         "exactly the active file plus MAX_LOG_FILES-1 copies")

        text = log.contents()
        self.assertIn("entry-0199", text, "the newest diagnostics survive")
        self.assertNotIn("entry-0000 ", text, "the oldest bytes are the ones that expire")

    def test_rotation_never_grows_past_max_files_across_many_rotations(self):
        log, tmp = self.fresh(max_file_bytes=500, max_files=4)
        for i in range(60):
            log.write(f"line-{i:03d}-" + "z" * 400)
        self.assertEqual(sorted(p.name for p in log.files()),
                         sorted([diag_log.LOG_FILE_NAME,
                                 f"{diag_log.LOG_FILE_NAME}.1",
                                 f"{diag_log.LOG_FILE_NAME}.2",
                                 f"{diag_log.LOG_FILE_NAME}.3"]))

    def test_a_single_huge_record_cannot_fill_the_file(self):
        """MAX_RECORD_CHARS bounds one write: a megabytes-long line from a
        corrupted stream must not outrun rotation."""
        log, tmp = self.fresh(max_file_bytes=100_000, max_files=2, max_record_chars=8000)
        log.write("HEAD" + "x" * 900_000)

        path = tmp / diag_log.LOG_FILE_NAME
        self.assertLessEqual(path.stat().st_size, 8000 + 200,
                             "the record is excerpted on its way in")
        self.assertIn("HEAD", path.read_text())
        self.assertIn("record excerpted to 8000 characters", path.read_text(),
                      "the file says the excerpt is an excerpt")

    def test_oversized_records_keep_the_detail_the_runner_had_to_drop(self):
        log, tmp = self.fresh()
        big = "snapshots-head-" + "j" * 40_000 + "-snapshots-tail"
        path = log.record_oversized(big, limit=2048, source="restic output")

        text = path.read_text()
        self.assertIn(f"{len(big)} characters total", text)
        self.assertIn("memory kept 2048", text)
        self.assertIn("snapshots-head-" + "j" * 40_000, text)
        self.assertIn("-snapshots-tail", text, "the whole record survives on disk")

    def test_oversized_records_are_also_excerpted_to_the_disk_bound(self):
        log, tmp = self.fresh(max_record_chars=6000)
        big = "y" * 100_000
        log.record_oversized(big, limit=1000)
        path = tmp / diag_log.LOG_FILE_NAME
        self.assertIn("file excerpt trimmed to", path.read_text())
        self.assertLessEqual(path.stat().st_size, 6000 + 200,
                             "even a spilled record respects the per-record cap")


class FailureTest(DiagTestCase):
    def test_an_unusable_log_directory_never_raises_into_a_backup(self):
        """A diagnostic log is not allowed to break the thing it documents."""
        blocker = _TMP / "not-a-directory"
        blocker.write_text("I am a file")
        log = self.track(diag_log.DiagLog(blocker / "logs"))
        for _ in range(diag_log.MAX_ERROR_STREAK + 5):
            self.assertIsNone(log.write("still backing up"))     # no exception
        self.assertTrue(log.disabled, "it gives up rather than spinning on a broken path")
        self.assertGreaterEqual(log.errors, diag_log.MAX_ERROR_STREAK)

    def test_a_disabled_logger_stops_writing_instead_of_retrying_forever(self):
        """Disabling is only about the file: the log window keeps working,
        because ResticIndicator._log appends to the window separately."""
        log, tmp = self.fresh()
        log.disabled = True
        self.assertIsNone(log.write("dropped"))
        self.assertFalse((tmp / diag_log.LOG_FILE_NAME).exists())

    def test_a_deleted_log_file_is_reopened_instead_of_silently_swallowing_records(self):
        """Someone cleaning up ~/.config must not turn later diagnostics into
        writes into a deleted inode."""
        log, tmp = self.fresh(max_file_bytes=1000, max_files=3)
        log.write("a" * 1200)
        shutil.rmtree(tmp)                       # deleted mid-run
        tmp.mkdir()
        self.assertIsNotNone(log.write("after the directory came back"))
        self.assertIn("after the directory came back",
                      (tmp / diag_log.LOG_FILE_NAME).read_text())

    def test_module_note_never_raises_when_the_directory_is_unwritable(self):
        """The module-level helper the app actually calls, with a file sitting
        where the log directory should be."""
        try:
            os.makedirs(_TMP / "cannot-exist", exist_ok=True)
            (_TMP / "cannot-exist" / "deep").write_text("file in the way")
            diag_log.set_log_dir(_TMP / "cannot-exist" / "deep")
            for _ in range(3):                   # repeated failures must stay silent
                self.assertIsNone(diag_log.note("must not raise"))
        finally:
            diag_log.active().close()
            diag_log.set_log_dir(_TMP)


class ConcurrencyTest(DiagTestCase):
    def test_concurrent_writers_do_not_interleave_or_lose_records(self):
        """Worker threads, the GTK thread and the settings dialog all log."""
        # Caps are deliberately far above what this test writes: the point is
        # the shared handle, and rotating mid-test would drop records and make
        # the count assertion meaningless.
        log, tmp = self.fresh(max_file_bytes=1_000_000, max_files=3)
        writers = 8
        per_writer = 150

        def worker(tag):
            for i in range(per_writer):
                log.write(f"{tag}-{i:03d} " + "payload " * 20)

        threads = [threading.Thread(target=worker, name=f"w{n}", args=(f"w{n}",))
                   for n in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        lines = log.contents().splitlines()
        records = [ln for ln in lines
                   if re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] w\d-\d{3} payload", ln)]
        self.assertEqual(len(records), writers * per_writer,
                         "no record may be lost, truncated, or merged with another "
                         "thread's write")
        self.assertEqual(log.errors, 0)

    def test_active_is_safe_to_call_from_many_threads(self):
        seen = set()
        lock = threading.Lock()

        def grab():
            log = diag_log.active()
            with lock:
                seen.add(id(log))

        threads = [threading.Thread(target=grab) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(seen), 1, "one shared log, not one per thread")


if __name__ == "__main__":
    unittest.main()
