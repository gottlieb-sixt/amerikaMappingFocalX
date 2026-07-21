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
from .mapping import run_mapping

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

    findings_meta = [{"key": k, "part": f.part, "type": f.damage_type,
                      "position": f.position, "orientation": f.orientation}
                     for k, f in zip(keys, findings)]
    mp = run_mapping(llm_key, findings_meta, truths, key, closeup_path,
                     log=lambda m: print(f"  {m}", flush=True))

    report = {
        "plate": plate,
        "checkin": name,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inspection_id": result.inspection_id,
        "images": len(images),
        "ground_truth_total": len(truths),
        "focalx_findings_total": len(findings),
        **mp,
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
    ph = report.get("physical", {})
    prec = f"{ph['recall']:.0%}" if ph.get("recall") is not None else "–"
    print(f"  → Recall {rec} (Zeilen) · {prec} (physisch: {ph.get('gt_found')}/{ph.get('gt_total')}) · "
          f"{ph.get('extras_unique')} unique Extras · data/results/{name}.json", flush=True)
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
