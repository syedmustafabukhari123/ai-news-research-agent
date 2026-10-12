"""Tavily news search setup with an enforced per-run call ceiling."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from langchain.tools import tool
from langchain_tavily import TavilySearch

DEFAULT_MAX_RESULTS = 3
MAX_SNIPPET_CHARS = 300
DEFAULT_MAX_RESPONSE_BYTES = 6000
MAX_TITLE_CHARS = 300
MAX_PUBLICATION_DATE_CHARS = 100
MAX_URL_CHARS = 2048
logger = logging.getLogger(__name__)


def canonicalize_source_url(url: str) -> str:
    """Normalize a source URL for duplicate detection, keeping meaningful query parameters."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = host if port is None or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid"}
    ])
    return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))


def _safe_error_diagnostics(error, query: str, api_key: str) -> dict[str, str | int | None]:
    """Extract small diagnostics while redacting credentials and request data."""
    exception = error if isinstance(error, BaseException) else None
    response = getattr(exception, "response", None) if exception else None
    status = getattr(response, "status_code", None) if response is not None else None
    status = status or getattr(exception, "status_code", None) if exception else status
    body = getattr(exception, "body", None) if exception else None

    code = None
    if isinstance(error, dict):
        status = status or error.get("status_code") or error.get("status")
        code = error.get("code") or error.get("error_code")
        body = error
    if isinstance(body, dict):
        detail = body.get("detail", body.get("error", body))
        if isinstance(detail, dict):
            code = code or detail.get("code") or detail.get("error_code")
    if code is None and exception is not None:
        code = getattr(exception, "code", None) or getattr(exception, "error_code", None)

    if isinstance(error, dict):
        detail_value = error.get("message") or error.get("detail") or error.get("error") or error
    elif exception is not None:
        detail_value = str(exception)
    else:
        detail_value = error
    detail = str(detail_value)
    status_match = re.search(r"\b(?:HTTP(?:Error)?\s*|Error\s+)([1-5]\d{2})\b", detail, re.IGNORECASE)
    if not status and status_match:
        status = int(status_match.group(1))
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    if query:
        detail = re.sub(re.escape(query), "[query redacted]", detail, flags=re.IGNORECASE)
    detail = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", detail)
    detail = re.sub(r"(?i)\b(?:tvly|gsk)[-_][A-Za-z0-9_-]{8,}\b", "[REDACTED]", detail)
    detail = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie|api[_-]?key|tavily_api_key)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        detail,
    )
    detail = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", detail)
    # Provider errors can echo URLs; omit query strings and fragments.
    detail = re.sub(r"(https?://[^\s?#]+)[?#][^\s]+", r"\1?[REDACTED]", detail)
    detail = " ".join(detail.split())[:300]
    status = status if isinstance(status, int) else None
    code = str(code)[:80] if code is not None else None
    return {
        "exception_type": type(exception).__name__ if exception else None,
        "http_status": status,
        "provider_code": code,
        "detail": detail,
    }


def _text(value, max_chars: int | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value:
        return None
    if max_chars is not None and len(value) > max_chars:
        return value[: max_chars - 3].rstrip() + "..."
    return value


def normalize_search_results(result, *, max_results: int = DEFAULT_MAX_RESULTS, max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> str:
    """Return compact, bounded JSON containing only useful source fields."""
    max_response_bytes = max(2, int(max_response_bytes))
    raw_results = result.get("results") if isinstance(result, dict) else None
    candidates = raw_results if isinstance(raw_results, list) else []
    items: list[dict[str, str]] = []
    truncated = False

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = _text(candidate.get("url"))
        if url and len(url) > MAX_URL_CHARS:
            truncated = True
            continue
        parsed = urlparse(url or "")
        if not url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        title = _text(candidate.get("title"), MAX_TITLE_CHARS)
        publication_date = _text(candidate.get("published_date") or candidate.get("publishedDate"), MAX_PUBLICATION_DATE_CHARS)
        snippet = _text(candidate.get("content") or candidate.get("snippet"), MAX_SNIPPET_CHARS)
        item = {"url": url}
        if title:
            item["title"] = title
        if publication_date:
            item["publication_date"] = publication_date
        if snippet:
            item["snippet"] = snippet
        items.append(item)
        if len(items) >= max(1, min(max_results, DEFAULT_MAX_RESULTS)):
            truncated = len(candidates) > len(items)
            break

    payload = {"results": items, "truncated": truncated}
    encode = lambda: json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encode().encode("utf-8")) > max_response_bytes:
        snippets = [(len(item.get("snippet", "")), index) for index, item in enumerate(items) if item.get("snippet")]
        if snippets:
            _, index = max(snippets)
            current = items[index]["snippet"]
            items[index]["snippet"] = current[: max(0, len(current) - 50)].rstrip()
            if not items[index]["snippet"]:
                del items[index]["snippet"]
            payload["truncated"] = True
            continue
        if items:
            items.pop()
            payload["truncated"] = True
            continue
        # The configured minimum is validated above, but this keeps behavior
        # bounded even if a caller supplies an unrealistically tiny limit.
        minimal = '{"results":[],"truncated":true}'
        return minimal if len(minimal.encode("utf-8")) <= max_response_bytes else "[]"
    return encode()


@dataclass
class SearchSession:
    """One run's bounded Tavily search tool and observable call count."""

    tavily_api_key: str
    max_searches: int = 4
    max_results: int = DEFAULT_MAX_RESULTS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    timeout_seconds: int = 30
    calls: int = 0
    failures: int = 0
    queries: set[str] = field(default_factory=set)
    evidence: list[dict[str, str]] = field(default_factory=list)
    _seen_source_urls: set[str] = field(default_factory=set)

    def build_tool(self, search_client=None):
        client = search_client or TavilySearch(
            tavily_api_key=self.tavily_api_key,
            max_results=min(self.max_results, DEFAULT_MAX_RESULTS),
            topic="news",
            search_depth="basic",
            include_answer=False,
            include_raw_content=False,
            include_images=False,
        )

        @tool("search_ai_news")
        def search_ai_news(query: str) -> str:
            """Search recent AI news. Use specific query terms and a date/window hint."""
            normalized = " ".join(query.casefold().split())
            if not normalized:
                return "Search skipped: query was empty."
            if self.calls >= self.max_searches:
                return f"Search limit reached ({self.max_searches}); use the evidence already collected."
            if normalized in self.queries:
                return "This exact query was already searched; refine it or use the existing results."
            self.calls += 1
            self.queries.add(normalized)
            try:
                result = client.invoke({"query": query})
            except Exception as exc:
                self.failures += 1
                diagnostics = _safe_error_diagnostics(exc, query, self.tavily_api_key)
                logger.warning(
                    "tavily_search_failed call=%d diagnostics=%s",
                    self.calls,
                    json.dumps(diagnostics, ensure_ascii=True, separators=(",", ":")),
                )
                return json.dumps({"error": "Tavily search failed"}, separators=(",", ":"))
            if isinstance(result, dict) and "error" in result:
                self.failures += 1
                diagnostics = _safe_error_diagnostics(result["error"], query, self.tavily_api_key)
                logger.warning(
                    "tavily_search_failed call=%d diagnostics=%s",
                    self.calls,
                    json.dumps(diagnostics, ensure_ascii=True, separators=(",", ":")),
                )
                return json.dumps({"error": "Tavily search failed"}, separators=(",", ":"))
            normalized_json = normalize_search_results(
                result,
                max_results=min(self.max_results, DEFAULT_MAX_RESULTS),
                max_response_bytes=self.max_response_bytes,
            )
            normalized = json.loads(normalized_json)
            new_results = []
            for item in normalized.get("results", []):
                canonical_url = canonicalize_source_url(item.get("url", ""))
                if not canonical_url:
                    continue
                if canonical_url in self._seen_source_urls:
                    continue
                self._seen_source_urls.add(canonical_url)
                new_results.append(item)
                self.evidence.append(dict(item))
            normalized["results"] = new_results
            return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

        return search_ai_news
