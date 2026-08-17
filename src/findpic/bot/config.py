"""Bot configuration, read from the environment.

Deliberately dependency-free: a dataclass and ``os.environ``. The bot has three
external couplings — the token, the API endpoint, and the shared file volume —
and each is a single variable so a deployment mistake is obvious rather than
buried in a settings framework.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Telegram's own cap when talking to the cloud API. A local Bot API server
#: raises this to 2 GB, which is why the deployment prefers one.
CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _env_ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            ids.add(int(chunk))
    return frozenset(ids)


class ConfigError(RuntimeError):
    """The bot cannot start with the configuration it was given."""


#: Where a local run looks for its settings, in order. Under Docker the variables
#: are already in the environment (compose reads `deploy/.env` itself), so none of
#: these exist in the container and the search costs nothing.
ENV_FILE_CANDIDATES = (
    Path("deploy/.env"),
    Path(".env"),
    Path(__file__).resolve().parents[3] / "deploy" / ".env",
)


def find_env_file() -> Path | None:
    """The .env to load, or None. ``FINDPIC_ENV_FILE`` overrides the search."""
    override = os.environ.get("FINDPIC_ENV_FILE")
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    return next((path for path in ENV_FILE_CANDIDATES if path.is_file()), None)


def load_env_file(path: Path | None = None) -> Path | None:
    """Load ``KEY=value`` pairs into the environment, if a file is there.

    Real environment variables always win, so `BOT_TOKEN=… python -m findpic.bot`
    still overrides whatever the file says — which is what anyone would expect,
    and what keeps a stale file from silently shadowing a deliberate override.

    Returns the file that was loaded, or None.
    """
    path = path or find_env_file()
    if path is None:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        # Strip one layer of matching quotes, the way a shell would.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


@dataclass(frozen=True)
class Config:
    """Everything the bot needs to run."""

    token: str
    #: Base URL of the Bot API. Empty means Telegram's cloud API.
    api_base: str = ""
    #: True when the API server runs in --local mode, where getFile returns an
    #: absolute path on the server's filesystem rather than a download URL.
    api_is_local: bool = False
    #: Where that server writes its files, as this container sees it. Must be the
    #: same bytes the server wrote, so it is a shared mount rather than a copy.
    api_files_root: Path = Path("/var/lib/telegram-bot-api")

    language: str = "en"
    #: Empty means the bot is open to everyone.
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)

    max_file_bytes: int = 20 * 1024 * 1024
    #: Minimum seconds between two analyses from one user.
    throttle_seconds: float = 3.0
    daily_quota: int = 50
    #: Analyses are remembered only long enough to serve the buttons under them.
    analysis_ttl_seconds: int = 24 * 3600

    #: Record who used the bot: account, what they asked for, when, and what
    #: camera their photos named. Never message text, never a location. Set
    #: ANALYTICS=0 and the bot writes nothing but the language preference.
    analytics: bool = True
    #: How long that record is kept. 0 keeps it forever, which is a decision
    #: rather than a default.
    analytics_retention_days: int = 90

    database_path: Path = Path("/data/findpic-bot.sqlite3")
    work_dir: Path = Path("/tmp/findpic")
    #: Delete the source file from the shared volume once analysed. Only ever
    #: applies to paths inside this bot's own token directory.
    delete_source_files: bool = True

    log_level: str = "INFO"
    drop_pending_updates: bool = True

    @property
    def is_public(self) -> bool:
        return not self.allowed_user_ids

    @property
    def token_directory(self) -> Path:
        """The subdirectory the local API server uses for this bot's files."""
        return self.api_files_root / self.token

    @classmethod
    def from_env(cls, use_env_file: bool = True) -> Config:
        loaded = load_env_file() if use_env_file else None

        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            where = f"{loaded} has no BOT_TOKEN" if loaded else "no .env file was found"
            raise ConfigError(
                "BOT_TOKEN is not set.\n"
                f"  Looked in the environment and for a .env file: {where}.\n"
                "  Fix it with either:\n"
                "    cp deploy/.env.example deploy/.env   # then put the token in deploy/.env\n"
                "    BOT_TOKEN='123:ABC' python -m findpic.bot --setup\n"
                "  Note deploy/.env.example is a tracked template — never put a real "
                "token in it."
            )
        if ":" not in token:
            raise ConfigError("BOT_TOKEN does not look like a Telegram bot token.")

        api_base = os.environ.get("BOT_API_BASE", "").strip().rstrip("/")
        api_is_local = _env_bool("BOT_API_IS_LOCAL", bool(api_base))

        # A local server means no 20 MB ceiling, so the default rises with it.
        default_max = 256 * 1024 * 1024 if api_is_local else CLOUD_DOWNLOAD_LIMIT
        max_bytes = _env_int("MAX_FILE_MB", 0) * 1024 * 1024 or default_max
        if not api_is_local:
            max_bytes = min(max_bytes, CLOUD_DOWNLOAD_LIMIT)

        return cls(
            token=token,
            api_base=api_base,
            api_is_local=api_is_local,
            api_files_root=Path(os.environ.get("BOT_API_FILES_ROOT", "/var/lib/telegram-bot-api")),
            language=os.environ.get("BOT_DEFAULT_LANGUAGE", "en").strip() or "en",
            allowed_user_ids=_env_ids("ALLOWED_USER_IDS"),
            admin_user_ids=_env_ids("ADMIN_USER_IDS"),
            max_file_bytes=max_bytes,
            throttle_seconds=float(os.environ.get("THROTTLE_SECONDS", "3") or 3),
            daily_quota=_env_int("DAILY_QUOTA", 50),
            analytics=_env_bool("ANALYTICS", True),
            analytics_retention_days=_env_int("ANALYTICS_RETENTION_DAYS", 90),
            database_path=Path(os.environ.get("DATABASE_PATH", "/data/findpic-bot.sqlite3")),
            work_dir=Path(os.environ.get("WORK_DIR", "/tmp/findpic")),
            delete_source_files=_env_bool("DELETE_SOURCE_FILES", True),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            drop_pending_updates=_env_bool("DROP_PENDING_UPDATES", True),
        )

    def describe(self) -> str:
        """A startup line that never prints the token.

        The UID is in here because access to a self-hosted Bot API server's files
        is granted by an ACL keyed on it, and a mismatch is otherwise invisible
        until the first photo fails.
        """
        endpoint = self.api_base or "api.telegram.org"
        mode = "local" if self.api_is_local else "cloud"
        access = "public" if self.is_public else f"{len(self.allowed_user_ids)} allowed users"
        if not self.analytics:
            stats = "off"
        elif self.analytics_retention_days > 0:
            stats = f"{self.analytics_retention_days}d"
        else:
            stats = "kept forever"
        return (
            f"uid={os.getuid()} · endpoint={endpoint} ({mode}) · access={access} · "
            f"max_file={self.max_file_bytes // 1024 // 1024}MB · "
            f"quota={self.daily_quota}/day · throttle={self.throttle_seconds}s · "
            f"analytics={stats}"
        )
