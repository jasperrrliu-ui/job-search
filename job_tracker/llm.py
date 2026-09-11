from __future__ import annotations

import json
import os
import urllib.request


FIT = {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]}
CERTAINTY = {"type": "string", "enum": ["EXPLICIT", "INFERRED", "UNKNOWN"]}
EVIDENCE = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "text": {"type": "string"},
        "certainty": CERTAINTY,
    },
    "required": ["field", "text", "certainty"],
    "additionalProperties": False,
}
ROLE = {
    "type": "object",
    "properties": {
        "role_archetype": {"type": "string"},
        "seniority_signal": {
            "type": "string",
            "enum": ["STRONG_NEGATIVE", "NEUTRAL", "UNKNOWN"],
        },
        "business_domain": {"type": "string"},
        "primary_responsibilities": {"type": "array", "items": {"type": "string"}},
        "model_ownership": {"type": "string"},
        "decision_target": {"type": "string"},
        "ai_ml_centrality": FIT,
        "analytics_intensity": FIT,
        "engineering_intensity": FIT,
        "research_intensity": FIT,
        "product_business_orientation": FIT,
    },
    "required": [
        "role_archetype",
        "seniority_signal",
        "business_domain",
        "primary_responsibilities",
        "model_ownership",
        "decision_target",
        "ai_ml_centrality",
        "analytics_intensity",
        "engineering_intensity",
        "research_intensity",
        "product_business_orientation",
    ],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "role_interpretation": ROLE,
        "career_direction_fit": FIT,
        "capability_fit": FIT,
        "resume_signal_fit": FIT,
        "trajectory_fit": FIT,
        "evidence": {"type": "array", "items": EVIDENCE},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "role_interpretation",
        "career_direction_fit",
        "capability_fit",
        "resume_signal_fit",
        "trajectory_fit",
        "evidence",
        "uncertainties",
    ],
    "additionalProperties": False,
}


def evaluate_with_openai(job: dict, domain: dict) -> dict:
    settings = domain["llm"]
    job_input = {
        "company": job.get("company"),
        "title": job["title"],
        "location": job.get("location"),
        "description": job.get("description", "")[: settings["max_description_chars"]],
        "candidate_domain": {
            "career_direction": domain["role_signals"],
            "resume_evidence": domain["resume_evidence"],
        },
    }
    body = {
        "model": os.environ.get("OPENAI_MODEL", settings["model"]),
        "store": False,
        "instructions": settings["instructions"],
        "input": json.dumps(job_input, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "job_semantic_interpretation",
                "strict": True,
                "schema": SCHEMA,
            }
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    for item in payload["output"]:
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    result = json.loads(content["text"])
                    result["evidence"] = {"semantic": result["evidence"]}
                    return result
    raise ValueError("OpenAI response did not contain structured output")
