from __future__ import annotations

import html
import json
import re
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlencode
from xml.etree import ElementTree
from urllib.parse import urljoin


USER_AGENT = "job-search/0.1 (contact: jliu_Seeu@outlook.com)"
ATS_SEARCH_TERMS = [
    "data analyst",
    "data scientist",
    "data science",
    "data engineer",
    "data engineering",
    "machine learning engineer",
    "ml engineer",
    "ai scientist",
    "machine learning scientist",
    "applied scientist",
    "decision scientist",
    "statistician",
    "biostatistician",
    "marketing scientist",
    "model evaluation",
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


def _taleo_payload(keyword: str, page: int) -> dict:
    return {
        "multilineEnabled": True,
        "sortingSelection": {
            "sortBySelectionParam": "3",
            "ascendingSortingOrder": "false",
        },
        "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": ""}},
        "filterSelectionParam": {"searchFilterSelections": []},
        "pageNo": page,
    }


def _get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def _post_form_text(url: str, fields: dict) -> str:
    request = urllib.request.Request(
        url,
        data=urlencode(fields).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode(errors="ignore")


def _phenom_jobs(page: str) -> list[dict]:
    marker = "phApp.ddo ="
    start = page.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = page.find("</script>", start)
    payload, _ = json.JSONDecoder().raw_decode(html.unescape(page[start:end]).lstrip())
    return payload.get("eagerLoadRefineSearch", {}).get("data", {}).get("jobs", [])


def _plain_text(value: str | None) -> str:
    value = value or ""
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _iso_from_millis(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _iso_from_epoch(value: int | str | None) -> str | None:
    if not value:
        return None
    number = int(value)
    if number > 10_000_000_000:
        number //= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()


def _target_title(title: str, title_rules: dict | None) -> bool:
    if not title_rules:
        return True
    families = title_rules["primary_title_families"] | title_rules[
        "secondary_title_families"
    ]
    return any(
        re.search(rf"\b{re.escape(term)}\b", title, flags=re.IGNORECASE)
        for terms in families.values()
        for term in terms
    )


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

    for search_text in ATS_SEARCH_TERMS:
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

    if provider == "successfactors_rss":
        jobs = []
        for search_text in ATS_SEARCH_TERMS:
            feed_url = source["feed_url"]
            separator = "&" if "?" in feed_url else "?"
            page = _get_text(f"{feed_url}{separator}keywords={search_text.replace(' ', '%20')}")
            root = ElementTree.fromstring(page)
            for item in root.findall("./channel/item"):
                title = _plain_text(item.findtext("title"))
                if not _target_title(title, title_rules):
                    continue
                url = item.findtext("link") or ""
                published = item.findtext("pubDate")
                jobs.append(
                    {
                        "requisition_id": url or title,
                        "title": title,
                        "location": title.rsplit("(", 1)[-1].rstrip(")") if "(" in title else "",
                        "official_url": url,
                        "description": _plain_text(item.findtext("description")),
                        "official_created_at": parsedate_to_datetime(published).isoformat() if published else None,
                        "official_updated_at": None,
                    }
                )
        return list({job["requisition_id"]: job for job in jobs}.values())

    if provider == "jobvite_xml":
        root = ElementTree.fromstring(_get_text(source["feed_url"]))
        jobs = []
        for item in root.findall("./job"):
            title = _plain_text(item.findtext("title"))
            if not _target_title(title, title_rules):
                continue
            posted = item.findtext("date")
            jobs.append(
                {
                    "requisition_id": item.findtext("requisitionid") or item.findtext("id"),
                    "title": title,
                    "location": _plain_text(item.findtext("location")),
                    "official_url": item.findtext("detail-url") or item.findtext("apply-url"),
                    "description": _plain_text(item.findtext("description")),
                    "official_created_at": datetime.strptime(posted, "%m/%d/%Y").replace(tzinfo=timezone.utc).isoformat() if posted else None,
                    "official_updated_at": None,
                }
            )
        return jobs

    if provider == "taleo_rest":
        jobs = []
        endpoint = source["api_url"] + "?" + urlencode(
            {"lang": "en", "portal": source["portal"]}
        )
        for search_text in ATS_SEARCH_TERMS:
            page_number = 1
            while True:
                payload = _post_json(endpoint, _taleo_payload(search_text, page_number))
                postings = payload.get("requisitionList", [])
                for posting in postings:
                    columns = posting.get("column", [])
                    title = columns[0] if columns else ""
                    if not _target_title(title, title_rules):
                        continue
                    requisition = posting.get("contestNo") or posting.get("jobId")
                    detail_url = source["career_url"] + "/jobdetail.ftl?" + urlencode(
                        {"job": requisition, "lang": "en"}
                    )
                    detail_page = _get_text(detail_url)
                    locations = columns[2] if len(columns) > 2 else ""
                    jobs.append(
                        {
                            "requisition_id": str(requisition),
                            "title": title,
                            "location": _plain_text(locations.replace('["', '').replace('"]', '')),
                            "official_url": detail_url,
                            "description": _plain_text(unquote(detail_page)),
                            "official_created_at": None,
                            "official_updated_at": None,
                        }
                    )
                paging = payload.get("pagingData", {})
                if page_number * paging.get("pageSize", 25) >= paging.get("totalCount", 0):
                    break
                page_number += 1
        return list({job["requisition_id"]: job for job in jobs}.values())

    if provider == "icims_jibe":
        jobs = []
        for search_text in ATS_SEARCH_TERMS:
            payload = _get_json(
                source["api_url"]
                + "?"
                + urlencode({"keywords": search_text, "page": 1, "limit": 100})
            )
            for item in payload.get("jobs", []):
                data = item.get("data", item)
                title = data.get("title", "")
                if not _target_title(title, title_rules):
                    continue
                requisition = str(data.get("req_id") or data.get("slug"))
                location = data.get("location") or ", ".join(
                    str(data[key]) for key in ("city", "state", "country") if data.get(key)
                )
                jobs.append(
                    {
                        "requisition_id": requisition,
                        "title": title,
                        "location": location,
                        "official_url": f"{source['careers_url']}/{data.get('slug')}?lang={data.get('language', 'en-us')}",
                        "description": _plain_text(data.get("description")),
                        "official_created_at": data.get("posted_date") or data.get("postedDate"),
                        "official_updated_at": data.get("updated_date") or data.get("updatedDate"),
                    }
                )
        return list({job["requisition_id"]: job for job in jobs}.values())

    if provider == "oracle_hcm":
        jobs = []
        for search_text in ATS_SEARCH_TERMS:
            find_params = (
                f"siteNumber={source['site']},limit=100,offset=0,keyword={search_text}"
            )
            url = (
                source["api_base"]
                + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                + "?onlyData=true"
                + "&expand=requisitionList.workLocation,requisitionList.otherWorkLocations,"
                + "requisitionList.secondaryLocations"
                + "&finder=findReqs;"
                + quote(find_params, safe="=,")
            )
            payload = _get_json(url)
            for posting in payload["items"][0].get("requisitionList", []):
                title = posting.get("Title", "")
                if not _target_title(title, title_rules):
                    continue
                description = " ".join(
                    str(posting.get(field) or "")
                    for field in (
                        "ShortDescriptionStr",
                        "ExternalResponsibilitiesStr",
                        "ExternalQualificationsStr",
                    )
                )
                requisition = str(posting["Id"])
                jobs.append(
                    {
                        "requisition_id": requisition,
                        "title": title,
                        "location": posting.get("PrimaryLocation", ""),
                        "official_url": f"{source['site_url']}/job/{requisition}",
                        "description": _plain_text(description),
                        "official_created_at": posting.get("PostedDate"),
                        "official_updated_at": None,
                    }
                )
        return list({job["requisition_id"]: job for job in jobs}.values())

    if provider == "eightfold_pcsx":
        jobs = []
        for search_text in ATS_SEARCH_TERMS:
            start = 0
            while True:
                query = urlencode(
                    {
                        "domain": source["domain"],
                        "start": start,
                        "num": 10,
                        "query": search_text,
                        "location": "",
                    }
                )
                payload = _get_json(source["api_url"] + "?" + query)
                positions = payload.get("data", {}).get("positions", [])
                for posting in positions:
                    title = posting.get("name") or posting.get("title") or ""
                    if not _target_title(title, title_rules):
                        continue
                    position_id = str(posting["id"])
                    detail = _get_json(
                        source["detail_url"]
                        + "?"
                        + urlencode(
                            {"position_id": position_id, "domain": source["domain"]}
                        )
                    ).get("data", {})
                    locations = posting.get("standardizedLocations") or posting.get("locations") or []
                    jobs.append(
                        {
                            "requisition_id": str(
                                posting.get("displayJobId")
                                or posting.get("atsJobId")
                                or position_id
                            ),
                            "title": title,
                            "location": "; ".join(
                                item if isinstance(item, str) else item.get("name", "")
                                for item in locations
                            ),
                            "official_url": source["careers_url"]
                            + (posting.get("positionUrl") or f"/careers/job/{position_id}"),
                            "description": _plain_text(
                                detail.get("jobDescription")
                                or posting.get("jobDescription")
                                or posting.get("job_description")
                            ),
                            "official_created_at": _iso_from_epoch(
                                posting.get("postedTs") or posting.get("creationTs")
                            ),
                            "official_updated_at": _iso_from_epoch(posting.get("t_update")),
                        }
                    )
                start += len(positions)
                count = payload.get("data", {}).get("count", 0)
                if not positions or start >= count or start >= 2000:
                    break
        return list({job["requisition_id"]: job for job in jobs}.values())

    if provider == "avature_html":
        links = {}
        for search_text in ATS_SEARCH_TERMS:
            for offset in range(0, 300, 6):
                page = _post_form_text(
                    source["search_url"] + f"?jobOffset={offset}",
                    {"search": search_text, "action": "search"},
                )
                found = 0
                for match in re.finditer(
                    r'<a[^>]+href=["\'](?P<url>[^"\']*JobDetail[^"\']*)["\'][^>]*>(?P<title>.*?)</a>',
                    page,
                    re.I | re.S,
                ):
                    url = urljoin(source["search_url"], html.unescape(match.group("url")))
                    title = _plain_text(match.group("title"))
                    if _target_title(title, title_rules):
                        links[url] = title
                    found += 1
                if found == 0:
                    break
        jobs = []
        for url, fallback_title in links.items():
            detail_page = _get_text(url)
            scripts = re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                detail_page,
                re.I | re.S,
            )
            data = next(
                (
                    json.loads(html.unescape(script))
                    for script in scripts
                    if '"@type"' in script and "JobPosting" in script
                ),
                {},
            )
            location_data = data.get("jobLocation") or []
            if isinstance(location_data, dict):
                location_data = [location_data]
            locations = []
            for item in location_data:
                address = item.get("address", {})
                locations.append(
                    ", ".join(
                        str(address.get(key))
                        for key in ("addressLocality", "addressRegion", "addressCountry")
                        if address.get(key)
                    )
                )
            jobs.append(
                {
                    "requisition_id": url.rstrip("/").split("/")[-1].split("?")[0],
                    "title": data.get("title") or fallback_title,
                    "location": "; ".join(filter(None, locations)),
                    "official_url": url,
                    "description": _plain_text(data.get("description") or detail_page),
                    "official_created_at": data.get("datePosted"),
                    "official_updated_at": None,
                }
            )
        return jobs

    if provider == "phenom_html":
        jobs = []
        for search_text in ATS_SEARCH_TERMS:
            for offset in range(0, 300, 10):
                page = _get_text(
                    source["search_url"]
                    + "?"
                    + urlencode({"keywords": search_text, "from": offset, "s": 1})
                )
                postings = _phenom_jobs(page)
                for posting in postings:
                    title = posting.get("title", "")
                    if not _target_title(title, title_rules):
                        continue
                    jobs.append(
                        {
                            "requisition_id": str(posting.get("reqId") or posting.get("jobId")),
                            "title": title,
                            "location": posting.get("location")
                            or posting.get("cityStateCountry")
                            or "",
                            "official_url": posting.get("applyUrl") or source["search_url"],
                            "description": _plain_text(
                                posting.get("descriptionTeaser")
                                or posting.get("ml_job_parser", {}).get("descriptionTeaser_ats")
                            ),
                            "official_created_at": posting.get("postedDate")
                            or posting.get("dateCreated"),
                            "official_updated_at": None,
                        }
                    )
                if len(postings) < 10:
                    break
        return list({job["requisition_id"]: job for job in jobs}.values())

    if provider == "workday":
        raise ValueError("Workday must use incremental discovery")

    raise ValueError(f"Unsupported provider: {provider}")
