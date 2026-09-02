#!/usr/bin/env python3
"""Beweist, dass derselbe Ingest nach S3 schreibt wie auf die Platte.

Der Tageslauf vom 04.08. hat die Kette bis zur lokalen Datei geprüft — aber
`S3Store` war nie ausgeführt. Dieser Test schließt die Lücke ohne AWS-Konto:
`moto` stellt ein S3 im Arbeitsspeicher bereit, das dieselbe boto3-Schnittstelle
bedient wie das echte.

Die Bilder kommen aus dem lokalen Probearchiv, die Downloads sind abgeklemmt.
Der Test braucht also kein Netz und liefert bei jedem Lauf dasselbe Ergebnis.

    ~/.cache/focalx-s3venv/bin/python scripts/archive_s3_test.py

Braucht boto3 und moto — bewusst nicht in requirements.txt, weil das Dashboard
beides nicht kennt.
"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive import ingest as ing
from archive.ingest import ingest_report, plan_objects, prefix_for, sha256
from archive.store import LocalStore, S3Store
from archive.survey import collect, summarise

QUELLE = Path("data/results")
BUCKET = "sixt-focalx-archiv-test"
PROBEN = 6

geprueft = 0


def pruefe(bedingung: bool, was: str) -> None:
    global geprueft
    geprueft += 1
    if not bedingung:
        raise AssertionError(was)
    print(f"  ✓ {was}")


@contextmanager
def ohne_netz(bilder: dict[str, bytes]):
    """Downloads durch das lokale Probearchiv ersetzen.

    So läuft `_fetch` samt Fehlerbehandlung und Ablage im Original — nur die
    Bytes kommen von der Platte statt von CloudFront."""
    class Antwort:
        def __init__(self, payload): self._p = payload
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *_): return False

    echt = ing.urllib.request.urlopen

    def ersatz(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else req
        if url in bilder:
            return Antwort(bilder[url])
        raise ing.urllib.error.HTTPError(url, 403, "abgelaufen", {}, None)

    ing.urllib.request.urlopen = ersatz
    try:
        yield
    finally:
        ing.urllib.request.urlopen = echt


def lade_proben() -> list[tuple[dict, dict[str, bytes]]]:
    """Unveränderte FocalX-Reports, je mit Bytes für jede darin genannte Adresse.

    Die Reports müssen roh sein — im Archiv stehen nur noch unsere Adressen,
    ein archivierter Report taugt als Ausgangspunkt also nicht mehr. Die Bytes
    stammen aus echten, heruntergeladenen Ausschnitten: verschiedene Objekte
    bekommen verschiedene Bilder, damit Prüfsummen etwas aussagen."""
    vorrat = sorted(QUELLE.rglob("closeups/*.jpg"))[:400]
    if not vorrat:
        return []
    proben = []
    for rp in sorted(QUELLE.rglob("focalx_report.json"))[:PROBEN]:
        report = json.loads(rp.read_text())
        geplant = plan_objects(report)
        if not geplant:
            continue
        bilder = {o["url"]: vorrat[i % len(vorrat)].read_bytes()
                  for i, o in enumerate(geplant)}
        proben.append((report, bilder))
    return proben


def test_gleichstand(proben) -> None:
    """Platte und S3 müssen bis aufs Byte dasselbe Archiv ergeben."""
    print("\nGleichstand Platte ↔ S3")
    with tempfile.TemporaryDirectory() as tmp:
        lokal = LocalStore(Path(tmp))
        s3 = S3Store(BUCKET)
        for report, bilder in proben:
            with ohne_netz(bilder):
                m_l = ingest_report(report, lokal, source="test")
                m_s = ingest_report(report, s3, source="test")
            pruefe(m_l["gespeichert"] == m_s["gespeichert"],
                   f"{report['InspectionId'][:8]}: gleiche Bilanz "
                   f"({m_l['gespeichert']['objekte']} Objekte)")

        k_l = sorted(lokal.list(""))
        k_s = sorted(s3.list(""))
        pruefe(k_l == k_s, f"gleiche Schlüsselmenge ({len(k_l)} Objekte)")

        # Zwei Stellen dürfen sich unterscheiden, ohne dass etwas falsch ist:
        # Manifeste tragen die Uhrzeit des Laufs (die Durchgänge können über
        # eine Sekundengrenze fallen), und der Report zeigt absichtlich auf
        # seine eigene Ablage — lokal ein Pfad, in S3 ein Bucket.
        def vergleichbar(schluessel: str, rohdaten: bytes, basis: str) -> object:
            if schluessel.endswith("manifest.json"):
                m = json.loads(rohdaten)
                m.pop("abgerufen_am", None)
                m.pop("erstmals_archiviert", None)
                return m
            if schluessel.endswith(".json") and not schluessel.endswith("manifest.json"):
                return json.loads(rohdaten.decode().replace(basis, "<ARCHIV>/"))
            return rohdaten

        abweichend = [k for k in k_l
                      if vergleichbar(k, lokal.get(k), lokal.uri(""))
                      != vergleichbar(k, s3.get(k), s3.uri(""))]
        pruefe(not abweichend,
               "jedes Objekt identisch (ohne Uhrzeit und Ablageadresse)")

        jpgs = [k for k in k_l if k.endswith(".jpg")]
        pruefe(jpgs and all(s3.get(k)[:2] == b"\xff\xd8" for k in jpgs),
               f"{len(jpgs)} Bilder sind gültige JPEGs (Kennung ffd8)")


def test_idempotenz(proben) -> None:
    """Ein zweiter Lauf darf nichts anlegen — sonst wächst das Archiv bei
    jedem Nachlauf um inhaltsgleiche Kopien.

    Das Manifest ist die eine erlaubte Ausnahme: Es beschreibt den jetzigen
    Bestand und wird aufgefrischt. Es darf dabei aber weder Vollständigkeit
    noch den Zeitpunkt der Ersterfassung verlieren."""
    print("\nIdempotenz")
    s3 = S3Store(BUCKET, prefix="lauf2")
    erst = {}
    for report, bilder in proben:
        with ohne_netz(bilder):
            erst[report["InspectionId"]] = ingest_report(report, s3, source="test")
    vorher = sorted(s3.list(""))
    nutzdaten = [k for k in vorher if not k.endswith("manifest.json")]
    pruefsummen = {k: sha256(s3.get(k)) for k in nutzdaten}

    for report, bilder in proben:
        with ohne_netz(bilder):
            m = ingest_report(report, s3, source="test")
        a = erst[report["InspectionId"]]
        pruefe(m["report"] == "unverändert",
               f"{report['InspectionId'][:8]}: Report unverändert")
        pruefe(m["erstmals_archiviert"] == a["erstmals_archiviert"],
               f"{report['InspectionId'][:8]}: Ersterfassung bleibt stehen")
        pruefe(m["vollstaendig"] == a["vollstaendig"]
               and m["gespeichert"]["objekte"] >= a["gespeichert"]["objekte"],
               f"{report['InspectionId'][:8]}: Bestand verschlechtert sich nicht")

    nachher = sorted(s3.list(""))
    pruefe(vorher == nachher, f"keine neuen Objekte ({len(nachher)} wie zuvor)")
    pruefe(all(sha256(s3.get(k)) == pruefsummen[k] for k in nutzdaten),
           f"kein Bild und kein Report überschrieben ({len(nutzdaten)} Objekte)")


def test_planungslauf_zerstoert_nichts(proben) -> None:
    """Ein Lauf ohne Downloads darf ein vollständiges Manifest nicht entwerten.

    Genau das ist passiert: `--no-download` schrieb "0 von 57 vorhanden" über
    ein vollständiges Manifest, und der Bestandsbericht meldete Datenverlust,
    wo keiner war."""
    print("\nPlanungslauf ohne Downloads")
    s3 = S3Store(BUCKET, prefix="planung")
    voll = {}
    for report, bilder in proben:
        with ohne_netz(bilder):
            voll[report["InspectionId"]] = ingest_report(report, s3, source="test")

    for report, _ in proben:
        m = ingest_report(report, s3, source="test", download=False)
        a = voll[report["InspectionId"]]
        pruefe(m["gespeichert"] == a["gespeichert"] and m["vollstaendig"] == a["vollstaendig"],
               f"{report['InspectionId'][:8]}: Bilanz bleibt "
               f"({m['gespeichert']['objekte']}/{m['erwartet']['objekte']})")
        pruefe(all(o.get("sha256") for o in m["objects"]
                   if o["status"] == "vorhanden"),
               f"{report['InspectionId'][:8]}: Prüfsummen bleiben erhalten")


def adressen(report: dict) -> tuple[list[str], int]:
    """Alle Adressfelder eines gespeicherten Reports: die gesetzten als Liste,
    die leeren als Anzahl."""
    gesetzt, leer = [], 0
    for o in report.get("OrientationResults") or []:
        werte = [o[f] for f in ing.BILD_FELDER if f in o]
        werte += [d[ing.CLOSEUP_FELD] for d in o.get("Damages") or []
                  if ing.CLOSEUP_FELD in d]
        for w in werte:
            gesetzt.append(w) if w else (leer := leer + 1)
    return gesetzt, leer


def test_umgeschriebene_links(proben) -> None:
    """Der gespeicherte Report muss ohne FocalX benutzbar sein: Jede Adresse
    zeigt auf ein Objekt, das wirklich im Bucket liegt — und keine einzige
    noch auf CloudFront."""
    print("\nReport mit Adressen ins Archiv")
    s3 = S3Store(BUCKET, prefix="links")
    for report, bilder in proben:
        with ohne_netz(bilder):
            m = ingest_report(report, s3, source="test")
        d = json.loads(s3.get(f"{m['prefix']}/report.json"))
        links, leer = adressen(d)
        kurz = report["InspectionId"][:8]
        pruefe(not any("cloudfront" in (l or "") for l in links),
               f"{kurz}: kein Link zeigt mehr auf FocalX")
        basis = s3.uri("")
        pruefe(all(l.startswith(basis) for l in links),
               f"{kurz}: alle {len(links)} Links zeigen auf {basis[:28]}…")
        # Vom Link zurück zum Schlüssel und nachsehen, ob das Objekt existiert.
        fehlt = [l for l in links if not s3.exists(l[len(basis):])]
        pruefe(not fehlt, f"{kurz}: jeder Link trifft ein vorhandenes Objekt")
        pruefe(len(d["Archiv"]["fehlend"]) * 0 == 0 and (leer > 0) == bool(d["Archiv"]["fehlend"]),
               f"{kurz}: {leer} nicht archivierte Stellen sind null und stehen in Archiv.fehlend")

    # Wiederholung darf nichts ändern — die Datei ist ohne Zeitstempel gebaut.
    vorher = {k: sha256(s3.get(k)) for k in s3.list("") if k.endswith("report.json")}
    for report, bilder in proben:
        with ohne_netz(bilder):
            ingest_report(report, s3, source="test")
    pruefe(all(sha256(s3.get(k)) == v for k, v in vorher.items()),
           f"bei gleichem Bestand entsteht dieselbe Datei ({len(vorher)} Stück)")

    # In keinem Report steht noch eine FocalX-Adresse, und nirgends im Archiv
    # eine Signatur — die wäre in Stunden wertlos und nur Ballast.
    jsons = [k for k in s3.list("") if k.endswith(".json")]
    pruefe(not [k for k in jsons if not k.endswith("manifest.json")
                and b"cloudfront" in (s3.get(k) or b"")],
           f"kein Report führt noch eine FocalX-Adresse ({len(jsons)} Dateien)")
    pruefe(not [k for k in jsons if b"Signature=" in (s3.get(k) or b"")],
           "keine abgelaufenen Signaturen im Archiv")


def test_luecke_wird_nachgetragen(proben) -> None:
    """Eine Lücke muss heilbar sein.

    Genau darauf ruht der Entwurf: Wir bewahren die FocalX-Adressen nicht auf,
    also muss ein zweiter Lauf eine `null`-Stelle allein aus dem frisch
    geholten Report wieder füllen können. Ginge das nicht, wäre jede
    Download-Panne endgültig."""
    print("\nNachlauf füllt eine Lücke")
    report, bilder = proben[0]
    s3 = S3Store(BUCKET, prefix="luecke")

    # Erster Lauf, bei dem zwei Bilder nicht ankommen (403 wie bei FocalX).
    verweigert = sorted(bilder)[:2]
    with ohne_netz({u: b for u, b in bilder.items() if u not in verweigert}):
        m = ingest_report(report, s3, source="test")
    pfad = f"{m['prefix']}/report.json"
    luecken = json.loads(s3.get(pfad))["Archiv"]["fehlend"]
    pruefe(len(luecken) == 2, f"{len(luecken)} Lücken nach dem Fehlversuch")
    pruefe(not m["vollstaendig"], "Manifest meldet die Inspektion als unvollständig")

    # Zweiter Lauf mit allen Bildern.
    with ohne_netz(bilder):
        m2 = ingest_report(report, s3, source="test")
    d = json.loads(s3.get(pfad))
    pruefe(d["Archiv"]["fehlend"] == [], "nach dem Nachlauf keine Lücke mehr")
    pruefe(m2["vollstaendig"], "Manifest meldet die Inspektion als vollständig")
    gesetzt, leer = adressen(d)
    basis = s3.uri("")
    pruefe(leer == 0 and all(s3.exists(l[len(basis):]) for l in gesetzt),
           f"alle {len(gesetzt)} Adressen treffen ein Objekt")


class Zaehlend:
    """Ein Store, der mitschreibt, wie oft Objekte gelesen werden.

    Nötig, weil sich der teure Fehler nicht am Ergebnis zeigt: Ein Lauf, der
    jedes Bild zur Kontrolle zurückliest, liefert dasselbe Manifest wie einer,
    der es lässt — nur eben mit dem vollen Umfang an GET-Verkehr."""

    def __init__(self, echt):
        self._e, self.gelesen = echt, []

    def get(self, key):
        self.gelesen.append(key)
        return self._e.get(key)

    def __getattr__(self, name):
        return getattr(self._e, name)


def test_wiederholung_liest_nichts_zurueck(proben) -> None:
    """Der zweite Lauf darf kein einziges Bild anfassen.

    Vorher las `_fetch` jedes vorhandene Objekt zurück, nur um die Prüfsumme
    neu auszurechnen, die längst im Manifest stand. Bei 2.000 Inspektionen am
    Tag sind das 19 GB Lesezugriff täglich für nichts."""
    print("\nWiederholungslauf ohne Lesezugriff")
    report, bilder = proben[0]
    s3 = S3Store(BUCKET, prefix="wdh")
    with ohne_netz(bilder):
        erst = ingest_report(report, s3, source="test")

    zaehler = Zaehlend(S3Store(BUCKET, prefix="wdh"))
    with ohne_netz(bilder):
        zweit = ingest_report(report, zaehler, source="test")
    bild_lesungen = [k for k in zaehler.gelesen if k.endswith(".jpg")]
    pruefe(not bild_lesungen,
           f"kein Bild zurückgelesen ({len(bild_lesungen)} statt "
           f"{erst['erwartet']['objekte']})")
    pruefe(zweit["gespeichert"]["objekte"] == erst["gespeichert"]["objekte"],
           f"trotzdem alle {zweit['gespeichert']['objekte']} Objekte verbucht")
    alt = {o["key"]: o["sha256"] for o in erst["objects"]}
    pruefe(all(o["sha256"] == alt[o["key"]] for o in zweit["objects"]),
           "jede Prüfsumme aus dem Manifest übernommen")
    pruefe(zweit["vollstaendig"], "Inspektion weiter als vollständig geführt")

    # Mit --pruefen soll er sehr wohl lesen — sonst wäre die Kontrolle wertlos.
    zaehler2 = Zaehlend(S3Store(BUCKET, prefix="wdh"))
    with ohne_netz(bilder):
        ingest_report(report, zaehler2, source="test", pruefen=True)
    pruefe(len([k for k in zaehler2.gelesen if k.endswith(".jpg")])
           == erst["erwartet"]["objekte"],
           "mit --pruefen wird jedes Bild geprüft")


def test_pruefen_findet_veraenderung(proben) -> None:
    """Eine stille Veränderung am Beleg muss auffallen — dafür sind die
    Prüfsummen da. Ohne diesen Test wäre `sha256` nur Zierde."""
    print("\nStille Veränderung am Archiv")
    report, bilder = proben[1]
    s3 = S3Store(BUCKET, prefix="manipuliert")
    with ohne_netz(bilder):
        m = ingest_report(report, s3, source="test")

    opfer = next(o["key"] for o in m["objects"] if o["key"].endswith(".jpg"))
    s3.put(f"{m['prefix']}/{opfer}", b"\xff\xd8\xff etwas ganz anderes")

    with ohne_netz(bilder):
        ohne = ingest_report(report, s3, source="test")
    pruefe(ohne["vollstaendig"],
           "ohne --pruefen bleibt die Veränderung unbemerkt (erwartet)")

    with ohne_netz(bilder):
        mit = ingest_report(report, s3, source="test", pruefen=True)
    treffer = [o for o in mit["objects"] if o["status"] == "pruefsumme_abweichend"]
    pruefe(len(treffer) == 1, f"mit --pruefen genau 1 Fund ({len(treffer)})")
    pruefe(treffer[0]["key"] == opfer, "und zwar das veränderte Objekt")
    pruefe(not mit["vollstaendig"] and opfer in mit["fehlend"],
           "die Inspektion gilt damit als nicht mehr vollständig")


def test_praefix() -> None:
    """Der Bucket wird geteilt — unser Präfix darf beim Auflisten nicht
    durchschlagen, sonst passen die Schlüssel im Manifest nicht mehr."""
    print("\nPräfix im geteilten Bucket")
    s3 = S3Store(BUCKET, prefix="focalx/archiv")
    s3.put("v1/2026/01/01/abc/report.json", b"{}")
    pruefe(list(s3.list("v1/")) == ["v1/2026/01/01/abc/report.json"],
           "list() liefert Schlüssel ohne Präfix zurück")
    pruefe(s3.exists("v1/2026/01/01/abc/report.json"), "exists() trifft")
    pruefe(s3.get("v1/2026/01/01/abc/report.json") == b"{}", "get() liefert Inhalt")
    pruefe(s3.uri("v1/x") == f"s3://{BUCKET}/focalx/archiv/v1/x", "uri() zeigt den vollen Pfad")
    pruefe(s3.get("gibtsnicht") is None, "get() auf Unbekanntes ergibt None")
    pruefe(not s3.exists("gibtsnicht"), "exists() auf Unbekanntes ist falsch")


def test_paginierung() -> None:
    """S3 liefert höchstens 1.000 Schlüssel je Seite. Ein Tag hat 14.477
    Objekte — wer den Paginator vergisst, verliert 93 % des Bestands
    lautlos und meldet trotzdem Erfolg."""
    print("\nPaginierung über 1.000 Objekte")
    s3 = S3Store(BUCKET, prefix="viele")
    for i in range(1500):
        s3.put(f"v1/obj{i:05d}.bin", b"x")
    pruefe(len(list(s3.list("v1/"))) == 1500, "alle 1.500 Schlüssel kommen zurück")


def test_survey(proben) -> None:
    """Der Bestandsbericht muss auch über S3 laufen — er ist die einzige
    Kontrolle, ob ein Tag vollständig ist."""
    print("\nBestandsbericht über S3")
    s3 = S3Store(BUCKET, prefix="bericht")
    for report, bilder in proben:
        with ohne_netz(bilder):
            ingest_report(report, s3, source="test")
    bericht = summarise(collect(s3))
    pruefe(bericht["inspektionen"] == len(proben),
           f"{bericht['inspektionen']} Inspektionen gefunden")
    pruefe(bericht["bytes"] > 0, f"{bericht['bytes'] / 1e6:.1f} MB verbucht")


def test_loeschweg(proben) -> None:
    """Ein Fahrzeug muss auf Anfrage restlos verschwinden können. Der
    Kennzeichen-Index ist der einzige Weg dorthin, der ohne vollständigen
    Durchlauf des Buckets auskommt."""
    print("\nLöschweg über den Kennzeichen-Index")
    s3 = S3Store(BUCKET, prefix="loeschen")
    for report, bilder in proben:
        with ohne_netz(bilder):
            ingest_report(report, s3, source="test")

    report = proben[0][0]
    schluessel = ing.plate_key(report.get("RegistrationNumber") or "")
    treffer = list(s3.list(f"index/plate/{schluessel}/"))
    pruefe(treffer, f"Index kennt {schluessel} ({len(treffer)} Inspektion(en))")

    prefix = prefix_for(report)
    betroffen = list(s3.list(prefix))
    pruefe(betroffen, f"{len(betroffen)} Objekte hängen an diesem Fahrzeug")
    andere = [k for k in s3.list("v1/") if not k.startswith(prefix)]
    pruefe(andere, f"{len(andere)} Objekte anderer Fahrzeuge bleiben unberührt")


def main() -> int:
    if not QUELLE.exists():
        print(f"Keine FocalX-Reports unter {QUELLE}.")
        return 1
    try:
        from moto import mock_aws
    except ImportError:
        print("Dieser Test braucht boto3 und moto:\n"
              "  python3 -m venv ~/.cache/focalx-s3venv\n"
              "  ~/.cache/focalx-s3venv/bin/python -m pip install boto3 'moto[s3]'\n"
              "  ~/.cache/focalx-s3venv/bin/python scripts/archive_s3_test.py")
        return 1
    import boto3

    proben = lade_proben()
    if not proben:
        print("Keine Reports im Probearchiv.")
        return 1
    bilder = sum(len(b) for _, b in proben)
    print(f"{len(proben)} Reports, {bilder} Bilder aus {QUELLE}")

    with mock_aws():
        boto3.client("s3", region_name="eu-central-1").create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        test_gleichstand(proben)
        test_idempotenz(proben)
        test_planungslauf_zerstoert_nichts(proben)
        test_umgeschriebene_links(proben)
        test_luecke_wird_nachgetragen(proben)
        test_wiederholung_liest_nichts_zurueck(proben)
        test_pruefen_findet_veraenderung(proben)
        test_praefix()
        test_paginierung()
        test_survey(proben)
        test_loeschweg(proben)

    print(f"\n{geprueft} Prüfungen bestanden.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
