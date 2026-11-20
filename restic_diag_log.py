"""Rotating on-disk diagnostic log (review fix #6).

The in-memory bounds in this app (restic_runner._LineTail,
restic_status.LogWindow) keep a days-long backup from growing without limit —
but "bounded" also means "not all of it is in RAM any more". Whatever the
memory bounds drop (an exceptionally large single record, output older than the
retained tail) is still useful to a user trying to diagnose a failed backup, so
it is written to disk instead of thrown away.

Disk use is bounded too, which is the point of the rotation:

    <config>/logs/restic-box.log        active file, rotated past MAX_LOG_FILE_BYTES
    <config>/logs/restic-box.log.1      one older copy
    …                                   up to MAX_LOG_FILES files in total

so restic-box's own diagnostics can never consume more than
MAX_LOG_FILES * MAX_LOG_FILE_BYTES bytes. Individual records are also capped
(MAX_RECORD_CHARS) so a single pathological line — restic reading a corrupted or
binary-polluted stream — cannot fill a file faster than rotation can follow.

Two rules this module exists to keep:

* Writing a diagnostic must never be able to break the app. Every operation is
  best-effort: failures are counted (``errors``) and the logger disables itself
  after too many, but it never raises into a backup thread.
* Progress records are not written here. restic streams one status record per
  file during a long backup; those are parsed for the progress bar and
  discarded (issue #14). Only operational messages, restic diagnostics and
  oversized records land in the file.

The log directory is resolved from restic_config.CONFIG_DIR on first use, and
``set_log_dir()`` relocates it — tests use that so the suite never writes into
the user's real ~/.config/restic-box.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

import restic_config as cfg_mod

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "restic-box.log"

MAX_LOG_FILE_BYTES = 2 * 1024 * 1024   # rotate the active file past this
MAX_LOG_FILES = 5                      # active file + rotated copies, total
MAX_RECORD_CHARS = 1024 * 1024         # one record's worth of detail on disk
MAX_ERROR_STREAK = 20                  # then the logger gives up for good
NOTE_RESERVE = 128                     # max length of a truncation note


def truncation_note(dropped_chars: int, where: str) -> str:
    """The note appended to a record that had to be excerpted.

    Shared by restic_runner._LineTail and restic_status.LogWindow so a truncated
    line always says the same thing in the same shape: how much is missing and
    where the full text is. Always fits in NOTE_RESERVE characters.
    """
    note = f"… [restic-box: {dropped_chars} more characters, full text: {where}]"
    if len(note) > NOTE_RESERVE:
        note = note[: NOTE_RESERVE - 1] + "…"
    return note


def _size_of(path: Path) -> int:
    """Size in bytes, or -1 when the path cannot be read (missing, or its
    directory is gone). -1 never matches a real tracked size, so it forces the
    handle to be reopened."""
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


class DiagLog:
    """Size-rotating append-only log. Thread-safe: worker threads, the GTK
    thread and the settings dialog all write to the same file."""

    def __init__(self, log_dir: Path | str, *,
                 max_file_bytes: int = MAX_LOG_FILE_BYTES,
                 max_files: int = MAX_LOG_FILES,
                 max_record_chars: int = MAX_RECORD_CHARS) -> None:
        self.log_dir = Path(log_dir)
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_record_chars = max_record_chars
        self.errors = 0
        self.disabled = False
        self._lock = threading.Lock()
        self._fh = None
        self._fh_size = 0
        self._bytes_written = 0

    # --- paths ----------------------------------------------------------

    @property
    def active_path(self) -> Path:
        return self.log_dir / LOG_FILE_NAME

    def _copy_path(self, index: int) -> Path:
        return self.log_dir / f"{LOG_FILE_NAME}.{index}"

    # --- writing --------------------------------------------------------

    def write(self, text: str) -> Path | None:
        """Append one record. Returns the file it landed in, or None on failure
        (never raises: a broken log directory must not break a backup).

        The record is excerpted to max_record_chars: rotation bounds how much
        log accumulates over time, this bounds what one pathological line can
        add in a single write.
        """
        record = _format(text)
        if len(record) > self.max_record_chars:
            record = (record[: self.max_record_chars]
                      + f" … [restic-box: record excerpted to {self.max_record_chars} characters]")
        if self.disabled:
            return None
        try:
            with self._lock:
                self._ensure_dir()
                self._rotate_if_needed()
                fh = self._current()
                record += "\n"
                fh.write(record)
                fh.flush()
                self._bytes_written += len(record.encode("utf-8", "replace"))
                self.errors = 0
                return self.active_path
        except OSError:
            self.errors += 1
            if self.errors >= MAX_ERROR_STREAK:
                self.disabled = True
            self._close_quietly()
            return None

    def record_oversized(self, text: str, *, limit: int, source: str = "") -> Path | None:
        """Preserve a record that was too large to keep in memory.

        The first ``max_record_chars`` characters are written (the head of the
        record is what a human can act on), and if even that is trimmed the
        fact is stated on the line so nobody mistakes the excerpt for the whole
        record. Returns the file, or None if the write failed.
        """
        base = (f"[oversized {source or 'output'}: {len(text)} characters total, "
                f"memory kept {limit}]")
        # Reserve room for the "trimmed" sentence so the whole record still fits
        # in max_record_chars and write() never has to clip it again.
        budget = max(1, self.max_record_chars - len(base) - 64)
        note = base if len(text) <= budget else base + f" — file excerpt trimmed to {budget}"
        detail = text[: budget]
        return self.write(f"{note}\n{detail}")

    # --- rotation -------------------------------------------------------

    def _ensure_dir(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.log_dir, 0o700)
        except OSError:
            pass    # an existing directory with other permissions is fine

    def _open(self):
        self._fh = self.active_path.open("a", encoding="utf-8", errors="replace")
        self._fh_size = _size_of(self.active_path)
        self._bytes_written = 0
        return self._fh

    def _current(self):
        """The open handle, reopened whenever it no longer matches the file on disk.

        The log file can be deleted, truncated or rotated out from under a
        running app — a user cleaning up ~/.config, a disk-full write, a second
        restic-box instance. A stale handle would then accept every later record
        into a file nobody can see, which is worse than not logging at all.
        """
        if self._fh is not None:
            if _size_of(self.active_path) == self._fh_size + self._bytes_written:
                return self._fh
            self._close_quietly()
        return self._open()

    def _close_quietly(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _rotate_if_needed(self) -> None:
        try:
            size = os.path.getsize(self.active_path)
        except FileNotFoundError:
            return
        if size <= self.max_file_bytes:
            return
        self._close_quietly()
        newest = self._copy_path(self.max_files - 1)
        try:
            os.remove(newest)                       # the oldest copy expires
        except FileNotFoundError:
            pass
        for index in range(self.max_files - 2, 0, -1):
            src, dst = self._copy_path(index), self._copy_path(index + 1)
            try:
                os.replace(src, dst)
            except FileNotFoundError:
                pass
        os.replace(self.active_path, self._copy_path(1))   # the active file becomes copy 1

    # --- test/diagnostic helpers ----------------------------------------

    def files(self) -> list[Path]:
        return sorted(p for p in self.log_dir.glob(f"{LOG_FILE_NAME}*") if p.is_file())

    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.files())

    def contents(self) -> str:
        """Newest-last: the active file plus its rotated copies."""
        out = []
        for path in sorted(self.files(), key=lambda p: (p.name != LOG_FILE_NAME, p.name),
                           reverse=True):
            out.append(path.read_text(encoding="utf-8", errors="replace"))
        return "".join(out)

    def close(self) -> None:
        with self._lock:
            self._close_quietly()


def _format(text: str) -> str:
    """Timestamp + one physical record per call. Embedded newlines are kept
    (they belong to the record), control characters are replaced."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"[{stamp}] {text}".replace("\r", "\n")


# --- module-level log, so callers never have to carry one around ----------

_default: DiagLog | None = None
_default_lock = threading.Lock()
_explicit_dir: Path | None = None


def log_dir() -> Path:
    """Where diagnostics are written. Resolved from the current CONFIG_DIR on
    first use so patching restic_config.CONFIG_DIR redirects it too."""
    if _explicit_dir is not None:
        return _explicit_dir
    return Path(cfg_mod.CONFIG_DIR) / LOG_DIR_NAME


def set_log_dir(path: Path | str | None) -> DiagLog:
    """Point the diagnostic log at ``path`` (None restores the default).

    Tests must call this with a temporary directory: the suite has no business
    writing into the user's real ~/.config/restic-box.
    """
    global _default, _explicit_dir
    with _default_lock:
        if _default is not None:
            _default.close()
        _explicit_dir = Path(path) if path is not None else None
        _default = DiagLog(log_dir())
        return _default


def active() -> DiagLog:
    global _default
    with _default_lock:
        if _default is None or _default.log_dir != log_dir():
            if _default is not None:
                _default.close()
            _default = DiagLog(log_dir())
        return _default


def note(text: str) -> Path | None:
    """Log one operational/diagnostic line. Best-effort by design."""
    return active().write(text)


def record_oversized(text: str, *, limit: int, source: str = "") -> Path | None:
    """Preserve the detail of a record that memory bounds had to drop."""
    return active().record_oversized(text, limit=limit, source=source)
