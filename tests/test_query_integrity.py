"""Query integrity and friendly-view swap tests (fix #6, restic-box-fixes2.md).

Two hazards, one story:

1. Bounded output retention made every query problem look like an empty
   repository. `restic snapshots` failing on the SFTP transport, a listing torn
   off mid-transfer, or a listing simply bigger than the retained output tail
   all answered `[]` — identical to "this repository has no snapshots".
2. The friendly-view builders deleted the existing `latest/` and `bydate/`
   trees *before* building the replacements, so hazard (1) — or any failure in
   between — turned a view the user could browse into a missing or empty one.

The fixes: queries stream and parse record by record (a listing of any size is
parsed complete rather than truncated) and raise `QueryError` when a command
fails or its output is incomplete, reserving `[]` for genuinely empty results.
View rebuilds happen in a `.building/` staging tree and are renamed into place
only when complete; a failed rebuild discards the staging tree and leaves the
previous view exactly as it was.

No network or ssh: query children are fakes, and the real-restic checks use a
throwaway local repository.
"""
import json
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
import restic_views as views  # noqa: E402
from tests.fakeproc import fake_popen  # noqa: E402

# A stand-in for MAX_QUERY_OUTPUT_CHARS / MAX_QUERY_LINE_CHARS, small enough
# that the listings these tests build overflow it on purpose: the point is that
# the retained-text path visibly truncates while the query path does not.
CAP = 4096


def cfg_local(repo="/repo-test", pw="/pw-test"):
    return cfg_mod.Config(backend="local", repo_path=repo, password_file=pw)


def snapshot_json(count: int) -> str:
    """One `restic snapshots --json` line describing *count* snapshots."""
    snaps = [{"id": f"{i:064x}", "short_id": f"{i:08x}",
              "time": f"2026-01-01T00:00:{i % 60:02d}Z",
              "paths": [f"/data/item{i:05d}"]} for i in range(count)]
    return json.dumps(snaps)


def node_lines(parent: str, count: int) -> list[str]:
    """Direct-child node records, as `restic ls --json` emits them."""
    lines = [json.dumps({"struct_type": "snapshot", "id": "a" * 64,
                         "short_id": "aaaaaaaa", "paths": [parent]})]
    for i in range(count):
        lines.append(json.dumps({"name": f"entry{i:05d}", "type": "file",
                                 "path": f"{parent}/entry{i:05d}",
                                 "struct_type": "node"}))
    return lines


class TruncationIsNotSilenceTest(unittest.TestCase):
    """Output larger than the retention cap is parsed complete, not truncated."""

    def setUp(self):
        self.cfg = cfg_local()

    def test_huge_snapshot_line_still_parses_complete(self):
        line = snapshot_json(120)
        self.assertGreater(len(line), CAP, "test fixture must exceed the cap")

        with mock.patch.object(runner, "MAX_QUERY_OUTPUT_CHARS", CAP), \
             mock.patch.object(runner, "MAX_QUERY_LINE_CHARS", CAP):
            with mock.patch.object(runner.subprocess, "Popen", new=fake_popen([line])):
                # the retained-text path demonstrably cannot carry this record…
                _rc, retained = runner.run_restic(["snapshots", "--json"], self.cfg)
                self.assertLess(len(retained), len(line))
            # …and the query path is not affected by it: every snapshot is
            # returned, with the same cap in force.
            with mock.patch.object(runner.subprocess, "Popen", new=fake_popen([line])):
                snaps = runner.run_snapshots_json(self.cfg)
        self.assertEqual(len(snaps), 120)
        self.assertEqual(snaps[-1]["paths"], ["/data/item00119"],
                         "the tail of the listing went missing")

    def test_long_ls_listing_returns_every_child(self):
        lines = node_lines("/data/files", 300)
        self.assertGreater(sum(len(x) for x in lines), CAP)

        with mock.patch.object(runner, "MAX_QUERY_OUTPUT_CHARS", CAP), \
             mock.patch.object(runner, "MAX_QUERY_LINE_CHARS", CAP):
            with mock.patch.object(runner.subprocess, "Popen", new=fake_popen(lines)):
                _rc, retained = runner.run_restic(["ls", "--json", "snap"], self.cfg)
                self.assertIn("[restic-box: dropped", retained,
                              "expected the retained-text path to drop lines")
            with mock.patch.object(runner.subprocess, "Popen", new=fake_popen(lines)):
                children = runner.run_ls_children(self.cfg, "snap", "/data/files")
        self.assertEqual(len(children), 300)
        self.assertEqual(children[-1][0], "entry00299")

    def test_huge_single_record_is_not_a_wall(self):
        # One child whose JSON record alone dwarfs the cap must survive:
        # streaming has no per-line excerpt in the parsed result.
        lines = node_lines("/data/files", 1)
        fat = json.dumps({"name": "x" * (3 * CAP), "type": "file",
                          "path": "/data/files/" + "x" * (3 * CAP),
                          "struct_type": "node"})
        with mock.patch.object(runner.subprocess, "Popen",
                               new=fake_popen(lines + [fat])):
            children = runner.run_ls_children(self.cfg, "snap", "/data/files")
        self.assertEqual(len(children), 2)
        self.assertTrue(children[1][0].startswith("xxxx"))


class QueryFailureIsVisibleTest(unittest.TestCase):
    """A failed or incomplete query raises; only an empty repository answers []."""

    def setUp(self):
        self.cfg = cfg_local()

    def _snaps(self, lines, returncode=0):
        with mock.patch.object(runner.subprocess, "Popen",
                               new=fake_popen(lines, returncode)):
            return runner.run_snapshots_json(self.cfg)

    def test_failed_snapshot_query_raises_not_empty(self):
        with self.assertRaises(runner.QueryError) as ctx:
            self._snaps(["Fatal: unable to open config file"], 1)
        self.assertEqual(ctx.exception.rc, 1)
        self.assertIn("failed", str(ctx.exception))

    def test_partial_exit_code_is_a_real_listing(self):
        snaps = [{"id": "a" * 64, "short_id": "aaaaaaaa", "paths": ["/data"]}]
        self.assertEqual(self._snaps([json.dumps(snaps)], runner.RC_PARTIAL), snaps)

    def test_garbage_output_is_not_an_empty_repository(self):
        with self.assertRaises(runner.QueryError) as ctx:
            self._snaps(["some ssh chatter", "and more"])
        self.assertIn("no JSON listing", str(ctx.exception))

    def test_torn_json_is_reported_not_swallowed(self):
        line = snapshot_json(50)[: 4000]          # cut mid-record, as a dropped
        # connection would leave it
        with self.assertRaises(runner.QueryError) as ctx:
            self._snaps([line])
        self.assertIn("unparsable", str(ctx.exception))

    def test_empty_array_is_a_successful_empty_repository(self):
        self.assertEqual(self._snaps(["[]"]), [])

    def test_failed_ls_raises(self):
        with mock.patch.object(runner.subprocess, "Popen",
                               new=fake_popen(["Fatal: snapshot does not exist"], 1)):
            with self.assertRaises(runner.QueryError):
                runner.run_ls_json(self.cfg, "deadbeef")
            with self.assertRaises(runner.QueryError):
                runner.run_ls_children(self.cfg, "deadbeef", "/data")

    def test_ls_without_records_is_incomplete_output(self):
        # restic always answers `ls` with at least the snapshot record: silence
        # means the listing never finished.
        with mock.patch.object(runner.subprocess, "Popen", new=fake_popen([])):
            with self.assertRaises(runner.QueryError):
                runner.run_ls_json(self.cfg, "aaaaaaaa")
            with self.assertRaises(runner.QueryError):
                runner.run_ls_children(self.cfg, "aaaaaaaa", "/data")

    def test_torn_ls_record_raises(self):
        lines = node_lines("/data", 3)
        lines.append('{"name": "bro')             # torn record at the end
        with mock.patch.object(runner.subprocess, "Popen", new=fake_popen(lines)):
            with self.assertRaises(runner.QueryError) as ctx:
                runner.run_ls_json(self.cfg, "aaaaaaaa")
        self.assertIn("unparsable", str(ctx.exception))

    def test_ssh_chatter_before_the_records_is_ignored(self):
        lines = ["Warning: Permanently added 'host' to the list of known hosts."]
        lines += node_lines("/data", 3)
        with mock.patch.object(runner.subprocess, "Popen", new=fake_popen(lines)):
            children = runner.run_ls_children(self.cfg, "aaaaaaaa", "/data")
        self.assertEqual(len(children), 3)

    def test_empty_directory_is_a_successful_empty_listing(self):
        only_header = [json.dumps({"struct_type": "snapshot", "id": "a" * 64,
                                   "short_id": "aaaaaaaa", "paths": ["/data/empty"]}),
                       json.dumps({"name": "empty", "type": "dir",
                                   "path": "/data/empty", "struct_type": "node"})]
        with mock.patch.object(runner.subprocess, "Popen", new=fake_popen(only_header)):
            self.assertEqual(runner.run_ls_children(self.cfg, "aaaaaaaa",
                                                    "/data/empty"), [])


@unittest.skipUnless(shutil.which("restic"), "restic not installed")
class RealResticQueryTest(unittest.TestCase):
    """The stream parsers against real restic, in a throwaway local repo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-query-e2e-")
        self.repo = os.path.join(self.tmp, "repo")
        self.pw = os.path.join(self.tmp, "pw")
        with open(self.pw, "w") as f:
            f.write("testpw\n")
        subprocess.run(["restic", "-q", "-r", self.repo, "--password-file", self.pw,
                        "init"], check=True)
        self.source = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.source, "sub"))
        os.makedirs(os.path.join(self.source, "vacant"))
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(self.source, name), "w") as f:
                f.write(name * 20)
        rc, _out, _added = runner.run_backup(
            cfg_local(self.repo, self.pw), cfg_mod.Directory(self.source))
        self.assertEqual(rc, 0)
        self.cfg = cfg_local(self.repo, self.pw)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _snapshot_ids(self):
        return [s["id"] for s in runner.run_snapshots_json(self.cfg)]

    def test_snapshots_query_reports_the_backup(self):
        snaps = runner.run_snapshots_json(self.cfg)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["paths"], [self.source])
        self.assertTrue(snaps[0]["id"])

    def test_ls_children_matches_the_snapshot(self):
        snap = self._snapshot_ids()[0]
        children = dict(runner.run_ls_children(self.cfg, snap, self.source))
        self.assertEqual(children, {"a.txt": False, "b.txt": False,
                                    "sub": True, "vacant": True})

    def test_empty_directory_lists_as_empty_not_failed(self):
        snap = self._snapshot_ids()[0]
        self.assertEqual(runner.run_ls_children(self.cfg, snap,
                                                self.source + "/vacant"), [])

    def test_ls_json_returns_the_whole_listing(self):
        snap = self._snapshot_ids()[0]
        entries = runner.run_ls_json(self.cfg, snap)
        paths = {e.get("path") for e in entries if e.get("struct_type") == "node"}
        self.assertIn(self.source + "/sub", paths)
        self.assertIn(self.source + "/a.txt", paths)

    def test_bogus_snapshot_raises_instead_of_listing_nothing(self):
        with self.assertRaises(runner.QueryError):
            runner.run_ls_json(self.cfg, "ffffffff")
        with self.assertRaises(runner.QueryError):
            runner.run_ls_children(self.cfg, "ffffffff", self.source)

    def test_empty_repository_lists_as_empty(self):
        empty_repo = os.path.join(self.tmp, "empty-repo")
        subprocess.run(["restic", "-q", "-r", empty_repo, "--password-file", self.pw,
                        "init"], check=True)
        self.assertEqual(
            runner.run_snapshots_json(cfg_local(empty_repo, self.pw)), [])


def _listing(root: Path) -> set[str]:
    """Relative paths of everything inside a published view."""
    out = set()
    for dirpath, dirs, files in os.walk(str(root)):
        for name in dirs + files:
            out.add(os.path.relpath(os.path.join(dirpath, name), str(root)))
    return out


def snap(snap_id: str, *paths: str) -> dict:
    return {"id": snap_id, "short_id": snap_id[:8], "time": "2026-01-01T00:00:00Z",
            "paths": list(paths)}


class ViewSwapTest(unittest.TestCase):
    """A rebuild only replaces a view once a complete replacement exists."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-view-swap-"))
        self.latest = self.tmp / "latest"
        self.bydate = self.tmp / "bydate"
        self.mount = self.tmp / "snapshots"
        (self.mount / "ids").mkdir(parents=True)
        self.logs: list[str] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_no_scratch(self):
        for published in (self.latest, self.bydate):
            for leftover in views.leftover_paths(published):
                self.assertFalse(leftover.exists(),
                                 f"{leftover.name} was left behind")

    def _build(self, snaps, ls_children=None):
        return views.build_latest(self.latest, self.mount, snaps,
                                  ls_children=ls_children, log=self.logs.append)

    def test_failed_corridor_listing_keeps_the_previous_view(self):
        self._build([snap("aaaa1111", "/mnt/a"), snap("bbbb2222", "/mnt/b")])
        before = _listing(self.latest)
        self.assertIn("mnt/b", before)

        def poisoned(snap_id, path):
            # an unknown directory: exactly what a failed query now means
            raise runner.QueryError("restic ls failed (exit 1)", rc=1)

        snaps = [snap("cccc3333", "/mnt/a"), snap("cccc3333", "/mnt/a/deep")]
        with self.assertRaises(runner.QueryError):
            self._build(snaps, ls_children=poisoned)

        self.assertEqual(_listing(self.latest), before,
                         "a failed rebuild destroyed the view it could not replace")
        self._assert_no_scratch()
        self.assertTrue(any("previous view kept" in m for m in self.logs), self.logs)

    def test_successful_rebuild_replaces_the_view_complete(self):
        self._build([snap("aaaa1111", "/mnt/a")])
        created = self._build([snap("bbbb2222", "/mnt/c"), snap("bbbb2222", "/mnt/d")])
        self.assertEqual(_listing(self.latest), {"mnt", "mnt/c", "mnt/d"})
        self.assertEqual(created, 2)
        self._assert_no_scratch()

    def test_successful_empty_listing_is_applied(self):
        # An empty repository is a *successful* query: latest/ legitimately
        # becomes empty, unlike a failed one (which raises instead).
        self._build([snap("aaaa1111", "/mnt/a")])
        self._build([])
        self.assertTrue(self.latest.is_dir())
        self.assertEqual(_listing(self.latest), set())
        self._assert_no_scratch()

    def test_published_view_stays_readonly(self):
        self._build([snap("aaaa1111", "/mnt/a")])
        self.assertEqual(self.latest.stat().st_mode & 0o777, 0o555)
        for dirpath, dirs, _files in os.walk(str(self.latest)):
            for name in dirs:
                self.assertEqual(os.stat(os.path.join(dirpath, name)).st_mode
                                 & 0o777, 0o555)

    def test_a_stale_staging_tree_from_a_crash_does_not_block_rebuilding(self):
        stale = views.staging_path(self.latest)
        stale.mkdir(parents=True)
        (stale / "orphan").mkdir()
        views.set_tree_readonly(stale, True)      # as a crash would leave it
        self._build([snap("aaaa1111", "/mnt/a")])
        self.assertEqual(_listing(self.latest), {"mnt", "mnt/a"})
        self._assert_no_scratch()

    def test_a_stale_backup_tree_from_a_crash_is_cleared(self):
        stale = views.backup_path(self.latest)
        stale.mkdir(parents=True)
        views.set_tree_readonly(stale, True)
        self._build([snap("aaaa1111", "/mnt/a")])
        self.assertEqual(_listing(self.latest), {"mnt", "mnt/a"})
        self._assert_no_scratch()

    def test_swap_failure_restores_the_previous_view(self):
        self._build([snap("aaaa1111", "/mnt/a"), snap("aaaa1111", "/mnt/b")])
        before = _listing(self.latest)
        real_replace = os.replace
        calls: list[tuple[str, str]] = []

        def flaky(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:      # the outgoing view is parked, but the
                # replacement never lands
                raise OSError(18, "Invalid cross-device link", src)
            return real_replace(src, dst)

        with mock.patch.object(views.os, "replace", new=flaky):
            with self.assertRaises(OSError):
                self._build([snap("cccc3333", "/mnt/z")])
        self.assertEqual(_listing(self.latest), before,
                         "the user's view must survive a failed swap")
        self._assert_no_scratch()

    def test_bydate_is_protected_the_same_way(self):
        snaps = [snap("aaaa1111", "/mnt/a"), snap("bbbb2222", "/mnt/b")]
        views.build_bydate(self.bydate, self.mount, snaps, log=self.logs.append)
        before = _listing(self.bydate)
        self.assertTrue(before)

        def poisoned(snap_id, path):
            raise runner.QueryError("restic ls failed (exit 1)", rc=1)

        nested = [snap("cccc3333", "/mnt/a"), snap("cccc3333", "/mnt/a/deep")]
        with self.assertRaises(runner.QueryError):
            views.build_bydate(self.bydate, self.mount, nested,
                               ls_children=poisoned, log=self.logs.append)
        self.assertEqual(_listing(self.bydate), before)
        self._assert_no_scratch()

    def test_bydate_rebuild_succeeds_and_swaps(self):
        views.build_bydate(self.bydate, self.mount, [snap("aaaa1111", "/mnt/a")])
        views.build_bydate(self.bydate, self.mount, [snap("bbbb2222", "/mnt/c")])
        listing = _listing(self.bydate)
        self.assertTrue(any(name.endswith("c") for name in listing), listing)
        self.assertFalse(any(name.endswith("a") for name in listing), listing)
        self._assert_no_scratch()


if __name__ == "__main__":
    unittest.main()
