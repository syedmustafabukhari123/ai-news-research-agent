# Testing and acceptance

## Automated tests

Run from the repository root with `python -m unittest discover -s tests -v`. Tests mock `ChatGroq`, `TavilySearch`, and agent execution; they do not require API credentials or make network requests.

Coverage includes missing/invalid configuration, report validation and formatting, no-results reporting, dated filename collision behavior, atomic local saving, search-call ceiling, compact Tavily normalization, snippet/result/response-size limits, absent publication dates, token usage extraction, HTTP 429 classification, and connection-reset diagnostics. All provider calls are mocked.

## Acceptance states

| State | Meaning |
|---|---|
| Implemented | Source/configuration/docs are present |
| Mock-tested | Deterministic paths pass without external services |
| Live-tested | An authorized manual run completed against Groq and Tavily |
| Scheduled and operational | User-installed cron executed successfully in the intended timezone |

The project must not describe itself as live-tested or operational until those latter steps are performed and observed. Cron activation is intentionally left to the user.

## Limitations

- Search snippets may not contain enough detail or reliable publication dates to support five items. The agent should include fewer items and mark unknown dates. Token totals are partial when a failed request or a provider response omits usage metadata.
- The model can still make factual errors; cited source URLs allow human review. The application validates structure and source presence, not the truth of each claim.
- Provider availability, model permissions, limits, and pricing may change; check current official dashboards before a live run.
