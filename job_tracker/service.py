from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .adapters import fetch_jobs
from .database import record_poll, sync_companies, upsert_jobs, utc_now
from .evaluation import evaluate


def load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


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

    for company in companies:
        if not company.get("active", True) or not company.get("source"):
            continue
        started_at = utc_now()
        try:
            jobs = fetch_jobs(company)
            has_successful_poll = connection.execute(
                """
                SELECT 1 FROM poll_runs
                JOIN companies ON companies.id = poll_runs.company_id
                WHERE companies.name=? AND poll_runs.success=1
                LIMIT 1
                """,
                (company["name"],),
            ).fetchone()
            company_baseline = baseline or has_successful_poll is None
            created, changed = upsert_jobs(
                connection, company["name"], jobs, company_baseline
            )
            evaluate_company_jobs(connection, company["name"], domain)
            record_poll(connection, company["name"], started_at, True, len(jobs), None)
            total_new += created
            total_changed += changed
            print(
                f"{company['name']}: {len(jobs)} open, {created} new, {changed} changed"
            )
        except Exception as exc:
            failures += 1
            record_poll(connection, company["name"], started_at, False, None, str(exc))
            print(f"{company['name']}: ERROR {exc}")

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
