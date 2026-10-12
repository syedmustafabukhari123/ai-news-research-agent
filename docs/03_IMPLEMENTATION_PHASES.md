# Implementation phases

Work proceeds sequentially and preserves the single-agent assignment boundary.

1. **Inspect and plan:** inventory the workspace; verify official imports, provider capabilities, model status, and dependency releases; write these documents.
2. **Foundation:** add project layout, pinned runtime dependencies, `.env.example`, `.gitignore`, configuration validation, and logging setup.
3. **Agent:** integrate `ChatGroq`, `TavilySearch`, current `create_agent`, a cautious analyst prompt, per-run tool-call cap, and graph-step cap.
4. **Reporting:** compute research timestamps, validate the final answer, support explicit no-results, and atomically save uniquely named Markdown reports.
5. **Scheduling:** add Linux wrapper and example cron line, document timezone/permissions/lock behavior, but do not install it.
6. **Tests and usage docs:** mock provider/agent boundaries, test deterministic behavior, document manual setup and future live integration procedure.

## Acceptance summary

- No API key or live call is needed to import the project or run unit tests.
- Missing configuration fails with an actionable message.
- Tool call and graph execution are bounded in code.
- A report is saved only after a valid completion, and existing reports are preserved.
- Cron and manual execution use the same Python entry point.
- All provider connectivity and real scheduling claims remain explicitly unverified until separately exercised.
