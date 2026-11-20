"""Test package for restic-box.

Beyond making the modules importable as ``tests.test_x``, this package does one
safety job that belongs at suite level (review fix #6): the app now keeps a
rotating diagnostic log in ~/.config/restic-box/logs, and importing a test
package is the one moment that reliably happens before every test module. The
log is therefore redirected here, once, to a throwaway directory, and removed
when the interpreter exits.

Individual modules may redirect it again when they need exact rotation
arithmetic (tests/test_diag_log.py does); the audit in tests/test_suite_hygiene.py
requires that of any module which reads or writes the log, and checks that the
directory in force while tests run is a temporary one.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import restic_diag_log as diag_log  # noqa: E402

#: Directory the whole suite writes its diagnostics to. Modules that redirect
#: it themselves restore this one when they finish.
DIAG_LOG_DIR = Path(tempfile.mkdtemp(prefix="rb-tests-logs-"))
diag_log.set_log_dir(DIAG_LOG_DIR)


@atexit.register
def _remove_suite_log_dir() -> None:
    try:
        diag_log.active().close()
    except Exception:      # a broken cleanup must not fail a passing run
        pass
    shutil.rmtree(DIAG_LOG_DIR, ignore_errors=True)
