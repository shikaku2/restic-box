"""SSH transport freshness tests (issue #5) + the reachability probe
(review fixes #4 and #7).

Connection settings must reach the actual restic commands, immediately:
explicit ssh args in every command, no permanently-appended ~/.ssh/config
alias, validation of hostile field values, and one-time commenting-out (never
deletion) of the stale alias older versions wrote.

Fix #4: the pre-flight gate asks ssh itself (same transport options as the
backup) instead of opening a raw TCP socket, so ProxyCommand/ProxyJump
configurations are no longer falsely rejected.

Fix #7: the probe asks for the SFTP subsystem — the one thing restic needs —
instead of running a trivial remote command, so an account that permits SFTP
but forbids remote command execution is no longer rejected either. There is now
one transport builder, used by restic, the probe and the settings dialog.

Nothing here ever connects: subprocess is mocked and the assertions are about
argv and exit codes.
"""
import dataclasses
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


class SSHCommandTest(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_mod.Config(
            backend="sftp",
            ssh_host="backup.example",
            ssh_user="backup-user",
            ssh_port=23,
            ssh_key="/tmp/key with 'quotes' and spaces",
            repo_path="/backups/repository",
            password_file="/tmp/password",
        )

    def transport_arguments(self, cfg):
        command = runner._base_cmd(cfg)
        option = command[command.index("-o") + 1]
        self.assertTrue(option.startswith("sftp.command="))
        return shlex.split(option.split("=", 1)[1])

    def test_key_path_survives_command_serialization(self):
        transport = self.transport_arguments(self.cfg)
        key = transport[transport.index("-i") + 1]
        self.assertEqual(key, self.cfg.ssh_key)

    def test_changed_settings_are_used_immediately(self):
        first = self.transport_arguments(self.cfg)

        self.cfg.ssh_host = "replacement.example"
        self.cfg.ssh_user = "replacement-user"
        self.cfg.ssh_port = 2222
        self.cfg.ssh_key = "/tmp/new key"

        second = self.transport_arguments(self.cfg)
        self.assertNotEqual(first, second)
        self.assertEqual(second[second.index("-p") + 1], "2222")
        self.assertEqual(second[second.index("-l") + 1], "replacement-user")
        self.assertEqual(second[second.index("-i") + 1], "/tmp/new key")
        self.assertEqual(second[-3:], ["-s", "replacement.example", "sftp"])
        # the URL also tracks the current settings — no cached alias anywhere
        self.assertIn("replacement-user@replacement.example",
                     runner.repo_url(self.cfg))

    def test_ensure_ssh_config_never_writes_user_files(self):
        with mock.patch("builtins.open") as open_file:
            runner.ensure_ssh_config(self.cfg)
        open_file.assert_not_called()

    def test_ssh_config_is_never_read_either(self):
        # HEAD's ensure_ssh_config read ~/.ssh/config; the new one must not
        # (so tests and runs cannot touch a real user's SSH config).
        with mock.patch.object(Path, "read_text") as read_text:
            runner.ensure_ssh_config(self.cfg)
        read_text.assert_not_called()

    def test_ipv6_transport_and_repository_url(self):
        self.cfg.ssh_host = "[2001:db8::1]"
        self.assertEqual(
            runner.repo_url(self.cfg),
            "sftp:backup-user@[2001:db8::1]:/backups/repository",
        )
        self.assertEqual(
            runner.ssh_command(self.cfg)[-3:],
            ["-s", "2001:db8::1", "sftp"],
        )

    def test_option_like_hostname_is_rejected(self):
        self.cfg.ssh_host = "-oProxyCommand=unexpected"
        with self.assertRaises(ValueError):
            runner._base_cmd(self.cfg)

    def test_newline_in_ssh_field_is_rejected(self):
        self.cfg.ssh_user = "user\nProxyCommand unexpected"
        with self.assertRaises(ValueError):
            runner._base_cmd(self.cfg)

    def test_unknown_backend_is_rejected(self):
        self.cfg.backend = "typo"
        with self.assertRaises(ValueError):
            runner.repo_url(self.cfg)

    def test_empty_user_and_key_are_allowed(self):
        # ssh then uses the local username and default identities/agent
        self.cfg.ssh_user = ""
        self.cfg.ssh_key = ""
        command = self.transport_arguments(self.cfg)
        self.assertNotIn("-l", command)
        self.assertNotIn("-i", command)
        self.assertEqual(
            runner.repo_url(self.cfg),
            "sftp:backup.example:/backups/repository",
        )

    def test_keepalive_options_present(self):
        # restic documentation: long runs need ServerAliveInterval/CountMax
        # on the ssh connection or idle servers drop it.
        command = self.transport_arguments(self.cfg)
        joined = " ".join(command)
        self.assertIn("ServerAliveInterval=60", joined)
        self.assertIn("ServerAliveCountMax=240", joined)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("StrictHostKeyChecking=accept-new", joined)

    def test_local_backend_has_no_sftp_options(self):
        cfg = cfg_mod.Config(backend="local", repo_path="/srv/repo",
                             password_file="/tmp/pw")
        command = runner._base_cmd(cfg)
        self.assertNotIn("-o", command)
        self.assertEqual(command[2], "/srv/repo")

    def test_rclone_backend_url(self):
        cfg = cfg_mod.Config(backend="rclone", rclone_remote="dropbox:",
                             repo_path="/repo", password_file="/tmp/pw")
        self.assertEqual(runner.repo_url(cfg), "rclone:dropbox:/repo")

    def test_connection_test_uses_same_transport(self):
        # Fix #7: there is only one transport builder. The probe the settings
        # dialog and the pre-flight run IS the argv restic is given as
        # -o sftp.command, subsystem request included.
        probe = runner.ssh_probe_command(self.cfg)
        self.assertEqual(probe, self.transport_arguments(self.cfg),
                         "the probe must be the backup's own ssh invocation")
        self.assertEqual(probe[0], "ssh")
        self.assertEqual(probe[probe.index("-p") + 1], "23")
        self.assertEqual(probe[probe.index("-l") + 1], "backup-user")
        self.assertEqual(probe[probe.index("-i") + 1], self.cfg.ssh_key)
        self.assertEqual(probe[-3:], ["-s", "backup.example", "sftp"])


class ReachabilityProbeTest(unittest.TestCase):
    """Review fix #4 + fix #7: the pre-flight gate is the backup's own ssh
    invocation — same transport options, asking for the SFTP subsystem.

    Only OpenSSH understands ProxyCommand / ProxyJump / Match blocks / per-host
    IdentityFile, so a socket probe could refuse a configuration whose real
    restic connection works fine. Likewise, an SSH account can permit SFTP while
    refusing remote command execution, so a probe that runs `echo OK` refuses a
    repository restic can back up. The probe delegates the whole transport
    question to ssh and reads the answer from ssh's exit status, using the same
    rule the settings dialog's Test Connection uses.
    subprocess is always mocked: no connection is made in this suite.
    """

    def setUp(self):
        self.cfg = cfg_mod.Config(
            backend="sftp", ssh_host="backup.example", ssh_user="backup-user",
            ssh_port=23, ssh_key="/tmp/backup key",
            repo_path="/backups/repository", password_file="/tmp/password",
        )
        self.launched = []
        self.kwargs = {}

    def _mock_run(self, returncode=0, stdout="", stderr=""):
        def fake(command, **kwargs):
            self.launched.append(list(command))
            self.kwargs.update(kwargs)
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        patcher = mock.patch.object(runner.subprocess, "run", new=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _options(self, argv):
        """(-o value) pairs, in order."""
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]

    def test_probe_reuses_the_backups_transport_options(self):
        self._mock_run()
        ok, _reason = runner.ssh_transport_ok(self.cfg)
        self.assertTrue(ok)
        probe = self.launched[0]

        # the ssh command restic itself would run, from -o sftp.command=…
        base = runner._base_cmd(self.cfg)
        option = base[base.index("-o") + 1]
        backup = shlex.split(option.split("=", 1)[1])

        self.assertEqual(probe, backup,
                         "fix #7: the probe is the backup's own ssh invocation, "
                         "nothing added and nothing dropped")
        self.assertEqual(probe[0], "ssh")
        self.assertEqual(self._options(probe), self._options(backup),
                         "the probe must carry exactly the options the backup uses")
        for flag, value in (("-p", "23"), ("-l", "backup-user"), ("-i", "/tmp/backup key")):
            self.assertEqual(probe[probe.index(flag) + 1], value)
        self.assertEqual(probe[-3:], ["-s", "backup.example", "sftp"],
                         "the probe must request the SFTP subsystem restic needs")
        self.assertNotIn("echo", probe,
                         "the probe must not ask for a remote command: an SFTP-only "
                         "account legitimately refuses those")
        self.assertNotIn("-F", probe,
                         "the probe must not bypass the user's ssh config: that is "
                         "where ProxyCommand/ProxyJump live")

    def test_probe_closes_stdin(self):
        # A subsystem session forwards stdin to the remote sftp-server, so the
        # probe must not leave the tray process's own stdin attached to it.
        self._mock_run()
        runner.ssh_transport_ok(self.cfg)
        self.assertEqual(self.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_probe_does_not_bypass_the_users_ssh_config(self):
        # ProxyCommand / ProxyJump / Match blocks live in the user's ssh config.
        # The probe must let ssh read it (no -F override) — the raw TCP pre-flight
        # this replaces could not see any of that (review fix #4). The guard that
        # the runner keeps no socket probe of its own lives in
        # tests/test_suite_hygiene.py.
        self._mock_run()
        runner.ssh_transport_ok(self.cfg)
        probe = self.launched[0]
        self.assertNotIn("-F", probe)
        self.assertEqual(probe[-3:], ["-s", "backup.example", "sftp"])

    def test_unreachable_host_gives_a_useful_reason(self):
        self._mock_run(returncode=255,
                       stderr="ssh: connect to host backup.example port 23: "
                              "Connection timed out")
        ok, reason = runner.ssh_transport_ok(self.cfg)
        self.assertFalse(ok)
        self.assertIn("Connection timed out", reason)
        self.assertFalse(runner.is_host_reachable(self.cfg))

    def test_auth_failure_is_unreachable(self):
        self._mock_run(returncode=255,
                       stderr="backup-user@backup.example: Permission denied (publickey)")
        ok, reason = runner.ssh_transport_ok(self.cfg)
        self.assertFalse(ok)
        self.assertIn("Permission denied", reason)

    def test_host_key_refusal_is_unreachable(self):
        self._mock_run(returncode=255,
                       stderr="Host key verification failed.")
        self.assertFalse(runner.ssh_transport_ok(self.cfg)[0])

    def test_subsystem_session_ending_oddly_is_still_reachable(self):
        # sftp-server exits with its own status when the probe closes its input;
        # that says nothing about the transport. Only ssh's own 255 (and auth
        # failures) mean the connection is unusable.
        self._mock_run(returncode=1, stderr="")
        ok, reason = runner.ssh_transport_ok(self.cfg)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")
        self.assertTrue(runner.is_host_reachable(self.cfg))

    def test_server_without_the_sftp_subsystem_is_reported_as_unusable(self):
        # The failure mode the old `echo OK` probe could not tell apart from a
        # working connection: ssh authenticated, then the subsystem request was
        # refused. restic cannot use this repository either.
        self._mock_run(returncode=255,
                       stderr="Subsystem request failed on channel 0")
        ok, reason = runner.ssh_transport_ok(self.cfg)
        self.assertFalse(ok)
        self.assertIn("SFTP subsystem", reason)
        self.assertIn("Subsystem request failed", reason)

    def test_timeout_is_reported_not_raised(self):
        def explode(command, **kwargs):
            self.launched.append(list(command))
            raise subprocess.TimeoutExpired(command, timeout=kwargs["timeout"])

        patcher = mock.patch.object(runner.subprocess, "run", new=explode)
        patcher.start()
        self.addCleanup(patcher.stop)
        ok, reason = runner.ssh_transport_ok(self.cfg)
        self.assertFalse(ok)
        self.assertIn("timed out", reason)

    def test_local_and_rclone_backends_launch_nothing(self):
        patcher = mock.patch.object(
            runner.subprocess, "run",
            side_effect=AssertionError("no SSH probe for a non-SFTP backend"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(runner.ssh_transport_ok(
            cfg_mod.Config(backend="local", repo_path="/srv/repo"))[0])
        self.assertTrue(runner.ssh_transport_ok(
            cfg_mod.Config(backend="rclone", rclone_remote="dbx:", repo_path="/repo"))[0])
        self.assertEqual(self.launched, [])

    def test_hostile_host_is_rejected_before_any_probe(self):
        cfg = dataclasses.replace(self.cfg, ssh_host="-oProxyCommand=evil")
        with self.assertRaises(ValueError):
            runner.ssh_transport_ok(cfg)
        self.assertEqual(self.launched, [],
                         "never hand an option-looking host to ssh")


MANAGED_BLOCK = (
    "# restic-box managed entry\n"
    "Host restic-storagebox\n"
    "  HostName old.example\n"
    "  User old-user\n"
    "  Port 23\n"
    "  IdentityFile /home/a/.ssh/id_ed25519\n"
    "  StrictHostKeyChecking accept-new\n"
    "  ServerAliveInterval 60\n"
)

# Exactly what the cleanup must leave in its place: the same lines, each one
# only prefixed with "# " (commenting out, never deleting).
DISABLED_BLOCK = (
    "# restic-box managed entry (commented out by restic-box: settings are "
    "now applied directly, keeping this entry for reference)\n"
    "# Host restic-storagebox\n"
    "#   HostName old.example\n"
    "#   User old-user\n"
    "#   Port 23\n"
    "#   IdentityFile /home/a/.ssh/id_ed25519\n"
    "#   StrictHostKeyChecking accept-new\n"
    "#   ServerAliveInterval 60\n"
)


class SshConfigHarness(unittest.TestCase):
    """Every alias test runs against a throwaway HOME: the real ~/.ssh/config
    is never read or written by the suite."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-ssh-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.tmp / ".ssh").mkdir()
        self.ssh_cfg = self.tmp / ".ssh" / "config"

    def _write(self, text):
        data = text.encode("utf-8") if isinstance(text, str) else text
        self.ssh_cfg.write_bytes(data)   # byte-exact: no newline translation
        return self.ssh_cfg.read_bytes()

    def _assert_untouched(self, before: bytes):
        self.assertFalse(runner.disable_legacy_ssh_alias())
        self.assertEqual(self.ssh_cfg.read_bytes(), before)
        self._assert_no_temp_files()

    def _assert_no_temp_files(self):
        leftovers = [p.name for p in (self.tmp / ".ssh").iterdir()
                     if p.name != "config"]
        self.assertEqual(leftovers, [], "temp files must not survive a cleanup run")

    def _active_text(self):
        """The part of the config OpenSSH actually honours: every line that is
        not blank and not a comment."""
        active = [ln for ln in self.ssh_cfg.read_text().splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
        return "\n".join(active)


class LegacyAliasDisableTest(SshConfigHarness):

    def test_managed_block_commented_out_user_blocks_intact(self):
        self._write(
            "Host other\n  User someone\n\n"
            + MANAGED_BLOCK +
            "\nHost keepme\n  Port 2222\n"
        )
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertEqual(
            self.ssh_cfg.read_text(),
            "Host other\n  User someone\n\n" + DISABLED_BLOCK
            + "\nHost keepme\n  Port 2222\n",
        )
        # ssh now honours only the user's own blocks: the alias is inert...
        self.assertNotIn("restic-storagebox", self._active_text())
        # ...but nothing was deleted: the old entry is still fully present.
        for body_line in MANAGED_BLOCK.splitlines()[1:]:
            self.assertIn(body_line.strip(), self.ssh_cfg.read_text())

    def test_trailing_managed_block_commented_in_place(self):
        self._write("Host other\n  User someone\n\n" + MANAGED_BLOCK + "\n")
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertEqual(self.ssh_cfg.read_text(),
                         "Host other\n  User someone\n\n" + DISABLED_BLOCK + "\n")

    def test_no_marker_means_no_change(self):
        original = ("Host *\n  ServerAliveInterval 15\n\n"
                    "Host myhost\n  User me\n")
        self._write(original)
        self.assertFalse(runner.disable_legacy_ssh_alias())
        self.assertEqual(self.ssh_cfg.read_text(), original)

    def test_second_call_is_noop(self):
        self._write("Host keepme\n  Port 2222\n\n" + MANAGED_BLOCK)
        self.assertTrue(runner.disable_legacy_ssh_alias())
        after = self.ssh_cfg.read_text()
        self.assertFalse(runner.disable_legacy_ssh_alias())
        self.assertEqual(self.ssh_cfg.read_text(), after)

    def test_missing_config_file_is_fine(self):
        self.assertFalse(runner.disable_legacy_ssh_alias())

    def test_user_host_named_like_alias_is_not_touched(self):
        # only the marked block is commented out; a user's own Host of the same
        # name (without our marker) stays active.
        self._write("Host restic-storagebox\n  HostName manual.example\n")
        self.assertFalse(runner.disable_legacy_ssh_alias())
        self.assertIn("manual.example", self._active_text())
        self.assertIn("Host restic-storagebox", self._active_text(),
                      "a user's own alias of the same name must keep working")

    def test_already_commented_body_is_not_double_commented(self):
        # A user who commented the block out by hand: their single "#" stays.
        self._write(
            "# restic-box managed entry\n"
            "Host restic-storagebox\n"
            "#   HostName old.example\n"
            "  User old-user\n"
        )
        self.assertTrue(runner.disable_legacy_ssh_alias())
        after = self.ssh_cfg.read_text()
        self.assertNotIn("##", after, "comment markers must not stack")
        self.assertIn("#   HostName old.example\n", after)
        self.assertIn("#   User old-user\n", after)

    def test_blank_lines_inside_and_around_the_block_are_preserved(self):
        self._write(
            "Host other\n  User someone\n\n"
            "# restic-box managed entry\n"
            "Host restic-storagebox\n"
            "  HostName old.example\n"
            "\n"
            "  Port 23\n"
            "\nHost keepme\n  Port 2222\n"
        )
        self.assertTrue(runner.disable_legacy_ssh_alias())
        after = self.ssh_cfg.read_text()
        self.assertEqual(after.count("\n\n"), 3,
                         "blank lines must stay exactly where they were")
        self.assertNotIn("restic-storagebox", self._active_text())
        self.assertIn("Host keepme\n  Port 2222", after)


class LegacyAliasSafetyTest(SshConfigHarness):
    """The cleanup must never damage a hand-edited configuration."""

    def test_marker_not_followed_by_our_host_leaves_file_untouched(self):
        # A user comment that happens to match our marker, followed by their
        # own block: commenting anything here would disable user configuration.
        before = self._write(
            "# restic-box managed entry\n"
            "Host somebody-else\n  HostName private.example\n  Port 2202\n"
        )
        self._assert_untouched(before)

    def test_marker_at_end_of_file_with_no_block_after_it(self):
        before = self._write("Host keepme\n  Port 2222\n\n# restic-box managed entry\n")
        self._assert_untouched(before)

    def test_marker_comment_inside_a_user_block_is_preserved(self):
        before = self._write(
            "Host keepme\n"
            "  # restic-box managed entry\n"   # indented user comment
            "  User me\n"
            "  Port 2222\n"
        )
        self._assert_untouched(before)

    def test_user_directives_after_the_block_are_not_commented_out(self):
        # A comment line is byte-preserved, but the indented line under it was
        # dangling on OUR alias (no Host header of its own): left uncommented
        # it would become a global ssh option once the alias is disabled.
        self._write(
            MANAGED_BLOCK +
            "\n# my own note about this file\n"
            "  StrictHostKeyChecking yes\n"     # dangling on restic-storagebox
            "Host keepme\n  Port 2222\n"
        )
        self.assertTrue(runner.disable_legacy_ssh_alias())
        after = self.ssh_cfg.read_text()
        self.assertNotIn("restic-storagebox", self._active_text())
        self.assertIn("\n# my own note about this file\n", after,
                      "the user's own comment must survive untouched")
        self.assertNotIn("StrictHostKeyChecking yes", self._active_text(),
                         "a line dangling on the alias must not leak as a global option")
        self.assertIn("#   StrictHostKeyChecking yes", after)
        self.assertIn("Host keepme\n  Port 2222", self._active_text())

    def test_two_managed_blocks_are_both_commented_out(self):
        # Repeated installs could have appended the entry more than once.
        self._write(
            "Host other\n  User someone\n\n" + MANAGED_BLOCK +
            "\n" + MANAGED_BLOCK +
            "\nHost keepme\n  Port 2222\n"
        )
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertEqual(
            self.ssh_cfg.read_text(),
            "Host other\n  User someone\n\n" + DISABLED_BLOCK
            + "\n" + DISABLED_BLOCK
            + "\nHost keepme\n  Port 2222\n",
        )

    def test_extra_whitespace_in_our_host_header_still_commented(self):
        self._write(
            "Host keepme\n  Port 2222\n\n"
            "# restic-box managed entry\n"
            "Host   restic-storagebox\n"
            "  HostName old.example\n"
            "  User old-user\n"
        )
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertEqual(
            self.ssh_cfg.read_text(),
            "Host keepme\n  Port 2222\n\n"
            + DISABLED_BLOCK.splitlines()[0] + "\n"
            + "# Host   restic-storagebox\n"
            + "#   HostName old.example\n"
            + "#   User old-user\n",
        )

    def test_crlf_configuration_keeps_its_line_endings(self):
        self._write(
            "Host other\r\n  User someone\r\n\r\n"
            + MANAGED_BLOCK.replace("\n", "\r\n") +
            "\r\nHost keepme\r\n  Port 2222\r\n"
        )
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertEqual(
            self.ssh_cfg.read_bytes(),
            b"Host other\r\n  User someone\r\n\r\n"
            + DISABLED_BLOCK.replace("\n", "\r\n").encode("utf-8")
            + b"\r\nHost keepme\r\n  Port 2222\r\n",
        )

    def test_undecodable_config_is_ignored_not_fatal(self):
        # A stray byte in someone's comment must not crash the app at startup.
        self._write(b"Host keepme\n  User caf\xe9\n\n" + MANAGED_BLOCK.encode())
        self.assertFalse(runner.disable_legacy_ssh_alias())   # must not raise
        self.assertIn(b"caf\xe9", self.ssh_cfg.read_bytes(), "file left as it is")

    def test_read_only_directory_means_no_change(self):
        self._write("Host keepme\n  Port 2222\n\n" + MANAGED_BLOCK)
        os.chmod(self.tmp / ".ssh", 0o555)
        self.addCleanup(os.chmod, self.tmp / ".ssh", 0o755)
        try:  # confirm the directory really refuses writes (root can override mode)
            probe = self.tmp / ".ssh" / ".probe"
            probe.touch()
            probe.unlink()
        except OSError:
            pass
        else:
            self.skipTest("0555 not enforced for this user (CAP_DAC_OVERRIDE)")
        self.assertFalse(runner.disable_legacy_ssh_alias())
        self.assertIn("restic-storagebox", self._active_text(),
                      "an unwritable directory must leave the file exactly as it was")

    def test_file_mode_is_preserved_but_loose_write_bits_are_dropped(self):
        self._write("Host keepme\n  Port 2222\n\n" + MANAGED_BLOCK)
        os.chmod(self.ssh_cfg, 0o644)
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertEqual(self.ssh_cfg.stat().st_mode & 0o777, 0o644)

        os.chmod(self.ssh_cfg, 0o666)
        self._write("Host keepme\n  Port 9999\n\n" + MANAGED_BLOCK)
        os.chmod(self.ssh_cfg, 0o666)
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertEqual(self.ssh_cfg.stat().st_mode & 0o777, 0o644,
                         "ssh refuses a group/world writable config: never create one")

    def test_symlinked_config_is_updated_through_the_symlink(self):
        real = self.tmp / "vault" / "ssh-config"     # e.g. an encrypted vault
        real.parent.mkdir()
        real.write_text("Host keepme\n  Port 2222\n\n" + MANAGED_BLOCK)
        os.symlink(real, self.ssh_cfg)

        self.assertTrue(runner.disable_legacy_ssh_alias())
        self.assertTrue(os.path.islink(self.ssh_cfg),
                        "a symlink must stay a symlink, not become a plain file")
        self.assertEqual(real.read_text(),
                         "Host keepme\n  Port 2222\n\n" + DISABLED_BLOCK)
        self.assertEqual([p.name for p in real.parent.iterdir()], ["ssh-config"],
                         "no temp file left behind next to the real file either")

    def test_directory_named_config_is_not_fatal(self):
        os.mkdir(self.ssh_cfg)
        self.assertFalse(runner.disable_legacy_ssh_alias())  # must not raise

    def test_binary_lookalike_is_not_fatal(self):
        self._write(bytes(range(256)))
        self.assertFalse(runner.disable_legacy_ssh_alias())  # must not raise

    def test_disable_leaves_no_temp_files(self):
        self._write("Host keepme\n  Port 2222\n\n" + MANAGED_BLOCK)
        self.assertTrue(runner.disable_legacy_ssh_alias())
        self._assert_no_temp_files()

    def test_no_config_file_is_created_when_absent(self):
        self.assertFalse(runner.disable_legacy_ssh_alias())
        self.assertFalse(self.ssh_cfg.exists(), "cleanup must not create a config")
        self._assert_no_temp_files()


if __name__ == "__main__":
    unittest.main()
