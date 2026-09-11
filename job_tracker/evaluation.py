from __future__ import annotations

import os
import re

from .llm import evaluate_with_openai


FIT_VALUES = {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}
DEGREE_RANK = {"BS": 1, "MS": 2, "PHD": 3}


def _sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
        if part.strip()
    ]


def _evidence(text: str, terms: list[str], limit: int = 4) -> list[dict]:
    found = []
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if any(term.lower() in lowered for term in terms):
            found.append(
                {"text": sentence[:300], "certainty": "EXPLICIT"}
            )
        if len(found) == limit:
            break
    return found


def _required_evidence(text: str, subject_pattern: str) -> list[dict]:
    found = []
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if not re.search(subject_pattern, lowered):
            continue
        if "preferred" in lowered or "nice to have" in lowered:
            continue
        if re.search(r"\b(required|minimum|must|need to have)\b", lowered):
            found.append({"text": sentence[:300], "certainty": "EXPLICIT"})
    return found


def _hard_eligibility(job: dict, domain: dict) -> tuple[str, list[dict]]:
    description = job.get("description", "")
    text = f"{job.get('location', '')}\n{description}".lower()
    failures = []

    for category, terms in domain["hard_fail_signals"].items():
        for item in _evidence(description, terms, limit=2):
            failures.append({"field": category, **item})

    for item in _required_evidence(description, r"\b(ph\.?d\.?|doctorate)\b"):
        failures.append({"field": "phd_required", **item})
    for item in _required_evidence(
        description, r"\b(publication|publications|published)\b"
    ):
        failures.append({"field": "publications_required", **item})

    if any(
        signal in text for signal in domain["search_scope"]["non_full_time_signals"]
    ):
        failures.append(
            {
                "field": "employment_type",
                "text": "Posting explicitly indicates a non-full-time role.",
                "certainty": "EXPLICIT",
            }
        )

    outside_us = domain["search_scope"]["outside_us_markers"]
    location = job.get("location", "").lower()
    us_marker = re.search(
        r"\b(united states|usa|us|u\.s\.|remote[, -]+us|"
        r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|"
        r"MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|"
        r"RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b",
        job.get("location", ""),
    )
    if any(marker in location for marker in outside_us) and not us_marker:
        failures.append(
            {
                "field": "location",
                "text": job.get("location", "Not listed"),
                "certainty": "EXPLICIT",
            }
        )

    if failures:
        return "FAIL", failures
    return "UNKNOWN", []


def _qualification_paths(description: str) -> list[dict]:
    degree_pattern = (
        r"(?P<degree>bachelor(?:['’]s)?|b\.?s\.?|master(?:['’]s)?|m\.?s\.?|"
        r"ph\.?d\.?|doctorate)"
    )
    years_pattern = r"(?P<years>\d+)\+?\s*(?:years?|yrs?)"
    paths = []
    for sentence in _sentences(description):
        for match in re.finditer(
            degree_pattern + r".{0,100}?" + years_pattern,
            sentence,
            flags=re.IGNORECASE,
        ):
            raw_degree = match.group("degree").lower()
            degree = (
                "PHD"
                if raw_degree.startswith(("ph", "doc"))
                else "MS"
                if raw_degree.startswith(("m", "master"))
                else "BS"
            )
            path = {
                "degree": degree,
                "years": int(match.group("years")),
                "evidence": sentence[:300],
            }
            if path not in paths:
                paths.append(path)
    return paths


def _evaluate_paths(paths: list[dict], candidate: dict) -> tuple[str, str]:
    if not paths:
        return "UNKNOWN", "No explicit degree/experience pathway was parsed."
    degree = candidate.get("degree")
    years = candidate.get("relevant_years")
    if degree not in DEGREE_RANK or years is None:
        return "UNKNOWN", "Qualification pathways found; candidate degree/YOE is incomplete."
    if any(
        DEGREE_RANK[degree] >= DEGREE_RANK[path["degree"]]
        and years >= path["years"]
        for path in paths
    ):
        return "PASS", "Candidate satisfies at least one explicit qualification pathway."
    return "FAIL", "Candidate does not satisfy any parsed qualification pathway."


def _semantic_rules(job: dict, domain: dict) -> tuple[bool, dict]:
    title = job["title"].lower()
    description = job.get("description", "")
    title_plausible = any(
        term in title for term in domain["title_plausibility_signals"]
    )
    seniority_signal = (
        "STRONG_NEGATIVE"
        if re.search(r"\b(staff|principal)\b", title)
        else "NEUTRAL"
    )

    buckets: dict[str, list[dict]] = {}
    bucket_counts: dict[str, int] = {}
    category_counts = {"primary": 0, "secondary": 0, "low_priority": 0}
    archetype_category = {}
    for category, archetypes in domain["role_signals"].items():
        for archetype, terms in archetypes.items():
            hits = _evidence(description, terms)
            if hits:
                buckets[archetype] = hits
                hit_count = sum(
                    term.lower() in description.lower() for term in terms
                )
                bucket_counts[archetype] = hit_count
                category_counts[category] += hit_count
                archetype_category[archetype] = category

    plausible = title_plausible or bool(buckets)
    if buckets:
        max_count = max(bucket_counts.values())
        leaders = [name for name, count in bucket_counts.items() if count == max_count]
        archetype = leaders[0] if len(leaders) == 1 else f"MIXED({', '.join(leaders)})"
        role_evidence = []
        for name in leaders:
            for item in buckets[name]:
                if item not in role_evidence:
                    role_evidence.append(item)
        role_evidence = role_evidence[:4]
        leader_categories = {archetype_category[name] for name in leaders}
        selected_category = (
            next(iter(leader_categories)) if len(leader_categories) == 1 else "mixed"
        )
    else:
        archetype = "UNKNOWN"
        role_evidence = []
        leaders = []
        selected_category = "unknown"

    primary = category_counts["primary"]
    secondary = category_counts["secondary"]
    low = category_counts["low_priority"]
    if primary >= 2 and primary >= low:
        career_fit = "HIGH"
    elif primary or secondary:
        career_fit = "MEDIUM"
    elif low >= 2:
        career_fit = "LOW"
    else:
        career_fit = "UNKNOWN"

    proven = _evidence(description, domain["resume_evidence"]["proven"])
    developing = _evidence(description, domain["resume_evidence"]["developing"])
    if len(proven) >= 2:
        capability_fit = resume_fit = "HIGH"
    elif proven:
        capability_fit = resume_fit = "MEDIUM"
    elif len(developing) >= 2:
        capability_fit = resume_fit = "LOW"
    else:
        capability_fit = resume_fit = "UNKNOWN"

    if selected_category == "primary":
        trajectory_fit = "HIGH" if career_fit == "HIGH" else "MEDIUM"
    elif selected_category == "secondary":
        trajectory_fit = "MEDIUM"
    elif career_fit == "LOW":
        trajectory_fit = "LOW"
    else:
        trajectory_fit = "UNKNOWN"

    result = {
        "role_interpretation": {
            "role_archetype": archetype,
            "seniority_signal": seniority_signal,
            "business_domain": "UNKNOWN",
            "primary_responsibilities": [item["text"] for item in role_evidence],
            "model_ownership": "UNKNOWN",
            "decision_target": "UNKNOWN",
            "ai_ml_centrality": "HIGH" if primary >= 2 else "MEDIUM" if primary else "UNKNOWN",
            "analytics_intensity": "UNKNOWN",
            "engineering_intensity": "HIGH" if len(developing) >= 2 else "UNKNOWN",
            "research_intensity": "HIGH" if "RESEARCH_HEAVY" in leaders else "UNKNOWN",
            "product_business_orientation": "UNKNOWN",
        },
        "career_direction_fit": career_fit,
        "capability_fit": capability_fit,
        "resume_signal_fit": resume_fit,
        "trajectory_fit": trajectory_fit,
        "evidence": {
            "role": role_evidence,
            "resume_proven": proven,
            "resume_developing": developing,
        },
        "uncertainties": [],
    }
    return plausible, result


def _decision(eligibility: str, plausible: bool, result: dict) -> tuple[str, str]:
    if eligibility == "FAIL":
        return "SKIP", "LOW"
    if not plausible:
        return "HOLD", "LOW"
    career = result["career_direction_fit"]
    capability = result["capability_fit"]
    resume = result["resume_signal_fit"]
    trajectory = result["trajectory_fit"]
    if career == "LOW" or trajectory == "LOW":
        return "SAVE", "LOW"
    if (
        eligibility == "PASS"
        and result["role_interpretation"].get("seniority_signal") != "STRONG_NEGATIVE"
        and career == "HIGH"
        and capability in {"HIGH", "MEDIUM"}
        and resume in {"HIGH", "MEDIUM"}
        and trajectory == "HIGH"
    ):
        return "APPLY_TODAY", "HIGH"
    return "REVIEW", "MEDIUM"


def evaluate(job: dict, domain: dict) -> dict:
    eligibility, hard_evidence = _hard_eligibility(job, domain)
    paths = _qualification_paths(job.get("description", ""))
    path_eligibility, path_reason = _evaluate_paths(paths, domain["candidate"])
    if eligibility != "FAIL":
        eligibility = path_eligibility

    plausible, semantic = _semantic_rules(job, domain)
    if (
        eligibility != "FAIL"
        and plausible
        and domain["llm"]["enabled"]
        and os.environ.get("OPENAI_API_KEY")
    ):
        semantic = evaluate_with_openai(job, domain)

    for key in (
        "career_direction_fit",
        "capability_fit",
        "resume_signal_fit",
        "trajectory_fit",
    ):
        if semantic.get(key) not in FIT_VALUES:
            semantic[key] = "UNKNOWN"

    decision, priority = _decision(eligibility, plausible, semantic)
    evidence = {"hard_eligibility": hard_evidence, **semantic.get("evidence", {})}
    role = semantic["role_interpretation"]
    reasons = [f"Eligibility {eligibility}: {path_reason}"]
    if role["role_archetype"] != "UNKNOWN":
        reasons.append(f"Role archetype: {role['role_archetype']}")
    if not plausible:
        reasons.append("No plausible target-role evidence.")

    uncertainties = list(semantic.get("uncertainties", []))
    if domain["candidate"].get("degree") is None:
        uncertainties.append("Candidate degree is not configured.")
    if domain["candidate"].get("relevant_years") is None:
        uncertainties.append("Candidate relevant YOE is not configured.")

    return {
        "eligibility": eligibility,
        "role_archetype": role["role_archetype"],
        "fit": "NOT_COLLAPSED",
        "freshness": "UNKNOWN",
        "priority": priority,
        "score": 0,
        "decision": decision,
        "reasoning": " ".join(reasons),
        "uncertainty": " ".join(dict.fromkeys(uncertainties)),
        "career_direction_fit": semantic["career_direction_fit"],
        "capability_fit": semantic["capability_fit"],
        "resume_signal_fit": semantic["resume_signal_fit"],
        "trajectory_fit": semantic["trajectory_fit"],
        "evidence": evidence,
        "qualification_paths": paths,
        "role_interpretation": role,
    }
