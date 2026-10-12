# Requirements and scope

## Goal

Produce a useful daily AI-news research report through an autonomous tool-using agent. A scheduled invocation and a manual invocation use the same `main.run()` workflow.

## Required behavior

1. Load `.env` and validate Groq/Tavily keys and bounded configuration.
2. Compute a timezone-aware report date and previous-24-hour research window.
3. Ask the Groq-backed LangChain agent to choose and refine news search queries.
4. Provide Tavily news search only; enforce a hard maximum of four searches per run.
5. Reject empty or malformed agent output; clearly mark no-results reports.
6. Save UTF-8 Markdown without overwriting an existing dated report.
7. Log run start/end, status, search count, failure details, destination, and duration without secrets.
8. Return nonzero exit status on configuration, API, timeout, or output failure.
9. Offer a Linux cron wrapper and example without installing or activating it.
10. Provide mock-based tests and instructions for a future explicitly authorized live check.

## Report fields

- Report date, timezone-aware research-window boundaries, and generation timestamp.
- Executive summary.
- Zero to five news items with headline, factual summary, significance, company/area, publication date or an explicit unverified label, and source URL.
- Emerging trends.
- Sources actually cited by the researched result.
- Explicit no-results label when no credible recent stories are found.

## Out of scope

Agents 2 and 3, email, chat, frontend, HTTP server, database, vector store, RAG, embeddings, authentication, cloud, Docker, Redis, Celery, alternate search APIs, and scheduler activation.

## Assumptions

- One Markdown report is sufficient; TXT output is omitted to keep the output contract simple.
- The date window is rolling 24 hours ending at invocation time. Search snippets may not expose reliable publication dates; the report must say so instead of guessing.
- Tavily's results are untrusted data. Tool output is passed to the model as evidence, never as instructions.
- If a tool/provider error prevents completion, no success report is saved.
