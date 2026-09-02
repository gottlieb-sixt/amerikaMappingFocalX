#!/usr/bin/env python3
"""Frische Inspektionen anlegen, damit der Archiv-Trockenlauf Vollbilder sieht.

Alle lokalen Reports sind älter als 7 Tage, ihre `ImageFileURL` liefert 403.
Der Vollbild-Pfad in `archive_probe.py` ist damit ungetestet. Dieses Skript
lädt vorhandene Check-in-Fotos erneut zu FocalX hoch, wartet den Report ab und
schiebt ihn sofort durch den Archiv-Ingest — solange die URLs frisch sind.

Es rührt `data/results/`, `data/reviews/` und `gold/` NICHT an. Reports landen
unter `data/archive_src/`, das Archiv unter `data/archive/`.

Nebenbei beantwortet der Lauf eine Mengenfrage: Ist das Vollbild im Archiv
byte-identisch mit dem, was wir hochgeladen haben, oder kodiert FocalX neu?
Davon hängt ab, ob sich der Speicherbedarf aus der Upload-Größe vorhersagen
lässt. Deshalb sollten die gewählten Autos unterschiedlich große Fotosätze haben.

    python3 -u scripts/archive_live_test.py --dry-run
    python3 -u scripts/archive_live_test.py NV-8777C8__0be22419 TX-WCV5796__8291f6e3
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

from archive.ingest import ingest_report, prefix_for, sha256  # noqa: E402
from archive.store import LocalStore                          # noqa: E402
from eval.focalx import FocalxClient                          # noqa: E402
from eval.pipeline import _env, images_for                    # noqa: E402

SRC = ROOT / "data" / "archive_src"
STORE = LocalStore(ROOT / "data" / "archive")


def run_one(checkin: Path, dry_run: bool) -> dict:
    name = checkin.name
    plate = name.split("__")[0]
    images = images_for(checkin)
    upload_bytes = sum(p.stat().st_size for p in (p for _, p in images))
    if dry_run:
        return {"checkin": name, "bilder": len(images), "upload_MB": upload_bytes / 1e6}

    # Ein Client je Auto: der Token-Zustand ist nicht threadsicher.
    client = FocalxClient(_env("FOCALX_PRECISE_USERNAME"), _env("FOCALX_PRECISE_PASSWORD"))
    t0 = time.time()
    result = client.inspect(plate, images,
                            on_progress=lambda m: print(f"  [{name}] {m}", flush=True))
    dauer = time.time() - t0

    report = result.raw_report
    dest = SRC / name / "focalx_report.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=1, ensure_ascii=False))

    manifest = ingest_report(report, STORE, source=f"api:{result.inspection_id}")

    # Upload-Byte gegen Archiv-Byte, je Slot. Beantwortet, ob FocalX neu kodiert.
    prefix = prefix_for(report)
    identisch = veraendert = fehlend = 0
    for label, src in images:
        gespeichert = STORE.get(
            f"{prefix}/images/{result.inspection_id}_{label}.jpg")
        if gespeichert is None:
            fehlend += 1
        elif sha256(gespeichert) == sha256(src.read_bytes()):
            identisch += 1
        else:
            veraendert += 1

    return {
        "checkin": name,
        "inspection_id": result.inspection_id,
        "dauer_min": dauer / 60,
        "bilder": len(images),
        "upload_MB": upload_bytes / 1e6,
        "orientierungen": manifest["erwartet"]["orientierungen"],
        "findings": manifest["erwartet"]["findings"],
        "archiv_MB": manifest["gespeichert"]["bytes"] / 1e6,
        "vollstaendig": manifest["vollstaendig"],
        "fehlend": len(manifest["fehlend"]),
        "bytes_identisch": identisch,
        "bytes_veraendert": veraendert,
        "bild_fehlt": fehlend,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checkins", nargs="*", help="Ordnernamen unter data/raw/<datum>/")
    ap.add_argument("--dry-run", action="store_true", help="nur zeigen, was liefe")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    alle = {d.name: d for d in sorted(ROOT.glob("data/raw/*/*")) if d.is_dir()}
    gewaehlt = [alle[c] for c in args.checkins if c in alle]
    fehlt = [c for c in args.checkins if c not in alle]
    if fehlt:
        print(f"Unbekannte Check-ins: {', '.join(fehlt)}", file=sys.stderr)
        return 1
    if not gewaehlt:
        print("Keine Check-ins angegeben.", file=sys.stderr)
        return 1

    print(f"{len(gewaehlt)} Inspektion(en), je ~6 min AI-Laufzeit\n")
    with ThreadPoolExecutor(min(args.workers, len(gewaehlt))) as pool:
        ergebnisse = list(pool.map(lambda d: run_one(d, args.dry_run), gewaehlt))

    print("\n=== Ergebnis ===")
    for r in ergebnisse:
        if args.dry_run:
            print(f"{r['checkin']}: {r['bilder']} Bilder, {r['upload_MB']:.1f} MB Upload")
            continue
        print(f"\n{r['checkin']}  ({r['inspection_id']})")
        print(f"  Laufzeit      {r['dauer_min']:.1f} min")
        print(f"  Report        {r['orientierungen']} Ansichten, {r['findings']} Funde")
        print(f"  Upload        {r['upload_MB']:.1f} MB")
        print(f"  Archiv        {r['archiv_MB']:.1f} MB · "
              f"{'vollständig' if r['vollstaendig'] else f'{r['fehlend']} fehlend'}")
        print(f"  Vollbilder    {r['bytes_identisch']} byte-identisch, "
              f"{r['bytes_veraendert']} verändert, {r['bild_fehlt']} nicht gespeichert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
