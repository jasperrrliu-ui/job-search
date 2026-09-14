from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            priority TEXT NOT NULL,
            active INTEGER NOT NULL,
            provider TEXT,
            source_token TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            requisition_id TEXT NOT NULL,
            source_listing_id TEXT,
            title TEXT NOT NULL,
            location TEXT NOT NULL,
            official_url TEXT NOT NULL,
            description TEXT NOT NULL,
            official_created_at TEXT,
            official_updated_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            notified_at TEXT,
            UNIQUE(company_id, requisition_id)
        );

        CREATE TABLE IF NOT EXISTS job_snapshots (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            captured_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS evaluations (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            config_version TEXT NOT NULL,
            job_content_hash TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            role_archetype TEXT NOT NULL,
            fit TEXT NOT NULL,
            freshness TEXT NOT NULL,
            priority TEXT NOT NULL,
            score INTEGER NOT NULL,
            decision TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            uncertainty TEXT NOT NULL,
            career_direction_fit TEXT NOT NULL DEFAULT 'UNKNOWN',
            capability_fit TEXT NOT NULL DEFAULT 'UNKNOWN',
            resume_signal_fit TEXT NOT NULL DEFAULT 'UNKNOWN',
            trajectory_fit TEXT NOT NULL DEFAULT 'UNKNOWN',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            qualification_paths_json TEXT NOT NULL DEFAULT '[]',
            role_interpretation_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS poll_runs (
            id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            success INTEGER NOT NULL,
            job_count INTEGER,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            decision TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_deliveries (
            delivery_date TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL,
            job_count INTEGER NOT NULL
        );
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(evaluations)").fetchall()
    }
    additions = {
        "career_direction_fit": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "capability_fit": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "resume_signal_fit": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "trajectory_fit": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
        "qualification_paths_json": "TEXT NOT NULL DEFAULT '[]'",
        "role_interpretation_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE evaluations ADD COLUMN {name} {definition}")
    job_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "source_listing_id" not in job_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN source_listing_id TEXT")
    if "official_updated_at" not in job_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN official_updated_at TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_listing "
        "ON jobs(company_id, source_listing_id) WHERE source_listing_id IS NOT NULL"
    )
    connection.commit()


def sync_companies(connection: sqlite3.Connection, companies: list[dict]) -> None:
    for company in companies:
        source = company.get("source") or {}
        connection.execute(
            """
            INSERT INTO companies(name, priority, active, provider, source_token)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                priority=excluded.priority,
                active=excluded.active,
                provider=excluded.provider,
                source_token=excluded.source_token
            """,
            (
                company["name"],
                company.get("priority", "neutral"),
                bool(company.get("active", True)),
                source.get("provider"),
                source.get("token"),
            ),
        )
    connection.commit()


def upsert_jobs(
    connection: sqlite3.Connection,
    company_name: str,
    jobs: list[dict],
    baseline: bool,
    close_missing: bool = True,
) -> tuple[int, int]:
    company = connection.execute(
        "SELECT id FROM companies WHERE name = ?", (company_name,)
    ).fetchone()
    company_id = company["id"]
    now = utc_now()
    created = 0
    changed = 0
    seen_ids: list[str] = []

    for job in jobs:
        requisition_id = job.get("requisition_id") or hashlib.sha256(
            f"{company_name}:{job['official_url']}".encode()
        ).hexdigest()
        seen_ids.append(requisition_id)
        payload = json.dumps(job, sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.sha256(payload.encode()).hexdigest()
        existing = connection.execute(
            "SELECT id, content_hash FROM jobs WHERE company_id=? AND requisition_id=?",
            (company_id, requisition_id),
        ).fetchone()

        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO jobs(
                    company_id, requisition_id, source_listing_id, title, location, official_url,
                    description, official_created_at, official_updated_at,
                    first_seen_at, last_seen_at,
                    status, content_hash, notified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    company_id,
                    requisition_id,
                    job.get("source_listing_id"),
                    job["title"],
                    job.get("location", ""),
                    job["official_url"],
                    job.get("description", ""),
                    job.get("official_created_at"),
                    job.get("official_updated_at"),
                    now,
                    now,
                    content_hash,
                    now if baseline else None,
                ),
            )
            job_id = cursor.lastrowid
            created += 1
        else:
            job_id = existing["id"]
            if existing["content_hash"] != content_hash:
                changed += 1
            connection.execute(
                """
                UPDATE jobs SET source_listing_id=?, title=?, location=?, official_url=?, description=?,
                    official_created_at=?, official_updated_at=?, last_seen_at=?,
                    status='OPEN', content_hash=?
                WHERE id=?
                """,
                (
                    job.get("source_listing_id"),
                    job["title"],
                    job.get("location", ""),
                    job["official_url"],
                    job.get("description", ""),
                    job.get("official_created_at"),
                    job.get("official_updated_at"),
                    now,
                    content_hash,
                    job_id,
                ),
            )

        if existing is None or existing["content_hash"] != content_hash:
            connection.execute(
                """
                INSERT INTO job_snapshots(job_id, captured_at, content_hash, payload)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, now, content_hash, payload),
            )

    if seen_ids and close_missing:
        placeholders = ",".join("?" for _ in seen_ids)
        connection.execute(
            f"UPDATE jobs SET status='CLOSED' WHERE company_id=? AND requisition_id NOT IN ({placeholders})",
            (company_id, *seen_ids),
        )
    connection.commit()
    return created, changed


def reconcile_workday_discovery(
    connection: sqlite3.Connection, company_name: str, listings: list[dict]
) -> list[dict]:
    company = connection.execute(
        "SELECT id FROM companies WHERE name=?", (company_name,)
    ).fetchone()
    company_id = company["id"]
    existing = connection.execute(
        "SELECT id, requisition_id, source_listing_id, official_url FROM jobs "
        "WHERE company_id=?",
        (company_id,),
    ).fetchall()
    by_listing_id = {
        row["source_listing_id"]: row
        for row in existing
        if row["source_listing_id"] is not None
    }
    now = utc_now()
    new_listings = []
    seen_ids = []

    for listing in listings:
        listing_id = listing["source_listing_id"]
        seen_ids.append(listing_id)
        row = by_listing_id.get(listing_id)
        if row is None:
            row = next(
                (
                    candidate
                    for candidate in existing
                    if candidate["official_url"].endswith(listing_id)
                    or candidate["requisition_id"] in listing_id
                ),
                None,
            )
        if row is None:
            new_listings.append(listing)
            continue
        connection.execute(
            "UPDATE jobs SET source_listing_id=?, title=?, location=?, official_url=?, "
            "last_seen_at=?, status='OPEN' WHERE id=?",
            (
                listing_id,
                listing["title"],
                listing.get("location", ""),
                listing["official_url"],
                now,
                row["id"],
            ),
        )

    if seen_ids:
        placeholders = ",".join("?" for _ in seen_ids)
        connection.execute(
            f"UPDATE jobs SET status='CLOSED' WHERE company_id=? "
            f"AND source_listing_id IS NOT NULL "
            f"AND source_listing_id NOT IN ({placeholders})",
            (company_id, *seen_ids),
        )
    connection.commit()
    return new_listings


def record_poll(
    connection: sqlite3.Connection,
    company_name: str,
    started_at: str,
    success: bool,
    job_count: int | None,
    error: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO poll_runs(company_id, started_at, finished_at, success, job_count, error)
        SELECT id, ?, ?, ?, ?, ? FROM companies WHERE name=?
        """,
        (started_at, utc_now(), success, job_count, error, company_name),
    )
    connection.commit()


def record_feedback(
    connection: sqlite3.Connection,
    job_id: int,
    decision: str,
    reason: str | None,
) -> None:
    connection.execute(
        "INSERT INTO feedback(job_id, decision, reason, created_at) VALUES (?, ?, ?, ?)",
        (job_id, decision, reason, utc_now()),
    )
    connection.commit()


def daily_delivery_exists(connection: sqlite3.Connection, delivery_date: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM daily_deliveries WHERE delivery_date=?", (delivery_date,)
    ).fetchone() is not None


def record_daily_delivery(
    connection: sqlite3.Connection, delivery_date: str, job_count: int
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO daily_deliveries(delivery_date, sent_at, job_count) "
        "VALUES (?, ?, ?)",
        (delivery_date, utc_now(), job_count),
    )
    connection.commit()
