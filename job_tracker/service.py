from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import (
    discover_workday_jobs,
    enrich_workday_jobs,
    fetch_jobs,
    workday_listing_job,
)
from .database import (
    reconcile_workday_discovery,
    record_poll,
    sync_companies,
    upsert_jobs,
    utc_now,
)
from .evaluation import evaluate

FULL_BOARD_PROVIDERS = {"greenhouse", "greenhouse_html", "ashby", "lever"}


def load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def has_successful_poll(
    connection: sqlite3.Connection, company_name: str
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM poll_runs
            JOIN companies ON companies.id = poll_runs.company_id
            WHERE companies.name=? AND poll_runs.success=1
            LIMIT 1
            """,
            (company_name,),
        ).fetchone()
        is not None
    )


def company_is_due(connection: sqlite3.Connection, company: dict) -> bool:
    interval = company.get("poll_interval_hours")
    if not interval:
        return True
    row = connection.execute(
        """
        SELECT poll_runs.finished_at FROM poll_runs
        JOIN companies ON companies.id = poll_runs.company_id
        WHERE companies.name=? AND poll_runs.success=1
        ORDER BY poll_runs.id DESC LIMIT 1
        """,
        (company["name"],),
    ).fetchone()
    if row is None:
        return True
    last_poll = datetime.fromisoformat(row["finished_at"])
    return datetime.now(timezone.utc) - last_poll >= timedelta(hours=interval)


def poll_all(
    connection: sqlite3.Connection,
    companies_path: Path,
    domain_path: Path,
    baseline: bool = False,
) -> tuple[int, int, int]:
    companies = load_json(companies_path)
    domain = load_json(domain_path)
    sync_companies(connection, companies)
    total_new = 0
    total_changed = 0
    failures = 0

    active = [
        company
        for company in companies
        if company.get("active", True) and company.get("source")
        and company_is_due(connection, company)
    ]
    regular = [
        company
        for company in active
        if company["source"]["provider"].lower() != "workday"
    ]
    workday = [
        company
        for company in active
        if company["source"]["provider"].lower() == "workday"
    ]

    for company in regular:
        started_at = utc_now()
        print(f"{company['name']}: polling started", flush=True)
        try:
            jobs = fetch_jobs(company, domain["candidate_generation"])
            company_baseline = baseline or not has_successful_poll(
                connection, company["name"]
            )
            created, changed = upsert_jobs(
                connection, company["name"], jobs, company_baseline
            )
            evaluate_company_jobs(connection, company["name"], domain)
            record_poll(connection, company["name"], started_at, True, len(jobs), None)
            total_new += created
            total_changed += changed
            print(
                f"{company['name']}: {len(jobs)} open, {created} new, {changed} changed",
                flush=True,
            )
        except Exception as exc:
            failures += 1
            record_poll(connection, company["name"], started_at, False, None, str(exc))
            print(f"{company['name']}: ERROR {exc}", flush=True)

    discoveries = {}
    workday_started = {company["name"]: utc_now() for company in workday}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for company in workday:
            future = executor.submit(
                discover_workday_jobs, company, domain["candidate_generation"]
            )
            futures[future] = company
        for future in as_completed(futures):
            company = futures[future]
            try:
                discoveries[company["name"]] = future.result()
            except Exception as exc:
                failures += 1
                record_poll(
                    connection,
                    company["name"],
                    workday_started[company["name"]],
                    False,
                    None,
                    str(exc),
                )
                print(f"{company['name']}: ERROR {exc}", flush=True)

    enrichment_inputs = {}
    for company in workday:
        postings = discoveries.get(company["name"])
        if postings is None:
            continue
        listing_jobs = [workday_listing_job(company, posting) for posting in postings]
        postings_needing_enrichment = reconcile_workday_discovery(
            connection, company["name"], listing_jobs
        )
        company_baseline = baseline or not has_successful_poll(
            connection, company["name"]
        )
        print(
            f"{company['name']}: {len(postings)} discovered, "
            f"{len(postings_needing_enrichment)} need enrichment",
            flush=True,
        )
        if postings_needing_enrichment:
            enrichment_inputs[company["name"]] = (
                company,
                postings_needing_enrichment,
                len(postings),
                company_baseline,
            )
        else:
            evaluate_company_jobs(connection, company["name"], domain)
            record_poll(
                connection,
                company["name"],
                workday_started[company["name"]],
                True,
                len(postings),
                None,
            )

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(enrich_workday_jobs, company, postings): (
                company,
                discovered_count,
                company_baseline,
            )
            for company, postings, discovered_count, company_baseline in enrichment_inputs.values()
        }
        for future in as_completed(futures):
            company, discovered_count, company_baseline = futures[future]
            try:
                jobs = future.result()
                created, changed = upsert_jobs(
                    connection, company["name"], jobs, company_baseline, close_missing=False
                )
                evaluate_company_jobs(connection, company["name"], domain)
                record_poll(
                    connection,
                    company["name"],
                    workday_started[company["name"]],
                    True,
                    discovered_count,
                    None,
                )
                total_new += created
                total_changed += changed
                print(
                    f"{company['name']}: {discovered_count} open, "
                    f"{created} new, {changed} changed",
                    flush=True,
                )
            except Exception as exc:
                failures += 1
                record_poll(
                    connection,
                    company["name"],
                    workday_started[company["name"]],
                    False,
                    None,
                    str(exc),
                )
                print(f"{company['name']}: ERROR {exc}", flush=True)

    return total_new, total_changed, failures


def evaluate_company_jobs(
    connection: sqlite3.Connection, company_name: str, domain: dict
) -> int:
    rows = connection.execute(
        """
        SELECT jobs.*, companies.name AS company FROM jobs
        JOIN companies ON companies.id = jobs.company_id
        WHERE companies.name=? AND jobs.status='OPEN'
        """,
        (company_name,),
    ).fetchall()

    created = 0
    for row in rows:
        result = evaluate(dict(row), domain)
        latest = connection.execute(
            """
            SELECT config_version, job_content_hash FROM evaluations
            WHERE job_id=? ORDER BY id DESC LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        if (
            latest
            and latest["config_version"] == domain["version"]
            and latest["job_content_hash"] == row["content_hash"]
        ):
            continue
        connection.execute(
            """
            INSERT INTO evaluations(
                job_id, config_version, job_content_hash, evaluated_at,
                eligibility, role_archetype, fit, freshness, priority,
                score, decision, reasoning, uncertainty,
                career_direction_fit, capability_fit, resume_signal_fit,
                trajectory_fit, evidence_json, qualification_paths_json,
                role_interpretation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                domain["version"],
                row["content_hash"],
                utc_now(),
                result["eligibility"],
                result["role_archetype"],
                result["fit"],
                result["freshness"],
                result["priority"],
                result["score"],
                result["decision"],
                result["reasoning"],
                result["uncertainty"],
                result["career_direction_fit"],
                result["capability_fit"],
                result["resume_signal_fit"],
                result["trajectory_fit"],
                json.dumps(result["evidence"], ensure_ascii=False),
                json.dumps(result["qualification_paths"], ensure_ascii=False),
                json.dumps(result["role_interpretation"], ensure_ascii=False),
            ),
        )
        created += 1
    connection.commit()
    return created


def evaluate_all_open_jobs(connection: sqlite3.Connection, domain: dict) -> int:
    """Re-score retained openings without polling sources or sending notifications."""
    companies = connection.execute(
        """
        SELECT DISTINCT companies.name
        FROM companies JOIN jobs ON jobs.company_id=companies.id
        WHERE jobs.status='OPEN'
        ORDER BY companies.name
        """
    ).fetchall()
    return sum(evaluate_company_jobs(connection, row["name"], domain) for row in companies)


def build_source_health_report(connection: sqlite3.Connection) -> str:
    """Expose source failures so a failed ATS is never mistaken for no openings."""
    rows = connection.execute(
        """
        SELECT companies.name, companies.provider,
               latest.finished_at, latest.success, latest.job_count, latest.error,
               (SELECT COUNT(*) FROM poll_runs history
                WHERE history.company_id=companies.id AND history.success=0) AS failure_count
               ,(SELECT MAX(success) FROM poll_runs history
                 WHERE history.company_id=companies.id) AS has_success
        FROM companies
        LEFT JOIN poll_runs latest ON latest.id=(
            SELECT id FROM poll_runs p WHERE p.company_id=companies.id
            ORDER BY id DESC LIMIT 1
        )
        WHERE companies.active=1 AND companies.provider IS NOT NULL
        ORDER BY CASE
                   WHEN latest.id IS NULL THEN 0
                   WHEN latest.success=0 THEN 1
                   ELSE 2
                 END,
                 failure_count DESC, companies.name
        """
    ).fetchall()
    unmonitored = [row for row in rows if not row["has_success"]]
    failing = [row for row in rows if row["finished_at"] is not None and not row["success"]]
    lines = [
        f"Active sources: {len(rows)}",
        f"Never successfully polled: {len(unmonitored)}",
        f"Latest poll failed: {len(failing)}",
        "",
    ]
    for heading, source_rows in (
        ("Never successfully polled", unmonitored),
        ("Latest poll failed", failing),
    ):
        if not source_rows:
            continue
        lines.extend([heading, ""])
        for row in source_rows:
            detail = row["error"] or "No poll record"
            lines.append(
                f"- {row['name']} ({row['provider']}): {detail} "
                f"[historical failures: {row['failure_count']}]"
            )
        lines.append("")
    return "\n".join(lines)


def build_tracker_status_report(
    connection: sqlite3.Connection, domain: dict | None = None
) -> str:
    """Return the tracker funnel from registered sources through human feedback."""
    companies = connection.execute(
        """
        SELECT COUNT(*) AS total, SUM(active) AS active,
               SUM(active=1 AND provider IS NOT NULL) AS active_with_source
        FROM companies
        """
    ).fetchone()
    polls = connection.execute(
        """
        SELECT COUNT(*) AS total, SUM(success=1) AS successful, SUM(success=0) AS failed,
               COUNT(DISTINCT CASE WHEN success=1 THEN company_id END) AS companies_with_success,
               MIN(started_at) AS first_poll, MAX(finished_at) AS latest_poll
        FROM poll_runs
        """
    ).fetchone()
    jobs = connection.execute(
        """
        SELECT COUNT(*) AS total, SUM(status='OPEN') AS open, SUM(status='CLOSED') AS closed,
               MIN(first_seen_at) AS first_seen, MAX(last_seen_at) AS latest_seen
        FROM jobs
        """
    ).fetchone()
    snapshots = connection.execute("SELECT COUNT(*) AS total FROM job_snapshots").fetchone()
    evaluations = connection.execute(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT job_id) AS jobs_evaluated FROM evaluations"
    ).fetchone()
    deliveries = connection.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(job_count), 0) AS jobs_delivered,
               MAX(sent_at) AS latest_delivery
        FROM daily_deliveries
        """
    ).fetchone()
    feedback = connection.execute("SELECT COUNT(*) AS total FROM feedback").fetchone()
    source_rows = connection.execute(
        "SELECT provider, COUNT(*) AS total FROM companies "
        "WHERE active=1 AND provider IS NOT NULL GROUP BY provider"
    ).fetchall()
    full_board_sources = sum(
        row["total"] for row in source_rows if row["provider"] in FULL_BOARD_PROVIDERS
    )
    title_filtered_sources = sum(
        row["total"] for row in source_rows if row["provider"] not in FULL_BOARD_PROVIDERS
    )

    latest_evaluations = connection.execute(
        """
        SELECT jobs.status, evaluations.config_version,
               evaluations.role_interpretation_json
        FROM jobs
        JOIN evaluations ON evaluations.id=(
            SELECT id FROM evaluations current
            WHERE current.job_id=jobs.id ORDER BY id DESC LIMIT 1
        )
        WHERE jobs.status='OPEN'
        """
    ).fetchall()
    role_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    target_open = 0
    for row in latest_evaluations:
        role = json.loads(row["role_interpretation_json"] or "{}")
        gate = role.get("candidate_gate", {})
        if gate.get("status") not in {"PRIMARY", "SECONDARY", "STRETCH"}:
            continue
        target_open += 1
        family = gate.get("family", "UNKNOWN")
        stage = role.get("career_stage", "YOE_UNKNOWN")
        role_counts[family] = role_counts.get(family, 0) + 1
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    def value(row: sqlite3.Row, name: str) -> int:
        return row[name] or 0

    lines = [
            "# Job Tracker Dashboard",
            "",
            f"_Database current through: {polls['latest_poll'] or 'NONE'}_",
            f"_Evaluation taxonomy: {(domain or {}).get('version', 'latest stored evaluation')}_",
            "",
            "| Stage | Metric | Value |",
            "|---|---|---:|",
            f"| 1. Sources | Registered companies | {value(companies, 'total'):,} |",
            f"| 1. Sources | Active companies | {value(companies, 'active'):,} |",
            f"| 1. Sources | Active companies with source | {value(companies, 'active_with_source'):,} |",
            f"| 2. Collection | Poll runs | {value(polls, 'total'):,} |",
            f"| 2. Collection | Successful polls | {value(polls, 'successful'):,} |",
            f"| 2. Collection | Failed polls | {value(polls, 'failed'):,} |",
            f"| 2. Collection | Companies successfully polled | {value(polls, 'companies_with_success'):,} |",
            f"| 3. Inventory | Unique jobs retained | {value(jobs, 'total'):,} |",
            f"| 3. Inventory | Open jobs | {value(jobs, 'open'):,} |",
            f"| 3. Inventory | Closed jobs | {value(jobs, 'closed'):,} |",
            f"| 3. Inventory | Job snapshots | {value(snapshots, 'total'):,} |",
            f"| 4. Evaluation | Evaluation records | {value(evaluations, 'total'):,} |",
            f"| 4. Evaluation | Distinct jobs evaluated | {value(evaluations, 'jobs_evaluated'):,} |",
            f"| 5. Delivery | Digests sent | {value(deliveries, 'total'):,} |",
            f"| 5. Delivery | Recommendations delivered | {value(deliveries, 'jobs_delivered'):,} |",
            f"| 6. Outcomes | Feedback/application outcomes | {value(feedback, 'total'):,} |",
            "",
            "## Collection coverage",
            "",
            "| Capture mode | Active sources | Historical interpretation |",
            "|---|---:|---|",
            f"| Full-board capture | {full_board_sources:,} | Existing raw history can be reclassified across role families |",
            f"| Title-filtered capture | {title_filtered_sources:,} | Historical roles omitted by old title queries cannot be recovered retroactively |",
            f"| No configured source | {max(0, value(companies, 'active') - value(companies, 'active_with_source')):,} | Not polled |",
            "",
            "## Open target roles by family",
            "",
            f"Open jobs classified into the current or latest stored target taxonomy: **{target_open:,}**.",
            "",
            "| Role family | Open jobs |",
            "|---|---:|",
        ]
    role_order = [
        "DATA_ANALYST",
        "DATA_SCIENTIST",
        "DATA_ENGINEER",
        "ML_ENGINEER",
    ]
    for family in role_order:
        lines.append(f"| {family} | {role_counts.pop(family, 0):,} |")
    for family in sorted(role_counts):
        lines.append(f"| {family} | {role_counts[family]:,} |")

    lines.extend(
        [
            "",
            "## Open target roles by career stage",
            "",
            "| Career stage | Open jobs |",
            "|---|---:|",
        ]
    )
    stage_order = [
        "INTERNSHIP_COOP",
        "NEW_GRAD_CAMPUS",
        "FULL_TIME_0_2_YOE",
        "YOE_UNKNOWN",
        "OUT_OF_SCOPE",
        "SENIOR_EXCEPTION",
    ]
    for stage in stage_order:
        lines.append(f"| {stage} | {stage_counts.pop(stage, 0):,} |")
    for stage in sorted(stage_counts):
        lines.append(f"| {stage} | {stage_counts[stage]:,} |")

    lines.extend(
        [
            "",
            "## Data window",
            "",
            f"- First poll: `{polls['first_poll'] or 'NONE'}`",
            f"- Latest poll: `{polls['latest_poll'] or 'NONE'}`",
            f"- First job seen: `{jobs['first_seen'] or 'NONE'}`",
            f"- Latest job seen: `{jobs['latest_seen'] or 'NONE'}`",
            f"- Latest digest: `{deliveries['latest_delivery'] or 'NONE'}`",
            "",
            "> Jobs and evaluations are tracker observations, not applications. "
            "Until outcomes are recorded, response and interview conversion cannot be measured.",
            "",
            "> Historical completeness differs by capture mode. A v2 re-evaluation can "
            "reclassify retained jobs, but it cannot reconstruct jobs that an older "
            "title-filtered poll never collected.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_digest(
    connection: sqlite3.Connection,
    first_seen_since: str | None = None,
    include_notified: bool = False,
) -> tuple[str, list[int]]:
    notification_filter = (
        "AND jobs.first_seen_at >= ? "
        "AND (jobs.notified_at IS NULL OR jobs.notified_at <> jobs.first_seen_at)"
        if include_notified
        else "AND jobs.notified_at IS NULL"
    )
    parameters = (first_seen_since,) if include_notified else ()
    rows = connection.execute(
        f"""
        SELECT jobs.id, jobs.title, jobs.location, jobs.official_url,
               jobs.official_created_at, jobs.official_updated_at,
               jobs.first_seen_at, companies.name AS company,
               evaluations.decision, evaluations.eligibility, evaluations.freshness, evaluations.score,
               evaluations.role_archetype, evaluations.career_direction_fit,
                evaluations.capability_fit, evaluations.resume_signal_fit,
                evaluations.trajectory_fit, evaluations.reasoning,
                evaluations.uncertainty, evaluations.role_interpretation_json
        FROM jobs
        JOIN companies ON companies.id = jobs.company_id
        JOIN evaluations ON evaluations.id = (
            SELECT id FROM evaluations e
            WHERE e.job_id = jobs.id ORDER BY id DESC LIMIT 1
        )
        WHERE jobs.status='OPEN' {notification_filter}
          AND evaluations.decision IN ('APPLY_TODAY', 'REVIEW')
        ORDER BY CASE evaluations.decision WHEN 'APPLY_TODAY' THEN 0 ELSE 1 END,
                 CASE evaluations.freshness
                   WHEN 'FRESH' THEN 0 WHEN 'RECENT' THEN 1
                   WHEN 'UPDATED_DATE_ONLY' THEN 2 WHEN 'AGE_UNKNOWN' THEN 3
                   ELSE 4 END,
                 evaluations.score DESC,
                 COALESCE(jobs.official_created_at, jobs.official_updated_at, jobs.first_seen_at) DESC
        """,
        parameters,
    ).fetchall()

    if not rows:
        return "Today: 0 new matching jobs\n", []

    active_rows = connection.execute(
        """
        SELECT companies.name AS company, evaluations.role_interpretation_json
        FROM jobs
        JOIN companies ON companies.id = jobs.company_id
        JOIN evaluations ON evaluations.id = (
            SELECT id FROM evaluations e
            WHERE e.job_id = jobs.id ORDER BY id DESC LIMIT 1
        )
        WHERE jobs.status='OPEN'
          AND evaluations.decision IN ('APPLY_TODAY', 'REVIEW')
        """
    ).fetchall()
    employer_counts: dict[str, int] = {}
    for row in active_rows:
        stage = json.loads(row["role_interpretation_json"]).get("career_stage")
        if stage in {"INTERNSHIP_COOP", "NEW_GRAD_CAMPUS", "FULL_TIME_0_2_YOE"}:
            employer_counts[row["company"]] = employer_counts.get(row["company"], 0) + 1

    lines = [f"Today: {len(rows)} new matching jobs"]
    if employer_counts:
        signals = sorted(employer_counts.items(), key=lambda item: (-item[1], item[0]))
        lines.extend(
            [
                f"Active early-career employers: {len(signals)}",
                "Signals: " + ", ".join(f"{company} ({count})" for company, count in signals[:15]),
            ]
        )
    lines.append("")

    sections = [
        ("INTERNSHIP_COOP", "Priority 1 — Internship / Co-op"),
        ("NEW_GRAD_CAMPUS", "Priority 2 — New Grad / Campus"),
        ("FULL_TIME_0_2_YOE", "Priority 3 — Full-time 0–2 YOE"),
        ("YOE_UNKNOWN", "Review — Target role with YOE not stated or not parsed"),
        ("SENIOR_EXCEPTION", "Stretch — Senior title with explicit <=2 YOE path"),
    ]
    grouped = {stage: [] for stage, _ in sections}
    for row in rows:
        stage = json.loads(row["role_interpretation_json"]).get("career_stage", "YOE_UNKNOWN")
        grouped.setdefault(stage, []).append(row)

    ids = []
    index = 0
    for stage, heading in sections:
        if not grouped[stage]:
            continue
        lines.extend([heading, ""])
        for row in grouped[stage]:
            index += 1
            ids.append(row["id"])
            source_event = (
                "NEWLY_POSTED" if row["official_created_at"] else "NEWLY_DISCOVERED"
            )
            lines.extend(
                [
                    f"{index}. {row['title']} — {row['company']}",
                    f"Location: {row['location'] or 'Not listed'}",
                    f"Career stage: {stage}",
                    f"Recommendation: {row['decision']}",
                    f"Eligibility: {row['eligibility']}",
                    f"Freshness: {row['freshness']}",
                    f"Search score: {row['score']}",
                    f"Role: {row['role_archetype']}",
                    f"Direction / Capability / Resume / Trajectory: "
                    f"{row['career_direction_fit']} / {row['capability_fit']} / "
                    f"{row['resume_signal_fit']} / {row['trajectory_fit']}",
                    f"Reason: {row['reasoning']}",
                    f"Uncertainty: {row['uncertainty'] or 'None recorded'}",
                    f"Source event: {source_event}",
                    f"Posted: {row['official_created_at'] or 'UNKNOWN'}",
                    f"Updated: {row['official_updated_at'] or 'UNKNOWN'}",
                    f"First seen: {row['first_seen_at']}",
                    f"URL: {row['official_url']}",
                    "",
                ]
            )
    return "\n".join(lines), ids


def mark_notified(connection: sqlite3.Connection, job_ids: list[int]) -> None:
    if not job_ids:
        return
    placeholders = ",".join("?" for _ in job_ids)
    connection.execute(
        f"UPDATE jobs SET notified_at=? WHERE id IN ({placeholders})",
        (utc_now(), *job_ids),
    )
    connection.commit()
