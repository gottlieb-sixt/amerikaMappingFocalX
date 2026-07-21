"""Multimodaler Mapping-Judge.

Kernidee (nach MAPPING.md): Ein Ground-Truth-Schaden aus der Datenbank kann zu
KEINEM, EINEM oder MEHREREN FocalX-Findings passen. Statt einer starren 1:1-
Heuristik bekommt ein Vision-LLM den DB-Schaden MIT seinen echten Fotos und ALLE
geografisch plausiblen FocalX-Kandidaten MIT ihren AI-Bildausschnitten — und
entscheidet bildbasiert, welcher Kandidat derselbe physische Schaden ist.

Gateway: llm.orange.sixt.com (Gemini, OpenAI-kompatibel, multimodal) — nur im
Sixt-Netz/VPN erreichbar. Fail-soft: ohne Key/Netz → None (Pipeline nutzt dann
den Heuristik-Fallback).
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

GATEWAY_URL = "https://llm.orange.sixt.com/v1/chat/completions"
MODEL = "vertex_ai/gemini-3.1-pro"
# Gemini 3.1 Pro zieht Reasoning-Tokens vom selben Budget ab — bei zu kleinem
# Wert kommt eine Antwort mit LEEREM choices[] zurück (alles fürs Denken verbraucht).
MAX_TOKENS = 20000

SYSTEM_PROMPT = """\
You are a meticulous vehicle-damage adjudicator. You are given ONE damage that
is recorded in the fleet database (the ground truth), together with its
reference photos, and a numbered list of candidate damages that an AI inspection
(FocalX) reported on the same car, each with its own close-up image.

Your job: select EVERY candidate that shows the SAME physical damage as the
database damage. Use the IMAGES as the primary evidence; the text fields are
supporting context.

Important: ONE real-world damage often appears as SEVERAL candidates — e.g. a
long scratch split into multiple boxes, a dent detected on two overlapping
photos, or a scratch that crosses a door and the adjacent fender. Include ALL
candidates that belong to that same physical damage, not just the single best
one. Be inclusive: if a candidate plausibly shows the described damage on the
right area of the car, include it.

Rules:
- Same physical damage means: same area of the car (same vehicle side and
  panel/region) AND consistent with the same damage in the photos.
- Allow naming differences ('fender-rear-right' vs 'Rear fender') and type
  synonyms (scuff ≈ scratch, curb rash ≈ rim scratch, chip ≈ stone chip).
- Adjacent parts CAN be the same damage. Only exclude a candidate when it is
  clearly a DIFFERENT damage or on a clearly different part/side.
- If no candidate matches, return an empty list.

IMPORTANT — the database reference photos are mixed: some show the damage close
up (2-3 shots), but one may be just a photo of the LICENSE PLATE or the whole
car for identification, which has NOTHING to do with the damage. IGNORE any
reference photo that only shows a number plate or a full-car overview — judge
the match from the actual damage close-ups and the text fields.

Output ONLY JSON:
{"matches": [<candidate numbers>], "confidence": 0.0-1.0,
 "reason": "<one sentence citing the visual evidence>"}"""


def _img_part(path: Path | None) -> dict | None:
    if not path or not Path(path).exists():
        return None
    try:
        b = Path(path).read_bytes()
    except Exception:
        return None
    uri = "data:image/jpeg;base64," + base64.b64encode(b).decode()
    return {"type": "image_url", "image_url": {"url": uri}}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _post_with_retry(api_key: str, body: str, attempts: int = 10) -> str | None:
    """POST an den Gateway; wiederholt bei 429 (Rate-Limit, max 10 parallel) und
    transienten Fehlern mit Backoff. Gibt den content-String zurück oder None.
    Wichtig: None heißt 'KI konnte NICHT urteilen' (→ Heuristik-Fallback), nicht
    'KI hat abgelehnt'."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(GATEWAY_URL, method="POST", data=body.encode())
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=180) as r:
                payload = json.loads(r.read().decode())
            choices = payload.get("choices") or []
            if not choices:   # Reasoning-Budget aufgebraucht, kein Text generiert
                if i < attempts - 1:
                    time.sleep(3)
                    continue
                return None
            return choices[0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(min(60, 5 * (i + 1)))   # 5,10,…,60s
                continue
            return None
        except Exception:
            if i < attempts - 1:
                time.sleep(3)
                continue
            return None
    return None


def judge_group(
    api_key: str,
    truth: dict,
    truth_images: list[Path],
    candidates: list[dict],
) -> dict | None:
    """truth: {part,type,side,projection,segment,severity}
    candidates: [{key, part, type, position, orientation, closeup: Path|None}]
    Returns {"match_key": <key>|None, "confidence": float, "reason": str} or None.
    """
    if not api_key or not candidates:
        return None

    content: list[dict] = []
    content.append(_text(
        "DATABASE DAMAGE (ground truth):\n" + json.dumps({
            "part": truth.get("part"), "type": truth.get("damage_type") or truth.get("type"),
            "side": truth.get("side_attr") or truth.get("side"),
            "projection": truth.get("projection"), "segment": truth.get("segment"),
            "severity": truth.get("severity"),
        }, indent=2)
    ))
    # Bis zu 4 Referenzfotos senden — NICHT auf 2 kappen: sonst könnte ein
    # Kennzeichen-Foto das eigentliche Schadensfoto verdrängen.
    added_truth_img = False
    for p in truth_images[:4]:
        part = _img_part(p)
        if part:
            content.append(_text("Database reference photo (may show the damage — or just a plate/overview, then ignore it):"))
            content.append(part)
            added_truth_img = True
    if not added_truth_img:
        content.append(_text("(No database reference photo available.)"))

    numbered = []
    for i, c in enumerate(candidates, start=1):
        numbered.append((i, c["key"]))
        content.append(_text(
            f"CANDIDATE {i} (FocalX {c['key']}):\n" + json.dumps({
                "part": c.get("part"), "type": c.get("type"),
                "capture_position": c.get("position"), "view": c.get("orientation"),
            }, indent=2)
        ))
        part = _img_part(c.get("closeup"))
        if part:
            content.append(_text(f"Candidate {i} close-up image:"))
            content.append(part)
        else:
            content.append(_text(f"(Candidate {i} has no close-up image.)"))

    content.append(_text(
        "Which candidate numbers show the SAME physical damage as the database "
        "damage? Include every matching candidate. Output ONLY the JSON object."
    ))

    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    })
    raw = _post_with_retry(api_key, body)
    if raw is None:
        return None
    try:
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        verdict = json.loads(raw)
    except Exception:
        return None

    # Modell antwortet meist mit einem Objekt, gelegentlich mit [obj] oder
    # einem Wrapper — auf ein dict normalisieren.
    if isinstance(verdict, list):
        verdict = next((v for v in verdict if isinstance(v, dict)), {})
    if not isinstance(verdict, dict):
        return None
    if "matches" not in verdict and "match" not in verdict:
        for v in verdict.values():
            if isinstance(v, dict) and ("matches" in v or "match" in v):
                verdict = v
                break

    nums = verdict.get("matches", verdict.get("match"))
    if nums is None:
        nums = []
    elif isinstance(nums, int):
        nums = [nums]
    num_to_key = dict(numbered)
    match_keys = [num_to_key[n] for n in nums if isinstance(n, int) and n in num_to_key]
    return {
        "match_keys": match_keys,
        "confidence": verdict.get("confidence"),
        "reason": verdict.get("reason", ""),
    }
