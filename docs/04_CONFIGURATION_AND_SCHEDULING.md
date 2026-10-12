# Configuration and scheduling

## Local setup

Use Python 3.11 or newer. Create an isolated virtual environment, activate it, then install `requirements.txt`. Copy `.env.example` to `.env` and supply your own Groq and Tavily keys. Do not commit `.env`.

Configuration variables are documented in `.env.example`. Important defaults are model `openai/gpt-oss-120b`, timezone `Asia/Karachi`, a 24-hour research window, at most two searches and three stories per run, three results per search, and a 120-second Groq request timeout. `MAX_SEARCHES` accepts values from 1 to 2. `openai/gpt-oss-20b` is an optional alternative if enabled for the account and confirmed to support tool calls; the application does not switch models automatically. Tavily output is reduced to title, URL, available publication date, and a maximum 300-character snippet, with a 6,000-byte response ceiling. Search limits and timeout are validated to safe bounds. The current LangChain Tavily tool does not expose a timeout constructor setting; its wrapped client uses its own request timeout.

Successful model-call usage metadata is accumulated where ChatGroq supplies it. Logs distinguish model calls from calls with token usage and mark incomplete token totals when any usage is unavailable. For 429 responses, only allow-listed status/error/header data is logged. The provider's `Retry-After` is honored by its bounded retry; no full-agent restart is performed. If the response does not identify a specific dimension, diagnostics report unknown throttling. Connection resets are recorded as connection errors separately from 429s.

## Manual run

From the repository root, execute:

```bash
python -m main
```

The report is written under `reports/`; logs are written under `logs/`. The application exits nonzero on failed research and does not create a success report.

## Linux cron

Make `scheduler/run_daily.sh` executable (`chmod 755 scheduler/run_daily.sh`). Review and customize `scheduler/cron.example`; install it manually with your preferred crontab editor. The example runs daily at 09:00 in Asia/Karachi using `CRON_TZ=Asia/Karachi` and an absolute `flock` lock path. Cron uses the host timezone unless configured otherwise. Cron should use an absolute path to the Python interpreter in the project's virtual environment; edit the example placeholder before installation.

The script resolves its own project root, changes to that directory, and runs `python -m main`. Linux `flock` prevents overlapping cron invocations. The Python entry point also uses a local lock. This repository never edits the user's system crontab.

## Windows Task Scheduler

The Windows launcher is `scheduler/run_daily.cmd`; it resolves the project root from its own location, handles spaces in paths, prefers `.venv\Scripts\python.exe`, and otherwise selects Python 3.12 through the Windows `py` launcher. It changes to the project root before starting `python -m main`, so `.env` is loaded by the application's absolute project-root path and relative `REPORT_DIR`/`LOG_DIR` values resolve from the project root. The application writes reports and its `agent.log` under the configured directories. Launcher stdout/stderr and exit codes are appended to `logs\scheduler-console.log`.

Run a local check that never starts the agent or calls Groq/Tavily:

```powershell
.\scheduler\run_daily.cmd --preflight-only
```

It checks Python 3.11+, the pinned project dependencies, `.env` key presence without showing values, the model, and resolved report/log paths. It exits nonzero if the selected environment is not ready. Do not use the daily task until this check passes. Create a project virtual environment with a supported Python and install `requirements.txt` before scheduling if the preflight reports missing dependencies. The launcher does not install packages.

The prepared `scheduler\windows_task.xml` defines a daily 13:00 trigger in local Windows time and is disabled. Review its account, start date, project path, and action before importing. To register the disabled task from PowerShell:

```powershell
schtasks.exe /Create /TN "\AI News Research Agent Daily" /XML "D:\Agentic Ai\News Agent\scheduler\windows_task.xml"
```

Inspect it before enabling:

```powershell
schtasks.exe /Query /TN "\AI News Research Agent Daily" /V /FO LIST
```

Enable only after preflight passes and a separate controlled live run is approved:

```powershell
schtasks.exe /Change /TN "\AI News Research Agent Daily" /ENABLE
```

Disable it:

```powershell
schtasks.exe /Change /TN "\AI News Research Agent Daily" /DISABLE
```

Remove it:

```powershell
schtasks.exe /Delete /TN "\AI News Research Agent Daily" /F
```

The task uses the current Windows account's interactive token, so that account must be logged on and the computer awake at 13:00. Task Scheduler is set to ignore a new instance while one is running; `main.py` also has a nonblocking process lock. The XML currently starts on 2026-10-13; update the local-time start boundary if importing it after that date. The project does not register or enable tasks automatically; the existing Windows task may be registered and enabled separately.

## Future live integration check

After adding personal keys to `.env`, the user can manually run `python -m main`. This makes real Groq and Tavily requests and may consume account quota. Confirm model availability/permissions, current provider terms, and desired cost limits first. Then check the exit code, `logs/agent.log` for a successful run and nonzero search count, the report's timestamps and cited source links, and that a second same-day run creates a suffixed file. Live API execution is not part of automated tests. To verify scheduling later, install the reviewed cron entry manually, wait for its next 09:00 Asia/Karachi run (or temporarily use a reviewed near-future time), and inspect its exit/log/report. Do not infer scheduler operation from merely installing the line.
