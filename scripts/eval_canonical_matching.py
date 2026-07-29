"""Kanonischer Fahrzeugrahmen-Abstand als GT<->FocalX-Matcher — gegen Gold.

Jeder Piktogramm-Punkt wird in (L, W, H) uebersetzt (L=Front->Heck, W=links->rechts,
H=oben->unten). Damit werden Eckteile, die auf zwei Projektionen liegen koennen
(z.B. FRONT_SIDE unten-links == DRIVER_SIDE unten-vorn), vergleichbar.

Kalibriert die Front-Richtung der Seitenansichten datengetrieben gegen die
Gold-Matches (Grid-Search) und misst dann Trennschaerfe/Retrieval/Recovery.

Nur lesend. Mit .venv/bin/python laufen.
"""
from __future__ import annotations

import glob
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval import pictogram as picto  # noqa: E402
from eval.canonical import canon  # noqa: E402  (o_drv/o_pas = die zwei kwargs)

GOLD = ROOT / "gold" / "mapping_gold.json"
FX = ROOT / "data" / "pictograms_llm_focalx"
SIDE = {"DRIVER_SIDE", "PASSENGER_SIDE"}
FR = {"FRONT_SIDE", "BACK_SIDE"}
THRESHOLDS = [3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40]


def _pk(p):
    return re.sub(r"[^A-Za-z0-9]", "", p or "").upper()


def _tmpl_size(projection, plate_key):
    t = picto._template(projection, plate_key)
    return (t[1], t[2]) if t else None


def _load_fx_records(plate_key):
    """→ Liste eindeutiger Records {keys:set, projection, u, v, localized}."""
    recs = []
    for jf in glob.glob(str(FX / plate_key / "*.json")):
        try:
            r = json.loads(Path(jf).read_text())
        except Exception:
            continue
        rid = r.get("id")
        if not rid:
            continue
        box = r.get("box")
        size = r.get("pictogram_size") or [0, 0]
        u = v = None
        if box and all(k in box for k in ("x", "y", "width", "height")) and all(size):
            u = (float(box["x"]) + float(box["width"]) / 2) / size[0]
            v = (float(box["y"]) + float(box["height"]) / 2) / size[1]
        recs.append({"keys": {k.strip() for k in str(rid).split("+") if k.strip()},
                     "projection": r.get("projection"), "u": u, "v": v,
                     "localized": u is not None})
    return recs


def _build():
    """Pro Auto: GT-Schaeden (u,v,proj) + FocalX-Records (u,v,proj) + Wahrheit."""
    gold = json.loads(GOLD.read_text())
    cars = []
    for car in gold.get("cars", []):
        pk = _pk(car.get("plate", ""))
        gts = []
        for dm in car.get("damages", []):
            if dm.get("excluded"):
                continue
            proj = dm.get("projection")
            ids = dm.get("damage_ids") or []
            keys = set(dm.get("finding_keys") or [])
            size = _tmpl_size(proj, pk) if proj else None
            uv = None
            if size:
                pt = picto.db_point(pk, ids, size[0], size[1])
                if pt:
                    uv = (pt[0] / size[0], pt[1] / size[1])
            gts.append({"gt_key": dm.get("gt_key"), "proj": proj, "uv": uv,
                        "keys": keys, "part": dm.get("part"), "side": dm.get("side")})
        cars.append({"pk": pk, "plate": car.get("plate"), "gts": gts,
                     "fx": _load_fx_records(pk)})
    return cars


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _median(xs):
    s = sorted(xs)
    return None if not s else (s[len(s) // 2] if len(s) % 2
                               else (s[len(s)//2-1] + s[len(s)//2]) / 2)


def _auc(pos, neg):
    if not pos or not neg:
        return None
    w = t = 0
    for a in pos:
        for b in neg:
            if a < b:
                w += 1
            elif a == b:
                t += 1
    return (w + 0.5 * t) / (len(pos) * len(neg))


KS = (1, 2, 3, 4, 5)


def _eval(cars, o_drv, o_pas, mirror=False):
    """→ (pos, neg, hit@k, n_matchable, n_with_usable)."""
    pos, neg = [], []
    hit = {k: 0 for k in KS}
    n_matchable = 0      # GT-Schaeden mit >=1 finding_key (End-to-End-Nenner)
    n_usable = 0         # davon mit >=1 lokalisiertem echten Finding (Ceiling)
    for car in cars:
        for g in car["gts"]:
            if not g["uv"] or not g["proj"]:
                if g["keys"]:
                    n_matchable += 1
                continue
            gc = canon(g["proj"], g["uv"][0], g["uv"][1], o_drv, o_pas)
            if gc is None:
                if g["keys"]:
                    n_matchable += 1
                continue
            ranked = []
            for rec in car["fx"]:
                if not rec["localized"]:
                    continue
                fc = canon(rec["projection"], rec["u"], rec["v"], o_drv, o_pas)
                if fc is None:
                    continue
                d = _dist(gc, fc)
                if mirror:                      # gespiegelte W-Variante zusaetzlich
                    fcm = (fc[0], 1.0 - fc[1], fc[2])
                    d = min(d, _dist(gc, fcm))
                is_true = bool(rec["keys"] & g["keys"])
                ranked.append((d, is_true))
                (pos if is_true else neg).append(d)
            if g["keys"]:
                n_matchable += 1
                if any(t for _, t in ranked):
                    n_usable += 1
                    ranked.sort(key=lambda r: r[0])
                    for k in KS:
                        if any(t for _, t in ranked[:k]):
                            hit[k] += 1
    return pos, neg, hit, n_matchable, n_usable


def main():
    cars = _build()

    # ── Kalibrierung: beste Front-Ausrichtung der Seitenansichten ──
    print("KALIBRIERUNG (Front-Richtung Seitenansicht, Median-Distanz echter Paare):")
    best = None
    for o_drv in (True, False):
        for o_pas in (True, False):
            pos, neg, _, _, _ = _eval(cars, o_drv, o_pas)
            med = _median(pos)
            auc = _auc(pos, neg)
            print(f"  driver_front_left={o_drv!s:>5} passenger_front_left={o_pas!s:>5}"
                  f"  → median_true={med*100:.1f}%  AUC={auc:.3f}  (n_true={len(pos)})")
            score = (auc or 0)
            if best is None or score > best[0]:
                best = (score, o_drv, o_pas)
    _, o_drv, o_pas = best
    print(f"\n→ gewaehlt: driver_front_left={o_drv}, passenger_front_left={o_pas}\n")

    for mirror in (False, True):
        tag = "MIT W-Spiegelung (Seiten-Flip erlaubt)" if mirror else "OHNE Spiegelung"
        pos, neg, hit, n_match, n_usable = _eval(cars, o_drv, o_pas, mirror=mirror)
        print("=" * 64)
        print(f"KANONISCHER ABSTAND — {tag}")
        print("=" * 64)
        print(f"  matchbare GT-Schaeden: {n_match} · davon mit lokal. echtem "
              f"Finding (Ceiling): {n_usable} ({100*n_usable/n_match:.0f}%)")
        print(f"  echte usable-Paare: {len(pos)} · nicht-Match-Paare: {len(neg)}")
        print(f"  Distanz echt median={_median(pos)*100:.1f}%  "
              f"nicht-match median={_median(neg)*100:.1f}%  AUC={_auc(pos,neg):.3f}")
        print(f"\n  {'Schwelle':>8} {'Recall':>7} {'Prec':>6} {'F1':>6}  "
              f"(paarweise, ueber usable-Paare)")
        for th in THRESHOLDS:
            t = th / 100.0
            tp = sum(1 for d in pos if d <= t)
            fp = sum(1 for d in neg if d <= t)
            rec = tp / len(pos) if pos else 0
            prec = tp / (tp + fp) if (tp + fp) else 0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
            print(f"  {th:>7}% {100*rec:>6.0f}% {100*prec:>5.0f}% {100*f1:>5.0f}%")
        print("\n  Shortlist-Recall (echtes Finding unter k naechsten):")
        print(f"    Nenner = alle {n_match} matchbaren GT-Schaeden "
              f"(Ceiling {100*n_usable/n_match:.0f}%):")
        for k in KS:
            print(f"      Recall@{k}: end-to-end {100*hit[k]/n_match:>3.0f}%  "
                  f"| von-usable {100*hit[k]/n_usable:>3.0f}%")
        print()


if __name__ == "__main__":
    main()
