from __future__ import annotations

import argparse
import html
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse


HEADERS = {"User-Agent": "job-search/0.1"}
UNICORN_URL = "https://www.wowls.com/articles/list-all-unicorn-companies-2026"
BLOCKED_CANDIDATES = {
    "Ball Corporation",
    "Builders FirstSource",
    "Healthpeak Properties",
}


def get(url: str) -> tuple[str, str]:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=12) as response:
        return response.geturl(), response.read(1_000_000).decode(errors="ignore")


def source_from_text(text: str) -> dict | None:
    text = html.unescape(text).replace("\\/", "/")

    match = re.search(
        r"https?://([a-z0-9.-]+\.myworkdayjobs\.com)/([^\"'<> ]+)", text, re.I
    )
    if match:
        host = match.group(1)
        parts = match.group(2).split("?")[0].strip("/").split("/")
        if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            parts.pop(0)
        if parts and parts[0] != "wday":
            site = parts[0]
            return {
                "provider": "workday",
                "token": site,
                "host": host,
                "tenant": host.split(".")[0],
                "site": site,
            }

    greenhouse = re.search(
        r"https?://boards\.greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)",
        text,
        re.I,
    )
    if greenhouse:
        return {"provider": "greenhouse", "token": greenhouse.group(1)}

    patterns = [
        ("greenhouse", r"https?://job-boards\.greenhouse\.io/([a-z0-9_-]+)"),
        ("greenhouse", r"https?://boards\.greenhouse\.io/([a-z0-9_-]+)"),
        ("ashby", r"https?://jobs\.ashbyhq\.com/([a-z0-9_-]+)"),
        ("lever", r"https?://jobs\.lever\.co/([a-z0-9_-]+)"),
        ("smartrecruiters", r"https?://(?:careers|jobs)\.smartrecruiters\.com/([a-z0-9_-]+)"),
    ]
    for provider, pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return {"provider": provider, "token": match.group(1)}
    return None


def valid_source(source: dict) -> bool:
    provider = source["provider"]
    token = source["token"]
    if provider == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        return isinstance(json.loads(get(url)[1]).get("jobs"), list)
    if provider == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
        return isinstance(json.loads(get(url)[1]).get("jobs"), list)
    if provider == "lever":
        url = f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1"
        return isinstance(json.loads(get(url)[1]), list)
    if provider == "smartrecruiters":
        url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1&offset=0"
        return isinstance(json.loads(get(url)[1]).get("content"), list)
    if provider == "workday":
        url = (
            f"https://{source['host']}/wday/cxs/{source['tenant']}/"
            f"{source['site']}/jobs"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
            ).encode(),
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            return isinstance(json.load(response).get("jobPostings"), list)
    return False


def discover(candidate: dict) -> tuple[str, dict] | None:
    if candidate.get("source") and valid_source(candidate["source"]):
        return candidate["name"], candidate["source"]
    url = candidate.get("careers_url")
    if not url:
        return None
    final_url, page = get(url)
    source = source_from_text(final_url + " " + page)
    if source and valid_source(source):
        return candidate["name"], source

    links = [urljoin(final_url, "/careers"), urljoin(final_url, "/jobs")]
    links += [
        urljoin(final_url, link)
        for link in re.findall(r'href=["\']([^"\']+)["\']', page, re.I)
        if re.search(r"job|career|position|opening", link, re.I)
    ]
    links.sort(
        key=lambda link: 0
        if re.search(
            r"greenhouse|ashbyhq|lever|myworkdayjobs|smartrecruiters",
            link,
            re.I,
        )
        else 1
    )
    for link in list(dict.fromkeys(links))[:5]:
        try:
            linked_url, linked_page = get(link)
            source = source_from_text(linked_url + " " + linked_page)
            if source and valid_source(source):
                return candidate["name"], source
        except Exception:
            pass
    return None


def unicorn_profile(item: dict) -> dict | None:
    _, page = get(item["url"])
    scripts = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S
    )
    organizations = []
    for script in scripts:
        data = json.loads(html.unescape(script))
        organizations.extend(data if isinstance(data, list) else [data])
    organization = next(
        (item for item in organizations if item.get("@type") == "Organization"), None
    )
    if not organization or not organization.get("url"):
        return None
    employees = str(organization.get("numberOfEmployees", {}).get("value", ""))
    numbers = [int(number.replace(",", "")) for number in re.findall(r"[\d,]+", employees)]
    if not numbers or numbers[0] < 1500:
        return None
    high_fit = bool(
        re.search(
            r"artificial intelligence|machine learning|generative ai|data infrastructure|healthtech|health technology",
            page,
            re.I,
        )
    )
    if numbers and numbers[0] >= 1500:
        screening = "PASS_1500_PLUS"
    elif high_fit:
        screening = "EXCEPTION_HIGH_FIT"
    else:
        return None
    return {
        "name": item["name"],
        "careers_url": organization["url"],
        "employee_range": employees,
        "company_screening": screening,
    }


def large_unicorn_candidates(workers: int) -> list[dict]:
    _, page = get(UNICORN_URL)
    scripts = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S
    )
    data = json.loads(html.unescape(scripts[0]))
    item_list = next(item for item in data if item.get("@type") == "ItemList")
    items = item_list["itemListElement"]
    candidates = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(unicorn_profile, item) for item in items]
        for future in as_completed(futures):
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception:
                pass
    print(f"Large unicorn profiles: {len(candidates)}", flush=True)
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", type=Path, default=Path("config/company_candidates.json"))
    parser.add_argument("--registry", type=Path, default=Path("config/companies.json"))
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--include-unicorns", action="store_true")
    args = parser.parse_args()

    pool = json.loads(args.pool.read_text(encoding="utf-8"))["companies"]
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    registered = {company["name"].casefold() for company in registry}
    registered_sources = {
        json.dumps(company.get("source"), sort_keys=True)
        for company in registry
        if company.get("source")
    }
    candidates = [
        company
        for company in pool
        if "S&P 500 ATS Map" in company.get("lists", [])
        and company["name"].casefold() not in registered
        and company["name"] not in BLOCKED_CANDIDATES
    ]
    if args.include_unicorns:
        candidates.extend(
            candidate
            for candidate in large_unicorn_candidates(args.workers)
            if candidate["name"].casefold() not in registered
        )

    found = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(discover, company): company for company in candidates}
        for future in as_completed(futures):
            company = futures[future]
            try:
                result = future.result()
                if result:
                    found.append(result)
                    print(f"{result[0]}: {result[1]['provider']}", flush=True)
            except Exception as exc:
                print(f"{company['name']}: ERROR {exc}", flush=True)

    added = 0
    candidate_by_name = {candidate["name"]: candidate for candidate in candidates}
    for name, source in sorted(found):
        source_key = json.dumps(source, sort_keys=True)
        if source_key in registered_sources:
            continue
        registry.append(
            {
                "name": name,
                "priority": "neutral",
                "active": True,
                "company_screening": candidate_by_name[name].get(
                    "company_screening", "S&P_500_LARGE_EMPLOYER_PROXY"
                ),
                "poll_interval_hours": 20,
                "source": source,
            }
        )
        if candidate_by_name[name].get("employee_range"):
            registry[-1]["employee_range"] = candidate_by_name[name]["employee_range"]
        registered_sources.add(source_key)
        added += 1
    args.registry.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Added {added} sources; registry now has {len(registry)} companies")


if __name__ == "__main__":
    main()
