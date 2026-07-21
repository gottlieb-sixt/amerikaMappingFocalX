#!/usr/bin/env python3
"""Wendet die aktuelle Mapping-Logik (Cluster + multimodaler Judge) auf BEREITS
vorhandene Ergebnisse an — ohne FocalX neu laufen zu lassen. Nutzt gespeicherte
Findings + Close-ups + Ground Truth.

  python3 scripts/remap.py            # alle data/results/*.json neu mappen
  python3 scripts/remap.py FL-07ELXT  # nur bestimmte
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.mapping import run_mapping
from eval.matcher import Truth
from eval.pipeline import ROOT, RESULTS, _env


def remap(path: Path, llm_key: str) -> None:
    r = json.loads(path.read_text())
    if r.get("skipped"):
        return
    plate_key = "".join(c for c in r["plate"] if c.isalnum()).upper()
    findings_meta = [{"key": f["key"], "part": f["part"], "type": f["type"],
                      "position": f["position"], "orientation": f.get("orientation")}
                     for f in r["findings"]]
    closeup_path = {f["key"]: ROOT / f["closeup"]
                    for f in r["findings"] if f.get("closeup")}
    truths = [Truth(
        damage_id=str(t["damage_id"]), part=t["part"], damage_type=t["damage_type"],
        side_attr=t["side_attr"], projection=t["projection"], segment=t["segment"],
        severity=t.get("severity"), case_number=t.get("case_number"),
    ) for t in r["truths"]]

    mp = run_mapping(llm_key, findings_meta, truths, plate_key, closeup_path,
                     log=lambda m: print(f"  {m}"))
    r.update(mp)
    path.write_text(json.dumps(r, indent=2))
    ph = r.get("physical", {})
    rec = f"{r['recall']:.0%}" if r["recall"] is not None else "–"
    prec = f"{ph['recall']:.0%}" if ph.get("recall") is not None else "–"
    print(f"{r['checkin']}: Zeilen-Recall {rec} · physisch {prec} "
          f"({ph.get('gt_found')}/{ph.get('gt_total')}) · {ph.get('extras_unique')} unique Extras")


def main():
    llm_key = _env("LLM_GW_API_KEY")
    if not llm_key:
        print("WARN: kein LLM_GW_API_KEY — nur Heuristik-Fallback")
    files = sorted(RESULTS.glob("*.json"))
    if sys.argv[1:]:
        files = [f for f in files if any(a in f.name for a in sys.argv[1:])]
    for f in files:
        print(f"=== {f.stem} ===")
        remap(f, llm_key)


if __name__ == "__main__":
    main()
