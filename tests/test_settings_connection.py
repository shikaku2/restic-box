"""Test Connection consistency (issue #5 + review fixes #4 and #7).

The settings dialog's Test Connection and the worker's reachability pre-flight
must be the same question with the same answer:

- same command: runner.ssh_probe_command() — which IS the ssh invocation the
  backup runs, including the request for the server's SFTP subsystem (fix #7:
  no remote command, because an SFTP-only account legitimately refuses those);
- same verdict rule: runner.classify_ssh_probe() — ssh's own 255 or an auth
  failure means unreachable, any other exit code means the transport and the
  subsystem channel came up;
- no raw TCP probing anywhere (fix #4 replaced it, so ProxyCommand / ProxyJump
  configurations are not falsely rejected).

subprocess is mocked throughout: nothing here connects.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_runner as runner  # noqa: E402
import restic_settings  # noqa: E402

APP = Path(__file__).resolve().parent.parent


def make_dialog(cfg):
    dlg = restic_settings.SettingsDialog.__new__(restic_settings.SettingsDialog)
    dlg._cfg = cfg
    logs = []
    dlg._on_log = logs.append
    dlg._logs = logs

    class Label:
        def __init__(self):
            self.text = None

        def set_text(self, t):
            self.text = t

    class Button:
        def __init__(self):
            self.sensitive = True

        def set_sensitive(self, v):
            self.sensitive = v

    dlg._lbl_test = Label()
    dlg._btn_test = Button()
    return dlg


def sftp_cfg():
    return cfg_mod.Config(backend="sftp", ssh_host="backup.example",
                          ssh_user="backup-user", ssh_port=23,
                          ssh_key="/tmp/backup key", repo_path="/backups/repo",
                          password_file="/tmp/password")


class TestConnectionRuleTest(unittest.TestCase):
    def _run_test_connection(self, returncode, stderr=""):
        dlg = make_dialog(sftp_cfg())
        launched = []
        seen = {}

        def fake_run(command, **kwargs):
            launched.append(list(command))
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, returncode, "", stderr)

        with mock.patch.object(restic_settings.subprocess, "run", new=fake_run), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            dlg._do_sftp_test_connection(dlg._cfg)
        return dlg, launched, seen

    def test_dialog_builds_the_probe_from_the_backups_transport(self):
        dlg, launched, seen = self._run_test_connection(0, "")
        probe = launched[0]
        self.assertEqual(len(launched), 1)
        self.assertEqual(probe, runner.ssh_probe_command(dlg._cfg),
                         "Test Connection must use the exact probe command the "
                         "worker's pre-flight uses")
        self.assertEqual(probe[-3:], ["-s", "backup.example", "sftp"],
                         "fix #7: the test asks for the SFTP subsystem a backup needs")
        self.assertNotIn("echo", probe,
                         "…and it does not ask for a remote command")
        self.assertEqual(seen.get("stdin"), subprocess.DEVNULL,
                         "a subsystem session must not be handed the app's stdin")
        self.assertEqual(dlg._lbl_test.text, "Connection OK")
        self.assertTrue(dlg._btn_test.sensitive, "the button re-enables either way")

    def test_ssh_error_is_reported_as_failure(self):
        dlg, _, _ = self._run_test_connection(255,
                                              "ssh: connect to host backup.example "
                                              "port 23: Connection refused")
        self.assertEqual(dlg._lbl_test.text, "Connection FAILED")
        self.assertTrue(any("Connection refused" in line for line in dlg._logs),
                        "the user must see WHY the connection failed")

    def test_auth_failure_is_reported_as_failure(self):
        dlg, _, _ = self._run_test_connection(255,
                                              "backup-user@backup.example: "
                                              "Permission denied (publickey)")
        self.assertEqual(dlg._lbl_test.text, "Connection FAILED")
        self.assertTrue(any("Permission denied" in line for line in dlg._logs))

    def test_missing_sftp_subsystem_is_reported_as_failure(self):
        # The one thing a backup needs; if the server will not hand it out the
        # repository is unusable, and the message has to say so.
        dlg, _, _ = self._run_test_connection(255, "Subsystem request failed on channel 0")
        self.assertEqual(dlg._lbl_test.text, "Connection FAILED")
        self.assertTrue(any("SFTP subsystem" in line for line in dlg._logs), dlg._logs)

    def test_storage_box_without_a_shell_still_passes(self):
        # Hetzner and similar boxes authenticate, offer SFTP, and refuse
        # arbitrary commands. The connection is fine — and since fix #7 the
        # test never asks for a command, so it says so too.
        dlg, _, _ = self._run_test_connection(1, "")
        self.assertEqual(dlg._lbl_test.text, "Connection OK")

    def test_timeout_is_reported_not_raised(self):
        dlg = make_dialog(sftp_cfg())

        def explode(command, **kwargs):
            raise subprocess.TimeoutExpired(command, timeout=kwargs["timeout"])

        with mock.patch.object(restic_settings.subprocess, "run", new=explode), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            dlg._do_sftp_test_connection(dlg._cfg)
        self.assertEqual(dlg._lbl_test.text, "Connection FAILED")
        self.assertTrue(any("timed out" in line for line in dlg._logs))

    def test_both_call_sites_share_one_verdict_rule(self):
        """No private copy of the exit-code logic may survive in the dialog:
        divergence between Test Connection and the pre-flight is exactly what
        issue #5 was about."""
        src = (APP / "restic_settings.py").read_text(encoding="utf-8")
        segment = src[src.index("def _do_sftp_test_connection"):
                      src.index("def _do_rclone_test_connection")]
        self.assertIn("runner.ssh_probe_command", segment)
        self.assertIn("runner.classify_ssh_probe", segment)
        self.assertNotIn("255", segment,
                         "the ssh-255 rule lives in runner.classify_ssh_probe only")
        self.assertNotIn("Permission denied", segment,
                         "auth-failure wording lives in the runner only")

    def test_the_worker_gate_and_the_dialog_ask_the_same_question(self):
        cfg = sftp_cfg()
        verdicts = {}

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 255, "",
                                              "ssh: connect to host backup.example "
                                              "port 23: No route to host")

        with mock.patch.object(runner.subprocess, "run", new=fake_run), \
             mock.patch.object(restic_settings.subprocess, "run", new=fake_run), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            verdicts["preflight"] = runner.ssh_transport_ok(cfg)
            dlg = make_dialog(cfg)
            dlg._do_sftp_test_connection(cfg)
            verdicts["dialog"] = dlg._lbl_test.text

        self.assertFalse(verdicts["preflight"][0])
        self.assertIn("No route to host", verdicts["preflight"][1])
        self.assertEqual(verdicts["dialog"], "Connection FAILED",
                         "a pre-flight skip and a Test Connection must agree")


if __name__ == "__main__":
    unittest.main()
