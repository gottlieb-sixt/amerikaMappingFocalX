"""Parst die numerisch dekodierten Ground-Truth-Antworten (data/ground_truth/)
in Truth-Objekte für den Matcher.

Feld-Alignment (verifiziert gegen die benannten Referenz-JSONs von MKB3317):
  Antwort:  1=meta · 2=damage_cases[] · 3=pictograms
  Case:     2=case_number · 31=damages[]
  Damage:   3=damage_number · 20=coordinates · 39=localized_values
  Coord:    3=projection_number · 4=segment_number · 6=x · 7=y
  Localized:1=part · 2=type · 3=severity · 4=side · 5=group
"""
from __future__ import annotations

import json
from pathlib import Path

from .matcher import Truth

PROJECTION_BY_NUMBER = {1: "FRONT_SIDE", 2: "BACK_SIDE", 3: "DRIVER_SIDE", 4: "PASSENGER_SIDE"}
SEGMENT_BY_NUMBER = {
    1: "TOP_LEFT", 2: "TOP_MID", 3: "TOP_RIGHT",
    4: "MID_LEFT", 5: "MID_MID", 6: "MID_RIGHT",
    7: "BOTTOM_LEFT", 8: "BOTTOM_MID", 9: "BOTTOM_RIGHT",
}


# Benchmark-Scope: nur Exterieur ohne Glas. Raus fliegen Windschutzscheibe /
# Scheiben / Glas / Sonnendach sowie alles Innenraum-bezogene.
EXCLUDE_KEYWORDS = (
    "windscreen", "windshield", "window", "glass", "sunroof",
    "interior", "dashboard", "seat", "cockpit", "boot interior",
)


def is_exterior_non_glass(part: str, group: str = "") -> bool:
    text = f"{part} {group}".lower()
    return not any(k in text for k in EXCLUDE_KEYWORDS)


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def load_truths(path: Path, exterior_only: bool = True) -> list[Truth]:
    data = json.loads(path.read_text())
    out: list[Truth] = []
    for case in _as_list(data.get("2")):
        if not isinstance(case, dict):
            continue
        for d in _as_list(case.get("31")):
            if not isinstance(d, dict):
                continue
            lv = d.get("39") if isinstance(d.get("39"), dict) else {}
            coords = [c for c in _as_list(d.get("20")) if isinstance(c, dict)]
            first = coords[0] if coords else {}
            try:
                proj = PROJECTION_BY_NUMBER.get(int(str(first.get("3", 0))), "")
            except ValueError:
                proj = ""
            try:
                seg = SEGMENT_BY_NUMBER.get(int(str(first.get("4", 0))), "")
            except ValueError:
                seg = ""
            part = str(lv.get("1") or "")
            group = str(lv.get("5") or "")
            if exterior_only and not is_exterior_non_glass(part, group):
                continue
            # Feld 31 = is_repaired (REPAIR_STATUS_TRUE=1; FALSE=0 wird im
            # Binärformat weggelassen). Reparierte Schäden sind nicht mehr am
            # Auto — FocalX kann sie nicht finden, sie zählen nicht als Miss.
            if d.get("31") == 1:
                continue
            out.append(Truth(
                damage_id=str(d.get("3") or d.get("1")),
                part=part,
                damage_type=str(lv.get("2") or ""),
                side_attr=str(lv.get("4") or ""),
                projection=proj,
                segment=seg,
                severity=str(lv.get("3")) if lv.get("3") else None,
                case_number=str(case.get("2") or ""),
            ))
    return out
