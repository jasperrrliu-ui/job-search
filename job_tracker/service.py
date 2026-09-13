from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        new_listings = reconcile_workday_discovery(
            connection, company["name"], listing_jobs
        )
        new_ids = {listing["source_listing_id"] for listing in new_listings}
        new_postings = [
            posting
            for posting in postings
            if posting["externalPath"] in new_ids
        ]
        company_baseline = baseline or not has_successful_poll(
            connection, company["name"]
        )
        print(
            f"{company['name']}: {len(postings)} discovered, "
            f"{len(new_postings)} need enrichment",
            flush=True,
        )
        if company_baseline:
            created, changed = upsert_jobs(
                connection, company["name"], new_listings, True, close_missing=False
            )
            evaluate_company_jobs(connection, company["name"], domain)
            record_poll(
                connection,
                company["name"],
                workday_started[company["name"]],
                True,
                len(postings),
                None,
            )
            total_new += created
            total_changed += changed
            print(
                f"{company['name']}: baseline saved without detail downloads",
                flush=True,
            )
        else:
            enrichment_inputs[company["name"]] = (company, new_postings, len(postings))

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(enrich_workday_jobs, company, postings): (
                company,
                discovered_count,
            )
            for company, postings, discovered_count in enrichment_inputs.values()
        }
        for future in as_completed(futures):
            company, discovered_count = futures[future]
            try:
                jobs = future.result()
                created, changed = upsert_jobs(
                    connection, company["name"], jobs, False, close_missing=False
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
) -> None:
    rows = connection.execute(
        """
        SELECT jobs.*, companies.name AS company FROM jobs
        JOIN companies ON companies.id = jobs.company_id
        WHERE companies.name=? AND jobs.status='OPEN'
        """,
        (company_name,),
    ).fetchall()

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
    connection.commit()


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
               jobs.first_seen_at, companies.name AS company,
               evaluations.decision, evaluations.eligibility,
               evaluations.role_archetype, evaluations.career_direction_fit,
               evaluations.capability_fit, evaluations.resume_signal_fit,
               evaluations.trajectory_fit, evaluations.reasoning,
               evaluations.uncertainty
        FROM jobs
        JOIN companies ON companies.id = jobs.company_id
        JOIN evaluations ON evaluations.id = (
            SELECT id FROM evaluations e
            WHERE e.job_id = jobs.id ORDER BY id DESC LIMIT 1
        )
        WHERE jobs.status='OPEN' {notification_filter}
          AND evaluations.decision IN ('APPLY_TODAY', 'REVIEW')
        ORDER BY CASE evaluations.decision WHEN 'APPLY_TODAY' THEN 0 ELSE 1 END,
                 jobs.first_seen_at DESC
        """,
        parameters,
    ).fetchall()

    if not rows:
        return "Today: 0 new matching jobs\n", []

    lines = [f"Today: {len(rows)} new matching jobs", ""]
    ids = []
    for index, row in enumerate(rows, start=1):
        ids.append(row["id"])
        lines.extend(
            [
                f"{index}. {row['title']} — {row['company']}",
                f"Location: {row['location'] or 'Not listed'}",
                f"Recommendation: {row['decision']}",
                f"Eligibility: {row['eligibility']}",
                f"Role: {row['role_archetype']}",
                f"Direction / Capability / Resume / Trajectory: "
                f"{row['career_direction_fit']} / {row['capability_fit']} / "
                f"{row['resume_signal_fit']} / {row['trajectory_fit']}",
                f"Reason: {row['reasoning']}",
                f"Uncertainty: {row['uncertainty'] or 'None recorded'}",
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
