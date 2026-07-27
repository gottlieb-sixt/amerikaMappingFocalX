#!/usr/bin/env python3
"""Zeichnet zu einer Damage-Nummer einen Punkt auf das passende Pictogram.

Sucht die Damage-Nummer (Feld 3) in einer Ground-Truth-JSON, liest die
Koordinaten (Feld 20: 3=projection, 6=x, 7=y), wählt das PNG in
pictograms/suv/<PROJECTION>.png und legt einen Punkt an (x, y) darüber.

Ausgabe = eigenständige HTML (Bild + CSS-Punkt), die im Browser geöffnet wird —
bewusst PIL-frei (stdlib only), damit es mit dem System-python3 läuft.

Nutzung:
  python3 scripts/plot_damage.py 42330579
  python3 scripts/plot_damage.py 42330579 --json data/ground_truth/MKJ2666.json
  python3 scripts/plot_damage.py 42330579 --scale 1.0 --no-open
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PICTO_DIR = ROOT / "pictograms" / "suv"
DEFAULT_JSON = ROOT / "data" / "ground_truth" / "MKJ2666.json"
OUT = ROOT / "data" / "plots"

PROJECTION_BY_NUMBER = {1: "FRONT_SIDE", 2: "BACK_SIDE", 3: "DRIVER_SIDE", 4: "PASSENGER_SIDE"}
# 3x3-Raster (Feld 4). Segment lokalisiert die Zelle, (x, y) sind Offsets darin.
SEGMENT_BY_NUMBER = {
    1: "TOP_LEFT", 2: "TOP_MID", 3: "TOP_RIGHT",
    4: "MID_LEFT", 5: "MID_MID", 6: "MID_RIGHT",
    7: "BOTTOM_LEFT", 8: "BOTTOM_MID", 9: "BOTTOM_RIGHT",
}
GRID_COLS = GRID_ROWS = 3


def png_size(path: Path) -> tuple[int, int]:
    """Breite/Höhe aus dem IHDR-Chunk lesen — ohne PIL."""
    with path.open("rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} ist kein PNG")
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def iter_damages(data: dict):
    """Alle Damages über alle Cases hinweg liefern (Felder tolerant gegen dict/list)."""
    cases = data.get("2") or []
    if isinstance(cases, dict):
        cases = [cases]
    for case in cases:
        dmgs = case.get("31") or []
        if isinstance(dmgs, dict):
            dmgs = [dmgs]
        for dm in dmgs:
            yield dm


def find_damage(data: dict, number: str) -> dict | None:
    for dm in iter_damages(data):
        if str(dm.get("3")) == str(number):
            return dm
    return None


def coords(dm: dict) -> dict:
    co = dm.get("20") or {}
    if isinstance(co, list):
        co = co[0] if co else {}
    return co or {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("number", help="Damage-Nummer (Feld 3)")
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON, help="Ground-Truth-JSON")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="Koordinaten-Skalierung: px = coord * scale (innerhalb der Zelle)")
    ap.add_argument("--grid", action="store_true", help="3x3-Raster einzeichnen (Debug)")
    ap.add_argument("--no-open", action="store_true", help="Browser nicht öffnen")
    args = ap.parse_args()

    if not args.json.exists():
        sys.exit(f"JSON fehlt: {args.json}")
    data = json.loads(args.json.read_text())

    dm = find_damage(data, args.number)
    if dm is None:
        nums = [str(d.get("3")) for d in iter_damages(data)]
        sys.exit(f"Damage {args.number} nicht gefunden. Vorhanden: {', '.join(nums)}")

    co = coords(dm)
    proj_n = int(str(co.get("3", 0) or 0))
    proj = PROJECTION_BY_NUMBER.get(proj_n, "")
    seg_n = int(str(co.get("4", 0) or 0))
    seg = SEGMENT_BY_NUMBER.get(seg_n, "")
    x_raw = float(co.get("6", 0) or 0)
    y_raw = float(co.get("7", 0) or 0)

    png = PICTO_DIR / f"{proj}.png"
    if not png.exists():
        avail = ", ".join(p.name for p in sorted(PICTO_DIR.glob("*.png")))
        sys.exit(f"Pictogram fehlt: {png.name} (projection={proj_n}={proj or '?'}). "
                 f"Vorhanden: {avail}")

    w, h = png_size(png)
    # Segment → Zelle im 3x3-Raster; (x, y) sind Offsets innerhalb der Zelle.
    col = (seg_n - 1) % GRID_COLS if seg_n else 0
    row = (seg_n - 1) // GRID_COLS if seg_n else 0
    cell_w, cell_h = w / GRID_COLS, h / GRID_ROWS
    x = col * cell_w + x_raw * args.scale
    y = row * cell_h + y_raw * args.scale

    print(f"Damage {args.number}: projection={proj} ({proj_n}), segment={seg} ({seg_n})"
          f" → cell(col={col},row={row}) + coord=({x_raw},{y_raw})*{args.scale:g}"
          f" → pixel=({x:.0f},{y:.0f}) auf {png.name} [{w}x{h}]")

    OUT.mkdir(parents=True, exist_ok=True)
    out_html = OUT / f"damage_{args.number}.html"
    b64 = base64.b64encode(png.read_bytes()).decode("ascii")
    img_uri = f"data:image/png;base64,{b64}"

    # Optionales 3x3-Raster (nur Debug via --grid); sonst nur der Punkt.
    grid = ""
    if args.grid:
        lines = []
        for c in range(1, GRID_COLS):
            lines.append(f'<div class="v" style="left:{c*cell_w}px"></div>')
        for r in range(1, GRID_ROWS):
            lines.append(f'<div class="hln" style="top:{r*cell_h}px"></div>')
        grid = "".join(lines)

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Damage {args.number}</title>
<style>
  body {{ margin:0; background:#111; color:#ddd; font:14px system-ui,sans-serif; }}
  .bar {{ padding:8px 12px; }}
  .stage {{ position:relative; display:inline-block; }}
  .stage img {{ display:block; }}
  .v {{ position:absolute; top:0; height:{h}px; width:1px; background:rgba(255,255,255,.25); }}
  .hln {{ position:absolute; left:0; width:{w}px; height:1px; background:rgba(255,255,255,.25); }}
  .dot {{ position:absolute; width:16px; height:16px; margin:-8px 0 0 -8px;
          border-radius:50%; background:#ff2d55; border:2px solid #fff;
          box-shadow:0 0 0 2px rgba(255,45,85,.4), 0 0 8px rgba(0,0,0,.6); }}
  .dot::after {{ content:"{args.number}"; position:absolute; left:20px; top:-2px;
                 white-space:nowrap; background:rgba(0,0,0,.7); padding:1px 5px;
                 border-radius:3px; font-size:11px; }}
</style></head>
<body>
  <div class="bar">Damage <b>{args.number}</b> · projection <b>{proj}</b> ·
       segment <b>{seg}</b> · coord ({x_raw:g}, {y_raw:g}) · scale {args.scale:g}
       → pixel ({x:.0f}, {y:.0f})</div>
  <div class="stage">
    <img src="{img_uri}" width="{w}" height="{h}" alt="{proj}">
    {grid}
    <div class="dot" style="left:{x}px; top:{y}px;"></div>
  </div>
</body></html>
"""
    out_html.write_text(html)
    print(f"Geschrieben: {out_html}")
    if not args.no_open:
        webbrowser.open(out_html.resolve().as_uri())


if __name__ == "__main__":
    main()
