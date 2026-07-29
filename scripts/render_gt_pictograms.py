"""Rendert pro GT-Schaden das echte Piktogramm: korrektes Fahrzeugklassen-Template
(aus include_pictogram) + Marker aus den DB-Koordinaten.

Quellen (bereits lokal, kein API-Call nötig):
  data/ground_truth_pictogram/<KEY>.json   volle GetDamageCasesByVehicle-Antwort
                                            (include_pictogram=true) — enthält je
                                            Auto die Fahrzeugklasse + je Schaden
                                            die Koordinaten.
  data/pictograms_db/<KLASSE>/<PROJ>.png    die echten leeren Templates pro Klasse
                                            (P_OFF/P_LIM/P_MEH/P_CMP_5 …).

Ausgabe:
  data/pictograms_gt/<KEY>/<damage_id>.png       Template + roter Marker (markiert)
  data/pictograms_gt/<KEY>/<damage_id>__blank.png das leere Klassen-Template

Braucht Pillow → mit .venv/bin/python laufen lassen.
"""
from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "ground_truth_pictogram"
TMPL = ROOT / "data" / "pictograms_db"
OUT = ROOT / "data" / "pictograms_gt"
GRID = 3
PROJ_FILE = {"FRONT_SIDE": "FRONT_SIDE.png", "BACK_SIDE": "BACK_SIDE.png",
             "DRIVER_SIDE": "DRIVER_SIDE.png", "PASSENGER_SIDE": "PASSENGER_SIDE.png",
             "TOP": "TOP.png", "TOP_SIDE": "TOP.png"}


def _class_of(data: dict) -> str | None:
    vp = data.get("vehicle_pictogram") or {}
    url = vp.get("original_pictogram") or ""
    m = re.search(r"Damage_Pictograms_All/([^/]+)/", urlparse(url).path)
    return m.group(1) if m else None


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def main() -> None:
    from PIL import Image, ImageDraw
    files = sorted(glob.glob(str(SRC / "*.json")))
    if not files:
        raise SystemExit("Keine data/ground_truth_pictogram/*.json — erst "
                         "scripts/fetch_pictograms.py laufen lassen.")
    n_ok = n_skip = 0
    by_class: dict[str, int] = {}
    for f in files:
        data = json.loads(Path(f).read_text())
        key = Path(f).stem
        cls = _class_of(data)
        if not cls or not (TMPL / cls).exists():
            print(f"  {key}: Klasse '{cls}' ohne Template → übersprungen")
            continue
        by_class[cls] = by_class.get(cls, 0)
        for case in data.get("damage_cases", []):
            for dmg in case.get("damages", []):
                did = str(dmg.get("damage_id") or dmg.get("damage_number") or "")
                coords = dmg.get("coordinates") or []
                if not did or not coords:
                    n_skip += 1
                    continue
                co = coords[0]
                proj = co.get("projection") or ""
                fname = PROJ_FILE.get(proj.upper())
                tpath = TMPL / cls / fname if fname else None
                if not tpath or not tpath.exists():
                    n_skip += 1
                    continue
                seg = int(_num(co.get("segment_number"), 0))
                x_raw = _num(co.get("x"))
                y_raw = _num(co.get("y"))
                img = Image.open(tpath).convert("RGB")
                w, h = img.size
                # blank ablegen
                dst_dir = OUT / key
                dst_dir.mkdir(parents=True, exist_ok=True)
                img.save(dst_dir / f"{did}__blank.png")
                # Marker: Segment → Zelle im 3x3-Raster, (x,y) Offset in der Zelle
                col = (seg - 1) % GRID if seg else 0
                row = (seg - 1) // GRID if seg else 0
                cw, ch = w / GRID, h / GRID
                px = max(0, min(w - 1, col * cw + x_raw))
                py = max(0, min(h - 1, row * ch + y_raw))
                draw = ImageDraw.Draw(img)
                r = max(9, int(min(w, h) * 0.025))
                draw.ellipse([px - r, py - r, px + r, py + r],
                             outline=(255, 255, 255), width=4)
                draw.ellipse([px - r + 5, py - r + 5, px + r - 5, py + r - 5],
                             fill=(255, 45, 85))
                img.save(dst_dir / f"{did}.png")
                n_ok += 1
                by_class[cls] += 1
    print(f"\nFertig. Gerenderte Schaden-Piktogramme: {n_ok} · übersprungen: {n_skip}")
    print("pro Fahrzeugklasse:", json.dumps(by_class))
    print(f"Ablage: {OUT.relative_to(ROOT)}/<KEY>/<damage_id>.png (+ __blank.png)")


if __name__ == "__main__":
    main()
