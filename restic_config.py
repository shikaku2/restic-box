"""Config dataclass + JSON persistence for restic-box."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import secrets
import shutil
import string
import tempfile
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "restic-box"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_PASSWORD_FILE = CONFIG_DIR / "password"

_CONFIG_LOCK = threading.RLock()  # the UI thread and the op worker both save


class ConfigError(ValueError):
    """Configuration could not be read or failed validation."""


@dataclass
class Directory:
    path: str
    excludes: list[str] = field(default_factory=list)
    enabled: bool = True
    cached_size: float = 0.0  # bytes reported by restic on last completed backup of this dir


#: How an operation attempt ended, as one word so it survives JSON unchanged.
#: Fix #9 (restic-box-fixes2.md) asks that these five be distinguishable, because
#: they mean different things to the user and to the scheduler: only the first two
#: left a snapshot behind, only the first three involved restic at all, and a
#: skipped attempt is the one that used to leave no trace whatsoever.
OUTCOME_SUCCESS = "success"      # every source readable, repository accepted the snapshot
OUTCOME_PARTIAL = "partial"      # snapshot exists, some files unreadable (restic rc=3)
OUTCOME_FAILED = "failed"        # restic ran and refused (nonzero rc), or the operation crashed
OUTCOME_CANCELLED = "cancelled"  # the user stopped it; nothing about the repository changed
OUTCOME_SKIPPED = "skipped"      # never launched: preflight, busy repository, or an active mount
OUTCOME_NONE = ""                # this identity has never been attempted
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_FAILED,
            OUTCOME_CANCELLED, OUTCOME_SKIPPED)

#: What a stored status may hold: one of the five outcomes, or nothing at all.
VALID_OUTCOMES = (OUTCOME_NONE,) + OUTCOMES

#: Outcomes that describe a run which talked to the repository, and so are allowed
#: to move the scheduling timestamps (last_backup_ts / last_check_ts).
ATTEMPT_OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_FAILED)


def _derived_backup_status(record: BackupRecord) -> str:
    """The status of a record written before fix #9 added one.

    Old records store ``ok``/``partial`` only, which is enough to tell success,
    partial and failure apart — and enough to say "there was an attempt", which is
    what the missing field must not be allowed to erase.
    """
    if record.ok:
        return OUTCOME_SUCCESS
    if record.partial:
        return OUTCOME_PARTIAL
    return OUTCOME_FAILED


@dataclass
class BackupRecord:
    """One backup outcome, tied to the configuration identity that produced it.

    Fix #1 (restic-box-fixes2.md): "backed up" is a statement about a specific
    repository *and* a specific set of source directories. Keeping outcomes per
    identity is what stops repository B inheriting repository A's success.

    Fix #9 adds the other half: the *attempt* and the *success* are separate
    facts. ``ts`` is the last run that reached the repository (what the scheduler
    waits on), ``status`` says how the most recent attempt ended — including the
    attempts that never launched — and ``ok_ts`` is the last backup that fully
    succeeded, which no failure or skip is allowed to overwrite.
    """
    ts: float = 0.0                    # when the last run that reached restic finished
    ok: bool = False                   # complete success only
    partial: bool = False              # restic rc=3: snapshot exists, files unreadable
    total_backup_size: float = 0.0
    compressed_backup_size: float = 0.0
    status: str = OUTCOME_NONE         # how the most recent attempt ended
    status_ts: float = 0.0             # when that attempt happened, skipped ones included
    reason: str = ""                   # why it did not succeed, for the tray label
    ok_ts: float = 0.0                 # last SUCCESSFUL backup for this identity

    def __post_init__(self):
        if not self.status and self.ts > 0:
            self.status = _derived_backup_status(self)
        if not self.status_ts and self.ts > 0:
            self.status_ts = self.ts
        if not self.ok_ts and self.status == OUTCOME_SUCCESS and self.ts > 0:
            self.ok_ts = self.ts


def _derived_check_status(record: CheckRecord) -> str:
    """Status of a check record written before fix #9: ``ok_ts`` already separated
    attempt from success, so a record whose success is not as new as its attempt
    is a failed attempt."""
    if record.ok_ts and record.ok_ts >= record.ts:
        return OUTCOME_SUCCESS
    return OUTCOME_FAILED


@dataclass
class CheckRecord:
    ts: float = 0.0                    # last check ATTEMPT for this identity
    ok_ts: float = 0.0                 # last SUCCESSFUL check for this identity
    subset_current: int = 1            # this repository's position in the subset rotation
    status: str = OUTCOME_NONE         # how the most recent attempt ended (fix #9)
    status_ts: float = 0.0             # when that attempt happened, skipped ones included
    reason: str = ""                   # why it did not succeed, for the tray label

    def __post_init__(self):
        if not self.status and self.ts > 0:
            self.status = _derived_check_status(self)
        if not self.status_ts and self.ts > 0:
            self.status_ts = self.ts


@dataclass
class Config:
    backend: str = "sftp"  # sftp | local | rclone
    ssh_host: str = ""
    ssh_port: int = 23
    ssh_user: str = ""
    ssh_key: str = str(Path.home() / ".ssh" / "id_ed25519")
    rclone_remote: str = ""
    repo_path: str = "/backups/restic-repo"
    password_file: str = str(DEFAULT_PASSWORD_FILE)
    compression: str = "max"  # off | auto | max
    backup_schedule: str = "disabled"   # disabled | interval | daily
    backup_interval_hours: int = 6
    backup_daily_hour: int = 2
    check_interval: str = "daily"       # daily | weekly
    check_day: int = 0                  # 0=Mon … 6=Sun (weekly only)
    check_hour: int = 3
    backup_on_startup: bool = True
    check_read_data_subset: bool = True
    check_subset_total: int = 100       # verify 1/N of data per check
    check_subset_current: int = 1       # which subset to check next (1-based, persisted)
    last_backup_ts: float = 0.0   # unix timestamp of last backup run that reached the repository, 0 = never (what the schedule waits on)
    last_backup_ok: bool = True   # complete success only — a partial run is not OK
    last_backup_partial: bool = False  # last run completed but skipped unreadable files (restic rc=3)
    last_check_ts: float = 0.0    # unix timestamp of last check ATTEMPT, 0 = never (a failed attempt still counts: the retry waits for the next schedule slot, never the next scheduler tick)
    last_check_ok_ts: float = 0.0  # unix timestamp of last SUCCESSFUL check (attempt and success tracked separately)
    # Fix #9 (restic-box-fixes2.md): how the most recent attempt of each scheduled
    # operation ended — success | partial | failed | cancelled | skipped — when it
    # happened, and why it did not succeed. These describe the TRAY, never the
    # schedule: a skip or a cancellation must not move last_backup_ts/last_check_ts
    # (fix #3's backoff owns retrying sooner), and last_*_ok_ts below is never
    # touched by anything but a genuine success.
    last_backup_status: str = OUTCOME_NONE
    last_backup_status_ts: float = 0.0
    last_backup_ok_ts: float = 0.0  # unix timestamp of the last FULLY SUCCESSFUL backup
    last_backup_reason: str = ""
    last_check_status: str = OUTCOME_NONE
    last_check_status_ts: float = 0.0
    last_check_reason: str = ""
    total_backup_size: float = 0.0  # total uncompressed bytes across enabled backup sources
    compressed_backup_size: float = 0.0  # repository-wide compressed bytes, including older snapshots
    # Outcomes keyed by configuration identity (see config_identity): the bookkeeping
    # fields above always describe the identity the config describes *now*, and these
    # histories are where every other repository/source combination's outcomes wait.
    backup_history: dict[str, BackupRecord] = field(default_factory=dict)
    check_history: dict[str, CheckRecord] = field(default_factory=dict)
    inhibit_sleep: bool = True
    ignore_inode: bool = False   # --ignore-inode on backup; stops full-read churn on CIFS/network shares where inodes/ctime change between backups
    ignore_ctime: bool = False   # --ignore-ctime on backup; for shares where only ctime drifts (inode numbers stay stable)
    split_threshold_gib: int = 0  # split sources >= this (allocated GiB) into independent snapshots; 0 = off
    split_drift_factor: float = 2.0  # stable replan: only touch a saved unit that grew past drift_factor * threshold
    retention_enabled: bool = False
    retention_years: int = 0
    retention_months: int = 0
    retention_days: int = 0
    retention_hours: int = 0
    directories: list[Directory] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        if not isinstance(d, dict):
            raise ConfigError("Configuration must be a JSON object.")
        raw_dirs = d.get("directories", [])
        if not isinstance(raw_dirs, list):
            raise ConfigError("'directories' must be a list.")
        known = set(cls.__dataclass_fields__) - {"directories"}
        obj = cls(**{k: v for k, v in d.items() if k in known})
        dir_fields = set(Directory.__dataclass_fields__)
        directories = []
        for i, rd in enumerate(raw_dirs):
            if not isinstance(rd, dict):
                raise ConfigError(f"directories[{i}] must be an object.")
            if "path" not in rd:
                raise ConfigError(f"directories[{i}] is missing 'path'.")
            # Forward-compatible: an unknown per-directory field (added by a newer
            # version) must not discard the whole configuration.
            directories.append(
                Directory(**{k: v for k, v in rd.items() if k in dir_fields})
            )
        obj.directories = directories
        obj.backup_history = _history_from_dict(d.get("backup_history", {}),
                                                 BackupRecord, "backup_history")
        obj.check_history = _history_from_dict(d.get("check_history", {}),
                                                CheckRecord, "check_history")
        _adopt_attempt_tracking(obj, d)
        validate_config(obj)
        return obj


def _adopt_attempt_tracking(cfg: Config, raw: dict) -> None:
    """Give a configuration written before fix #9 the fields it never stored.

    Older versions recorded ``last_backup_ts`` plus ok/partial, which cannot tell a
    skipped or cancelled attempt from a failed run, and let a failure overwrite the
    time of the last backup that really succeeded. Everything such a file does say
    is kept and reinterpreted; nothing is invented, and a config that already has
    these fields is left exactly as it is.
    """
    if "last_backup_status" not in raw and cfg.last_backup_ts > 0:
        cfg.last_backup_status = _derived_backup_status(BackupRecord(
            ts=cfg.last_backup_ts, ok=cfg.last_backup_ok, partial=cfg.last_backup_partial))
        cfg.last_backup_status_ts = cfg.last_backup_ts
    if ("last_backup_ok_ts" not in raw and cfg.last_backup_status == OUTCOME_SUCCESS
            and cfg.last_backup_ts > 0):
        cfg.last_backup_ok_ts = cfg.last_backup_ts
    if "last_check_status" not in raw and cfg.last_check_ts > 0:
        cfg.last_check_status = _derived_check_status(CheckRecord(
            ts=cfg.last_check_ts, ok_ts=cfg.last_check_ok_ts))
        cfg.last_check_status_ts = cfg.last_check_ts


def _history_from_dict(raw, cls, name: str) -> dict:
    """Read one identity→record map out of JSON, tolerating unknown fields."""
    if isinstance(raw, cls):          # already parsed (in-memory round trip)
        return raw
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be an object.")
    known = set(cls.__dataclass_fields__)
    history = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ConfigError(f"{name} keys must be strings.")
        if isinstance(value, cls):
            history[key] = value
        elif isinstance(value, dict):
            history[key] = cls(**{k: v for k, v in value.items() if k in known})
        else:
            raise ConfigError(f"{name}[{key}] must be an object.")
    return history


# Settings that define *which repository* an operation talked to. restic's
# repository is its location plus its key, so the credentials that select them
# belong in the identity too.
REPOSITORY_FIELDS = (
    "backend", "ssh_host", "ssh_port", "ssh_user", "ssh_key",
    "rclone_remote", "repo_path", "password_file",
)

# Bookkeeping that describes the repository+sources, never a user preference:
# it lives in backup_history/check_history per identity, and the Config fields
# last_backup_*/last_check_*/total_backup_size/compressed_backup_size/
# check_subset_current always mirror the selected identity's record.

MAX_HISTORY_ENTRIES = 16  # identities remembered; oldest outcomes expire first


def config_identity(cfg: Config) -> str:
    """Identity of the repository *and* enabled source configuration in *cfg*.

    An operation's results may only be credited to the configuration it actually
    ran against, which needs one comparable value for "the thing that was backed
    up". Scheduling, retention, compression and split settings deliberately do
    NOT change it: tweaking those must not make a repository look never backed
    up. Enabling/disabling a directory, editing its excludes, or pointing at
    another repository DOES: those change what a backup covers.
    """
    repo = {name: getattr(cfg, name) for name in REPOSITORY_FIELDS}
    sources = sorted((d.path, tuple(d.excludes)) for d in cfg.directories if d.enabled)
    blob = json.dumps({"repo": repo, "sources": [list(s) for s in sources]},
                      sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _bounded(history: dict) -> dict:
    """Keep the most recent MAX_HISTORY_ENTRIES identities, so the config file
    cannot grow every time a test repository is tried."""
    if len(history) <= MAX_HISTORY_ENTRIES:
        return history

    def recency(item):
        _key, record = item
        return max((getattr(record, field, 0.0) or 0.0)
                   for field in ("ts", "ok_ts", "status_ts"))

    return dict(sorted(history.items(), key=recency)[-MAX_HISTORY_ENTRIES:])


def stash_bookkeeping(cfg: Config) -> Config:
    """File the current bookkeeping fields under the identity they describe.

    Called before settings replace those fields, so the outcome of repository A's
    last backup is not thrown away when the live config starts describing
    repository B.
    """
    identity = config_identity(cfg)
    backup = BackupRecord(
        ts=cfg.last_backup_ts, ok=cfg.last_backup_ok, partial=cfg.last_backup_partial,
        total_backup_size=cfg.total_backup_size,
        compressed_backup_size=cfg.compressed_backup_size,
        status=cfg.last_backup_status, status_ts=cfg.last_backup_status_ts,
        reason=cfg.last_backup_reason, ok_ts=cfg.last_backup_ok_ts,
    )
    check = CheckRecord(ts=cfg.last_check_ts, ok_ts=cfg.last_check_ok_ts,
                        subset_current=cfg.check_subset_current,
                        status=cfg.last_check_status, status_ts=cfg.last_check_status_ts,
                        reason=cfg.last_check_reason)
    histories: dict[str, dict] = {}
    for name, record, cls in (("backup_history", backup, BackupRecord),
                              ("check_history", check, CheckRecord)):
        history = dict(getattr(cfg, name))
        # Anything worth remembering: a run that reached the repository, a success,
        # or (fix #9) an attempt that only ever got skipped or cancelled.
        if (record.ts > 0 or record.status_ts > 0
                or getattr(record, "ok_ts", 0.0) > 0):
            history[identity] = record
        else:
            history.pop(identity, None)   # nothing to remember for this identity
        histories[name] = _bounded(history)
    return replace(cfg, **histories)


def rebind_bookkeeping(cfg: Config) -> Config:
    """Point the bookkeeping fields at the history of the identity cfg describes.

    This is the other half of fix #1: after a settings save, the tray, the
    "due?" tests and the size labels must speak about the repository the user
    selected now — with its own history, or as never backed up if it has none.
    """
    identity = config_identity(cfg)
    backup = cfg.backup_history.get(identity)
    check = cfg.check_history.get(identity)
    changes: dict = {}
    if backup is None:
        changes.update(last_backup_ts=0.0, last_backup_ok=True, last_backup_partial=False,
                       last_backup_status=OUTCOME_NONE, last_backup_status_ts=0.0,
                       last_backup_ok_ts=0.0, last_backup_reason="",
                       total_backup_size=0.0, compressed_backup_size=0.0)
    else:
        changes.update(last_backup_ts=backup.ts, last_backup_ok=backup.ok,
                       last_backup_partial=backup.partial,
                       last_backup_ok_ts=backup.ok_ts,
                       last_backup_status=backup.status,
                       last_backup_status_ts=backup.status_ts,
                       last_backup_reason=backup.reason,
                       total_backup_size=backup.total_backup_size,
                       compressed_backup_size=backup.compressed_backup_size)
    if check is None:
        changes.update(last_check_ts=0.0, last_check_ok_ts=0.0, check_subset_current=1,
                       last_check_status=OUTCOME_NONE, last_check_status_ts=0.0,
                       last_check_reason="")
    else:
        changes.update(last_check_ts=check.ts, last_check_ok_ts=check.ok_ts,
                       check_subset_current=check.subset_current,
                       last_check_status=check.status,
                       last_check_status_ts=check.status_ts,
                       last_check_reason=check.reason)
    return replace(cfg, **changes)


def _record_bookkeeping(cfg: Config, name: str, record) -> Config:
    """Store *record* under *name*'s identity and, when that identity is what the
    live config describes, publish it as the live bookkeeping too."""
    identity, record = record
    history = _bounded({**getattr(cfg, name), identity: record})
    out = replace(cfg, **{name: history})
    if identity == config_identity(cfg):
        if name == "backup_history":
            out = replace(out, last_backup_ts=record.ts, last_backup_ok=record.ok,
                          last_backup_partial=record.partial,
                          last_backup_ok_ts=record.ok_ts,
                          last_backup_status=record.status,
                          last_backup_status_ts=record.status_ts,
                          last_backup_reason=record.reason,
                          total_backup_size=record.total_backup_size,
                          compressed_backup_size=record.compressed_backup_size)
        else:
            out = replace(out, last_check_ts=record.ts, last_check_ok_ts=record.ok_ts,
                          check_subset_current=record.subset_current,
                          last_check_status=record.status,
                          last_check_status_ts=record.status_ts,
                          last_check_reason=record.reason)
    return out


def record_backup(cfg: Config, identity: str, *, ts: float, ok: bool, partial: bool,
                  total_backup_size: float | None = None,
                  compressed_backup_size: float | None = None,
                  status: str | None = None, reason: str = "") -> Config:
    """Credit a backup outcome to *identity* — the configuration the run used.

    When the live config has moved on to another repository/sources, the outcome
    is filed in that identity's history and the live bookkeeping is left exactly
    as the user's newer settings have it: repository B never inherits A's
    success timestamp.

    Fix #9: *this* run is recorded as an attempt of its own (``status``, at ``ts``)
    while the identity's last genuine success is carried forward unchanged. A
    failed backup therefore reports itself honestly without erasing the fact that
    an earlier one worked.
    """
    def previous(field: str) -> float:
        # A run that has no size for the field keeps the value that identity
        # already had — never the live config's number, because when the live
        # config describes a different repository its sizes belong to IT.
        old = cfg.backup_history.get(identity)
        if old is not None:
            return getattr(old, field)
        if identity == config_identity(cfg):
            return getattr(cfg, field)
        return 0.0

    if status is not None and status not in ATTEMPT_OUTCOMES:
        raise ConfigError(f"record_backup cannot record {status!r}: a run that never "
                          "reached the repository is recorded with record_attempt.")
    _finite_nonnegative(ts, "backup attempt ts")
    outcome = status or (OUTCOME_SUCCESS if ok else
                         OUTCOME_PARTIAL if partial else OUTCOME_FAILED)
    old = cfg.backup_history.get(identity)
    if old is not None:
        previous_ok_ts = old.ok_ts
    elif identity == config_identity(cfg):
        previous_ok_ts = cfg.last_backup_ok_ts
    else:
        previous_ok_ts = 0.0
    record = (identity, BackupRecord(
        ts=ts, ok=ok, partial=partial,
        total_backup_size=previous("total_backup_size")
        if total_backup_size is None else total_backup_size,
        compressed_backup_size=previous("compressed_backup_size")
        if compressed_backup_size is None else compressed_backup_size,
        status=outcome, status_ts=ts, reason=reason,
        ok_ts=ts if outcome == OUTCOME_SUCCESS else previous_ok_ts,
    ))
    return _record_bookkeeping(cfg, "backup_history", record)


def record_check(cfg: Config, identity: str, *, ts: float, ok_ts: float,
                 subset_current: int, status: str | None = None,
                 reason: str = "") -> Config:
    """Same rule as record_backup, for integrity-check results (fix #1 asks for
    both): attempt and success times and the subset rotation belong to the
    repository that was actually checked."""
    if status is not None and status not in ATTEMPT_OUTCOMES:
        raise ConfigError(f"record_check cannot record {status!r}: a check that never "
                          "reached the repository is recorded with record_attempt.")
    _finite_nonnegative(ts, "check attempt ts")
    outcome = status or (OUTCOME_SUCCESS if (ok_ts and ok_ts >= ts) else OUTCOME_FAILED)
    record = (identity, CheckRecord(ts=ts, ok_ts=ok_ts, subset_current=subset_current,
                                    status=outcome, status_ts=ts, reason=reason))
    return _record_bookkeeping(cfg, "check_history", record)


#: Which per-identity history an operation's outcome belongs to. "unlock" changes no
#: bookkeeping at all: it asks the repository to drop a stale lock, and there is no
#: outcome of it to show the user.
_HISTORY_FIELDS = {"backup": "backup_history", "check": "check_history",
                   "check_full": "check_history"}


def record_attempt(cfg: Config, identity: str, *, op: str, ts: float,
                   status: str, reason: str = "") -> Config:
    """Record an operation that was attempted but never reached restic.

    Fix #9 (restic-box-fixes2.md): when a scheduled backup or check was skipped —
    the SSH preflight failed, the user stopped it before anything launched, another
    operation owns the repository, a mount is holding it — nothing used to be
    written down. The tray then kept showing the previous success as though the
    attempt that just failed to happen had produced it.

    Two lines this must not cross. It does not move ``last_backup_ts`` /
    ``last_check_ts``: those are what the schedule waits on, a run that actually
    talked to the repository sets them, and letting a skip push the next attempt a
    whole interval away would defeat the retry backoff (fix #3). And it never
    touches ``last_*_ok_ts``: only a genuine success moves that. A cancellation in
    particular is a user's decision, not an outcome of the repository.
    """
    name = _HISTORY_FIELDS.get(op)
    if name is None:
        return cfg
    if status not in OUTCOMES or status in (OUTCOME_SUCCESS, OUTCOME_PARTIAL):
        raise ConfigError(f"record_attempt cannot record {status!r}: use "
                          "record_backup/record_check for a run that reached the repository.")
    if not isinstance(reason, str):
        raise ConfigError("attempt reason must be a string.")
    _finite_nonnegative(ts, "attempt ts")
    belongs = identity == config_identity(cfg)
    history = cfg.backup_history if name == "backup_history" else cfg.check_history

    def carried(record_field: str, live_field: str, default):
        # Fix #1's rule applies here too: an attempt filed against another
        # repository may borrow that repository's own history, but never the
        # live config's — those stamps belong to whichever repository is
        # selected now. Nothing about it is known, so nothing is claimed.
        old = history.get(identity)
        if old is not None:
            return getattr(old, record_field)
        if belongs:
            return getattr(cfg, live_field)
        return default

    if name == "backup_history":
        history = cfg.backup_history
        record = BackupRecord(
            ts=carried("ts", "last_backup_ts", 0.0),
            ok=carried("ok", "last_backup_ok", True),
            partial=carried("partial", "last_backup_partial", False),
            total_backup_size=carried("total_backup_size", "total_backup_size", 0.0),
            compressed_backup_size=carried("compressed_backup_size",
                                           "compressed_backup_size", 0.0),
            status=status, status_ts=ts, reason=reason,
            ok_ts=carried("ok_ts", "last_backup_ok_ts", 0.0),
        )
    else:
        history = cfg.check_history
        record = CheckRecord(
            ts=carried("ts", "last_check_ts", 0.0),
            ok_ts=carried("ok_ts", "last_check_ok_ts", 0.0),
            subset_current=carried("subset_current", "check_subset_current", 1),
            status=status, status_ts=ts, reason=reason)
    return _record_bookkeeping(cfg, name, (identity, record))


def backup_outcome(cfg: Config) -> tuple[str, float, float, str]:
    """What the live bookkeeping says about backups:

    ``(status, last attempt, last success, reason)`` — where the attempt is the
    most recent thing tried, including one that never launched, and the success is
    the last run that fully completed.

    A configuration written before fix #9 stored only ``last_backup_ts`` with
    ok/partial flags, which cannot express a skipped or cancelled attempt. Those
    fields are interpreted here rather than left to each caller to guess at, so the
    tray describes an old config and a new one the same way.
    """
    status = cfg.last_backup_status
    if not status and cfg.last_backup_ts > 0:
        status = _derived_backup_status(BackupRecord(
            ts=cfg.last_backup_ts, ok=cfg.last_backup_ok, partial=cfg.last_backup_partial))
    attempt_ts = max(cfg.last_backup_status_ts, cfg.last_backup_ts)
    ok_ts = cfg.last_backup_ok_ts
    if status == OUTCOME_SUCCESS and attempt_ts > ok_ts:
        ok_ts = attempt_ts
    return status, attempt_ts, ok_ts, cfg.last_backup_reason


def check_outcome(cfg: Config) -> tuple[str, float, float, str]:
    """The same four facts about integrity checks (fix #9 tracks both)."""
    status = cfg.last_check_status
    if not status and cfg.last_check_ts > 0:
        status = _derived_check_status(CheckRecord(
            ts=cfg.last_check_ts, ok_ts=cfg.last_check_ok_ts))
    attempt_ts = max(cfg.last_check_status_ts, cfg.last_check_ts)
    return status, attempt_ts, cfg.last_check_ok_ts, cfg.last_check_reason


def _finite_nonnegative(value, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ConfigError(f"{name} must be a finite, nonnegative number.")


def validate_config(cfg: Config) -> None:
    """Validate stored types and scheduling ranges, not connection readiness.

    Empty connection fields are allowed so a first-run configuration is valid.
    """
    defaults = Config()

    for name in Config.__dataclass_fields__:
        if name == "directories":
            continue
        value = getattr(cfg, name)
        default = getattr(defaults, name)
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ConfigError(f"{name} must be true or false.")
        elif isinstance(default, int):
            if type(value) is not int:
                raise ConfigError(f"{name} must be an integer.")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ConfigError(f"{name} must be a finite number.")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise ConfigError(f"{name} must be a string.")

    choices = {
        "backend": {"sftp", "local", "rclone"},
        "compression": {"off", "auto", "max"},
        "backup_schedule": {"disabled", "interval", "daily"},
        "check_interval": {"daily", "weekly"},
    }
    for name, allowed in choices.items():
        if getattr(cfg, name) not in allowed:
            raise ConfigError(f"{name} must be one of: {', '.join(sorted(allowed))}.")

    ranges = {
        "ssh_port": (1, 65535),
        "backup_daily_hour": (0, 23),
        "check_hour": (0, 23),
        "check_day": (0, 6),
    }
    for name, (low, high) in ranges.items():
        if not low <= getattr(cfg, name) <= high:
            raise ConfigError(f"{name} must be between {low} and {high}.")

    if cfg.backup_interval_hours < 1:
        raise ConfigError("backup_interval_hours must be at least 1.")
    if cfg.check_subset_total < 1:
        raise ConfigError("check_subset_total must be at least 1.")
    if cfg.check_subset_current < 1:
        raise ConfigError("check_subset_current must be at least 1.")
    if cfg.split_threshold_gib < 0:
        raise ConfigError("split_threshold_gib cannot be negative.")
    if cfg.split_drift_factor < 1:
        raise ConfigError("split_drift_factor must be at least 1.")

    for name in ("last_backup_ts", "last_check_ts", "last_check_ok_ts", "total_backup_size",
                 "compressed_backup_size", "retention_years", "retention_months",
                 "retention_days", "retention_hours",
                 "last_backup_ok_ts", "last_backup_status_ts",      # fix #9
                 "last_check_status_ts"):
        _finite_nonnegative(getattr(cfg, name), name)

    for name in ("last_backup_status", "last_check_status"):
        value = getattr(cfg, name)
        if not isinstance(value, str) or value not in VALID_OUTCOMES:
            raise ConfigError(f"{name} must be one of: {', '.join(OUTCOMES)} (or empty).")
    for name in ("last_backup_reason", "last_check_reason"):
        if not isinstance(getattr(cfg, name), str):
            raise ConfigError(f"{name} must be a string.")

    for i, directory in enumerate(cfg.directories):
        prefix = f"directories[{i}]"
        if not isinstance(directory, Directory):
            raise ConfigError(f"{prefix} must be a Directory.")
        if not isinstance(directory.path, str) or not directory.path.strip():
            raise ConfigError(f"{prefix}.path must be a nonempty string.")
        if type(directory.enabled) is not bool:
            raise ConfigError(f"{prefix}.enabled must be true or false.")
        if not isinstance(directory.excludes, list) or not all(
            isinstance(item, str) for item in directory.excludes
        ):
            raise ConfigError(f"{prefix}.excludes must be a list of strings.")
        _finite_nonnegative(directory.cached_size, f"{prefix}.cached_size")

    for name, record_type in (("backup_history", BackupRecord), ("check_history", CheckRecord)):
        history = getattr(cfg, name)
        if not isinstance(history, dict):
            raise ConfigError(f"{name} must be an object.")
        for key, record in history.items():
            prefix = f"{name}[{key}]"
            if not isinstance(key, str):
                raise ConfigError(f"{name} keys must be strings.")
            if not isinstance(record, record_type):
                raise ConfigError(f"{prefix} must be a {record_type.__name__}.")
            _finite_nonnegative(record.ts, f"{prefix}.ts")
            _finite_nonnegative(record.status_ts, f"{prefix}.status_ts")
            if not isinstance(record.status, str) or record.status not in VALID_OUTCOMES:
                raise ConfigError(f"{prefix}.status must be one of: "
                                  f"{', '.join(OUTCOMES)} (or empty).")
            if not isinstance(record.reason, str):
                raise ConfigError(f"{prefix}.reason must be a string.")
            if record_type is BackupRecord:
                if type(record.ok) is not bool or type(record.partial) is not bool:
                    raise ConfigError(f"{prefix}.ok/.partial must be true or false.")
                _finite_nonnegative(record.total_backup_size, f"{prefix}.total_backup_size")
                _finite_nonnegative(record.compressed_backup_size,
                                    f"{prefix}.compressed_backup_size")
            else:
                _finite_nonnegative(record.ok_ts, f"{prefix}.ok_ts")
                if type(record.subset_current) is not int or record.subset_current < 1:
                    raise ConfigError(f"{prefix}.subset_current must be a positive integer.")


def _parse_config_file(path: Path) -> Config | None:
    """Read + validate one config file. Returns None if it cannot be used."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None  # unreadable or not valid JSON
    try:
        return Config.from_dict(data)
    except (TypeError, ValueError):
        return None  # malformed fields


def load_config() -> Config:
    """Load configuration.

    * File absent          → defaults (a first run).
    * File corrupt/invalid → fall back to the .bak last-known-good copy written
      by save_config; if *that* is unusable too, raise ConfigError. Defaults
      never silently replace a user's real settings — a corrupted config is
      reported, not hidden.
    """
    with _CONFIG_LOCK:
        main = Path(CONFIG_FILE)
        bak = main.with_name(main.name + ".bak")
        cfg = _parse_config_file(main)
        if cfg is not None:
            return cfg
        cfg = _parse_config_file(bak)
        if cfg is not None:
            return cfg
        if main.exists():
            raise ConfigError(
                f"Could not read a usable configuration from {main} or {bak}. "
                "Nothing was reset or overwritten — fix or remove the file "
                "manually to recover."
            )
        return Config()


def _sync_directory(path: Path) -> None:
    """Best-effort directory fsync for rename durability."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # some filesystems do not support directory fsync


def save_config(cfg: Config) -> None:
    """Atomic, serialized write: unique temp file in the same directory +
    os.replace, then a refreshed .bak last-known-good copy.

    The real config file is never opened in truncating mode, so an interrupted
    or concurrent save cannot leave half-written JSON behind.
    """
    with _CONFIG_LOCK:
        validate_config(cfg)
        payload = json.dumps(
            asdict(cfg), indent=2, ensure_ascii=True, allow_nan=False
        ) + "\n"

        destination = Path(CONFIG_FILE)
        destination.parent.mkdir(parents=True, exist_ok=True)

        fd, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            _sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

        # Refresh the last-known-good copy from the just-validated content.
        try:
            backup = destination.with_name(destination.name + ".bak")
            shutil.copyfile(destination, backup)
        except OSError:
            pass


# Fields maintained by backup/check operations, never by the settings dialog.
# A dialog is built from a config captured when it was opened; if an operation
# finished while the dialog sat open, collect() would carry the stale values
# from opening time back into the config (issue #4).
OPERATION_FIELDS = (
    "last_backup_ts",
    "last_backup_ok",
    "last_backup_partial",
    "last_check_ts",
    "last_check_ok_ts",
    # fix #9: how each scheduled operation's last attempt ended. A dialog opened
    # before an operation ran must not carry its own stale copy back in.
    "last_backup_status",
    "last_backup_status_ts",
    "last_backup_ok_ts",
    "last_backup_reason",
    "last_check_status",
    "last_check_status_ts",
    "last_check_reason",
    "check_subset_current",
    "total_backup_size",
    "compressed_backup_size",
    "backup_history",     # per-identity outcomes: never a dialog's to carry (fix #1)
    "check_history",
)


def merge_settings(live: Config, edited: Config) -> Config:
    """Apply dialog-edited settings onto the *live* configuration.

    Settings (connection, schedule, directories…) come from the dialog, but
    operation bookkeeping (timestamps, subset rotation, size stats, per-dir
    cached_size) is taken from the live config as it is *now* — so saving a
    long-open dialog cannot restore stale operation results, and cannot clobber
    what a finishing backup/check just recorded.

    Fix #1 (restic-box-fixes2.md): bookkeeping belongs to the repository and
    source set it describes. Before the new settings replace it, the current
    outcome is filed in history under the OLD identity; after the save, the
    bookkeeping fields are rebound to the NEW identity's own history. Pointing
    at a repository that was never backed up therefore shows "never backed up",
    not the previous repository's green light — and switching back restores it.
    """
    changes = {
        name: getattr(edited, name)
        for name in Config.__dataclass_fields__
        if name not in OPERATION_FIELDS and name != "directories"
    }
    live_sizes = {d.path: d.cached_size for d in live.directories}
    directories = [
        replace(d, cached_size=live_sizes.get(d.path, d.cached_size))
        for d in edited.directories
    ]
    stashed = stash_bookkeeping(live)
    merged = rebind_bookkeeping(
        replace(stashed, **changes, directories=directories))
    validate_config(merged)
    return merged


def password_file_path(cfg: Config) -> Path:
    value = cfg.password_file.strip()
    if not value:
        raise ValueError("Choose a password-file path first.")
    return Path(value).expanduser()


def password_file_exists(cfg: Config) -> bool:
    try:
        return password_file_path(cfg).is_file()
    except (OSError, ValueError):
        return False


def generate_password(cfg: Config, overwrite: bool = False) -> str:
    """Generate a 48-char random password and write it to the password file.

    Refuses to touch an existing password file unless *overwrite* is explicitly
    set: silently replacing a repository's only password makes existing backups
    unreadable (changing a restic repository password is a key-management
    operation, not a file rewrite). The file is created with O_EXCL at mode 0600
    (plus O_NOFOLLOW: a symlink is never written through), so a secret is never
    briefly world-readable and an existing file can never be clobbered.
    Raises FileExistsError if the file already exists and *overwrite* is False.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    pw = "".join(secrets.choice(alphabet) for _ in range(48))
    p = password_file_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(p), flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ELOOP):
            # EEXIST: file present. ELOOP: a (even dangling) symlink — refuse
            # to write through it and report it like an existing file.
            raise FileExistsError(
                f"Refusing to overwrite {p}"
                + ("" if overwrite else " — pass overwrite=True only if you are "
                   "sure this file is not an existing repository's only password")
            ) from exc
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(pw + "\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if not overwrite:
            # We created this file exclusively; discard an incomplete result
            # rather than leave a plausible-looking truncated credential.
            p.unlink(missing_ok=True)
        raise
    os.chmod(str(p), 0o600)  # enforce 0600 even for an overwrite or odd umask
    _sync_directory(p.parent)
    return pw
