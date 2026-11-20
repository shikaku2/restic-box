# restic-box

A GTK3 system-tray applet for [restic](https://restic.net/) backups to SSH storage boxes, local repositories, and rclone remotes.

![restic-box tray icon](icon.svg)

## Features

- Tray icon with backup progress ring
- Scheduled backups (interval or daily)
- Automatic restic repository checks on a rolling subset schedule
- Per-directory backup sources with per-source excludes
- Backends: SFTP, local path, or rclone remotes
- Retention / forget policy
- Sleep inhibition during active backups
- Log window with live output
- Rotating diagnostic log file, so nothing important is lost when the window trims

## Installation

### AUR

```bash
yay -S restic-box
```

### AppImage (recommended — no system Python deps needed)

Download the latest release from the [Releases page](../../releases):

| Variant | Description |
|---|---|
| `restic-box-vX.Y.Z-x86_64.AppImage` | Requires `restic` on your `$PATH` |
| `restic-box-bundled-restic-vX.Y.Z-x86_64.AppImage` | Includes restic binary |
| `restic-box-bundled-restic-rclone-vX.Y.Z-x86_64.AppImage` | Includes restic and rclone binaries |

```bash
chmod +x restic-box-*.AppImage
./restic-box-*.AppImage
```

To install system-wide, copy to `~/.local/bin/` and register the desktop entry:

```bash
cp restic-box-*.AppImage ~/.local/bin/restic-box
# Extract and install the .desktop + icon (optional):
~/.local/bin/restic-box --appimage-extract-and-run \
    sh -c 'cp $APPDIR/restic-box.desktop ~/.local/share/applications/ && \
           cp $APPDIR/restic-box.png ~/.local/share/icons/'
```

Or run the program and right click the tray icon and check Run on Startup.  Checkmark supports both AppImage and running from source.

## From source

### Requirements

### System packages (Arch Linux)

```
sudo pacman -S restic rclone python-gobject python-cairo
# AUR: libayatana-appindicator  (or libappindicator-gtk3 from extra)
```

### System packages (Ubuntu / Debian)

```
sudo apt install restic rclone python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-3.0 gir1.2-appindicator3-0.1
```

### pip (after system GTK libs are present)

```
pip install PyGObject pycairo
```

```bash
git clone https://github.com/Shikaku2/restic-box
cd restic-box
python main.py
```

Or install via pip:

```bash
pip install .
restic-box
```

## Auto-split

restic-box can split large backup sources into independent snapshot units. This is useful when backing up very large directories (hundreds of GB or more) — if a backup run is interrupted, every finished unit is already a complete, restorable snapshot, and the interrupted unit re-reads from its start while uploading only the data still missing (restic dedups against the repository).

### How it works

With a threshold set (e.g. **100 GiB**):

- A directory **below** the threshold is backed up as a single unit, unchanged. Existing snapshots of it stay valid parents.
- A directory **at or above** the threshold is split:
  - Each child directory at or above the threshold is split recursively.
  - Each loose file at or above the threshold becomes its own unit.
  - If the leftovers (small children and small loose files together) still weigh at or above the threshold, they are packed into **"group"** units of at most the threshold each — one multi-path snapshot per group. This is what makes a flat folder of medium-sized files (a games folder, a photo archive) splittable instead of one unfinishable unit.
  - Everything left over (unpacked leftovers, anything created later) goes into one **"rest"** unit: the directory itself with the carved children/files excluded.
- The rest unit runs first (quick snapshot of the small stuff), then big pieces run smallest-to-largest.

### Enabling it

Open **Settings** → **Options** and set **Auto-split (GiB)** to a positive number. `0` (the default) means autosplitting is off — all sources are backed up as configured, no splitting applied.

### Notes

- A fresh split plan is computed on every backup run from current filesystem sizes — no stale plans.
- If the plan changes between runs, the new unit boundaries mean previous parent snapshots are no longer valid for those units (the next backup will fully re-read them, but will still dedup against restic's global object store).

## Configuration

On first run, open **Settings** from the tray menu to configure:

- SFTP host, port, user, and key path; a local path; or an rclone remote and path
- Restic repository path on the selected backend
- Backup directories and schedules

Configuration is saved to `~/.config/restic-box/config.json`.

### Logs and diagnostics

The tray's log window is deliberately bounded: the last 2 000 lines, and no
individual line longer than 4 096 characters. A backup that runs for days streams
one progress record per file, and an unbounded window is how a log viewer stops
responding.

Anything the window trims or shortens is not simply discarded. Every operational
line is also written to:

```
~/.config/restic-box/logs/restic-box.log
```

which rotates to `restic-box.log.1`, `restic-box.log.2`, `restic-box.log.3`,
`restic-box.log.4` — five files of at most 2 MB each, so restic-box's own
diagnostics can never grow past about 10 MB. When
a single line is too large to show, the window shows its beginning plus a note
naming this file, and the file holds the full record (excerpted to 1 MB, with the
real size stated).

Progress records themselves go to neither place: they are parsed for the tray's
progress ring and dropped. Writing hundreds of thousands of them to disk instead
would only move the problem.

The directory is created with mode `0700` because the log quotes repository and
source paths.

### Note on retention scope

Retention is applied to the **whole repository**, not just to the directories
this machine backs up. restic groups snapshots by `(hostname, paths)`, and
`--keep-within` and the other `--keep-*` rules are counted per group, from that
group's own newest snapshot. Two consequences worth knowing:

- A directory that used to be in the backup list and no longer is keeps its
  newest snapshot forever. restic-box will not forget it; remove it by hand with
  `restic forget <snapshot-id>`.
- Snapshots taken by another machine (a shared repository) are counted in their
  own groups too, so retention here cannot accidentally decide how many of those
  survive.

### Note on per-repository bookkeeping

"Last backup" and "last check" describe a **specific repository combined with a
specific set of source directories**. restic-box keeps every such combination's
outcomes in a small history inside its config file, keyed by a fingerprint of
the repository (backend, host, path, credentials) plus the enabled source
paths and excludes.

- If you change the repository or the source list while a backup or check is
  running, its result is credited to the configuration it actually used — the
  newly selected repository is *not* marked as backed up, and its schedule
  treats it as never backed up.
- Switching back to an earlier repository/sources restores that combination's
  own timestamps, subset rotation, and size statistics from history.
- Changing only scheduling, retention, compression, or split settings does not
  change the fingerprint, so a repository does not lose its "backed up" status
  because you retuned those options.

### Note on network shares (CIFS/SMB/NFS)

Network filesystems often renumber inodes and bump ctime on remount — sometimes even
mid-backup — even when the file contents haven't changed. restic uses inode + ctime to
decide whether a file is new, so this can make it re-read and re-upload unchanged files
as full local reads every run.

In **Settings → Options** you can enable:

- **Ignore inode changes** — adds `--ignore-inode` (ignores inode number *and* ctime,
  relying on size + mtime). This is the usual fix for CIFS/SMB/NFS sources.
- **Ignore ctime changes only** — adds `--ignore-ctime`, for sources where only ctime
  churns but inode numbers stay stable.

Both are off by default. They apply to backup runs (including auto-split units).

### Note on shared repositories

restic's `backup` command only takes a non-exclusive lock, so it does not prevent two separate machines from backing up to the same repository at the same time — this is expected restic behavior (concurrent backups are safe by design), but it means restic-box has no way to stop you from accidentally pointing two computers at the same repo/path. If you intend a repository to be used by a single machine, make sure only that machine is configured to back up to it.

## Vendor extensions

You can drop pure-Python packages into `~/.local/share/restic-box/vendor/` and they will be loaded automatically at startup. This works whether you're running from source or an AppImage.

## Building the AppImage locally

Requires [appimage-builder](https://appimage-builder.readthedocs.io/en/latest/intro/install.html) and `wget`/`bzip2`.

```bash
cd packaging

# Without bundled restic (expects restic on $PATH):
VERSION=0.1.0 BUNDLE_RESTIC=0 appimage-builder --recipe AppImageBuilder.yml --skip-tests

# With bundled restic:
VERSION=0.1.0 BUNDLE_RESTIC=1 BUNDLE_SUFFIX=-bundled-restic appimage-builder --recipe AppImageBuilder.yml --skip-tests

# With bundled restic and rclone:
VERSION=0.1.0 BUNDLE_RESTIC=1 BUNDLE_RCLONE=1 BUNDLE_SUFFIX=-bundled-restic-rclone \
    appimage-builder --recipe AppImageBuilder.yml --skip-tests
```

## License

See [LICENSE.txt](LICENSE.txt).

### Bundled third-party software

The `-bundled` AppImage variant includes [restic](https://restic.net/), copyright 2014 Alexander Neumann and contributors, used under the [BSD 2-Clause License](https://github.com/restic/restic/blob/master/LICENSE):

> Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
>
> 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
> 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The `-bundled-rclone` AppImage variant also includes [rclone](https://rclone.org/), copyright Nick Craig-Wood and contributors, used under the [MIT License](https://github.com/rclone/rclone/blob/master/COPYING):

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
