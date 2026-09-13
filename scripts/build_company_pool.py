from __future__ import annotations

import argparse
import csv
import html
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


LENNY_URL = "https://www.lennysjobs.com/lenny100"
UNICORN_URL = "https://www.wowls.com/articles/list-all-unicorn-companies-2026"


def public_source(ats: str, ats_url: str) -> dict | None:
    if not ats_url:
        return None
    path = urlparse(ats_url).path.strip("/").split("/")
    if ats == "Greenhouse" and len(path) == 1:
        return {"provider": "greenhouse", "token": path[0]}
    if ats == "Lever" and len(path) == 1:
        return {"provider": "lever", "token": path[0]}
    return None


def key(name: str) -> str:
    aliases = {
        "advancedmicrodevices": "amd",
        "nvidiacorporation": "nvidia",
        "thinkingmachines": "thinkingmachineslab",
        "together": "togetherai",
    }
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return aliases.get(normalized, normalized)


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request) as response:
        return response.read().decode("utf-8")


def lenny_companies() -> list[str]:
    page = fetch(LENNY_URL)
    return [
        html.unescape(name).strip()
        for name in re.findall(
            r'<a class="font-semibold" href="/co/[^"]+">([^<]+)</a>', page
        )
    ]


def unicorn_companies() -> list[str]:
    page = fetch(UNICORN_URL)
    scripts = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        page,
        flags=re.DOTALL,
    )
    for script in scripts:
        data = json.loads(html.unescape(script))
        objects = data if isinstance(data, list) else [data]
        for item_list in objects:
            if item_list.get("@type") == "ItemList":
                return [item["name"] for item in item_list["itemListElement"]]
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="S&P 500 ATS Map CSV")
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--registry", type=Path, default=Path("config/companies.json")
    )
    parser.add_argument(
        "--snapshots", type=Path, default=Path("config/ring1_source_snapshots.json")
    )
    args = parser.parse_args()

    companies: dict[str, dict] = {}

    def add(name: str, source_list: str, fields: dict | None = None) -> None:
        company = companies.setdefault(
            key(name),
            {
                "name": name,
                "lists": [],
                "company_lens": {
                    "employee_threshold": "VERIFY",
                    "us_hiring": "VERIFY",
                    "relevant_role_history": "UNKNOWN",
                },
                "status": "CANDIDATE",
            },
        )
        if source_list not in company["lists"]:
            company["lists"].append(source_list)
        if fields:
            company.update(fields)

    with args.input.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            add(
                row["company"],
                "S&P 500 ATS Map",
                {
                    "ticker": row["ticker"],
                    "sector": row["sector"],
                    "sub_industry": row["sub_industry"],
                    "hq": row["hq"],
                    "domain": row["domain"],
                    "careers_url": row["careers_url"],
                    "detected_ats": row["ats"] or "Unknown",
                    "ats_url": row["ats_url"],
                    "source": public_source(row["ats"], row["ats_url"]),
                },
            )

    snapshots = json.loads(args.snapshots.read_text(encoding="utf-8"))
    for source in snapshots["sources"]:
        for name in source["companies"]:
            add(name, source["name"])

    for name in lenny_companies():
        add(name, "Lenny 100")
    for name in unicorn_companies():
        add(name, "WOWLS Unicorn Companies 2026")

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    for entry in registry:
        fields = {"registry": {"active": entry.get("active", False)}}
        if entry.get("source"):
            fields["registry"]["source"] = entry["source"]
        if entry.get("company_screening"):
            fields["company_lens"] = {
                "employee_threshold": entry["company_screening"],
                "us_hiring": "VERIFY",
                "relevant_role_history": "UNKNOWN",
            }
        if entry.get("active") and entry.get("source"):
            fields["status"] = "ACTIVE"
        add(entry["name"], "Existing registry", fields)

    output = sorted(companies.values(), key=lambda company: company["name"].casefold())
    payload = {
        "policy": "Candidate pool only. Promote after Company Lens screening and ATS source verification.",
        "employee_policy": "Normally require 1,500+ employees; allow smaller companies only as EXCEPTION_HIGH_FIT.",
        "sources": [
            {
                "name": "S&P 500 ATS Map",
                "url": "https://www.atsresumeai.com/research/ats-2026/sp500-ats-map.csv",
            },
            {"name": "Lenny 100", "url": LENNY_URL},
            {"name": "WOWLS Unicorn Companies 2026", "url": UNICORN_URL},
            *[
                {field: source[field] for field in ("name", "url", "scope")}
                for source in snapshots["sources"]
            ],
            {"name": "Existing registry", "url": None},
        ],
        "companies": output,
    }
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
