#!/usr/bin/env python3
"""Die Aufbewahrungsregeln am Archiv-Bucket hinterlegen.

    export AWS_PROFILE=focalx-deployer
    ~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_lifecycle.py
    ~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_lifecycle.py --anwenden

Ohne ``--anwenden`` wird nur gezeigt, was gesetzt würde. Das ist Absicht: Eine
Löschregel ist der einzige Teil dieses Aufbaus, der Daten vernichtet, und sie
tut es leise und unwiderruflich.

**Wir löschen nicht selbst — S3 tut es.** Das ist mehr als ein Detail: Eine
Lifecycle-Regel läuft mit den Rechten von S3, nicht mit unseren. Sie
funktioniert deshalb, obwohl unserer Rolle ``s3:DeleteObject`` ausdrücklich
verboten ist. Für den DSGVO-Löschweg auf Verlangen hilft das trotzdem nicht:
Der braucht Löschen *auf Zuruf*, nicht nach Ablauf einer Frist.

Gelöscht wird rollierend — jedes Objekt drei Jahre nach *seiner* Ablage, nicht
zu einem Stichtag.
"""
from __future__ import annotations

import argparse
import json
import sys

JAHRE = 3
TAGE = 1095            # 3 × 365; Schaltjahre spielen bei dieser Frist keine Rolle
KUEHLEN_NACH = 90      # Tage in Standard, bevor es ins günstigere Lager geht
KLEIN = 131072         # 128 KB — darunter lohnt Glacier IR nicht, s. u.
UNVOLLSTAENDIG = 7     # abgebrochene Uploads aufräumen

# Regeln, deren Kennung so beginnt, verwaltet dieses Skript. Alles andere am
# Bucket bleibt unangetastet.
UNSER_PRAEFIX = "focalx-"


def regeln(prefix: str, kuehlen: bool, altlasten: list[str]) -> list[dict]:
    """Der komplette Regelsatz, von kurzlebig nach langlebig gelesen."""
    liste: list[dict] = [
        {
            # Kostet nichts, verhindert aber, dass halb hochgeladene Objekte
            # unsichtbar Speicher belegen und ewig mitbezahlt werden.
            "ID": "focalx-abgebrochene-uploads",
            "Filter": {"Prefix": prefix},
            "Status": "Enabled",
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": UNVOLLSTAENDIG},
        },
    ]
    if kuehlen:
        liste.append({
            # Nach drei Monaten sieht praktisch niemand mehr ein Bild an.
            # Glacier Instant Retrieval liefert trotzdem sofort aus, kostet
            # aber ein Fünftel. Ohne diese Regel wären es statt ~170 $ im
            # Monat rund 640 $.
            #
            # Der Größenfilter hat zwei Gründe: Glacier IR berechnet jedes
            # Objekt mit mindestens 128 KB, kleinere lägen dort also teurer
            # als in Standard. Und er hält Reports und Manifeste (~13–50 KB)
            # von selbst heraus — die sollen jederzeit sofort lesbar bleiben,
            # ohne dass wir das Ablageschema dafür ändern müssten.
            "ID": "focalx-bilder-kuehlen",
            "Filter": {"And": {"Prefix": prefix, "ObjectSizeGreaterThan": KLEIN}},
            "Status": "Enabled",
            "Transitions": [{"Days": KUEHLEN_NACH, "StorageClass": "GLACIER_IR"}],
        })
    liste.append({
        "ID": "focalx-nach-drei-jahren-loeschen",
        "Filter": {"Prefix": prefix},
        "Status": "Enabled",
        "Expiration": {"Days": TAGE},
    })
    for alt in altlasten:
        # Testdaten aus der Bauphase. Von Hand bekommen wir sie nicht weg —
        # `s3:DeleteObject` ist unserer Rolle ausdrücklich verboten. S3 selbst
        # darf es, also erledigt es eine Regel mit der kürzestmöglichen Frist.
        liste.append({
            "ID": f"{UNSER_PRAEFIX}altlast-{alt.strip('/').replace('/', '-')}",
            "Filter": {"Prefix": alt},
            "Status": "Enabled",
            "Expiration": {"Days": 1},
        })
    return liste


def versioniert(s3, bucket: str) -> bool:
    antwort = s3.get_bucket_versioning(Bucket=bucket)
    return antwort.get("Status") == "Enabled"


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default="eu-central-1")
    ap.add_argument("--bucket", default="sixt-focalx-archiv-test-180111006559")
    ap.add_argument("--prefix", default="focalx-push/",
                    help="nur hierunter wird gelöscht; alles daneben bleibt")
    ap.add_argument("--ohne-kuehlen", action="store_true",
                    help="nur löschen, nicht ins günstigere Lager umlagern")
    ap.add_argument("--altlast", action="append", default=[], metavar="PREFIX",
                    help="Testdaten aus der Bauphase, morgen weg (mehrfach)")
    ap.add_argument("--anwenden", action="store_true",
                    help="ohne dieses Wort passiert nichts")
    return ap


def main() -> int:
    args = parser().parse_args()
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("boto3 fehlt — requirements-archive.txt installieren", file=sys.stderr)
        return 2

    s3 = boto3.session.Session(region_name=args.region).client("s3")

    # S3 vergleicht Prefixe zeichenweise, nicht ordnerweise: "focalx-" fasst
    # auch "focalx-push/" an. Eine Regel, die morgen löscht, darf das Archiv
    # unter keinen Umständen streifen.
    for alt in args.altlast:
        if alt.startswith(args.prefix) or args.prefix.startswith(alt):
            print(f"Abbruch: --altlast {alt!r} überschneidet sich mit dem "
                  f"Archiv unter {args.prefix!r}", file=sys.stderr)
            return 4

    neu = regeln(args.prefix, kuehlen=not args.ohne_kuehlen,
                 altlasten=args.altlast)

    if versioniert(s3, args.bucket):
        # Bei Versionierung entsteht beim Ablauf nur eine Löschmarkierung; die
        # eigentlichen Daten blieben als alte Version liegen und würden weiter
        # berechnet. Wer das übersieht, löscht drei Jahre lang nichts.
        print("Achtung: Der Bucket ist versioniert. Für alte Versionen und "
              "Löschmarkierungen braucht es zusätzliche Regeln — hier bewusst "
              "nicht geraten.", file=sys.stderr)
        return 3

    try:
        vorhanden = s3.get_bucket_lifecycle_configuration(
            Bucket=args.bucket)["Rules"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
            raise
        vorhanden = []

    fremd = [r for r in vorhanden
             if not str(r.get("ID", "")).startswith(UNSER_PRAEFIX)]
    if fremd:
        print(f"{len(fremd)} fremde Regel(n) bleiben unangetastet: "
              f"{', '.join(r.get('ID', '?') for r in fremd)}")

    gesamt = fremd + neu
    print(f"\ns3://{args.bucket}\n")
    for regel in neu:
        was = []
        if "Transitions" in regel:
            t = regel["Transitions"][0]
            was.append(f"nach {t['Days']} Tagen → {t['StorageClass']}")
        if "Expiration" in regel:
            tage = regel["Expiration"]["Days"]
            wann = f" ({JAHRE} Jahren)" if tage == TAGE else ""
            was.append(f"nach {tage} Tag(en){wann} gelöscht")
        if "AbortIncompleteMultipartUpload" in regel:
            tage = regel["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"]
            was.append(f"abgebrochene Uploads nach {tage} Tagen weg")
        filt = regel["Filter"].get("And", regel["Filter"])
        grenze = filt.get("ObjectSizeGreaterThan")
        wenn = f", nur über {grenze // 1024} KB" if grenze else ""
        print(f"  {regel['ID']}")
        print(f"      unter {filt.get('Prefix', '')!r}{wenn}")
        for eintrag in was:
            print(f"      {eintrag}")

    if not args.anwenden:
        print(f"\nProbelauf — nichts geändert. Mit --anwenden setzen.")
        print(json.dumps(gesamt, indent=2, ensure_ascii=False))
        return 0

    s3.put_bucket_lifecycle_configuration(
        Bucket=args.bucket, LifecycleConfiguration={"Rules": gesamt})
    print(f"\n{len(neu)} Regel(n) gesetzt, {len(fremd)} fremde behalten.")
    print("S3 räumt einmal täglich auf; die erste Wirkung ist frühestens "
          "morgen sichtbar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
