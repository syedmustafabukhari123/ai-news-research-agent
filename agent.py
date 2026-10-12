"""Groq-backed LangChain agent and bounded research invocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping
import re

import httpx
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.callbacks import BaseCallbackHandler
from langchain_groq import ChatGroq

from config import MAX_SEARCHES_PER_RUN, MAX_STORIES_PER_RUN, Settings
from prompts import SYSTEM_PROMPT
from report import StoryCandidate, StructuredResearchOutput
from tools import DEFAULT_MAX_RESULTS, SearchSession

SAFE_RATE_LIMIT_HEADERS = {
    "retry-after", "retry-after-ms", "x-request-id",
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
}


def _token_counts(value) -> dict[str, int] | None:
    """Extract standardized LangChain usage or the Groq token_usage shape."""
    if not isinstance(value, Mapping):
        return None
    keys = ("input_tokens", "output_tokens", "total_tokens")
    if "input_tokens" in value or "output_tokens" in value:
        counts = {
            "input_tokens": value.get("input_tokens"),
            "output_tokens": value.get("output_tokens"),
            "total_tokens": value.get("total_tokens"),
        }
    elif any(key in value for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        counts = {
            "input_tokens": value.get("prompt_tokens"),
            "output_tokens": value.get("completion_tokens"),
            "total_tokens": value.get("total_tokens"),
        }
    else:
        return None
    if not any(isinstance(counts[key], int) and counts[key] >= 0 for key in keys):
        return None
    return {key: value if isinstance(value, int) and value >= 0 else 0 for key, value in counts.items()}


def extract_usage_metadata(response) -> dict[str, int] | None:
    """Read usage from supported ChatGeneration/AIMessage metadata."""
    for group in getattr(response, "generations", []) or []:
        for generation in group[:1]:
            message = getattr(generation, "message", None)
            counts = _token_counts(getattr(message, "usage_metadata", None))
            if counts:
                return counts
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, Mapping):
                counts = _token_counts(response_metadata.get("token_usage"))
                if counts:
                    return counts
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, Mapping):
        return _token_counts(llm_output.get("token_usage"))
    return None


class RunUsageTracker(BaseCallbackHandler):
    """Per-run LangChain callback that collects counts without prompt content."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model_calls = 0
        self.usage_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.last_http_error: dict | None = None

    def on_http_response(self, response) -> None:
        """Retain only status and allowlisted headers from failed responses."""
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or status < 400:
            return
        headers_source = getattr(response, "headers", {})
        headers = {}
        if isinstance(headers_source, Mapping):
            for key, value in headers_source.items():
                normalized = str(key).lower()
                if normalized in SAFE_RATE_LIMIT_HEADERS:
                    headers[normalized] = str(value)[:120]
        category = None
        if status == 429:
            categories = []
            if headers.get("x-ratelimit-remaining-tokens", "").strip() == "0":
                categories.append("TPM")
            if headers.get("x-ratelimit-remaining-requests", "").strip() == "0":
                categories.append("RPD")
            category = ", ".join(dict.fromkeys(categories)) or "unknown throttling"
        self.last_http_error = {
            "provider_status": status,
            "rate_limit_category": category,
            "rate_limit_headers": headers,
        }

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.model_calls += 1

    def on_llm_end(self, response, **kwargs):
        counts = extract_usage_metadata(response)
        if counts is None:
            return
        self.usage_calls += 1
        self.input_tokens += counts["input_tokens"]
        self.output_tokens += counts["output_tokens"]
        self.total_tokens += counts["total_tokens"]

    def snapshot(self) -> dict:
        return {
            "model_name": self.model_name,
            "model_calls": self.model_calls,
            "usage_calls": self.usage_calls,
            "input_tokens": self.input_tokens if self.usage_calls else None,
            "output_tokens": self.output_tokens if self.usage_calls else None,
            "total_tokens": self.total_tokens if self.usage_calls else None,
            "token_usage_complete": self.usage_calls == self.model_calls and self.model_calls > 0,
        }


def provider_error_diagnostics(error: BaseException, *, prior_http_error: dict | None = None) -> dict:
    """Extract safe status, rate-limit headers, and connection classification."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__

    response = next((getattr(exc, "response", None) for exc in chain if getattr(exc, "response", None) is not None), None)
    status = next((getattr(exc, "status_code", None) for exc in chain if isinstance(getattr(exc, "status_code", None), int)), None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    headers_source = getattr(response, "headers", {}) if response is not None else {}
    headers = {}
    if isinstance(headers_source, Mapping):
        for key, value in headers_source.items():
            normalized = str(key).lower()
            if normalized in SAFE_RATE_LIMIT_HEADERS:
                headers[normalized] = str(value)[:120]

    body_text_parts = []
    for exc in chain:
        body = getattr(exc, "body", None)
        if isinstance(body, Mapping):
            body_error = body.get("error", body)
            if isinstance(body_error, Mapping):
                for key in ("code", "type", "message"):
                    value = body_error.get(key)
                    if isinstance(value, str):
                        body_text_parts.append(value)
    body_text = " ".join(body_text_parts).casefold()
    categories = []
    for label, patterns in {
        "RPM": (r"requests? per minute", r"\brpm\b"),
        "TPM": (r"tokens? per minute", r"\btpm\b"),
        "RPD": (r"requests? per day", r"\brpd\b"),
        "TPD": (r"tokens? per day", r"\btpd\b"),
    }.items():
        if any(re.search(pattern, body_text) for pattern in patterns):
            categories.append(label)
    if headers.get("x-ratelimit-remaining-tokens", "").strip() == "0":
        categories.append("TPM")
    if headers.get("x-ratelimit-remaining-requests", "").strip() == "0":
        categories.append("RPD")
    categories = list(dict.fromkeys(categories))

    connection_error = next((
        exc for exc in chain
        if isinstance(exc, (ConnectionError, TimeoutError))
        or any(marker in type(exc).__name__.casefold() for marker in ("connecterror", "connectionerror", "timeoutexception"))
    ), None)
    winerror = next((getattr(exc, "winerror", None) for exc in chain if getattr(exc, "winerror", None) is not None), None)
    errno = next((getattr(exc, "errno", None) for exc in chain if getattr(exc, "errno", None) is not None), None)
    is_reset = winerror == 10054 or errno == 10054
    diagnostics = {
        "provider_error_type": type(error).__name__,
        "provider_status": status,
        "rate_limit_category": (", ".join(categories) if len(categories) > 1 else categories[0]) if categories else ("unknown throttling" if status == 429 else None),
        "rate_limit_headers": headers,
        "connection_error_type": type(connection_error).__name__ if connection_error else None,
        "connection_category": "connection_reset" if is_reset else ("connection_error" if connection_error else None),
        "connection_errno": winerror if winerror is not None else errno,
    }
    if prior_http_error and status is None:
        diagnostics["prior_http_error"] = prior_http_error
    return diagnostics


class ResearchError(RuntimeError):
    """Raised when the agent cannot complete a research run."""

    def __init__(self, message: str, *, search_calls: int = 0, search_failures: int = 0, usage: dict | None = None, provider_diagnostics: dict | None = None, output_diagnostics: dict | None = None):
        super().__init__(message)
        self.search_calls = search_calls
        self.search_failures = search_failures
        self.usage = usage or {}
        self.provider_diagnostics = provider_diagnostics or {}
        self.output_diagnostics = output_diagnostics or {}


@dataclass
class ResearchResult:
    search_calls: int = 0
    search_failures: int = 0
    stories: list[StoryCandidate] = field(default_factory=list)
    model_name: str = ""
    model_calls: int = 0
    usage_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_usage_complete: bool = False
    evidence: list[dict[str, str]] = field(default_factory=list)
    output_format: str = "langchain_tool_strategy"
    parse_status: str = "parsed"


def build_research_task(start: datetime, end: datetime, timezone_name: str) -> str:
    return (
        "Research significant AI news published in this interval: "
        f"{start.isoformat()} through {end.isoformat()} ({timezone_name}). "
        "Search for current evidence first, then write the requested report. "
        "The interval is rolling and must be respected; state when a source's date cannot be verified. "
        f"Report date: {end.date().isoformat()}. Generated at: {end.isoformat()}."
    )


def run_research(settings: Settings, start: datetime, end: datetime) -> ResearchResult:
    """Run the actual LangChain model/tool loop with bounded search and steps."""
    session = SearchSession(
        tavily_api_key=settings.tavily_api_key,
        max_searches=min(settings.max_searches, MAX_SEARCHES_PER_RUN),
        max_results=min(settings.max_results, DEFAULT_MAX_RESULTS),
        timeout_seconds=min(settings.timeout_seconds, 60),
    )
    usage = RunUsageTracker(settings.groq_model)
    task = build_research_task(start, end, settings.timezone_name)
    # Allow each capped search/tool turn to return to the model, then finalize.
    # The extra step covers one tool call rejected by the hard search cap.
    step_limit = min(settings.max_searches, MAX_SEARCHES_PER_RUN) * 2 + 3
    try:
        with httpx.Client(event_hooks={"response": [usage.on_http_response]}) as http_client:
            model = ChatGroq(
                model=settings.groq_model,
                api_key=settings.groq_api_key,
                temperature=0,
                timeout=settings.timeout_seconds,
                # Keep the SDK's single bounded retry; it honors provider Retry-After.
                max_retries=1,
                http_client=http_client,
            )
            agent = create_agent(
                model=model,
                tools=[session.build_tool()],
                system_prompt=SYSTEM_PROMPT,
                response_format=ToolStrategy(StructuredResearchOutput, handle_errors=False),
                name="ai_news_research_agent",
            )
            result = agent.invoke(
                {"messages": [{"role": "user", "content": task}]},
                {"recursion_limit": step_limit, "callbacks": [usage]},
            )
    except Exception as exc:
        exc_name = type(exc).__name__.casefold()
        parse_status = "parse_failed" if "structuredoutput" in exc_name or "validationerror" in exc_name else "agent_execution_failed"
        raise ResearchError(
            f"Research agent failed ({type(exc).__name__}).",
            search_calls=session.calls,
            search_failures=session.failures,
            usage=usage.snapshot(),
            provider_diagnostics=provider_error_diagnostics(exc, prior_http_error=usage.last_http_error),
            output_diagnostics={
                "format": "langchain_tool_strategy", "parse_status": parse_status,
                "candidate_items": None, "accepted_items": 0, "rejected_items": None, "rejection_reason_codes": [],
            },
        ) from exc

    structured = result.get("structured_response") if isinstance(result, dict) else None
    try:
        report = structured if isinstance(structured, StructuredResearchOutput) else StructuredResearchOutput.model_validate(structured)
    except Exception as exc:
        raise ResearchError(
            "Research agent returned malformed structured output.",
            search_calls=session.calls,
            search_failures=session.failures,
            usage=usage.snapshot(),
            provider_diagnostics={"last_http_error": usage.last_http_error} if usage.last_http_error else {},
            output_diagnostics={
                "format": "langchain_tool_strategy", "parse_status": "parse_failed",
                "candidate_items": None, "accepted_items": 0, "rejected_items": None, "rejection_reason_codes": [],
            },
        ) from exc
    usage_data = usage.snapshot()
    return ResearchResult(
        search_calls=session.calls,
        search_failures=session.failures,
        stories=list(report.stories[:MAX_STORIES_PER_RUN]),
        model_name=settings.groq_model,
        model_calls=usage_data["model_calls"],
        usage_calls=usage_data["usage_calls"],
        input_tokens=usage_data["input_tokens"],
        output_tokens=usage_data["output_tokens"],
        total_tokens=usage_data["total_tokens"],
        token_usage_complete=usage_data["token_usage_complete"],
        evidence=[dict(item) for item in session.evidence],
        output_format="langchain_tool_strategy",
        parse_status="parsed",
    )
