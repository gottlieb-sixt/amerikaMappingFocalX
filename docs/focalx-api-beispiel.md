# FocalX-API — eine echte Antwort, Feld für Feld

Begleitdokument zu [`focalx-api.md`](focalx-api.md). Hier steht **eine einzige,
echte Antwort** vollständig und unverändert, dazu die Erklärung jedes Feldes am
konkreten Wert.

**Die vollständige Rohantwort liegt als Datei bei:**
[`beispiel/damagereport-beispiel.json`](beispiel/damagereport-beispiel.json)
(32.380 Bytes, unverändert so, wie der Server sie geliefert hat). In diesem
Dokument sind nur die CloudFront-Signaturen durch `…` gekürzt — sie sind je
900 Zeichen lang und inzwischen abgelaufen.

| | |
|---|---|
| Fahrzeug | `TX-YDM4339`, Check-in `TX-YDM4339__912dcb4c` |
| Inspektion | `490a1810-1979-4237-9b5d-ff6fc905c446` |
| Analyse | 04.08.2026, 11:15:41 bis 11:21:07 UTC — **5,4 Minuten** |
| Ergebnis | 19 ausgewertete Bilder, **6 Findings**, 5 verschiedene Schadensarten |
| Herkunft | Lauf `fl500`, Original unter `data/runs/fl500/results/TX-YDM4339__912dcb4c/focalx_report.json` |

---

## 1. Die Anfrage, die diese Antwort erzeugt hat

```http
GET /api/v2/service/inspections/490a1810-1979-4237-9b5d-ff6fc905c446/damagereport/ HTTP/1.1
Host: sixttwo.tenant.focalx.ai
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…
Accept: application/json
```

Mehr braucht es nicht: kein Query-Parameter, kein Body, keine Paginierung. Der
Slug `sixttwo` im Hostnamen stammt aus dem Benutzerprofil, die UUID aus dem
Anlegen der Inspektion.

---

## 2. Die Antwort, Ebene 1: der Rahmen

Das sind die 22 Top-Level-Felder **wörtlich so, wie sie kommen** — nur
`OrientationResults` ist hier ausgeklammert und folgt in Abschnitt 3:

```json
{
  "InspectionId": "490a1810-1979-4237-9b5d-ff6fc905c446",
  "ProcessId": "7BAQMZBAHUYK",
  "Account": "sixttwo",
  "Provider": "w",
  "FrameNumber": "1",
  "MetaData": "",
  "UserID": "None",
  "StartTime": "2026-08-04 11:15:41.436831+00:00",
  "EndTime": "2026-08-04 11:21:07.058924+00:00",
  "RegistrationNumber": "TXYDM4339",
  "Name": "None",
  "Email": "None",
  "Phone": "None",
  "Mileage": "0",
  "MileageImageFilename": "490a1810-1979-4237-9b5d-ff6fc905c446",
  "Latitude": "",
  "Longitude": "",
  "Completed": true,
  "OrientationResults": [ … 19 Einträge, siehe Abschnitt 3 … ],
  "Workflow": "",
  "DynamicForm": [],
  "vehicleData": {
    "Make": "unknown",
    "Model": "unknown",
    "Mileage": "0",
    "CarTotalWeight": "None",
    "BodyType": "Station",
    "BodySize": "medium",
    "Segment": "",
    "EngineType": "None",
    "Vin": "0",
    "RegisterNumber": "TXYDM4339",
    "TechnicalWeight": "None",
    "MotorKilometerPerLiter": "None",
    "Variant": "None"
  }
}
```

Was hier zu sehen ist:

- **`Completed: true`** ist das einzige Signal, dass die Analyse fertig ist. Nur
  dann darf man die Findings verwenden.
- **`RegistrationNumber`** ist unser `TX-YDM4339` ohne Bindestrich. Der Server
  normalisiert das Kennzeichen; wer damit zurückmappt, muss dasselbe tun.
- **`MileageImageFilename`** enthält keinen Dateinamen, sondern noch einmal die
  `InspectionId` — in allen 692 geprüften Reports so.
- **`UserID`, `Name`, `Email`, `Phone`** sind der *String* `"None"`, nicht
  `null`. Eine Prüfung auf `is None` greift hier nicht.
- **`vehicleData`** ist ein Echo unserer Anlage-Angaben. Wir senden bewusst
  `make=unknown`, `year=2000` und so weiter, weil die Analyse diese Werte nicht
  braucht; entsprechend steht hier `unknown`. `BodyType` kommt großgeschrieben
  zurück (`Station`), obwohl wir `station` gesendet haben.
- **`Provider: "w"`, `FrameNumber: "1"`, `ProcessId`** sind über alle Reports
  konstant, tragen also keine Information über dieses Fahrzeug.

---

## 3. Ebene 2: die 19 ausgewerteten Bilder

`OrientationResults` hat genau einen Eintrag je hochgeladenem Foto — hier alle
19 in Reportreihenfolge. Die Spalte „Slot" ist der Name, unter dem *wir* das
Bild hochgeladen haben (aus `ImageFileName` gelesen), die Spalte „erkannt als"
das, was die AI daraus gemacht hat (`OrientationName`):

| # | Slot (unser Upload) | erkannt als | `Orientation` | Findings |
|---:|---|---|---:|---:|
| 0 | `custom_aleft-front-1` | `left-front` | 58 | 0 |
| 1 | `custom_arear-left` | `rear-left` | 33 | 0 |
| 2 | `custom_arear-right-bumper` | `rear-right` | 19 | **2** |
| 3 | `custom_arear` | `rear` | 55 | **1** |
| 4 | `custom_arear-right` | `rear-right` | 19 | **1** |
| 5 | `custom_abcright-rear` | `right-rear` | 56 | 0 |
| 6 | `custom_aright-front-1` | `right-front` | 54 | 0 |
| 7 | `custom_afront-right` | `front-right` | 27 | **1** |
| 8 | `custom_abcfront-right-fender` | `front-right` | 27 | **1** |
| 9 | `custom_afront` | `front` | 27 | 0 |
| 10 | `custom_afront-bonnet-windshield` | `front` | 27 | 0 |
| 11 | `custom_arear-right-wheel` | `wheel-right-rear` | 44 | 0 |
| 12 | `custom_arear-left-wheel` | `wheel-left-rear` | 22 | 0 |
| 13 | `custom_afront-right-wheel` | `wheel-right-front` | 33 | 0 |
| 14 | `custom_afront-left-wheel` | `wheel-left-front` | 11 | 0 |
| 15 | `custom_aleft-rear` | `left-rear` | 53 | 0 |
| 16 | `custom_front-left-fender` | `front-left` | 57 | 0 |
| 17 | `custom_arear-left-bumper` | `rear-left` | 33 | 0 |
| 18 | `custom_afront-left` | `front-left` | 57 | 0 |

Drei Dinge, die man an dieser Tabelle direkt sieht:

1. **Die Reihenfolge ist nicht die Upload-Reihenfolge** und nicht sortiert. Sie
   ist nur innerhalb einer Antwort stabil.
2. **Die AI-Ansichten sind gröber als unsere Slots.** Vier verschiedene Slots
   landen auf `front-right`/`front` (Zeilen 7–10). Aus `OrientationName` lässt
   sich also nicht rekonstruieren, welches Foto gemeint war — dafür braucht man
   `ImageFileName`.
3. **`Orientation` (die Zahl) ist als Schlüssel unbrauchbar.** In dieser einen
   Antwort steht `27` für `front-right` (Zeilen 7, 8) *und* für `front`
   (Zeilen 9, 10), und `33` für `rear-left` (Zeilen 1, 17) *und* für
   `wheel-right-front` (Zeile 13).

### 3.1 Ein Bild ohne Fund — vollständig

```json
{
  "Orientation": "58",
  "Manual": "False",
  "ImagePositionMasterName": "left-front\r",
  "OrientationName": "left-front\r",
  "ImageFileName": "sixttwo/v2/490a1810-1979-4237-9b5d-ff6fc905c446_custom_aleft-front-1.jpg",
  "ImageFileURL": "https://d28fru6cy3377n.cloudfront.net/sixttwo/v2/490a1810-1979-4237-9b5d-ff6fc905c446_custom_aleft-front-1.jpg?Expires=1786447272&Signature=…&Key-Pair-Id=K1M3AE88YGGVIM",
  "ImageFileDrawURL": "https://d28fru6cy3377n.cloudfront.net/sixttwo/v2/490a1810-1979-4237-9b5d-ff6fc905c446_custom_aleft-front-1.jpg?Expires=1786447272&Signature=…&Key-Pair-Id=K1M3AE88YGGVIM",
  "Luminous": "blur-imagae",
  "ImageCharacteristic": [],
  "ImageView": [],
  "ColorType": "",
  "Damages": []
}
```

Wichtig daran:

- **`OrientationName` endet auf `\r`.** Das ist kein Darstellungsfehler, das
  Zeichen ist im JSON. Wer `== "left-front"` vergleicht, findet nichts.
- **`Manual` ist hier der String `"False"`** — auf Finding-Ebene ist dasselbe
  Feld ein echter Boolean `false`. Zwei Typen, ein Name.
- **`ImageFileDrawURL` ist zeichengleich identisch mit `ImageFileURL`.** Der
  Name legt ein annotiertes Vollbild nahe, es gibt keins.
- **`Luminous: "blur-imagae"`** meldet ein unscharfes Bild — mit dem Tippfehler
  des Anbieters. Genau dieses Foto hat keinen Fund; wer Recall-Ausfälle
  untersucht, hat hier ein verwertbares Signal.
- **`Expires=1786447272`** entspricht dem 11.08.2026, 13:21 Uhr — exakt
  168 Stunden nach `EndTime`. Die URL ist heute tot (HTTP 403).

### 3.2 Ein Bild mit zwei Funden — vollständig

```json
{
  "Orientation": "19",
  "Manual": "False",
  "ImagePositionMasterName": "rear-right\r",
  "OrientationName": "rear-right\r",
  "ImageFileName": "sixttwo/v2/490a1810-1979-4237-9b5d-ff6fc905c446_custom_arear-right-bumper.jpg",
  "ImageFileURL": "https://d28fru6cy3377n.cloudfront.net/sixttwo/v2/…_custom_arear-right-bumper.jpg?Expires=1786447272&Signature=…&Key-Pair-Id=K1M3AE88YGGVIM",
  "ImageFileDrawURL": "https://d28fru6cy3377n.cloudfront.net/sixttwo/v2/…_custom_arear-right-bumper.jpg?Expires=1786447272&Signature=…&Key-Pair-Id=K1M3AE88YGGVIM",
  "Luminous": null,
  "ImageCharacteristic": [],
  "ImageView": [],
  "ColorType": "",
  "Damages": [
    {
      "DamageNumber": "fa666e55-7879-4db1-8535-958cada19d2d",
      "MarkerPoint": "[1,1,1,1]",
      "ZoneId": "CC62",
      "PartId": "117",
      "PartName": "fender-rear-right",
      "Manual": false,
      "Height": 188.474870017331,
      "Length": 87.08838821490453,
      "DamageCategoryID": "3",
      "DamageCategoryName": "3",
      "DamageSubAreaCode": "F0",
      "DamageTypeCode": "20",
      "DamageTypeName": "dent",
      "PartLocation": "117",
      "RepairMethodCode": "6",
      "CloseUpImageFilename": "https://de7tbluxc5a3b.cloudfront.net/sixttwo/2026-08-1785842457994_sixttwo/v2/490a1810-1979-4237-9b5d-ff6fc905c446_custom_arear-right-bumper2e73c588-44d7-4a2f-ad91-00aaa8b4485b_annotated_closeshot_thumbnail.jpg"
    },
    {
      "DamageNumber": "7bdd9167-475c-49df-a5a0-c7ddb50bed84",
      "MarkerPoint": "[1,1,1,1]",
      "ZoneId": "DD72",
      "PartId": "25",
      "PartName": "trunk-cover",
      "Manual": false,
      "Height": 128.68284228769483,
      "Length": 115.68457538994792,
      "DamageCategoryID": "1",
      "DamageCategoryName": "1",
      "DamageSubAreaCode": "E0",
      "DamageTypeCode": "29",
      "DamageTypeName": "contamination-exterior",
      "PartLocation": "25",
      "RepairMethodCode": "6",
      "CloseUpImageFilename": "https://de7tbluxc5a3b.cloudfront.net/sixttwo/2026-08-1785842458040_sixttwo/v2/490a1810-1979-4237-9b5d-ff6fc905c446_custom_arear-right-bumperd2b030d2-891e-4af1-ab20-a84e9a1b6809_annotated_closeshot_thumbnail.jpg"
    }
  ]
}
```

Dieses eine Foto zeigt exemplarisch, warum die Auswertung aufwendig ist: **ein
Bild, zwei völlig verschiedene Meldungen** — eine Delle am rechten hinteren
Kotflügel und eine Verschmutzung auf der Heckklappe. Der Slot heißt
`arear-right-bumper`, gemeldet werden Teile, die weder Stoßfänger noch
zwangsläufig rechts sind. Die Fotoposition sagt also nichts über den Ort des
Schadens; maßgeblich ist `PartName`.

---

## 4. Ebene 3: das Finding, Feld für Feld

Am ersten Fund des obigen Bildes:

| Feld | Wert | Was es bedeutet |
|---|---|---|
| `DamageNumber` | `fa666e55-7879-4db1-8535-958cada19d2d` | Eindeutige ID des Fundes. **Kommt in der Close-up-URL nicht vor** — dort steht `2e73c588-…`, eine andere UUID |
| `DamageTypeName` | `dent` | **Die Schadensart** |
| `DamageTypeCode` | `20` | Code dazu; `20` = dent |
| `PartName` | `fender-rear-right` | **Das Bauteil** — die einzig verlässliche Ortsangabe |
| `PartId` | `117` | Numerische Bauteil-ID |
| `PartLocation` | `117` | Kopie von `PartId`, ohne Zusatznutzen |
| `ZoneId` | `CC62` | Fahrzeugzone; Präfix `CC` = rechte Seite |
| `Height` | `188.47…` | Größenmaß, Einheit undokumentiert |
| `Length` | `87.09…` | Größenmaß, Einheit undokumentiert |
| `DamageCategoryID` | `3` | Klasse 3 = klassische Lack-/Blechschäden |
| `DamageCategoryName` | `3` | Kopie von `DamageCategoryID`, kein Klartext |
| `DamageSubAreaCode` | `F0` | `F0` erscheint nur bei Seitenzonen (`AA`/`CC`), `E0` nur bei Front/Heck |
| `RepairMethodCode` | `6` | Konstante in allen 11.694 Findings |
| `Manual` | `false` | KI-Fund, nicht manuell erfasst |
| `MarkerPoint` | `"[1,1,1,1]"` | **Keine Bounding Box.** String, Konstante |
| `CloseUpImageFilename` | `https://de7tbluxc5a3b…_annotated_closeshot_thumbnail.jpg` | Der annotierte Ausschnitt |

Der zweite Fund desselben Bildes zeigt den Kontrast: `contamination-exterior`,
Code `29`, `DamageCategoryID: 1`, Zone `DD72` (Heck), `DamageSubAreaCode: E0`.
Beide Findings sind also klar unterscheidbar — nur eben nicht im Bildraum.

### 4.1 Alle 6 Findings dieses Fahrzeugs

So sieht das komplette Ergebnis in Kurzform aus:

| Key | Slot des Fotos | Bauteil | Art | Code | Kat. | Zone | Sub | H × L |
|---|---|---|---|---:|---:|---|---|---|
| F1 | `custom_arear-right-bumper` | `fender-rear-right` | `dent` | 20 | 3 | CC62 | F0 | 188 × 87 |
| F2 | `custom_arear-right-bumper` | `trunk-cover` | `contamination-exterior` | 29 | 1 | DD72 | E0 | 129 × 116 |
| F3 | `custom_arear` | `moulding-bumper-rear` | `scratch` | 50 | 3 | DD71 | E0 | 140 × 145 |
| F4 | `custom_arear-right` | `wheel-rear-right` | `curbrash` | 17 | 1 | CC61 | 00 | 87 × 199 |
| F5 | `custom_afront-right` | `moulding-bumper-front` | `fluid-spillage-exterior` | 30 | 1 | BB21 | E0 | 76 × 23 |
| F6 | `custom_abcfront-right-fender` | `wheel-front-right` | `curbrash` | 17 | 1 | CC31 | 00 | 116 × 241 |

Gut zu sehen: Die beiden Felgenschäden (F4, F6) haben `DamageSubAreaCode: 00`
und die größten Längenwerte — das Muster aus der Gesamtauswertung, dass Räder
bei `Height`/`Length` deutlich über den Karosserieteilen liegen, wiederholt sich
hier im Kleinen.

Die Schlüssel `F1`–`F6` stehen **nicht** in der API-Antwort. Sie entstehen erst
bei uns (`eval/pipeline.py`), indem die Findings in Reportreihenfolge
durchnumeriert werden, nachdem Glas- und Interieur-Funde ausgefiltert wurden.

---

## 5. Wo die Bounding Box wäre — und was stattdessen kommt

Beide Findings dieses Fotos haben `MarkerPoint: "[1,1,1,1]"`. Es gibt in der
ganzen Antwort keine Zahl, die sagt, *wo* im Bild die Delle sitzt. Die Antwort
darauf steckt ausschließlich im Close-up-Bild, in das FocalX den Rahmen
einzeichnet:

![Close-up des ersten Fundes: Delle am rechten hinteren Kotflügel, Rahmen von FocalX eingezeichnet](beispiel/closeup-F1.jpg)

Das ist `F1` aus der Tabelle oben — 658 × 975 px, heruntergeladen aus
`CloseUpImageFilename` und lokal als `closeups/F1.jpg` abgelegt. Der Rahmen ist
Bildinhalt, keine Koordinate.

**Anatomie der Close-up-URL** am Beispiel:

```
https://de7tbluxc5a3b.cloudfront.net
  /sixttwo                                    ← Account
  /2026-08-1785842457994_sixttwo              ← Datum + Millisekunden-Stempel
  /v2
  /490a1810-…-ff6fc905c446                    ← InspectionId
  _custom_arear-right-bumper                  ← unser Slot-Name
  2e73c588-44d7-4a2f-ad91-00aaa8b4485b        ← interne UUID (NICHT DamageNumber)
  _annotated_closeshot_thumbnail.jpg
```

Keine Query, keine Signatur — anders als bei den Walkaround-Fotos. Trotzdem
nicht dauerhaft erreichbar, deshalb lädt die Pipeline die Close-ups sofort nach
dem Report herunter.

---

## 6. Was wir daraus machen

Aus den 32 KB Rohantwort entsteht `data/runs/fl500/results/TX-YDM4339__912dcb4c.json`.
Der Findings-Teil daraus, wörtlich:

```json
"findings": [
  {
    "key": "F1",
    "position": "custom_arear-right-bumper",
    "orientation": "rear-right\r",
    "part": "fender-rear-right",
    "type": "dent",
    "closeup": "data/runs/fl500/results/TX-YDM4339__912dcb4c/closeups/F1.jpg"
  },
  {
    "key": "F2",
    "position": "custom_arear-right-bumper",
    "orientation": "rear-right\r",
    "part": "trunk-cover",
    "type": "contamination-exterior",
    "closeup": "data/runs/fl500/results/TX-YDM4339__912dcb4c/closeups/F2.jpg"
  }
]
```

Von 16 API-Feldern je Finding behalten wir vier plus den Bildpfad. Weggelassen
werden die Konstanten und Dubletten (`RepairMethodCode`, `PartLocation`,
`DamageCategoryName`, `MarkerPoint`, `Manual`) sowie `ZoneId`, `Height`,
`Length` und `DamageCategoryID` — die drei letzten sind die einzigen
weggelassenen Felder mit echtem Informationsgehalt. Da der Rohreport vollständig
liegen bleibt, lassen sie sich jederzeit nachziehen, ohne FocalX erneut zu
befragen.

Dieses Fahrzeug hatte übrigens 2 dokumentierte SHARK-Schäden gegenüber 6
FocalX-Funden — ein typisches Verhältnis und der Grund, warum das Mapping
zwischen beiden Welten nötig ist.

---

## 7. Wie die Antwort aussieht, solange sie nicht fertig ist

Dafür liegt kein Beispiel im Repo — wir speichern nur fertige Reports. Belegt
über den Poll-Code (`eval/focalx.py`, `_poll_report`) und die Laufprotokolle ist
dieses Verhalten:

- Direkt nach `submitImages` antwortet der Endpoint eine Weile mit **HTTP 404**.
- Danach kommt **HTTP 200 mit `"Completed": false`**. `OrientationResults`
  enthält dann schon Einträge, aber weniger als die hochgeladenen 19, und die
  `Damages`-Listen füllen sich schrittweise. Genau deshalb darf man Findings
  erst bei `Completed: true` verwenden.
- Der Client protokolliert währenddessen Zeilen wie
  `poll: completed=False orientations=7 damages=2`.
- Ein leerer Report mit `Completed: true` und `OrientationResults: []` ist das
  Symptom für ungültige Positionslabels — nicht für ein schadenfreies Auto.

---

*Alle Werte in diesem Dokument sind aus
`docs/beispiel/damagereport-beispiel.json` entnommen. Stand: 01.09.2026.*
