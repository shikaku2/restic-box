"""Restic command execution helpers."""

# restic exit codes
RC_OK = 0
RC_PARTIAL = 3  # some source files unreadable — backup still created

import json
import socket
import subprocess
from pathlib import Path
from typing import Callable, Optional

from restic_config import Config, Directory, password_file_path

_SSH_ALIAS = "restic-storagebox"
_ssh_config_done = False  # written once per process; SSH config rarely changes


def ensure_ssh_config(cfg: Config) -> None:
    """Write the storage-box SSH alias to ~/.ssh/config if not already present."""
    global _ssh_config_done
    if cfg.backend != "sftp" or _ssh_config_done:
        return
    ssh_cfg = Path.home() / ".ssh" / "config"
    marker = f"Host {_SSH_ALIAS}"
    entry = (
        f"\n# restic-box managed entry\n"
        f"Host {_SSH_ALIAS}\n"
        f"  HostName {cfg.ssh_host}\n"
        f"  User {cfg.ssh_user}\n"
        f"  Port {cfg.ssh_port}\n"
        f"  IdentityFile {cfg.ssh_key}\n"
        f"  StrictHostKeyChecking accept-new\n"
        f"  ServerAliveInterval 60\n"
    )
    existing = ssh_cfg.read_text() if ssh_cfg.exists() else ""
    if marker not in existing:
        ssh_cfg.parent.mkdir(parents=True, exist_ok=True)
        with open(ssh_cfg, "a") as f:
            f.write(entry)
        ssh_cfg.chmod(0o600)
    _ssh_config_done = True


def is_host_reachable(cfg: Config) -> bool:
    """TCP-probe the SFTP host; always True for local backend."""
    if cfg.backend != "sftp":
        return True
    try:
        with socket.create_connection((cfg.ssh_host, cfg.ssh_port), timeout=3):
            return True
    except OSError:
        return False


def repo_url(cfg: Config) -> str:
    if cfg.backend == "local":
        return cfg.repo_path
    if cfg.backend == "rclone":
        remote = cfg.rclone_remote.strip().rstrip(":")
        return f"rclone:{remote}:{cfg.repo_path}"
    return f"sftp:{_SSH_ALIAS}:{cfg.repo_path}"


def list_rclone_remotes() -> list[str]:
    try:
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]


def _base_cmd(cfg: Config) -> list[str]:
    return [
        "restic",
        "-r", repo_url(cfg),
        "--password-file", str(password_file_path(cfg)),
    ]


OutputCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[float, int, int], None]]
ProcCallback = Optional[Callable[["subprocess.Popen[str]"], None]]


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
    )
    if on_proc:
        on_proc(proc)
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        if on_output:
            on_output(line)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def init_repo(cfg: Config, on_output: OutputCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    return run_restic(["init"], cfg, on_output)


def run_backup(
    cfg: Config,
    directory: Directory,
    on_output: OutputCallback = None,
    on_progress: ProgressCallback = None,
    on_proc: ProcCallback = None,
) -> tuple[int, str]:
    """Run restic backup with --json output for progress streaming."""
    ensure_ssh_config(cfg)
    args = ["backup", directory.path, f"--compression={cfg.compression}", "--json"]
    for exc in directory.excludes:
        args += ["--exclude", exc]

    proc = subprocess.Popen(
        _base_cmd(cfg) + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if on_proc:
        on_proc(proc)
    lines: list[str] = []
    assert proc.stdout is not None

    for raw in proc.stdout:
        raw = raw.rstrip()
        lines.append(raw)
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
                # progress is shown in the tray — skip logging status lines
            elif mtype == "summary":
                if on_output:
                    on_output(
                        f"Done — {msg.get('files_new', 0)} new files, "
                        f"{fmt_bytes(msg.get('data_added', 0))} added"
                    )
            elif on_output:
                on_output(raw)
        except (json.JSONDecodeError, ValueError):
            if on_output:
                on_output(raw)

    proc.wait()
    return proc.returncode, "\n".join(lines)


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
    ensure_ssh_config(cfg)
    rc, out = run_restic(["snapshots", "--json"], cfg)
    if rc not in (RC_OK, RC_PARTIAL):
        return []
    # SSH warnings may appear before the JSON — skip to the first '[' line
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("["):
            try:
                return json.loads(line) or []
            except json.JSONDecodeError:
                pass
    return []


def run_raw_data_size(cfg: Config) -> float | None:
    """Return the compressed/deduplicated repository data size reported by restic stats."""
    ensure_ssh_config(cfg)
    rc, out = run_restic(["stats", "--mode", "raw-data", "--json"], cfg)
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
    ensure_ssh_config(cfg)
    _, out = run_restic(["ls", "--json", snapshot_id], cfg)
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def retention_duration(cfg: Config) -> str:
    """Build the --keep-within duration string, e.g. '1y2m3d4h'. Returns '' if all zero."""
    parts = [
        (cfg.retention_years,  "y"),
        (cfg.retention_months, "m"),
        (cfg.retention_days,   "d"),
        (cfg.retention_hours,  "h"),
    ]
    return "".join(f"{v}{u}" for v, u in parts if v > 0)


def run_retention_forget(cfg: Config, on_output: OutputCallback = None) -> tuple[int, str]:
    """Run forget --keep-within <duration> --prune."""
    ensure_ssh_config(cfg)
    duration = retention_duration(cfg)
    if not duration:
        return RC_OK, ""
    return run_restic(["forget", "--keep-within", duration, "--prune"], cfg, on_output)


def run_forget(cfg: Config, snapshot_id: str, prune: bool = True) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    args = ["forget", snapshot_id]
    if prune:
        args.append("--prune")
    return run_restic(args, cfg)


def run_prune(cfg: Config, on_output: OutputCallback = None) -> tuple[int, str]:
    ensure_ssh_config(cfg)
    return run_restic(["prune"], cfg, on_output)


def start_mount(cfg: Config, mountpoint: str) -> "subprocess.Popen[str]":
    """Start `restic mount <mountpoint>` and return the blocking Popen."""
    ensure_ssh_config(cfg)
    return subprocess.Popen(
        _base_cmd(cfg) + ["mount", mountpoint],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
