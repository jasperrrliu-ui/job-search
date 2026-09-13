from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from urllib.parse import urlparse


def public_source(ats: str, ats_url: str) -> dict | None:
    if not ats_url:
        return None
    path = urlparse(ats_url).path.strip("/").split("/")
    if ats == "Greenhouse" and len(path) == 1:
        return {"provider": "greenhouse", "token": path[0]}
    if ats == "Lever" and len(path) == 1:
        return {"provider": "lever", "token": path[0]}
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    with args.input.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    companies = []
    for row in rows:
        candidate = {
            "name": row["company"],
            "ticker": row["ticker"],
            "sector": row["sector"],
            "sub_industry": row["sub_industry"],
            "hq": row["hq"],
            "domain": row["domain"],
            "careers_url": row["careers_url"],
            "detected_ats": row["ats"] or "Unknown",
            "ats_url": row["ats_url"],
            "source": public_source(row["ats"], row["ats_url"]),
            "company_lens": {
                "employee_threshold": "VERIFY",
                "us_hiring": "LIKELY",
                "relevant_role_history": "UNKNOWN",
            },
            "status": "CANDIDATE",
        }
        companies.append(candidate)

    payload = {
        "source": "S&P 500 ATS Map, ATS Resume AI, June 2026",
        "source_url": "https://www.atsresumeai.com/research/ats-2026/sp500-ats-map.csv",
        "policy": "Candidate pool only. Promote after Company Lens screening and source verification.",
        "companies": companies,
    }
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
