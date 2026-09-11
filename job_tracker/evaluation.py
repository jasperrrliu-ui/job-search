from __future__ import annotations

import os

from .llm import evaluate_with_openai


def evaluate(job: dict, domain: dict) -> dict:
    title = job["title"].lower()
    description = job.get("description", "").lower()

    title_hits = [term for term in domain["target_titles"] if term.lower() in title]
    signal_hits = [
        term for term in domain["positive_signals"] if term.lower() in description
    ]

    if not title_hits:
        result = {
            "eligibility": "UNKNOWN",
            "role_archetype": "UNKNOWN",
            "fit": "UNKNOWN",
            "freshness": "UNKNOWN",
            "priority": "UNKNOWN",
            "score": 0,
            "decision": "HOLD",
            "reasoning": "No current title match; domain knowledge is incomplete.",
            "uncertainty": "Target-role knowledge is incomplete.",
        }
        return result

    score = min(100, 50 + len(title_hits) * 15 + len(signal_hits) * 5)
    decision = (
        "APPLY_TODAY"
        if score >= domain["action_thresholds"]["apply_today"]
        else "REVIEW"
    )
    reason = f"Title match: {', '.join(title_hits)}"
    if signal_hits:
        reason += f"; JD signals: {', '.join(signal_hits[:3])}"
    result = {
        "eligibility": "UNKNOWN",
        "role_archetype": "UNCLASSIFIED",
        "fit": "POSSIBLE",
        "freshness": "UNKNOWN",
        "priority": "MEDIUM",
        "score": score,
        "decision": decision,
        "reasoning": reason,
        "uncertainty": "Eligibility and personal-fit policies are incomplete.",
    }
    if domain["llm"]["enabled"] and os.environ.get("OPENAI_API_KEY"):
        llm_result = evaluate_with_openai(job, domain)
        llm_result["freshness"] = "UNKNOWN"
        return llm_result
    return result
