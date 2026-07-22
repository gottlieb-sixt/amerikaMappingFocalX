#!/usr/bin/env python3
"""Exportiert die menschlich validierten Mappings als Gold-Standard-Datensatz.

Jedes im Dashboard reviewte Mapping (AI bestätigt ODER manuell gewählt ODER
bewusst "kein Match") ist Ground Truth für die Mapping-Aufgabe:
  DB-Schaden (SHARK)  →  FocalX-Finding-Keys ([] = FocalX hat ihn nicht).

Der Export ist selbst-enthaltend (Schadens- und Finding-Metadaten inline) und
wird nach gold/mapping_gold.json geschrieben — das liegt bewusst NICHT unter
data/ und wird mit committet.

  python3 scripts/export_gold.py            # nur abgeschlossene Autos (✔️)
  python3 scripts/export_gold.py --all      # alle Autos mit Reviews
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "results"
REVIEWS = ROOT / "data" / "reviews"
OUT = ROOT / "gold" / "mapping_gold.json"


def main() -> None:
    include_all = "--all" in sys.argv
    cars = []
    n_entries = n_mapped = 0
    for rf in sorted(REVIEWS.glob("*.json")):
        res_file = RESULTS / rf.name
        if not res_file.exists():
            continue
        rev = json.loads(rf.read_text())
        done = bool((rev.get("_meta") or {}).get("done"))
        if not done and not include_all:
            continue
        r = json.loads(res_file.read_text())
        truths = {str(t["damage_id"]): t for t in r["truths"]}
        findings = {f["key"]: f for f in r["findings"]}
        damages = []
        for gt_key, v in sorted(rev.items()):
            if gt_key == "_meta":
                continue
            ids = gt_key.split("+")
            t = truths.get(ids[0], {})
            chosen = sorted(v.get("human") or [])
            damages.append({
                "gt_key": gt_key,
                "damage_ids": ids,
                "part": t.get("part"),
                "type": t.get("damage_type"),
                "side": t.get("side_attr"),
                "severity": t.get("severity"),
                "projection": t.get("projection"),
                "segment": t.get("segment"),
                "finding_keys": chosen,
                "findings": [{
                    "key": k,
                    "part": findings.get(k, {}).get("part"),
                    "type": findings.get(k, {}).get("type"),
                    "position": findings.get(k, {}).get("position"),
                } for k in chosen],
                "excluded": v.get("verdict") == "excluded",
                "exclude_reason": v.get("reason") or None,
                "ai_proposal_at_review": sorted(v.get("ai") or []),
                "ai_available": v.get("ai_available", True),
                "reviewed_at": v.get("ts"),
            })
            n_entries += 1
            if chosen:
                n_mapped += 1
        cars.append({
            "checkin": rf.stem,
            "plate": r["plate"],
            "review_done": done,
            "damages": damages,
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "description": "Human-validated gold standard: SHARK DB damage → FocalX finding keys "
                       "([] = not found by FocalX). Source: manual review in dashboard.",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cars_total": len(cars),
        "entries_total": n_entries,
        "entries_mapped": n_mapped,
        "cars": cars,
    }, indent=2, ensure_ascii=False))
    print(f"{OUT}: {len(cars)} Autos · {n_entries} Urteile · {n_mapped} mit Mapping")


if __name__ == "__main__":
    main()
