from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from .evaluation import _location_status, _qualification_paths, _research_heavy, _title_has


CAMPUS_SIGNALS = [
    "new grad",
    "new graduate",
    "recent graduate",
    "university graduate",
    "campus hire",
    "campus hiring",
    "campus recruiting",
    "campus program",
    "university hire",
    "university hiring",
    "university recruiting",
    "early career",
    "early careers",
    "entry level",
    "entry-level",
    "new graduate program",
    "graduate development program",
    "students and graduates",
    "2026 graduate",
    "2027 graduate",
    "class of 2026",
    "class of 2027",
]


def _event_date(row: sqlite3.Row) -> str:
    value = row["official_created_at"] or row["first_seen_at"]
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()


def _target_title(title: str, domain: dict) -> bool:
    families = domain["candidate_generation"]["primary_title_families"]
    return any(_title_has(title, term) for terms in families.values() for term in terms)


def _excluded_title(title: str, domain: dict) -> bool:
    seniority = domain["seniority"]
    excluded = seniority["strong_negative_title_terms"] + seniority["senior_title_terms"]
    return any(_title_has(title, term) for term in excluded)


def _campus_signal(title: str, description: str) -> str | None:
    if re.search(r"\bcampus\b", title, flags=re.IGNORECASE):
        return "campus (title)"
    text = f"{title}\n{description}".lower()
    return next((signal for signal in CAMPUS_SIGNALS if signal in text), None)


def _low_experience_signal(title: str, description: str, domain: dict) -> bool:
    if any(
        _title_has(title, term)
        for term in domain["candidate_generation"]["entry_title_terms"]
    ):
        return True
    if any(path["years"] <= 2 for path in _qualification_paths(description)):
        return True
    return re.search(
        r"\b(?:0|1|2)(?:\+|\s*[-–]\s*[0-2])?\s*(?:years?|yrs?)\b.{0,50}\bexperience\b",
        description,
        flags=re.IGNORECASE,
    ) is not None


def build_campus_report(connection: sqlite3.Connection, domain: dict) -> str:
    rows = connection.execute(
        """
        SELECT jobs.*, companies.name AS company
        FROM jobs JOIN companies ON companies.id = jobs.company_id
        ORDER BY companies.name, jobs.official_created_at, jobs.first_seen_at
        """
    ).fetchall()

    target = []
    internships = []
    explicit = []
    compatible = []
    for row in rows:
        title = row["title"]
        text = f"{title}\n{row['description']}".lower()
        if not _target_title(title, domain):
            continue
        if _location_status(row["location"], domain) != "PASS":
            continue
        if _research_heavy(dict(row), domain):
            continue
        target.append(row)
        if re.search(r"\b(intern|internship|co[- ]?op)\b", text):
            internships.append(row)
            continue
        if not _excluded_title(title, domain) and _campus_signal(
            title, row["description"]
        ):
            explicit.append(row)
            continue
        if not _excluded_title(title, domain) and _low_experience_signal(
            title, row["description"], domain
        ):
            compatible.append(row)

    applications = connection.execute(
        "SELECT COUNT(DISTINCT job_id) FROM feedback WHERE decision='APPLY'"
    ).fetchone()[0]
    interviews = connection.execute(
        "SELECT COUNT(DISTINCT job_id) FROM feedback WHERE decision='INTERVIEW'"
    ).fetchone()[0]

    by_company: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in explicit:
        by_company[row["company"]].append(row)

    ranked = sorted(
        by_company.items(),
        key=lambda item: (
            -sum(row["status"] == "OPEN" for row in item[1]),
            -len({_event_date(row) for row in item[1]}),
            -len(item[1]),
            item[0],
        ),
    )

    share = 100 * len(explicit) / len(target) if target else 0
    lines = [
        "Campus / New Grad reverse tracking report",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Scope: US Data Analyst / Data Scientist / Data Engineer / Machine Learning Engineer roles; internships, campus/new-grad, and full-time <=2 YOE tracks.",
        f"All jobs retained in database: {len(rows)}",
        f"Target-title US jobs: {len(target)}",
        f"Internship/co-op jobs: {len(internships)}",
        f"Explicit campus/new-grad jobs: {len(explicit)} ({share:.1f}% of target roles)",
        f"Additional <=2 YOE compatible jobs: {len(compatible)}",
        f"Companies with explicit campus/new-grad evidence: {len(by_company)}",
        "",
        "Company signals",
        "Format: company — explicit roles / currently open / distinct posting dates / observed range",
    ]
    if ranked:
        for company, company_rows in ranked[:30]:
            dates = sorted({_event_date(row) for row in company_rows})
            opened = sum(row["status"] == "OPEN" for row in company_rows)
            lines.append(
                f"- {company} — {len(company_rows)} / {opened} / {len(dates)} / "
                f"{dates[0]} to {dates[-1]}"
            )
    else:
        lines.append("- No explicit campus/new-grad target roles found in the retained database.")

    lines.extend(["", "Internship/co-op roles"])
    for row in sorted(internships, key=_event_date, reverse=True)[:75]:
        lines.extend(
            [
                f"- {_event_date(row)} | {row['company']} | {row['title']} | {row['status']}",
                f"  {row['official_url']}",
            ]
        )

    lines.extend(["", "Explicit campus/new-grad roles"])
    for row in sorted(explicit, key=_event_date, reverse=True)[:75]:
        matched = _campus_signal(row["title"], row["description"])
        lines.extend(
            [
                f"- {_event_date(row)} | {row['company']} | {row['title']} | {row['status']}",
                f"  Signal: {matched}",
                f"  {row['official_url']}",
            ]
        )

    lines.extend(["", "Observed application funnel"])
    if applications:
        lines.append(
            f"Applications recorded: {applications}; interviews recorded: {interviews}; "
            f"observed interview conversion: {100 * interviews / applications:.1f}%"
        )
    else:
        lines.append(
            "No APPLY feedback is recorded yet, so response probability cannot be estimated from your own outcomes."
        )
    lines.extend(
        [
            "",
            "Interpretation",
            "- Explicit campus/new-grad share measures supply, not your probability of receiving an interview.",
            "- A company becomes a strong recurring-campus signal when it appears on multiple distinct posting dates.",
            "- Record APPLY, INTERVIEW, and REJECTED feedback to estimate your personal conversion rate over time.",
        ]
    )
    return "\n".join(lines)
