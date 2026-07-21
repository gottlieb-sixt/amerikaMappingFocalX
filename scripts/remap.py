#!/usr/bin/env python3
"""Wendet die (neue) Mapping-Logik auf BEREITS vorhandene Ergebnisse an —
ohne FocalX neu laufen zu lassen. Nutzt die gespeicherten Findings + Close-ups
+ Ground Truth und rechnet nur Kandidaten-Gruppierung + multimodalen Judge neu.

Schnell zum Iterieren am Matching (braucht nur LLM-Gateway/VPN, kein FocalX).

  python3 scripts/remap.py            # alle data/results/*.json neu mappen
  python3 scripts/remap.py FL-07ELXT  # nur bestimmte
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.judge import judge_group
from eval.matcher import Truth, candidates_per_truth, heuristic_confident
from eval.pipeline import ROOT, RESULTS, gt_images, _env


def remap(path: Path, llm_key: str) -> None:
    r = json.loads(path.read_text())
    if r.get("skipped"):
        return
    plate_key = "".join(c for c in r["plate"] if c.isalnum()).upper()
    findings = r["findings"]                      # [{key,position,part,type,closeup}]
    by_key = {f["key"]: f for f in findings}
    truths = [Truth(
        damage_id=str(t["damage_id"]), part=t["part"], damage_type=t["damage_type"],
        side_attr=t["side_attr"], projection=t["projection"], segment=t["segment"],
        severity=t.get("severity"), case_number=t.get("case_number"),
    ) for t in r["truths"]]

    cand = candidates_per_truth(
        [(f["key"], f["position"], f["part"], f["type"]) for f in findings], truths)

    matched_map, pairs = {}, []
    for t in truths:
        avail = cand[t.damage_id]
        if not avail:
            pairs.append({"damage_id": t.damage_id, "findings": [], "via": None,
                          "confidence": None, "reason": "keine Kandidaten in der Nähe",
                          "candidates": []})
            continue
        cand_dicts = [{
            "key": k, "part": by_key[k]["part"], "type": by_key[k]["type"],
            "position": by_key[k]["position"], "orientation": by_key[k].get("orientation"),
            "closeup": (ROOT / by_key[k]["closeup"]) if by_key[k].get("closeup") else None,
        } for k, _ in avail]
        verdict = judge_group(
            llm_key,
            {"part": t.part, "damage_type": t.damage_type, "side_attr": t.side_attr,
             "projection": t.projection, "segment": t.segment, "severity": t.severity},
            gt_images(plate_key, t.damage_id), cand_dicts)
        chosen, via, conf, reason = [], None, None, ""
        if verdict is None:
            best_k, best_s = avail[0]
            bf = by_key[best_k]
            if heuristic_confident(bf["position"], bf["part"], bf["type"], t):
                chosen, via, reason = [best_k], "heuristic", f"Heuristik: Seite+Typ+Bauteil (Score {best_s}, KI n/a)"
        elif verdict.get("match_keys"):
            chosen, via = list(verdict["match_keys"]), "ai"
            conf, reason = verdict.get("confidence"), verdict.get("reason", "")
        else:
            via, reason = "ai_rejected", verdict.get("reason", "")
        if chosen:
            matched_map[t.damage_id] = chosen
        pairs.append({"damage_id": t.damage_id, "findings": chosen, "via": via,
                      "confidence": conf, "reason": reason, "candidates": [k for k, _ in avail]})

    matched_truths = set(matched_map)
    matched_findings = {k for ks in matched_map.values() for k in ks}
    keys = [f["key"] for f in findings]
    r["found"] = sorted(matched_truths)
    r["missed"] = sorted(t.damage_id for t in truths if t.damage_id not in matched_truths)
    r["extra_findings"] = sorted(k for k in keys if k not in matched_findings)
    r["recall"] = round(len(matched_truths) / len(truths), 3) if truths else None
    r["pairs"] = pairs
    path.write_text(json.dumps(r, indent=2))
    rec = f"{r['recall']:.0%}" if r["recall"] is not None else "–"
    print(f"{r['checkin']}: Recall {rec} — {len(matched_truths)}/{len(truths)} "
          f"gefunden, {len(r['extra_findings'])} zusätzlich")


def main():
    llm_key = _env("LLM_GW_API_KEY")
    if not llm_key:
        print("WARN: kein LLM_GW_API_KEY — nur Heuristik-Fallback")
    files = sorted(RESULTS.glob("*.json"))
    if sys.argv[1:]:
        files = [f for f in files if any(a in f.name for a in sys.argv[1:])]
    for f in files:
        remap(f, llm_key)


if __name__ == "__main__":
    main()
