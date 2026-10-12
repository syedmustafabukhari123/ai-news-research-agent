"""Environment-backed configuration for the AI news research agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
MAX_SEARCHES_PER_RUN = 2
MAX_STORIES_PER_RUN = 3


class ConfigurationError(ValueError):
    """Raised when required settings are missing or invalid."""


def _positive_int(name: str, default: str, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false).")


@dataclass(frozen=True)
class Settings:
    groq_api_key: str
    tavily_api_key: str
    groq_model: str
    timezone_name: str
    report_dir: Path
    log_dir: Path
    max_searches: int
    max_results: int
    timeout_seconds: int
    research_window_hours: int
    log_level: str
    evidence_audit_enabled: bool = False

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def research_window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        end = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        return end - timedelta(hours=self.research_window_hours), end


def load_settings(*, validate_keys: bool = True) -> Settings:
    """Load the project .env and return validated settings."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
    missing = [name for name, value in (("GROQ_API_KEY", groq_key), ("TAVILY_API_KEY", tavily_key)) if not value]
    if validate_keys and missing:
        raise ConfigurationError("Missing required API key(s): " + ", ".join(missing) + ". Set them in .env or the process environment.")

    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Karachi").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(f"APP_TIMEZONE is not a valid IANA timezone: {timezone_name!r}.") from exc

    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    if not model:
        raise ConfigurationError("GROQ_MODEL cannot be empty.")
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")

    return Settings(
        groq_api_key=groq_key,
        tavily_api_key=tavily_key,
        groq_model=model,
        timezone_name=timezone_name,
        report_dir=Path(os.getenv("REPORT_DIR", str(PROJECT_ROOT / "reports"))).expanduser(),
        log_dir=Path(os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs"))).expanduser(),
        max_searches=_positive_int("MAX_SEARCHES", str(MAX_SEARCHES_PER_RUN), 1, MAX_SEARCHES_PER_RUN),
        max_results=_positive_int("TAVILY_MAX_RESULTS", "3", 1, 5),
        timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", "120", 10, 300),
        research_window_hours=_positive_int("RESEARCH_WINDOW_HOURS", "24", 1, 72),
        log_level=log_level,
        evidence_audit_enabled=_boolean("EVIDENCE_AUDIT_ENABLED", False),
    )
