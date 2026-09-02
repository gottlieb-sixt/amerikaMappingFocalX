# FocalX-API — vollständige Analyse der Schnittstelle

Grundlagenanalyse: welche Endpoints es gibt, wie man eine Anfrage stellt, was
genau zurückkommt und wo welche Information (Schadensart, Bauteil, Position,
Bounding Box, Bilder) liegt.

> **Eine echte Antwort komplett und Feld für Feld erklärt:**
> [`focalx-api-beispiel.md`](focalx-api-beispiel.md) · Rohdatei:
> [`beispiel/damagereport-beispiel.json`](beispiel/damagereport-beispiel.json)

**Belegbasis.** Jede Aussage in diesem Dokument ist gegen echte Antworten
geprüft, die im Repo liegen: **692 vollständige Reports** aus den Läufen v1, v2,
v3 und fl500 mit **12.745 Bildauswertungen** und **11.694 Findings**
(`data/**/focalx_report.json`), dazu drei am 02.09.2026 frisch angelegte
Inspektionen. Wo eine Aussage aus dem Code oder aus dokumentierter Erfahrung
statt aus den Daten stammt, steht das ausdrücklich dabei.
Client-Implementierung: `eval/focalx.py`; Referenzskript des Anbieters:
`upload.py`.

> **Achtung, Stand 02.09.2026: FocalX hat sich zwischen August und September
> spürbar geändert** — Analysedauer von 5,5 min auf 17 s, andere Funde auf
> denselben Fotos, anderes Kennzeichenformat. Die Abschnitte 3.0.4, 3.0.5 und
> die Fallen 9.10/9.11 halten das fest. Zahlen aus den Läufen bis August bleiben
> als historische Messung stehen, sind aber nicht mehr die Gegenwart.

---

## 1. Die Kurzantworten

| Frage | Antwort |
|---|---|
| Welcher Endpoint liefert die Ergebnisse? | `GET https://<slug>.tenant.focalx.ai/api/v2/service/inspections/<inspection_id>/damagereport/` |
| Was braucht man dafür? | Benutzer + Passwort (→ JWT), den Tenant-Slug aus dem Benutzerprofil, eine Inspektions-ID, Header `Authorization: Bearer <token>` |
| Wo steht die Schadensart? | `OrientationResults[].Damages[].DamageTypeName` (+ `DamageTypeCode`) |
| Wo steht das betroffene Bauteil? | `Damages[].PartName` (+ `PartId`, `ZoneId`) |
| Wo ist die Bounding Box? | **Nirgends.** Die API gibt keine Pixelkoordinaten heraus. `MarkerPoint` sieht wie eine Box aus, ist aber eine Konstante (Abschnitt 6). Die Box existiert nur eingebrannt im Close-up-Bild. |
| Wo sind die Bilder? | Auf zwei CloudFront-Verteilungen: das Walkaround-Foto in `OrientationResults[].ImageFileURL` (signiert, 7 Tage gültig), der annotierte Schadensausschnitt in `Damages[].CloseUpImageFilename` (unsigniert). |
| Wie erfährt man, dass der Report fertig ist? | Feld `Completed`. Vorher liefert der Endpoint `404` oder ein Objekt mit `Completed: false` und unvollständigen `OrientationResults`. |

---

## 2. Voraussetzungen und Zugang

| Was | Wert / Herkunft |
|---|---|
| Auth-Domain | `tenant.focalx.ai` (Konstante `DOMAIN` in `eval/focalx.py`) |
| Zugangsdaten | `FOCALX_PRECISE_USERNAME`, `FOCALX_PRECISE_PASSWORD` aus `.env` (gitignored) |
| Tenant-Slug | **nicht** raten — kommt aus dem Benutzerprofil, Feld `client.slug`. Alle Service-Aufrufe laufen gegen `https://<slug>.tenant.focalx.ai` |
| `process_id` | `7BAQMZBAHUYK` — in allen 692 Reports identisch; bestimmt den Analyse-Workflow |
| Token | JWT, Feld `access` aus `/api/v1/token/`. Kurzlebig: bei `401` neu anmelden und den Aufruf wiederholen (der Client tut das beim Polling automatisch) |
| Account im Report | `sixttwo` (652 Reports) bzw. `sixt` (40) — hängt am benutzten Zugang |

Wichtig an der Zweiteilung: **Login und Profil laufen gegen die Basis-Domain,
alles andere gegen die Slug-Subdomain.** Ein Aufruf von
`create-inspection` gegen `tenant.focalx.ai` ohne Slug schlägt fehl.

---

## 3. Alle Endpoints im Überblick

| # | Methode | URL | Zweck |
|---|---|---|---|
| 1 | POST | `https://tenant.focalx.ai/api/v1/token/` | Login → JWT |
| 2 | GET | `https://tenant.focalx.ai/api/v1/customer/user-profile/` | Tenant-Slug + **Katalog der erlaubten Positionslabels** (`custom_data[].label`) |
| 3 | POST | `https://<slug>.tenant.focalx.ai/api/v1/service/create-inspection/` | Inspektion anlegen → `inspection.id` |
| 4 | POST | `…/api/v2/service/inspections/<id>/signedurls/` | Pro Bild eine S3-Upload-Erlaubnis |
| 5 | POST | *(S3-URL aus Schritt 4)* | Das Bild selbst hochladen — **ohne** Auth-Header |
| 6 | POST | `…/api/v2/service/inspections/<id>/submitImages/` | Analyse starten |
| 7 | GET | `…/api/v1/service/inspections/<id>/` | Statusdetail, u. a. `is_images_submitted` |
| 8 | GET | `…/api/v2/service/inspections/<id>/damagereport/` | **Das Ergebnis** |

Nur 1, 2, 7 und 8 sind lesend. Für eine reine Auswertung bestehender
Inspektionen braucht man 1, 2 und 8.

### 3.0 Drei weitere Endpoints, die wir NICHT nutzen

Die Anbieterdokumentation führt (Stand 01.09.2026, Inhaltsverzeichnis) drei
Funktionen, die in `eval/focalx.py` fehlen. Wir pollen also nicht, weil es
nichts Besseres gibt, sondern weil der Client sie nicht kennt:

| Laut Anbieterdoku | Warum es zählt |
|---|---|
| **Damage Report via Webhook (POST)** | FocalX kann den Report aktiv zustellen. Ersetzt das Pollen und beseitigt die Frage, wann ein Report fertig ist |
| **All Inspections (with Date)** | Listet Inspektionen zu einem Datum. Damit lassen sich Inspektionen auch dann finden, wenn man ihre ID nicht selbst erzeugt hat — die Voraussetzung für einen Archiv-Dienst, der nicht selbst hochlädt |
| **Generate Link API** | Zweck noch nicht geklärt; falls er frische Bild-URLs liefert, ist das die Antwort auf den 168-Stunden-Verfall (Abschnitt 7.1) |

**Die Endpoint-Liste unten ist die von uns im Betrieb genutzte Teilmenge, nicht
der volle Funktionsumfang der API.** Webhook und Generate-Link sind noch nicht
erhoben; die Inspektionsliste dagegen ist inzwischen live nachgemessen:

### 3.0.1 Inspektionsliste — gemessen am 01.09.2026

```
GET {base}/api/v2/service/inspections/inspectionswithdate/
    ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
Authorization: Bearer <token>
```

Gefiltert wird nach `created_on` der Inspektion in FocalX, **nicht** nach dem
Check-in-Datum des Fahrzeugs. Zeitraum ist beidseitig inklusiv.

Vier Abweichungen der Anbieterdoku von der Realität — die Doku trägt an dieser
Stelle sogar ein eingebautes Geständnis („*The fields below depend on your API's
actual output. I'm reusing fields based on your previous CreateInspection
response example*"), die Feldliste dort ist also geraten:

| Doku | Realität (Tenant `sixttwo`) |
|---|---|
| Hülle `{count, next, previous, results}` | `{"inspection": [...]}` — **keine Paginierung**, kein `count`. 707 Einträge kamen in einer Antwort, ~12 s |
| 11 Felder je Inspektion | 37 Felder je Inspektion |
| `meta_data` als JSON-String nutzbar | bei uns durchgehend leer (wir senden `""` beim Anlegen) |
| `thumbnail_url_public` = Bild der Inspektion | konstantes Platzhalterbild `…/images/car.png`, unbrauchbar |

Die für einen Archiv-Dienst brauchbaren Felder:

| Feld | Wofür |
|---|---|
| `id` | Inspektions-ID → Schlüssel für `damagereport/` |
| `created_on`, `updated_on` | ISO-8601 mit Zeitzone; `created_on` ist das Filterkriterium |
| `inspection_status` | gemessen: `COMPLETE` (704), `AWAITING_REVIEW` (2), `PROCESSING_AI` (1) |
| `is_images_submitted`, `is_pdf_ready`, `is_user_approved` | Fortschritt ohne Report-Abruf |
| `number_of_images_for_this_inspection_uploaded` | Vollständigkeitsprüfung des Uploads |
| `platform`, `app_version` | trennt Mobile-App-Inspektionen von unseren (`IOS` = unser Client) |
| `process_id` | bei unseren gesetzt (`7BAQMZBAHUYK`), bei App-Inspektionen leer |

**Der Endpoint liefert fremde Inspektionen mit.** Von 707 Einträgen im Zeitraum
01.07.–02.09.2026 stammten 690 von unserem Client (`platform=IOS`) und 17 aus
der Mobile-App (`platform=Android(16)-samsung-a16x` u. ä.). Für einen
Archiv-Dienst ist das die entscheidende Eigenschaft: Er findet Inspektionen,
die er nicht selbst angelegt hat, und braucht daher keinen Upload-Weg.

**Kein Kennzeichen in Liste und Detail — aber im Report.** Weder die Liste noch
das Detail (`GET /api/v1/service/inspections/{id}/`, 100 Felder) enthält
`registration_number`, obwohl `create-inspection` es entgegennimmt. Das Detail
verweist nur per UUID auf ein Fahrzeugobjekt (`car_item = "acc0cdeb-…"`);
`car_number`, `vin_numbers` und `meta_data` waren bei unseren Inspektionen leer.

Der Report liefert es dagegen zuverlässig, auf zwei Wegen:
`RegistrationNumber` auf der oberen Ebene und `vehicleData.RegisterNumber`.
In allen 666 lokalen Reports gefüllt und identisch mit dem erwarteten
Kennzeichen. Entscheidend für ein Archiv: **auch bei fremden Inspektionen.**
Fünf live geprüfte Mobile-App-Inspektionen (`process_id` leer) trugen ihr
Kennzeichen im Report (`TEST006`, `M FN809`, …) — die App schickt es beim
Anlegen mit, und der Report-GET gibt es heraus, auch wenn wir die Inspektion
nicht angelegt haben.

Die Zuordnung Inspektion → Fahrzeug läuft für einen Archiv-Dienst deshalb über
den Report, nicht über die Liste: Tagesliste holen, je ID den Report ziehen,
Kennzeichen daraus lesen. Zwei Vorbehalte bleiben:

- Das Feld ist **Freitext ohne Prüfung**. Unter den App-Inspektionen standen
  `TEST`, `TEST4`, `TEST006`. Wer die Inspektion anlegt, bestimmt die
  Datenqualität; ein Archiv braucht eine Plausibilitätsprüfung und eine Ablage
  für nicht zuordenbare Fälle.
- `MetaData` war in **jedem** geprüften Report leer — weder wir noch die App
  nutzen es. Für die Verknüpfung mit einer Check-in- oder Fall-ID im
  Sixt-System ist das der vorgesehene Platz und die einzige belastbare
  Alternative zum Freitext-Kennzeichen.

Nützliche Zusatzfelder nur im Detail: `is_ai_processing_completed`
(Fertig-Signal ohne Report-Abruf), `last_image_uploaded_time`,
`total_ai_detected_damages_on_inspection` (Fundzahl als Plausibilitätsprüfung
gegen den geladenen Report).

**Laufzeit gemessen (01.09.):** fünf Report-Abrufe dauerten 45 s, also ~9 s je Report
(große JSON-Antworten). Sequenziell wären 2.000 Inspektionen am Tag rund
5 Stunden — der Ingest muss parallelisieren, und dafür brauchen wir das
Rate-Limit von FocalX.

### 3.0.2 Weitere Doku-Abweichungen (bekannt aus dem Betrieb)

- `/signedurls` heißt real
  `POST {base}/api/v2/service/inspections/{id}/signedurls/`, nicht `/signedurls`.
- Das Antwortfeld heißt `genarated_url` (Tippfehler im Produkt), die Doku
  schreibt `generated_url`.
- Beim Bild-Objekt wirkt `position`, nicht das dokumentierte `imageposition` —
  und entscheidend ist ohnehin der Dateiname (Abschnitt 5.4).
- Der Report-GET akzeptiert laut Doku `?x-api-key=…`; mit Bearer-Token allein
  funktioniert er seit Juli 2026 durchgehend.

### 3.0.3 Bilder einer fremden Inspektion — live gemessen (01.09.2026)

Geprüft an einer Mobile-App-Inspektion (`29493f16-…`, Kennzeichen `TSHNAM`,
23 Orientierungen, 56 Funde), die wir nicht angelegt haben:

| Messung | Ergebnis |
|---|---|
| `ImageFileURL` (Vollbild) abrufbar | ja, HTTP 200 für alle 23 |
| Vollbilder gesamt | **129,6 MB** (Schnitt 5,63 MB, Spanne 3,1–7,3 MB) |
| Closeups gesamt | **34,6 MB** über 56 Ausschnitte |
| **Summe je Check-in** | **164,2 MB** |
| URLs signiert | ja (`?`-Parameter) ⇒ sie verfallen, s. Abschnitt 7.1 |

Zwei Folgerungen, die den Archiv-Entwurf bestimmen:

**Die Vollbilder kommen mit heraus, auch bei fremden Inspektionen.** Ein Archiv
kann also den kompletten Walkaround sichern, ohne selbst hochzuladen. Das war
die offene Kernfrage.

**Es gibt kein annotiertes Vollbild.** `ImageFileDrawURL` war bei *allen* 23
Orientierungen zeichengleich mit `ImageFileURL` und lieferte byte-identische
Dateien. Der Feldname verspricht ein eingezeichnetes Bild, geliefert wird das
Original. Markierte Schäden existieren nur als **Closeup-Ausschnitte**. Wer
Vollbilder mit Rahmen will, muss sie selbst rendern — aus `MarkerPoint`
(Abschnitt 5.2), das aber kein Rechteck ist.

**Diese 164 MB sind als Rechengrundlage untauglich — sie stammen von einem
Ausreißer.** Die geprüfte App-Inspektion arbeitete mit 7,5-Megapixel-Fotos; die
Flotte besteht zu 86 % aus 1-Megapixel-Aufnahmen. Die belastbare Zahl steht in
Abschnitt 3.0.4: **11,9 MB je Check-in**, also 2,15 TB statt 29,5 TB über
90 Tage bei 2.000 Check-ins am Tag.

### 3.0.4 Was ein Check-in wirklich wiegt — gemessen am 02.09.2026

Grundlage sind drei frisch angelegte Inspektionen (deren Vollbilder noch lebten)
und die Auswertung aller 744 vollständigen Check-ins unter `data/raw/`.
Werkzeug: `scripts/archive_probe.py`, `scripts/archive_live_test.py`.

**Die Flotte kennt zwei Auflösungen:**

| Auflösung | Anteil der Check-ins | Upload (19 Fotos) |
|---|---|---|
| 678 × 1440 (1,0 MP) | 86 % | ⌀ 1,8 MB |
| 1884 × 3998 (7,5 MP) | 12 % | ⌀ 13,8 MB |

**FocalX gibt die Bilder größer zurück, als es sie bekommen hat.** Über
57 Bildpaare (Upload gegen heruntergeladenes Vollbild): identische Pixelmaße,
aber Median **Faktor 2,73** an Bytes, Spanne 1,71–3,85. Die Bilder werden also
neu kodiert, ohne Informationsgewinn. Konsequenz für jede Mengenrechnung: Vom
Upload auf den Speicherbedarf zu schließen unterschätzt um fast Faktor drei.

| Bestandteil | je Check-in |
|---|---|
| 19 Vollbilder (3,2 MB Upload × 2,73) | 8,8 MB |
| ⌀ 16,9 Closeups à 177 KB | 3,1 MB |
| Report | 0,04 MB |
| **Summe** | **11,9 MB, 38 Objekte** |

Drei gemessene Einzelwerte zur Spannweite: 0,9 MB (19 Bilder, keine Funde),
7,8 MB (19 Funde), 39,6 MB (7,5 MP, 38 Funde).

### 3.0.5 Die Analyse ist zwanzigmal schneller geworden (02.09.2026)

Dasselbe Fahrzeug, dieselben 19 Fotos, zwei Läufe:

| | 05.08.2026 | 02.09.2026 |
|---|---|---|
| `StartTime` → `EndTime` | 5 min 32 s | **17 s** |
| Funde | 32 | **38** |
| davon deckungsgleich über (Slot, Bauteil, Art) | — | 26 |

Kein Cache-Effekt: Die Ergebnisse unterscheiden sich (6 Funde verschwunden,
12 neu). FocalX hat zwischen August und September Modell und Laufzeit
gewechselt, ohne Ankündigung und ohne Versionshinweis im Report.

Zwei Folgen. Für einen Archiv-Dienst ist es eine Entlastung — die Rechnung
„2.000 Inspektionen brauchen 5 Stunden" gilt nicht mehr, und das Rate-Limit
verliert an Schärfe. Für jede Auswertung, die auf FocalX-Funden aufbaut, ist es
eine Warnung: **Messwerte altern mit dem Modell.** Der Recall dieses Projekts
wurde gegen eine Fassung gemessen, die es so nicht mehr gibt.

Das ist zugleich das stärkste Argument für das Archiv: Da ein erneuter Abruf
andere Funde liefert, ist die beim Check-in gespeicherte Fassung die einzige,
die sich später noch belegen lässt.

### 3.1 Der Ablauf einer Analyse

```
POST /api/v1/token/                        → access-Token
GET  /api/v1/customer/user-profile/        → slug + erlaubte Positionslabels
POST /api/v1/service/create-inspection/    → inspection_id
  ├── je Bild: POST …/signedurls/          → presigned POST (url + fields)
  └── je Bild: POST <S3-URL>               → multipart-Upload
POST …/submitImages/                       → AI startet
GET  …/inspections/<id>/  (verifizieren)   → is_images_submitted == true
GET  …/damagereport/  (alle 20 s pollen)   → bis Completed == true
```

Laufzeit in der Praxis, gemessen an `StartTime`/`EndTime` über alle 692 Reports
der Läufe bis August: Median **5,5 Minuten** je Fahrzeug bei 19 Bildern,
Ausreißer bis 121 Minuten. **Seit September gilt das nicht mehr** — drei
Inspektionen am 02.09. liefen in 17 bis 35 Sekunden durch (Abschnitt 3.0.5).
Timeout im Client: 30 Minuten, Poll-Intervall 20 Sekunden. Das Intervall ist
seither überdimensioniert: Es kostet bei jeder Inspektion bis zu 20 Sekunden
Wartezeit auf ein Ergebnis, das längst fertig ist.

### 3.2 Anlegen der Inspektion (Schritt 3)

`application/x-www-form-urlencoded`, nicht JSON. Felder, die der Client sendet:

```
make=unknown  model=unknown  year=2000  registration_number=<KENNZEICHEN>
mileage=0  vin=  body_type=station  color=unknown  energy_type=petrol
body_size=medium  process_id=7BAQMZBAHUYK  platform=IOS  meta_data=
```

Die Fahrzeugangaben landen unverändert im Report unter `vehicleData` und werden
von der Analyse nicht gebraucht — `registration_number` ist das einzige Feld mit
praktischem Wert, weil es den Report wiedererkennbar macht (Feld
`RegistrationNumber`, ohne Sonderzeichen: `FL-46ELZE` → `FL46ELZE`).

### 3.3 Bild-Upload (Schritte 4 und 5)

Anfrage an `…/signedurls/`:

```json
{"images": [{"name": "<inspection_id>_<position>.jpg", "position": "<position>"}]}
```

Antwort ist eine **Liste**; das erste Element enthält:

| Feld | Bedeutung |
|---|---|
| `genarated_url.url` | S3-Ziel für den POST (API-Tippfehler im Feldnamen, so belassen) |
| `genarated_url.fields` | Alle Formularfelder, die der Upload mitsenden muss (`key`, `policy`, `x-amz-signature`, `x-amz-credential`, `x-amz-date`, `x-amz-algorithm`, `x-amz-meta-test_key`) |
| `image_name` | Der Name, unter dem das Bild abgelegt wird |

Der Upload ist ein `multipart/form-data`-POST direkt an S3: erst alle `fields`
als Formularfelder, dann das Feld `file` mit den Bildbytes. **Kein
Authorization-Header** — die Signatur steckt in den Feldern.

---

## 4. Der GET auf den Damage-Report

```http
GET /api/v2/service/inspections/<inspection_id>/damagereport/ HTTP/1.1
Host: <slug>.tenant.focalx.ai
Authorization: Bearer <access-token>
Accept: application/json
```

Als `curl`:

```bash
TOKEN=$(curl -s -X POST https://tenant.focalx.ai/api/v1/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"…","password":"…"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access"])')

SLUG=$(curl -s https://tenant.focalx.ai/api/v1/customer/user-profile/ \
  -H "Authorization: Bearer $TOKEN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["client"]["slug"])')

curl -s "https://$SLUG.tenant.focalx.ai/api/v2/service/inspections/$INSPECTION_ID/damagereport/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Verhalten der Statuscodes (so behandelt der Client sie in `_poll_report`):

| Code | Bedeutung | Reaktion |
|---|---|---|
| 200 + `Completed: false` | Analyse läuft, Teilergebnisse sichtbar | weiter pollen (20 s) |
| 200 + `Completed: true` | fertig | Ergebnis übernehmen |
| 401 | Token abgelaufen | neu anmelden, sofort erneut versuchen |
| 404 | Report noch nicht angelegt | weiter pollen |
| 429/5xx | Serveraussetzer | Backoff, bis zu 4 Versuche |

Der Aufruf ist **idempotent und beliebig oft wiederholbar** — so kommt man an
frische Bild-URLs, wenn die alten abgelaufen sind. Vorsicht dabei: siehe
Falle 9.6.

---

## 5. Aufbau der Antwort

Drei Ebenen: Inspektion → Bildauswertung je Ansicht → Findings je Bild.

```
{ InspectionId, RegistrationNumber, Completed, StartTime, EndTime, vehicleData, …
  OrientationResults: [                    ← ein Eintrag je hochgeladenes Bild
    { OrientationName, ImageFileName, ImageFileURL, …
      Damages: [                           ← ein Eintrag je erkannter Schaden
        { DamageTypeName, PartName, ZoneId, Height, Length,
          CloseUpImageFilename, MarkerPoint, … } ] } ] }
```

### 5.1 Ebene 1 — Inspektion (22 Felder, in allen 692 Reports vorhanden)

| Feld | Typ | Belegte Werte / Bedeutung |
|---|---|---|
| `InspectionId` | str | UUID der Inspektion; taucht in allen Dateinamen wieder auf |
| `RegistrationNumber` | str | Kennzeichen ohne Sonderzeichen |
| `Completed` | bool | Fertig-Flag; **das Abbruchkriterium beim Pollen** |
| `StartTime` / `EndTime` | str | ISO-Zeitstempel mit Zeitzone, Beginn und Ende der Analyse |
| `OrientationResults` | list | Kern der Antwort, siehe 5.2 |
| `ProcessId` | str | `7BAQMZBAHUYK` in allen Reports |
| `Account` | str | `sixttwo` / `sixt` |
| `Provider` | str | `w` in allen Reports |
| `FrameNumber` | str | `1` in allen Reports |
| `vehicleData` | dict | Rückgabe der Anlage-Angaben: `Make`, `Model`, `Mileage`, `BodyType`, `BodySize`, `Segment`, `EngineType`, `Vin`, `RegisterNumber`, `CarTotalWeight`, `TechnicalWeight`, `MotorKilometerPerLiter`, `Variant` |
| `MileageImageFilename` | str | **Fehlbenennung:** enthält in allen 692 Reports exakt die `InspectionId`, kein Dateiname |
| `Mileage` | str | `0` (wir senden keinen Kilometerstand) |
| `UserID`, `Name`, `Email`, `Phone` | str | durchweg der String `"None"` |
| `Latitude`, `Longitude`, `MetaData`, `Workflow` | str | durchweg leer |
| `DynamicForm` | list | durchweg leer |

Für die Auswertung relevant sind damit genau fünf Felder: `InspectionId`,
`RegistrationNumber`, `Completed`, `StartTime`/`EndTime` und
`OrientationResults`.

### 5.2 Ebene 2 — Bildauswertung (`OrientationResults[]`, 12 Felder)

| Feld | Typ | Bedeutung / belegte Werte |
|---|---|---|
| `ImageFileName` | str | S3-Pfad des ausgewerteten Bildes, z. B. `sixt/v2/<insp>_custom_afront-right.jpg`. **Hier steht der von uns hochgeladene Slot-Name** — die einzige verlässliche Quelle dafür, welches unserer 19 Fotos gemeint ist |
| `ImageFileURL` | str | Signierte CloudFront-URL des Fotos (`Expires`, `Signature`, `Key-Pair-Id`) |
| `ImageFileDrawURL` | str | In allen 12.745 Fällen **zeichengleich identisch** mit `ImageFileURL` — kein separates annotiertes Vollbild |
| `OrientationName` | str | Die von der AI erkannte Ansicht aus ihrem eigenen 12er-Vokabular (5.4). Achtung: endet in vielen Reports auf `\r` |
| `ImagePositionMasterName` | str | In allen Daten gleich `OrientationName` |
| `Orientation` | str | Numerische ID der Ansicht — **nicht eindeutig**: `27` steht sowohl für `front` als für `front-right`, `33` für `rear-left` und `wheel-right-front`. Nicht als Schlüssel verwenden |
| `Damages` | list | Findings auf diesem Bild, siehe 5.3. Leer bei 5.771 der Bilder |
| `Luminous` | str/None | Bildqualität, meist leer; sonst `clear-image` (25), `blur-imagae` (77, Tippfehler im Original), `dark-image` (20) |
| `ImageCharacteristic` | list | meist `[]`; sonst `['car']`, `['dirty-car']`, `['rainy-car']` |
| `ImageView` | list | meist `[]`; sonst `['complete-image']` (40), `['incomplete-image']` (19) |
| `ColorType` | str | meist leer; sonst `Grey`, `Black`, `White`, `Silver` |
| `Manual` | str | `"False"` — String, nicht Boolean |

Verteilung der Findings je Bild: 5.771 Bilder ohne Fund, 3.876 mit einem,
2.037 mit zwei, danach abfallend bis maximal 12 auf einem Bild.

### 5.3 Ebene 3 — Das Finding (`Damages[]`, 16 Felder)

Alle 16 Felder sind in allen 11.694 Findings vorhanden.

| Feld | Beispiel | Bedeutung — belegt über 11.694 Findings |
|---|---|---|
| `DamageNumber` | `29b883b3-…` | UUID des Findings. Eindeutig, aber **nicht** der Bezeichner in der Close-up-URL (dort steckt eine zweite, andere UUID) |
| `DamageTypeName` | `scratch` | **Die Schadensart.** 21 verschiedene Werte, siehe 5.5 |
| `DamageTypeCode` | `50` | Code zur Art; zwei Formatfamilien (numerisch und Slug), siehe 5.5 |
| `PartName` | `rear-bumper` | **Das Bauteil.** 60 verschiedene Werte |
| `PartId` | `148` | Numerische Bauteil-ID; bei `grille-upper`, `grille-lower`, `wheel` durchgehend `None` |
| `PartLocation` | `148` | In allen 11.694 Findings **identisch mit `PartId`** — redundant |
| `ZoneId` | `DD71` | Fahrzeugzone, 37 Werte. Präfix = Seite: `AA` links, `BB` front, `CC` rechts, `DD` heck, `ZZ` Sonstiges. Je Bauteil immer dieselbe Zone, umgekehrt können mehrere Bauteile in einer Zone liegen |
| `Height` | `18.76` | Größenmaß, Einheit nicht dokumentiert (5.6) |
| `Length` | `95.54` | Größenmaß, Einheit nicht dokumentiert (5.6) |
| `DamageCategoryID` | `3` | Schadensklasse 0–6; korreliert mit der Art (5.7) |
| `DamageCategoryName` | `3` | **Immer gleich `DamageCategoryID`** — kein Klartext |
| `DamageSubAreaCode` | `E0` | Drei Werte: `00` (überall), `E0` (nur Front-/Heckzonen), `F0` (nur Seitenzonen) |
| `RepairMethodCode` | `6` | **Konstante:** `6` in allen 11.694 Findings. Kein Informationsgehalt |
| `Manual` | `false` | Boolean (anders als auf Bildebene); durchweg `false` = KI-Fund |
| `MarkerPoint` | `[1,1,1,1]` | **Keine Bounding Box** — siehe Abschnitt 6 |
| `CloseUpImageFilename` | URL | Annotierter Bildausschnitt, siehe Abschnitt 7 |

### 5.4 Positionsvokabular: unsere 19 Slots ↔ die 12 AI-Ansichten

Wir laden 19 Bilder unter Tenant-Slot-Namen hoch; die AI ordnet sie ihrem
eigenen, gröberen Vokabular zu. Belegte Zuordnung:

| hochgeladener Slot (`ImageFileName`) | erkannt als (`OrientationName`) |
|---|---|
| `custom_afront` | `front` |
| `custom_afront-bonnet-windshield` | `front` |
| `custom_afront-left` | `front-left` |
| `custom_front-left-fender` | `front-left` |
| `custom_afront-right` | `front-right` |
| `custom_abcfront-right-fender` | `front-right` |
| `custom_arear` | `rear` |
| `custom_arear-left-bumper` | `rear-left` |
| `custom_arear-left` | `rear-left` |
| `custom_arear-right-bumper` | `rear-right` |
| `custom_arear-right` | `rear-right` |
| `custom_aleft-front-1` | `left-front` |
| `custom_aleft-rear` | `left-rear` |
| `custom_aright-front-1` | `right-front` |
| `custom_abcright-rear` | `right-rear` |
| `custom_afront-left-wheel` | `wheel-left-front` |
| `custom_afront-right-wheel` | `wheel-right-front` |
| `custom_arear-left-wheel` | `wheel-left-rear` |
| `custom_arear-right-wheel` | `wheel-right-rear` |

Drei Konsequenzen:

Erstens ist die Abbildung **nicht umkehrbar** — aus `front-left` lässt sich
nicht rekonstruieren, ob das Diagonalfoto oder das Kotflügelfoto gemeint war;
wer das braucht, muss `ImageFileName` auswerten.

Zweitens tragen die Slot-Namen den Präfix `custom_` (11.776 Bilder), ältere
Läufe dagegen nicht (`front`, `rear-left`, `aleft-front`, …). Der Name, den man
beim Upload vergibt, erscheint unverändert im Report.

Drittens — und das ist der Grund, die Tabelle nicht als Gesetz zu lesen — **hält
die Zuordnung nur für Labels aus dem Katalog.** Gemessen über die Läufe:

| Labelsatz | Anteil der dominanten Ansicht je Slot |
|---|---|
| `custom_`-Labels (Lauf fl500, 11.377 Bilder) | **99,4 %** |
| Katalog-Labels ohne Präfix (`front`, `rear-left`, …) | 95–100 % je Slot |
| Die `a…`-Labels ohne `frame_url` (Experimentierphase v1) | **nur 46–100 %, im Mittel deutlich schlechter** |

Konkret bei den `a…`-Labels: `afront-left` landete in 13 Bildern auf vier
verschiedenen Ansichten (`left-front` 6×, `front-left` 2×, `wheel-left-front`
2×, `wheel` 1×), `arear-left-wheel` in 11 Bildern auf `wheel-left-rear` (6×),
`left-rear` (2×), `wheel-right-rear` (2×) und `wheel` (1×). Das ist dieselbe
Ursache wie in Falle 9.1: Labels ohne `frame_url` wertet die AI nicht sauber
aus. Für aktuelle Läufe ist die Tabelle belastbar, für die frühen v1-Autos
nicht.

### 5.5 Schadensarten mit Codes

| `DamageTypeName` | `DamageTypeCode` | Findings |
|---|---|---|
| `scratch` | `50` | 4.000 |
| `dent` | `20` | 2.437 |
| `curbrash` | `17` | 2.252 |
| `Scuffed` | `scuffed` | 1.550 |
| `contamination-exterior` | `29` | 567 |
| `misaligned` | `23` | 452 |
| `fluid-spillage-exterior` | `30` | 165 |
| `Scratched Except Glass` | `scratched-except-glass` | 94 |
| `part-missing` | `18` | 81 |
| `Chipped Paneledge` | `chipped-paneledge` | 42 |
| `Chipped Except Glassandpanel` | `chipped-except-glassandpanel` | 16 |
| `Chipped Glass` | `chipped-glass` | 14 |
| `Scratched Glass` | `scratched-glass` | 8 |
| `wreckage` / `stone-chip` / `rust` | `24` / `57` / `13` | je 3 |
| `Glass Broken` | `glass-broken` | 2 |
| *(leer)* | `1` | 2 |
| `cut` / `offpaint` / `tear` | `16` / `15` / `27` | je 1 |

**Zwei Namensschemata in einem Feld:** kleingeschriebene Kurznamen mit
numerischem Code (`scratch`/`50`) neben Title-Case-Namen mit Slug-Code
(`Scuffed`/`scuffed`). Wer auf den Namen filtert, muss normalisieren — der
Client tut das mit `.strip().lower()`. Dasselbe Problem in `PartName`:
`fog-light-right` und `Fog Light Right` bezeichnen dasselbe Bauteil.

### 5.6 `Height` und `Length` — das einzige Größensignal

Die Einheit ist nicht dokumentiert. Belegt ist:

- Wertebereich 0 bis 6.406; Median 59,8 (`Height`) und 79,9 (`Length`).
- 22 Findings haben in beiden Feldern 0.
- Die Werte skalieren mit dem Bauteil: Räder (Felgenschäden) liegen im Median
  bei 165–180, Karosserieteile wie `rear-bumper` bei 42/62, `bonnet` bei 38/48,
  `grille-upper` bei 31/42.

Das Muster passt zu Millimetern der Schadensausdehnung, die Extremwerte über
6.000 (also über 6 m) widerlegen eine saubere mm-Interpretation aber. Als
belastbare Größenangabe taugt das Feld deshalb nicht; als grober Sortierschlüssel
innerhalb eines Bauteils ist es plausibel.

### 5.7 `DamageCategoryID` — Schadensklasse

Die Zahl gruppiert die Arten, ist aber keine Schwere:

| ID | dominante Arten |
|---|---|
| 1 | `curbrash` (2.191), `Scuffed` (1.336), `contamination-exterior` (509) |
| 2 | Mischung, alle Arten in kleiner Zahl |
| 3 | `scratch` (3.780), `dent` (2.293) |
| 4 | Mischung, klein (34 Findings) |
| 5 | `misaligned` (418), `part-missing` (72) |
| 6 | `part-missing` (7) |
| 0 / leer | je 2 Findings |

Gelesen: 3 = klassische Lack-/Blechschäden, 1 = oberflächlich/Verschmutzung,
5 = strukturell (verschoben, fehlend). Das ist eine Beobachtung aus der
Verteilung, keine Zusicherung des Anbieters.

---

## 6. Die Bounding-Box-Frage — beantwortet

**Die API liefert keine Koordinaten des Schadens im Bild.** Das ist der
wichtigste Befund dieser Analyse, weil er nicht offensichtlich ist: Es gibt ein
Feld, das genau danach aussieht.

`MarkerPoint` enthält über alle 11.694 Findings hinweg genau zwei Werte:

| Wert | Findings | Bedeutung |
|---|---|---|
| `"[1,1,1,1]"` | 11.210 | Regelfall |
| `"[100,100,100,100]"` | 484 | Sonderfall, siehe unten |

Es sind Strings, keine Listen, und beide Werte sind Konstanten — sie variieren
nicht mit Bauteil, Ansicht oder Schadensart. Als Box wären sie ohnehin
unbrauchbar (ein Punkt bzw. ein Quadrat an derselben Stelle).

Der Sonderfall ist trotzdem informativ: **`[100,100,100,100]` korreliert
perfekt (484 von 484) mit Findings, deren `CloseUpImageFilename` von genau zwei
Findings geteilt wird.** Es gibt 242 solcher Paare. Der Wert markiert also
Findings, die sich einen annotierten Ausschnitt mit einem zweiten Finding
teilen — nützlich als Dublettenhinweis, nicht als Geometrie.

**Wo die Box tatsächlich existiert:** eingezeichnet im Close-up-Bild
(`…_annotated_closeshot_thumbnail.jpg`). Sie ist Pixel im JPEG, nicht Zahl in
der Antwort. Deshalb arbeitet dieses Projekt mit `scripts/locate_pictograms.py`:
Ein LLM schaut sich Close-up und Bauteilnamen an und projiziert den Schaden auf
ein Piktogramm — der Ersatz für die Koordinaten, die die API nicht hergibt.

---

## 7. Wo die Bilder liegen

### 7.1 Auf Anbieterseite: zwei CloudFront-Verteilungen

| Was | Verteilung | Signatur | Anatomie |
|---|---|---|---|
| Walkaround-Foto (das von uns hochgeladene Bild) | `d28fru6cy3377n.cloudfront.net` | **signiert**: `?Expires=…&Signature=…&Key-Pair-Id=…` | `/<account>/v2/<insp>_<slot>.jpg` |
| Close-up (annotierter Ausschnitt) | `de7tbluxc5a3b.cloudfront.net` | **unsigniert**, keine Query | `/<account>/<datum><epoch-ms>_<account>/v2/<insp>_<slot><uuid>_annotated_closeshot_thumbnail.jpg` |

Belegte Eigenschaften:

- **Gültigkeit der Foto-URLs: exakt 168 Stunden (7 Tage)** ab Ende der Analyse —
  über alle 12.745 Bilder hinweg identisch (Median 168,0 h, Maximum 169,9 h).
  Am 02.09. mit einem Negativbeweis bestätigt: drei **gespeicherte** URLs aus
  Juli und August erneut abgerufen, alle HTTP 403.
- **Die Frist gilt der URL, nicht dem Bild — und sie ist erneuerbar.** Ein
  erneuter `damagereport/`-GET liefert frisch signierte URLs mit neuen sieben
  Tagen. Am 02.09. an einem kompletten Tag vom 04.08. geprüft, also vier Wochen
  nach der Analyse: **7.814 von 7.814 Vollbildern kamen mit HTTP 200**. Es
  braucht dafür keine „Generate Link API", der gewöhnliche Report-GET genügt.
  Damit ist ein mehrtägiger Ausfall des Archiv-Dienstes **kein** automatischer
  Datenverlust.
- **Aber nur innerhalb des aktuellen Mandanten.** Der Mandant wechselte Ende
  Juli von `sixt` auf `sixttwo` (Feld `Account`, und im Pfad:
  `cloudfront.net/sixt/v2/…` gegen `…/sixttwo/v2/…`, dieselbe Verteilung
  `d28fru6cy3377n`). Drei Inspektionen vom 21.07. aus dem alten Mandanten sind
  mit den heutigen Zugangsdaten **nicht mehr abrufbar**: Report-GET ⇒ HTTP 500,
  und in der Tagesliste tauchen sie gar nicht erst auf. Es ist kein
  Altersproblem — der 28.07. (36 Tage alt) liefert im neuen Mandanten
  einwandfrei, ebenso der 26.07. und der 03.08. Ein Mandantenwechsel ist damit
  ein **harter Schnitt**: Was bis dahin nicht archiviert ist, ist verloren, egal
  wie jung es war. Der Fehlercode führt dabei in die Irre — 500 sieht nach
  Serverpanne aus, nicht nach fehlender Berechtigung.
- **Close-up-URLs verfallen nicht mit — bis auf wenige Ausreißer.** Stichprobe
  über 25 Reports: 23 von 23 lieferten HTTP 200, der älteste vom 21.07. Über
  einen ganzen Tag gemessen (04.08., vier Wochen alt): **10 von 6.673 Closeups
  waren weg (0,15 %)**, verteilt auf drei Inspektionen. Sie tragen keine
  Ablaufinformation und werden anders als die Vollbilder auch **nicht** durch
  einen neuen Report-GET erneuert — eine verlorene Closeup-URL bleibt verloren.
  Deshalb weiterhin sofort laden.
- Die UUID im Close-up-Dateinamen ist **nicht** die `DamageNumber`, sondern eine
  zweite, sonst nirgends im Report auftauchende ID. Die Verknüpfung
  Finding → Bild läuft ausschließlich über das Feld `CloseUpImageFilename`.
- Alle 11.694 Findings haben eine Close-up-URL; 11.452 sind eindeutig, 242 von
  je zwei Findings geteilt (Abschnitt 6).
- Close-ups sind Thumbnails: längste Kante 1.024 px, häufigste Größen
  1024×1024 und 678×1024.

### 7.2 Lokal im Repo

| Pfad | Inhalt |
|---|---|
| `data/raw/<datum>/<PLATE>__<checkin8>/<SLOT>.jpg` | Die Check-in-Fotos, die hochgeladen werden (aus Lynx) |
| `data/results/<PLATE>__<checkin8>/focalx_report.json` | **Die unveränderte GET-Antwort** — die Quelle für alle Aussagen hier |
| `data/results/<PLATE>__<checkin8>/closeups/F<n>.jpg` | Die heruntergeladenen Close-ups, umbenannt auf den Finding-Schlüssel `F1`, `F2`, … |
| `data/results/<PLATE>__<checkin8>.json` | Das aufbereitete Ergebnis: Findings auf `F<n>` reduziert (`key`, `position`, `orientation`, `part`, `type`, `closeup`), dazu Ground Truth und Mapping |
| `data/results/inflight/<checkin>.json` | Laufende Inspektion (`inspection_id`), damit ein Abbruch nicht 19 Bilder neu hochladen muss; nach einer Stunde verfallen |
| `data/runs/<run>/results/…` | Dieselbe Struktur für versionierte Läufe (`v2`, `v3`, `fl500`) |

Die Reduktion von der API-Antwort auf `data/results/<checkin>.json` findet in
`eval/pipeline.py` statt. Verworfen werden dabei alle Felder ohne
Informationsgehalt (`RepairMethodCode`, `PartLocation`, `MarkerPoint`,
`Manual`, `DynamicForm`, die `None`-Strings) sowie Glas- und Interieur-Findings
(`is_exterior_non_glass`). **Der Rohreport bleibt vollständig erhalten**, jede
später gewünschte Größe lässt sich also ohne neuen API-Lauf nachziehen — auch
`Height`/`Length`, `ZoneId` und `DamageCategoryID`, die aktuell nicht
ausgewertet werden.

---

## 8. Der Finding-Schlüssel `F<n>`

Nichts an `F1`, `F2`, … kommt von FocalX. Die Schlüssel entstehen in
`eval/pipeline.py`, indem die Findings in Reportreihenfolge durchnumeriert
werden, **nachdem** Glas/Interieur ausgefiltert wurde. Sie sind damit stabil nur
innerhalb eines gespeicherten Ergebnisses. Wer nach einem erneuten GET dieselben
Findings wiedererkennen will, muss über `(position, part, type)` matchen — die
Reihenfolge kann sich ändern (siehe 9.6).

---

## 9. Die Fallen

Alle hier genannten Punkte sind belegt oder in früheren Sitzungen teuer bezahlt
worden.

**9.1 Positionslabels sind ein festes Vokabular.** Ein `position`, das nicht im
Katalog aus `/api/v1/customer/user-profile/` (`custom_data[].label`) steht, wird
klaglos angenommen — die AI ignoriert das Bild dann still. Ergebnis: Die
Inspektion läuft 25 Minuten und liefert einen leeren Report. Die
Integrationsdokumentation des Anbieters behauptet freie Wahl; das ist falsch.

**9.2 Die AI liest die Ansicht aus dem Dateinamen, nicht aus `position`.**
Verifiziert am 28.07.2026: `position="front-left"` mit Datei
`custom_afront-left.jpg` ergibt einen leeren Report. Der Slot-Name muss im
Namen des hochgeladenen Objekts stehen (`<insp>_<slot>.jpg`).

**9.3 `submitImages` kann ein stiller No-Op sein.** HTTP 200, aber
`is_images_submitted` bleibt `false` und die Analyse startet nie. Nur ein GET auf
`…/inspections/<id>/` zeigt das; der Client wiederholt bis das Flag kippt.

**9.4 `OrientationName` endet oft auf `\r`.** Im Report steht `"front\r"`. Wer
darauf vergleicht, ohne zu strippen, findet nichts.

**9.5 `MarkerPoint` ist keine Box.** Abschnitt 6.

**9.6 Ein erneuter GET kann andere Findings liefern.** Der Endpoint ist der
einzige Weg zu frischen Bild-URLs, aber Anzahl und Reihenfolge der Findings
können sich zwischen zwei Abrufen unterscheiden. Deshalb niemals über den Index
oder über `F<n>` zuordnen, sondern über `(position, part, type)`.

Zwei verschiedene Effekte, beide am 02.09. beziffert — sie nicht zu verwechseln
spart viel Verwirrung:

**Über Wochen ändert sich das Ergebnis.** Dasselbe Fahrzeug: 32 Funde im
August, 38 im September, nur 26 deckungsgleich. Ursache ist der Modellwechsel
(Abschnitt 3.0.5). Wer eine Auswertung auf gespeicherten Funden aufbaut, darf
den Report nicht nachladen.

**Innerhalb von Minuten ändert sich nur die Reihenfolge.** Ein ganzer Tag
(425 Inspektionen) zweimal abgerufen: **8 Reports (1,9 %) wichen ab, alle
ausschließlich durch vertauschte Einträge im selben `Damages`-Feld** — kein
einziger Wert war anders. Dazu kommt: Jeder Abruf liefert neue `Expires`- und
`Signature`-Parameter, weshalb ein reiner Bytevergleich **jeden** Zweitabruf als
Abweichung meldet. Wer Reports auf Gleichheit prüft, muss beides herausrechnen
(so macht es `archive/ingest.py:fingerprint`).

**9.10 Das Kennzeichenformat ist nicht stabil.** Dasselbe Fahrzeug, dieselbe
Eingabe beim Anlegen: Der Report vom 05.08. trug `TXWCV5796`, der vom 02.09.
`TX-WCV5796`. FocalX hat seine Normalisierung geändert. Wer `RegistrationNumber`
als Schlüssel verwendet, findet dasselbe Auto irgendwann unter zwei Namen —
immer selbst normalisieren (`re.sub(r"[^A-Za-z0-9]", "", plate).upper()`).

**9.11 Die zurückgelieferten Vollbilder sind nicht die hochgeladenen.** Gleiche
Pixelmaße, aber Median 2,73-mal so viele Bytes (Abschnitt 3.0.4). Für
Prüfsummenvergleiche zwischen Upload und Download taugt das nicht, und
Mengenschätzungen aus der Upload-Größe liegen um fast Faktor drei daneben.

**9.7 Zwei Namensschemata in `DamageTypeName` und `PartName`.** `scratch` neben
`Scuffed`, `fog-light-right` neben `Fog Light Right`. Immer normalisieren.

**9.8 `Orientation` (die Zahl) ist nicht eindeutig.** `27` steht für `front` und
für `front-right`. Als Schlüssel unbrauchbar.

**9.9 Bild-URLs verfallen nach 7 Tagen — aber Vollbilder sind nachholbar.**
Eine gespeicherte `ImageFileURL` ist nach 168 Stunden tot; ein neuer
Report-GET liefert aber frische Signaturen und damit dieselben Bilder wieder
(7.814 von 7.814 nach vier Wochen, Abschnitt 7.1). **Für Close-ups gilt das
nicht** — deren URLs werden nicht neu signiert, und 0,15 % waren nach vier
Wochen verschwunden. Close-ups also weiterhin sofort laden; die Pipeline tut
das vor allem anderen.

---

## 10. Was die Schnittstelle nicht hergibt

Zur Vollständigkeit, weil es die Grenzen des Benchmarks bestimmt:

- **Keine Koordinaten** des Schadens im Foto (Abschnitt 6).
- **Keine Konfidenz** je Finding — kein Score, keine Wahrscheinlichkeit.
- **Keine Schweregrad-Angabe**, die mit der SHARK-Systematik vergleichbar wäre;
  `Height`/`Length` sind das einzige Größensignal und nicht kalibriert (5.6).
- **Keine Reparaturempfehlung** trotz des Feldnamens `RepairMethodCode`, der
  konstant `6` ist.
- **Keine Zuordnung mehrerer Ansichten zum selben physischen Schaden.** Ein
  Schaden, der auf drei Fotos sichtbar ist, erzeugt drei unabhängige Findings.
  Das Entdoppeln ist Aufgabe unseres Clusterings (`eval/mapping.py`).

---

*Verifiziert am 01.09.2026 gegen 692 Reports mit 11.694 Findings aus den Läufen
v1, v2, v3 und fl500; ergänzt am 02.09.2026 um drei frisch angelegte
Inspektionen (Mengen, Laufzeit, Modellwechsel, Kennzeichenformat).*
