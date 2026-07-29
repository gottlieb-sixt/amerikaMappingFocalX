"""LLM-Piktogramm-Lokalisierer für die 20 v01-Autos (GT- ODER FocalX-Schäden).

Für JEDEN Schaden wird das passende Piktogramm mit einem Referenzgitter
versehen, zusammen mit den Fotos + Bauteil/Seite/Projektion an den multimodalen
LLM geschickt (Prompt: pictogram_prompt), die zurückgelieferte Bounding-Box
eingelesen und aufs saubere Piktogramm gezeichnet.

Anders als eval/pictogram.py (deterministischer Marker aus DB-Koordinaten)
bestimmt HIER der LLM die Position aus Fotos + Beschreibung.

Zwei Quellen (--source):
  gt      (Standard) GT-Schäden aus gold/mapping_gold.json + GT-Fotos.
          GT hat DB-Koordinaten → ließe sich auch deterministisch zeichnen
          (eval/pictogram.py); der LLM-Weg dient nur dem Vergleich.
  focalx  FocalX-Schäden (physische Finding-Cluster) aus den Results eines Runs
          + FocalX-Close-ups. HIER ist der Prompt nötig: FocalX liefert KEINE
          Piktogramm-Koordinaten, nur Bauteil/Position + Nahaufnahme.
          Mit --run <id> aus einem versionierten Run (data/runs/<id>/) statt v01.

Gespeichert wird pro Schaden:
  data/pictograms_llm[_focalx]/<PLATEKEY>/<id>.json  (Box + Selbsteinschätzung)
  data/pictograms_llm[_focalx]/<PLATEKEY>/<id>.png   (Piktogramm mit roter Box)
Gitter-Vorlagen (einmal pro Projektion) unter <OUT>/_grids/.

Resümierbar: vorhandene <id>.json werden übersprungen (außer --force).
Braucht Pillow + Netz (VPN) → mit .venv/bin/python laufen lassen.

Aufruf:
  .venv/bin/python scripts/locate_pictograms.py --source focalx --dry-run
  .venv/bin/python scripts/locate_pictograms.py --source focalx FL-07ELXT
  .venv/bin/python scripts/locate_pictograms.py --source focalx   # alle 20 Autos
  .venv/bin/python scripts/locate_pictograms.py --source focalx --run v3
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.judge import MAX_TOKENS, MODEL, _post_with_retry, _text  # noqa: E402
from eval.mapping import _loads_lenient  # noqa: E402
from eval.matcher import finding_side_zone  # noqa: E402
from eval import pictogram as _picto  # noqa: E402  klassen-korrekte Template-Auflösung

GOLD = ROOT / "gold" / "mapping_gold.json"
GT_PHOTOS = ROOT / "data" / "gt_photos"
RESULTS_V01 = ROOT / "data" / "results"
PICTO_DIR = ROOT / "pictograms" / "suv"
OUT_GT = ROOT / "data" / "pictograms_llm"
OUT_FOCALX = ROOT / "data" / "pictograms_llm_focalx"
PROMPT_FILE = ROOT / "pictogram_prompt"
PROMPT_VERSION = "v1"
AI_PAUSE_S = 2.0

# Vehicle-Seite → Piktogramm-Projektion (LHD: Fahrer = links).
PROJ_FROM_SIDE = {"left": "DRIVER_SIDE", "right": "PASSENGER_SIDE",
                  "front": "FRONT_SIDE", "rear": "BACK_SIDE"}
SIDE_LABEL = {"left": "Driver side", "right": "Passenger side",
              "front": "Front", "rear": "Rear"}

# Gitter-Vorlagen sind quellenunabhängig → einmal rendern, von beiden nutzen.
GRIDS = ROOT / "data" / "pictogram_grids"
OUT = OUT_GT   # wird in main() je nach --source gesetzt

# Projektions-String → Template-Dateiname (Top.png weicht in der Schreibung ab).
PROJ_FILE = {
    "FRONT_SIDE": "FRONT_SIDE.png", "BACK_SIDE": "BACK_SIDE.png",
    "DRIVER_SIDE": "DRIVER_SIDE.png", "PASSENGER_SIDE": "PASSENGER_SIDE.png",
    "TOP": "Top.png", "TOP_SIDE": "Top.png",
}


def _plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate or "").upper()


def _api_key() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("LLM_GW_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    import os
    return os.environ.get("LLM_GW_API_KEY", "")


def _img_part(path: Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    b = p.read_bytes()
    uri = f"data:{mime};base64," + base64.b64encode(b).decode()
    return {"type": "image_url", "image_url": {"url": uri}}


def _grid_template(projection: str, plate_key: str | None = None):
    """Piktogramm-Vorlage MIT Referenzgitter (+ PASSENGER/DRIVER-Labels bei
    FRONT_SIDE/BACK_SIDE), pro Fahrzeugklasse+Projektion gerendert und gecacht.
    Nutzt das klassen-korrekte Template dieses Autos (SUV nur als Fallback).
    → (grid_png_path, width, height, template_filename) oder None."""
    src = _picto._template_path(projection, plate_key)
    if src is None:
        return None
    fname = src.name
    cls = _picto._class_for(plate_key) or "suv"
    out = GRIDS / cls / f"{(projection or '').upper()}.png"
    from PIL import Image  # lazy: fail-soft ohne Pillow
    if out.exists():
        w, h = Image.open(out).size
        return out, w, h, fname

    from PIL import ImageDraw, ImageFont
    img = Image.open(src).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    step = 50
    line = (205, 205, 205)
    lab = (90, 90, 90)
    for x in range(0, w, step):
        draw.line([(x, 0), (x, h)], fill=line, width=1)
        if font and x:
            draw.text((x + 1, 1), str(x), fill=lab, font=font)
    for y in range(0, h, step):
        draw.line([(0, y), (w, y)], fill=line, width=1)
        if font and y:
            draw.text((1, y + 1), str(y), fill=lab, font=font)

    # LHD-Seiten-Anker (siehe pictogram_prompt): FRONT_SIDE → DRIVER rechts,
    # PASSENGER links; BACK_SIDE → DRIVER links, PASSENGER rechts.
    proj = (projection or "").upper()
    labels = None
    if proj == "FRONT_SIDE":
        labels = [("PASSENGER", 0.02), ("DRIVER", 0.78)]
    elif proj == "BACK_SIDE":
        labels = [("DRIVER", 0.02), ("PASSENGER", 0.72)]
    if labels and font:
        for text, fx in labels:
            draw.text((int(w * fx), 14), text, fill=(220, 30, 40), font=font)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out, w, h, fname


def _photos_for(plate_key: str, damage_ids: list[str]) -> list[Path]:
    photos: list[Path] = []
    d = GT_PHOTOS / plate_key
    if not d.exists():
        return photos
    for did in damage_ids:
        photos.extend(sorted(d.glob(f"{did}_*.jpg")))
    return photos


def _build_prompt(part, side, projection, fname, w, h) -> str:
    txt = PROMPT_FILE.read_text()
    txt = txt.rstrip()
    if txt.endswith("```"):
        txt = txt[:-3].rstrip()
    repl = {
        "{part}": part or "?", "{side}": side or "?",
        "{projection}": projection or "?", "{pictogram_file}": fname,
        "{pictogram_width}": str(w), "{pictogram_height}": str(h),
        "${prompt_version}": PROMPT_VERSION,
    }
    for k, v in repl.items():
        txt = txt.replace(k, v)
    return txt


def _render_box(projection: str, box: dict, label: str, out_png: Path,
                plate_key: str | None = None) -> None:
    src = _picto._template_path(projection, plate_key)
    if src is None:
        return
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(src).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    x = max(0, min(w, int(box.get("x", 0))))
    y = max(0, min(h, int(box.get("y", 0))))
    bw = int(box.get("width", 0)); bh = int(box.get("height", 0))
    x2 = max(0, min(w, x + bw)); y2 = max(0, min(h, y + bh))
    draw.rectangle([x, y, x2, y2], outline=(255, 45, 85), width=3)
    if label:
        try:
            font = ImageFont.load_default()
            draw.text((x, max(0, y - 11)), label[:40], fill=(255, 45, 85), font=font)
        except Exception:
            pass
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)


def _locate(api_key, grid_png, photos, prompt) -> dict | None:
    content: list[dict] = []
    gp = _img_part(grid_png)
    if gp is None:
        return None
    content.append(gp)                      # Piktogramm MUSS das erste Bild sein
    content.append(_text("Damage photos (in order after the pictogram):"))
    for p in photos[:4]:
        ip = _img_part(p)
        if ip:
            content.append(ip)
    if len(content) == 2:                    # keine Fotos angehängt
        content.append(_text("(No damage photos available for this record.)"))
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    })
    raw = _post_with_retry(api_key, body)
    if raw is None:
        return None
    try:
        return _loads_lenient(raw)
    except Exception:
        return {"_parse_error": raw[:500]}


def _gold_plate_keys() -> set[str]:
    gold = json.loads(GOLD.read_text())
    return {_plate_key(c.get("plate", "")) for c in gold.get("cars", [])}


def _side_from_part(part: str) -> str | None:
    s = (part or "").lower()
    if "left" in s:
        return "left"
    if "right" in s:
        return "right"
    if "rear" in s or "back" in s:
        return "rear"
    if "front" in s or "bonnet" in s or "hood" in s or "bumper" in s:
        return "front"
    return None


# Bauteile, die an der Front bzw. am Heck sitzen — auch wenn ihr Name eine Seite
# nennt. "tail light right" gehört aufs Heck: das Seitenwort sagt nur, welches
# der beiden Rücklichter gemeint ist, nicht welche Ansicht es zeigt.
END_PARTS = {
    "rear": ("trunk", "tailgate", "boot", "tail light", "rear light",
             "number plate rear", "rear window", "rear screen", "spoiler"),
    "front": ("grille", "windscreen", "windshield", "number plate front",
              "radiator", "headlight", "bonnet", "hood"),
}


def _projection_for(part: str, position: str) -> str:
    """Auf WELCHE Piktogramm-Ansicht gehört der Schaden?

    Entscheidend ist das beschädigte Bauteil, nicht der Blickwinkel des Fotos.
    Ein Heck-Diagonalfoto zeigt Seite UND Heck: "fender rear left" gehört auf die
    Fahrerseite, "rear bumper" aufs Heck — beide stammen vom selben Bild. Die
    Foto-Position (via matcher.LABEL_SIDE_ZONE) ist nur der Rückfall für Bauteile
    ohne verwertbaren Namen (z. B. "fuel lid")."""
    p = (part or "").lower()
    # Stoßfänger (samt Spoiler/Abschlussleiste) gehören auf die Stirnansicht, auch
    # wenn eine Seite vermerkt ist — so hält es SHARK selbst (geprüft gegen 187
    # DB-Schäden). FocalX schreibt hier ohnehin kein Seitenwort in den Bauteilnamen.
    if "bumper" in p or "spoiler" in p:
        return PROJ_FROM_SIDE["rear" if ("rear" in p or "loading" in p) else "front"]
    for proj_side, words in END_PARTS.items():
        if any(w in p for w in words):
            return PROJ_FROM_SIDE[proj_side]
    side = _side_from_part(p)
    if side:
        return PROJ_FROM_SIDE[side]
    photo_side, _zone = finding_side_zone(position or "")
    return PROJ_FROM_SIDE.get(photo_side or "", "")


def _iter_records():
    """GT-Schäden der 20 Gold-Autos.
    → dict(plate, plate_key, id, part, side, projection, damage_ids, excluded)."""
    gold = json.loads(GOLD.read_text())
    for car in gold.get("cars", []):
        plate = car.get("plate", "")
        pk = _plate_key(plate)
        for dm in car.get("damages", []):
            yield {
                "plate": plate, "plate_key": pk,
                "id": dm.get("gt_key") or "+".join(dm.get("damage_ids", [])),
                "part": dm.get("part", ""), "side": dm.get("side", ""),
                "projection": dm.get("projection", ""),
                "damage_ids": dm.get("damage_ids", []),
                "excluded": bool(dm.get("excluded")),
                "photos": _photos_for(pk, dm.get("damage_ids", [])),
            }


def _iter_focalx_records(results_dir: Path = RESULTS_V01, restrict_gold: bool = True):
    """FocalX-Schäden = physische Finding-Cluster der Results eines Runs. Für v01
    auf die 20 Gold-Autos beschränkt (Parität zum GT-Set); bei --run zählt, was im
    Run liegt. Projektion aus der Positions-/Bauteil-Seite abgeleitet (FocalX
    liefert keine Piktogramm-Koordinaten).
    → dict(plate, plate_key, id, part, side, projection, finding_keys, photos)."""
    gold_keys = _gold_plate_keys() if restrict_gold else None
    for f in sorted(results_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("skipped"):
            continue
        plate = d.get("plate", "")
        pk = _plate_key(plate)
        if gold_keys is not None and pk not in gold_keys:
            continue
        by_key = {fi.get("key"): fi for fi in d.get("findings", []) if fi.get("key")}
        # Vorrang hat die matching-freie Clusterung aus scripts/cluster_findings.py;
        # sie gilt für den ganzen Run einheitlich. Die `physical`-Sicht stammt aus
        # einem Mapping-Lauf und liegt nur bei einzelnen Autos vor.
        clusters = d.get("finding_clusters")
        if not clusters:
            clusters = (d.get("physical", {}) or {}).get("finding_clusters")
        if not clusters:
            clusters = [[k] for k in by_key]
        for keys in clusters:
            reps = [by_key[k] for k in keys if k in by_key]
            if not reps:
                continue
            rep = reps[0]
            part = (rep.get("part") or "").replace("-", " ")
            projection = _projection_for(part, rep.get("position", ""))
            side = _side_from_part(part) or finding_side_zone(rep.get("position", ""))[0]
            photos = [ROOT / r["closeup"] for r in reps if r.get("closeup")]
            yield {
                "plate": plate, "plate_key": pk,
                "id": "+".join(keys),
                "part": part, "side": SIDE_LABEL.get(side or "", side or ""),
                "projection": projection,
                "finding_keys": list(keys),
                "type": rep.get("type", ""),
                "excluded": False,
                "photos": [p for p in photos if p.exists()],
            }


_META_KEYS = ("plate", "plate_key", "id", "part", "side", "projection",
              "excluded", "damage_ids", "finding_keys", "type")


def _meta(r: dict) -> dict:
    return {k: r[k] for k in _META_KEYS if k in r}


def main() -> None:
    global OUT
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    force = "--force" in argv
    source = "gt"
    if "--source" in argv:
        source = argv[argv.index("--source") + 1]
    run_id = ""
    if "--run" in argv:
        run_id = argv[argv.index("--run") + 1]
    filters = [a for a in argv if not a.startswith("--")]
    filters = [a for a in filters if a not in (source, run_id)]
    limit = 0
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
        filters = [a for a in filters if a != str(limit)]

    if source == "focalx":
        if run_id and run_id != "v1":
            from eval import runs as runs_mod
            res_dir = runs_mod.results_dir(run_id)
            OUT = res_dir.parent / "pictograms_llm_focalx"
            recs = list(_iter_focalx_records(res_dir, restrict_gold=False))
            print(f"Run: {run_id} → {res_dir.relative_to(ROOT)}", flush=True)
        else:
            OUT = OUT_FOCALX
            recs = list(_iter_focalx_records())
        noun = "FocalX-Schäden"
    else:
        OUT = OUT_GT
        recs = list(_iter_records())
        noun = "GT-Schäden"

    if filters:
        recs = [r for r in recs
                if any(f.upper().replace("-", "") in r["plate_key"] for f in filters)]

    todo = []
    for r in recs:
        safe = re.sub(r"[^A-Za-z0-9+_-]", "_", r["id"])
        r["_json"] = OUT / r["plate_key"] / f"{safe}.json"
        r["_png"] = OUT / r["plate_key"] / f"{safe}.png"
        if r["_json"].exists() and not force:
            continue
        todo.append(r)
    if limit:
        todo = todo[:limit]

    n_cars = len({r["plate_key"] for r in recs})
    print(f"Quelle: {source} · Autos: {n_cars} · {noun} gesamt: {len(recs)} · "
          f"zu verarbeiten (offen): {len(todo)}", flush=True)
    if dry:
        by_proj: dict[str, int] = {}
        for r in recs:
            key = r["projection"] or "(leer)"
            by_proj[key] = by_proj.get(key, 0) + 1
        print("Projektionen:", json.dumps(by_proj, ensure_ascii=False), flush=True)
        return

    api_key = _api_key()
    if not api_key:
        print("FEHLER: LLM_GW_API_KEY fehlt (.env).", file=sys.stderr)
        sys.exit(1)

    ok = miss = err = 0
    durations: list[float] = []
    t_start = time.monotonic()
    for i, r in enumerate(todo, 1):
        tag = f"[{i}/{len(todo)}] {r['plate']} {r['id']} ({r['projection'] or '—'})"
        tmpl = _grid_template(r["projection"], r["plate_key"])
        if tmpl is None:
            r["_json"].parent.mkdir(parents=True, exist_ok=True)
            r["_json"].write_text(json.dumps({
                **_meta(r), "box": None, "damages": [],
                "not_localizable_reason": "no pictogram template for projection",
            }, ensure_ascii=False, indent=2))
            miss += 1
            print(f"{tag} → keine Vorlage (gespeichert als not_localizable)", flush=True)
            continue
        grid_png, w, h, fname = tmpl
        photos = r.get("photos", [])
        prompt = _build_prompt(r["part"], r["side"], r["projection"], fname, w, h)
        _t0 = time.monotonic()
        res = _locate(api_key, grid_png, photos, prompt)
        secs = round(time.monotonic() - _t0, 1)
        durations.append(secs)
        if res is None:
            err += 1
            print(f"{tag} → KI-Fehler (kein Urteil, nächster Lauf versucht erneut)", flush=True)
            time.sleep(AI_PAUSE_S)
            continue

        dmgs = res.get("damages") if isinstance(res, dict) else None
        box = dmgs[0] if isinstance(dmgs, list) and dmgs else None
        record = {
            **_meta(r),
            "pictogram_template": fname, "pictogram_size": [w, h],
            "n_photos": len(photos[:4]),
            "seconds": secs,
            "box": box,
            "damages": dmgs or [],
            "not_localizable_reason": res.get("not_localizable_reason") if isinstance(res, dict) else None,
        }
        r["_json"].parent.mkdir(parents=True, exist_ok=True)
        r["_json"].write_text(json.dumps(record, ensure_ascii=False, indent=2))
        if box and all(k in box for k in ("x", "y", "width", "height")):
            try:
                _render_box(r["projection"], box, box.get("label", ""), r["_png"],
                            r["plate_key"])
            except Exception as e:
                print(f"{tag} → Box-Render-Fehler: {e!r}", flush=True)
            ok += 1
            print(f"{tag} → Box {box.get('x')},{box.get('y')} "
                  f"{box.get('width')}x{box.get('height')} "
                  f"[{box.get('part_match','?')}/{box.get('side_match','?')}/"
                  f"{box.get('photo_evidence','?')}/{box.get('sub_part_localized','?')}]", flush=True)
        else:
            miss += 1
            reason = record["not_localizable_reason"] or "keine Box"
            print(f"{tag} → not_localizable: {reason}", flush=True)
        time.sleep(AI_PAUSE_S)

    print(f"\nFertig ({source}). Lokalisiert: {ok} · not_localizable: {miss} · KI-Fehler: {err}", flush=True)
    wall = time.monotonic() - t_start
    if durations:
        ds = sorted(durations)
        med = (ds[len(ds)//2] if len(ds) % 2
               else (ds[len(ds)//2 - 1] + ds[len(ds)//2]) / 2)
        print(f"⏱ ZEIT — Wanduhr {wall/60:.1f} min für {len(durations)} Schäden · "
              f"KI {sum(ds)/60:.1f} min ({100*sum(ds)/wall:.0f} %)", flush=True)
        print(f"  pro Schaden          Ø {sum(ds)/len(ds):.1f} s · median {med:.1f} s "
              f"· min {ds[0]:.1f} s · max {ds[-1]:.1f} s · "
              f"Ø {wall/len(durations):.1f} s Wanduhr (inkl. {AI_PAUSE_S} s Pause)",
              flush=True)
    print(f"Ergebnisse: {OUT.relative_to(ROOT)}/<PLATEKEY>/<id>.json (+ .png)", flush=True)


if __name__ == "__main__":
    main()
