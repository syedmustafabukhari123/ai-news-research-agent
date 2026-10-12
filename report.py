"""Report validation, Markdown formatting, and collision-safe persistence."""

from __future__ import annotations

import os
import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field

from tools import canonicalize_source_url


class ReportValidationError(ValueError):
    """Raised when agent output cannot be treated as a report."""


class FactualClaim(BaseModel):
    """Atomic factual statement tied to an exact fragment of retrieved evidence."""

    model_config = ConfigDict(extra="forbid")

    claim_text: str = Field(max_length=500, description="One concise factual claim.")
    source_url: str = Field(max_length=2048, description="Retrieved Tavily source URL supporting this claim.")
    evidence_excerpt: str = Field(max_length=300, description="Exact excerpt from that source's retained title or snippet.")


class StoryCandidate(BaseModel):
    """One structured story candidate returned by the research agent."""

    model_config = ConfigDict(extra="forbid")

    headline: str | None = Field(default=None, description="Evidence-based news headline.")
    summary: str | None = Field(default=None, description="Concise facts supported by retrieved source snippets.")
    publication_date: str | None = Field(default=None, description="Source-supplied date, or Not verified.")
    source_urls: list[str] = Field(default_factory=list, description="Original URLs returned by Tavily for this story.")
    interpretation: str | None = Field(default=None, description="Optional inference, clearly distinct from facts.")
    claims: list[FactualClaim] = Field(default_factory=list, description="Atomic facts with exact source evidence references.")


class StructuredResearchOutput(BaseModel):
    """Typed final output for LangChain's structured response format."""

    model_config = ConfigDict(extra="forbid")

    stories: list[StoryCandidate] = Field(default_factory=list, description="Unique evidence-supported news stories.")


@dataclass
class ValidatedStory:
    headline: str
    summary: str
    publication_date: str
    source_urls: list[str]
    interpretation: str | None = None
    claims: list["ValidatedClaim"] = field(default_factory=list)


@dataclass
class ValidatedClaim:
    claim_text: str
    source_url: str
    evidence_excerpt: str


@dataclass
class StoryRejection:
    candidate_index: int
    reason_code: str
    headline: str = ""


@dataclass
class StoryValidationResult:
    accepted: list[ValidatedStory] = field(default_factory=list)
    rejected: list[StoryRejection] = field(default_factory=list)
    warnings: list[dict[str, int | str]] = field(default_factory=list)
    claim_rejections: list[dict[str, int | str]] = field(default_factory=list)


def _candidate_mapping(candidate) -> Mapping:
    if isinstance(candidate, BaseModel):
        return candidate.model_dump()
    return candidate if isinstance(candidate, Mapping) else {}


def _candidate_date_matches(candidate_date: str, source_date: str) -> bool:
    candidate_signature = _date_signature(candidate_date)
    source_signature = _date_signature(source_date)
    if candidate_signature and source_signature:
        return candidate_signature == source_signature
    return candidate_date.strip().casefold() == source_date.strip().casefold()


def _normalize_evidence_whitespace(value: str) -> str:
    return " ".join(value.split())


def _story_as_mapping(story: ValidatedStory) -> dict[str, str]:
    return {"headline": story.headline, "summary": story.summary}


def validate_story_items(candidates: Sequence, evidence: list[dict[str, str]]) -> StoryValidationResult:
    """Validate candidates independently and retain every valid unique story."""
    result = StoryValidationResult()
    evidence_by_url: dict[str, dict[str, str]] = {}
    for record in evidence:
        canonical = canonicalize_source_url(str(record.get("url", "")))
        if canonical:
            evidence_by_url[canonical] = record

    accepted_urls: dict[str, ValidatedStory] = {}
    for index, raw_candidate in enumerate(candidates, start=1):
        candidate = _candidate_mapping(raw_candidate)
        headline = str(candidate.get("headline") or "").strip()
        model_summary = str(candidate.get("summary") or "").strip()
        reason = None
        if not headline:
            reason = "missing_headline"
        elif not model_summary and not candidate.get("claims"):
            reason = "missing_summary"
        if reason:
            result.rejected.append(StoryRejection(index, reason, headline[:160]))
            continue

        source_urls = candidate.get("source_urls") or []
        if not isinstance(source_urls, list) or not source_urls:
            result.rejected.append(StoryRejection(index, "missing_source_urls", headline[:160]))
            continue

        normalized_urls: list[str] = []
        supporting_records: list[dict[str, str]] = []
        for raw_url in source_urls:
            url = str(raw_url).strip()
            parsed = urlparse(url)
            canonical = canonicalize_source_url(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not canonical:
                reason = "invalid_source_url"
                break
            record = evidence_by_url.get(canonical)
            if record is None:
                reason = "source_not_retrieved"
                break
            if canonical not in normalized_urls:
                normalized_urls.append(canonical)
                supporting_records.append(record)
        if reason:
            result.rejected.append(StoryRejection(index, reason, headline[:160]))
            continue
        if not normalized_urls:
            result.rejected.append(StoryRejection(index, "missing_source_urls", headline[:160]))
            continue

        claims = candidate.get("claims") or []
        accepted_claims: list[ValidatedClaim] = []
        for claim_index, raw_claim in enumerate(claims, start=1):
            claim = _candidate_mapping(raw_claim)
            claim_text = str(claim.get("claim_text") or "").strip()
            claim_url = str(claim.get("source_url") or "").strip()
            excerpt = str(claim.get("evidence_excerpt") or "").strip()
            claim_reason = None
            if not claim_text:
                claim_reason = "claim_missing_text"
            elif not claim_url or not canonicalize_source_url(claim_url):
                claim_reason = "claim_invalid_source_url"
            else:
                claim_canonical = canonicalize_source_url(claim_url)
                claim_record = evidence_by_url.get(claim_canonical)
                if claim_record is None:
                    claim_reason = "claim_source_not_retrieved"
                elif claim_canonical not in normalized_urls:
                    claim_reason = "claim_source_not_listed"
                elif not excerpt:
                    claim_reason = "claim_missing_excerpt"
                else:
                    normalized_excerpt = _normalize_evidence_whitespace(excerpt)
                    source_fields = [
                        _normalize_evidence_whitespace(str(claim_record.get(key, "")))
                        for key in ("title", "snippet")
                    ]
                    if not normalized_excerpt or not any(normalized_excerpt in field for field in source_fields):
                        claim_reason = "claim_excerpt_not_found"
                    else:
                        try:
                            _validate_supported_claims({"headline": "", "summary": claim_text}, [claim_record])
                            claim_tokens = _story_tokens(claim_text)
                            source_tokens = _story_tokens(" ".join(source_fields))
                            if claim_tokens and len(claim_tokens & source_tokens) / len(claim_tokens) < 0.30:
                                claim_reason = "claim_insufficient_overlap"
                            else:
                                accepted_claims.append(ValidatedClaim(
                                    claim_text=claim_text,
                                    source_url=str(claim_record.get("url", "")).strip(),
                                    evidence_excerpt=normalized_excerpt,
                                ))
                        except ReportValidationError as exc:
                            message = str(exc).casefold()
                            claim_reason = "unsupported_technical_claim" if "technical claim" in message else (
                                "unsupported_numerical_claim" if "numerical claim" in message else "claim_insufficient_evidence"
                            )
            if claim_reason:
                result.claim_rejections.append({
                    "candidate_index": index,
                    "claim_index": claim_index,
                    "reason_code": claim_reason,
                })

        if not accepted_claims:
            candidate_claim_reasons = [
                str(entry["reason_code"]) for entry in result.claim_rejections
                if entry["candidate_index"] == index
            ]
            story_reason = next((reason for reason in candidate_claim_reasons if reason in {
                "unsupported_technical_claim", "unsupported_numerical_claim",
            }), "no_supported_claims")
            result.rejected.append(StoryRejection(index, story_reason, headline[:160]))
            continue

        # The report summary is derived exclusively from accepted claim text.
        summary = " ".join(
            claim.claim_text.rstrip().rstrip(".") + "." for claim in accepted_claims
        )
        accepted_records = [
            evidence_by_url[canonicalize_source_url(claim.source_url)]
            for claim in accepted_claims
        ]
        try:
            _validate_supported_claims({"headline": headline, "summary": summary}, accepted_records)
        except ReportValidationError as exc:
            message = str(exc).casefold()
            reason = "unsupported_technical_claim" if "technical claim" in message else (
                "unsupported_numerical_claim" if "numerical claim" in message else "insufficient_evidence"
            )
            result.rejected.append(StoryRejection(index, reason, headline[:160]))
            continue

        # Tavily's date is retained as provider metadata only; it is not an
        # independently verified publication timestamp.
        claim_urls = list(dict.fromkeys(canonicalize_source_url(claim.source_url) for claim in accepted_claims))
        accepted_records = [evidence_by_url[url] for url in claim_urls]
        known_dates = [str(record.get("publication_date", "")).strip() for record in accepted_records]
        known_dates = [date for date in known_dates if date]
        candidate_date = str(candidate.get("publication_date") or "").strip()
        if known_dates:
            if candidate_date and "not verified" not in candidate_date.casefold():
                matching_date = next((date for date in known_dates if _candidate_date_matches(candidate_date, date)), None)
                if matching_date is None:
                    result.rejected.append(StoryRejection(index, "publication_date_mismatch", headline[:160]))
                    continue
                publication_date = matching_date
            else:
                publication_date = known_dates[0]
        else:
            publication_date = "Not verified"
            if candidate_date and "not verified" not in candidate_date.casefold():
                result.warnings.append({"candidate_index": index, "reason_code": "publication_date_unverified"})

        story = ValidatedStory(
            headline=headline,
            summary=summary,
            publication_date=publication_date,
            source_urls=[str(record.get("url", "")).strip() for record in accepted_records],
            interpretation=(str(candidate.get("interpretation") or "").strip() or None),
            claims=accepted_claims,
        )
        item = {"headline": headline, "summary": summary}
        duplicate = next((prior for prior in result.accepted if _same_story(_story_as_mapping(prior), item)), None)
        canonical_set = set(normalized_urls)
        prior_duplicate = next((prior for url in canonical_set if (prior := accepted_urls.get(url)) is not None), None)
        if duplicate is not None:
            for record, canonical in zip(supporting_records, normalized_urls):
                if canonical not in {canonicalize_source_url(url) for url in duplicate.source_urls}:
                    duplicate.source_urls.append(str(record.get("url", "")).strip())
                    accepted_urls[canonical] = duplicate
            existing_claims = {(claim.claim_text, canonicalize_source_url(claim.source_url)) for claim in duplicate.claims}
            for claim in accepted_claims:
                identity = (claim.claim_text, canonicalize_source_url(claim.source_url))
                if identity not in existing_claims:
                    duplicate.claims.append(claim)
                    existing_claims.add(identity)
            duplicate.summary = " ".join(claim.claim_text.rstrip().rstrip(".") + "." for claim in duplicate.claims)
            result.rejected.append(StoryRejection(index, "duplicate_story_merged", headline[:160]))
            continue
        if prior_duplicate is not None:
            result.rejected.append(StoryRejection(index, "duplicate_source_url", headline[:160]))
            continue
        result.accepted.append(story)
        for canonical in normalized_urls:
            accepted_urls[canonical] = story
    return result


def render_structured_report(
    stories: Sequence[ValidatedStory], *, report_date: str, research_window: str, generated_at: str
) -> str:
    """Render validated story objects to stable Markdown without model formatting."""
    if not stories:
        raise ReportValidationError("No stories passed independent evidence validation.")
    lines = [
        "## AI Daily News Report", "", f"**Report Date:** {report_date}",
        f"**Research Window:** {research_window}", f"**Generated At:** {generated_at}", "",
        "### Executive Summary", "",
        f"Accepted {len(stories)} unique evidence-validated {('story' if len(stories) == 1 else 'stories')}.", "",
        "### Top AI News", "",
    ]
    all_urls: list[str] = []
    for story in stories:
        lines.extend([f"#### {story.headline}", "", f"Summary: {story.summary}"])
        if story.interpretation:
            lines.append(f"Why it matters (interpretation): {story.interpretation}")
        date_label = (
            "Publication date (Tavily metadata; not independently verified)"
            if story.publication_date != "Not verified" else "Publication date"
        )
        lines.extend([f"{date_label}: {story.publication_date}", "Sources:"])
        for url in story.source_urls:
            lines.append(f"- {url}")
            if url not in all_urls:
                all_urls.append(url)
        lines.append("")
    lines.extend([
        "### Emerging Trends", "",
        "No additional trend claims are made beyond the validated stories above.", "",
        "### Sources", "",
    ])
    lines.extend(f"- {url}" for url in all_urls)
    return "\n".join(lines).rstrip() + "\n"


class EvidenceAuditError(ReportValidationError):
    """Raised when accepted evidence cannot be safely represented in the audit artifact."""


def _audit_text(value, *, limit: int, secrets: Sequence[str] = ()) -> str:
    text = " ".join(str(value or "").split())[:limit]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\b(?:tvly|gsk)[-_][A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|password|secret)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]", text,
    )
    return text


def _audit_source_url(value: str, *, secrets: Sequence[str] = ()) -> str:
    parsed = urlparse(str(value or ""))
    canonical = canonicalize_source_url(str(value or ""))
    if not canonical:
        raise EvidenceAuditError("Accepted claim has a malformed source URL.")
    safe_parts = urlparse(canonical)
    sensitive_names = {
        "key", "api_key", "apikey", "token", "access_token", "auth", "authorization",
        "signature", "sig", "password", "passwd", "secret", "credential", "cookie", "session",
    }
    query = urlencode([
        (key, value) for key, value in parse_qsl(safe_parts.query, keep_blank_values=True)
        if key.casefold() not in sensitive_names
    ])
    safe_url = urlunparse((safe_parts.scheme, safe_parts.netloc, safe_parts.path, "", query, ""))
    return _audit_text(safe_url, limit=2048, secrets=secrets)


def build_evidence_audit(
    candidates: Sequence,
    validation: StoryValidationResult,
    evidence: Sequence[Mapping],
    *,
    report_file: str,
    generated_at: str,
    secrets: Sequence[str] = (),
) -> dict:
    """Create a bounded audit payload from accepted claims and their exact evidence."""
    evidence_by_url = {
        canonicalize_source_url(str(record.get("url", ""))): record
        for record in evidence
        if isinstance(record, Mapping) and canonicalize_source_url(str(record.get("url", "")))
    }
    stories = []
    for story in validation.accepted:
        accepted_claims = []
        for claim in story.claims:
            canonical = canonicalize_source_url(claim.source_url)
            record = evidence_by_url.get(canonical)
            if not record:
                raise EvidenceAuditError("Accepted claim is missing its retrieved evidence record.")
            title = _audit_text(record.get("title", ""), limit=300, secrets=secrets)
            snippet = _audit_text(record.get("snippet", ""), limit=300, secrets=secrets)
            raw_excerpt = _normalize_evidence_whitespace(claim.evidence_excerpt)
            excerpt = _audit_text(claim.evidence_excerpt, limit=300, secrets=secrets)
            if not raw_excerpt or not any(
                raw_excerpt in _normalize_evidence_whitespace(str(source_text or ""))
                for source_text in (record.get("title", ""), record.get("snippet", ""))
            ):
                raise EvidenceAuditError("Accepted claim excerpt is missing from its retained source evidence.")
            safe_url = _audit_source_url(claim.source_url, secrets=secrets)
            accepted_claims.append({
                "claim_text": _audit_text(claim.claim_text, limit=500, secrets=secrets),
                "source_url": safe_url,
                "evidence_excerpt": excerpt,
                "source_title": title,
                "source_snippet": snippet,
                "provider_publication_date": _audit_text(record.get("publication_date", ""), limit=100, secrets=secrets) or None,
                "validation_status": "accepted",
            })
        stories.append({
            "headline": _audit_text(story.headline, limit=300, secrets=secrets),
            "claims": accepted_claims,
        })

    candidates_by_index = {
        index: _candidate_mapping(candidate)
        for index, candidate in enumerate(candidates, start=1)
    }
    rejected_claims = []
    for rejection in validation.claim_rejections:
        candidate_index = rejection.get("candidate_index")
        candidate = candidates_by_index.get(candidate_index, {})
        reason = str(rejection.get("reason_code", "unknown"))
        if not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
            reason = "invalid_reason_code"
        rejected_claims.append({
            "candidate_index": candidate_index,
            "claim_index": rejection.get("claim_index"),
            "headline": _audit_text(candidate.get("headline", ""), limit=300, secrets=secrets),
            "reason_code": reason,
            "validation_status": "rejected",
        })
    return {
        "schema_version": 1,
        "report_file": _audit_text(Path(report_file).name, limit=255, secrets=secrets),
        "generated_at": _audit_text(generated_at, limit=100, secrets=secrets),
        "accepted_stories": stories,
        "rejected_claims": rejected_claims,
    }


def save_evidence_audit(audit: Mapping, report_path_value: Path) -> Path:
    """Atomically create a private local JSON audit file beside its Markdown report."""
    audit_path = report_path_value.with_name(report_path_value.stem + ".audit.json")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=audit_path.parent, delete=False) as temp:
            temp_name = temp.name
            json.dump(audit, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.link(temp_name, audit_path)
        return audit_path
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def _report_items(top_news: str) -> list[dict[str, str | list[str]]]:
    """Read item blocks in the supported heading or Markdown-table layout."""
    rows = []
    table_lines = [line for line in top_news.splitlines() if line.strip().startswith("|")]
    if len(table_lines) >= 3:
        for line in table_lines[2:]:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 6 or not cells[0].strip("* ").isdigit():
                continue
            rows.append({
                "headline": cells[1],
                "summary": cells[2],
                "publication_date": cells[5],
                "sources": re.findall(r"https?://[^\s)>|,]+", cells[-1]),
            })
        if rows:
            return rows

    blocks = re.split(r"(?m)^####\s+", top_news)[1:]
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        headline = lines[0].strip().strip("*# ")
        content = "\n".join(lines[1:])
        date_match = re.search(r"(?im)^\s*(?:[-*]\s*)?Publication date:\s*(.+?)\s*$", content)
        rows.append({
            "headline": headline,
            "summary": " ".join(re.findall(r"(?im)^\s*(?:[-*]\s*)?Summary:\s*(.+?)\s*$", content)),
            "publication_date": date_match.group(1) if date_match else "",
            "sources": re.findall(r"https?://[^\s)>|,]+", content),
        })
    return rows


def _story_tokens(value: str) -> set[str]:
    ignored = {
        "about", "after", "amid", "and", "are", "for", "from", "has", "have", "into", "its", "new", "over",
        "the", "this", "today", "with", "what", "when", "where", "which", "while", "will", "your", "their",
        "adds", "added", "announces", "announced", "company", "daily", "news", "quietly", "says", "story", "stories",
        "lists", "listed", "update", "updated", "report", "roundup", "top", "models", "model", "launch", "launched",
        "openai", "google", "anthropic", "microsoft", "meta", "amazon", "nvidia", "apple", "deepmind", "android",
    }
    return {token for token in re.findall(r"[a-z0-9]+", value.casefold()) if len(token) > 2 and token not in ignored}


def _same_story(first: dict, second: dict) -> bool:
    left = _story_tokens(str(first.get("headline", "")) + " " + str(first.get("summary", "")))
    right = _story_tokens(str(second.get("headline", "")) + " " + str(second.get("summary", "")))
    if not left or not right:
        return False
    shared = left & right
    overlap = len(shared) / min(len(left), len(right))
    if len(shared) >= 3 and overlap >= 0.68:
        return True
    event_terms = {"pricing", "api", "benchmark", "funding", "acquisition", "launch", "release", "research"}
    shared_entities = {token for token in shared if len(token) >= 6 and token not in event_terms}
    return len(shared_entities) >= 1 and bool(shared & event_terms)


def _date_signature(value: str) -> tuple[int, int, int] | None:
    iso = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", value)
    if iso:
        try:
            return tuple(map(int, iso.groups()))
        except ValueError:
            return None
    named = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})[,]?\s+(20\d{2})\b", value)
    if not named:
        named = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})[,]?\s+(20\d{2})\b", value)
        if not named:
            return None
        month_name, day_text, year_text = named.groups()
    else:
        day_text, month_name, year_text = named.groups()
    try:
        month = datetime.strptime(month_name[:3].title(), "%b").month
        return int(year_text), month, int(day_text)
    except ValueError:
        return None


def _validate_supported_claims(item: dict, source_records: list[dict]) -> None:
    evidence_text = " ".join(
        " ".join(str(record.get(key, "")) for key in ("title", "snippet", "publication_date"))
        for record in source_records
    ).casefold()
    summary = str(item.get("summary", ""))
    claims = " ".join((str(item.get("headline", "")), summary)).casefold()
    unsupported_benchmark_terms = (
        r"on[- ]device", r"inference", r"computer[- ]vision", r"multimodal", r"smartphones?",
        r"android\s*13", r"reporting\s+api", r"workloads?",
    )
    for pattern in unsupported_benchmark_terms:
        for match in re.finditer(pattern, claims):
            if not re.search(pattern, evidence_text):
                raise ReportValidationError(f"Report includes a technical claim not supported by source evidence: {match.group(0)}")
    numbers = set(re.findall(r"(?<![a-z])\$?\b\d+(?:[,.]\d+)*(?:%|\s?m\b|\s?k\b)?", claims))
    for number in numbers:
        if not re.search(r"(?<!\d)" + re.escape(number.casefold()) + r"(?!\d)", evidence_text):
            raise ReportValidationError(f"Report includes a numerical claim not supported by source evidence: {number}")
    summary_tokens = _story_tokens(summary)
    evidence_tokens = _story_tokens(evidence_text)
    if summary_tokens and len(summary_tokens & evidence_tokens) / len(summary_tokens) < 0.25:
        raise ReportValidationError("Report summary is not sufficiently supported by retrieved source evidence.")
    headline_tokens = _story_tokens(str(item.get("headline", "")))
    if headline_tokens and len(headline_tokens & evidence_tokens) / len(headline_tokens) < 0.25:
        raise ReportValidationError("Report headline is not sufficiently supported by retrieved source evidence.")


def validate_report(text: str, evidence: list[dict[str, str]] | None = None) -> str:
    cleaned = text.strip()
    if len(cleaned) < 40:
        raise ReportValidationError("Research output is empty or too short to be a report.")
    required = (
        "AI Daily News Report",
        "**Report Date:**",
        "**Research Window:**",
        "**Generated At:**",
        "### Executive Summary",
        "### Top AI News",
        "### Emerging Trends",
        "### Sources",
    )
    missing = [section for section in required if section not in cleaned]
    if missing:
        raise ReportValidationError("Research output is missing required report field(s): " + ", ".join(missing))
    no_results = "no relevant recent news found" in cleaned.casefold()
    if not no_results and not re.search(r"https?://\S+", cleaned):
        raise ReportValidationError("Research output contains no source URL.")
    for url in re.findall(r"https?://[^\s)\]>]+", cleaned):
        if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc:
            raise ReportValidationError("Research output contains an invalid source URL.")
    if not no_results:
        top_news_match = re.search(r"### Top AI News\s*(.*?)(?=### Emerging Trends)", cleaned, re.DOTALL | re.IGNORECASE)
        items = _report_items(top_news_match.group(1)) if top_news_match else []
        if not items:
            raise ReportValidationError("Report has no parseable story items.")
        evidence_by_url = {}
        if evidence is not None:
            for record in evidence:
                canonical = canonicalize_source_url(str(record.get("url", "")))
                if canonical:
                    evidence_by_url[canonical] = record
        seen_urls: set[str] = set()
        for index, item in enumerate(items):
            if evidence is not None and (not str(item.get("headline", "")).strip() or not str(item.get("summary", "")).strip()):
                raise ReportValidationError("Each report item needs a headline and an evidence-based summary.")
            item_urls = list(dict.fromkeys(canonicalize_source_url(url) for url in item["sources"]))
            item_urls = [url for url in item_urls if url]
            if not item_urls:
                raise ReportValidationError("Every report item must cite at least one valid source URL.")
            for canonical in item_urls:
                if canonical in seen_urls:
                    raise ReportValidationError("The report repeats a source URL across multiple story items.")
                seen_urls.add(canonical)
                if evidence is not None and canonical not in evidence_by_url:
                    raise ReportValidationError("Report cites a URL that was not returned by search.")
            publication_date = str(item.get("publication_date", ""))
            supporting_records = [evidence_by_url[url] for url in item_urls if url in evidence_by_url]
            known_dates = [str(record.get("publication_date", "")) for record in supporting_records if record.get("publication_date")]
            if evidence is not None and not known_dates and not re.search(r"not verified", publication_date, re.IGNORECASE):
                raise ReportValidationError("A publication date without source evidence must be marked Not verified.")
            if evidence is not None and known_dates and not publication_date:
                raise ReportValidationError("A source-supplied publication date must be retained in the report.")
            if evidence is not None and known_dates and re.search(r"not verified|unavailable|unknown", publication_date, re.IGNORECASE):
                raise ReportValidationError("A verified source publication date cannot be marked Not verified.")
            if known_dates and publication_date:
                report_date = _date_signature(publication_date)
                source_dates = {_date_signature(value) for value in known_dates}
                source_dates.discard(None)
                if report_date and source_dates and report_date not in source_dates:
                    raise ReportValidationError("Report publication date does not match retrieved source metadata.")
                if evidence is not None and not report_date and not any(value.casefold() in publication_date.casefold() for value in known_dates):
                    raise ReportValidationError("Report publication date cannot be matched to retrieved source metadata.")
            if evidence is not None:
                _validate_supported_claims(item, supporting_records)
            for prior in items[:index]:
                prior_urls = {canonicalize_source_url(url) for url in prior["sources"]}
                if set(item_urls) & prior_urls or _same_story(prior, item):
                    raise ReportValidationError("Report contains duplicate stories; combine them and retain supporting URLs.")
    return cleaned + "\n"


def format_report(text: str, evidence: list[dict[str, str]] | None = None) -> str:
    return validate_report(text, evidence=evidence)


def report_path(report_dir: Path, report_date: datetime | str) -> Path:
    date_text = report_date.strftime("%Y-%m-%d") if isinstance(report_date, datetime) else str(report_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        raise ValueError("report_date must use YYYY-MM-DD format.")
    base = report_dir / f"ai_news_{date_text}.md"
    if not base.exists():
        return base
    version = 2
    while True:
        candidate = report_dir / f"ai_news_{date_text}_{version}.md"
        if not candidate.exists():
            return candidate
        version += 1


def save_report(text: str, report_dir: Path, report_date: datetime | str, *, evidence: list[dict[str, str]] | None = None) -> Path:
    """Validate then atomically create a unique UTF-8 Markdown file."""
    content = format_report(text, evidence=evidence)
    report_dir.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=report_dir, delete=False) as temp:
            temp_name = temp.name
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())

        # A hard link publishes the fully written file atomically and fails if
        # another run has already claimed that date-based name.
        candidate = report_path(report_dir, report_date)
        while True:
            try:
                os.link(temp_name, candidate)
                return candidate
            except FileExistsError:
                stem = candidate.stem
                match = re.search(r"_(\d+)$", stem)
                version = int(match.group(1)) + 1 if match else 2
                date_text = report_date.strftime("%Y-%m-%d") if isinstance(report_date, datetime) else str(report_date)
                candidate = report_dir / f"ai_news_{date_text}_{version}.md"
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def save_structured_report(
    stories: Sequence[ValidatedStory],
    report_dir: Path,
    report_date: datetime | str,
    *,
    research_window: str,
    generated_at: str,
) -> Path:
    """Persist deterministic Markdown rendered only from validated stories."""
    date_text = report_date.strftime("%Y-%m-%d") if isinstance(report_date, datetime) else str(report_date)
    content = render_structured_report(
        stories,
        report_date=date_text,
        research_window=research_window,
        generated_at=generated_at,
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=report_dir, delete=False) as temp:
            temp_name = temp.name
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        candidate = report_path(report_dir, date_text)
        while True:
            try:
                os.link(temp_name, candidate)
                return candidate
            except FileExistsError:
                stem = candidate.stem
                match = re.search(r"_(\d+)$", stem)
                version = int(match.group(1)) + 1 if match else 2
                candidate = report_dir / f"ai_news_{date_text}_{version}.md"
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
