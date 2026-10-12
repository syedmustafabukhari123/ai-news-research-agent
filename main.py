"""Manual and cron entry point for one AI news research run."""

from __future__ import annotations

import logging
import json
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from agent import ResearchError, run_research
from config import ConfigurationError, Settings, load_settings
from report import (
    ReportValidationError,
    build_evidence_audit,
    report_path,
    save_evidence_audit,
    save_structured_report,
    validate_story_items,
)
from tools import canonicalize_source_url

EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_BUSY = 3
EXIT_RESEARCH = 4
EXIT_OUTPUT = 5


def _research_failure_log_fields(exc: BaseException) -> str:
    usage = getattr(exc, "usage", {})
    provider = getattr(exc, "provider_diagnostics", {})
    safe = {
        "model_name": usage.get("model_name"),
        "model_calls": usage.get("model_calls"),
        "usage_calls": usage.get("usage_calls"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "token_usage_complete": usage.get("token_usage_complete"),
        "provider": provider,
        "output": getattr(exc, "output_diagnostics", {}),
    }
    return json.dumps(safe, ensure_ascii=True, separators=(",", ":"))


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def run_lock(lock_path: Path):
    """Acquire a nonblocking process lock on Windows and POSIX."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AlreadyRunningError("Another news agent run already holds the lock.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AlreadyRunningError("Another news agent run already holds the lock.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level))
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(settings.log_dir / "agent.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(file_handler)


def execute_run(settings: Settings, *, now: datetime | None = None) -> Path:
    """Run research, validate the output, and save a report; injectable for tests."""
    start_time = time.monotonic()
    started_at = (now or datetime.now(settings.timezone)).astimezone(settings.timezone)
    window_start, window_end = settings.research_window(started_at)
    logger = logging.getLogger(__name__)
    logger.info("run_started at=%s window_start=%s window_end=%s model=%s", started_at.isoformat(), window_start.isoformat(), window_end.isoformat(), settings.groq_model)
    try:
        result = run_research(settings, window_start, window_end)
    except Exception as exc:
        logger.error(
            "run_failed ended_at=%s status=failed stage=research model=%s model_calls=%s usage_calls=%s input_tokens=%s output_tokens=%s total_tokens=%s token_usage_complete=%s searches=%d search_failures=%d error_type=%s diagnostics=%s duration_seconds=%.2f",
            datetime.now(settings.timezone).isoformat(),
            getattr(exc, "usage", {}).get("model_name", settings.groq_model),
            getattr(exc, "usage", {}).get("model_calls"),
            getattr(exc, "usage", {}).get("usage_calls"),
            getattr(exc, "usage", {}).get("input_tokens"),
            getattr(exc, "usage", {}).get("output_tokens"),
            getattr(exc, "usage", {}).get("total_tokens"),
            getattr(exc, "usage", {}).get("token_usage_complete"),
            getattr(exc, "search_calls", 0),
            getattr(exc, "search_failures", 0),
            type(exc).__name__,
            _research_failure_log_fields(exc),
            time.monotonic() - start_time,
        )
        raise
    try:
        validation = validate_story_items(result.stories, result.evidence)
        rejection_reasons = Counter(item.reason_code for item in validation.rejected)
        claim_rejection_reasons = Counter(item["reason_code"] for item in validation.claim_rejections)
        accepted_claims = sum(len(story.claims) for story in validation.accepted)
        logger.info(
            "report_validation format=%s parse_status=%s candidate_items=%d accepted_items=%d rejected_items=%d rejection_reasons=%s claim_accepted=%d claim_rejected=%d claim_rejection_reasons=%s warnings=%s model_calls=%s input_tokens=%s output_tokens=%s total_tokens=%s token_usage_complete=%s",
            result.output_format,
            result.parse_status,
            len(result.stories),
            len(validation.accepted),
            len(validation.rejected),
            json.dumps(dict(rejection_reasons), separators=(",", ":")),
            accepted_claims,
            len(validation.claim_rejections),
            json.dumps(dict(claim_rejection_reasons), separators=(",", ":")),
            json.dumps(validation.warnings, separators=(",", ":")),
            result.model_calls,
            result.input_tokens,
            result.output_tokens,
            result.total_tokens,
            result.token_usage_complete,
        )
        source_audit = []
        for source in result.evidence:
            title = str(source.get("title", ""))[:300]
            for secret in (settings.groq_api_key, settings.tavily_api_key):
                if secret:
                    title = title.replace(secret, "[REDACTED]")
            title = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", title)
            title = re.sub(r"(?i)\b(?:tvly|gsk)[-_][A-Za-z0-9_-]{8,}\b", "[REDACTED]", title)
            source_audit.append({
                "title": title,
                "url": canonicalize_source_url(source.get("url", "")),
                "publication_date": source.get("publication_date", "Not verified"),
            })
        logger.info("source_evidence=%s", json.dumps(source_audit, ensure_ascii=True, separators=(",", ":")))
        if not validation.accepted:
            raise ReportValidationError("No story candidates passed independent evidence validation.")
        audit = None
        if settings.evidence_audit_enabled:
            anticipated_report = report_path(settings.report_dir, window_end.date().isoformat())
            audit = build_evidence_audit(
                result.stories,
                validation,
                result.evidence,
                report_file=anticipated_report.name,
                generated_at=started_at.isoformat(),
                secrets=(settings.groq_api_key, settings.tavily_api_key),
            )
        output = save_structured_report(
            validation.accepted,
            settings.report_dir,
            window_end.date().isoformat(),
            research_window=f"{window_start.isoformat()} through {window_end.isoformat()} ({settings.timezone_name})",
            generated_at=started_at.isoformat(),
        )
        audit_path = None
        if audit is not None:
            audit["report_file"] = output.name
            try:
                audit_path = save_evidence_audit(audit, output)
            except OSError as exc:
                # Audit is optional: preserve the existing report workflow, but
                # make a configured artifact write failure visible without data.
                logger.error("evidence_audit_write_failed error_type=%s", type(exc).__name__)
    except (ReportValidationError, OSError, ValueError):
        logger.exception(
            "run_failed ended_at=%s status=failed stage=report_save searches=%d search_failures=%d duration_seconds=%.2f",
            datetime.now(settings.timezone).isoformat(), result.search_calls, result.search_failures, time.monotonic() - start_time,
        )
        raise
    ended_at = datetime.now(settings.timezone)
    logger.info(
        "run_succeeded ended_at=%s model=%s model_calls=%d usage_calls=%d input_tokens=%s output_tokens=%s total_tokens=%s token_usage_complete=%s searches=%d search_failures=%d report=%s evidence_audit=%s duration_seconds=%.2f",
        ended_at.isoformat(), result.model_name, result.model_calls, result.usage_calls,
        result.input_tokens, result.output_tokens, result.total_tokens, result.token_usage_complete,
        result.search_calls, result.search_failures, output.resolve(), audit_path.resolve() if audit_path else None,
        time.monotonic() - start_time,
    )
    return output


def main() -> int:
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
        logging.error("configuration_error: %s", exc)
        return EXIT_CONFIGURATION
    configure_logging(settings)
    lock_file = settings.log_dir / "agent.lock"
    try:
        with run_lock(lock_file):
            execute_run(settings)
        return EXIT_OK
    except AlreadyRunningError as exc:
        logging.getLogger(__name__).warning("run_skipped: %s", exc)
        return EXIT_BUSY
    except (ResearchError, TimeoutError) as exc:
        logging.getLogger(__name__).error(
            "research_failed error_type=%s searches=%d model_usage=%s",
            type(exc).__name__, getattr(exc, "search_calls", 0), _research_failure_log_fields(exc),
        )
        return EXIT_RESEARCH
    except (ReportValidationError, OSError, ValueError) as exc:
        logging.getLogger(__name__).exception("run_failed stage=output error=%s", exc)
        return EXIT_OUTPUT
    except Exception:
        logging.getLogger(__name__).exception("run_failed stage=unexpected")
        return EXIT_RESEARCH


if __name__ == "__main__":
    sys.exit(main())
