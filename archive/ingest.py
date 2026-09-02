"""Eine Inspektion ins Archiv legen: Report, Vollbilder, Closeups, Manifest.

Grundhaltung: FocalX ändert sein Verhalten ohne Ankündigung. Zwischen dem
05.08. und dem 02.09.2026 wechselten Modell (32 → 38 Funde auf denselben
Fotos), Geschwindigkeit (5,5 min → 17 s) und Kennzeichenformat
(`TXWCV5796` → `TX-WCV5796`). Das Archiv verlässt sich deshalb auf nichts
davon: eigener Suchschlüssel, inhaltlich unveränderlicher Erstreport,
Prüfsummen zu jedem Objekt.

Gespeichert wird **eine** Reportfassung, und in der zeigen alle Adressen ins
Archiv statt zu FocalX. Die Originaladressen sind nach sieben Tagen tot und
nach einem Mandantenwechsel wertlos; was sie erschließen, liegt dann längst
bei uns.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .store import Store

SCHEMA = "focalx-archive/v1"
SCHEMA_ARCHIV = "focalx-archiv-report/1"
UA = {"User-Agent": "sixt-focalx-archive/1"}
TIMEOUT_S = 60

# Die drei Felder, in denen FocalX Adressen führt — mit einem vollständigen
# Report über alle Ebenen gesucht, es gibt keine weiteren. `ImageFileDrawURL`
# zeigt in allen 101 geprüften Fällen auf dieselbe Datei wie `ImageFileURL`;
# es ist keine zweite Bildsorte, muss aber trotzdem umgeschrieben werden.
BILD_FELDER = ("ImageFileURL", "ImageFileDrawURL")
CLOSEUP_FELD = "CloseUpImageFilename"

# Kennzeichen sind Freitext ohne Prüfung — die App-Inspektionen trugen TEST,
# TEST4, TEST006. Ein Archiv, das die stillschweigend annimmt, ist technisch
# vollständig und fachlich wertlos.
PLATE_RE = re.compile(r"^[A-Z0-9]{5,10}$")
PLATE_BLOCK = re.compile(r"^TEST", re.I)
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# S3 erlaubt Schlüssel bis 1024 Bytes, gängige Dateisysteme aber nur 255 Bytes je
# Pfadbestandteil. Im Tenant liegen Inspektionen aus einem fremden Arbeitsablauf
# (Bahnverladung), deren Slot-Namen die Frachtpapiere enthalten — über 200 Zeichen
# allein für den Slot. Ein Archiv, das nur auf S3 liefe, würde das nie merken;
# jeder lokale Trockenlauf bricht daran ab.
MAX_NAME_BYTES = 180


def safe_name(name: str) -> str:
    """Kürzt zu lange Dateinamen, ohne sie mehrdeutig zu machen.

    Der Kürzel-Anhang ist aus dem vollen Namen abgeleitet, bleibt also über
    Läufe hinweg gleich — sonst legte jeder Durchlauf dasselbe Bild neu an.
    Der ungekürzte Name steht im Manifest."""
    roh = name.encode("utf-8", "surrogatepass")
    if len(roh) <= MAX_NAME_BYTES:
        return name
    stamm, punkt, endung = name.rpartition(".")
    endung = f".{endung}" if punkt else ""
    kuerzel = sha256(name.encode("utf-8", "surrogatepass"))[:12]
    platz = MAX_NAME_BYTES - len(kuerzel) - len(endung) - 1
    gekuerzt = stamm.encode("utf-8", "surrogatepass")[:platz].decode(
        "utf-8", "ignore")
    return f"{gekuerzt}~{kuerzel}{endung}"


def fingerprint(report: dict) -> str:
    """Inhaltskennung eines Reports — ohne Adressen, ohne Reihenfolge.

    Sie beantwortet nur: Sagt FocalX heute dasselbe über dieses Fahrzeug wie
    beim ersten Abruf? Adressen gehören nicht dazu; sie sagen, wo ein Bild
    liegt, nicht was zu sehen ist. Das ist aus drei Gründen nötig:

    `Expires` und `Signature` werden bei **jeder** Anfrage neu erzeugt; schon
    zwei Minuten Abstand ändern die Bytes. Das ergab 72 Fehlalarme unter
    425 Inspektionen.

    Die Funde eines Fotos kommen in wechselnder Reihenfolge: bei 8 von 425
    Inspektionen tauschten zwei Einträge im selben `Damages`-Feld die Plätze,
    ohne dass sich ein Wert änderte.

    Und der gespeicherte Report trägt **unsere** Adressen, der frisch geholte
    die von FocalX. Ohne diese Bereinigung gälte jeder Zweitabruf als
    Abweichung."""
    rein = json.loads(json.dumps(report))
    rein.pop("Archiv", None)
    for orient in rein.get("OrientationResults") or []:
        for feld in BILD_FELDER:
            orient.pop(feld, None)
        for dmg in orient.get("Damages") or []:
            dmg.pop(CLOSEUP_FELD, None)
        if orient.get("Damages"):
            orient["Damages"] = sorted(
                orient["Damages"],
                key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False))
    return sha256(json.dumps(rein, sort_keys=True, ensure_ascii=False).encode())


def plate_key(raw: str) -> str:
    """Suchschlüssel, der die Schreibweise des Anbieters überlebt."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


def plate_plausibel(raw: str) -> bool:
    return bool(PLATE_RE.match(plate_key(raw))) and not PLATE_BLOCK.match(raw or "")


def prefix_for(report: dict) -> str:
    """v1/<jjjj>/<mm>/<tt>/<inspection_id> — Datum zuerst, weil der Ingest
    tageweise läuft und ein Wiederholungslauf so räumlich begrenzt bleibt."""
    stamp = (report.get("StartTime") or report.get("EndTime") or "")[:10]
    teile = stamp.split("-") if stamp else []
    y, m, d = (teile + ["0000", "00", "00"])[:3]
    return f"v1/{y}/{m}/{d}/{report.get('InspectionId') or 'unbekannt'}"


def closeup_name(url: str) -> str:
    """Dateiname aus der internen UUID der Closeup-URL.

    NICHT `F<n>`: unsere Nummerierung verschiebt sich, sobald der Report erneut
    geholt wird. Die UUID in der URL ist stabil und entdoppelt nebenbei die
    Ausschnitte, die sich zwei Funde teilen."""
    base = url.split("?")[0].rsplit("/", 1)[-1]
    hits = UUID_RE.findall(base)
    return f"{hits[-1]}.jpg" if hits else safe_name(f"{sha256(url.encode())[:16]}.jpg")


def plan_objects(report: dict) -> list[dict]:
    """Was zu dieser Inspektion gehört — festgelegt vor dem ersten Byte,
    damit hinterher prüfbar ist, ob wirklich alles ankam."""
    objects: dict[str, dict] = {}
    for orient in report.get("OrientationResults") or []:
        url = (orient.get("ImageFileURL") or "").strip()
        name = (orient.get("ImageFileName") or "").rsplit("/", 1)[-1]
        if url and name:
            key = f"images/{safe_name(name)}"
            eintrag = {
                "key": key,
                "kind": "image",
                "url": url,
                # Der Slot ist die einzige verlässliche Angabe, welches Foto
                # gemeint war — OrientationName ist gröber und nicht umkehrbar.
                "slot": name.rsplit("_", 1)[-1].removesuffix(".jpg"),
            }
            if safe_name(name) != name:
                eintrag["original_name"] = name
            objects.setdefault(key, eintrag)
        for dmg in orient.get("Damages") or []:
            curl = (dmg.get("CloseUpImageFilename") or "").strip()
            if not curl.startswith("http"):
                continue
            key = f"closeups/{closeup_name(curl)}"
            entry = objects.setdefault(key, {
                "key": key, "kind": "closeup", "url": curl, "findings": [],
            })
            entry["findings"].append(dmg.get("DamageNumber"))
    return list(objects.values())


def _closeup_key(wert: str) -> str | None:
    """Archivschlüssel eines Ausschnitts — aus der FocalX-Adresse oder aus
    einer schon umgeschriebenen.

    Beide Formen müssen gehen: beim ersten Ablegen steht dort CloudFront, beim
    Neuverlinken nach einem Umzug schon `s3://…/closeups/…`."""
    if not wert:
        return None
    if wert.startswith("http"):
        return f"closeups/{closeup_name(wert)}"
    schnitt = wert.rfind("/closeups/")
    return wert[schnitt + 1:] if schnitt >= 0 else None


def archiv_keys(report: dict) -> set[str]:
    """Welche Archivschlüssel dieser Report nennt — egal, ob er noch die
    FocalX-Adressen trägt oder schon unsere.

    `plan_objects` kann das nicht leisten: Es plant Downloads und lässt
    deshalb alles fallen, was nicht mit `http` beginnt. Genau das trifft aber
    auf jeden bereits archivierten Report zu."""
    keys = set()
    for orient in report.get("OrientationResults") or []:
        name = (orient.get("ImageFileName") or "").rsplit("/", 1)[-1]
        if name and any((orient.get(f) or "").strip() for f in BILD_FELDER):
            keys.add(f"images/{safe_name(name)}")
        for dmg in orient.get("Damages") or []:
            key = _closeup_key((dmg.get(CLOSEUP_FELD) or "").strip())
            if key:
                keys.add(key)
    return keys


def archiv_report(report: dict, store: Store, prefix: str,
                  vorhanden: set[str]) -> dict:
    """Der Report, wie wir ihn ablegen: Adressen zeigen ins Archiv, nicht zu
    FocalX.

    Das ist die einzige Reportfassung, die gespeichert wird. Die
    FocalX-Adressen kommen nicht mit — sie sind nach sieben Tagen tot und nach
    einem Mandantenwechsel ohnehin wertlos. Alles, was FocalX inhaltlich sagt
    (Funde, Bauteile, Schadensarten, Koordinaten, Zeiten), bleibt unangetastet.

    Die Datei wird bei jedem Lauf neu geschrieben: Ein Nachlauf, der ein
    fehlendes Bild doch noch holt, soll den Link nachtragen. Der **Inhalt**
    bleibt dabei der des ersten Abrufs — nur die Adressen werden aufgefrischt.

    Bewusst ohne Zeitstempel: Bei gleichem Bestand kommt dieselbe Datei heraus,
    sonst schriebe jeder Lauf ein neues Objekt.

    Was nicht im Archiv liegt, bekommt `null`. Die FocalX-Adresse
    stehenzulassen wäre schlimmer als eine Lücke: Sie sieht gültig aus und
    liefert doch nur 403. Welche Stellen betroffen sind, steht in
    `Archiv.fehlend` und ausführlich im Manifest."""
    kopie = json.loads(json.dumps(report))
    fehlend: list[str] = []

    def adresse(key: str | None) -> str | None:
        if key and key in vorhanden:
            return store.uri(f"{prefix}/{key}")
        if key:
            fehlend.append(key)
        return None

    for orient in kopie.get("OrientationResults") or []:
        name = (orient.get("ImageFileName") or "").rsplit("/", 1)[-1]
        bildkey = f"images/{safe_name(name)}" if name else None
        for feld in BILD_FELDER:
            if feld in orient:
                orient[feld] = adresse(bildkey)
        for dmg in orient.get("Damages") or []:
            if CLOSEUP_FELD in dmg:
                dmg[CLOSEUP_FELD] = adresse(_closeup_key(
                    (dmg.get(CLOSEUP_FELD) or "").strip()))

    kopie["Archiv"] = {
        "schema": SCHEMA_ARCHIV,
        "hinweis": "Adressen zeigen in dieses Archiv, nicht zu FocalX. "
                   "null bedeutet: nicht archiviert, siehe manifest.json. "
                   "Die FocalX-Adressen werden nicht aufbewahrt.",
        "basis": store.uri(""),
        "prefix": prefix,
        "fehlend": sorted(set(fehlend)),
    }
    return kopie


def _fetch(obj: dict, store: Store, prefix: str,
           bekannt: dict[str, dict] | None = None, pruefen: bool = False) -> dict:
    """Ein Objekt holen und ablegen. Fehler werden vermerkt, nicht geworfen —
    ein totes Bild darf die anderen achtzehn nicht mitreißen.

    Was schon im Archiv liegt und im letzten Manifest mit Prüfsumme steht, wird
    weder geholt noch zurückgelesen. Ohne das kostete jeder Wiederholungslauf
    den vollen Umfang an GET-Verkehr: 19 GB am Tag, nur um Prüfsummen
    auszurechnen, die schon dastehen. Fehlt die Prüfsumme (Manifest verloren),
    wird einmal zurückgelesen und sie damit wiederhergestellt."""
    key = f"{prefix}/{obj['key']}"
    if store.exists(key):
        frueher = (bekannt or {}).get(obj["key"]) or {}
        if frueher.get("sha256") and not pruefen:
            return {**obj, "status": "vorhanden",
                    "bytes": frueher.get("bytes"), "sha256": frueher["sha256"]}
        payload = store.get(key) or b""
        ist = sha256(payload)
        if pruefen and frueher.get("sha256") and frueher["sha256"] != ist:
            # Stille Veränderung an einem Beleg. Das muss laut sein: mit diesem
            # Status zählt das Objekt als fehlend und taucht im Bericht auf.
            return {**obj, "status": "pruefsumme_abweichend",
                    "bytes": len(payload), "sha256": frueher["sha256"],
                    "note": f"gefunden {ist[:16]}…"}
        return {**obj, "status": "vorhanden", "bytes": len(payload), "sha256": ist}
    try:
        req = urllib.request.Request(obj["url"], headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        # 403 heißt bei den Vollbildern fast immer: älter als 168 Stunden.
        note = "url_abgelaufen" if e.code == 403 else f"http_{e.code}"
        return {**obj, "status": e.code, "bytes": None, "sha256": None, "note": note}
    except Exception as e:
        return {**obj, "status": "fehler", "bytes": None, "sha256": None,
                "note": type(e).__name__}
    try:
        store.put(key, payload)
    except Exception as e:
        # Auch das Ablegen kann scheitern (zu langer Name, volle Platte, S3-Fehler).
        # Ein einzelnes Objekt darf den Tageslauf nicht abbrechen — der Fehler
        # gehört ins Manifest, damit ihn der Bestandsbericht aufgreift.
        return {**obj, "status": "ablagefehler", "bytes": None, "sha256": None,
                "note": f"{type(e).__name__}: {e}"[:200]}
    return {**obj, "status": 200, "bytes": len(payload), "sha256": sha256(payload)}


def ingest_report(report: dict, store: Store, source: str,
                  download: bool = True, workers: int = 6,
                  pruefen: bool = False) -> dict:
    prefix = prefix_for(report)
    plate = (report.get("RegistrationNumber") or "").strip()

    # Inhaltlich gilt der erste Abruf. Ein späterer lieferte nachweislich
    # andere Funde (32 → 38 auf denselben Fotos); der käme daneben, nie darüber.
    alt = store.get(f"{prefix}/report.json")
    vorher = json.loads(alt) if alt else None
    abweichend = None
    if vorher is None:
        report_note = "neu"
        massgeblich = report
    elif fingerprint(vorher) == fingerprint(report):
        # Inhaltlich dasselbe. Weiter mit dem frischen Report: Er trägt noch
        # alle FocalX-Adressen, und nur daraus lässt sich ein Ausschnitt
        # nachverlinken, der beim letzten Mal fehlte.
        report_note = "unverändert"
        massgeblich = report
    else:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        report_note = f"abweichend → report.{stamp}.json"
        massgeblich = vorher
        abweichend = f"{prefix}/report.{stamp}.json"

    def schreibe(ziel: str, inhalt: dict, da: set[str]) -> None:
        store.put(ziel, json.dumps(archiv_report(inhalt, store, prefix, da),
                                   indent=1, ensure_ascii=False).encode())

    # Den Report vor den Bildern ablegen. Bricht der Lauf mitten im Download ab,
    # sind die Funde trotzdem gesichert; die Adressen stehen dann auf dem Stand
    # von vorher und werden am Ende aufgefrischt.
    schon_da = {k for k in archiv_keys(massgeblich)
                if store.exists(f"{prefix}/{k}")}
    schreibe(f"{prefix}/report.json", massgeblich, schon_da)

    # Das vorige Manifest wird an drei Stellen gebraucht: für den Zeitpunkt der
    # Ersterfassung, um bei einem Lauf ohne Downloads nichts zu vergessen, und
    # damit ein Wiederholungslauf vorhandene Objekte nicht noch einmal anfasst.
    vorheriges = None
    roh = store.get(f"{prefix}/manifest.json")
    if roh:
        try:
            vorheriges = json.loads(roh)
        except ValueError:
            pass

    bekannt = {o["key"]: o for o in (vorheriges or {}).get("objects", [])
               if o.get("status") in (200, "vorhanden")}

    planned = plan_objects(report)
    if download:
        with ThreadPoolExecutor(max(1, workers)) as pool:
            results = list(pool.map(
                lambda o: _fetch(o, store, prefix, bekannt, pruefen), planned))
    else:
        # Ohne Downloads zählt, was schon im Archiv liegt. Sonst schriebe ein
        # reiner Planungslauf ein Manifest mit "0 von 57 vorhanden" über ein
        # vollständiges — und der Bestandsbericht meldete Datenverlust, wo
        # keiner ist.
        results = [
            {**bekannt[o["key"]], **o, "status": "vorhanden",
             "bytes": bekannt[o["key"]].get("bytes"),
             "sha256": bekannt[o["key"]].get("sha256")}
            if o["key"] in bekannt
            else {**o, "status": "übersprungen", "bytes": None, "sha256": None}
            for o in planned
        ]

    gespeichert = [r for r in results if r["status"] in (200, "vorhanden")]
    fehlend = [r["key"] for r in results if r not in gespeichert]
    n_findings = sum(len(o.get("Damages") or [])
                     for o in report.get("OrientationResults") or [])

    jetzt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Das Manifest wird bei jedem Lauf neu geschrieben — es beschreibt den
    # jetzigen Bestand, nicht den von damals. Der Zeitpunkt der Ersterfassung
    # ist aber eine Aussage über den Beleg selbst und darf dabei nicht
    # verlorengehen.
    erstmals = jetzt
    if vorheriges:
        erstmals = (vorheriges.get("erstmals_archiviert")
                    or vorheriges.get("abgerufen_am") or jetzt)

    manifest = {
        "schema": SCHEMA,
        "inspection_id": report.get("InspectionId"),
        "prefix": prefix,
        "registration_number": plate,       # wie FocalX es liefert
        "plate_key": plate_key(plate),      # unser Suchschlüssel
        "plate_plausibel": plate_plausibel(plate),
        "account": report.get("Account"),
        "completed": report.get("Completed"),
        "analyse": {"start": report.get("StartTime"), "ende": report.get("EndTime")},
        "erstmals_archiviert": erstmals,
        "abgerufen_am": jetzt,
        "quelle": source,
        "report": report_note,
        "erwartet": {
            "orientierungen": len(report.get("OrientationResults") or []),
            "findings": n_findings,
            "objekte": len(planned),
        },
        "gespeichert": {
            "objekte": len(gespeichert),
            "bytes": sum(r["bytes"] or 0 for r in gespeichert),
        },
        "vollstaendig": not fehlend and bool(planned),
        "fehlend": fehlend,
        # Die Herkunft ohne Query: Der Pfad sagt, woher das Objekt stammt,
        # `Expires`/`Signature` sind nach Stunden wertlos und machten ein
        # Viertel des Manifests aus.
        "objects": sorted(({**r, "url": (r.get("url") or "").split("?")[0]}
                           for r in results), key=lambda r: r["key"]),
    }
    store.put(f"{prefix}/manifest.json",
              json.dumps(manifest, indent=1, ensure_ascii=False).encode())

    # Adressen auf den Stand nach den Downloads bringen. Weicht der maßgebliche
    # Report vom gerade geholten ab, passen dessen Schlüssel nicht zum Plan —
    # dann muss die Verfügbarkeit direkt in der Ablage nachgesehen werden.
    geholt = {r["key"] for r in gespeichert}
    if massgeblich is report:
        schreibe(f"{prefix}/report.json", massgeblich, geholt)
    else:
        schreibe(f"{prefix}/report.json", massgeblich,
                 {k for k in archiv_keys(massgeblich)
                  if store.exists(f"{prefix}/{k}")})
        schreibe(abweichend, report, geholt)

    _index(store, manifest)
    return manifest


def _index(store: Store, manifest: dict) -> None:
    """Zeiger Kennzeichen → Inspektion, damit „lösche alles zu Fahrzeug X"
    nicht bedeutet, Millionen Objekte zu durchsuchen.

    Bewusst ein eigenes Objekt je Paar statt einer Sammelliste: Anlegen ist
    dann ein einzelner Schreibvorgang ohne Lesen-Ändern-Schreiben, also
    gefahrlos, wenn mehrere Ingest-Vorgänge gleichzeitig laufen."""
    key = manifest["plate_key"] or "OHNE-KENNZEICHEN"
    store.put(
        f"index/plate/{key}/{manifest['inspection_id']}.json",
        json.dumps({
            "prefix": manifest["prefix"],
            "registration_number": manifest["registration_number"],
            "plate_plausibel": manifest["plate_plausibel"],
            "analyse_start": manifest["analyse"]["start"],
        }, indent=1, ensure_ascii=False).encode(),
    )
