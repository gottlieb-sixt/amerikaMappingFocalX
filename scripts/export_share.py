#!/usr/bin/env python3
"""Weitergabe-Paket: menschlich validiertes Mapping DB-Schaden → FocalX-Finding.

Erzeugt aus den Reviews (data/reviews) + Ergebnissen (data/results) einen
selbst-erklärenden CSV-Satz, den man verschicken kann. Basis und Filter sind
identisch zur 📊-Seite im Dashboard: nur ✔️-abgeschlossene Autos, automatische
Ausschlüsse (repariert / erst nach dem Check-in erfasst) und manuelle
Ausschlüsse sind als Status ausgewiesen statt weggelassen.

  python3 scripts/export_share.py            # → data/export/<datum>/
  python3 scripts/export_share.py --out DIR  # eigenes Zielverzeichnis
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "results"
REVIEWS = ROOT / "data" / "reviews"
GROUND_TRUTH = ROOT / "data" / "ground_truth"
RAW = ROOT / "data" / "raw"

SOURCE_NAMES = {2: "Agent-App (Operation App)", 10: "Damage Gate (Scan-Portal)",
                14: "Versicherung (ASSURED)"}

SIZE_ORDER = ["≤ 0,5 Zoll", "≤ 1 Zoll", "> 1 Zoll", "< 2 Zoll", "2–4 Zoll", "> 4 Zoll",
              "komplett", "ohne Angabe"]
SIZE_RULES = [("0.5 inch", "≤ 0,5 Zoll"), ("up to 1 inch", "≤ 1 Zoll"),
              ("> 1 inch", "> 1 Zoll"), ("< 2 inch", "< 2 Zoll"),
              ("2-4 inch", "2–4 Zoll"), ("> 4 inch", "> 4 Zoll"),
              ("complete", "komplett")]


def size_bucket(severity: str | None) -> str:
    s = (severity or "").lower()
    return next((label for needle, label in SIZE_RULES if needle in s), "ohne Angabe")


def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate or "").upper()


def _cases(key: str) -> list[dict]:
    f = GROUND_TRUTH / f"{key}.json"
    if not f.exists():
        return []
    cases = json.loads(f.read_text()).get("2") or []
    return [cases] if isinstance(cases, dict) else cases


def _damages(case: dict) -> list[dict]:
    dms = case.get("31") or []
    if isinstance(dms, dict):
        dms = [dms]
    return [d for d in dms if isinstance(d, dict)]


def repaired_ids(key: str) -> set[str]:
    """Feld 31 = is_repaired: schon repariert ⇒ am Auto nicht mehr sichtbar."""
    return {str(d.get("3")) for c in _cases(key) for d in _damages(c)
            if d.get("31") == 1}


def late_ids(key: str, checkin: str) -> set[str]:
    """Fall erst NACH dem Check-in-Tag angelegt (Feld 34) ⇒ auf den Fotos unmöglich."""
    day = checkin_day(checkin)
    if not day:
        return set()
    try:
        cutoff = dt.datetime.fromisoformat(f"{day}T23:59:59+02:00").timestamp()
    except ValueError:
        return set()
    out: set[str] = set()
    for c in _cases(key):
        v = c.get("34") or c.get("32")
        try:
            ts = int(str(v.get("1"))) if isinstance(v, dict) else None
        except (TypeError, ValueError):
            ts = None
        if ts and ts > cutoff:
            out |= {str(d.get("3")) for d in _damages(c)}
    return out


def source_map(key: str) -> dict[str, int]:
    """Schadensnummer → source_system des Falls (Case-Feld 11)."""
    out: dict[str, int] = {}
    for c in _cases(key):
        try:
            src = int(str(c.get("11")))
        except (TypeError, ValueError):
            src = -1
        for d in _damages(c):
            out[str(d.get("3"))] = src
    return out


def checkin_day(checkin: str) -> str:
    dirs = sorted(RAW.glob(f"*/{checkin}"))
    return dirs[0].parent.name if dirs else ""


def gt_clusters_of(r: dict) -> list[list[str]]:
    ph = r.get("physical") or {}
    return ph.get("gt_clusters") or [[str(t["damage_id"])] for t in r["truths"]]


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    """UTF-8 mit BOM und ';' — so öffnet Excel (DE) die Datei ohne Import-Dialog."""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


README = """FocalX-Benchmark — menschlich validiertes Mapping
=================================================

Frage: Wie viele der tatsächlich in SHARK dokumentierten Fahrzeugschäden findet
FocalX auf den Check-in-Fotos (19-Positionen-Walkaround, Branch 40139) wieder?

Jede Zeile in schaeden.csv ist ein physischer DB-Schaden, jede Zuordnung zu
einem FocalX-Fund wurde manuell an Foto und Close-up geprüft — es sind keine
KI-Urteile in diesen Zahlen.

Dateien
-------
schaeden.csv            Ein Datensatz je physischem DB-Schaden mit Mapping-Status.
focalx_funde.csv        Alle FocalX-Funde, inkl. der ohne DB-Gegenstück.
zusammenfassung.csv     Trefferquote je Fahrzeug und gesamt.
quote_nach_groesse.csv  Trefferquote nach Schadensgröße (kumuliert).

Spalten in schaeden.csv
-----------------------
Kennzeichen / Check-in-Datum / Check-in-ID   Fahrzeug und Check-in-Vorgang.
Schaden-Nr                DB-Schadensnummer(n). Mehrere Nummern getrennt durch
                          "+" = derselbe physische Schaden, vom Scanner
                          mehrfach erfasst; gezählt wird er genau einmal.
Bauteil / Typ / Seite / Schwere / Ansicht / Segment
                          Angaben aus SHARK (localized values + Koordinaten).
Erfassungsquelle          Welches System den Fall angelegt hat. "Damage Gate"
                          ist das automatische Scan-Portal, das auch kleinste
                          Schäden erfasst.
Status                    "gewertet" = fließt in die Quote ein.
                          "repariert" und "nach Check-in erfasst" werden
                          automatisch ausgeschlossen: der Schaden war zum
                          Zeitpunkt der Fotos nicht (mehr) am Auto.
                          "manuell ausgeschlossen" = beim Review verworfen,
                          Grund steht in der Nachbarspalte.
FocalX gefunden           ja / nein, nur für gewertete Schäden.
FocalX-Funde              Zugeordnete Fund-Kennungen (F1, F2 …), passend zu
                          focalx_funde.csv.
FocalX-Beschreibung       Was FocalX an dieser Stelle gemeldet hat
                          (Bauteil / Typ / Fotoposition).

Zählregeln
----------
- Nur Fahrzeuge, deren Review vollständig abgeschlossen ist.
- Nur Außenhaut ohne Glas und Innenraum — beidseitig, d. h. Glas-/Innenraum-
  Funde von FocalX zählen auch nicht als Fehlalarm.
- Physische Schäden werden gezählt, nicht DB-Zeilen.

Warum die Quote zweimal ausgewiesen ist
---------------------------------------
Das Damage Gate erfasst automatisch auch Kleinstschäden, die auf einem
Walkaround-Foto praktisch nicht sichtbar sind. Die Gesamtquote enthält diese
Schäden, die Spalten "ohne Damage Gate" zeigen die Quote allein auf den
menschlich erfassten Schäden. Beide Zahlen gehören zusammengelesen, ebenso wie
quote_nach_groesse.csv: je größer der Schaden, desto höher die Trefferquote.
(Die Größenleiter ist kumuliert — jede Zeile enthält alle Schäden ihrer Größe
und aller größeren. Schäden ohne Größenangabe in SHARK fehlen dort, in
schaeden.csv und zusammenfassung.csv sind sie enthalten.)

Nicht in diesen Zahlen enthalten
--------------------------------
Die Zuordnung DB-Schaden ↔ FocalX-Fund wird im Projekt zusätzlich von einem
KI-Modell vorgeschlagen. Für diesen Export spielt das keine Rolle: jede hier
ausgewiesene Zuordnung stammt aus dem manuellen Review.
"""


def main() -> None:
    argv = sys.argv[1:]
    out_dir = None
    if "--out" in argv:
        out_dir = Path(argv[argv.index("--out") + 1])
    out = out_dir or ROOT / "data" / "export" / time.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)

    dmg_rows: list[list] = []
    find_rows: list[list] = []
    sum_rows: list[list] = []
    size_stat: dict[tuple[str, bool], list[int]] = {}     # (Größe, Gate) → [gefunden, gesamt]
    n_counted = n_found = 0

    for rev_file in sorted(REVIEWS.glob("*.json")):
        res_file = RESULTS / rev_file.name
        if not res_file.exists():
            continue
        rev = json.loads(rev_file.read_text())
        if not (rev.get("_meta") or {}).get("done"):
            continue
        r = json.loads(res_file.read_text())
        checkin, plate = rev_file.stem, r["plate"]
        key = plate_key(plate)
        day = checkin_day(checkin)
        repaired, late, srcs = repaired_ids(key), late_ids(key, checkin), source_map(key)
        truths = {str(t["damage_id"]): t for t in r["truths"]}
        findings = {f["key"]: f for f in r["findings"]}
        by_ids = {frozenset(k.split("+")): v for k, v in rev.items() if k != "_meta"}

        car_counted = car_found = car_nogate = car_nogate_found = 0
        mapped_keys: dict[str, list[str]] = {}
        for ids in gt_clusters_of(r):
            v = by_ids.get(frozenset(ids), {})
            t = truths.get(ids[0], {})
            chosen = sorted(v.get("human") or [])
            if all(d in repaired for d in ids):
                status, reason = "repariert", "laut SHARK bereits repariert"
            elif all(d in (repaired | late) for d in ids):
                status, reason = ("nach Check-in erfasst",
                                  "Fall erst nach dem Check-in-Tag angelegt")
            elif v.get("verdict") == "excluded":
                status, reason = "manuell ausgeschlossen", (v.get("reason") or "").strip()
            elif not v:
                continue                      # nicht reviewt (kommt bei ✔️ nicht vor)
            else:
                status, reason = "gewertet", ""

            counted = status == "gewertet"
            src = srcs.get(ids[0], -1)
            if counted:
                car_counted += 1
                car_found += bool(chosen)
                if src != 10:
                    car_nogate += 1
                    car_nogate_found += bool(chosen)
                cell = size_stat.setdefault((size_bucket(t.get("severity")), src == 10),
                                            [0, 0])
                cell[0] += bool(chosen)
                cell[1] += 1
                for k in chosen:
                    mapped_keys.setdefault(k, []).append("+".join(ids))
            dmg_rows.append([
                plate, day, checkin, "+".join(ids),
                t.get("part") or "", t.get("damage_type") or "",
                t.get("side_attr") or "", t.get("severity") or "",
                t.get("projection") or "", t.get("segment") or "",
                SOURCE_NAMES.get(src, f"unbekannt ({src})"),
                status, reason,
                ("ja" if chosen else "nein") if counted else "",
                ", ".join(chosen),
                " | ".join(
                    f"{k}: {findings.get(k, {}).get('part') or '?'} / "
                    f"{findings.get(k, {}).get('type') or '?'} / "
                    f"{findings.get(k, {}).get('position') or '?'}" for k in chosen),
                t.get("case_number") or "",
            ])

        for fkey, f in findings.items():
            gt = mapped_keys.get(fkey, [])
            find_rows.append([
                plate, day, checkin, fkey,
                f.get("part") or "", f.get("type") or "", f.get("position") or "",
                "ja" if gt else "nein", ", ".join(gt),
            ])

        n_counted += car_counted
        n_found += car_found
        sum_rows.append([plate, day, checkin, car_counted, car_found,
                         car_counted - car_found,
                         f"{car_found / car_counted:.0%}" if car_counted else "–",
                         car_nogate, car_nogate_found,
                         f"{car_nogate_found / car_nogate:.0%}" if car_nogate else "–",
                         len(findings), sum(1 for k in findings if k in mapped_keys)])

    sum_rows.append(["GESAMT", "", f"{len(sum_rows)} Fahrzeuge", n_counted, n_found,
                     n_counted - n_found,
                     f"{n_found / n_counted:.0%}" if n_counted else "–",
                     sum(int(r_[7]) for r_ in sum_rows),
                     sum(int(r_[8]) for r_ in sum_rows),
                     (f"{sum(int(r_[8]) for r_ in sum_rows) / sum(int(r_[7]) for r_ in sum_rows):.0%}"
                      if sum(int(r_[7]) for r_ in sum_rows) else "–"),
                     sum(int(r_[10]) for r_ in sum_rows),
                     sum(int(r_[11]) for r_ in sum_rows)])

    # Größenleiter: jede Zeile kumuliert alle Schäden ihrer Größe und größer.
    sizes = [b for b in SIZE_ORDER[:6] if any(k[0] == b for k in size_stat)]
    size_rows = []
    for i, sb in enumerate(sizes):
        bigger = set(sizes[i:])
        cells: list = [f"{sb} und größer"]
        for gates in ({True, False}, {False}):
            g = sum(v[0] for k, v in size_stat.items() if k[0] in bigger and k[1] in gates)
            t_ = sum(v[1] for k, v in size_stat.items() if k[0] in bigger and k[1] in gates)
            cells += [g, t_, f"{g / t_:.0%}" if t_ else "–"]
        size_rows.append(cells)

    write_csv(out / "schaeden.csv", [
        "Kennzeichen", "Check-in-Datum", "Check-in-ID", "Schaden-Nr", "Bauteil",
        "Typ", "Seite", "Schwere", "Ansicht", "Segment", "Erfassungsquelle",
        "Status", "Ausschlussgrund", "FocalX gefunden", "FocalX-Funde",
        "FocalX-Beschreibung", "Fall-Nr",
    ], dmg_rows)
    write_csv(out / "focalx_funde.csv", [
        "Kennzeichen", "Check-in-Datum", "Check-in-ID", "Fund", "Bauteil (FocalX)",
        "Typ (FocalX)", "Fotoposition", "DB-Schaden zugeordnet", "Schaden-Nr",
    ], find_rows)
    write_csv(out / "zusammenfassung.csv", [
        "Kennzeichen", "Check-in-Datum", "Check-in-ID", "Gewertete DB-Schäden",
        "Von FocalX gefunden", "Nicht gefunden", "Trefferquote",
        "Gewertet ohne Damage Gate", "Gefunden ohne Damage Gate",
        "Trefferquote ohne Damage Gate",
        "FocalX-Funde gesamt", "davon einem DB-Schaden zugeordnet",
    ], sum_rows)
    write_csv(out / "quote_nach_groesse.csv", [
        "Schadensgröße (kumuliert)", "Gefunden", "Gesamt", "Trefferquote",
        "Gefunden ohne Damage Gate", "Gesamt ohne Damage Gate",
        "Trefferquote ohne Damage Gate",
    ], size_rows)
    (out / "LIESMICH.txt").write_text(README, encoding="utf-8")

    quote = f"{n_found / n_counted:.0%}" if n_counted else "–"
    print(f"{out}")
    print(f"  schaeden.csv            {len(dmg_rows)} Zeilen · {n_counted} gewertet · "
          f"{n_found} gefunden ({quote})")
    print(f"  focalx_funde.csv        {len(find_rows)} Zeilen")
    print(f"  zusammenfassung.csv     {len(sum_rows)} Zeilen")
    print(f"  quote_nach_groesse.csv  {len(size_rows)} Zeilen")


if __name__ == "__main__":
    main()
