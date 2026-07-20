"""Evaluation pipeline: per car → FocalX → match against ground truth → result JSON.

Data layout (until the download API is wired in, cars can be prepared by hand):

  data/cars/<PLATE>/
      images/<position-label>.jpg     walk-around photos, named by FocalX label
      ground_truth.json               raw damage-case response (SHARK format,
                                      like system-damages/damages.json entries)
  data/results/<PLATE>.json           written by this pipeline
  data/results/<PLATE>/closeups/      downloaded AI close-ups

Run:  python -m eval.pipeline <PLATE> [<PLATE> ...]        (single-threaded)
      python -m eval.pipeline --all                        (every car in data/cars)

TODO(api): replace `load_local_images` / `load_local_ground_truth` with the
real download endpoints once provided.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from .focalx import FocalxClient, VALID_WALKAROUND
from .judge import judge_pair
from .matcher import Truth, match

ROOT = Path(__file__).resolve().parent.parent
CARS = ROOT / "data" / "cars"
RESULTS = ROOT / "data" / "results"


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


# ── inputs (local for now; API later) ───────────────────────────────────────

def load_local_images(plate: str) -> list[tuple[str, Path]]:
    """images/<label>.jpg — the stem must be a valid FocalX position label."""
    folder = CARS / plate / "images"
    out = []
    for p in sorted(folder.glob("*.jpg")):
        label = p.stem
        if label not in VALID_WALKAROUND:
            print(f"  WARN: '{label}' is not a known FocalX label — upload would be IGNORED by the AI")
        out.append((label, p))
    return out


def load_local_ground_truth(plate: str) -> list[Truth]:
    """Parses a raw damage-case response (the SHARK format)."""
    path = CARS / plate / "ground_truth.json"
    obj = json.loads(path.read_text())
    truths = []
    for case in obj.get("damage_cases", []):
        for d in case.get("damages", []):
            lv = d.get("localized_values", {})
            co = (d.get("coordinates") or [{}])[0]
            truths.append(Truth(
                damage_id=str(d.get("damage_number") or d.get("damage_id")),
                part=lv.get("part") or d.get("part") or "",
                damage_type=lv.get("type") or "",
                side_attr=lv.get("side") or "",
                projection=co.get("projection") or "",
                segment=co.get("segment") or "",
                severity=lv.get("severity"),
                case_number=case.get("case_number"),
            ))
    return truths


# ── evaluation ──────────────────────────────────────────────────────────────

def evaluate(plate: str, client: FocalxClient, llm_key: str) -> dict:
    print(f"=== {plate} ===")
    images = load_local_images(plate)
    truths = load_local_ground_truth(plate)
    print(f"  {len(images)} images, {len(truths)} ground-truth damages")

    result = client.inspect(plate, images, on_progress=lambda m: print(f"  {m}"))
    findings = result.findings
    print(f"  FocalX: {len(findings)} finding(s) on {result.orientations} orientation(s)")

    keys = [f"F{i+1}" for i in range(len(findings))]
    finding_tuples = [
        (k, f.position, f.part, f.damage_type) for k, f in zip(keys, findings)
    ]
    m = match(finding_tuples, truths)

    # LLM judge: verify matches + decide ambiguous pairs.
    by_key = dict(zip(keys, findings))
    by_id = {t.damage_id: t for t in truths}
    judged = []
    for k, tid, s in list(m.matched) + list(m.ambiguous):
        f, t = by_key[k], by_id[tid]
        verdict = judge_pair(
            llm_key,
            {"position": f.position, "part": f.part, "type": f.damage_type},
            {"part": t.part, "type": t.damage_type, "side": t.side_attr,
             "projection": t.projection, "severity": t.severity},
        )
        judged.append({"finding": k, "damage_id": tid, "score": s,
                       "heuristic_matched": (k, tid, s) in m.matched,
                       "judge": verdict})

    # Judge can promote ambiguous pairs / veto heuristic ones.
    confirmed = set()
    for j in judged:
        v = j["judge"]
        if v is None:
            if j["heuristic_matched"]:
                confirmed.add((j["finding"], j["damage_id"]))
        elif v.get("same_damage"):
            confirmed.add((j["finding"], j["damage_id"]))
    matched_truths = {t for _, t in confirmed}
    matched_findings = {f for f, _ in confirmed}

    # Close-up downloads for the dashboard.
    closeup_dir = RESULTS / plate / "closeups"
    closeup_dir.mkdir(parents=True, exist_ok=True)
    closeups = {}
    for k, f in zip(keys, findings):
        if f.close_up_url and f.close_up_url.startswith("http"):
            dest = closeup_dir / f"{k}.jpg"
            if client.download(f.close_up_url, dest):
                closeups[k] = str(dest.relative_to(ROOT))

    report = {
        "plate": plate,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inspection_id": result.inspection_id,
        "images": len(images),
        "ground_truth_total": len(truths),
        "focalx_findings_total": len(findings),
        "found": sorted(matched_truths),
        "missed": sorted(t.damage_id for t in truths if t.damage_id not in matched_truths),
        "extra_findings": sorted(k for k in keys if k not in matched_findings),
        "recall": round(len(matched_truths) / len(truths), 3) if truths else None,
        "pairs": judged,
        "findings": [
            {"key": k, "position": f.position, "orientation": f.orientation,
             "part": f.part, "type": f.damage_type, "closeup": closeups.get(k)}
            for k, f in zip(keys, findings)
        ],
        "truths": [t.__dict__ for t in truths],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{plate}.json").write_text(json.dumps(report, indent=2))
    print(f"  → recall {report['recall']}: {len(matched_truths)}/{len(truths)} found, "
          f"{len(report['extra_findings'])} extra · saved data/results/{plate}.json")
    return report


def main() -> None:
    args = sys.argv[1:]
    plates = ([p.name for p in CARS.iterdir() if p.is_dir()]
              if args == ["--all"] else [re.sub(r"[^A-Za-z0-9]", "", a).upper() for a in args])
    if not plates:
        print(__doc__)
        return
    client = FocalxClient(_env("FOCALX_PRECISE_USERNAME"), _env("FOCALX_PRECISE_PASSWORD"))
    llm_key = _env("LLM_GW_API_KEY")
    for plate in plates:
        try:
            evaluate(plate, client, llm_key)
        except Exception as e:
            print(f"  FAILED {plate}: {e}")


if __name__ == "__main__":
    main()
