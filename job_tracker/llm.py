from __future__ import annotations

import json
import os
import urllib.request


SCHEMA = {
    "type": "object",
    "properties": {
        "eligibility": {"type": "string", "enum": ["PASS", "FAIL", "UNCERTAIN"]},
        "role_archetype": {"type": "string"},
        "fit": {"type": "string", "enum": ["STRONG", "POSSIBLE", "WEAK", "UNKNOWN"]},
        "priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]},
        "decision": {"type": "string", "enum": ["APPLY_NOW", "APPLY_TODAY", "REVIEW", "SAVE", "SKIP"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasoning": {"type": "string"},
        "uncertainty": {"type": "string"}
    },
    "required": [
        "eligibility", "role_archetype", "fit", "priority", "decision",
        "score", "reasoning", "uncertainty"
    ],
    "additionalProperties": False
}


def evaluate_with_openai(job: dict, domain: dict) -> dict:
    settings = domain["llm"]
    job_input = {
        "company": job.get("company"),
        "title": job["title"],
        "location": job.get("location"),
        "description": job.get("description", "")[: settings["max_description_chars"]],
        "domain_config": {
            "target_titles": domain["target_titles"],
            "positive_signals": domain["positive_signals"],
            "knowledge_status": domain["knowledge_status"]
        }
    }
    body = {
        "model": os.environ.get("OPENAI_MODEL", settings["model"]),
        "store": False,
        "instructions": settings["instructions"],
        "input": json.dumps(job_input, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "job_evaluation",
                "strict": True,
                "schema": SCHEMA
            }
        }
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    for item in payload["output"]:
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return json.loads(content["text"])
    raise ValueError("OpenAI response did not contain structured output")

