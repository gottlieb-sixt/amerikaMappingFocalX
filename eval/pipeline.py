"""Evaluation pipeline: Check-in → FocalX → Match gegen Ground Truth → Result.

Datenlayout (von den Lynx-Fetch-Skripten erzeugt):
  data/raw/<datum>/<PLATE>__<checkin8>/<POSITION>.jpg   Check-in-Fotos
  data/ground_truth/<PLATEKEY>.json                     Schadensfälle (numerisch)
  data/results/<PLATE>__<checkin8>.json                 Ergebnis (dieses Skript)

Run:
  python -m eval.pipeline 2026-07-20            # alle Check-ins des Tages
  python -m eval.pipeline FL-DX29HV             # bestimmtes Auto (Substring)
  python -m eval.pipeline --limit 2 2026-07-20  # nur N Check-ins (Pilot)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from .focalx import FocalxClient
from .ground_truth import load_truths
from .judge import judge_group
from .matcher import candidates_per_truth, heuristic_confident

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
GT_PHOTOS = ROOT / "data" / "gt_photos"
GT = ROOT / "data" / "ground_truth"
RESULTS = ROOT / "data" / "results"

# Check-in-Positionsname → kanonisches FocalX-Label (LHD: left = Fahrerseite).
POSITION_MAP = {
    "EXTERIOR_FRONT_STRAIGHT": "front",
    "FRONT_BONNET": "afront",
    "DIAGONAL_FRONT_LEFT": "front-left",
    "FRONT_LEFT_FENDER": "afront-left",
    "TYRE_RIM_FRONT_LEFT": "afront-left-wheel",
    "LEFT_SIDE_FRONT_DOOR": "aleft-front",
    "LEFT_SIDE_REAR_DOOR": "aleft-rear",
    "LEFT_SIDE_REAR_FENDER": "left-rear",
    "TYRE_RIM_REAR_LEFT": "arear-left-wheel",
    "DIAGONAL_REAR_LEFT": "rear-left",
    "EXTERIOR_REAR_STRAIGHT": "rear",
    "DIAGONAL_REAR_RIGHT": "rear-right",
    "TYRE_RIM_REAR_RIGHT": "arear-right-wheel",
    "RIGHT_SIDE_REAR_FENDER": "right-rear",
    "RIGHT_SIDE_REAR_DOOR": "abcright-rear",
    "RIGHT_SIDE_FRONT_DOOR": "aright-front",
    "TYRE_RIM_FRONT_RIGHT": "afront-right-wheel",
    "FRONT_RIGHT_FENDER": "afront-right",
    "DIAGONAL_FRONT_RIGHT": "front-right",
}


def _env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        envfile = ROOT / ".env"
        if envfile.exists():
            for line in envfile.read_text().splitlines():
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip()
                    break
    return val


def images_for(checkin_dir: Path) -> list[tuple[str, Path]]:
    out = []
    for p in sorted(checkin_dir.glob("*.jpg")):
        label = POSITION_MAP.get(p.stem)
        if label:
            out.append((label, p))
    return out


def gt_images(key: str, damage_id: str) -> list[Path]:
    """Original-Schadensfotos aus der DB (scripts/download_gt_photos.py)."""
    return sorted((GT_PHOTOS / key).glob(f"{damage_id}_*.jpg"))


def evaluate(checkin_dir: Path, client: FocalxClient, llm_key: str) -> dict:
    name = checkin_dir.name                       # PLATE__checkin8
    plate = name.split("__")[0]
    key = re.sub(r"[^A-Za-z0-9]", "", plate).upper()
    print(f"=== {name} ===", flush=True)

    images = images_for(checkin_dir)
    gt_file = GT / f"{key}.json"
    truths = load_truths(gt_file) if gt_file.exists() else []
    print(f"  {len(images)} Bilder, {len(truths)} Ground-Truth-Schäden", flush=True)
    if not images:
        # Alter Foto-Flow (vor den 19 Positionen) — nichts zu inspizieren.
        print("  ÜBERSPRUNGEN: keine Positions-Bilder", flush=True)
        return {"plate": plate, "checkin": name, "skipped": "no_position_images"}

    result = client.inspect(key, images, on_progress=lambda m: print(f"  {m}", flush=True))
    # Symmetrischer Scope: Glas-/Interior-Findings zählen weder als Treffer
    # noch als False Positives.
    from .ground_truth import is_exterior_non_glass
    findings = [f for f in result.findings if is_exterior_non_glass(f.part or "")]
    n_excluded = len(result.findings) - len(findings)
    if n_excluded:
        print(f"  {n_excluded} Glas-/Interior-Finding(s) ausgefiltert", flush=True)
    print(f"  FocalX: {len(findings)} Finding(s) auf {result.orientations} Ansichten", flush=True)

    keys = [f"F{i + 1}" for i in range(len(findings))]
    by_key = dict(zip(keys, findings))
    by_id = {t.damage_id: t for t in truths}

    # Close-ups ZUERST laden — der multimodale Judge braucht die Bilder.
    closeup_dir = RESULTS / name / "closeups"
    closeup_dir.mkdir(parents=True, exist_ok=True)
    closeups: dict[str, str] = {}
    closeup_path: dict[str, Path] = {}
    for k, f in zip(keys, findings):
        if f.close_up_url and f.close_up_url.startswith("http"):
            dest = closeup_dir / f"{k}.jpg"
            if client.download(f.close_up_url, dest):
                closeups[k] = str(dest.relative_to(ROOT))
                closeup_path[k] = dest

    # Kandidaten je GT-Schaden (großzügig — die KI sortiert per Bild aus).
    cand = candidates_per_truth(
        [(k, f.position, f.part, f.damage_type) for k, f in zip(keys, findings)], truths)

    # Jeder GT-Schaden wird gegen SEINE VOLLEN Kandidaten geprüft — kein
    # Verbrauchen. Die KI darf MEHRERE Funde bestätigen (ein realer Schaden kann
    # von FocalX in mehrere Boxen/Fotos aufgeteilt sein). Dadurch geht kein
    # gültiger Match verloren, nur weil ein anderer GT früher dran war.
    matched_map: dict[str, list[str]] = {}   # damage_id → [finding keys]
    pairs = []
    for t in truths:
        avail = cand[t.damage_id]
        if not avail:
            pairs.append({"damage_id": t.damage_id, "findings": [], "via": None,
                          "confidence": None, "reason": "keine Kandidaten in der Nähe",
                          "candidates": []})
            continue
        cand_dicts = [{
            "key": k, "part": by_key[k].part, "type": by_key[k].damage_type,
            "position": by_key[k].position, "orientation": by_key[k].orientation,
            "closeup": closeup_path.get(k),
        } for k, _ in avail]
        verdict = judge_group(
            llm_key,
            {"part": t.part, "damage_type": t.damage_type, "side_attr": t.side_attr,
             "projection": t.projection, "segment": t.segment, "severity": t.severity},
            gt_images(key, t.damage_id),
            cand_dicts,
        )
        chosen, via, conf, reason = [], None, None, ""
        if verdict is None:
            # Strenger Fallback ohne KI: bester Kandidat nur bei Seite+Typ+Bauteil.
            best_k, best_s = avail[0]
            bf = by_key[best_k]
            if heuristic_confident(bf.position, bf.part, bf.damage_type, t):
                chosen, via = [best_k], "heuristic"
                reason = f"Heuristik: Seite+Typ+Bauteil (Score {best_s}, KI n/a)"
        elif verdict.get("match_keys"):
            chosen, via = list(verdict["match_keys"]), "ai"
            conf, reason = verdict.get("confidence"), verdict.get("reason", "")
        else:
            via, reason = "ai_rejected", verdict.get("reason", "")
        if chosen:
            matched_map[t.damage_id] = chosen
        pairs.append({
            "damage_id": t.damage_id, "findings": chosen, "via": via,
            "confidence": conf, "reason": reason,
            "candidates": [k for k, _ in avail],
        })

    matched_truths = set(matched_map.keys())
    matched_findings = {k for ks in matched_map.values() for k in ks}

    report = {
        "plate": plate,
        "checkin": name,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inspection_id": result.inspection_id,
        "images": len(images),
        "ground_truth_total": len(truths),
        "focalx_findings_total": len(findings),
        "found": sorted(matched_truths),
        "missed": sorted(t.damage_id for t in truths if t.damage_id not in matched_truths),
        "extra_findings": sorted(k for k in keys if k not in matched_findings),
        "recall": round(len(matched_truths) / len(truths), 3) if truths else None,
        "pairs": pairs,
        "findings": [
            {"key": k, "position": f.position, "orientation": f.orientation,
             "part": f.part, "type": f.damage_type, "closeup": closeups.get(k)}
            for k, f in zip(keys, findings)
        ],
        "truths": [t.__dict__ for t in truths],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(report, indent=2))
    rec = f"{report['recall']:.0%}" if report["recall"] is not None else "– (0 GT)"
    print(f"  → Recall {rec}: {len(matched_truths)}/{len(truths)} gefunden, "
          f"{len(report['extra_findings'])} zusätzlich · data/results/{name}.json", flush=True)
    return report


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
        args = [a for a in args if a != str(limit)]
    dirs = sorted(d for d in RAW.glob("*/*/") if d.is_dir())
    if args:
        dirs = [d for d in dirs
                if any(a in str(d) or a.replace("-", "") in d.name.replace("-", "") for a in args)]
    dirs = [d for d in dirs if not (RESULTS / f"{d.name}.json").exists()]
    if "--only-damaged" in sys.argv:
        def in_scope(d):
            key = re.sub(r"[^A-Za-z0-9]", "", d.name.split("__")[0]).upper()
            gt = GT / f"{key}.json"
            return bool(images_for(d)) and gt.exists() and bool(load_truths(gt))
        dirs = [d for d in dirs if in_scope(d)]
    if limit:
        dirs = dirs[:limit]
    print(f"{len(dirs)} Check-in(s) zu bewerten")
    client = FocalxClient(_env("FOCALX_PRECISE_USERNAME"), _env("FOCALX_PRECISE_PASSWORD"))
    llm_key = _env("LLM_GW_API_KEY")
    for d in dirs:
        try:
            evaluate(d, client, llm_key)
        except Exception as e:
            print(f"  FEHLER {d.name}: {e}", flush=True)


if __name__ == "__main__":
    main()
