# AI News Research Agent — Master Plan

This repository implements **Agent 1 only**: a local, scheduled or manually run AI news research agent. It uses LangChain's current `create_agent` loop, Groq for inference, and Tavily for news search. It does not send email, expose a web UI/API, store a database, deploy to cloud, or activate a scheduler.

## Supporting documents

- [Requirements and scope](01_REQUIREMENTS.md)
- [Architecture and data flow](02_ARCHITECTURE.md)
- [Sequential implementation phases](03_IMPLEMENTATION_PHASES.md)
- [Configuration and cron setup](04_CONFIGURATION_AND_SCHEDULING.md)
- [Testing and acceptance](05_TESTING_AND_ACCEPTANCE.md)

## Implementation decisions

- Python 3.11 or newer; dependency versions are pinned in `requirements.txt`.
- The current LangChain factory is `langchain.agents.create_agent`. It executes the model/tool loop. The implementation caps both Tavily calls (four per run) and LangGraph steps.
- `ChatGroq` from `langchain-groq` is configured through `GROQ_MODEL`. The default is `openai/gpt-oss-120b`, currently listed by Groq as tool-capable. The formerly common `llama-3.3-70b-versatile` is deprecated for free/developer tiers as of August 16, 2026.
- Tavily is accessed through `langchain_tavily.TavilySearch`, with news topic, basic search, at most five results, and raw content/images disabled. Its current LangChain integration does not expose a constructor timeout; Groq request timeout is configurable and Tavily uses the integration client's own timeout behavior.
- Research timestamps use `ZoneInfo` and `APP_TIMEZONE` (default `Asia/Karachi`). The research window is the preceding 24 hours.
- Reports are dated Markdown files. If a dated file already exists, a numeric run suffix is used rather than overwriting it.
- A process lock prevents overlapping local runs. Linux `flock` is used in the cron wrapper; manual concurrent runs are guarded by an application lock file.
- API keys are read from the environment or `.env`, never logged. Agent tools have no shell or filesystem access.

## Verification boundary

Current official LangChain integration docs and PyPI release metadata were checked while preparing this implementation. Source behavior can be unit-tested with mocks. No live Groq/Tavily requests, API billing, cron installation, or scheduler activation are part of this work. See the acceptance document for what remains unverified.
