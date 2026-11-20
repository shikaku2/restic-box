"""Repository-init threading tests (issue #11).

collect() reads GTK widgets and must only run on the main thread. The worker
receives a plain Config, reports back through GLib.idle_add, handles runner
exceptions, and the button re-enables only after completion. Review fix #2:
the completion handoff also survives setup/diagnostic failures (repo_url
raising before restic ever runs).
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_config as cfg_mod  # noqa: E402
import restic_settings  # noqa: E402


def make_dialog():
    dlg = restic_settings.SettingsDialog.__new__(restic_settings.SettingsDialog)
    dlg._cfg = cfg_mod.Config(backend="local", repo_path="/repo-x",
                              # Keep the test hermetic: the diagnostics in
                              # _do_init_repo report the password file, and a
                              # default path would point at the real user's.
                              password_file=os.path.join(
                                  tempfile.gettempdir(), "restic-box-test-pw"))

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

    dlg._lbl_init = Label()
    dlg._btn_init = Button()
    dlg.collect = lambda: dlg._cfg
    return dlg


class InitRepoThreadTest(unittest.TestCase):
    def test_collect_runs_on_clicking_thread_not_worker(self):
        dlg = make_dialog()
        collect_threads = []

        def fake_collect():
            collect_threads.append(threading.current_thread().name)
            return dlg._cfg

        dlg.collect = fake_collect
        started = []

        def fake_thread(target, args=(), daemon=False):
            started.append((target, args))
            return mock.Mock()

        with mock.patch.object(restic_settings.threading, "Thread",
                               new=fake_thread):
            dlg._init_repo(None)

        main = threading.current_thread().name
        self.assertEqual(collect_threads, [main],
                         "collect() must run on the main (click) thread")
        target, args = started[0]
        self.assertEqual(target, dlg._do_init_repo)  # bound-method identity: ==, not is
        self.assertEqual(len(args), 1)
        self.assertIsInstance(args[0], cfg_mod.Config)
        self.assertFalse(dlg._btn_init.sensitive, "button disabled during init")
        self.assertEqual(dlg._lbl_init.text, "Initializing…")

    def test_worker_reports_success_through_idle_add_and_reenables(self):
        dlg = make_dialog()
        with mock.patch.object(restic_settings.runner, "init_repo",
                               new=lambda cfg, on_output=None: (0, "")), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            dlg._do_init_repo(dlg._cfg)
        self.assertEqual(dlg._lbl_init.text, "Init OK")
        self.assertTrue(dlg._btn_init.sensitive)

    def test_worker_reports_failure(self):
        dlg = make_dialog()
        with mock.patch.object(restic_settings.runner, "init_repo",
                               new=lambda cfg, on_output=None: (1, "")), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            dlg._do_init_repo(dlg._cfg)
        self.assertIn("Init failed (rc=1)", dlg._lbl_init.text)
        self.assertTrue(dlg._btn_init.sensitive, "a failed init may be retried")

    def test_worker_exception_is_reported_not_swallowed(self):
        """e.g. invalid SSH fields raise ValueError at command-build time."""
        dlg = make_dialog()

        def boom(cfg, on_output=None):
            raise ValueError("invalid ssh_host")

        with mock.patch.object(restic_settings.runner, "init_repo", new=boom), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            dlg._do_init_repo(dlg._cfg)  # must not raise into the thread
        self.assertIn("Init failed: invalid ssh_host", dlg._lbl_init.text)
        self.assertTrue(dlg._btn_init.sensitive)

    def test_diagnostic_failure_before_restic_still_completes_ui(self):
        """Review fix #2: repo_url() ran BEFORE the worker's try; if the
        settings were invalid enough for URL building to raise, the thread
        died and the Initialize button stayed disabled forever."""
        dlg = make_dialog()
        idle_calls = []

        def bad_url(cfg):
            raise ValueError("Enter a valid SSH hostname or IP address.")

        def never_called(cfg, on_output=None):
            raise AssertionError("init_repo must not run after setup failed")

        with mock.patch.object(restic_settings.runner, "repo_url", new=bad_url), \
             mock.patch.object(restic_settings.runner, "init_repo", new=never_called), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: idle_calls.append((fn, a))):
            dlg._do_init_repo(dlg._cfg)   # must not raise into the worker thread

        # exactly one completion handoff to the main thread
        self.assertEqual(len(idle_calls), 1)
        fn, args = idle_calls[0]
        self.assertEqual(fn.__name__, "_init_repo_done")
        # performing the handoff reports the error and re-enables the dialog
        fn(args[0])
        self.assertIn("Enter a valid SSH hostname", dlg._lbl_init.text)
        self.assertTrue(dlg._btn_init.sensitive,
                        "a setup failure must leave the dialog retryable")

    def test_password_diagnostic_failure_also_completes_ui(self):
        dlg = make_dialog()

        def bad_path(cfg):
            raise ValueError("password file must not be empty")

        with mock.patch.object(restic_settings.cfg_mod, "password_file_path",
                               new=bad_path), \
             mock.patch.object(restic_settings.runner, "init_repo",
                               new=lambda cfg, on_output=None: (0, "")), \
             mock.patch.object(restic_settings.GLib, "idle_add",
                               new=lambda fn, *a, **k: fn(*a, **k)):
            dlg._do_init_repo(dlg._cfg)
        self.assertIn("password file must not be empty", dlg._lbl_init.text)
        self.assertTrue(dlg._btn_init.sensitive)


if __name__ == "__main__":
    unittest.main()
