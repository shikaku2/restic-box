"""Bounded process termination tests (issue #6).

"Stop All" must actually stop: SIGTERM to the command's whole process group
(restic's sftp backend spawns ssh/sftp children that keep the stdout pipe
open), bounded wait, then SIGKILL.
"""
import inspect
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_runner as runner  # noqa: E402


def spawn(command):
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )


class StopProcessTest(unittest.TestCase):
    def test_stops_a_cooperative_process(self):
        proc = spawn(["sleep", "300"])
        start = time.monotonic()
        try:
            runner.stop_process(proc, timeout=5)
        finally:
            proc.stdout.close()
        self.assertLess(time.monotonic() - start, 4.0)
        self.assertIsNotNone(proc.poll())          # exited, not still running
        self.assertLess(proc.returncode, 0)        # by signal (SIGTERM)

    def test_escalates_to_kill_when_sigterm_is_ignored(self):
        # bash can exec-replace itself with the command it runs, dropping its
        # traps; a Python child makes "ignores SIGTERM" unambiguous. The child
        # announces that its handler is installed before we signal, so the test
        # measures the escalation, not a startup race.
        proc = spawn([sys.executable, "-c",
                      "import signal, time;"
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                      "print('armed', flush=True); time.sleep(300)"])
        self.assertEqual(proc.stdout.readline().strip(), "armed")
        start = time.monotonic()
        try:
            runner.stop_process(proc, timeout=1)   # bounded: 1 s grace, then kill
        finally:
            proc.stdout.close()
        self.assertLess(time.monotonic() - start, 8.0)
        self.assertEqual(proc.returncode, -9)      # SIGKILL

    def test_group_children_are_signalled_too(self):
        # The failure mode Stop All used to hang on: a backgrounded child keeps
        # the stdout pipe open, so terminating only the leader leaves the
        # reader thread blocked. Signalling the session kills the group.
        proc = spawn(["sh", "-c", "sleep 300 & echo $!; sleep 300"])
        child_pid = int(proc.stdout.readline().strip())
        try:
            runner.stop_process(proc, timeout=5)
        finally:
            proc.stdout.close()
        self.assertIsNotNone(proc.poll())

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self._alive(child_pid):
                break
            time.sleep(0.1)
        else:
            self.fail(f"session child {child_pid} survived Stop")
        self.assertFalse(self._alive(child_pid))

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            # a zombie is dead: init reparents and reaps the orphaned child
            return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
        except (FileNotFoundError, IndexError):
            return False

    def test_finished_process_is_a_noop(self):
        proc = spawn(["true"])
        proc.wait()
        proc.stdout.close()
        runner.stop_process(proc, timeout=1)       # must not raise

    def test_already_reaped_process_is_a_noop(self):
        proc = spawn(["true"])
        proc.wait()
        proc.stdout.close()
        # simulate the worker thread having reaped it first: pid no longer exists
        runner.stop_process(proc, timeout=1)
        runner.stop_process(proc, timeout=1)

    def test_commands_run_in_their_own_session(self):
        # stop_process signals the group only when the command leads its own
        # session; verify the runner starts every command that way.
        src = inspect.getsource(runner)
        self.assertGreaterEqual(src.count("start_new_session=True"), 3)


if __name__ == "__main__":
    unittest.main()
