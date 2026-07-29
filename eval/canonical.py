"""Kanonischer Fahrzeugrahmen — Piktogramm-Punkte projektionsübergreifend vergleichen.

Ein Punkt auf einer der vier Seitenansichten wird zu (L, W, H):
  L = Front 0 → Heck 1 · W = Fahrerseite 0 → Beifahrerseite 1 · H = oben 0 → unten 1.

Damit liegen Eckschäden nah beieinander, die DB und FocalX auf VERSCHIEDENEN
Ansichten verorten (Heck unten-links ↔ Fahrerseite unten-hinten). Genau daran
scheitert der Vergleich innerhalb einer Projektion: bei 17 der 57 echten
Gold-Paare sind sich DB und FocalX über die Ansicht uneinig, und zwar
ausschließlich zwischen benachbarten Ansichten.

Die Front-Richtung der Seitenansichten ist gegen die Gold-Matches kalibriert
(scripts/eval_canonical_matching.py, Grid-Search über die vier Kombinationen):
Fahrerseite mit Front links, Beifahrerseite mit Front rechts — die Ansichten
sind also gespiegelt, wie geometrisch zu erwarten. Eine zusätzlich erlaubte
W-Spiegelung (Seitenverwechslung von FocalX) verschlechtert die Trennschärfe
und ist deshalb NICHT drin.

Braucht PIL (nur für die Templategröße) → mit .venv/bin/python laufen.
"""
from __future__ import annotations

import glob
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from eval import pictogram as picto

ROOT = Path(__file__).resolve().parent.parent
FX_LLM = ROOT / "data" / "pictograms_llm_focalx"

DRIVER_FRONT_LEFT = True
PASSENGER_FRONT_LEFT = False

FACES = ("FRONT_SIDE", "DRIVER_SIDE", "BACK_SIDE", "PASSENGER_SIDE")
FACE_LABEL = {"FRONT_SIDE": "Front", "DRIVER_SIDE": "Fahrerseite",
              "BACK_SIDE": "Heck", "PASSENGER_SIDE": "Beifahrerseite"}


def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate or "").upper()


def canon(projection: str | None, u: float, v: float,
          driver_front_left: bool = DRIVER_FRONT_LEFT,
          passenger_front_left: bool = PASSENGER_FRONT_LEFT):
    """(Ansicht, u∈[0,1] von links, v∈[0,1] von oben) → (L, W, H) oder None.

    Front: Bild-links = Beifahrerseite. Heck: Bild-links = Fahrerseite."""
    if projection == "FRONT_SIDE":
        return (0.0, 1.0 - u, v)
    if projection == "BACK_SIDE":
        return (1.0, u, v)
    if projection == "DRIVER_SIDE":
        return (u if driver_front_left else 1.0 - u, 0.0, v)
    if projection == "PASSENGER_SIDE":
        return (u if passenger_front_left else 1.0 - u, 1.0, v)
    return None


def dist(a, b) -> float:
    """Abstand im kanonischen Rahmen (0 = identisch, 1 = Fahrzeuglänge)."""
    return math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])


# ── Abwicklung für die Darstellung ───────────────────────────────────────────
# Der Streifen läuft einmal um das Auto herum, damit Ecken NEBENEINANDER liegen:
#   Front (Beifahrer→Fahrer) | Fahrerseite (vorn→hinten) | Heck (Fahrer→Beifahrer)
#   | Beifahrerseite (hinten→vorn) — und schließt bei s=4 wieder an s=0 an.
# Die Ansicht wird mitgegeben, weil ein Eckpunkt zu zwei Flächen gehört.

STRIP_FACES = ("FRONT_SIDE", "DRIVER_SIDE", "BACK_SIDE", "PASSENGER_SIDE")


def strip(projection: str | None, u: float, v: float, **kw):
    """(Ansicht, u, v) → (s ∈ [0,4], h ∈ [0,1]) für die Streifendarstellung."""
    p = canon(projection, u, v, **kw)
    if p is None:
        return None
    L, W, H = p
    if projection == "FRONT_SIDE":
        return 1.0 - W, H
    if projection == "DRIVER_SIDE":
        return 1.0 + L, H
    if projection == "BACK_SIDE":
        return 2.0 + W, H
    return 4.0 - L, H                        # PASSENGER_SIDE


# ── Punkte laden ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def template_size(projection: str, pk: str) -> tuple[int, int] | None:
    t = picto._template(projection, pk)
    return (t[1], t[2]) if t else None


def gt_uv(pk: str, damage_ids: tuple[str, ...] | list[str], projection: str):
    """DB-Koordinaten → (u, v) auf der Ansicht. None, wenn nicht verortbar."""
    size = template_size(projection, pk) if projection else None
    if not size:
        return None
    pt = picto.db_point(pk, list(damage_ids), size[0], size[1])
    return None if pt is None else (pt[0] / size[0], pt[1] / size[1])


def gt_point(pk: str, damage_ids, projection: str, **kw):
    """DB-Schaden → kanonischer Punkt oder None."""
    uv = gt_uv(pk, damage_ids, projection)
    return None if uv is None else canon(projection, uv[0], uv[1], **kw)


def gt_strip(pk: str, damage_ids, projection: str, **kw):
    """DB-Schaden → Streifenkoordinate (s, h) oder None."""
    uv = gt_uv(pk, damage_ids, projection)
    return None if uv is None else strip(projection, uv[0], uv[1], **kw)


def fx_records(pk: str, base: Path | None = None, **kw) -> list[dict]:
    """FocalX-Lokalisierungen eines Autos aus data/pictograms_llm_focalx/<KEY>/.

    Ein Record entspricht einem Lokalisierungs-Lauf und kann mehrere Findings
    umfassen (id='F7+F9'), weil nahe Findings dort geclustert wurden.
    base überschreibt das Verzeichnis — nötig für versionierte FocalX-Runs
    (data/runs/<id>/pictograms_llm_focalx), deren Finding-Keys eigene sind.
    → {id, keys:set, projection, uv, point, box, size, localized, reason}"""
    out: list[dict] = []
    for jf in sorted(glob.glob(str((base or FX_LLM) / pk / "*.json"))):
        try:
            r = json.loads(Path(jf).read_text())
        except Exception:
            continue
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        box = r.get("box") or {}
        size = r.get("pictogram_size") or [0, 0]
        uv = point = spt = None
        if all(k in box for k in ("x", "y", "width", "height")) and all(size):
            uv = ((float(box["x"]) + float(box["width"]) / 2) / size[0],
                  (float(box["y"]) + float(box["height"]) / 2) / size[1])
            point = canon(r.get("projection"), uv[0], uv[1], **kw)
            spt = strip(r.get("projection"), uv[0], uv[1], **kw)
        out.append({
            "id": rid,
            "keys": {k.strip() for k in rid.split("+") if k.strip()},
            "projection": r.get("projection"),
            "uv": uv, "point": point, "strip": spt,
            "box": box or None, "size": size,
            "localized": point is not None,
            "reason": r.get("not_localizable_reason"),
        })
    return out


def rank(gt_pt, records: list[dict]) -> list[tuple[float, dict]]:
    """FocalX-Records nach kanonischem Abstand zum DB-Punkt sortiert
    (nur lokalisierte). Das ist die Shortlist, kein Urteil."""
    if gt_pt is None:
        return []
    scored = [(dist(gt_pt, r["point"]), r) for r in records if r["localized"]]
    scored.sort(key=lambda x: x[0])
    return scored


def rank_of(gt_pt, records: list[dict], keys: set[str]) -> int | None:
    """Auf welchem Platz steht das erste Finding aus keys? None = nicht dabei."""
    for i, (_d, r) in enumerate(rank(gt_pt, records), start=1):
        if r["keys"] & keys:
            return i
    return None


# ── Das Gate: Piktogramm-Vorstufe vor dem Judge ──────────────────────────────
# Bewusst grob kalibriert (40, nicht 37,5): die Schwellen sind an denselben 48
# Gold-Matches gemessen, gegen die später gescort wird. Bei τ=40 % erreicht der
# Gate-Recall mit 92 % die geometrische Obergrenze; k=5 und k=6 bringen null
# zusätzlichen Recall, kosten aber Bilder.

TAU = 40.0
K = 4


def rank_clusters(gt_pt, clusters: list[list[str]],
                  fx_by_key: dict[str, dict]) -> list[dict]:
    """Finding-Cluster (aus data/results) nach kanonischem Abstand zum DB-Punkt.

    Abstand eines Clusters = kürzester Abstand seiner verorteten Mitglieder; ein
    Cluster kann über zwei Ansichten verteilt sein. Cluster ohne verortetes
    Mitglied fallen heraus.
    → [{index, keys, dist (in %), closest_key, projection, strip}] aufsteigend."""
    out: list[dict] = []
    if gt_pt is None:
        return out
    for i, ks in enumerate(clusters):
        near = sorted((dist(gt_pt, fx_by_key[k]["point"]), k) for k in ks
                      if k in fx_by_key and fx_by_key[k]["localized"])
        if not near:
            continue
        d, k0 = near[0]
        out.append({"index": i, "keys": sorted(ks), "dist": 100 * d,
                    "closest_key": k0, "projection": fx_by_key[k0]["projection"],
                    "strip": fx_by_key[k0]["strip"]})
    out.sort(key=lambda c: c["dist"])
    return out


def shortlist(gt_pt, clusters: list[list[str]], fx_by_key: dict[str, dict],
              extra_indices=(), tau: float = TAU, k: int = K) -> list[dict]:
    """Vorauswahl für den Judge: Cluster mit Abstand ≤ tau, höchstens k, nach
    Abstand sortiert. extra_indices (z. B. das Bauteilklassen-Gate) werden ohne
    Abstand angehängt — das ist die Union-Variante. Leere Liste heißt: kein
    Kandidat in Reichweite, das Urteil kann ohne KI leer bleiben."""
    out = [c for c in rank_clusters(gt_pt, clusters, fx_by_key)
           if c["dist"] <= tau][:k]
    have = {c["index"] for c in out}
    for i in extra_indices:
        if i in have:
            continue
        out.append({"index": i, "keys": sorted(clusters[i]), "dist": None,
                    "closest_key": None, "projection": None, "strip": None})
    return out
