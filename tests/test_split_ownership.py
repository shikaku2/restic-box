"""Explicit plan-ownership tests (review oversight: "the inode/exclude matcher
is not merely cosmetic").

plan_stable() must decide coverage from what surviving units *declare* they
own (dir/rest units their path, group units their surviving paths, file
units their path) — never by re-interpreting generated carve patterns
through fnmatch. fnmatch stays only as the documented sizing/drift
approximation for the user's own excludes, which no unit owns.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restic_split as split  # noqa: E402

MB = 1024 * 1024
T = 10 * MB  # threshold; drift_factor 0.5 -> drift 5 MB everywhere below


def _stable(prev, size, children, bigfiles, smallfiles, root="/src",
            excludes=(), drift_factor=0.5):
    return split.plan_stable(prev, root, list(excludes), T, drift_factor,
                             size, children, bigfiles, smallfiles,
                             exists=lambda p: True)


def _esc(p: str) -> str:
    return split.escape_pattern(p)


def _units(plan):
    return {(u.kind, u.path) for u in plan}


class StaleExclusionOwnershipTest(unittest.TestCase):
    def test_dir_to_file_replacement_is_recarved_not_masked_by_stale_exclude(self):
        """/src/big used to be a carved DIRECTORY; it is now a big FILE. The
        old dir unit no longer owns the path, so its exclusion must not mask
        the new file from planning: a file unit is carved for it."""
        prev = [
            split.Unit("/src", [_esc("/src/big")], 60.0, "rest"),
            split.Unit("/src/big", [], 40.0, "dir"),  # gone: /src/big is now a file
        ]
        size = {"/src": 100 * MB}  # scan: only directories land in `size`
        children = {"/src": []}
        bigfiles = {"/src": [("/src/big", 12 * MB)]}

        plan = _stable(prev, size, children, bigfiles, {})

        self.assertIn(("file", "/src/big"), _units(plan),
                      "the recreated big file must get its own unit instead of "
                      "hiding behind the vanished dir unit's exclusion")
        rest = [u for u in plan if u.kind == "rest"][0]
        self.assertIn(_esc("/src/big"), rest.excludes,
                      "the rest unit must exclude the carved file (exactly once)")
        self.assertEqual(rest.excludes.count(_esc("/src/big")), 1)
        # Coverage: rest unit's allocated share excludes the carved file.
        self.assertLess(rest.size, size["/src"])

    def test_orphan_exclude_with_no_owning_unit_is_released_and_recarved(self):
        """A saved plan carries an exclusion whose unit vanished (stale or
        hand-edited plan). A NEW big directory at that path must be planned,
        not skipped because an exclude glob mentions it."""
        prev = [split.Unit("/src", [_esc("/src/gone")], 70.0, "rest")]
        size = {"/src": 100 * MB, "/src/gone": 30 * MB}
        children = {"/src": ["/src/gone"]}

        plan = _stable(prev, size, children, {}, {})

        self.assertIn(("dir", "/src/gone"), _units(plan),
                      "an exclusion no unit owns must not block re-carving")
        rest = [u for u in plan if u.kind == "rest"][0]
        self.assertIn(_esc("/src/gone"), rest.excludes)

    def test_user_base_exclude_is_respected_and_never_carved(self):
        """The user's own exclude (no owning unit) still classifies the entry
        as carved-off: planning must not create a unit for an excluded path."""
        prev = [split.Unit("/src", ["/src/private"], 70.0, "rest")]
        size = {"/src": 100 * MB, "/src/private": 30 * MB}
        children = {"/src": ["/src/private"]}

        plan = _stable(prev, size, children, {}, {}, excludes=["/src/private"])

        self.assertNotIn("/src/private", {u.path for u in plan},
                         "a user-excluded path must never get a backup unit")
        rest = [u for u in plan if u.kind == "rest"][0]
        self.assertIn("/src/private", rest.excludes,
                      "the user's exclude must survive reconciliation")

    def test_nested_carve_hierarchy_is_not_resplit_spuriously(self):
        """Deep-hierarchy invariant: /src rest -> /src/a rest -> /src/a/b dir.
        Each ancestor's coverage check finds the intermediate owned by a live
        unit, so untouched levels keep their exact path sets (parent-snapshot
        continuity) and no duplicate unit is created for /src/a/b."""
        prev = [
            split.Unit("/src", [_esc("/src/a"), _esc("/src/a/b")], 40.0, "rest"),
            split.Unit("/src/a", [_esc("/src/a/b")], 20.0, "rest"),
            split.Unit("/src/a/b", [], 40.0, "dir"),
        ]
        size = {"/src": 100 * MB, "/src/a": 60 * MB, "/src/a/b": 40 * MB}
        children = {"/src": ["/src/a"], "/src/a": ["/src/a/b"]}

        plan = _stable(prev, size, children, {}, {})

        self.assertEqual(len(plan), 3)
        paths = [u.path for u in plan]
        self.assertEqual(paths, ["/src", "/src/a", "/src/a/b"],
                         "nested rest units must be reused in stored order")
        by_path = {u.path: u for u in plan}
        self.assertEqual(by_path["/src"].excludes,
                         [_esc("/src/a"), _esc("/src/a/b")])
        self.assertEqual(by_path["/src/a"].excludes, [_esc("/src/a/b")])
        # /src/a/b is backed up exactly once — no duplicate carved unit.
        self.assertEqual([u for u in plan if u.path == "/src/a/b"], [by_path["/src/a/b"]])

    def test_group_membership_is_explicit_ownership(self):
        """Group units own their surviving member paths; a member that grew
        past the threshold is pulled out (old exclusion semantics preserved
        via the new unit), and the rest unit's coverage view follows the same
        explicit ownership data."""
        members = ["/src/f1", "/src/f2", "/src/f3"]
        prev = [
            split.Unit("/src", [_esc(m) for m in members], 5.0, "rest"),
            split.Unit("/src/f1", [], 8.0, "group", paths=list(members)),
        ]
        size = {"/src": 25 * MB}
        children = {"/src": []}
        # f2 grew into its own territory (>= threshold).
        bigfiles = {"/src": [("/src/f1", 3 * MB), ("/src/f2", 12 * MB),
                             ("/src/f3", 3 * MB)]}

        plan = _stable(prev, size, children, bigfiles, {})

        units = _units(plan)
        self.assertIn(("file", "/src/f2"), units,
                      "a grown group member must be planned standalone")
        rest = [u for u in plan if u.kind == "rest"][0]
        self.assertIn(_esc("/src/f2"), rest.excludes,
                      "the pulled-out member stays excluded from the rest unit")
        group = [u for u in plan if u.kind == "group"]
        self.assertTrue(group and set(group[0].paths) == {"/src/f1", "/src/f3"},
                        "the group keeps its remaining members")


if __name__ == "__main__":
    unittest.main()
