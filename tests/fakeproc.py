"""Fake subprocess plumbing shared by the runner tests.

The stand-in has to behave like the real ``subprocess.Popen`` in exactly the ways
the runner depends on:

* ``stdout`` is an iterable of text lines (the runner reads it line by line);
* ``stdout`` has ``close()`` — the runner closes the pipe when the child is
  done, and a fake without a close() would quietly let that requirement rot;
* ``wait()`` returns the recorded exit code.

Kept here rather than duplicated per module so a change in what run_restic /
run_backup require of a process shows up as one edit.
"""


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class FakeProc:
    """Stand-in for subprocess.Popen in the run_* read loops."""

    def __init__(self, lines, returncode=0):
        self.stdout = FakeStdout(lines)
        self.returncode = returncode
        self.waited = False

    def wait(self):
        self.waited = True
        return self.returncode


def fake_popen(lines, returncode=0):
    """A subprocess.Popen replacement yielding one FakeProc."""

    def _open(*args, **kwargs):
        return FakeProc(lines, returncode)

    return _open
