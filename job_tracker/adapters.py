from __future__ import annotations

import html
import json
import re
import urllib.request
from datetime import datetime, timezone


USER_AGENT = "job-search/0.1 (contact: jliu_Seeu@outlook.com)"
WORKDAY_SEARCH_TERMS = [
    "data scientist",
    "ai scientist",
    "machine learning scientist",
    "applied scientist",
]


def _get_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def _plain_text(value: str | None) -> str:
    value = value or ""
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _iso_from_millis(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _target_title(title: str, title_rules: dict | None) -> bool:
    if not title_rules:
        return True
    lowered = title.lower()
    families = title_rules["primary_title_families"] | title_rules[
        "secondary_title_families"
    ]
    return any(term in lowered for terms in families.values() for term in terms)


def discover_workday_jobs(
    company: dict, title_rules: dict | None = None
) -> list[dict]:
    print(f"{company['name']}: polling started", flush=True)
    source = company["source"]
    host = source["host"]
    tenant = source["tenant"]
    site = source["site"]
    list_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    postings = {}

    for search_text in WORKDAY_SEARCH_TERMS:
        offset = 0
        pages = 0
        while True:
            payload = _post_json(
                list_url,
                {
                    "appliedFacets": {},
                    "limit": 20,
                    "offset": offset,
                    "searchText": search_text,
                },
            )
            page = payload["jobPostings"]
            pages += 1
            for posting in page:
                if _target_title(posting["title"], title_rules):
                    postings[posting["externalPath"]] = posting
            offset += len(page)
            if offset >= payload["total"] or not page:
                break
        print(
            f"{company['name']}: Workday '{search_text}' {pages} pages",
            flush=True,
        )

    print(
        f"{company['name']}: Workday {len(postings)} unique target postings",
        flush=True,
    )
    return list(postings.values())


def workday_listing_job(company: dict, posting: dict) -> dict:
    source = company["source"]
    external_path = posting["externalPath"]
    return {
        "source_listing_id": external_path,
        "requisition_id": external_path,
        "title": posting["title"],
        "location": posting.get("locationsText", ""),
        "official_url": f"https://{source['host']}/{source['site']}{external_path}",
        "description": "",
        "official_created_at": None,
        "official_updated_at": None,
    }


def enrich_workday_jobs(company: dict, postings: list[dict]) -> list[dict]:
    source = company["source"]
    host = source["host"]
    tenant = source["tenant"]
    site = source["site"]
    jobs = []

    for posting in postings:
        external_path = posting["externalPath"]
        detail = _get_json(
            f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
        )["jobPostingInfo"]
        description = _plain_text(detail.get("jobDescription"))
        if detail.get("timeType"):
            description = f"Employment type: {detail['timeType']}. {description}"
        start_date = detail.get("startDate")
        jobs.append(
            {
                "source_listing_id": external_path,
                "requisition_id": str(
                    detail.get("jobReqId") or detail.get("id") or external_path
                ),
                "title": detail.get("title") or posting["title"],
                "location": detail.get("location")
                or posting.get("locationsText", ""),
                "official_url": detail.get("externalUrl")
                or f"https://{host}/{site}{external_path}",
                "description": description,
                "official_created_at": (
                    f"{start_date}T00:00:00+00:00" if start_date else None
                ),
                "official_updated_at": None,
            }
        )
    return jobs


def fetch_jobs(company: dict, title_rules: dict | None = None) -> list[dict]:
    source = company["source"]
    provider = source["provider"].lower()
    token = source.get("token")

    if provider == "greenhouse":
        payload = _get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        )
        return [
            {
                "requisition_id": str(job["id"]),
                "title": job["title"],
                "location": (job.get("location") or {}).get("name", ""),
                "official_url": job["absolute_url"],
                "description": _plain_text(job.get("content")),
                "official_created_at": None,
                "official_updated_at": job.get("updated_at"),
            }
            for job in payload["jobs"]
        ]

    if provider == "greenhouse_html":
        board_url = f"https://job-boards.greenhouse.io/{token}"
        page = _get_text(board_url)
        pattern = re.compile(
            r'<a href="(?P<url>https://job-boards\.greenhouse\.io/[^\"]+/jobs/(?P<id>\d+))"[^>]*>'
            r'.*?<p class="body body--medium">(?P<title>.*?)</p>'
            r'<p class="body body__secondary body--metadata">(?P<location>.*?)</p>',
            re.DOTALL,
        )
        return [
            {
                "requisition_id": match.group("id"),
                "title": _plain_text(match.group("title")),
                "location": _plain_text(match.group("location")),
                "official_url": match.group("url"),
                "description": "",
                "official_created_at": None,
                "official_updated_at": None,
            }
            for match in pattern.finditer(page)
        ]

    if provider == "ashby":
        payload = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        return [
            {
                "requisition_id": str(job.get("id") or job.get("jobUrl")),
                "title": job["title"],
                "location": job.get("location", ""),
                "official_url": job.get("jobUrl") or job.get("applyUrl"),
                "description": _plain_text(
                    job.get("descriptionHtml") or job.get("descriptionPlain")
                ),
                "official_created_at": job.get("publishedAt"),
                "official_updated_at": job.get("updatedAt"),
            }
            for job in payload["jobs"]
        ]

    if provider == "lever":
        payload = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
        return [
            {
                "requisition_id": str(job["id"]),
                "title": job["text"],
                "location": (job.get("categories") or {}).get("location", ""),
                "official_url": job["hostedUrl"],
                "description": _plain_text(
                    job.get("descriptionPlain") or job.get("description")
                ),
                "official_created_at": _iso_from_millis(job.get("createdAt")),
                "official_updated_at": _iso_from_millis(job.get("updatedAt")),
            }
            for job in payload
        ]

    if provider == "smartrecruiters":
        postings = []
        offset = 0
        while True:
            payload = _get_json(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
                f"?limit=100&offset={offset}"
            )
            postings.extend(payload["content"])
            offset += len(payload["content"])
            if offset >= payload["totalFound"] or not payload["content"]:
                break
        jobs = []
        for posting in postings:
            if not _target_title(posting["name"], title_rules):
                continue
            detail = _get_json(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings/"
                f"{posting['id']}"
            )
            sections = detail.get("jobAd", {}).get("sections", {})
            description = " ".join(
                section.get("text", "") for section in sections.values()
            )
            jobs.append(
                {
                    "requisition_id": str(posting["id"]),
                    "title": posting["name"],
                    "location": posting.get("location", {}).get("fullLocation", ""),
                    "official_url": detail.get("postingUrl") or posting.get("ref"),
                    "description": _plain_text(description),
                    "official_created_at": posting.get("releasedDate"),
                    "official_updated_at": None,
                }
            )
        return jobs

    if provider == "workday":
        raise ValueError("Workday must use incremental discovery")

    raise ValueError(f"Unsupported provider: {provider}")
