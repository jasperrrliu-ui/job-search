from __future__ import annotations

import argparse
import html
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, urlunparse


HEADERS = {"User-Agent": "job-search/0.1"}
UNICORN_URL = "https://www.wowls.com/articles/list-all-unicorn-companies-2026"
BLOCKED_CANDIDATES = {
    "Ball Corporation",
    "Builders FirstSource",
    "Healthpeak Properties",
}
SUPPORTED_ATS_NAMES = {
    "Workday",
    "Greenhouse",
    "Ashby",
    "Lever",
    "SmartRecruiters",
}


def candidate_urls(candidate: dict) -> list[str]:
    urls = [candidate.get("careers_url"), candidate.get("ats_url")]
    urls = [url for url in urls if url]
    if urls:
        return list(dict.fromkeys(urls))
    domain = candidate.get("domain")
    if domain:
        return [f"https://{domain}/careers", f"https://www.{domain}/careers"]
    return []


def search_result_urls(candidate: dict) -> list[str]:
    if candidate.get("detected_ats") not in {"Phenom People", "iCIMS"}:
        return []
    urls = []
    for url in candidate_urls(candidate):
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        prefix = "/".join(parts[:2]) if len(parts) >= 2 else ""
        path = f"/{prefix}/search-results" if prefix else "/search-results"
        urls.append(
            urlunparse(
                (parsed.scheme, parsed.netloc, path, "", urlencode({"keywords": "data scientist"}), "")
            )
        )
    return list(dict.fromkeys(urls))


def get(url: str) -> tuple[str, str]:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=12) as response:
        return response.geturl(), response.read(1_000_000).decode(errors="ignore")


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**HEADERS, "Content-Type": "application/json", "tz": "America/New_York"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def taleo_payload(keyword: str = "") -> dict:
    return {
        "multilineEnabled": True,
        "sortingSelection": {
            "sortBySelectionParam": "3",
            "ascendingSortingOrder": "false",
        },
        "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": ""}},
        "filterSelectionParam": {"searchFilterSelections": []},
        "pageNo": 1,
    }


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
    if provider == "jobvite_xml":
        page = get(source["feed_url"])[1]
        return "<result>" in page.lower() and "<job>" in page.lower()
    if provider == "taleo_rest":
        payload = post_json(
            source["api_url"] + "?" + urlencode({"lang": "en", "portal": source["portal"]}),
            taleo_payload(),
        )
        return isinstance(payload.get("requisitionList"), list)
    if provider == "successfactors_rss":
        return "<rss" in get(source["feed_url"])[1].lower()
    if provider == "icims_jibe":
        return isinstance(json.loads(get(source["api_url"] + "?page=1&limit=1")[1]).get("jobs"), list)
    if provider == "oracle_hcm":
        url = (
            source["api_base"]
            + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            + "?onlyData=true&expand=requisitionList.workLocation"
            + f"&finder=findReqs;siteNumber={source['site']},limit=1,offset=0"
        )
        item = json.loads(get(url)[1])["items"][0]
        return isinstance(item.get("requisitionList"), list)
    if provider == "eightfold_pcsx":
        query = urlencode(
            {
                "domain": source["domain"],
                "start": 0,
                "num": 1,
                "query": "data scientist",
                "location": "",
            }
        )
        payload = json.loads(get(source["api_url"] + "?" + query)[1])
        return isinstance(payload.get("data", {}).get("positions"), list)
    if provider == "avature_html":
        return "jobdetail" in get(source["search_url"] + "?jobOffset=0")[1].lower()
    if provider == "phenom_html":
        page = get(source["search_url"] + "?keywords=data%20scientist")[1]
        return "phApp.ddo" in page and "eagerLoadRefineSearch" in page
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


def provider_source(candidate: dict) -> dict | None:
    url = next(iter(candidate_urls(candidate)), None)
    if not url:
        return None
    parsed = urlparse(url)
    if candidate.get("detected_ats") == "Jobvite":
        page = get(url)[1]
        match = re.search(r"companyEId\s*:\s*['\"]([^'\"]+)", page)
        if not match:
            return None
        company_id = match.group(1)
        return {
            "provider": "jobvite_xml",
            "feed_url": f"http://app.jobvite.com/CompanyJobs/Xml.aspx?c={company_id}",
        }
    if candidate.get("detected_ats") == "Oracle Taleo":
        _, page = get(url)
        links = re.findall(
            r"https?://[a-z0-9.-]+\.taleo\.net/careersection/[^\"'<> ]+",
            html.unescape(page).replace("\\/", "/"),
            re.I,
        )
        for link in links:
            parsed_link = urlparse(link.rstrip("\\"))
            match = re.search(r"/careersection/([^/]+)/", parsed_link.path)
            if not match:
                continue
            search_url = urlunparse(
                (parsed_link.scheme, parsed_link.netloc, f"/careersection/{match.group(1)}/jobsearch.ftl", "", "lang=en", "")
            )
            search_page = get(search_url)[1]
            portal = re.search(r"portalNo:\s*['\"]([^'\"]+)", search_page)
            if portal:
                root = urlunparse((parsed_link.scheme, parsed_link.netloc, "", "", "", ""))
                return {
                    "provider": "taleo_rest",
                    "api_url": root + "/careersection/rest/jobboard/searchjobs",
                    "career_url": root + f"/careersection/{match.group(1)}",
                    "portal": portal.group(1),
                }
        return None
    if candidate.get("detected_ats") == "iCIMS":
        return {
            "provider": "icims_jibe",
            "api_url": urlunparse((parsed.scheme, parsed.netloc, "/api/jobs", "", "", "")),
            "careers_url": urlunparse((parsed.scheme, parsed.netloc, "/jobs", "", "", "")),
        }
    if candidate.get("detected_ats") == "Oracle HCM Cloud":
        match = re.search(r"/sites/([^/?#]+)", parsed.path)
        if not match:
            return None
        root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        return {
            "provider": "oracle_hcm",
            "api_base": root,
            "site": match.group(1),
            "site_url": urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"/hcmUI/CandidateExperience/en/sites/{match.group(1)}",
                    "",
                    "",
                    "",
                )
            ),
        }
    if candidate.get("detected_ats") == "Eightfold":
        domain = candidate.get("domain")
        if not domain:
            return None
        root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        return {
            "provider": "eightfold_pcsx",
            "api_url": root + "/api/pcsx/search",
            "detail_url": root + "/api/pcsx/position_details",
            "careers_url": root,
            "domain": domain,
        }
    if candidate.get("detected_ats") == "Phenom People":
        search_urls = search_result_urls(candidate)
        if not search_urls:
            return None
        parsed_search = urlparse(search_urls[0])
        return {
            "provider": "phenom_html",
            "search_url": urlunparse(
                (
                    parsed_search.scheme,
                    parsed_search.netloc,
                    parsed_search.path,
                    "",
                    "",
                    "",
                )
            ),
        }
    if candidate.get("detected_ats") != "SAP SuccessFactors":
        return None
    feed_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "/services/rss/job/",
            "",
            urlencode({"locale": "en_US"}),
            "",
        )
    )
    return {"provider": "successfactors_rss", "feed_url": feed_url}


def avature_source(page: str) -> dict | None:
    hosts = list(dict.fromkeys(re.findall(r"https?://([a-z0-9.-]+\.avature\.net)", page, re.I)))
    prefixes = ["/careers", "/ml", "/en_US/careers"]
    for host in hosts:
        for prefix in prefixes:
            source = {
                "provider": "avature_html",
                "search_url": f"https://{host}{prefix}/SearchJobs",
            }
            try:
                if valid_source(source):
                    return source
            except Exception:
                pass
    return None


def discover(candidate: dict) -> tuple[str, dict] | None:
    if candidate.get("source") and valid_source(candidate["source"]):
        return candidate["name"], candidate["source"]
    embedded = candidate.get("registry", {}).get("source")
    if embedded and valid_source(embedded):
        return candidate["name"], embedded
    known = provider_source(candidate)
    if known and valid_source(known):
        return candidate["name"], known
    for url in candidate_urls(candidate) + search_result_urls(candidate):
        try:
            final_url, page = get(url)
            if candidate.get("detected_ats") == "Avature":
                source = avature_source(final_url + " " + page)
                if source:
                    return candidate["name"], source
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
    parser.add_argument("--all-candidates", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("outputs/source-coverage.json"))
    parser.add_argument("--only-provider")
    parser.add_argument("--no-report", action="store_true")
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
        if company["name"].casefold() not in registered
        and company["name"] not in BLOCKED_CANDIDATES
        and (args.all_candidates or "S&P 500 ATS Map" in company.get("lists", []))
        and candidate_urls(company)
        and (not args.only_provider or company.get("detected_ats") == args.only_provider)
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
    if args.no_report:
        print(f"Added {added} sources; registry now has {len(registry)} companies")
        return
    found_by_name = {name.casefold() for name, _ in found}
    coverage = {"verified_active": [], "known_unsupported": [], "needs_official_url": [], "unverified": []}
    for company in pool:
        name = company["name"]
        if name.casefold() in registered or name.casefold() in found_by_name:
            coverage["verified_active"].append(name)
        elif (
            company.get("detected_ats")
            and company["detected_ats"] not in {"Unknown", *SUPPORTED_ATS_NAMES}
        ):
            coverage["known_unsupported"].append({"name": name, "provider": company["detected_ats"]})
        elif not candidate_urls(company):
            coverage["needs_official_url"].append(name)
        else:
            coverage["unverified"].append(name)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(coverage, indent=2) + "\n", encoding="utf-8")
    print(f"Added {added} sources; registry now has {len(registry)} companies")
    print("Coverage: " + ", ".join(f"{key}={len(value)}" for key, value in coverage.items()))


if __name__ == "__main__":
    main()
