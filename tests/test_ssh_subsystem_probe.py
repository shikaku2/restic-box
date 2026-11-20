"""SFTP-subsystem pre-flight tests (fix #7, restic-box-fixes2.md).

The bug: the reachability pre-flight (and the settings dialog's Test
Connection) opened an SSH session and ran `echo OK`. That answers a question
restic never asks. An SSH account can legitimately offer the SFTP subsystem
while refusing remote command execution — SFTP-only storage providers are
configured exactly that way — so restic-box reported such a repository as
unreachable, skipped the operation, and backed off on a connection that works.

The fix: ask for the subsystem itself. One transport builder
(`restic_runner.ssh_command`) now serves all three jobs — restic's
`-o sftp.command`, the pre-flight, and Test Connection — so the probe cannot
drift from the backup, and stdin is /dev/null so a subsystem session can never
be handed the tray process's keyboard.

No network and no real SSH server: the tests put a stand-in `ssh` earlier on
PATH that answers like the server under test. The stand-in is a real
subprocess, so the argv the app builds, the exit codes it classifies, and the
stdin it passes are exercised for real.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_runner as runner  # noqa: E402
import restic_settings  # noqa: E402

# A stand-in for ssh. It records what it was asked to do (every argv entry, and
# what its stdin points at), then answers like a server of the configured mode:
#
#   normal        — subsystem and remote-command requests both fine
#   sftp-only     — SFTP subsystem fine, remote commands refused (fix #7's case)
#   no-subsystem  — remote commands fine, no SFTP subsystem: restic is useless
#   session-odd   — subsystem opens, the session exits non-zero when its input
#                   closes: says nothing about the transport
#   refused / auth— the connection or the login itself fails
#   stuck         — the server never answers: only the app's own bound is finite
FAKE_SSH = """#!/bin/sh
log=${FAKE_SSH_LOG:-/dev/null}
mode=${FAKE_SSH_MODE:-normal}

for a in "$@"; do echo "ARG: $a" >> "$log"; done
echo "STDIN: $(readlink /proc/self/fd/0)" >> "$log"

# OpenSSH order: ssh [opts] [-s] host [subsystem-or-command]
positional=""
expect_value=0
subsystem=0
for arg in "$@"; do
    if [ "$expect_value" -eq 1 ]; then expect_value=0; continue; fi
    case "$arg" in
        -s) subsystem=1 ;;
        -p|-l|-i|-o|-F|-J|-e|-b|-c) expect_value=1 ;;
        -*) ;;
        *) positional="$positional $arg" ;;
    esac
done

set -- $positional
host=$1
shift
requested="$*"

case "$mode" in
    refused)
        echo "ssh: connect to host $host port 23: Connection refused" >&2
        exit 255 ;;
    auth)
        echo "$host: Permission denied (publickey)" >&2
        exit 255 ;;
    stuck)
        exec sleep 30 ;;
esac

if [ "$subsystem" -eq 1 ]; then
    echo "REQUEST: subsystem $requested" >> "$log"
    case "$mode" in
        no-subsystem)
            echo "Subsystem request failed on channel 0" >&2
            exit 255 ;;
        session-odd) exit 1 ;;
        *) exit 0 ;;
    esac
fi

if [ -n "$requested" ]; then
    echo "REQUEST: exec $requested" >> "$log"
    case "$mode" in
        sftp-only)
            echo "Received message: Request [exec] failed on channel 0" >&2
            exit 255 ;;
        *) exit 0 ;;
    esac
fi

exit 0
"""


def transport_arguments(cfg) -> list[str]:
    """The ssh argv restic is given, via `-o sftp.command=…`."""
    command = runner._base_cmd(cfg)
    option = command[command.index("-o") + 1]
    assert option.startswith("sftp.command=")
    return shlex.split(option.split("=", 1)[1])


class FakeSshHarness(unittest.TestCase):
    """Runs the app's SSH probe against a fake ssh placed earlier on PATH."""

    def setUp(self):
        if not shutil.which("sh"):
            self.skipTest("no /bin/sh to run the fake ssh with")
        self.bin = Path(tempfile.mkdtemp(prefix="rb-fakessh-"))
        self.addCleanup(shutil.rmtree, self.bin, True)
        script = self.bin / "ssh"
        script.write_text(FAKE_SSH, encoding="utf-8")
        os.chmod(script, 0o755)
        self.log = self.bin / "log"

        self.cfg = cfg_mod.Config(backend="sftp", ssh_host="backup.example",
                                   ssh_user="backup-user", ssh_port=23,
                                   ssh_key="/tmp/backup key",
                                   repo_path="/backups/repo",
                                   password_file="/tmp/pw")

        patcher = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_SSH_LOG": str(self.log),
            "FAKE_SSH_MODE": "normal",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(shutil.which("ssh"), str(script),
                         "the fake must shadow the real ssh: this suite never connects")

    def serve(self, mode: str) -> None:
        os.environ["FAKE_SSH_MODE"] = mode

    def _after(self, prefix: str) -> list[str]:
        if not self.log.exists():
            return []
        return [line[len(prefix):] for line in self.log.read_text().splitlines()
                if line.startswith(prefix)]

    def requests(self) -> list[str]:
        """What the app asked ssh to open, in order."""
        return self._after("REQUEST: ")

    def argv(self) -> list[str]:
        """The full argv the fake was invoked with. `$@` inside the script
        leaves out argv[0], so the program name is put back: the comparison is
        against the argv the app handed to exec."""
        return ["ssh"] + self._after("ARG: ")

    def stdin_targets(self) -> list[str]:
        return self._after("STDIN: ")

    def dialog_verdict(self, cfg=None) -> str:
        """The settings dialog's Test Connection, through the same fake ssh."""
        cfg = cfg or self.cfg
        dlg = restic_settings.SettingsDialog.__new__(restic_settings.SettingsDialog)
        logs: list[str] = []
        dlg._cfg = cfg
        dlg._on_log = logs.append
        dlg._logs = logs

        class Label:
            def __init__(self):
                self.text = None

            def set_text(self, text):
                self.text = text

        class Button:
            def __init__(self):
                self.sensitive = True

            def set_sensitive(self, value):
                self.sensitive = value

        dlg._lbl_test = Label()
        dlg._btn_test = Button()
        with mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            dlg._do_sftp_test_connection(cfg)
        return dlg._lbl_test.text


class SftpOnlyAccountTest(FakeSshHarness):
    """The regression: a server that offers SFTP and refuses shells must pass."""

    def test_preflight_accepts_a_working_sftp_transport(self):
        self.serve("sftp-only")
        reachable, reason = runner.ssh_transport_ok(self.cfg)
        self.assertTrue(reachable, reason)
        self.assertEqual(reason, "")
        self.assertTrue(runner.is_host_reachable(self.cfg))

    def test_the_probe_never_asks_for_a_remote_command(self):
        self.serve("sftp-only")
        runner.ssh_transport_ok(self.cfg)
        self.assertEqual(self.requests(), ["subsystem sftp"],
                         "fix #7: the pre-flight opens the subsystem, nothing else")

    def test_the_old_probe_shape_would_have_been_refused(self):
        """What the bug looked like: the same server answers `echo OK` with
        ssh's own 255, which the reachability rule reads as unreachable."""
        self.serve("sftp-only")
        legacy = ["ssh", "-p", "23", "-l", "backup-user", "-o", "BatchMode=yes",
                  "backup.example", "echo", "OK"]
        result = subprocess.run(legacy, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 255, result.stderr)
        self.assertEqual(self.requests(), ["exec echo OK"],
                         "the old probe asked for a shell command")
        self.assertFalse(runner.classify_ssh_probe(result.returncode, result.stderr)[0])

        self.log.unlink()      # what the app asks now, on the same server:
        reachable, reason = runner.ssh_transport_ok(self.cfg)
        self.assertTrue(reachable, reason)
        self.assertEqual(self.requests(), ["subsystem sftp"])

    def test_settings_dialog_agrees_with_the_pre_flight(self):
        self.serve("sftp-only")
        self.assertTrue(runner.ssh_transport_ok(self.cfg)[0])
        self.assertEqual(self.dialog_verdict(), "Connection OK")


class MissingSubsystemTest(FakeSshHarness):
    """A server without the subsystem a backup needs is genuinely unusable."""

    def test_preflight_reports_it_and_says_why(self):
        self.serve("no-subsystem")
        reachable, reason = runner.ssh_transport_ok(self.cfg)
        self.assertFalse(reachable)
        self.assertIn("SFTP subsystem", reason)
        self.assertIn("Subsystem request failed", reason)
        self.assertFalse(runner.is_host_reachable(self.cfg))

    def test_settings_dialog_agrees_with_the_pre_flight(self):
        self.serve("no-subsystem")
        self.assertFalse(runner.ssh_transport_ok(self.cfg)[0])
        self.assertEqual(self.dialog_verdict(), "Connection FAILED")


class TransportConsistencyTest(FakeSshHarness):
    """Host, port, identity file and options must reach ssh identically whether
    the caller is restic, the pre-flight, or Test Connection."""

    def _assert_matches_backup(self, argv: list[str]):
        self.assertEqual(argv, transport_arguments(self.cfg),
                         "the probe must hand ssh exactly what restic hands ssh")

    def test_preflight_argv_equals_the_backups_argv(self):
        self.serve("normal")
        runner.ssh_transport_ok(self.cfg)
        self._assert_matches_backup(self.argv())
        self.assertIn("-s", self.argv())
        self.assertEqual(self.argv()[-1], "sftp")

    def test_test_connection_argv_equals_the_backups_argv(self):
        self.serve("normal")
        self.assertEqual(self.dialog_verdict(), "Connection OK")
        self._assert_matches_backup(self.argv())

    def test_spaces_in_the_identity_file_survive_both_calls(self):
        self.serve("normal")
        runner.ssh_transport_ok(self.cfg)
        self.dialog_verdict()
        keys = [self.argv()[i + 1] for i, a in enumerate(self.argv()) if a == "-i"]
        self.assertEqual(keys, [self.cfg.ssh_key, self.cfg.ssh_key])

    def test_ipv6_host_and_custom_port_are_used_unchanged(self):
        self.cfg.ssh_host = "[2001:db8::1]"
        self.cfg.ssh_port = 2222
        self.serve("normal")
        runner.ssh_transport_ok(self.cfg)
        argv = self.argv()
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-s") + 1], "2001:db8::1",
                         "ssh wants the bare address; restic's URL keeps brackets")
        self.assertEqual(self.requests(), ["subsystem sftp"])

    def test_keepalive_and_batch_options_travel_with_the_probe(self):
        self.serve("normal")
        runner.ssh_transport_ok(self.cfg)
        joined = " ".join(self.argv())
        for option in ("BatchMode=yes", "StrictHostKeyChecking=accept-new",
                       "ConnectTimeout=10", "ServerAliveInterval=60",
                       "ServerAliveCountMax=240"):
            self.assertIn(option, joined)
        self.assertNotIn("-F", self.argv(),
                         "ssh must still read the user's config: that is where "
                         "ProxyCommand / ProxyJump live")

    def test_stdin_is_never_handed_to_the_subsystem_session(self):
        self.serve("normal")
        runner.ssh_transport_ok(self.cfg)
        self.dialog_verdict()
        self.assertEqual(self.stdin_targets(), ["/dev/null", "/dev/null"],
                         "a subsystem session forwards stdin to the remote "
                         "sftp-server: the tray's keyboard must not be wired to it")


class SessionAndFailureModesTest(FakeSshHarness):
    """Exit codes that do not mean a broken transport must not gate backups."""

    def test_subsystem_session_with_an_odd_exit_is_still_reachable(self):
        self.serve("session-odd")
        reachable, reason = runner.ssh_transport_ok(self.cfg)
        self.assertTrue(reachable, reason)
        self.assertEqual(self.requests(), ["subsystem sftp"])

    def test_connection_refused_is_unreachable(self):
        self.serve("refused")
        reachable, reason = runner.ssh_transport_ok(self.cfg)
        self.assertFalse(reachable)
        self.assertIn("Connection refused", reason)
        self.assertEqual(self.dialog_verdict(), "Connection FAILED")

    def test_authentication_failure_is_unreachable(self):
        self.serve("auth")
        reachable, reason = runner.ssh_transport_ok(self.cfg)
        self.assertFalse(reachable)
        self.assertIn("Permission denied", reason)
        self.assertEqual(self.dialog_verdict(), "Connection FAILED")

    def test_a_stuck_server_times_out_instead_of_hanging_the_worker(self):
        # The fake really sleeps: only the app's own wall-clock bound makes the
        # pre-flight finite, and the timeout must not come back as "reachable".
        self.serve("stuck")
        reachable, reason = runner.ssh_transport_ok(self.cfg, timeout=1)
        self.assertFalse(reachable)
        self.assertIn("timed out", reason)


if __name__ == "__main__":
    unittest.main()
