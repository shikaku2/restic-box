"""Retention scope & grouping tests (review fix #3).

Documented policy (run_retention_forget + the settings retention tab), with
the grouping/window behaviour verified here against real restic rather than
taken from the man page:

- OWNERSHIP: restic-box owns the WHOLE repository it is configured with.
  Automatic retention sends no --host/--path/--tag filter, so the age window
  applies to every snapshot in that repository, including snapshots stored by
  other tools or hosts. A --path filter would be worse: whole groups outside
  the current settings would never be considered again and would leak forever.
- GROUPING: restic groups snapshots by (host, paths) BY DEFAULT. Tags join the
  grouping only via an explicit --group-by tag, which this app never passes.
  The per-run restic-box-run=… tags are run identities for the snapshot
  browser's bydate/ views (see tests/test_views_runs.py), nothing more.
- WINDOW BASE: --keep-within is measured per group, from that group's own
  newest snapshot. A group that stops receiving snapshots therefore keeps its
  newest snapshot indefinitely — a source path removed from the settings has
  to be forgotten by hand (snapshot browser).
- Duration units: y/m/d/h only (m = month); restic rejects finer units.

The unit tests pin the argv so no filter or --group-by can appear silently.
The end-to-end tests drive real restic against a throwaway LOCAL repository in
a temp directory — never a real repository, host, or credential.
"""
import os
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


class RetentionArgvTest(unittest.TestCase):
    """The whole-repository scope must stay exactly as documented: filters and
    grouping changes are forbidden by policy."""

    def _captured_args(self, cfg):
        captured = {}

        def fake_run(args, c, on_output=None, on_proc=None):
            captured["args"] = list(args)
            return 0, ""

        with mock.patch.object(runner, "run_restic", new=fake_run):
            runner.run_retention_forget(cfg)
        return captured.get("args")

    def test_whole_repo_scope_carries_no_ownership_filters(self):
        cfg = cfg_mod.Config(backend="local", repo_path="/repo-x",
                             retention_enabled=True, retention_years=1,
                             retention_months=2, retention_days=3,
                             retention_hours=4)
        args = self._captured_args(cfg)
        self.assertEqual(args, ["forget", "--keep-within", "1y2m3d4h", "--prune"])
        for forbidden in ("--host", "--path", "--tag", "--group-by",
                          "--keep-last", "--keep-hourly", "--keep-daily"):
            self.assertNotIn(forbidden, args,
                             f"{forbidden} would change the documented retention "
                             "scope/grouping without review")

    def test_zero_duration_launches_nothing(self):
        cfg = cfg_mod.Config(backend="local", repo_path="/repo-x",
                             retention_enabled=True)  # every component zero
        with mock.patch.object(runner, "run_restic",
                               side_effect=AssertionError("must not run")):
            rc, out = runner.run_retention_forget(cfg)
        self.assertEqual((rc, out), (runner.RC_OK, ""))

    def test_documented_grouping_is_host_paths_not_host_tag_paths(self):
        """The review caught the old docs claiming tags are part of restic's
        default grouping; they are not (default is host,paths)."""
        doc = " ".join((runner.run_retention_forget.__doc__ or "").split())
        self.assertIn("(host, paths)", doc)
        self.assertIn("--group-by tag", doc)
        self.assertNotIn("(host, tag, paths)", doc,
                         "restic's DEFAULT grouping is host,paths — "
                         "tags only when --group-by tag is requested")

    def test_documented_window_is_per_group_from_the_groups_own_newest(self):
        """Verified against real restic: the window is not measured from the
        repository-wide newest snapshot, and an abandoned group is NOT aged
        out automatically. The docs must say so."""
        doc = " ".join((runner.run_retention_forget.__doc__ or "").split())
        self.assertIn("group's own newest snapshot", doc)
        self.assertIn("abandoned", doc)

    def test_settings_ui_states_the_ownership_policy(self):
        """Fix #3 asked for the policy to be documented for the user, not just
        internally."""
        src = (Path(__file__).resolve().parent.parent / "restic_settings.py").read_text(
            encoding="utf-8")
        tab = src[src.index("def _retention_tab"):src.index("# --- collect ---")]
        tab = " ".join(tab.split())
        self.assertIn("whole repository", tab)
        self.assertIn("(host, paths)", tab)
        self.assertIn("--group-by", tab)
        self.assertIn("remove it with Forget + Prune", tab)


@unittest.skipUnless(shutil.which("restic"), "needs the restic executable")
class RetentionSemanticsE2ETest(unittest.TestCase):
    """Real restic, throwaway local repository.

    Every snapshot gets an explicit --time, so the outcome depends only on
    restic's grouping/window rules, not on the wall clock:

      group A  rb-host      src-a  09:00 and 11:00   -> 09:00 forgotten (2h gap)
      group B  unrelated    src-b  08:00 and 10:30   -> 08:00 forgotten: a
                                          foreign-host snapshot ages out too
                                          (whole-repository ownership)
      group C  rb-host      src-c  12:00 only        -> kept
      group E  rb-host      src-e  08:00 and 10:10   -> 08:00 forgotten, 10:10
                                          kept forever: a group with no newer
                                          snapshot never ages out by itself

    Window: 1h (retention_hours=1), which is what retention_duration builds.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-retention-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pw = self.tmp / "pw"
        self.pw.write_text("test-password\n")
        os.chmod(self.pw, 0o600)
        self.repo = self.tmp / "repo"
        self.cfg = cfg_mod.Config(backend="local", repo_path=str(self.repo),
                                  password_file=str(self.pw),
                                  retention_enabled=True, retention_hours=1)
        rc, out = runner.init_repo(self.cfg)
        self.assertEqual(rc, 0, out)

    def _backup(self, subdir, host, when):
        src = self.tmp / subdir
        src.mkdir(exist_ok=True)
        (src / "f.txt").write_text(f"{subdir} @ {when}\n")
        result = subprocess.run(
            ["restic", "--repo", str(self.repo), "--password-file", str(self.pw),
             "--no-cache", "backup", "--host", host, "--time", when, str(src)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _seed(self):
        self._backup("src-a", "rb-host", "2026-11-14 09:00:00")
        self._backup("src-a", "rb-host", "2026-11-14 11:00:00")
        self._backup("src-b", "unrelated-host", "2026-11-14 08:00:00")
        self._backup("src-b", "unrelated-host", "2026-11-14 10:30:00")
        self._backup("src-c", "rb-host", "2026-11-14 12:00:00")
        self._backup("src-e", "rb-host", "2026-11-14 08:00:00")
        self._backup("src-e", "rb-host", "2026-11-14 10:10:00")

    def _rows(self):
        """(host, path, 'YYYY-MM-DD HH:MM:SS') -> snapshot id."""
        return {(s.get("hostname"), tuple(s.get("paths") or ()),
                 s.get("time", "")[:19].replace("T", " ")): s["id"]
                for s in runner.run_snapshots_json(self.cfg)}

    def test_window_and_ownership_across_hosts_and_path_groups(self):
        self._seed()
        before = self._rows()
        self.assertEqual(len(before), 7, "setup: 7 snapshots in 4 (host,paths) groups")

        rc, out = runner.run_retention_forget(self.cfg)
        self.assertEqual(rc, runner.RC_OK, out)
        self.assertEqual(runner.retention_duration(self.cfg), "1h")
        self.assertIn("within 1h", out)

        after = self._rows()
        paths = {p for _, p, _ in after}
        hosts = {h for h, _, _ in after}

        # Out-of-window snapshots are forgotten in EVERY group, restic-box's own
        # and the unrelated machine's alike: the forget command carries no
        # --host/--path filter (whole-repository ownership).
        self.assertNotIn(("rb-host", (str(self.tmp / "src-a"),), "2026-11-14 09:00:00"), after)
        self.assertNotIn(("unrelated-host", (str(self.tmp / "src-b"),), "2026-11-14 08:00:00"),
                         after, "whole-repo ownership: a foreign out-of-window snapshot ages out too")

        # Each group's newest survives, foreign hosts included: ownership is
        # scope, not a vendetta against other machines.
        self.assertIn(("rb-host", (str(self.tmp / "src-a"),), "2026-11-14 11:00:00"), after)
        self.assertIn(("unrelated-host", (str(self.tmp / "src-b"),), "2026-11-14 10:30:00"), after)
        self.assertIn(("rb-host", (str(self.tmp / "src-c"),), "2026-11-14 12:00:00"), after)
        self.assertIn("unrelated-host", hosts)

        self.assertEqual(paths, {(str(self.tmp / p),) for p in ("src-a", "src-b", "src-c", "src-e")},
                         "no group disappears entirely: the window prunes within groups")
        self.assertEqual(len(after), 4, out)

    def test_abandoned_path_group_never_ages_out_automatically(self):
        """The documented limitation, pinned as behaviour: group E's newest
        (10:10) is older than group A's and group C's snapshots, yet it is kept
        — the window is measured from each group's own newest. A source path
        removed from the settings therefore needs a manual forget."""
        self._backup("src-e", "rb-host", "2026-11-14 08:00:00")
        self._backup("src-e", "rb-host", "2026-11-14 10:10:00")
        self._backup("src-c", "rb-host", "2026-11-14 12:00:00")

        rc, _out = runner.run_retention_forget(self.cfg)
        self.assertEqual(rc, runner.RC_OK)

        after = self._rows()
        kept_e = [t for h, p, t in after if h == "rb-host" and p == (str(self.tmp / "src-e"),)]
        self.assertEqual(kept_e, ["2026-11-14 10:10:00"],
                         "the group's older snapshot is pruned, its newest stays "
                         "(this is the leak the settings tab warns about)")

        # …and a manual forget from the snapshot browser does remove it.
        snap_id = after[("rb-host", (str(self.tmp / "src-e"),), "2026-11-14 10:10:00")]
        rc, out = runner.run_forget(self.cfg, snap_id)
        self.assertEqual(rc, runner.RC_OK, out)
        self.assertFalse(any("src-e" in p for _, p, _ in self._rows()),
                         "a manual forget removes the abandoned group")

    def test_repo_integrity_after_whole_repo_forget_prune(self):
        """--prune ran together with the forgets; the survivors must still form
        a verifiable repository."""
        self._backup("src", "rb-host", "2026-11-14 10:00:00")
        self._backup("src", "rb-host", "2026-11-14 13:00:00")
        rc, out = runner.run_retention_forget(self.cfg)
        self.assertEqual(rc, runner.RC_OK, out)
        self.assertEqual(len(self._rows()), 1)
        rc, out = runner.run_check_full(self.cfg)
        self.assertEqual(rc, runner.RC_OK, out)


if __name__ == "__main__":
    unittest.main()
