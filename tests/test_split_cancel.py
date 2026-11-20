"""Split planning cancellation tests (issue #6).

A Stop request must abort scanning/planning itself — not just the subprocess
loop. SplitCancelled propagates out of expand(); expand() must NOT treat it
like a planning failure and fall back to backing up the whole unsplit source.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_split as split  # noqa: E402


def make_tree(root: Path, n_files: int = 60):
    for i in range(n_files):
        (root / f"f{i}.bin").write_bytes(b"x" * 100)
    (root / "sub").mkdir()
    for i in range(3):
        (root / "sub" / f"g{i}.bin").write_bytes(b"y" * 100)


class ScanCancellationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-cancel-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        make_tree(self.tmp)

    def test_scan_aborts_when_stop_requested(self):
        checks = []

        def should_stop():
            checks.append(1)
            return len(checks) >= 5  # trips a little into the walk

        with self.assertRaises(split.SplitCancelled):
            split.scan(str(self.tmp), [], 1024, None, should_stop)
        self.assertTrue(checks)  # the hook was actually consulted

    def test_scan_without_hook_is_unaffected(self):
        size, *_ = split.scan(str(self.tmp), [], 1024)
        self.assertGreater(size[str(self.tmp)], 0)

    def test_plan_stable_aborts_when_stop_requested(self):
        size, children, bigfiles, smallfiles = split.scan(str(self.tmp), [], 1024)
        prev = split.plan_from_scan(str(self.tmp), [], 1024, size, children,
                                    bigfiles, smallfiles)
        with self.assertRaises(split.SplitCancelled):
            split.plan_stable(prev, str(self.tmp), [], 1024, 2.0, size, children,
                              bigfiles, smallfiles, should_stop=lambda: True)

    def test_build_plan_aborts_when_stop_requested(self):
        with self.assertRaises(split.SplitCancelled):
            split.build_plan(str(self.tmp), [], 1024, None, lambda: True)


class ExpandCancellationTest(unittest.TestCase):
    """expand() is what _run_backup calls: cancellation must escape it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-cancel-exp-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        make_tree(self.tmp)
        # keep saved plans inside the sandbox: expand() persists plans under
        # the real CONFIG_DIR otherwise
        plans = self.tmp / "plans"
        patcher = mock.patch.object(split, "CONFIG_DIR", plans)
        patcher.start()
        self.addCleanup(patcher.stop)

        class D:  # duck-typed Directory
            path = str(self.tmp)
            excludes: list = []
            cached_size = 0.0

        self.directory = D()

    def test_expand_reraises_instead_of_falling_back(self):
        # A planning *failure* falls back to [directory] (back up everything).
        # A cancellation must not: falling back would launch exactly the huge
        # single-unit backup the user just asked to stop.
        with self.assertRaises(split.SplitCancelled):
            split.expand(self.directory, 1, None, 2.0, lambda: True)
        self.assertEqual(self.directory.cached_size, 0.0)  # untouched

    def test_expand_without_cancellation_still_plans(self):
        units = split.expand(self.directory, 1024 ** 3 // 1, None, 2.0, lambda: False)
        self.assertTrue(units)  # planned normally, threshold 1 GiB → single unit

    def test_cancelled_expand_does_not_save_a_partial_plan(self):
        plan_path = split.plan_file_path(str(self.tmp), 1024, [])
        with self.assertRaises(split.SplitCancelled):
            split.expand(self.directory, 1, None, 2.0, lambda: True)
        self.assertFalse(plan_path.exists())


if __name__ == "__main__":
    unittest.main()
