"""Restic command execution helpers.

SFTP transport (issue #5): every command carries the SSH host, port, user and
key that are currently in the settings, passed explicitly via restic's
``-o sftp.command`` option. The app no longer appends a fixed alias to the
user's ~/.ssh/config: an appended alias went stale the moment the settings
changed (once written, nothing updated it), and OpenSSH's first-match rule
could let an earlier ``Host *`` block override parts of it anyway. The
settings dialog's Test Connection and the reachability pre-flight ask for the
exact thing a backup needs — the SFTP subsystem, through the same ssh command
(fix #7) — so a passed test, a passed pre-flight and a real backup are the
same question with the same answer.

``disable_legacy_ssh_alias()`` comments out the stale alias older versions
wrote — commenting, never deleting, so nothing the user owns is lost.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from restic_config import Config, Directory, password_file_path
import restic_diag_log as diag_log

# restic exit codes
RC_OK = 0
RC_PARTIAL = 3  # some source files unreadable — backup still created

# Wall-clock bound for the SSH reachability probe (restic_runner.ssh_transport_ok).
# ssh's own ConnectTimeout=10 covers the connection; this bounds the whole probe,
# including slow ProxyCommand helpers.
SSH_PROBE_TIMEOUT = 20

# Issue #14: process output is retained only up to these bounds. A backup can
# run for days and stream hundreds of thousands of progress records; they are
# consumed for the progress bar and deliberately NOT kept. Diagnostic output
# that callers do keep is trimmed oldest-first, whole lines only — callers
# json-parse individual lines (snapshots/ls/stats output), so trimming works by
# dropping whole records. A record so large that even one of them overflows the
# cap is excerpted visibly instead of being kept whole (review fix #6, below).
# The query cap is deliberately generous: it is a backstop against runaway
# output, not a working limit.
MAX_BACKUP_OUTPUT_CHARS = 256 * 1024   # backup diagnostics (run_backup)
MAX_QUERY_OUTPUT_CHARS = 8 * 1024 * 1024  # run_restic output that callers parse

# Review fix #6: those totals are only a bound if no single record can blow
# through them. A line longer than its per-line limit is kept as its head plus a
# visible truncation note, and its full text is written to the rotating
# diagnostic log file (restic_diag_log) — so memory stays bounded without the
# detail simply vanishing.
#
# The limits differ by caller: query output is json-parsed record by record, so
# one record is allowed to use the whole query budget (a snapshots --json stream
# really is one line); backup diagnostics are human-facing, so they get a much
# tighter per-line limit.
MAX_QUERY_LINE_CHARS = MAX_QUERY_OUTPUT_CHARS
MAX_BACKUP_LINE_CHARS = 64 * 1024


class _LineTail:
    """Most recent output lines, bounded in total size AND per line (issue #14,
    review fix #6).

    Two independent bounds:

    * total retained characters stay within ``limit``, oldest whole lines
      evicted first;
    * no retained line exceeds ``max_line`` characters. An oversized record is
      stored as its head plus a truncation note, and its full text is handed to
      ``restic_diag_log.record_oversized`` so the detail still reaches the user
      via the rotating diagnostic log file.

    Before fix #6 only the first bound existed: "keep the newest line even if it
    alone exceeds the cap" let one exceptional line retain an arbitrary amount
    of memory.
    """

    def __init__(self, limit: int, max_line: int | None = None) -> None:
        self.limit = limit
        self.max_line = min(max_line or limit, limit)
        self.parts: deque[str] = deque()
        self.length = 0
        self.dropped = 0
        self.truncated = 0

    def append(self, line: str) -> str | None:
        """Retain one line. Returns the truncation note when the line had to be
        cut, so the caller can surface it where the user will see it."""
        note = None
        if len(line) > self.max_line:
            self.truncated += 1
            # Room for the note inside max_line, so the stored line never
            # exceeds it (for pathologically small caps, the note shrinks).
            note_limit = max(8, min(diag_log.NOTE_RESERVE, self.max_line // 3))
            where = diag_log.record_oversized(line, limit=self.max_line,
                                              source="restic output")
            note = diag_log.truncation_note(len(line) - (self.max_line - note_limit),
                                            str(where) if where
                                            else "diagnostic log unavailable")
            if len(note) > note_limit:
                note = note[: note_limit - 1] + "…"
            line = line[: self.max_line - len(note)] + note
        self.parts.append(line)
        self.length += len(line) + 1
        # Every stored line is at most max_line <= limit, so this loop can now
        # always reach the bound: the newest line no longer gets an exemption.
        while self.length > self.limit and len(self.parts) > 1:
            first = self.parts.popleft()
            self.length -= len(first) + 1
            self.dropped += 1
        return note

    def text(self) -> str:
        if self.dropped:
            head = f"[restic-box: dropped {self.dropped} earlier output line(s)]"
            return head + "\n" + "\n".join(self.parts)
        return "\n".join(self.parts)


class QueryError(RuntimeError):
    """A restic *query* (snapshots / ls / stats) failed or its output was
    incomplete (review fix #6, restic-box-fixes2.md).

    Before this, every failure path in the query helpers returned `[]` — the
    same value a genuinely empty repository produces. Callers then treated "the
    SFTP server closed the connection mid-listing" as "this repository has no
    snapshots", which let the friendly-view rebuild replace a correct view with
    an empty one. A query now answers with a complete list, or raises:

    * empty list            → the repository/snapshot really is empty;
    * QueryError           → unknown: failed command, unparsable or truncated
                             output, missing JSON listing.
    """

    def __init__(self, message: str, rc: int | None = None,
                 output: str = "") -> None:
        super().__init__(message)
        self.rc = rc
        self.output = output


_LEGACY_SSH_ALIAS = "restic-storagebox"        # alias older versions appended
_MANAGED_SSH_MARK = "# restic-box managed entry"  # ... behind this marker
_MANAGED_SSH_DISABLED_MARK = _MANAGED_SSH_MARK + (
    " (commented out by restic-box: settings are now applied directly,"
    " keeping this entry for reference)"
)


def _single_line(value: str, name: str) -> str:
    """Reject values that could smuggle line breaks into commands or URLs."""
    if not isinstance(value, str) or any(c in value for c in ("\x00", "\r", "\n")):
        raise ValueError(f"{name} must not contain line breaks.")
    return value


def _validated_ssh_fields(cfg: Config) -> tuple[str, str, int, str]:
    host = _single_line(cfg.ssh_host, "SSH host").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # bracketed IPv6 input is fine; ssh wants the bare host
    if (
        not host
        or host.startswith("-")  # an option-looking host would reach ssh as a flag
        or any(c.isspace() for c in host)
        or any(c in host for c in "/\\@")
    ):
        raise ValueError("Enter a valid SSH hostname or IP address.")

    user = _single_line(cfg.ssh_user, "SSH user").strip()
    if user and (
        user.startswith("-")
        or any(c.isspace() for c in user)
        or any(c in user for c in "/\\@:")
    ):
        raise ValueError("Enter a valid SSH username.")

    if type(cfg.ssh_port) is not int or not 1 <= cfg.ssh_port <= 65535:
        raise ValueError("SSH port must be an integer from 1 to 65535.")

    key = _single_line(cfg.ssh_key, "SSH key").strip()
    if key:
        # Absolute paths keep a relative name from being read as an SSH option
        # token (e.g. "none") by ssh's config parser. An empty key is allowed:
        # ssh then uses the default identities / ssh-agent, as usual.
        key = str(Path(key).expanduser().absolute())
    return host, user, cfg.ssh_port, key


def ssh_command(cfg: Config) -> list[str]:
    """Explicit SSH transport built from the supplied configuration: the ssh
    invocation that opens the server's SFTP subsystem.

    This is the *only* SSH transport the app has: restic gets it verbatim as
    ``-o sftp.command``, the reachability pre-flight runs it, and the settings
    dialog's Test Connection runs it. One builder, one transport, so the probe
    cannot drift away from the backup (review fix #4 and fix #7).

    The list is exec'd directly (never through a shell).
    """
    host, user, port, key = _validated_ssh_fields(cfg)
    command = ["ssh", "-p", str(port)]
    if user:
        command += ["-l", user]
    if key:
        command += ["-i", key]
    command += [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=240",
        "-s", host, "sftp",
    ]
    return command


def ensure_ssh_config(cfg: Config) -> None:
    """Compatibility hook: validate the SFTP fields before a command runs.

    This deliberately never touches ~/.ssh/config — transport comes from
    ssh_command() instead (issue #5). Old managed aliases are commented out
    by disable_legacy_ssh_alias(), once per app start.
    """
    if cfg.backend == "sftp":
        _validated_ssh_fields(cfg)


def _comment_line(line: str) -> str:
    """Prefix a config line with ``# `` in place, keeping the line's original
    content and trailing newline untouched; blank and already-commented lines
    are returned unchanged (never double-commented)."""
    if not line.strip() or line.strip().startswith("#"):
        return line
    return "# " + line


def disable_legacy_ssh_alias() -> bool:
    """Comment out — never delete — the fixed SSH alias older restic-box
    versions appended to ~/.ssh/config (issue #5 residue: it could never be
    updated in place, and current commands carry their settings explicitly).

    Deletion would destroy something the user may have come to rely on or
    hand-edit, so every line of our own block only gains a ``# `` prefix:
    ssh stops honouring the alias, but the entry stays visible, keeps its
    exact content, and can be re-enabled or removed by the user at will.

    This edits a file the user owns and hand-edits, so the rules are strict:

    - Nothing is created, appended, reordered, or reformatted. Everything that
      is not part of our own block is written back byte-for-byte as it was
      read, including blank lines and the file's line endings.
    - A marker line is treated as ours only when the next non-blank line is our
      own ``Host restic-storagebox`` header. A user comment that happens to look
      like the marker — or a marker left orphaned by hand-editing — is left in
      place, together with the block below it.
    - The commented region covers everything OpenSSH attributes to our Host
      block: the header, indented body lines, blank lines, and already-commented
      lines (a user who commented some line by hand gets it left exactly as it
      was, never double-commented). It stops at the first unindented,
      uncommented line, so a user directive immediately after our block is
      never commented out. Stopping early at a hand-added comment instead
      would strand the remaining indented lines outside any Host block, where
      OpenSSH would apply them globally.
    - The marker line itself is replaced with one that explains why the block
      below it is commented; that also makes a second run a clean no-op.
    - The rewrite is atomic (temp file in the same directory + ``os.replace``),
      preserves the file's mode (minus group/other write bits), preserves its
      line endings, and keeps any symlink indirection instead of replacing the
      symlink with a plain file.
    - A damaged, unreadable, or unwritable configuration is left exactly as it
      is and no error propagates: a stale alias is never a reason for the tray
      app to fail to start.

    Returns True only if the file actually changed.
    """
    try:
        ssh_cfg = Path.home() / ".ssh" / "config"
        # newline="" disables universal-newline translation: a file that uses
        # CRLF must be written back with CRLF, not silently reformatted.
        with open(ssh_cfg, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
        return False
    if _MANAGED_SSH_MARK not in text:
        return False

    lines = text.splitlines(keepends=True)
    alias_header = re.compile(rf"^Host\s+{re.escape(_LEGACY_SSH_ALIAS)}$")
    out: list[str] = []
    disabled_blocks = 0
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        if line.strip() != _MANAGED_SSH_MARK:
            out.append(line)
            i += 1
            continue

        # Verify the marker is ours before touching anything under it.
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j >= n or not alias_header.match(lines[j].strip()):
            out.append(line)  # a user's comment, not our block
            i += 1
            continue

        disabled_blocks += 1
        # Replace our own marker with one that explains the commented-out
        # block; the newline style of the original line is kept.
        marker_eol = line[len(line.rstrip("\r\n")):]
        out.append(_MANAGED_SSH_DISABLED_MARK + marker_eol)
        if j > i + 1:
            out.extend(lines[i + 1:j])  # blank lines between marker and header
        out.append(_comment_line(lines[j]))  # our Host header line
        i = j + 1
        while i < n:
            body = lines[i]
            stripped = body.strip()
            if stripped == _MANAGED_SSH_MARK:
                break  # another managed block starts here: let the outer loop
                # validate and disable it too (never absorb it as plain comment)
            if stripped and not body[:1].isspace() and not stripped.startswith("#"):
                break  # an unindented, uncommented line is the user's own directive
            out.append(_comment_line(body))
            i += 1

    if not disabled_blocks:
        return False
    new_text = "".join(out)
    if new_text == text:
        return False

    # Write through the real target so a symlinked ~/.ssh/config keeps its
    # indirection rather than becoming a regular file.
    try:
        target = Path(os.path.realpath(ssh_cfg))
        mode = target.stat().st_mode & 0o7777
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                    dir=str(target.parent))
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(new_text)   # exact bytes: no newline reformatting
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode & ~0o022)  # never group/world writable
            os.replace(temporary, target)
        except OSError:
            temporary.unlink(missing_ok=True)
            return False
        temporary.unlink(missing_ok=True)  # no-op once replaced
    except OSError:
        return False
    return True


def ssh_probe_command(cfg: Config) -> list[str]:
    """The command used to test SSH connectivity: the transport the backups use,
    byte for byte — ssh with the backup's host, port, user, key and options,
    opening the server's SFTP subsystem (restic's ``sftp.command``).

    Review fix #4 replaced a raw TCP socket probe: only OpenSSH honours
    ProxyCommand / ProxyJump / Match blocks / IdentitiesOnly, so a socket probe
    could reject a configuration whose real SSH connection works.

    Review fix #7 replaced the trailing ``echo OK`` remote command with the
    subsystem request itself. An SSH account can offer SFTP while refusing
    remote command execution — SFTP-only storage providers are configured
    exactly that way — and the old probe rejected those repositories even
    though restic, which never asks for a shell, backs them up happily. Asking
    for the subsystem asks for the one thing restic needs, using the same
    options, and nothing else.
    """
    return ssh_command(cfg)


def classify_ssh_probe(returncode: int, stderr: str) -> tuple[bool, str]:
    """One success/failure rule, shared by the reachability pre-flight and the
    settings dialog's Test Connection (issue #5, review fixes #4 and #7), so a
    passed test and a real backup mean the same connection:

      - auth failures in stderr   -> unreachable
      - ssh's own error code 255  -> unreachable: no route, connection refused,
        ProxyCommand/ProxyJump failure, bad key, host key refused, or the server
        refusing the SFTP subsystem that restic requires (OpenSSH reports that
        as "Subsystem request failed on channel 0" and exits 255)
      - any other exit code       -> reachable: SSH connected, authenticated and
        opened the subsystem channel; the session simply ended. sftp-server
        exits with its own status when its input closes, which says nothing
        about the transport. The authoritative test of the repository is still
        what restic itself reports when it runs.

    Returns ``(reachable, reason)``; reason is empty when reachable.
    """
    stderr = (stderr or "").strip()
    auth_failed = any(s in stderr for s in ("Permission denied", "Authentication failed",
                                           "Host key verification failed"))
    if auth_failed:
        return False, f"SSH authentication failed: {stderr}"
    if returncode == 255:
        if "subsystem" in stderr.lower():
            return False, ("the SSH server does not offer the SFTP subsystem that "
                           f"restic needs: {stderr}")
        return False, f"SSH transport failed: {stderr or 'ssh exited with 255'}"
    return True, ""


def ssh_transport_ok(cfg: Config, *, timeout: int = SSH_PROBE_TIMEOUT) -> tuple[bool, str]:
    """Probe the SSH transport the backups would use (ssh_probe_command).

    Returns ``(reachable, reason)``; reason is empty when reachable and a
    human-readable explanation otherwise. Only meaningful for the sftp backend:
    local and rclone repositories have no SSH transport, so they are always
    reachable and nothing is launched for them.

    The probe is a real ssh invocation, which is the point (review fix #4):
    OpenSSH itself resolves ProxyCommand / ProxyJump / Match blocks, so a
    transport that works for restic also passes the probe, and one that does
    not is reported before an operation starts. It asks for the SFTP subsystem
    rather than a remote command (review fix #7), so a server that permits SFTP
    but forbids shells passes — as it should.

    stdin is /dev/null: a subsystem session forwards stdin to the remote
    sftp-server, so leaving the tray process's own stdin attached would feed
    that session whatever the terminal has buffered.
    """
    if cfg.backend != "sftp":
        return True, ""
    command = ssh_probe_command(cfg)   # raises ValueError on hostile field values
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"SSH probe timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not run the SSH probe: {exc}"
    return classify_ssh_probe(result.returncode, result.stderr)


def is_host_reachable(cfg: Config) -> bool:
    """Reachability pre-flight for an operation.

    Review fix #4: this is an SSH probe built from the same transport options
    as the backup (ssh_probe_command), NOT a raw TCP probe of host:port — so a
    ProxyCommand/ProxyJump configuration is no longer falsely rejected. Review
    fix #7: the probe asks for the SFTP subsystem, so an SFTP-only account is
    no longer rejected either. It is still only a pre-flight: the authoritative
    behaviour is what restic itself reports when it runs.
    """
    reachable, _reason = ssh_transport_ok(cfg)
    return reachable


def repo_url(cfg: Config) -> str:
    path = _single_line(cfg.repo_path, "Repository path").strip()
    if not path:
        raise ValueError("Repository path must not be empty.")

    if cfg.backend == "local":
        return path
    if cfg.backend == "rclone":
        remote = _single_line(cfg.rclone_remote, "Rclone remote").strip().rstrip(":")
        if not remote:
            raise ValueError("Choose a valid rclone remote name.")
        return f"rclone:{remote}:{path}"
    if cfg.backend == "sftp":
        host, user, _port, _key = _validated_ssh_fields(cfg)
        display_host = f"[{host}]" if ":" in host else host
        target = f"{user}@{display_host}" if user else display_host
        return f"sftp:{target}:{path}"
    raise ValueError(f"Unknown backend: {cfg.backend!r}")


def list_rclone_remotes() -> list[str] | None:
    """Return list of remotes, or None if rclone is not installed."""
    try:
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]


def _base_cmd(cfg: Config) -> list[str]:
    command = [
        "restic",
        "-r", repo_url(cfg),
        "--password-file", str(password_file_path(cfg)),
    ]
    if cfg.backend == "sftp":
        # restic parses sftp.command as an argument list; shlex.join keeps
        # spaces/quotes inside key paths intact without invoking a shell.
        command += ["-o", "sftp.command=" + shlex.join(ssh_command(cfg))]
    return command


OutputCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[float, int, int], None]]
ProcCallback = Optional[Callable[["subprocess.Popen[str]"], None]]


def stop_process(proc: "subprocess.Popen[str]", timeout: float = 10.0) -> None:
    """Terminate a tracked process, escalating to kill if it does not cooperate.

    Every restic command this module starts gets its own process group
    (start_new_session), so signalling the group also takes down the sftp/ssh
    helpers restic spawns — otherwise they keep the stdout pipe open and the
    reader thread never finishes, which is how "Stop All" used to hang.
    Bounded: SIGTERM, wait(timeout), then SIGKILL. Never raises: a process
    that already exited is fine.
    """
    if proc.poll() is not None:
        return
    group_id = None
    if os.name == "posix":
        try:
            if os.getpgid(proc.pid) == proc.pid:
                group_id = proc.pid
        except ProcessLookupError:
            return

    def send(sig: int) -> None:
        try:
            if group_id is not None:
                os.killpg(group_id, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run_restic(
    args: list[str],
    cfg: Config,
    on_output: OutputCallback = None,
    on_proc: ProcCallback = None,
) -> tuple[int, str]:
    proc = subprocess.Popen(
        _base_cmd(cfg) + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    if on_proc:
        on_proc(proc)
    retained = _LineTail(MAX_QUERY_OUTPUT_CHARS, MAX_QUERY_LINE_CHARS)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        note = retained.append(line)
        if on_output:
            on_output(line)
            if note:
                on_output(note)      # the user must see that this record was excerpted
    proc.stdout.close()              # every caller runs this constantly: no leaked pipe
    proc.wait()
    return proc.returncode, retained.text()


def _query_lines(
    args: list[str],
    cfg: Config,
    on_line: Callable[[str], None],
    on_output: OutputCallback = None,
    on_proc: ProcCallback = None,
) -> int:
    """Run a restic query, handing every stdout line to *on_line* as it arrives.

    Streamed on purpose (review fix #6). `run_restic()` retains a bounded tail
    of raw text — right for human diagnostics, wrong for machine-parsed
    listings: a listing larger than the tail used to be parsed from a truncated
    buffer, silently answering "these are all the snapshots" with a partial
    list. Here nothing is retained as text, so a listing of any size is either
    parsed in full (memory bounded by what the caller actually needs) or the
    query is reported as failed. The caller drains to EOF even after recording
    an error, so the child never blocks on a full pipe.
    """
    proc = subprocess.Popen(
        _base_cmd(cfg) + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    if on_proc:
        on_proc(proc)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            on_line(line)
            if on_output:
                on_output(line)
    finally:
        proc.stdout.close()
    proc.wait()
    return proc.returncode


def init_repo(cfg: Config, on_output: OutputCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    return run_restic(["init"], cfg, on_output)


def targets_of(directory) -> list[str]:
    """Backup targets for a Directory or a split unit (units may carry several paths)."""
    return list(getattr(directory, "paths", None) or [directory.path])


def run_backup(
    cfg: Config,
    directory: Directory,
    on_output: OutputCallback = None,
    on_progress: ProgressCallback = None,
    on_proc: ProcCallback = None,
    tag: str | None = None,
) -> tuple[int, str, int]:
    """Run restic backup with --json output for progress streaming.

    tag: an optional restic --tag value stamped on the snapshot; the indicator
    passes `restic-box-run=<id>` so every unit of one backup operation is one
    identifiable run in the bydate/ view (issue #8).

    Returns ``(returncode, output_lines, data_added)`` where *data_added* is
    restic's summary ``data_added`` — the size of newly added tree data
    **before** deduplication/compression (restic reports the uploaded,
    compressed volume separately only via stats).
    """
    ensure_ssh_config(cfg)
    args = ["backup", *targets_of(directory), f"--compression={cfg.compression}", "--json"]
    if tag:
        args += ["--tag", tag]
    # Network shares (CIFS/SMB/NFS) can renumber inodes and bump ctime between backup
    # runs even when contents are unchanged; restic then re-reads every file as "new"
    # (full-read churn, re-uploading unchanged data). --ignore-inode (which also covers
    # ctime) is the usual fix; --ignore-ctime handles shares where only ctime drifts.
    if cfg.ignore_inode:
        args.append("--ignore-inode")
    if cfg.ignore_ctime:
        args.append("--ignore-ctime")
    excl_file: str | None = None
    result_data_added: int = 0
    # Fix #2 (restic-box-fixes2.md): `.zfs` is the ZFS snapshot directory —
    # traversing .zfs/snapshot would back up the dataset's entire history as
    # duplicate data. The split planner already skips it while sizing; restic
    # itself must skip it too. A bare pattern (no slash) matches the name at
    # any depth, so dataset roots and nested datasets are covered alike.
    # A user who typed .zfs into the excludes keeps their own entry (shown in
    # the settings UI, used exactly once), and a user who deliberately pointed
    # a source path INTO .zfs gets that backup: the guard only covers traversal
    # around a target, never the target they chose.
    targets = targets_of(directory)
    deliberate = any(".zfs" in PurePosixPath(t).parts for t in targets)
    excludes = list(directory.excludes)
    if not deliberate and ".zfs" not in excludes:
        excludes.insert(0, ".zfs")
    if len(excludes) > 200:
        # split "rest" units can carry thousands of excludes; keep argv small
        fd, excl_file = tempfile.mkstemp(prefix="restic-box-excl-", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(excludes) + "\n")
        args += ["--exclude-file", excl_file]
    else:
        for exc in excludes:
            args += ["--exclude", exc]

    try:
        proc = subprocess.Popen(
            _base_cmd(cfg) + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        if on_proc:
            on_proc(proc)
        retained = _LineTail(MAX_BACKUP_OUTPUT_CHARS, MAX_BACKUP_LINE_CHARS)
        assert proc.stdout is not None

        def retain(raw: str) -> None:
            """Retain one diagnostic record, streaming it (and, when it had to be
            excerpted, the truncation note) to the caller's log."""
            note = retained.append(raw)
            if on_output:
                on_output(raw)
                if note:
                    on_output(note)

        for raw in proc.stdout:
            raw = raw.rstrip()
            try:
                msg = json.loads(raw)
                mtype = msg.get("message_type", "")
                if mtype == "status":
                    if on_progress:
                        on_progress(
                            float(msg.get("percent_done", 0.0)),
                            int(msg.get("bytes_done", 0)),
                            int(msg.get("total_bytes", 0)),
                        )
                    # progress is shown in the tray — a days-long backup
                    # streams hundreds of thousands of these; never retained
                    # and never written to the diagnostic log either (issue #14,
                    # review fix #6): parsing and discarding is the whole point.
                elif mtype == "summary":
                    data_added = msg.get("data_added", 0)
                    # The summary's total_bytes_processed is the authoritative
                    # processed volume; emit it as a final 100% progress event
                    # so callers that record the last total get the truth,
                    # not whatever the last intermediate status happened to say.
                    processed = msg.get("total_bytes_processed")
                    if processed is not None and on_progress:
                        on_progress(1.0, int(processed), int(processed))
                    note = retained.append(raw)
                    if note and on_output:
                        on_output(note)
                    if on_output:
                        on_output(
                            f"Done — {msg.get('files_new', 0)} new files, "
                            f"{fmt_bytes(data_added)} added (uncompressed)"
                        )
                    result_data_added = data_added
                else:
                    retain(raw)
            except (json.JSONDecodeError, ValueError):
                retain(raw)

        proc.stdout.close()          # the child is done writing: release the pipe
        proc.wait()
        return proc.returncode, retained.text(), result_data_added
    finally:
        if excl_file:
            try:
                os.unlink(excl_file)
            except OSError:
                pass


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"



def run_check(cfg: Config, on_output: OutputCallback = None, on_proc: ProcCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    args = ["check"]
    if cfg.check_read_data_subset:
        args.append(f"--read-data-subset={cfg.check_subset_current}/{cfg.check_subset_total}")
    return run_restic(args, cfg, on_output, on_proc)


def run_check_full(cfg: Config, on_output: OutputCallback = None, on_proc: ProcCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    return run_restic(["check", "--read-data"], cfg, on_output, on_proc)


def run_unlock(cfg: Config, on_output: OutputCallback = None, on_proc: ProcCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    return run_restic(["unlock"], cfg, on_output, on_proc)


def run_snapshots(cfg: Config) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    return run_restic(["snapshots", "--last"], cfg)


def run_snapshots_json(cfg: Config) -> list[dict]:
    """Every snapshot in the repository, as restic reports them.

    Raises QueryError when the command failed, produced no JSON listing, or the
    listing could not be parsed. An empty list means the repository genuinely
    has no snapshots (review fix #6).
    """
    ensure_ssh_config(cfg)
    snapshots: list[dict] | None = None
    problem: str | None = None

    def on_line(line: str) -> None:
        # SSH warnings can precede the JSON; the listing itself is one line
        # that starts with '[' — parse it, and only it.
        nonlocal snapshots, problem
        if snapshots is not None or problem is not None:
            return
        line = line.strip()
        if not line.startswith("["):
            return
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            # Truncated or torn JSON: incomplete output, NOT a short listing.
            problem = f"unparsable snapshot listing: {exc}"
            return
        if not isinstance(parsed, list):
            problem = "snapshot listing was not a JSON array"
            return
        snapshots = parsed

    rc = _query_lines(["snapshots", "--json"], cfg, on_line)
    if rc not in (RC_OK, RC_PARTIAL):
        raise QueryError(f"restic snapshots failed (exit {rc})", rc)
    if problem is not None:
        raise QueryError(problem, rc)
    if snapshots is None:
        raise QueryError("restic snapshots produced no JSON listing", rc)
    return snapshots


def run_raw_data_size(cfg: Config, on_output: OutputCallback = None,
                      on_proc: ProcCallback = None) -> float | None:
    """Return the compressed/deduplicated repository data size reported by restic stats.

    None means "unknown" — a failed or unparsable stats query, not a zero-byte
    repository (review fix #6: callers already treat None as unknown).
    """
    ensure_ssh_config(cfg)
    rc, out = run_restic(["stats", "--mode", "raw-data", "--json"], cfg,
                         on_output, on_proc)
    if rc not in (RC_OK, RC_PARTIAL):
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            stats = json.loads(line)
        except json.JSONDecodeError:
            continue
        size = stats.get("total_size")
        if isinstance(size, (int, float)):
            return float(size)
    return None


def run_ls_json(cfg: Config, snapshot_id: str) -> list[dict]:
    """All records restic `ls --json` reports for *snapshot_id*.

    Raises QueryError on a failed command or incomplete output; the caller can
    tell "snapshot has no files" from "the listing did not finish" (review
    fix #6). Records arrive parsed line by line, so a listing larger than the
    retained-output cap is returned complete instead of truncated.
    """
    ensure_ssh_config(cfg)
    entries: list[dict] = []
    problem: str | None = None

    def on_line(line: str) -> None:
        nonlocal problem
        if problem is not None:
            return                      # keep draining: never strand the child
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            return                      # ssh chatter before the records
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            problem = f"unparsable ls record: {exc}"

    rc = _query_lines(["ls", "--json", snapshot_id], cfg, on_line)
    if rc not in (RC_OK, RC_PARTIAL):
        raise QueryError(f"restic ls failed (exit {rc})", rc)
    if problem is not None:
        raise QueryError(problem, rc)
    if not entries:
        raise QueryError(f"restic ls produced no records for {snapshot_id}", rc)
    return entries


def run_ls_children(cfg: Config, snapshot_id: str, path: str) -> list[tuple[str, bool]]:
    """Immediate children of *path* within a snapshot, as restic sees them.

    restic lists the whole subtree under *path*; only the direct children are
    wanted (names, and whether each is a directory). Tree paths come back absolute
    because every backup target here is absolute.

    An empty list means the directory really has no children. A command failure
    or a torn listing raises QueryError — an unknown directory must never be
    published to the friendly views as an empty one (review fix #6).
    """
    ensure_ssh_config(cfg)
    target = "/" + path.strip("/")
    want_parent = target if target != "/" else "/"
    children: list[tuple[str, bool]] = []
    records = 0
    problem: str | None = None

    def on_line(line: str) -> None:
        # Filtered while streaming: only direct children are retained, so a
        # huge subtree costs the parent list only its own entries.
        nonlocal records, problem
        if problem is not None:
            return
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            return
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            problem = f"unparsable ls record: {exc}"
            return
        if entry.get("struct_type") not in ("node", "snapshot"):
            return
        records += 1
        if entry.get("struct_type") != "node":
            return
        entry_path = entry.get("path") or ""
        if (entry_path.rsplit("/", 1)[0] or "/") != want_parent:
            return
        name = entry_path.rsplit("/", 1)[-1]
        if name:
            children.append((name, entry.get("type") == "dir"))

    rc = _query_lines(["ls", "--json", snapshot_id, target], cfg, on_line)
    if rc not in (RC_OK, RC_PARTIAL):
        raise QueryError(f"restic ls failed (exit {rc})", rc)
    if problem is not None:
        raise QueryError(problem, rc)
    if records == 0:
        # restic always answers with at least the snapshot record: silence means
        # the listing never finished, not that the directory is empty.
        raise QueryError(
            f"restic ls produced no records for {snapshot_id}:{target}", rc)
    return children


def retention_duration(cfg: Config) -> str:
    """Build the --keep-within duration string, e.g. '1y2m3d4h'. Returns '' if all zero."""
    parts = [
        (cfg.retention_years,  "y"),
        (cfg.retention_months, "m"),
        (cfg.retention_days,   "d"),
        (cfg.retention_hours,  "h"),
    ]
    return "".join(f"{v}{u}" for v, u in parts if v > 0)


def run_retention_forget(cfg: Config, on_output: OutputCallback = None,
                         on_proc: ProcCallback = None) -> tuple[int, str]:
    """Run forget --keep-within <duration> --prune.

    Explicit retention scope + grouping policy (review fix #3). The grouping
    and window semantics below were verified against real restic (see
    tests/test_retention_scope.py), not just from the man page:

    - OWNERSHIP: restic-box owns the WHOLE repository named in its settings.
      A restic-box repository is expected to be dedicated to restic-box; the
      app deliberately sends no --host/--path/--tag filter, so the age window
      applies to every snapshot in that repository — including ones created by
      other tools or hosts if the repository is shared anyway. A --path filter
      is the alternative, but it is worse: whole snapshot groups outside the
      current settings would never be considered again and would leak forever.
    - GROUPING: restic's forget groups snapshots by (host, paths) BY DEFAULT;
      tags join the grouping only when --group-by tag is requested, and this
      app never changes --group-by. The per-run ``restic-box-run=…`` tags are
      run identities for the snapshot browser's bydate/ views, nothing more:
      they do NOT create separate retention groups.
    - WINDOW BASE: --keep-within is measured per group, from that group's own
      newest snapshot (restic prints "keep all snapshots within X of the
      newest" once per group). Within a group, snapshots older than
      newest-of-that-group minus the window are forgotten and pruned.
    - Consequence for split plans: each (host, paths) group ages on its own
      schedule. A group abandoned by a settings edit still has its own newest
      snapshot, so that ONE snapshot survives indefinitely — restic-box
      cannot age it out with --keep-within alone. Removing a source path
      therefore needs a manual forget (the snapshot browser does this), which
      is why the settings tab says so out loud.
    - Duration units: restic accepts only y/m/d/h (m = month) and rejects
      finer units such as seconds, which is what retention_duration builds.
    - Automatic retention is gated by the indicator on fully successful runs
      only (see _run_backup: partial/failed runs never trigger forget).
    """
    ensure_ssh_config(cfg)
    duration = retention_duration(cfg)
    if not duration:
        return RC_OK, ""
    return run_restic(["forget", "--keep-within", duration, "--prune"], cfg,
                      on_output, on_proc)


def run_forget(cfg: Config, snapshot_id: str, prune: bool = True,
               on_output: OutputCallback = None,
               on_proc: ProcCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    args = ["forget", snapshot_id]
    if prune:
        args.append("--prune")
    return run_restic(args, cfg, on_output, on_proc)


def run_prune(cfg: Config, on_output: OutputCallback = None,
              on_proc: ProcCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    return run_restic(["prune"], cfg, on_output, on_proc)


def start_mount(cfg: Config, mountpoint: str) -> "subprocess.Popen[str]":
    """Start `restic mount <mountpoint>` and return the blocking Popen."""
    ensure_ssh_config(cfg)
    return subprocess.Popen(
        _base_cmd(cfg) + ["mount", mountpoint],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
