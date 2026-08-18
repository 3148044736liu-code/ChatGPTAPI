"""
Centralized configuration — loads from .env with sensible defaults.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

_CODE_ROOT = Path(__file__).resolve().parent.parent
_CWD = Path.cwd()

# Prefer the invocation directory as project root when running from
# a checkout (e.g. `nix run .#proxy` from repo root). Fall back to the
# code location (used for packaged/store execution).
if (_CWD / "src").exists() and (_CWD / "scripts").exists():
    _PROJECT_ROOT = _CWD
else:
    _PROJECT_ROOT = _CODE_ROOT

# Load .env from current working directory first, then from the
# resolved project root.
load_dotenv(_CWD / ".env")
load_dotenv(_PROJECT_ROOT / ".env")


class Config:
    """All project settings in one place."""

    # Paths
    PROJECT_ROOT: Path = _PROJECT_ROOT
    BROWSER_DATA_DIR: Path = _PROJECT_ROOT / os.getenv("BROWSER_DATA_DIR", "browser_data")
    LOG_DIR: Path = _PROJECT_ROOT / os.getenv("LOG_DIR", "logs")
    IMAGES_DIR: Path = _PROJECT_ROOT / os.getenv("IMAGES_DIR", "downloads/images")
    DATA_DIR: Path = _PROJECT_ROOT / os.getenv("DATA_DIR", "data")
    FILES_DIR: Path = DATA_DIR / "files"
    TEMP_DIR: Path = DATA_DIR / "tmp"
    DATABASE_PATH: Path = DATA_DIR / "catgpt_gateway.db"

    # Browser
    HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"
    # Windows service runner keeps the real browser visible and topmost when
    # enabled. The runner writes a heartbeat consumed by /dashboard.
    BROWSER_KEEP_FOREGROUND: bool = os.getenv(
        "BROWSER_KEEP_FOREGROUND", "false"
    ).lower() == "true"
    BROWSER_FOREGROUND_STATE_FILE: Path = _PROJECT_ROOT / os.getenv(
        "BROWSER_FOREGROUND_STATE_FILE",
        "data/browser_foreground_state.json",
    )
    SLOW_MO: int = int(os.getenv("SLOW_MO", "25"))
    CHATGPT_URL: str = os.getenv("CHATGPT_URL", "https://chatgpt.com")
    CLAUDE_URL: str = os.getenv("CLAUDE_URL", "https://claude.ai")

    # Provider selection: "chatgpt" or "claude"
    PROVIDER: str = os.getenv("PROVIDER", "chatgpt").lower()

    @classmethod
    def provider_url(cls) -> str:
        """Return the target URL for the active provider."""
        if cls.PROVIDER == "claude":
            return cls.CLAUDE_URL
        return cls.CHATGPT_URL

    # Timeouts (ms)
    RESPONSE_TIMEOUT: int = int(os.getenv("RESPONSE_TIMEOUT", "120000"))
    SELECTOR_TIMEOUT: int = int(os.getenv("SELECTOR_TIMEOUT", "10000"))

    # Human simulation (ms)
    TYPING_SPEED_MIN: int = int(os.getenv("TYPING_SPEED_MIN", "50"))
    TYPING_SPEED_MAX: int = int(os.getenv("TYPING_SPEED_MAX", "150"))
    THINKING_PAUSE_MIN: int = int(os.getenv("THINKING_PAUSE_MIN", "500"))
    THINKING_PAUSE_MAX: int = int(os.getenv("THINKING_PAUSE_MAX", "1500"))
    # Completion poll interval — how often to check if response is ready (ms)
    POLL_INTERVAL_MS: int = int(os.getenv("POLL_INTERVAL_MS", "300"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG")
    VERBOSE: bool = os.getenv("VERBOSE", "true").lower() == "true"
    LOG_RETENTION_DAYS: int = max(1, int(os.getenv("LOG_RETENTION_DAYS", "30")))

    # API (Phase 3)
    API_HOST: str = os.getenv("API_HOST", "192.168.8.222")
    API_PORT: int = int(os.getenv("API_PORT", "5061"))
    RATE_LIMIT_SECONDS: int = int(os.getenv("RATE_LIMIT_SECONDS", "5"))
    API_TOKEN: str = os.getenv("API_TOKEN", "")  # Legacy project token; empty never disables auth.
    API_USER_TOKENS: str = os.getenv("API_USER_TOKENS", "")
    ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")
    BOOTSTRAP_ADMIN_TOKEN: str = os.getenv("BOOTSTRAP_ADMIN_TOKEN", "")
    DASHBOARD_REQUIRE_ADMIN_TOKEN: bool = os.getenv(
        "DASHBOARD_REQUIRE_ADMIN_TOKEN", "true"
    ).lower() == "true"
    DOWNLOAD_SECRET: str = os.getenv("DOWNLOAD_SECRET", "") or API_TOKEN or secrets.token_hex(32)
    MAX_CONCURRENT_SESSIONS: int = max(1, int(os.getenv("MAX_CONCURRENT_SESSIONS", "1")))
    # Minimum idle time between browser-backed requests.  The runtime
    # middleware applies this globally so managed and compatibility routes
    # cannot operate the ChatGPT page at the same time.
    SESSION_REQUEST_GAP_MS: int = max(0, int(os.getenv("SESSION_REQUEST_GAP_MS", "30000")))
    BROWSER_TASK_GAP_MIN_SECONDS: float = max(
        0.0, float(os.getenv("BROWSER_TASK_GAP_MIN_SECONDS", "30"))
    )
    BROWSER_TASK_GAP_MAX_SECONDS: float = max(
        BROWSER_TASK_GAP_MIN_SECONDS,
        float(os.getenv("BROWSER_TASK_GAP_MAX_SECONDS", "50")),
    )
    MAX_BROWSER_QUEUE_DEPTH: int = max(1, int(os.getenv("MAX_BROWSER_QUEUE_DEPTH", "100")))
    MAX_FILE_SIZE_MB: int = max(1, int(os.getenv("MAX_FILE_SIZE_MB", "25")))
    REMOTE_FILE_CONNECT_TIMEOUT_SECONDS: float = max(
        1.0, float(os.getenv("REMOTE_FILE_CONNECT_TIMEOUT_SECONDS", "10"))
    )
    REMOTE_FILE_READ_TIMEOUT_SECONDS: float = max(
        1.0, float(os.getenv("REMOTE_FILE_READ_TIMEOUT_SECONDS", "30"))
    )
    FILE_TTL_HOURS: int = max(1, int(os.getenv("FILE_TTL_HOURS", "72")))
    # Per-owner disk quota for stored files (uploads + generated files).
    USER_FILE_QUOTA_MB: int = max(1, int(os.getenv("USER_FILE_QUOTA_MB", "500")))
    # Max number of live (non-expired) files allowed inside one session.
    MAX_FILES_PER_SESSION: int = max(1, int(os.getenv("MAX_FILES_PER_SESSION", "20")))
    # Interval of the background expired-file cleanup task.
    FILE_CLEANUP_INTERVAL_MINUTES: int = max(1, int(os.getenv("FILE_CLEANUP_INTERVAL_MINUTES", "10")))
    # When true, /v1/chat/completions without an explicit session_id is also
    # routed through the managed session worker pool (ephemeral sessions).
    OPENAI_USE_SESSION_POOL: bool = os.getenv("OPENAI_USE_SESSION_POOL", "false").lower() == "true"
    # Operations dashboard telemetry. Data is kept in memory and contains no
    # request/response bodies or credentials.
    DASHBOARD_ONLINE_WINDOW_SECONDS: int = max(
        30, int(os.getenv("DASHBOARD_ONLINE_WINDOW_SECONDS", "300"))
    )
    DASHBOARD_MAX_RECENT_REQUESTS: int = max(
        20, int(os.getenv("DASHBOARD_MAX_RECENT_REQUESTS", "200"))
    )
    CORS_ALLOWED_ORIGINS: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    )

    @classmethod
    def token_owners(cls) -> dict[str, str]:
        """Return API token -> owner mappings, preserving the legacy token."""
        mappings: dict[str, str] = {}
        if cls.API_TOKEN:
            mappings[cls.API_TOKEN] = "default"
        for entry in cls.API_USER_TOKENS.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            owner, token = entry.split(":", 1)
            owner = owner.strip()
            token = token.strip()
            if owner and token:
                mappings[token] = owner
        return mappings

    @classmethod
    def owner_for_token(cls, token: str) -> str | None:
        return cls.token_owners().get(token)

    # VNC
    VNC_PASSWORD: str = os.getenv("VNC_PASSWORD", "catgpt")

    # Viewport base (will be jittered ±20px)
    VIEWPORT_WIDTH: int = 1280
    VIEWPORT_HEIGHT: int = 720

    @classmethod
    def ensure_dirs(cls) -> None:
        """Create required directories if they don't exist."""
        cls.BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.FILES_DIR.mkdir(parents=True, exist_ok=True)
        cls.TEMP_DIR.mkdir(parents=True, exist_ok=True)
