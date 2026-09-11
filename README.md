# Job Search

Minimal target-company job tracker. It polls public ATS job-board APIs, stores job
history in SQLite, applies editable domain rules, and renders a daily digest.

## Current scope

- Providers: Greenhouse API, Greenhouse board HTML, Ashby, Lever
- Storage: local SQLite (`data/jobs.db`)
- Evaluation: deterministic hard eligibility, parsed degree/YOE pathways, and
  separate direction/capability/resume/trajectory fit dimensions
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

## GitHub Actions

`.github/workflows/job-search.yml` polls every two hours and sends the digest at
07:00 America/New_York. Add the Gmail app password as a repository Actions secret
named `SMTP_PASSWORD`, then manually run the workflow once to create the baseline
and verify delivery. The SQLite database is persisted in the Actions cache.

## Configuration

- `config/companies.json`: mutable company registry and ATS source settings
- `config/source_backlog.json`: classified companies that still need an adapter
- `config/domain.json`: candidate facts, hard-fail rules, target role clusters,
  resume evidence, and optional semantic-interpreter settings
- `config/schedule.json`: mutable polling and digest schedule

The program contains no Jasper-specific companies, titles, or thresholds. Those
values live in configuration and may be changed without editing the tracker.

## Optional LLM evaluation

The default evaluator is free and deterministic. Only explicitly US-located jobs
enter the email; ambiguous `Remote` locations stay `UNKNOWN`/`HOLD`. It never
rejects `Senior`, `Sr.`, `II`, or `III` from title alone and stores explicit
degree/YOE pathways separately. Lead, Staff, and Principal are saved as strong
negative review signals rather than eligibility failures.

For plausible or ambiguous jobs, an optional OpenAI Responses API interpreter
can classify responsibilities and role identity with quoted JD evidence. It
cannot run hard filters or choose the final action. Enable it with
`llm.enabled=true` and `OPENAI_API_KEY`; change the model with `OPENAI_MODEL`.
Purchased Codex credits are not API credits. Never commit an API key.
