from __future__ import annotations

import html
import json
import re
import urllib.request
from datetime import datetime, timezone


USER_AGENT = "job-search/0.1 (contact: jliu_Seeu@outlook.com)"


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
                "official_created_at": job.get("updated_at"),
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
            }
            for job in payload
        ]

    if provider == "workday":
        host = source["host"]
        tenant = source["tenant"]
        site = source["site"]
        list_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        search_terms = [
            "data scientist",
            "data science",
            "applied scientist",
            "quantitative scientist",
            "machine learning",
            "artificial intelligence",
            "ai engineer",
            "model evaluation",
        ]
        postings = {}
        for search_text in search_terms:
            offset = 0
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
                for posting in page:
                    if _target_title(posting["title"], title_rules):
                        postings[posting["externalPath"]] = posting
                offset += len(page)
                if offset >= payload["total"] or not page:
                    break

        jobs = []
        for external_path, posting in postings.items():
            detail = _get_json(
                f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
            )["jobPostingInfo"]
            description = _plain_text(detail.get("jobDescription"))
            if detail.get("timeType"):
                description = f"Employment type: {detail['timeType']}. {description}"
            start_date = detail.get("startDate")
            jobs.append(
                {
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
                }
            )
        return jobs

    raise ValueError(f"Unsupported provider: {provider}")
