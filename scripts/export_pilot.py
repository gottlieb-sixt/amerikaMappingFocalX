#!/usr/bin/env python3
"""Rohdaten-Export des US-Pilotlaufs: DB-Schaden ↔ FocalX-Fund, Zeile für Zeile.

Anders als export_share.py (20 menschlich validierte Autos, aufbereitete Quoten)
gibt dieser Export die ungefilterte Zuordnung des Stapellaufs heraus: jeden
SHARK-Schaden mit dem KI-Fund, dem er zugeordnet wurde, jeden FocalX-Fund ohne
DB-Gegenstück und jeden DB-Schaden ohne KI-Fund. Die Urteile stammen vom
LLM-Judge, NICHT von Menschen — deshalb streng getrennt von der 📊-Seite.

  python3 scripts/export_pilot.py --run fl500
  python3 scripts/export_pilot.py --run fl500 --out /tmp/pilot
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Die Protobuf-Feldkarte (repariert / nach Check-in erfasst / Erfassungsquelle)
# steht in export_share.py. Zweimal pflegen wäre die sichere Quelle für stille
# Abweichungen, deshalb importieren statt kopieren.
_spec = importlib.util.spec_from_file_location(
    "export_share", Path(__file__).resolve().parent / "export_share.py")
share = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(share)

plate_key = share.plate_key
write_csv = share.write_csv
SOURCE_NAMES = share.SOURCE_NAMES

MAPPING_HEADER = [
    "kennzeichen", "checkin_id", "checkin_datum", "zeilentyp",
    "db_schaden_id", "db_schaden_cluster", "db_case_number",
    "db_bauteil", "db_typ", "db_seite", "db_ansicht", "db_segment", "db_schwere",
    "db_fall_erfasst_am", "db_erfassung_relativ_zum_checkin", "db_erfassungsquelle",
    "db_gewertet", "db_ausschlussgrund",
    "ki_fund_keys", "ki_fund_cluster", "ki_bauteil", "ki_typ",
    "ki_fotopositionen", "ki_closeup_dateien",
    "ki_distanz_pct", "naechste_distanz_pct", "kandidaten_geprueft",
    "mapping_urteil", "mapping_konfidenz", "mapping_begruendung",
]

# Eine Zeile je geprüftem Kandidaten. Das ist die Beweisebene: das Urteil in
# mapping_roh.csv ist das Ergebnis, hier steht, WAS der Judge gesehen und über
# jeden einzelnen Kandidaten gesagt hat.
KANDIDATEN_HEADER = [
    "kennzeichen", "checkin_id", "checkin_datum",
    "db_schaden_cluster", "db_bauteil", "db_typ", "db_ansicht", "db_segment",
    "kandidat_nr", "ki_fund_cluster", "ki_bauteil", "ki_typ",
    "ki_fotopositionen", "distanz_pct", "verdict", "als_treffer_gewertet",
    "konfidenz", "begruendung",
]

FAHRZEUG_HEADER = [
    "kennzeichen", "checkin_id", "checkin_datum", "mapping_status",
    "db_schaden_zeilen", "db_schaeden_physisch", "db_schaeden_gewertet",
    "focalx_funde", "focalx_funde_physisch",
    "db_schaeden_mit_mapping", "db_schaeden_ohne_mapping", "ki_funde_ohne_mapping",
    "focalx_inspection_id",
]


# Alle Zeitstempel in derselben Zone lesen wie die ⏰-Ausschlussregel in
# export_share.late_ids (+02:00). Sonst widersprechen sich die Spalten
# "erfassung_relativ" und "gewertet" bei US-Check-ins am Tagesrand.
TZ = dt.timezone(dt.timedelta(hours=2))


def iso(ts: int | None) -> str:
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(int(ts), TZ).isoformat(sep=" ", timespec="seconds")


def erfassung_relativ(ts: int | None, day: str) -> str:
    """Antwortet auf „wurde der Schaden bei DIESEM Check-in aufgenommen?"

    Der Zeitstempel hängt am Fall (Feld 34, Rückfall 32), nicht an der einzelnen
    Schadenszeile — mehrere Schäden eines Falls teilen ihn deshalb."""
    if not ts or not day:
        return "unbekannt"
    d = dt.datetime.fromtimestamp(int(ts), TZ).date().isoformat()
    if d == day:
        return "am Check-in-Tag erfasst"
    return "vor dem Check-in erfasst" if d < day else "nach dem Check-in erfasst"


def load_proposals(run_dir: Path, strategy: str, checkin: str) -> dict | None:
    """gt_key → Urteil, oder None wenn für dieses Auto noch nicht geurteilt wurde."""
    f = run_dir / "strategies" / strategy / f"{checkin}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text()).get("proposals") or {}
    except Exception:
        return None


def main() -> None:
    argv = sys.argv[1:]

    def val(flag: str, default: str) -> str:
        return argv[argv.index(flag) + 1] if flag in argv else default

    run = val("--run", "fl500")
    strategy = val("--strategy", "v08-all-distance")
    run_dir = ROOT / "data" / "runs" / run
    results = run_dir / "results"
    if not results.exists():
        sys.exit(f"{results.relative_to(ROOT)} fehlt — Run-Name prüfen.")
    out = Path(val("--out", "")) if "--out" in argv else (
        ROOT / "data" / "export" / f"pilot-{run}-{time.strftime('%Y-%m-%d')}")
    out.mkdir(parents=True, exist_ok=True)

    map_rows: list[list] = []
    car_rows: list[list] = []
    kand_rows: list[list] = []
    n_judged = n_open = 0

    for f in sorted(results.glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        if r.get("skipped"):
            continue
        checkin, plate = r["checkin"], r["plate"]
        pk = plate_key(plate)
        day = share.checkin_day(checkin)
        late = share.late_ids(pk, checkin)
        src = share.source_map(pk)

        truths = {str(t["damage_id"]): t for t in (r.get("truths") or [])}
        findings = {x["key"]: x for x in (r.get("findings") or [])}
        # Reparierte und Glas-/Innenraum-Schäden sind bereits in load_truths
        # ausgeflogen, ebenso Glas-Funde in der Pipeline — was hier liegt, ist
        # der Benchmark-Scope.
        gt_clusters = r.get("gt_clusters") or [[i] for i in truths]
        f_clusters = r.get("finding_clusters") or [[k] for k in findings]
        cluster_of_key = {k: cl for cl in f_clusters for k in cl}

        props = load_proposals(run_dir, strategy, checkin)
        judged = props is not None
        n_judged += judged
        n_open += (not judged)
        status = "geurteilt" if judged else "Mapping noch offen"

        mapped_keys: set[str] = set()
        n_hit = n_miss = 0

        for cl in gt_clusters:
            ids = [str(i) for i in cl if str(i) in truths]
            if not ids:
                continue
            gt_key = "+".join(sorted(ids))
            p = (props or {}).get(gt_key)
            keys = [k for k in ((p or {}).get("finding_keys") or []) if k in findings]
            mapped_keys |= set(keys)
            via = (p or {}).get("via") or ""
            if not judged:
                typ, urteil = "mapping_offen", ""
            elif keys:
                typ, urteil = "gemappt", via
                n_hit += 1
            elif via in ("ai", "ai_rejected"):
                typ, urteil = "nur_db_schaden", via
                n_miss += 1
            elif p is None:
                # Auto insgesamt geurteilt, dieses Paar aber nie — der Lauf wurde
                # mitten im Auto abgebrochen. Kein Urteil, also auch kein Miss.
                typ, urteil = "urteil_offen", ""
            else:
                # KI-Call fehlgeschlagen (Rate-Limit, Netz). Als eigener Typ,
                # damit die Zahlen sauber bleiben.
                typ, urteil = "urteil_fehlgeschlagen", via
            p = p or {}

            ki_parts = sorted({str(findings[k].get("part") or "") for k in keys})
            ki_types = sorted({str(findings[k].get("type") or "") for k in keys})
            ki_pos = [str(findings[k].get("position") or "") for k in keys]
            ki_cl = sorted({"+".join(sorted(cluster_of_key.get(k, [k]))) for k in keys})
            closeups = [str(findings[k].get("closeup") or "") for k in keys]
            # Die DB-Seite ist für alle Kandidatenzeilen dieselbe: der Judge hat
            # den physischen Schaden beurteilt, nicht die einzelne DB-Zeile.
            t0 = truths[ids[0]]

            # Geometrie-Evidenz: Abstand des zugeordneten Kandidaten und der des
            # nächstliegenden überhaupt. Die zweite Zahl macht die Fehlgriffe
            # lesbar — „nächster Kandidat 6 % entfernt und trotzdem verworfen"
            # ist eine andere Geschichte als „nichts war in der Nähe".
            cand_entries = (p.get("candidates") or [])
            gemappte = {k for k in keys}
            dist_hit = [c.get("distance") for c in cand_entries
                        if c.get("distance") is not None
                        and gemappte & set(c.get("keys") or [])]
            dist_all = [c.get("distance") for c in cand_entries
                        if c.get("distance") is not None]

            for num, c in enumerate(cand_entries, start=1):
                ck_keys = [k for k in (c.get("keys") or []) if k in findings]
                kand_rows.append([
                    plate, checkin, day,
                    gt_key, t0.get("part") or "", t0.get("damage_type") or "",
                    t0.get("projection") or "", t0.get("segment") or "",
                    num, "+".join(sorted(ck_keys)),
                    c.get("part") or "",
                    " / ".join(sorted({str(findings[k].get("type") or "")
                                       for k in ck_keys})),
                    " / ".join(str(findings[k].get("position") or "")
                               for k in ck_keys),
                    "" if c.get("distance") is None else c["distance"],
                    c.get("verdict") or "", "ja" if c.get("match") else "nein",
                    c.get("confidence") or "", (c.get("reason") or "").strip(),
                ])

            for did in ids:
                t = truths[did]
                grund = "nach Check-in erfasst" if did in late else ""
                map_rows.append([
                    plate, checkin, day, typ,
                    did, gt_key, t.get("case_number") or "",
                    t.get("part") or "", t.get("damage_type") or "",
                    t.get("side_attr") or "", t.get("projection") or "",
                    t.get("segment") or "", t.get("severity") or "",
                    iso(t.get("created_at")),
                    erfassung_relativ(t.get("created_at"), day),
                    SOURCE_NAMES.get(src.get(did), f"unbekannt ({src.get(did)})"),
                    "nein" if grund else "ja", grund,
                    "+".join(keys), ", ".join(ki_cl),
                    " / ".join(x for x in ki_parts if x),
                    " / ".join(x for x in ki_types if x),
                    " / ".join(x for x in ki_pos if x),
                    " ".join(x for x in closeups if x),
                    min(dist_hit) if dist_hit else "",
                    min(dist_all) if dist_all else "",
                    len(cand_entries) or "",
                    urteil, p.get("confidence") or "", (p.get("reason") or "").strip(),
                ])

        # FocalX-Funde ohne DB-Gegenstück: das sind die Kandidaten für
        # Falschalarme (oder für Schäden, die in SHARK fehlen).
        n_extra = 0
        for cl in f_clusters:
            keys = [k for k in cl if k in findings]
            if not keys or any(k in mapped_keys for k in keys):
                continue
            n_extra += 1
            cl_key = "+".join(sorted(keys))
            for k in keys:
                x = findings[k]
                map_rows.append([
                    plate, checkin, day,
                    "nur_ki_fund" if judged else "mapping_offen_ki_fund",
                    "", "", "", "", "", "", "", "", "", "", "", "",
                    "", "",
                    k, cl_key, x.get("part") or "", x.get("type") or "",
                    x.get("position") or "", x.get("closeup") or "",
                    "", "", "",
                    "", "", "",
                ])

        gewertet = sum(1 for cl in gt_clusters
                       if any(str(i) in truths and str(i) not in late for i in cl))
        car_rows.append([
            plate, checkin, day, status,
            len(truths), len(gt_clusters), gewertet,
            len(findings), len(f_clusters),
            n_hit, n_miss, n_extra, r.get("inspection_id") or "",
        ])

    for name, header, rows in (("mapping_roh.csv", MAPPING_HEADER, map_rows),
                               ("fahrzeuge.csv", FAHRZEUG_HEADER, car_rows),
                               ("kandidaten_roh.csv", KANDIDATEN_HEADER, kand_rows)):
        # Eine verrutschte Spalte fällt in 14 000 Zeilen niemandem auf, macht
        # aber jede Auswertung falsch. Deshalb hart prüfen statt hoffen.
        bad = next((r for r in rows if len(r) != len(header)), None)
        if bad is not None:
            sys.exit(f"{name}: Zeile hat {len(bad)} Felder, Kopf {len(header)}")
        write_csv(out / name, header, rows)
    (out / "DOKUMENTATION.md").write_text(
        doc_text(run, strategy, map_rows, car_rows, kand_rows), encoding="utf-8")

    print(f"→ {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    print(f"   mapping_roh.csv    {len(map_rows)} Zeilen")
    print(f"   fahrzeuge.csv      {len(car_rows)} Fahrzeuge "
          f"({n_judged} geurteilt, {n_open} Mapping offen)")
    print(f"   kandidaten_roh.csv {len(kand_rows)} geprüfte Kandidaten")
    print(f"   DOKUMENTATION.md   Spaltenreferenz + nachgerechnete Kennzahlen")


def _md_table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def doc_text(run: str, strategy: str, map_rows: list[list], car_rows: list[list],
             kand_rows: list[list]) -> str:
    """Die Doku wird MITGERECHNET, nicht gepflegt.

    Eine handgeschriebene Beschreibung driftet beim nächsten Lauf von den Zahlen
    weg — und genau dann wird sie weitergeschickt. Alles hier Genannte stammt aus
    den Zeilen, die eben geschrieben wurden."""
    M = {name: i for i, name in enumerate(MAPPING_HEADER)}
    F = {name: i for i, name in enumerate(FAHRZEUG_HEADER)}
    K = {name: i for i, name in enumerate(KANDIDATEN_HEADER)}

    def col(rows, header_map, name):
        return [r[header_map[name]] for r in rows]

    # Zeilen zählen ist nicht Schäden zählen: ein physischer Schaden hat je
    # DB-Zeile eine Exportzeile. Deshalb überall über die Cluster entdoppeln.
    db_cluster: dict[tuple, str] = {}
    ki_cluster: dict[tuple, str] = {}
    for r in map_rows:
        if r[M["db_schaden_id"]]:
            db_cluster[(r[M["checkin_id"]], r[M["db_schaden_cluster"]])] = r[M["zeilentyp"]]
        else:
            ki_cluster[(r[M["checkin_id"]], r[M["ki_fund_cluster"]])] = r[M["zeilentyp"]]

    def count(d: dict, *typen: str) -> int:
        return sum(1 for v in d.values() if v in typen)

    types = Counter(col(map_rows, M, "zeilentyp"))
    days = sorted({d for d in col(car_rows, F, "checkin_datum") if d})
    plates = len(set(col(car_rows, F, "kennzeichen")))
    judged = [r for r in car_rows if r[F["mapping_status"]] == "geurteilt"]
    n_open = len(car_rows) - len(judged)
    # Ein offenes Auto ohne DB-Schaden im Scope ist keine ausstehende Arbeit:
    # der Judge hätte nichts zu urteilen. Getrennt ausweisen, sonst liest sich
    # der Rückstand größer, als er ist.
    open_nowork = sum(1 for r in car_rows
                      if r[F["mapping_status"]] != "geurteilt"
                      and int(r[F["db_schaeden_physisch"]]) == 0)

    def total(name: str, rows=None) -> int:
        return sum(int(r[F[name]]) for r in (rows if rows is not None else car_rows))

    gemappt = count(db_cluster, "gemappt")
    nur_db = count(db_cluster, "nur_db_schaden")
    geurteilt_db = gemappt + nur_db
    quote = f"{100 * gemappt / geurteilt_db:.1f} %" if geurteilt_db else "—"
    cars_hit = sum(1 for r in judged if int(r[F["db_schaeden_mit_mapping"]]) > 0)

    typ_tab = _md_table(
        ["zeilentyp", "Bedeutung", "Zeilen", "physische Einheiten"],
        [["`gemappt`", "DB-Schaden mit zugeordnetem FocalX-Fund",
          types.get("gemappt", 0), gemappt],
         ["`nur_db_schaden`", "DB-Schaden, dem die KI keinen Fund zuordnet",
          types.get("nur_db_schaden", 0), nur_db],
         ["`nur_ki_fund`", "FocalX-Fund ohne DB-Gegenstück",
          types.get("nur_ki_fund", 0), count(ki_cluster, "nur_ki_fund")],
         ["`urteil_fehlgeschlagen`", "KI-Call abgebrochen (Netz/Rate-Limit)",
          types.get("urteil_fehlgeschlagen", 0), count(db_cluster, "urteil_fehlgeschlagen")],
         ["`urteil_offen`", "Auto in Arbeit, dieses Paar nie geurteilt",
          types.get("urteil_offen", 0), count(db_cluster, "urteil_offen")],
         ["`mapping_offen`", "DB-Schaden, Auto noch nicht geurteilt",
          types.get("mapping_offen", 0), count(db_cluster, "mapping_offen")],
         ["`mapping_offen_ki_fund`", "FocalX-Fund, Auto noch nicht geurteilt",
          types.get("mapping_offen_ki_fund", 0), count(ki_cluster, "mapping_offen_ki_fund")]])

    # Quelle und Zeitstempel hängen am SHARK-FALL, nicht am physischen Schaden:
    # 162 Cluster bestehen aus Zeilen verschiedener Fälle mit verschiedenen
    # Zeitstempeln (derselbe Schaden Wochen später erneut erfasst). Ein
    # Cluster-Repräsentant wäre willkürlich, also je DB-Zeile zählen.
    def verteilung(name: str) -> str:
        c = Counter(r[M[name]] or "(leer)" for r in map_rows if r[M["db_schaden_id"]])
        return _md_table(["Wert", "DB-Schadenszeilen"],
                         [[k, v] for k, v in c.most_common()])

    # Geometrie-Abdeckung selbst nachrechnen. Der Pilotlauf hat gezeigt, wie
    # teuer eine handgeschriebene Einschränkung ist: sie stand noch da, als die
    # Ursache (fehlendes Pillow im Judge-Interpreter) längst behoben war.
    n_kand = len(kand_rows)
    n_mit_dist = sum(1 for r in kand_rows if r[K["distanz_pct"]] != "")
    geo_pct = f"{100 * n_mit_dist / n_kand:.0f} %" if n_kand else "—"
    treffer_d = [float(r[K["distanz_pct"]]) for r in kand_rows
                 if r[K["distanz_pct"]] != "" and r[K["als_treffer_gewertet"]] == "ja"]
    ablehn_d = [float(r[K["distanz_pct"]]) for r in kand_rows
                if r[K["distanz_pct"]] != "" and r[K["als_treffer_gewertet"]] == "nein"]

    def med(xs: list[float]) -> str:
        if not xs:
            return "—"
        s = sorted(xs)
        return f"{s[len(s) // 2]:.0f} %"

    geo_tab = _md_table(
        ["Kandidatenurteile", "Anzahl", "Median-Abstand"],
        [["mit Geometrie-Evidenz", n_mit_dist, med(treffer_d + ablehn_d)],
         ["ohne (kein Ort geschätzt)", n_kand - n_mit_dist, "—"],
         ["als Treffer gewertet", len(treffer_d), med(treffer_d)],
         ["verworfen", len(ablehn_d), med(ablehn_d)]])

    konf = Counter()
    for (ck, cl), typ in db_cluster.items():
        if typ != "gemappt":
            continue
        konf[next(r[M["mapping_konfidenz"]] or "(leer)" for r in map_rows
                  if r[M["checkin_id"]] == ck and r[M["db_schaden_cluster"]] == cl)] += 1

    return DOC.format(
        run=run, strategy=strategy, erzeugt=time.strftime("%d.%m.%Y"),
        tage=len(days), von=days[0] if days else "?", bis=days[-1] if days else "?",
        cars=len(car_rows), plates=plates, judged=len(judged), open_=n_open,
        open_nowork=open_nowork, open_work=n_open - open_nowork,
        rows=len(map_rows), db_rows=total("db_schaden_zeilen"),
        db_phys=len(db_cluster), fx=total("focalx_funde"),
        fx_phys=len(ki_cluster) + gemappt,
        gemappt=gemappt, nur_db=nur_db, geurteilt_db=geurteilt_db, quote=quote,
        cars_hit=cars_hit, cars_hit_pct=f"{100 * cars_hit / len(judged):.0f}" if judged else "—",
        typ_tab=typ_tab,
        quelle_tab=verteilung("db_erfassungsquelle"),
        erfassung_tab=verteilung("db_erfassung_relativ_zum_checkin"),
        konf_tab=_md_table(["Konfidenz", "Mappings"],
                           [[k, v] for k, v in konf.most_common()]),
        kand=n_kand, geo_pct=geo_pct, geo_tab=geo_tab,
        geo_satz=("Die Geometrie-Evidenz fehlt in diesem Lauf vollständig"
                  if not n_mit_dist else
                  f"{geo_pct} der Kandidatenurteile haben Geometrie-Evidenz"),
        spalten_tab=_md_table(["#", "Spalte", "Bedeutung"],
                              [[i + 1, f"`{n}`", SPALTEN_DOC.get(n, "")]
                               for i, n in enumerate(MAPPING_HEADER)]),
        fahrzeug_tab=_md_table(["#", "Spalte", "Bedeutung"],
                               [[i + 1, f"`{n}`", FAHRZEUG_DOC.get(n, "")]
                                for i, n in enumerate(FAHRZEUG_HEADER)]),
        kandidaten_tab=_md_table(["#", "Spalte", "Bedeutung"],
                                 [[i + 1, f"`{n}`", KANDIDATEN_DOC.get(n, "")]
                                  for i, n in enumerate(KANDIDATEN_HEADER)]))


SPALTEN_DOC = {
    "kennzeichen": "US-Kennzeichen des Fahrzeugs.",
    "checkin_id": "Eindeutiger Check-in-Vorgang: `KENNZEICHEN__hash`. Ein Fahrzeug kann mehrere Check-ins haben.",
    "checkin_datum": "Tag des Check-ins (Ordnername in `data/raw/`), US-Datum.",
    "zeilentyp": "Welcher der Fälle unten — siehe Tabelle „Zeilentypen\".",
    "db_schaden_id": "SHARK-Schadensnummer (`damage_number`, Feld 3). Leer bei `nur_ki_fund`.",
    "db_schaden_cluster": "Alle Schadensnummern desselben physischen Schadens, „+\"-verbunden. **Für physische Zählungen hierüber entdoppeln.**",
    "db_case_number": "SHARK-Fallnummer, zu der der Schaden gehört.",
    "db_bauteil": "Bauteil laut SHARK (`localized_values.part`).",
    "db_typ": "Schadensart laut SHARK (Dent, Scratch, …).",
    "db_seite": "Fahrzeugseite laut SHARK (`side`).",
    "db_ansicht": "Piktogramm-Ansicht aus den Koordinaten: FRONT_SIDE / BACK_SIDE / DRIVER_SIDE / PASSENGER_SIDE.",
    "db_segment": "Segment innerhalb der Ansicht (3×3-Raster, z. B. MID_MID).",
    "db_schwere": "Schwere/Größe laut SHARK, z. B. „< 2 inch superficial\".",
    "db_fall_erfasst_am": "Zeitstempel des SHARK-**Falls** (Feld 34, Rückfall 32) in UTC+02:00 — nicht der einzelnen Schadenszeile. Schäden eines Falls teilen ihn.",
    "db_erfassung_relativ_zum_checkin": "Lage dieses Zeitstempels zum Check-in-Tag: vor / am Check-in-Tag / nach dem Check-in erfasst.",
    "db_erfassungsquelle": "System, das den Fall angelegt hat (Case-Feld 11). „Damage Gate\" ist das automatische Scan-Portal und erfasst auch Kleinstschäden.",
    "db_gewertet": "`nein` = automatisch ausgeschlossen, Grund in der Nachbarspalte.",
    "db_ausschlussgrund": "Derzeit nur „nach Check-in erfasst\". Reparierte Schäden sind gar nicht im Export.",
    "ki_fund_keys": "Zugeordnete FocalX-Fund-Kennungen (`F1`, `F7` …), „+\"-verbunden. Bei `nur_ki_fund` die eine Kennung dieser Zeile.",
    "ki_fund_cluster": "Fund-Cluster: dieselbe Beschädigung auf mehreren Fotos. Zum Entdoppeln der KI-Seite.",
    "ki_bauteil": "Bauteil laut FocalX (eigenes Vokabular, z. B. `door-front-right`).",
    "ki_typ": "Schadensart laut FocalX (dent, scratch, …).",
    "ki_fotopositionen": "Kamera-Slot des Walkarounds. **Sagt, von wo das Foto blickt — nicht, wo der Schaden sitzt.**",
    "ki_closeup_dateien": "Pfade der Close-up-Ausschnitte im Repo (nicht im Export enthalten).",
    "ki_distanz_pct": "Abstand des zugeordneten Funds zum DB-Schadensort auf dem Fahrzeug (0 = gleiche Stelle, 100 = eine Fahrzeuglänge). Leer, wenn nicht zugeordnet oder eine Seite nicht verortbar.",
    "naechste_distanz_pct": "Abstand des NÄCHSTLIEGENDEN geprüften Kandidaten, unabhängig vom Urteil. Bei `nur_db_schaden` die entscheidende Spalte: kleiner Wert = es lag was in der Nähe und der Judge hat es verworfen.",
    "kandidaten_geprueft": "Wie viele FocalX-Fund-Cluster der Judge für diesen DB-Schaden bewertet hat. Jeder davon ist eine Zeile in `kandidaten_roh.csv`.",
    "mapping_urteil": "`ai` = KI hat zugeordnet · `ai_rejected` = KI hat alle Kandidaten verworfen · leer = kein Urteil.",
    "mapping_konfidenz": "Selbsteinschätzung des Judge (high/medium/low), sofern geliefert.",
    "mapping_begruendung": "Freitext-Begründung des Judge, sofern geliefert.",
}

KANDIDATEN_DOC = {
    "kennzeichen": "US-Kennzeichen.",
    "checkin_id": "Check-in-Vorgang.",
    "checkin_datum": "Tag des Check-ins.",
    "db_schaden_cluster": "Der physische DB-Schaden, um den es geht — **Verbindungsschlüssel zu `mapping_roh.csv`** (Spalte `db_schaden_cluster`, zusammen mit `checkin_id`).",
    "db_bauteil": "Bauteil des DB-Schadens laut SHARK.",
    "db_typ": "Schadensart des DB-Schadens laut SHARK.",
    "db_ansicht": "Piktogramm-Ansicht des DB-Schadens.",
    "db_segment": "Segment des DB-Schadens innerhalb der Ansicht.",
    "kandidat_nr": "Nummer, unter der dieser Kandidat dem Judge im Prompt vorlag (1-basiert).",
    "ki_fund_cluster": "Der FocalX-Fund-Cluster dieses Kandidaten.",
    "ki_bauteil": "Bauteil laut FocalX.",
    "ki_typ": "Schadensart laut FocalX.",
    "ki_fotopositionen": "Kamera-Slots, auf denen der Fund sichtbar ist.",
    "distanz_pct": "Abstand zum DB-Schadensort in % der Fahrzeuglänge. Leer = eine der beiden Seiten war nicht auf dem Piktogramm verortbar; der Judge hat dann „no location estimate\" gesehen.",
    "verdict": "Wortlaut des Judge-Urteils zu diesem Kandidaten (z. B. `Duplicate`, `Not Duplicate`).",
    "als_treffer_gewertet": "`ja` = dieses Urteil zählt als Zuordnung. Übersetzt den Freitext oben in die Auswertungslogik.",
    "konfidenz": "Konfidenz des Judge für diesen Kandidaten.",
    "begruendung": "Begründung des Judge für genau diesen Kandidaten — die eigentliche Nachvollziehbarkeit.",
}

FAHRZEUG_DOC = {
    "kennzeichen": "US-Kennzeichen.",
    "checkin_id": "Check-in-Vorgang.",
    "checkin_datum": "Tag des Check-ins.",
    "mapping_status": "`geurteilt` oder `Mapping noch offen`.",
    "db_schaden_zeilen": "DB-Zeilen (Schadensnummern) im Scope.",
    "db_schaeden_physisch": "Physische DB-Schäden nach Entdoppelung.",
    "db_schaeden_gewertet": "Davon nicht automatisch ausgeschlossen.",
    "focalx_funde": "FocalX-Funde im Scope (Einzelfunde).",
    "focalx_funde_physisch": "Fund-Cluster nach Entdoppelung über Fotos.",
    "db_schaeden_mit_mapping": "Physische DB-Schäden mit zugeordnetem Fund.",
    "db_schaeden_ohne_mapping": "Physische DB-Schäden ohne Fund (geurteilt).",
    "ki_funde_ohne_mapping": "Fund-Cluster ohne DB-Gegenstück.",
    "focalx_inspection_id": "FocalX-Inspection-ID für Rückfragen an den Anbieter.",
}

DOC = """# US-Pilot — Rohdaten FocalX-Fund ↔ SHARK-Schaden

Lauf `{run}` · Mapping-Strategie `{strategy}` · Export erzeugt am {erzeugt}

## Worum es geht

Die Frage hinter dem Datensatz: **Wie viele der in SHARK dokumentierten
Fahrzeugschäden findet FocalX auf den Check-in-Fotos wieder?** Dazu wurden echte
Check-in-Walkarounds (19 Kamerapositionen) durch FocalX analysiert und die
FocalX-Funde anschließend von einem LLM gegen die SHARK-Schäden gemappt.

> **Das sind KI-Urteile, keine menschlich validierten Zahlen.** Die Zuordnung
> DB-Schaden ↔ FocalX-Fund stammt hier vom LLM-Judge. Es gibt einen zweiten,
> menschlich geprüften Datensatz über 20 Fahrzeuge — beide Zahlenwelten nicht
> vermischen und nicht gegeneinander rechnen.

## Grundgesamtheit

- **{cars} Check-ins** über **{plates} Fahrzeuge**, {tage} Check-in-Tage
  ({von} bis {bis}).
- FocalX ist für alle {cars} Check-ins durchgelaufen.
- **{judged} davon sind gemappt**, **{open_} noch nicht** (Spalte
  `mapping_status` in `fahrzeuge.csv`, Zeilentypen `mapping_offen*`).
- Enthalten sind nur Check-ins, für die SHARK mindestens einen Außenhaut-Schaden
  kennt und für die Walkaround-Fotos vorliegen. Check-ins ohne dokumentierten
  Schaden wurden nicht analysiert.

**Scope beidseitig:** nur Außenhaut ohne Glas und Innenraum. Glas-, Scheiben- und
Innenraum-Funde von FocalX sind ebenso ausgefiltert wie die entsprechenden
DB-Schäden — sonst zählte FocalX Funde als Fehlalarm, die nie zur Aufgabe
gehörten. **Reparierte Schäden** (SHARK `is_repaired`) sind gar nicht enthalten:
sie waren zum Zeitpunkt der Fotos nicht mehr am Fahrzeug.

## Dateien

| Datei | Inhalt |
|---|---|
| `mapping_roh.csv` | {rows} Zeilen — die Rohzuordnung, eine Zeile je DB-Schadensnummer bzw. je FocalX-Fund |
| `kandidaten_roh.csv` | {kand} Zeilen — jedes Einzelurteil des Judge: welchen Kandidaten er für welchen DB-Schaden geprüft und mit welcher Begründung angenommen oder verworfen hat |
| `fahrzeuge.csv` | {cars} Zeilen — ein Check-in je Zeile mit Zählern und Status |
| `DOKUMENTATION.md` | dieses Dokument |

`mapping_roh.csv` beantwortet „was kam heraus", `kandidaten_roh.csv` „warum".
Verbunden werden beide über `checkin_id` + `db_schaden_cluster`.

CSV-Format: UTF-8 mit BOM, Semikolon als Trennzeichen — Excel (DE) öffnet das
ohne Import-Dialog.

## Zeilentypen — die drei gefragten Fälle

{typ_tab}

„Zeilen" ≠ „physische Einheiten": ein physischer Schaden, den der Scanner
mehrfach erfasst hat, belegt mehrere Zeilen. Die rechte Spalte ist über
`db_schaden_cluster` bzw. `ki_fund_cluster` entdoppelt.

## Kennzahlen

- **{gemappt} von {geurteilt_db} geurteilten physischen DB-Schäden** wurden einem
  FocalX-Fund zugeordnet = **{quote}**.
- {nur_db} DB-Schäden blieben ohne Fund.
- {cars_hit} der {judged} geurteilten Fahrzeuge ({cars_hit_pct} %) haben
  mindestens ein Mapping.
- Rohvolumen: {db_rows} DB-Schadenszeilen ({db_phys} physisch) und {fx}
  FocalX-Funde ({fx_phys} physisch).

### Erfassungszeitpunkt der DB-Schäden

{erfassung_tab}

Damit ist die Frage „wurde die Schadens-ID bei diesem Check-in aufgenommen?"
beantwortbar: „am Check-in-Tag erfasst" heißt ja, „vor dem Check-in erfasst"
heißt Vorschaden. **„nach dem Check-in erfasst" kommt in diesem Datensatz nicht
vor** — die SHARK-Abfrage ist auf den Check-in-Zeitpunkt geschnitten, kein Fall
im Export ist jünger als sein Check-in-Tag. Die entsprechende Ausschlussregel
greift hier also nicht.

### Erfassungsquelle

{quelle_tab}

Das Damage Gate ist ein automatisches Scan-Portal und erfasst auch Schäden, die
auf einem Walkaround-Foto praktisch nicht sichtbar sind. Für eine faire
Bewertung von FocalX lohnt eine getrennte Auswertung mit und ohne diese Quelle.

### Konfidenz der Mappings

{konf_tab}

### Geometrie-Evidenz

Die Strategie zeigt dem Judge das Piktogramm des DB-Schadens und je Kandidat den
Abstand zu dieser Stelle (0 % = gleicher Punkt, 100 % = eine Fahrzeuglänge).
Stand dieses Exports: **{geo_satz}.**

{geo_tab}

Der Abstand ist Evidenz, kein Filter — der Judge darf einen weit entfernten
Kandidaten annehmen und einen nahen verwerfen. Genau diese Fälle findest du über
`naechste_distanz_pct` in `mapping_roh.csv`.

## Spalten in `mapping_roh.csv`

{spalten_tab}

## Spalten in `kandidaten_roh.csv`

{kandidaten_tab}

## Spalten in `fahrzeuge.csv`

{fahrzeug_tab}

## Zählregeln

1. **Physisch zählen, nicht Zeilen.** Über `db_schaden_cluster` bzw.
   `ki_fund_cluster` entdoppeln.
2. **Nur `mapping_status = geurteilt`** auswerten. Die {open_} offenen Fahrzeuge
   sind vollständig enthalten, damit die Grundgesamtheit stimmt — ihre Schäden
   sind aber weder Treffer noch Miss.
3. `urteil_fehlgeschlagen` und `urteil_offen` sind **keine** Miss: hier hat die
   KI nichts gesagt. Aus dem Nenner nehmen.
4. `nur_ki_fund` ist **kein** bewiesener Fehlalarm. Es kann auch ein echter
   Schaden sein, der in SHARK fehlt — das unterscheidet nur ein Mensch am Bild.

## Einschränkungen

1. **KI-Urteile, nicht validiert.** Siehe Kasten oben.
2. **Geometrie-Abdeckung: {geo_satz}.** Wo die Spalte `distanz_pct` leer ist,
   hat der Judge nur Bilder und Bauteilnamen gesehen — entweder weil der
   FocalX-Fund nicht auf dem Piktogramm verortet werden konnte oder weil der
   DB-Schaden keine brauchbaren Koordinaten hat. Bei niedriger Abdeckung ist die
   Trefferquote von {quote} die **Untergrenze** dessen, was die Strategie
   leisten kann.
3. **{open_} von {cars} Check-ins sind noch nicht gemappt.** Bei {open_nowork}
   davon kennt SHARK keinen Schaden im Scope — dort gibt es nichts zuzuordnen,
   die restlichen {open_work} warten auf den Judge.
4. **Fotoposition ≠ Schadensposition.** `ki_fotopositionen` sagt, von wo die
   Kamera blickte. Ein Heck-Diagonalfoto zeigt Heck und Seite gleichzeitig.
5. **Zeitstempel am Fall, nicht am Schaden.** `db_fall_erfasst_am` gilt für alle
   Schäden eines SHARK-Falls. Angegeben in UTC+02:00, während der Check-in-Tag
   ein US-Datum ist — bei Check-ins am späten Abend Ortszeit kann das um einen
   Tag verschoben wirken.
6. **Close-up-Bilder sind nicht enthalten**, nur ihre Pfade im Repo.

## Reproduzieren

```bash
python3 scripts/export_pilot.py --run {run} --strategy {strategy}
```
"""




if __name__ == "__main__":
    main()
