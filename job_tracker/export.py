"""Export the Tracker SQLite snapshot for CareerGraph after a poll."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def export(db_path: Path, output_dir: Path, run_id: str, poll_started_at: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("run_id may contain only letters, digits, underscores, and hyphens")
    datetime.fromisoformat(poll_started_at.replace("Z", "+00:00"))
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    db = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        companies = db.execute(
            "SELECT id, name FROM companies WHERE active=1 ORDER BY name"
        ).fetchall()
        if not companies:
            raise ValueError("Tracker has no active companies")
        health = {}
        poll_details = {}
        for company in companies:
            latest = db.execute(
                "SELECT started_at, success, error FROM poll_runs "
                "WHERE company_id=? ORDER BY id DESC LIMIT 1",
                (company["id"],),
            ).fetchone()
            health[company["name"]] = (
                "success" if latest and latest["success"] else "failed"
            )
            poll_details[company["name"]] = {
                "started_at": latest["started_at"] if latest else None,
                "success": bool(latest["success"]) if latest else False,
                "error": latest["error"] if latest else None,
            }
        rows = [
            dict(row) for row in db.execute(
                "SELECT c.name AS company, c.provider, j.requisition_id, "
                "j.source_listing_id, j.title, j.location, j.official_url, "
                "j.description, j.official_created_at, j.official_updated_at, "
                "j.first_seen_at, j.last_seen_at, j.status "
                "FROM jobs j JOIN companies c ON c.id=j.company_id "
                "WHERE c.active=1 ORDER BY c.name, j.requisition_id"
            )
        ]
    finally:
        db.close()
    if not rows:
        raise ValueError("Tracker has no job rows for active companies")

    output_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = output_dir / "jobs.jsonl.gz"
    manifest_path = output_dir / "manifest.json"
    latest_path = output_dir / "latest.json"
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    compressed = gzip.compress(payload, mtime=0)
    jobs_path.write_bytes(compressed)
    manifest = {
        "run_id": run_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "poll_started_at": poll_started_at,
        "jobs_key": f"exports/{run_id}/jobs.jsonl.gz",
        "jobs_sha256": hashlib.sha256(compressed).hexdigest(),
        "job_count": len(rows),
        "companies": health,
        "polls": poll_details,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    latest_path.write_text(json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--poll-started-at", required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.db, args.output_dir, args.run_id, args.poll_started_at), indent=2))


if __name__ == "__main__":
    main()
