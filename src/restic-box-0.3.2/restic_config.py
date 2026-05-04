"""Config dataclass + JSON persistence for restic-box."""

import json
import secrets
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "restic-box"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_PASSWORD_FILE = CONFIG_DIR / "password"


@dataclass
class Directory:
    path: str
    excludes: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class Config:
    backend: str = "sftp"  # sftp | local
    ssh_host: str = ""
    ssh_port: int = 23
    ssh_user: str = ""
    ssh_key: str = str(Path.home() / ".ssh" / "id_ed25519")
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
    last_backup_ts: float = 0.0   # unix timestamp of last backup attempt, 0 = never
    last_backup_ok: bool = True
    last_check_ts: float = 0.0    # unix timestamp of last check attempt, 0 = never
    total_backup_size: float = 0.0  # total uncompressed bytes across enabled backup sources
    retention_enabled: bool = False
    retention_years: int = 0
    retention_months: int = 0
    retention_days: int = 0
    retention_hours: int = 0
    directories: list[Directory] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        raw_dirs = d.get("directories", [])
        known = set(cls.__dataclass_fields__) - {"directories"}
        obj = cls(**{k: v for k, v in d.items() if k in known})
        obj.directories = [Directory(**rd) for rd in raw_dirs]
        return obj


def load_config() -> Config:
    try:
        with open(CONFIG_FILE) as f:
            return Config.from_dict(json.load(f))
    except Exception:
        return Config()


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg.to_dict(), f, indent=2)


def password_file_path(cfg: Config) -> Path:
    return Path(cfg.password_file).expanduser()


def password_file_exists(cfg: Config) -> bool:
    return password_file_path(cfg).exists()


def generate_password(cfg: Config) -> str:
    """Generate a 48-char random password and write it to the password file (mode 0o600)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    pw = "".join(secrets.choice(alphabet) for _ in range(48))
    p = password_file_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pw + "\n")
    p.chmod(0o600)
    return pw
