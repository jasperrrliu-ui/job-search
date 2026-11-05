"""Export the Tracker SQLite snapshot after polling for CareerGraph.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def export(db_path: Path, output_dir: Path, run_id: str, poll_started_at: str) -> dict:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        companies = db.execute("SELECT id, name FROM companies WHERE active=1").fetchall()
        health = {}
        for company in companies:
            poll = db.execute("SELECT started_at, success FROM poll_runs WHERE company_id=? "
                              "ORDER BY id DESC LIMIT 1", (company["id"],)).fetchone()
            health[company["name"]] = ("success" if poll and poll["started_at"] >= poll_started_at
                                        and poll["success"] else "failed")
        rows = [dict(row) for row in db.execute(
            "SELECT c.name AS company, c.provider, j.requisition_id, j.source_listing_id, "
            "j.title, j.location, j.official_url, j.description, j.official_created_at, "
            "j.official_updated_at, j.first_seen_at, j.last_seen_at, j.status "
            "FROM jobs j JOIN companies c ON c.id=j.company_id WHERE c.active=1 "
            "ORDER BY c.name, j.requisition_id")]
    finally:
        db.close()
    if not rows or not health:
        raise ValueError("Tracker export requires active companies and job rows")
    folder = output_dir / "exports" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    content = ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)).encode()
    compressed = gzip.compress(content, mtime=0)
    (folder / "jobs.jsonl.gz").write_bytes(compressed)
    manifest = {"run_id": run_id, "exported_at": datetime.now(timezone.utc).isoformat(),
                "poll_started_at": poll_started_at, "jobs_key": f"exports/{run_id}/jobs.jsonl.gz",
                "jobs_sha256": hashlib.sha256(compressed).hexdigest(),
                "job_count": len(rows), "companies": health}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "exports" / "latest.json").write_text(
        json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--db", type=Path, required=True)
    cli.add_argument("--output-dir", type=Path, required=True)
    cli.add_argument("--run-id", required=True)
    cli.add_argument("--poll-started-at", required=True)
    args = cli.parse_args()
    print(json.dumps(export(args.db, args.output_dir, args.run_id, args.poll_started_at), indent=2))


if __name__ == "__main__":
    main()

