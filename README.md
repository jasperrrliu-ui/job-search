# Job Search

Minimal target-company job tracker. It polls public ATS job-board APIs, stores job
history in SQLite, applies editable domain rules, and renders a daily digest.

## Current scope

- Providers: Greenhouse API, Greenhouse board HTML, Ashby, Lever
- Storage: local SQLite (`data/jobs.db`)
- Evaluation: editable rules in `config/domain.json`; incomplete knowledge returns
  `UNKNOWN`/`REVIEW`, never an automatic rejection
- Delivery: text digest preview; real email and cloud scheduling come after the
  local flow is validated

The Lenny 100 is stored as a neutral-priority company registry. Eighty-one ATS
sources have been verified. Anduril and SpaceX are recorded but disabled because
each exposes more than 2,000 postings; companies without a source remain in the
registry and are not polled yet. Their source types and next adapter work are
recorded in `config/source_backlog.json`.

## Run

Python 3.11+ is enough. There are no third-party dependencies.

```powershell
# First run only: save today's openings as the baseline.
python -m job_tracker bootstrap

# Later runs: detect new or changed jobs.
python -m job_tracker poll

# Preview jobs not yet included in a sent digest.
python -m job_tracker digest

# Save the preview to a file.
python -m job_tracker digest --output outputs/digest.txt
```

To mark the current digest as sent:

```powershell
python -m job_tracker digest --mark-sent
```

Do not use `--mark-sent` for ordinary previews.

Record a real decision so it can become future domain knowledge:

```powershell
python -m job_tracker feedback --job-id 123 --decision APPLY --reason "Strong applied AI role"
```

The `email` command sends through any SMTP provider and marks included jobs as
sent only after delivery succeeds. It needs four environment variables:

```powershell
$env:SMTP_HOST = "smtp.example.com"
$env:SMTP_PORT = "587"
$env:SMTP_USER = "your-sender-account"
$env:SMTP_PASSWORD = "your-provider-password-or-api-key"
python -m job_tracker email
```

The recipient and subject live in `config/delivery.json`. Do not commit passwords
or API keys to Git.

## Configuration

- `config/companies.json`: mutable company registry and ATS source settings
- `config/source_backlog.json`: classified companies that still need an adapter
- `config/domain.json`: mutable target titles, fit signals, and action thresholds
- `config/schedule.json`: mutable polling and digest schedule

The program contains no Jasper-specific companies, titles, or thresholds. Those
values live in configuration and may be changed without editing the tracker.

## Optional LLM evaluation

The default evaluator is free and deterministic. To evaluate title-matched jobs
with the OpenAI Responses API, set `llm.enabled` to `true` in
`config/domain.json` and provide `OPENAI_API_KEY`. The model can be changed with
`OPENAI_MODEL`. Never commit an API key.
