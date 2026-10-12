# Autonomous AI News Research Agent

Agent 1 of a larger assignment: a local Python news researcher that can run manually or through Linux cron. It uses a real LangChain tool-using loop (`create_agent`), Groq for inference, and Tavily for news search. No other agents or out-of-scope services are included.

## Project layout

```text
.
├── agent.py                 # Groq + LangChain agent loop and run limits
├── config.py                # dotenv settings, validation, timezone/window
├── main.py                  # shared manual/cron workflow, lock, logs, exit codes
├── prompts.py               # analyst role and evidence/report requirements
├── report.py                # validation, Markdown format, safe dated saves
├── tools.py                 # Tavily integration and two-search enforcement
├── scheduler/
│   ├── cron.example         # editable 09:00 Asia/Karachi example
│   └── run_daily.sh         # Linux cron wrapper with flock
├── docs/                    # plan, requirements, architecture, setup, acceptance
├── reports/                 # created when the first successful run is saved
├── logs/                    # created on application start
├── tests/                   # provider-free deterministic tests
├── .env.example
└── requirements.txt
```

See [the master plan](docs/00_MASTER_PLAN.md) and [architecture](docs/02_ARCHITECTURE.md) for details.

## Requirements and install

Use Python 3.11 or newer. Create and activate a virtual environment, then install the pinned packages:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and provide your own `GROQ_API_KEY` and `TAVILY_API_KEY`. Never commit `.env`. The default configurable model is `openai/gpt-oss-120b`; check current Groq model access before a live run. Current docs show Groq tool use for this model. The older `llama-3.3-70b-versatile` default is deprecated for free/developer tiers.

Configuration controls model, IANA timezone, report/log folders, rolling research window, request timeout, and logging. Each run uses at most two Tavily searches and reports at most three stories, with up to three normalized results per search. Snippets are at most 300 characters and each tool response is capped at 6,000 UTF-8 bytes. `openai/gpt-oss-20b` is an optional model choice when available and tool-call support is confirmed. More detail is in [configuration and scheduling](docs/04_CONFIGURATION_AND_SCHEDULING.md).

## Run manually

```bash
python -m main
```

The report is saved as `reports/ai_news_YYYY-MM-DD.md`. Additional successful runs for a date get `_2`, `_3`, etc.; existing reports are preserved. The agent returns typed structured story candidates; each is validated independently against retrieved Tavily evidence, and Markdown is rendered deterministically from accepted stories. Invalid candidates are logged with bounded reason codes; no report is written when none pass validation. Operational output goes to the console and `logs/agent.log`.

### Optional evidence audit

Set `EVIDENCE_AUDIT_ENABLED=true` in `.env` to write a companion file such as `reports/ai_news_YYYY-MM-DD.audit.json`. Set it to `false` (the default) or remove the setting to disable it. The JSON records accepted claims with their retrieved URL, exact cited excerpt, bounded source title/snippet (up to 300 characters each), provider-supplied publication date, and validation status. Rejected claims are recorded with their story/claim indices and sanitized reason codes, without rejected text or URLs. The file is atomically created beside the Markdown report and can be inspected with any text editor or JSON viewer. It contains third-party research text and remains local; the application does not publish or upload it. No audit file is created when report validation fails. If audit-file writing fails after report creation, the report remains and a sanitized error type is logged.

## Tests

```bash
python -m unittest discover -s tests -v
```

All unit tests mock external provider/agent boundaries and do not need keys or make network calls. A live run is separate, consumes provider quota, and is not part of these tests.

## Cron

Review and edit `scheduler/cron.example`, replacing the absolute project and virtualenv paths. Make the wrapper executable with `chmod 755 scheduler/run_daily.sh`, then install the reviewed line yourself using `crontab -e`. The example uses `CRON_TZ=Asia/Karachi` for 09:00 daily. Cron otherwise follows the host timezone. The wrapper resolves the project root and uses `flock`; it does not alter a crontab or activate scheduling automatically.

Linux cron remains available through the files above. Windows scheduling uses the Task Scheduler launcher described below.

## Windows Task Scheduler

For Windows, use `scheduler\run_daily.cmd` and the task definition template in `scheduler\windows_task.xml`, configured for 1:00 PM local Windows time and disabled by default. The launcher uses the project `.venv` when present, handles spaces in paths, and writes scheduler output under `logs`. Run the provider-free check first with `scheduler\run_daily.cmd --preflight-only`; it verifies the selected Python, dependencies, `.env` presence without printing values, and output paths. Review the Task Scheduler import, enable, disable, and removal steps in [configuration and scheduling](docs/04_CONFIGURATION_AND_SCHEDULING.md). The project does not register or enable tasks automatically.

## Verification status

The implementation and mock tests are provided. Groq and Tavily connectivity has been verified in controlled live runs; the Windows task is configured locally, but its scheduled execution has not yet been observed. Linux cron is not activated automatically. Use [the acceptance guide](docs/05_TESTING_AND_ACCEPTANCE.md) for the distinctions between implemented, mock-tested, live-tested, and operational.
