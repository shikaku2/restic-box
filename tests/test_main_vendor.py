"""Vendor path precedence tests (review oversight on main.py).

main.py documents: AppImage bundle > user vendor > dev checkout. Building the
path by repeatedly inserting at sys.path[0] reverses the candidate list, so
the candidates must be inserted in reverse to end up in the documented order.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN_PY = (REPO / "main.py").read_text(encoding="utf-8")


def run_scenario(appdir: bool, user: bool, checkout: bool):
    """Materialise the chosen vendor layouts and import main.py in a child
    process; return which vendor directory its probe module resolved from and
    the vendor order that ended up in sys.path."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        home = tmp / "home"
        checkout_dir = tmp / "checkout"
        app = tmp / "appimage"

        checkout_dir.mkdir()
        home.mkdir()
        (checkout_dir / "main.py").write_text(MAIN_PY, encoding="utf-8")
        # Stand in for the indicator import so the child stays cheap.
        (checkout_dir / "restic_indicator.py").write_text("def main():\n    pass\n",
                                                          encoding="utf-8")

        layouts = []
        if appdir:
            d = app / "usr" / "share" / "restic-box" / "vendor"
            d.mkdir(parents=True)
            layouts.append(("appimage", d))
        if user:
            d = home / ".local" / "share" / "restic-box" / "vendor"
            d.mkdir(parents=True)
            layouts.append(("user", d))
        if checkout:
            d = checkout_dir / "vendor"
            d.mkdir()
            layouts.append(("checkout", d))

        for name, d in layouts:
            (d / "probe_vendor_marker.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")

        env = dict(os.environ, HOME=str(home))
        if appdir:
            env["APPDIR"] = str(app)
        else:
            env.pop("APPDIR", None)

        script = (
            "import main, sys\n"
            "try:\n"
            "    import probe_vendor_marker as p\n"
            "    winner = p.NAME\n"
            "except ImportError:\n"
            "    winner = ''\n"
            "winners = [q for q in sys.path if 'vendor' in q]\n"
            "print(winner)\n"
            "print('\\n'.join(winners))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], cwd=checkout_dir, env=env,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"main.py import failed:\n{proc.stdout}\n{proc.stderr}")
        winner, *order = proc.stdout.splitlines()
        order = [o for o in order if o.strip()]
        paths = {name: str(d) for name, d in layouts}
        return winner, order, paths


class VendorPrecedenceTest(unittest.TestCase):
    def test_appimage_beats_user_and_checkout(self):
        winner, order, paths = run_scenario(appdir=True, user=True, checkout=True)
        self.assertEqual(winner, "appimage",
                         "bundled vendor code must win over user and checkout")
        self.assertEqual(order, [paths["appimage"], paths["user"], paths["checkout"]],
                         "sys.path must read left-to-right in the documented order")

    def test_user_beats_checkout_when_no_bundle(self):
        winner, order, paths = run_scenario(appdir=False, user=True, checkout=True)
        self.assertEqual(winner, "user")
        self.assertEqual(order, [paths["user"], paths["checkout"]])

    def test_checkout_used_as_last_resort(self):
        winner, _order, _paths = run_scenario(appdir=False, user=False, checkout=True)
        self.assertEqual(winner, "checkout")

    def test_missing_vendors_are_simply_skipped(self):
        winner, order, _paths = run_scenario(appdir=False, user=False, checkout=False)
        self.assertEqual(winner, "", "no vendor module should be importable")
        self.assertEqual(order, [])


if __name__ == "__main__":
    unittest.main()
