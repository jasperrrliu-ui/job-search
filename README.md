# Job Search

Minimal target-company job tracker. It polls public ATS job-board APIs, stores job
history in SQLite, applies editable domain rules, and renders a daily digest.

## Current scope

- Providers: Greenhouse API, Greenhouse board HTML, Ashby, Lever, Workday
- Storage: local SQLite (`data/jobs.db`)
- Evaluation: deterministic hard eligibility, parsed degree/YOE pathways, and
  separate direction/capability/resume/trajectory fit dimensions
- Delivery: text digest preview; real email and cloud scheduling come after the
  local flow is validated

The active registry polls verified ATS sources. New companies are
silently baselined on their first successful poll, so their historical openings
are not misreported as newly posted jobs. A separate 1,586-company multi-source
Ring 1 pool is retained for Company Lens screening and future adapter work. It
combines the S&P 500, Lenny 100, a current unicorn database, AI/cloud rankings,
and the existing registry; candidate entries do not increase scheduled polling
cost until their official source is verified.

Run a source-coverage pass to promote candidates with an official careers entry
that resolves to a supported public ATS. It also writes a transparent ledger of
companies that are `verified_active`, `known_unsupported`,
`needs_official_url`, or `unverified`; being in the candidate pool never means a
company is silently treated as monitored.

```powershell
python scripts/discover_company_sources.py --all-candidates --workers 20
```

Company Lens uses 1,500 employees as the default minimum. Smaller companies stay
out unless they are explicitly marked `EXCEPTION_HIGH_FIT`. Data Science and
Applied/AI Scientist hiring are equally important screening signals.

Workday polling uses focused Data/AI/adjacent-science discovery searches, five bounded
network workers, and the listing `externalPath` as a persistent ID. Full job descriptions
are fetched for newly discovered postings and backfilled when an existing Workday record
has no usable description, so recommendations are not based on title alone.

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

# Analyze all retained jobs for campus/new-grad supply and recurring employers.
python -m job_tracker campus-report --output outputs/campus-report.txt

# Identify ATS sources that are failing or have never successfully polled.
python -m job_tracker source-health --output outputs/source-health.txt

# Re-score all existing openings after changing domain rules. This does not poll
# an ATS or send email.
python -m job_tracker reevaluate
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

`.github/workflows/job-search.yml` sends morning and afternoon digests at about
08:17 and 14:17 America/New_York, with 09:17 and 15:17 fallback triggers. A
fallback skips polling when its delivery slot already succeeded. Add the Gmail app
password as a repository Actions secret named `SMTP_PASSWORD`, then manually run
the workflow once to verify delivery. The SQLite database is persisted in the
Actions cache.

Run the manual `Campus and New Grad Report` workflow to analyze every job retained
in that cache. It emails separate counts for explicit campus/new-grad roles and
roles that merely appear compatible with zero to two years of experience, plus
company-level recurrence signals.

## Configuration

- `config/companies.json`: mutable company registry and ATS source settings
- `config/company_candidates.json`: non-active large-employer candidate pool
- `config/company_lens.json`: company-size threshold and activation policy
- `config/source_backlog.json`: classified companies that still need an adapter
- `config/domain.json`: candidate facts, hard-fail rules, target role clusters,
  resume evidence, and optional semantic-interpreter settings
- `config/schedule.json`: mutable polling and digest schedule

The program contains no Jasper-specific companies, titles, or thresholds. Those
values live in configuration and may be changed without editing the tracker.

## Optional LLM evaluation

The default evaluator is free and deterministic. It labels source-backed job age as
`FRESH`, `RECENT`, `STALE`, `UPDATED_DATE_ONLY`, or `AGE_UNKNOWN`; it never treats
the system's `first_seen_at` timestamp as an official posting date. Only explicitly US-located jobs
enter the email; ambiguous `Remote` locations stay `UNKNOWN`/`HOLD`. The target
titles are Data Scientist, AI/ML Scientist, and non-research-heavy Applied
Scientist. Senior/Sr. titles are excluded unless the JD contains an explicit
qualification path requiring no more than two years of experience; those rare
exceptions remain low-priority review items. Lead, Staff, Principal, Manager,
Director, Head, VP, Architect, and Supervisor titles are excluded at the
candidate gate. Digests group results into New Grad/Campus, Early Career,
explicit <=2 YOE compatible, Standard, and Senior Exception sections. A
standard role with neither direct early-career evidence nor an explicit <=2 YOE
path is retained as employer-market intelligence but is not sent as an
application recommendation.

For plausible or ambiguous jobs, an optional OpenAI Responses API interpreter
can classify responsibilities and role identity with quoted JD evidence. It
cannot run hard filters or choose the final action. Enable it with
`llm.enabled=true` and `OPENAI_API_KEY`; change the model with `OPENAI_MODEL`.
Purchased Codex credits are not API credits. Never commit an API key.
