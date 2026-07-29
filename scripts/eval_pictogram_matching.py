"""Wie gut sagt allein der PIKTOGRAMM-ABSTAND ein GT<->FocalX-Match voraus?

Idee (vor jedem KI-Matching): jeder GT-Schaden hat eine deterministische Position
auf dem Piktogramm (DB-Koordinaten), jedes FocalX-Finding eine per-AI lokalisierte
Position (Box-Mittelpunkt). Ist der normierte Abstand klein und die Projektion
gleich, ist es vermutlich derselbe Schaden. Dieses Skript misst, wie tragfaehig
dieses reine Geometrie-Signal ist — als Grundlage fuer ein Gate/Shortlist vor der KI.

Wahrheit: gold/mapping_gold.json → je GT-Schaden finding_keys (menschlich).
GT-Position: eval.pictogram.db_point (Klassen-Template).
FocalX-Position: data/pictograms_llm_focalx/<KEY>/<Fk>.json (Box + pictogram_size).

Nur lesend, aendert nichts. Mit .venv/bin/python laufen (PIL fuer Templategroesse).
"""
from __future__ import annotations

import glob
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval import pictogram as picto  # noqa: E402

GOLD = ROOT / "gold" / "mapping_gold.json"
FX = ROOT / "data" / "pictograms_llm_focalx"

# Normierte-Distanz-Schwellen (% der Template-Diagonale), die wir durchprobieren.
THRESHOLDS = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50]


def _plate_key(plate: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]", "", plate or "").upper()


def _tmpl_size(projection: str, plate_key: str):
    t = picto._template(projection, plate_key)
    return (t[1], t[2]) if t else None


def _load_fx(plate_key: str):
    """→ (records, index).

    records = Liste eindeutiger Lokalisierungs-Records, je {keys:set, projection,
    center|None, size, localized}. Die Lokalisierung clustert nahe Findings zu
    Kombi-Records (id='F7+F9'); Gold referenziert Einzel-Keys → keys enthaelt
    alle Teil-Keys. index bildet jeden Einzel-Key auf seinen Record ab."""
    records: list[dict] = []
    index: dict[str, dict] = {}
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
        center = None
        if box and all(k in box for k in ("x", "y", "width", "height")):
            center = (float(box["x"]) + float(box["width"]) / 2,
                      float(box["y"]) + float(box["height"]) / 2)
        keys = {fk.strip() for fk in str(rid).split("+") if fk.strip()}
        rec = {"keys": keys, "projection": r.get("projection"), "center": center,
               "size": size, "localized": center is not None}
        records.append(rec)
        for fk in keys:
            index[fk] = rec
    return records, index


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _pct(xs, p):
    s = sorted(xs)
    if not s:
        return None
    i = min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))
    return s[i]


def _auc(pos, neg) -> float | None:
    """AUC = P(distanz(pos) < distanz(neg)); kleiner Abstand = Match."""
    if not pos or not neg:
        return None
    # Ueber alle Paare (Mann-Whitney); bei Gleichstand 0.5.
    wins = ties = 0
    for a in pos:
        for b in neg:
            if a < b:
                wins += 1
            elif a == b:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def main() -> None:
    gold = json.loads(GOLD.read_text())

    n_gt = 0                     # GT-Schaeden im Scope (nicht excluded)
    n_gt_pos = 0                 # davon mit >=1 finding_key (echtes Match existiert)
    true_total = 0              # echte (GT,Fk)-Paare
    true_same_proj = 0         # davon gleiche Projektion (Geometrie moeglich)
    true_localized = 0         # davon Fk lokalisiert (Box vorhanden)
    true_usable = 0            # gleiche Projektion UND lokalisiert UND GT-Punkt da

    pos_d: list[float] = []     # normierte Distanzen echter Paare (usable)
    neg_d: list[float] = []     # normierte Distanzen nicht-Match-Paare (same proj)

    # Retrieval: pro GT-Schaden Rangliste der same-proj-Findings nach Distanz.
    retr_total = 0             # GT-Schaeden mit >=1 usable echtem Match
    retr_hit_at = {1: 0, 2: 0, 3: 0}
    retr_true_dist: list[float] = []   # Distanz zum (besten) echten Finding

    for car in gold.get("cars", []):
        plate = car.get("plate", "")
        pk = _plate_key(plate)
        records, index = _load_fx(pk)

        for dm in car.get("damages", []):
            if dm.get("excluded"):
                continue
            n_gt += 1
            proj = dm.get("projection")
            ids = dm.get("damage_ids") or []
            keys = set(dm.get("finding_keys") or [])
            if keys:
                n_gt_pos += 1

            size = _tmpl_size(proj, pk) if proj else None
            gt_pt = None
            if size:
                gt_pt = picto.db_point(pk, ids, size[0], size[1])

            # Wahrheits-Buchhaltung ueber ALLE finding_keys (auch nicht nutzbare).
            for fk in keys:
                true_total += 1
                f = index.get(fk)
                if f and f.get("projection") == proj:
                    true_same_proj += 1
                if f and f.get("localized"):
                    true_localized += 1

            if not size or gt_pt is None:
                continue
            diag = math.hypot(size[0], size[1])

            # Distanzen zu allen same-proj lokalisierten Records dieses Autos.
            ranked = []  # (dist_norm, is_true)
            for rec in records:
                if rec.get("projection") != proj or not rec.get("localized"):
                    continue
                # FocalX-Center ggf. auf GT-Templategroesse skalieren.
                fx_c = rec["center"]
                fw, fh = (rec.get("size") or [size[0], size[1]])
                if fw and fh and (fw != size[0] or fh != size[1]):
                    fx_c = (fx_c[0] * size[0] / fw, fx_c[1] * size[1] / fh)
                dn = 100 * math.hypot(gt_pt[0] - fx_c[0], gt_pt[1] - fx_c[1]) / diag
                is_true = bool(rec["keys"] & keys)
                ranked.append((dn, is_true))
                (pos_d if is_true else neg_d).append(dn)
                if is_true:
                    true_usable += 1

            # Retrieval nur, wenn es ein nutzbares echtes Match gibt.
            if any(t for _, t in ranked) and keys:
                retr_total += 1
                ranked.sort(key=lambda r: r[0])
                for k in (1, 2, 3):
                    if any(t for _, t in ranked[:k]):
                        retr_hit_at[k] += 1
                best_true = min(d for d, t in ranked if t)
                retr_true_dist.append(best_true)

    # ── Ausgabe ──────────────────────────────────────────────────────────────
    print("=" * 68)
    print("PIKTOGRAMM-ABSTAND als Match-Signal (GT deterministisch ↔ FocalX AI)")
    print("=" * 68)
    print(f"GT-Schaeden im Scope (nicht excluded): {n_gt}")
    print(f"  davon mit echtem Match (finding_keys): {n_gt_pos}")
    print(f"Echte (GT,Finding)-Paare gesamt: {true_total}")
    if true_total:
        print(f"  gleiche Projektion:   {true_same_proj:>3} "
              f"({100*true_same_proj/true_total:.0f}%)  ← Geometrie ueberhaupt moeglich")
        print(f"  FocalX lokalisiert:   {true_localized:>3} "
              f"({100*true_localized/true_total:.0f}%)")
        print(f"  nutzbar (proj+loc+GT-Punkt): {true_usable:>3} "
              f"({100*true_usable/true_total:.0f}%)  ← OBERGRENZE Geometrie-Recall")

    print("\n-- Normierte Distanz (% der Diagonale), same-projection-Paare --")
    print(f"  ECHTE Matches   n={len(pos_d):>4}  "
          f"median={_median(pos_d):.1f}  p25={_pct(pos_d,25):.1f}  p75={_pct(pos_d,75):.1f}"
          if pos_d else "  ECHTE Matches: keine")
    print(f"  NICHT-Matches   n={len(neg_d):>4}  "
          f"median={_median(neg_d):.1f}  p25={_pct(neg_d,25):.1f}  p75={_pct(neg_d,75):.1f}"
          if neg_d else "  NICHT-Matches: keine")
    auc = _auc(pos_d, neg_d)
    if auc is not None:
        print(f"  AUC (Trennschaerfe klein=Match): {auc:.3f}  (0.5=Zufall, 1=perfekt)")

    print("\n-- Als GATE: behalte Paare mit Distanz <= Schwelle --")
    print(f"  {'Schwelle%':>9} {'Recall':>7} {'Prec':>6} {'F1':>6}   (Recall = Anteil echter usable-Paare erhalten)")
    for t in THRESHOLDS:
        tp = sum(1 for d in pos_d if d <= t)
        fp = sum(1 for d in neg_d if d <= t)
        rec = tp / len(pos_d) if pos_d else 0
        prec = tp / (tp + fp) if (tp + fp) else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
        print(f"  {t:>8}% {100*rec:>6.0f}% {100*prec:>5.0f}% {100*f1:>5.0f}%")

    print("\n-- Als STANDALONE-MATCHER (Nearest-Neighbor pro GT-Schaden) --")
    if retr_total:
        for k in (1, 2, 3):
            print(f"  Recall@{k} (echtes Finding unter den {k} naechsten): "
                  f"{100*retr_hit_at[k]/retr_total:.0f}%  ({retr_hit_at[k]}/{retr_total})")
        print(f"  Median-Distanz zum echten Finding: {_median(retr_true_dist):.1f}%")
    else:
        print("  keine auswertbaren GT-Schaeden")

    print("\nHinweis: Obergrenze Geometrie-Recall wird durch Projektions-Mismatch")
    print("und nicht lokalisierbare FocalX-Findings begrenzt (siehe oben).")


if __name__ == "__main__":
    main()
