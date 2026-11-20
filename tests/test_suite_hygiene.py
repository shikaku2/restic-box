"""Suite hygiene: tests must never need a network or a real ~/.ssh/config.

Three guarantees worth pinning down:

1. No test module may import a network primitive, and none may launch a real
   ssh/sftp/scp. Transport behaviour is verified from the argument lists restic
   would receive, never by connecting — except for the audited fake-ssh harness
   of review fix #7, which the audit permits only after checking that PATH is
   redirected and the fake provably shadows the real binary.
2. Normal app operation never reads or writes ~/.ssh/config — only the
   one-time legacy-alias cleanup may (commenting its own block out, never
   deleting it), and any test that runs it must redirect HOME to a throwaway
   directory.
3. The reachability pre-flight asks ssh itself (review fix #4), with the same
   transport options the backup uses — it is not a raw TCP probe of
   host:port, which could reject ProxyCommand/ProxyJump setups that work.
   Review fix #7: that invocation requests the SFTP subsystem rather than a
   remote command, so an SFTP-only account is not rejected either. Here it is
   checked from its argv and from ssh's exit code. The runner therefore needs
   no socket at all, which this module also asserts.
4. Review fix #6 writes diagnostics to disk (~/.config/restic-box/logs). Any
   test module that touches that log must redirect it to a throwaway directory
   with set_log_dir(); this module audits every test source so the suite can
   never create or append to the user's real log file.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TESTS = Path(__file__).resolve().parent
APP = TESTS.parent
SELF = Path(__file__).name

import restic_config as cfg_mod  # noqa: E402
import restic_runner as runner  # noqa: E402
from tests.fakeproc import FakeStdout  # noqa: E402

NETWORK_IMPORT = re.compile(r"^\s*(?:import|from)\s+(socket|urllib|http|requests|paramiko)\b",
                            re.MULTILINE)
NETWORK_CALL = re.compile(r"\b(create_connection|getaddrinfo|urlopen|socket\.\w+)\b")
REMOTE_ARGV = re.compile(r"\[\s*[\"'](ssh|sftp|scp|rsync|curl|wget)[\"']")
# Review fix #7: one test module runs the SSH pre-flight against a fake ssh
# placed earlier on PATH, which needs the name `ssh` in its argv lists. The
# audit permits that only when it can verify the fake shadows the real binary.
FAKE_SSH_MARKER = re.compile(r"\bFAKE_SSH\b")
PATH_SHADOW = re.compile(r"[\"']PATH[\"']\s*:")
SHADOW_ASSERT = re.compile(r"shutil\.which\(\s*[\"']ssh[\"']\s*\)")
LAUNCHER = re.compile(r"\b(?:subprocess\.(?:run|Popen|call|check_output|check_call)|os\.system)\s*\(")
# Review fix #6: the diagnostic log lives in the user's real config directory
# unless a test redirects it. Two patterns, and any test module matching the
# first must also match the second.
DIAG_LOG_USE = re.compile(r"\brestic_diag_log\b|\bdiag_log\.")
DIAG_LOG_REDIRECT = re.compile(r"\bset_log_dir\s*\(")

MANAGED = (
    "Host ssh.example\n  ProxyCommand /usr/bin/cloudflared access ssh --hostname %h\n"
    "  User aaron\n  Port 12369\n"
    "Host 192.168.1.*\n    IPQoS none\n"
    "\n# restic-box managed entry\n"
    "Host restic-storagebox\n"
    "  HostName old.example\n  User u565388\n  Port 23\n"
    "  IdentityFile /home/a/.ssh/id_ed25519\n"
    "  StrictHostKeyChecking accept-new\n  ServerAliveInterval 60\n"
)


def _test_sources():
    for path in sorted(TESTS.glob("*.py")):
        if path.name != SELF:
            yield path, path.read_text(encoding="utf-8")


class NoNetworkTest(unittest.TestCase):
    def test_no_test_module_imports_network_primitives(self):
        offenders = [p.name for p, src in _test_sources() if NETWORK_IMPORT.search(src)]
        self.assertEqual(offenders, [], "test modules must not import socket/urllib/requests")

    def test_no_test_module_calls_network_primitives(self):
        offenders = [p.name for p, src in _test_sources() if NETWORK_CALL.search(src)]
        self.assertEqual(offenders, [],
                         "connection logic is checked from argv lists, not by connecting")

    def test_no_test_module_launches_ssh_like_commands(self):
        """Transports are verified as argument lists — with one audited exception.

        Review fix #7 needed the pre-flight exercised against server behaviours
        (SFTP subsystem present/absent, exec refused), which a mocked subprocess
        can only imitate. The exception: a module may run the name `ssh` when it
        creates a fake ssh in a throwaway directory, redirects PATH to it, and
        asserts the shadowing took effect. That way the audit checks the safety
        property instead of assuming it, and a real transport still cannot be
        launched from a test.
        """
        for path, src in _test_sources():
            match = REMOTE_ARGV.search(src)
            if match and FAKE_SSH_MARKER.search(src):
                self.assertTrue(PATH_SHADOW.search(src),
                                f"{path.name} runs a fake ssh without redirecting PATH")
                self.assertTrue(SHADOW_ASSERT.search(src),
                                f"{path.name} runs a fake ssh without asserting it "
                                "shadows the real binary")
                continue
            self.assertIsNone(match,
                              f"{path.name} launches {match.group(1) if match else ''}; "
                              "transports are verified as argument lists only")

    def test_subprocess_launches_are_local_helpers_only(self):
        """Every directly-spawned binary in the suite is a local helper: restic
        against a temporary local repository, or sleep/echo/python for
        process-control tests. (Indirect helpers like spawn(argv) are covered by
        the ssh-launcher check above.)"""
        allowed = {"restic", "sleep", "sh", "echo", "cat", "true", "python3", "sys"}
        for path, src in _test_sources():
            for m in LAUNCHER.finditer(src):
                first = re.match(r"\s*\[?\s*[\"']([\w./-]+)[\"']", src[m.end():m.end() + 60])
                if not first:
                    continue
                name = Path(first.group(1)).name
                self.assertIn(name, allowed,
                              f"{path.name} spawns an unexpected binary: {name}")


class DiagLogHygieneTest(unittest.TestCase):
    """Review fix #6 put a real file inside the user's ~/.config/restic-box.

    Tests are allowed to exercise it — but only against a throwaway directory.
    Without this audit a test that imported restic_diag_log and called note()
    would quietly create ~/.config/restic-box/logs/restic-box.log on the
    developer's machine, which is the kind of thing a suite must make impossible
    rather than merely discourage.
    """

    def test_every_module_that_touches_the_log_redirects_it(self):
        offenders = [path.name for path, src in _test_sources()
                     if DIAG_LOG_USE.search(src) and not DIAG_LOG_REDIRECT.search(src)]
        self.assertEqual(offenders, [],
                         "these test modules use the diagnostic log without calling "
                         "set_log_dir(<temporary directory>): " + ", ".join(offenders))

    def test_the_log_is_actually_covered(self):
        """Guards against the silent way out: deleting the redirect (or the
        whole test module) to satisfy the check above."""
        covered = sorted(path.name for path, src in _test_sources() if DIAG_LOG_USE.search(src))
        self.assertTrue(covered, "no test module exercises the diagnostic log at all")

    def test_the_log_in_force_while_tests_run_is_a_temporary_directory(self):
        """The suite-wide net in tests/__init__.py must be what is active here:
        if any module leaves the log pointing at the user's real
        ~/.config/restic-box/logs, this is where it shows up."""
        import restic_diag_log as diag_log

        active = diag_log.log_dir()
        self.assertNotEqual(active, cfg_mod.CONFIG_DIR / "logs",
                            "the suite is logging into the user's real config directory")
        self.assertIn(Path(tempfile.gettempdir()), active.parents,
                      f"the suite's diagnostic log is not in a temporary directory: {active}")

    def test_importing_the_log_module_does_not_write_to_the_users_home(self):
        """The log directory is resolved on first use, so merely importing the
        app (which the suite does constantly) must not create it. Checked in a
        child interpreter with HOME pointed at a throwaway directory."""
        fake_home = Path(tempfile.mkdtemp(prefix="rb-diag-home-"))
        try:
            env = dict(os.environ, HOME=str(fake_home))
            proc = subprocess.run(
                [sys.executable, "-c", "import restic_config, restic_diag_log"],
                cwd=str(APP), env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((fake_home / ".config" / "restic-box" / "logs").exists(),
                             "importing created the user's log directory")
        finally:
            shutil.rmtree(fake_home, ignore_errors=True)

    def test_the_app_only_reaches_the_log_through_its_public_api(self):
        """Every app-side use of the log must go through DiagLog's own bounded
        entry points. A module that constructed its own DiagLog, or opened the
        file itself, would write around the record cap and the rotation.

        LOG_FILE_NAME and log_dir() are allowed because restic_status quotes the
        path so a truncated line says where the full text is — that is display,
        not writing."""
        allowed = {"note", "record_oversized", "truncation_note", "log_dir",
                   "set_log_dir", "LOG_FILE_NAME", "active", "MAX_LOG_FILES",
                   "MAX_LOG_FILE_BYTES", "MAX_RECORD_CHARS", "NOTE_RESERVE"}
        offenders = []
        for path in sorted(APP.glob("restic_*.py"), key=lambda p: p.name):
            if path.name == "restic_diag_log.py":
                continue
            src = path.read_text(encoding="utf-8")
            for attr in set(re.findall(r"\bdiag_log\.([A-Za-z_][\w]*)", src)):
                if attr not in allowed:
                    offenders.append(f"{path.name}: diag_log.{attr}")
            if re.search(r"\bDiagLog\s*\(", src) or re.search(r"\bopen\s*\([^)]*restic-box\.log", src):
                offenders.append(f"{path.name}: builds its own log file")
        self.assertEqual(offenders, [],
                         "bypasses the diagnostic log's bounds: " + ", ".join(offenders))

    def test_the_readme_describes_the_log_that_actually_exists(self):
        """Documented limits and documented behaviour have to match the code:
        a user who trusts a README about where their diagnostics went is going to
        look there when a backup fails."""
        import restic_diag_log as diag_log
        import restic_status as status

        readme = (APP / "README.md").read_text(encoding="utf-8").replace("\u00a0", " ")
        self.assertIn(".config/restic-box/logs/restic-box.log", readme,
                      "the README must name the file the truncation notes name")

        def spaced(value: int) -> str:
            return f"{value:,}".replace(",", " ")

        self.assertIn(spaced(status.LogWindow.MAX_LOG_LINES), readme,
                      "README line cap disagrees with LogWindow.MAX_LOG_LINES")
        self.assertIn(spaced(status.LogWindow.MAX_LOG_LINE_CHARS), readme,
                      "README per-line cap disagrees with LogWindow.MAX_LOG_LINE_CHARS")
        self.assertIn(f"{diag_log.MAX_LOG_FILE_BYTES // (1024 * 1024)} MB", readme,
                      "README rotation size disagrees with MAX_LOG_FILE_BYTES")
        self.assertIn(f"{diag_log.MAX_RECORD_CHARS // (1024 * 1024)} MB", readme,
                      "README record cap disagrees with MAX_RECORD_CHARS")
        for index in range(1, diag_log.MAX_LOG_FILES):
            self.assertIn(f"`restic-box.log.{index}`", readme,
                          f"README omits rotated copy .{index} of MAX_LOG_FILES")
        self.assertIn("0700", readme, "the private directory is part of the promise")

    def test_the_app_has_one_logging_path_and_it_is_bounded_in_both_ways(self):
        src = (APP / "restic_indicator.py").read_text(encoding="utf-8")
        self.assertIn("diag_log.note(", src,
                      "the window mirrors every operational line to the file")
        self.assertIn("diag_log.record_oversized(",
                      (APP / "restic_runner.py").read_text(encoding="utf-8"),
                      "retention spills what it drops instead of discarding it")
        import restic_status as status
        self.assertTrue(hasattr(status.LogWindow, "MAX_LOG_LINE_CHARS"),
                        "the visible log needs a per-line bound as well as a line count")


class SshConfigUntouchedTest(unittest.TestCase):
    """~/.ssh/config is the user's own file: only the explicit cleanup may change it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-hygiene-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.tmp / ".ssh").mkdir()
        self.ssh_cfg = self.tmp / ".ssh" / "config"
        self.ssh_cfg.write_text(MANAGED)
        self.before = self.ssh_cfg.read_bytes()

    def sftp_cfg(self):
        return cfg_mod.Config(backend="sftp", ssh_host="backup.example", ssh_user="u565388",
                              ssh_port=23, ssh_key="/home/a/.ssh/id_ed25519",
                              repo_path="/backups/repo", password_file="/tmp/pw")

    def test_normal_operations_leave_the_config_byte_identical(self):
        cfg = self.sftp_cfg()
        lines = ['{"message_type": "summary", "data_added": 0, "files_new": 0}']

        class FakeProc:
            """See tests/fakeproc.py: stdout must be closable, because the
            runner closes the pipe it creates."""

            def __init__(self):
                self.stdout = FakeStdout(lines)
                self.returncode = 0

            def wait(self):
                return 0

        def fake_popen(*_args, **_kwargs):
            return FakeProc()

        def forbidden_probe(*_args, **_kwargs):
            raise AssertionError("normal operations must not run an SSH probe")

        with mock.patch.object(runner.subprocess, "Popen", new=fake_popen), \
             mock.patch.object(runner.subprocess, "run", new=forbidden_probe):
            runner.ensure_ssh_config(cfg)              # the pre-command hook
            runner.repo_url(cfg)
            runner.ssh_command(cfg)                    # the one transport builder
            runner.ssh_probe_command(cfg)              # …which is also the probe argv
            runner.run_backup(cfg, cfg_mod.Directory("/src"))
            runner.run_restic(["snapshots", "--json"], cfg)
            runner.run_retention_forget(cfg)

        self.assertEqual(self.ssh_cfg.read_bytes(), self.before,
                         "backups/queries must not modify the user's SSH config")
        self.assertEqual([p.name for p in (self.tmp / ".ssh").iterdir()], ["config"])

    def test_only_the_explicit_cleanup_changes_it(self):
        self.assertTrue(runner.disable_legacy_ssh_alias())
        after = self.ssh_cfg.read_text()
        active = "\n".join(ln for ln in after.splitlines()
                           if ln.strip() and not ln.strip().startswith("#"))
        self.assertNotIn("restic-storagebox", active,
                         "the alias must no longer be honoured by ssh")
        self.assertNotIn("old.example", active,
                         "the alias body must be commented out, not just the header")
        self.assertIn("# Host restic-storagebox", after,
                      "commenting out keeps the entry visible for the user")
        self.assertIn("ProxyCommand /usr/bin/cloudflared", active,
                      "the user's own blocks must stay active through the cleanup")
        self.assertIn("IPQoS none", active)

    def test_alias_tests_redirect_home(self):
        for path, src in _test_sources():
            if "legacy_ssh_alias" not in src:
                continue
            self.assertRegex(
                src, r"patch\.dict\(\s*os\.environ[^\n]*HOME",
                f"{path.name} calls the ssh-config cleanup without redirecting HOME")


class ReachabilityTest(unittest.TestCase):
    """The pre-flight transport check, verified without ever making a call.

    Review fix #4: the check is an ssh invocation carrying the backup's own
    transport options, not a raw TCP connect to host:port. OpenSSH is what knows
    about ProxyCommand / ProxyJump / Match blocks, so asking ssh is the only way
    the gate can agree with the real operation. Review fix #7: that invocation
    requests the SFTP subsystem — the thing a backup actually needs — instead of
    running a remote command an SFTP-only account would refuse. subprocess.run
    is mocked here; the suite still never opens a connection, and the runner no
    longer imports socket at all.
    """

    def setUp(self):
        self.calls = []

        def fake_run(command, **kwargs):
            self.calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        patcher = mock.patch.object(runner.subprocess, "run", new=fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sftp_cfg(self, host="backup.example", port=2222):
        return cfg_mod.Config(backend="sftp", ssh_host=host, ssh_user="u",
                              ssh_port=port, ssh_key="", repo_path="/r")

    def test_no_test_module_imports_socket(self):
        """The socket-based pre-flight is gone, so no test needs socket anymore."""
        offenders = [p.name for p, src in _test_sources() if NETWORK_IMPORT.search(src)]
        offenders.append(
            "restic_runner.py" if "import socket" in (APP / "restic_runner.py").read_text()
            else "")
        self.assertEqual([o for o in offenders if o], [],
                         "the runner must not keep a raw socket probe: it rejected "
                         "ProxyCommand/ProxyJump configurations that worked")

    def test_local_and_rclone_backends_never_probe(self):
        self.assertTrue(runner.is_host_reachable(cfg_mod.Config(backend="local",
                                                                repo_path="/srv/repo")))
        self.assertTrue(runner.is_host_reachable(cfg_mod.Config(backend="rclone",
                                                                rclone_remote="dbx:",
                                                                repo_path="/repo")))
        self.assertEqual(self.calls, [])

    def test_sftp_probe_uses_the_configured_host_and_port(self):
        cfg = self.sftp_cfg()
        self.assertTrue(runner.is_host_reachable(cfg))
        self.assertEqual(len(self.calls), 1)
        probe = self.calls[0]
        self.assertEqual(probe[0], "ssh")
        self.assertEqual(probe[probe.index("-p") + 1], "2222")
        self.assertEqual(probe[probe.index("-l") + 1], "u")
        self.assertEqual(probe[-3:], ["-s", "backup.example", "sftp"],
                         "the probe must reach the configured host through the "
                          "SFTP subsystem a backup would use")
        self.assertNotIn("-F", probe,
                         "the probe may not bypass the ssh config that carries "
                         "ProxyCommand/ProxyJump")

    def test_unreachable_host_reports_false_instead_of_raising(self):
        def refuse(command, **kwargs):
            self.calls.append(list(command))
            # what ssh prints when the host is down / refused / unauthenticated
            return subprocess.CompletedProcess(command, 255, "",
                                              "ssh: connect to host down.example "
                                              "port 23: No route to host")

        patcher = mock.patch.object(runner.subprocess, "run", new=refuse)
        patcher.start()
        self.addCleanup(patcher.stop)
        reachable, reason = runner.ssh_transport_ok(self.sftp_cfg("down.example", 23))
        self.assertFalse(reachable)
        self.assertIn("No route to host", reason)

    def test_invalid_host_is_rejected_before_any_probe(self):
        cfg = self.sftp_cfg("-oProxyCommand=x")
        with self.assertRaises(ValueError):
            runner.is_host_reachable(cfg)
        self.assertEqual(self.calls, [], "never probe a host we would pass as an option")


if __name__ == "__main__":
    unittest.main()
