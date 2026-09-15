from __future__ import annotations

import argparse
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .database import (
    connect,
    daily_delivery_exists,
    init_schema,
    record_daily_delivery,
    record_feedback,
)
from .mailer import send_digest
from .service import build_digest, load_json, mark_notified, poll_all


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "jobs.db"
COMPANIES = ROOT / "config" / "companies.json"
DOMAIN = ROOT / "config" / "domain.json"
DELIVERY = ROOT / "config" / "delivery.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Target-company job tracker")
    parser.add_argument(
        "command", choices=["bootstrap", "poll", "digest", "email", "feedback"]
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mark-sent", action="store_true")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--job-id", type=int)
    parser.add_argument(
        "--decision",
        choices=["APPLY", "MAYBE", "SKIP", "SAVE", "CONTACT", "INTERVIEW", "REJECTED"],
    )
    parser.add_argument("--reason")
    args = parser.parse_args()

    connection = connect(args.db)
    init_schema(connection)

    if args.command == "feedback":
        record_feedback(connection, args.job_id, args.decision, args.reason)
        print(f"Saved {args.decision} feedback for job {args.job_id}")
        return

    if args.command in {"bootstrap", "poll"}:
        new, changed, failures = poll_all(
            connection,
            COMPANIES,
            DOMAIN,
            baseline=args.command == "bootstrap",
        )
        print(f"Done: {new} new, {changed} changed, {failures} failures")
        return

    if args.command == "email":
        local_now = datetime.now(ZoneInfo("America/New_York"))
        delivery_date = local_now.date().isoformat()
        delivery_slot = "morning" if local_now.hour < 14 else "afternoon"
        delivery_key = f"{delivery_date}:{delivery_slot}"
        if args.daily and not args.force and local_now.hour < 7:
            print(f"Daily email not due yet: {local_now:%H:%M}")
            return
        if args.daily and not args.force and daily_delivery_exists(
            connection, delivery_key
        ):
            print(f"Daily email already sent for {delivery_key}")
            return
        first_seen_since = None
        if args.force:
            local_midnight = datetime.combine(
                local_now.date(), time.min, tzinfo=local_now.tzinfo
            )
            first_seen_since = local_midnight.astimezone(timezone.utc).isoformat()
        digest, job_ids = build_digest(
            connection,
            first_seen_since=first_seen_since,
            include_notified=args.force,
        )
        delivery = load_json(DELIVERY)
        send_digest(delivery["subject"], digest, delivery["recipient"])
        mark_notified(connection, job_ids)
        if args.daily:
            record_daily_delivery(connection, delivery_key, len(job_ids))
        print(f"Sent digest with {len(job_ids)} jobs to {delivery['recipient']}")
        return

    digest, job_ids = build_digest(connection)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(digest, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(digest)
    if args.mark_sent:
        mark_notified(connection, job_ids)
        print(f"Marked {len(job_ids)} jobs as sent")


if __name__ == "__main__":
    main()
