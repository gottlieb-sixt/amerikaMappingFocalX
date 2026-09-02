#!/usr/bin/env python3
"""Trockenlauf des FocalX-Archivs — derselbe Code, der später gegen S3 läuft.

Drei Phasen:

    local    aus gespeicherten Reports (keine Zugangsdaten nötig; die
             Vollbild-URLs sind nach 7 Tagen tot, das zeigt der Bericht)
    day      Tagesliste bei FocalX abfragen, Reports ziehen, ablegen
    rebuild  Adressen in den Reports neu setzen (nach einem Umzug des Archivs)
    survey   Bestandsbericht über das, was schon im Archiv liegt

    python3 scripts/archive_probe.py local --limit 5
    python3 scripts/archive_probe.py local --limit 5 --again
    python3 scripts/archive_probe.py day 2026-09-02 --dry-run
    python3 scripts/archive_probe.py rebuild
    python3 scripts/archive_probe.py survey

Das Ziel ist standardmäßig `data/archive/`. Mit `--bucket` schreibt derselbe
Ablauf nach S3, sobald es ein Konto und boto3 gibt.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from archive.ingest import archiv_report, ingest_report, plan_objects  # noqa: E402
from archive.source import FocalxSource, day_guard, local_reports  # noqa: E402
from archive.store import LocalStore, S3Store                 # noqa: E402
from archive.survey import collect, render, summarise         # noqa: E402
from eval.pipeline import _env                                # noqa: E402

DATA = ROOT / "data"


def store_from(args):
    if args.bucket:
        return S3Store(args.bucket, args.prefix)
    return LocalStore(DATA / args.root)


def frisch_bytes(m: dict) -> int:
    """Nur das wirklich Geholte. `gespeichert.bytes` zählt auch mit, was schon
    im Archiv lag — als Durchsatzangabe wäre das eine Lüge."""
    return sum(o["bytes"] or 0 for o in m["objects"] if o["status"] == 200)


def zeige(m: dict, dauer: float, vorher: dict | None = None) -> None:
    haken = "vollständig" if m["vollstaendig"] else f"{len(m['fehlend'])} fehlend"
    schon = sum(1 for o in m["objects"] if o["status"] == "vorhanden")
    print(f"{m['registration_number'] or '???'}  {m['inspection_id']}")
    print(f"  Präfix     {m['prefix']}")
    print(f"  Report     {m['report']}")
    print(f"  Objekte    {m['gespeichert']['objekte']}/{m['erwartet']['objekte']} "
          f"· {m['gespeichert']['bytes'] / 1e6:.1f} MB · {haken}"
          + (f" · {schon} schon da" if schon else ""))
    if not m["plate_plausibel"]:
        print(f"  ACHTUNG    Kennzeichen unplausibel: {m['registration_number']!r}")
    if m["fehlend"]:
        gruende: dict[str, int] = {}
        offen = set(m["fehlend"])
        for o in m["objects"]:
            if o["key"] in offen:
                g = o.get("note") or str(o["status"])
                gruende[g] = gruende.get(g, 0) + 1
        print(f"  Grund      {', '.join(f'{k}: {v}' for k, v in gruende.items())}")
    if vorher is not None:
        gleich = (vorher["gespeichert"]["bytes"] == m["gespeichert"]["bytes"]
                  and m["report"] == "unverändert")
        print(f"  Wiederholung {'identisch — nichts neu geschrieben' if gleich else 'ABWEICHUNG'}")
    print(f"  Dauer      {dauer:.1f} s\n")


def cmd_local(args) -> int:
    store = store_from(args)
    quellen = list(local_reports(DATA, args.checkin))[: args.limit]
    if not quellen:
        print("Keine Reports gefunden.", file=sys.stderr)
        return 1
    print(f"Ziel: {store.uri()}  ({len(quellen)} Inspektion(en))\n")
    gesamt = 0
    for report, herkunft in quellen:
        vorher = None
        if args.again:
            from archive.ingest import prefix_for
            alt = store.get(f"{prefix_for(report)}/manifest.json")
            if alt:
                import json
                vorher = json.loads(alt)
        t0 = time.time()
        m = ingest_report(report, store, source=herkunft,
                          download=not args.no_download, workers=args.workers,
                          pruefen=args.pruefen)
        gesamt += m["gespeichert"]["bytes"]
        zeige(m, time.time() - t0, vorher)
    print(f"Summe: {gesamt / 1e6:.1f} MB in {store.uri()}")
    return 0


def cmd_day(args) -> int:
    store = store_from(args)
    quelle = FocalxSource(_env("FOCALX_PRECISE_USERNAME"),
                          _env("FOCALX_PRECISE_PASSWORD"))
    eintraege = quelle.day(args.start, args.end)
    warnung = day_guard(eintraege)
    print(f"Tagesliste {args.start}..{args.end or args.start}: "
          f"{len(eintraege)} Inspektion(en)")
    if warnung:
        print(f"WARNUNG: {warnung}")

    # Ein unfertiger Report hat unvollständige OrientationResults — den zu
    # archivieren hieße, eine Lücke als Ergebnis festzuschreiben.
    fertig = [e for e in eintraege if e.get("inspection_status") == "COMPLETE"]
    uebersprungen = len(eintraege) - len(fertig)
    if uebersprungen:
        print(f"{uebersprungen} nicht COMPLETE — übersprungen")
    if args.limit:
        fertig = fertig[: args.limit]
    if args.dry_run:
        print(f"\n--dry-run: {len(fertig)} Report-Abrufe + Bilddownloads stünden an.")
        return 0

    # Ein Report-Abruf dauerte am 01.09. rund 9 s. Sequenziell wären 425
    # Inspektionen über eine Stunde, bevor das erste Bild geladen ist — der
    # Tageslauf muss also mehrere Autos gleichzeitig bearbeiten. Bewusst
    # zurückhaltend voreingestellt: Das Rate-Limit von FocalX ist unbeantwortet.
    def eins(e: dict) -> tuple[dict | None, float, str]:
        t0 = time.time()
        try:
            report = quelle.report(e["id"])
        except Exception as exc:
            return None, time.time() - t0, f"Report nicht abrufbar — {type(exc).__name__}"
        if not report.get("Completed"):
            return None, time.time() - t0, "Report nicht fertig"
        try:
            m = ingest_report(report, store, source=f"api:{e['id']}",
                              download=not args.no_download, workers=args.workers,
                              pruefen=args.pruefen)
        except Exception as exc:
            # Ein Auto je Vorgang: Ein kaputter Datensatz kostet dieses eine
            # Fahrzeug, nicht den Tageslauf. Vorbild ist scripts/batch.py.
            return None, time.time() - t0, f"Ingest gescheitert — {type(exc).__name__}: {exc}"
        return m, time.time() - t0, ""

    print()
    lauf0 = time.time()
    gesamt = frisch = fehler = 0
    with ThreadPoolExecutor(max(1, args.cars)) as pool:
        for e, (m, dauer, problem) in zip(fertig, pool.map(eins, fertig)):
            if m is None:
                fehler += 1
                print(f"{e['id']}: {problem}\n")
                continue
            gesamt += m["gespeichert"]["bytes"]
            frisch += frisch_bytes(m)
            if not args.quiet:
                zeige(m, dauer)

    laufzeit = time.time() - lauf0
    print(f"Summe: {gesamt / 1e6:.1f} MB in {store.uri()} "
          f"— davon {frisch / 1e6:.1f} MB in diesem Lauf geholt")
    print(f"Laufzeit: {laufzeit / 60:.1f} min für {len(fertig)} Inspektion(en) "
          f"({laufzeit / max(1, len(fertig)):.1f} s je Auto, "
          f"{frisch / 1e6 / max(0.001, laufzeit) * 8:.1f} Mbit/s), {fehler} Fehler")
    return 0


def cmd_rebuild(args) -> int:
    """Die Adressen in den Reports neu setzen, ohne etwas herunterzuladen.

    Nötig, sobald das Archiv umzieht — anderer Bucket, anderes Präfix. Die
    Adressen sind abgeleitet; sie neu zu schreiben kostet nichts und ist
    allemal besser, als dafür Terabytes erneut zu holen. Die Funde bleiben
    dabei unangetastet."""
    store = store_from(args)
    n = 0
    for key in sorted(store.list("v1/")):
        if not key.endswith("/report.json"):
            continue
        prefix = key.rsplit("/", 1)[0]
        report = json.loads(store.get(key))
        # Aus dem Manifest, nicht aus der Ablage: Es weiß auch, welche Objekte
        # zu diesem Report gehören sollten, wenn sie fehlen.
        vorhanden = {o["key"] for o in plan_objects(report)
                     if store.exists(f"{prefix}/{o['key']}")}
        neu = archiv_report(report, store, prefix, vorhanden)
        store.put(key, json.dumps(neu, indent=1, ensure_ascii=False).encode())
        n += 1
        fehlt = len(neu["Archiv"]["fehlend"])
        print(f"  {prefix}  {len(vorhanden)} Adressen"
              + (f", {fehlt} Lücken" if fehlt else ""))
    print(f"\n{n} Report(s) neu verlinkt auf {store.uri()}")
    return 0


def cmd_survey(args) -> int:
    store = store_from(args)
    bericht = summarise(collect(store))
    print(f"Bestand in {store.uri()}\n")
    print(render(bericht))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", help="S3-Bucket statt data/archive/")
    ap.add_argument("--prefix", default="", help="Präfix im Bucket")
    ap.add_argument("--root", default="archive",
                    help="Unterordner in data/ (Vorgabe: archive)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--pruefen", action="store_true",
                    help="vorhandene Objekte zurücklesen und gegen die "
                         "Prüfsumme im Manifest halten (kostet vollen "
                         "Lesezugriff, findet dafür stille Veränderungen)")
    sub = ap.add_subparsers(dest="phase", required=True)

    p = sub.add_parser("local", help="aus gespeicherten Reports")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--checkin", help="Substring-Filter auf den Pfad")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--again", action="store_true", help="Idempotenz prüfen")
    p.set_defaults(func=cmd_local)

    p = sub.add_parser("day", help="Tagesliste bei FocalX")
    p.add_argument("start", help="JJJJ-MM-TT")
    p.add_argument("end", nargs="?", help="JJJJ-MM-TT (Vorgabe: wie start)")
    p.add_argument("--limit", type=int, default=0, help="0 = alle")
    p.add_argument("--cars", type=int, default=3,
                   help="parallel bearbeitete Inspektionen (Vorgabe 3)")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true", help="nur die Zusammenfassung")
    p.set_defaults(func=cmd_day)

    p = sub.add_parser("rebuild", help="Adressen in den Reports neu setzen")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("survey", help="Bestandsbericht")
    p.set_defaults(func=cmd_survey)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
