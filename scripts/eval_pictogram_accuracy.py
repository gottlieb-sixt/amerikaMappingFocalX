"""Misst die Genauigkeit des LLM-Piktogramm-Mappings gegen die Wahrheit.

Für GT-Schäden kennen wir die ECHTE Position: die SHARK-DB-Koordinaten
(projection/segment/x/y) ergeben deterministisch einen Punkt aufs Piktogramm
(dieselbe Mathematik wie eval/pictogram.py). Der LLM bekam Bauteil/Seite/
Projektion + Fotos und hat eine Bounding-Box zurückgegeben.

Dieses Skript vergleicht beides:
  * Treffer  = liegt der DB-Punkt IN der LLM-Box?
  * Nähe     = Distanz DB-Punkt ↔ Box-Mittelpunkt (in px und % der Bilddiagonale)

Da der LLM die DB-Seite/-Projektion vorgegeben bekam, misst der Vergleich vor
allem die Platzierung INNERHALB der Projektion (vorne/hinten/oben/unten am
Bauteil) — nicht die Seite. Der DB-Punkt ist die Referenz; er ist selbst nicht
perfekt, aber die beste Wahrheit, die wir haben.

Lauf:  python3 scripts/eval_pictogram_accuracy.py            # GT (Standard)
       python3 scripts/eval_pictogram_accuracy.py --json     # Rohwerte als JSON
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "ground_truth"
LLM_DIR = ROOT / "data" / "pictograms_llm"
PICTO_DIR = ROOT / "pictograms" / "suv"
GRID = 3
PROJECTION_BY_NUMBER = {1: "FRONT_SIDE", 2: "BACK_SIDE", 3: "DRIVER_SIDE", 4: "PASSENGER_SIDE"}


def _iter_damages(data: dict):
    cases = data.get("2") or []
    if isinstance(cases, dict):
        cases = [cases]
    for case in cases:
        dmgs = case.get("31") or []
        if isinstance(dmgs, dict):
            dmgs = [dmgs]
        for dm in dmgs:
            if isinstance(dm, dict):
                yield dm


def _coords(dm: dict) -> dict:
    co = dm.get("20") or {}
    if isinstance(co, list):
        co = co[0] if co else {}
    return co or {}


def db_point(plate_key: str, damage_ids: list[str], size: tuple[int, int]):
    """→ (proj_str, x, y) aus den DB-Koordinaten des ersten Schadens mit gültiger
    Projektion+Segment. size = (w,h) des Piktogramms. None, wenn nichts brauchbar."""
    gt = GT_DIR / f"{plate_key}.json"
    if not gt.exists():
        return None
    try:
        data = json.loads(gt.read_text())
    except Exception:
        return None
    by_id = {str(d.get("3") or d.get("1")): d for d in _iter_damages(data)}
    w, h = size
    for did in damage_ids:
        dm = by_id.get(str(did))
        if not dm:
            continue
        co = _coords(dm)
        try:
            proj_n = int(str(co.get("3", 0) or 0))
            seg_n = int(str(co.get("4", 0) or 0))
            x_raw = float(co.get("6", 0) or 0)
            y_raw = float(co.get("7", 0) or 0)
        except (TypeError, ValueError):
            continue
        proj = PROJECTION_BY_NUMBER.get(proj_n)
        if not proj or not seg_n:
            continue
        col = (seg_n - 1) % GRID
        row = (seg_n - 1) // GRID
        cw, ch = w / GRID, h / GRID
        x = max(0, min(w - 1, col * cw + x_raw))
        y = max(0, min(h - 1, row * ch + y_raw))
        return proj, x, y
    return None


def _median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def main() -> None:
    want_json = "--json" in sys.argv
    files = sorted(LLM_DIR.glob("*/*.json"))
    rows = []
    n_total = n_nolocal = n_nocoords = n_projmis = 0

    for f in files:
        rec = json.loads(f.read_text())
        n_total += 1
        box = rec.get("box")
        if not box or not all(k in box for k in ("x", "y", "width", "height")):
            n_nolocal += 1
            continue
        size = tuple(rec.get("pictogram_size") or (0, 0))
        if size == (0, 0):
            continue
        pt = db_point(rec["plate_key"], rec.get("damage_ids", []), size)
        if pt is None:
            n_nocoords += 1
            continue
        proj_db, px, py = pt
        if proj_db != rec.get("projection"):
            n_projmis += 1   # DB-Projektion ≠ dem, was der LLM bekam (selten)
        w, h = size
        diag = math.hypot(w, h)
        bx, by = float(box["x"]), float(box["y"])
        bw, bh = float(box["width"]), float(box["height"])
        cx, cy = bx + bw / 2, by + bh / 2
        hit = (bx <= px <= bx + bw) and (by <= py <= by + bh)
        dist = math.hypot(cx - px, cy - py)
        rows.append({
            "plate": rec["plate"], "id": rec.get("gt_key") or rec.get("id"),
            "projection": rec.get("projection"),
            "hit": hit, "dist_px": dist, "dist_pct": 100 * dist / diag,
            "sub": (box.get("sub_part_localized") or "?"),
            "side_m": (box.get("side_match") or "?"),
        })

    if want_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    n = len(rows)
    print("═" * 64)
    print("PIKTOGRAMM-MAPPING — Genauigkeit LLM-Box vs. DB-Punkt (GT)")
    print("═" * 64)
    print(f"Dateien gesamt: {n_total}  ·  bewertbar (Box + DB-Koordinaten): {n}")
    print(f"  not_localizable (LLM keine Box): {n_nolocal}"
          f"  ·  ohne DB-Koordinaten: {n_nocoords}"
          f"  ·  DB-Projektion ≠ vorgegeben: {n_projmis}")
    if not n:
        print("\n(Noch keine bewertbaren Ergebnisse — GT-Lauf läuft evtl. noch.)")
        return

    hits = sum(r["hit"] for r in rows)
    d_pct = [r["dist_pct"] for r in rows]
    within10 = sum(1 for r in rows if r["dist_pct"] <= 10)
    within20 = sum(1 for r in rows if r["dist_pct"] <= 20)
    print("\n── Gesamt ──")
    print(f"  Punkt-in-Box-Trefferquote:      {hits}/{n} = {100*hits/n:.0f}%")
    print(f"  Mittelpunkt ≤10% Diagonale weg: {within10}/{n} = {100*within10/n:.0f}%")
    print(f"  Mittelpunkt ≤20% Diagonale weg: {within20}/{n} = {100*within20/n:.0f}%")
    print(f"  Median-Distanz: {_median(d_pct):.1f}% der Diagonale "
          f"({_median([r['dist_px'] for r in rows]):.0f} px)")

    print("\n── nach Projektion ──")
    projs: dict[str, list] = {}
    for r in rows:
        projs.setdefault(r["projection"], []).append(r)
    for p, rs in sorted(projs.items()):
        h = sum(x["hit"] for x in rs)
        print(f"  {p:<15} n={len(rs):>3}  Treffer {100*h/len(rs):>3.0f}%  "
              f"Median {_median([x['dist_pct'] for x in rs]):>4.1f}%")

    print("\n── nach Selbsteinschätzung sub_part_localized ──")
    subs: dict[str, list] = {}
    for r in rows:
        subs.setdefault(r["sub"], []).append(r)
    for s, rs in sorted(subs.items()):
        h = sum(x["hit"] for x in rs)
        print(f"  {s:<12} n={len(rs):>3}  Treffer {100*h/len(rs):>3.0f}%  "
              f"Median {_median([x['dist_pct'] for x in rs]):>4.1f}%")


if __name__ == "__main__":
    main()
