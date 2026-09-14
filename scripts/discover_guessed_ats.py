from __future__ import annotations

import argparse
import json
import re
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


HEADERS = {"User-Agent": "job-search/0.1"}
TARGET_TITLE = re.compile(
    r"data scientist|applied scientist|decision scientist|machine learning|"
    r"ml engineer|artificial intelligence|ai engineer|research scientist",
    re.I,
)
SUFFIXES = {"inc", "incorporated", "corporation", "corp", "company", "co", "limited", "ltd", "plc"}


def slugs(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", text)
    trimmed = [word for word in words if word not in SUFFIXES]
    values = ["".join(words), "-".join(words), "".join(trimmed), "-".join(trimmed)]
    return list(dict.fromkeys(value for value in values if len(value) >= 3))


def get_json(url: str):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def company_key(name: str) -> str:
    return "".join(word for word in re.findall(r"[a-z0-9]+", name.casefold()) if word not in SUFFIXES)


def probe(name: str, only_provider: str | None = None) -> tuple[dict, list[str]] | None:
    for token in slugs(name):
        checks = [
            (
                {"provider": "greenhouse", "token": token},
                f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                lambda payload: [job.get("title", "") for job in payload.get("jobs", [])],
            ),
            (
                {"provider": "ashby", "token": token},
                f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                lambda payload: [job.get("title", "") for job in payload.get("jobs", [])],
            ),
            (
                {"provider": "lever", "token": token},
                f"https://api.lever.co/v0/postings/{token}?mode=json&limit=100",
                lambda payload: [job.get("text", "") for job in payload],
            ),
            (
                {"provider": "smartrecruiters", "token": token},
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=0",
                lambda payload: [job.get("name", "") for job in payload.get("content", [])],
            ),
        ]
        for source, url, titles_from in checks:
            if only_provider and source["provider"] != only_provider:
                continue
            try:
                payload = get_json(url)
                titles = titles_from(payload)
                if source["provider"] == "smartrecruiters" and titles:
                    actual = payload["content"][0]["company"]["name"]
                    if company_key(actual) != company_key(name):
                        continue
                if titles:
                    return source, titles
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", type=Path, default=Path("config/company_candidates.json"))
    parser.add_argument("--registry", type=Path, default=Path("config/companies.json"))
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--provider",
        choices=["greenhouse", "ashby", "lever", "smartrecruiters"],
    )
    args = parser.parse_args()

    pool = json.loads(args.pool.read_text(encoding="utf-8"))["companies"]
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    names = {company["name"].casefold() for company in registry}
    sources = {
        json.dumps(company.get("source"), sort_keys=True)
        for company in registry
        if company.get("source")
    }
    candidates = [company for company in pool if company["name"].casefold() not in names]

    found = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(probe, company["name"], args.provider): company
            for company in candidates
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                company = futures[future]
                source, titles = result
                is_large = "S&P 500 ATS Map" in company.get("lists", [])
                detected = company.get("detected_ats", "Unknown").casefold()
                provider_matches = detected in {"unknown", source["provider"]}
                if (is_large and provider_matches) or (
                    not is_large and any(TARGET_TITLE.search(title) for title in titles)
                ):
                    found.append((company, source))
                    print(f"{company['name']}: {source['provider']}", flush=True)

    added = 0
    for company, source in sorted(found, key=lambda item: item[0]["name"]):
        source_key = json.dumps(source, sort_keys=True)
        if source_key in sources:
            continue
        is_large = "S&P 500 ATS Map" in company.get("lists", [])
        registry.append(
            {
                "name": company["name"],
                "priority": "neutral",
                "active": True,
                "company_screening": (
                    "S&P_500_LARGE_EMPLOYER_PROXY" if is_large else "EXCEPTION_HIGH_FIT"
                ),
                "poll_interval_hours": 20,
                "source": source,
            }
        )
        sources.add(source_key)
        added += 1
    args.registry.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Added {added} sources; registry now has {len(registry)} companies")


if __name__ == "__main__":
    main()
