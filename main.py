"""Entrypoint for restic-box."""

import os
import sys
from pathlib import Path

# Vendor path resolution order:
#   1. $APPDIR/usr/share/restic-box/vendor  — bundled extras inside AppImage
#   2. ~/.local/share/restic-box/vendor     — user-installed pure-Python extensions
#   3. <script dir>/vendor                  — dev/standalone checkout
_vendor_candidates: list[Path] = []
_appdir = os.environ.get("APPDIR")
if _appdir:
    _vendor_candidates.append(Path(_appdir) / "usr" / "share" / "restic-box" / "vendor")
_vendor_candidates += [
    Path.home() / ".local" / "share" / "restic-box" / "vendor",
    Path(__file__).parent / "vendor",
]
# Insert reversed so the *first* candidate ends up leftmost in sys.path and
# therefore wins: AppImage bundle > user vendor > checkout vendor, exactly the
# documented precedence (repeated insert(0) puts the last inserted first).
for _vp in reversed(_vendor_candidates):
    if _vp.is_dir():
        _s = str(_vp)
        if _s not in sys.path:
            sys.path.insert(0, _s)

from restic_indicator import main

if __name__ == "__main__":
    main()
