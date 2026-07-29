"""Rendert zu einem GT-Schaden ein Pictogram mit rotem Ortsmarker.

Quelle der Position = die SHARK-Koordinaten aus der rohen Ground-Truth-JSON
(Feld 20: 3=projection, 4=segment[3x3], 6=x, 7=y) — dieselbe Logik wie
scripts/plot_damage.py, nur als gerastertes PNG (PIL) statt HTML, damit das
Bild dem multimodalen Judge als räumlicher Anker mitgegeben werden kann.

Fail-soft: fehlt PIL, das Pictogram-PNG, die Koordinaten oder die JSON, gibt
render() None zurück — der Aufrufer schickt dann eben nur die Nahaufnahmen.
Ergebnisse werden unter data/plots/picto/<PLATEKEY>/<damage_id>.png gecacht.

Braucht Pillow → v05 mit .venv/bin/python laufen lassen (nicht System-python3).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PICTO_DIR = ROOT / "pictograms" / "suv"          # Fallback-Template (SUV)
PICTO_DB = ROOT / "data" / "pictograms_db"        # echte Templates pro Fahrzeugklasse
GT_PICTO_DIR = ROOT / "data" / "ground_truth_pictogram"  # include_pictogram-Antworten
GT_DIR = ROOT / "data" / "ground_truth"
CACHE = ROOT / "data" / "plots" / "picto"
COMPARE = ROOT / "data" / "plots" / "compare"

PROJECTION_BY_NUMBER = {1: "FRONT_SIDE", 2: "BACK_SIDE", 3: "DRIVER_SIDE", 4: "PASSENGER_SIDE"}
# Projektions-String → Template-Datei im SUV-Fallback (Top.png weicht in der Schreibung ab).
PROJ_FILE = {
    "FRONT_SIDE": "FRONT_SIDE.png", "BACK_SIDE": "BACK_SIDE.png",
    "DRIVER_SIDE": "DRIVER_SIDE.png", "PASSENGER_SIDE": "PASSENGER_SIDE.png",
    "TOP": "Top.png", "TOP_SIDE": "Top.png",
}
# In data/pictograms_db heißen die Dateien einheitlich groß (TOP.png statt Top.png).
PROJ_FILE_DB = {
    "FRONT_SIDE": "FRONT_SIDE.png", "BACK_SIDE": "BACK_SIDE.png",
    "DRIVER_SIDE": "DRIVER_SIDE.png", "PASSENGER_SIDE": "PASSENGER_SIDE.png",
    "TOP": "TOP.png", "TOP_SIDE": "TOP.png",
}
GRID = 3

_CLASS_CACHE: dict[str, str | None] = {}


def _class_for(plate_key: str | None) -> str | None:
    """Fahrzeug-Piktogrammklasse (P_OFF/P_LIM/P_MEH/P_CMP_5 …) aus der
    include_pictogram-Antwort. None, wenn unbekannt → SUV-Fallback."""
    if not plate_key:
        return None
    if plate_key in _CLASS_CACHE:
        return _CLASS_CACHE[plate_key]
    cls = None
    f = GT_PICTO_DIR / f"{plate_key}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text())
            url = (data.get("vehicle_pictogram") or {}).get("original_pictogram") or ""
            m = re.search(r"Damage_Pictograms_All/([^/]+)/", urlparse(url).path)
            cls = m.group(1) if m else None
        except Exception:
            cls = None
    _CLASS_CACHE[plate_key] = cls
    return cls


def _template_path(projection: str, plate_key: str | None) -> Path | None:
    """Bevorzugt das klassen-korrekte Template dieses Autos, sonst SUV-Fallback."""
    proj = (projection or "").upper()
    cls = _class_for(plate_key)
    if cls:
        fname = PROJ_FILE_DB.get(proj)
        if fname:
            cand = PICTO_DB / cls / fname
            if cand.exists():
                return cand
    fname = PROJ_FILE.get(proj)
    if fname and (PICTO_DIR / fname).exists():
        return PICTO_DIR / fname
    return None

GT_GREEN = (46, 158, 91)
LLM_RED = (208, 67, 59)


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


def render(plate_key: str, damage_id: str, scale: float = 1.0) -> Path | None:
    """→ Pfad zum gerenderten Pictogram-PNG (gecacht) oder None (fail-soft)."""
    out = CACHE / plate_key / f"{damage_id}.png"
    if out.exists():
        return out

    gt = GT_DIR / f"{plate_key}.json"
    if not gt.exists():
        return None
    try:
        data = json.loads(gt.read_text())
    except Exception:
        return None

    dm = next((d for d in _iter_damages(data) if str(d.get("3")) == str(damage_id)), None)
    if dm is None:
        return None
    co = _coords(dm)
    try:
        proj_n = int(str(co.get("3", 0) or 0))
        seg_n = int(str(co.get("4", 0) or 0))
        x_raw = float(co.get("6", 0) or 0)
        y_raw = float(co.get("7", 0) or 0)
    except (TypeError, ValueError):
        return None

    proj = PROJECTION_BY_NUMBER.get(proj_n)
    if not proj:
        return None
    png = _template_path(proj, plate_key)
    if png is None:
        return None

    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    img = Image.open(png).convert("RGB")
    w, h = img.size
    # Segment → Zelle im 3x3-Raster; (x, y) sind Offsets innerhalb der Zelle.
    col = (seg_n - 1) % GRID if seg_n else 0
    row = (seg_n - 1) // GRID if seg_n else 0
    cw, ch = w / GRID, h / GRID
    x = col * cw + x_raw * scale
    y = row * ch + y_raw * scale
    x = max(0, min(w - 1, x))
    y = max(0, min(h - 1, y))

    draw = ImageDraw.Draw(img)
    r = max(12, int(min(w, h) * 0.025))
    draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255), width=4)
    draw.ellipse([x - r + 5, y - r + 5, x + r - 5, y + r - 5], fill=(255, 45, 85))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# ── Vergleich LLM-Box vs. DB-Wahrheit ────────────────────────────────────────
# Für GT-Schäden ist die echte Position bekannt (DB-Koordinaten). Diese Helfer
# rendern die Wahrheit (grüner Punkt), die LLM-Vorhersage (rote Box) und ein
# Overlay beider — als visuelle Grundlage, um zu sehen, wie stark das
# Prompt-Mapping von der Wahrheit abweicht.

def _template(projection: str, plate_key: str | None = None):
    """→ (PIL.Image RGB, w, h) der sauberen Vorlage oder None (fail-soft).
    Nutzt das klassen-korrekte Template dieses Autos, sonst SUV-Fallback."""
    src = _template_path(projection, plate_key)
    if src is None:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    img = Image.open(src).convert("RGB")
    return img, img.size[0], img.size[1]


def db_point(plate_key: str, damage_ids: list[str], w: int, h: int):
    """DB-Wahrheit: der erste Schaden mit gültiger Projektion+Segment ergibt
    einen (x, y)-Punkt auf einer w×h-Vorlage. None, wenn nichts brauchbar."""
    gt = GT_DIR / f"{plate_key}.json"
    if not gt.exists():
        return None
    try:
        data = json.loads(gt.read_text())
    except Exception:
        return None
    by_id = {str(d.get("3") or d.get("1")): d for d in _iter_damages(data)}
    for did in damage_ids or []:
        dm = by_id.get(str(did))
        if not dm:
            continue
        co = _coords(dm)
        try:
            seg_n = int(str(co.get("4", 0) or 0))
            x_raw = float(co.get("6", 0) or 0)
            y_raw = float(co.get("7", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not seg_n:
            continue
        col = (seg_n - 1) % GRID
        row = (seg_n - 1) // GRID
        cw, ch = w / GRID, h / GRID
        x = max(0.0, min(w - 1, col * cw + x_raw))
        y = max(0.0, min(h - 1, row * ch + y_raw))
        return x, y
    return None


def _scaled_box(box: dict, src_size, w: int, h: int) -> dict:
    """LLM-Box wurde auf einem Template der Größe src_size lokalisiert; auf ein
    w×h-Template umrechnen, damit sie auf dem klassen-korrekten Piktogramm sitzt."""
    if not box or not src_size:
        return box
    sw, sh = src_size
    if not sw or not sh or (sw == w and sh == h):
        return box
    sx, sy = w / sw, h / sh
    out = dict(box)
    for k, s in (("x", sx), ("y", sy), ("width", sx), ("height", sy)):
        if k in out and out[k] is not None:
            try:
                out[k] = float(out[k]) * s
            except (TypeError, ValueError):
                pass
    return out


def _dot(draw, x, y, color, r=9):
    draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255), width=3)
    draw.ellipse([x - r + 3, y - r + 3, x + r - 3, y + r - 3], fill=color)


def render_gt(plate_key: str, damage_ids: list[str], projection: str,
              cache_id: str) -> Path | None:
    """Vorlage mit grünem DB-Wahrheitspunkt."""
    t = _template(projection, plate_key)
    if t is None:
        return None
    img, w, h = t
    pt = db_point(plate_key, damage_ids, w, h)
    if pt is None:
        return None
    from PIL import ImageDraw
    _dot(ImageDraw.Draw(img), pt[0], pt[1], GT_GREEN, r=max(9, int(min(w, h) * 0.02)))
    out = COMPARE / plate_key / f"{cache_id}__gt.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def render_llm(plate_key: str, projection: str, box: dict,
               cache_id: str, src_size=None) -> Path | None:
    """Vorlage mit roter LLM-Box."""
    t = _template(projection, plate_key)
    if t is None or not box:
        return None
    img, w, h = t
    box = _scaled_box(box, src_size, w, h)
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    x = max(0, min(w, int(box.get("x", 0))))
    y = max(0, min(h, int(box.get("y", 0))))
    x2 = max(0, min(w, x + int(box.get("width", 0))))
    y2 = max(0, min(h, y + int(box.get("height", 0))))
    draw.rectangle([x, y, x2, y2], outline=LLM_RED, width=3)
    label = box.get("label")
    if label:
        try:
            draw.text((x, max(0, y - 11)), str(label)[:40], fill=LLM_RED,
                      font=ImageFont.load_default())
        except Exception:
            pass
    out = COMPARE / plate_key / f"{cache_id}__llm.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def render_overlay(plate_key: str, damage_ids: list[str], projection: str,
                   box: dict, cache_id: str, src_size=None):
    """Beides auf einer Vorlage: grüner DB-Punkt + rote LLM-Box + Verbindungs-
    linie. → (Pfad, distanz_px, distanz_pct_diagonale) oder None."""
    t = _template(projection, plate_key)
    if t is None:
        return None
    img, w, h = t
    box = _scaled_box(box, src_size, w, h)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    dist = dist_pct = None
    cx = cy = None
    if box and all(k in box for k in ("x", "y", "width", "height")):
        x = max(0, min(w, int(box["x"])))
        y = max(0, min(h, int(box["y"])))
        bw, bh = int(box["width"]), int(box["height"])
        draw.rectangle([x, y, min(w, x + bw), min(h, y + bh)], outline=LLM_RED, width=3)
        cx, cy = x + bw / 2, y + bh / 2
    pt = db_point(plate_key, damage_ids, w, h)
    if pt is not None:
        if cx is not None:
            draw.line([cx, cy, pt[0], pt[1]], fill=(120, 120, 120), width=2)
        _dot(draw, pt[0], pt[1], GT_GREEN, r=max(9, int(min(w, h) * 0.02)))
        if cx is not None:
            import math
            dist = math.hypot(cx - pt[0], cy - pt[1])
            dist_pct = 100 * dist / math.hypot(w, h)
    out = COMPARE / plate_key / f"{cache_id}__overlay.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out, dist, dist_pct
