"""LLM judge: verifies heuristic matches and decides ambiguous pairs.

Text-only call to the Sixt LLM gateway (Gemini, OpenAI-compatible) following
the MAPPING.md clustering-judge principle: the model sees the FocalX finding
and the DB damage as structured text and answers same_damage yes/no with a
confidence. Fail-soft: without a key / off-VPN every pair stays "unreviewed".

NOTE: llm.orange.sixt.com is only reachable inside the Sixt network/VPN.
"""
from __future__ import annotations

import json
import urllib.request

GATEWAY_URL = "https://llm.orange.sixt.com/v1/chat/completions"
MODEL = "vertex_ai/gemini-3.1-pro"

SYSTEM_PROMPT = """\
You are a vehicle damage adjudicator. You get ONE damage found by an AI \
inspection (FocalX) and ONE damage recorded in the fleet database. Decide \
whether they describe the SAME physical damage on the car.

Consider: vehicle side / viewing position, the part (allow naming differences \
like 'fender-rear-right' vs 'Rear fender'), the damage type (scuff≈scratch), \
and plausibility. Adjacent parts CAN be the same damage (a scratch crossing \
door and fender); different sides of the car can NOT.

Output ONLY JSON: {"same_damage": true|false, "confidence": 0.0-1.0, \
"reason": "<one sentence>"}"""


def judge_pair(api_key: str, finding: dict, truth: dict) -> dict | None:
    """Returns {"same_damage": bool, "confidence": float, "reason": str} or None."""
    if not api_key:
        return None
    user = (
        "FocalX finding:\n" + json.dumps(finding, indent=2) +
        "\n\nDatabase damage:\n" + json.dumps(truth, indent=2) +
        "\n\nSame physical damage? Output ONLY the JSON object."
    )
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    })
    try:
        req = urllib.request.Request(GATEWAY_URL, method="POST", data=body.encode())
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read().decode())
        content = payload["choices"][0]["message"]["content"]
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        verdict = json.loads(content)
        if isinstance(verdict.get("same_damage"), bool):
            return verdict
    except Exception:
        pass
    return None
