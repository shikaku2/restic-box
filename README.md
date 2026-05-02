# restic-box

A GTK3 system-tray applet for [restic](https://restic.net/) backups to SSH storage boxes (e.g. Hetzner Storage Box).

![restic-box tray icon](icon.png)

## Features

- Tray icon with backup progress ring
- Scheduled backups (interval or daily)
- Automatic restic repository checks on a rolling subset schedule
- Per-directory backup sources with per-source excludes
- Retention / forget policy
- Sleep inhibition during active backups
- Log window with live output

## Requirements

### System packages (Arch Linux)

```
sudo pacman -S restic python-gobject python-cairo
# AUR: libayatana-appindicator  (or libappindicator-gtk3 from extra)
```

### System packages (Ubuntu / Debian)

```
sudo apt install restic python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-3.0 gir1.2-appindicator3-0.1
```

### pip (after system GTK libs are present)

```
pip install PyGObject pycairo
```

## Installation

### AppImage (recommended — no system Python deps needed)

Download the latest release from the [Releases page](../../releases):

| Variant | Description |
|---|---|
| `restic-box-vX.Y.Z-x86_64.AppImage` | Requires `restic` on your `$PATH` |
| `restic-box-bundled-vX.Y.Z-x86_64.AppImage` | Includes restic binary, fully self-contained |

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

### From source

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

## Configuration

On first run, open **Settings** from the tray menu to configure:

- SSH host, port, user, and key path for your storage box
- Restic repository path on the remote
- Backup directories and schedules

Configuration is saved to `~/.config/restic-box/config.json`.

## Vendor extensions

You can drop pure-Python packages into `~/.local/share/restic-box/vendor/` and they will be loaded automatically at startup. This works whether you're running from source or an AppImage.

## Building the AppImage locally

Requires [appimage-builder](https://appimage-builder.readthedocs.io/en/latest/intro/install.html) and `wget`/`bzip2`.

```bash
cd packaging

# Without bundled restic (expects restic on $PATH):
VERSION=0.1.0 BUNDLE_RESTIC=0 appimage-builder --recipe AppImageBuilder.yml --skip-tests

# With bundled restic:
VERSION=0.1.0 BUNDLE_RESTIC=1 appimage-builder --recipe AppImageBuilder.yml --skip-tests
```

## License

See [LICENSE.txt](LICENSE.txt).
