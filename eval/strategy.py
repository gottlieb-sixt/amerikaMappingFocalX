"""Strategie-Framework: Prompt-/Modell-Varianten des Mapping-Judges gegen den
eingefrorenen Gold-Standard messen.

Begriffe:
  Strategie  = benannte Judge-Variante (System-Prompt + Parameter) unter
               strategies/<name>/ (meta.json + prompt.txt) — wird committet.
  Lauf       = die Vorschläge einer Strategie für die Gold-Autos unter
               data/strategies/<name>/<checkin>.json — gitignored wie alle Daten.
  Benchmark  = die menschlich validierten Urteile aus gold/mapping_gold.json,
               gefiltert wie auf der Dashboard-Seite (ohne 🚫-Ausschlüsse, ohne
               🔧/⏰-Auto-Ausschlüsse, ohne Urteile ohne KI-Verfügbarkeit) —
               fix 126 Urteile, damit alle Strategien identisch verglichen werden.

WICHTIG: Cluster (GT + Findings) und Kandidatenlisten bleiben aus dem
v01-Lauf eingefroren (data/results/*.json). Strategien variieren NUR das
Judge-Urteil pro GT-Cluster — sonst passen die gt_keys nicht mehr zum Gold.

v01 ist die Baseline: ihre Vorschläge stehen als ai_proposal_at_review im
Gold-Standard und brauchen keinen eigenen Lauf.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRATEGIES = ROOT / "strategies"
RUNS = ROOT / "data" / "strategies"
GOLD = ROOT / "gold" / "mapping_gold.json"


def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate).upper()


# ── Strategien (committete Definitionen) ────────────────────────────────────

def load_strategies() -> dict[str, dict]:
    """name → meta (inkl. 'prompt'-Text). Sortiert nach Name (v01, v02, …)."""
    out: dict[str, dict] = {}
    if not STRATEGIES.exists():
        return out
    for d in sorted(STRATEGIES.iterdir()):
        mf = d / "meta.json"
        if not d.is_dir() or not mf.exists():
            continue
        meta = json.loads(mf.read_text())
        meta.setdefault("name", d.name)
        pf = d / "prompt.txt"
        meta["prompt"] = pf.read_text() if pf.exists() else None
        out[d.name] = meta
    return out


# ── Benchmark (Gold-Standard, gefiltert wie im Dashboard) ───────────────────

def _cases_of(key: str) -> list[dict]:
    f = ROOT / "data" / "ground_truth" / f"{key}.json"
    if not f.exists():
        return []
    cases = json.loads(f.read_text()).get("2") or []
    return [cases] if isinstance(cases, dict) else cases


def auto_excluded_ids(key: str, checkin: str) -> set[str]:
    """🔧 reparierte (Schadens-Feld 31) + ⏰ nach dem Check-in-Tag erfasste
    Schäden (Fall-Feld 34/32 vs. data/raw/<datum>/) — identisch zur
    Dashboard-Logik, damit der Benchmark dieselben 126 Urteile umfasst."""
    dirs = sorted((ROOT / "data" / "raw").glob(f"*/{checkin}"))
    cutoff = None
    if dirs:
        try:
            cutoff = _dt.datetime.fromisoformat(
                dirs[0].parent.name + "T23:59:59+02:00").timestamp()
        except ValueError:
            pass
    out: set[str] = set()
    for c in _cases_of(key):
        v = c.get("34") or c.get("32")
        try:
            ts = int(str(v.get("1"))) if isinstance(v, dict) else None
        except (TypeError, ValueError):
            ts = None
        late = bool(cutoff and ts and ts > cutoff)
        dms = c.get("31") or []
        if isinstance(dms, dict):
            dms = [dms]
        for dm in dms:
            if isinstance(dm, dict) and (late or dm.get("31") == 1):
                out.add(str(dm.get("3")))
    return out


def benchmark_records() -> list[dict]:
    """Ein Eintrag pro Gold-Urteil im Benchmark:
    {checkin, plate, gt_key, damage_ids, gold_keys, part, type, side, severity,
     v01_keys (Baseline-Vorschlag)}."""
    gold = json.loads(GOLD.read_text())
    records: list[dict] = []
    for car in gold["cars"]:
        if not car["review_done"]:
            continue
        auto = auto_excluded_ids(plate_key(car["plate"]), car["checkin"])
        for d in car["damages"]:
            if d["excluded"] or not d.get("ai_available", True):
                continue
            if all(i in auto for i in d["damage_ids"]):
                continue
            records.append({
                "checkin": car["checkin"], "plate": car["plate"],
                "gt_key": d["gt_key"], "damage_ids": d["damage_ids"],
                "gold_keys": sorted(d["finding_keys"]),
                "part": d.get("part"), "type": d.get("type"),
                "side": d.get("side"), "severity": d.get("severity"),
                "v01_keys": sorted(d.get("ai_proposal_at_review") or []),
            })
    return records


# ── Läufe (Vorschläge einer Strategie) ──────────────────────────────────────

def run_proposals(name: str) -> dict[tuple[str, str], dict]:
    """(checkin, gt_key) → Proposal-Dict aus data/strategies/<name>/."""
    out: dict[tuple[str, str], dict] = {}
    d = RUNS / name
    if not d.exists():
        return out
    for f in sorted(d.glob("*.json")):
        r = json.loads(f.read_text())
        for gt_key, p in (r.get("proposals") or {}).items():
            out[(r["checkin"], gt_key)] = p
    return out


def proposals_for(name: str, meta: dict | None = None) -> dict[tuple[str, str], dict]:
    """Vorschläge einer Strategie — Baseline v01 kommt aus dem Gold-Standard."""
    meta = meta or load_strategies().get(name) or {}
    if meta.get("baseline"):
        return {(rec["checkin"], rec["gt_key"]): {"finding_keys": rec["v01_keys"],
                                                  "via": "gold"}
                for rec in benchmark_records()}
    return run_proposals(name)


# ── Scoring ─────────────────────────────────────────────────────────────────

def classify(proposal, chosen) -> str:
    """Gleiche Kategorien wie die Review-Verdicts im Dashboard."""
    proposal, chosen = set(proposal), set(chosen)
    if not proposal and not chosen:
        return "confirmed_empty"          # korrekt: kein Match
    if proposal == chosen:
        return "confirmed"                # exakt richtig gemappt
    if proposal and not chosen:
        return "rejected"                 # fälschlich gemappt
    if not proposal and chosen:
        return "human_added"              # Match übersehen
    return "corrected"                    # falsches/unvollständiges Finding


VERDICTS = ["confirmed", "corrected", "human_added", "confirmed_empty", "rejected"]


def score(records: list[dict], proposals: dict[tuple[str, str], dict]) -> dict:
    """Bewertet einen Lauf gegen den Benchmark. Nicht abgedeckte Urteile werden
    ausgewiesen, aber nicht gewertet (Lauf noch unvollständig)."""
    counts = {v: 0 for v in VERDICTS}
    scored: list[dict] = []
    missing = 0
    for rec in records:
        p = proposals.get((rec["checkin"], rec["gt_key"]))
        if p is None:
            missing += 1
            continue
        v = classify(p.get("finding_keys") or [], rec["gold_keys"])
        counts[v] += 1
        scored.append({**rec, "proposal_keys": sorted(p.get("finding_keys") or []),
                       "verdict": v})
    tot = sum(counts.values())
    ok = counts["confirmed"] + counts["confirmed_empty"]
    ai_match = counts["confirmed"] + counts["corrected"] + counts["rejected"]
    return {
        "counts": counts,
        "records": scored,
        "covered": tot,
        "missing": missing,
        "total": len(records),
        "mappable": counts["confirmed"] + counts["corrected"] + counts["human_added"],
        "nonmap": counts["confirmed_empty"] + counts["rejected"],
        "accuracy": ok / tot if tot else None,
        "precision": counts["confirmed"] / ai_match if ai_match else None,
    }
