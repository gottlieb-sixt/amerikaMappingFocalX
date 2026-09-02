# Vor dem Start: Was anzufragen und was sicherzustellen ist

Ziel des Vorhabens: Alles, was FocalX zu einem Fahrzeug erzeugt — Vollbilder des
Walkarounds, Schadensausschnitte, Report-JSON — dauerhaft in einem
Sixt-eigenen AWS-Speicher archivieren. Dieses Dokument ist die Vorstufe zum
Architekturplan: Es listet, **was bei wem angefragt werden muss** und **was vor
dem ersten Handschlag geprüft sein muss**. Wie daraus ein Dienst wird, der von
allein läuft, steht in [`aws-archiv-betrieb.md`](aws-archiv-betrieb.md).
Grundlage sind Messungen vom
01./02.09.2026 gegen den Produktivtenant `sixttwo`, nicht die Anbieterdoku — die
weicht an mehreren Stellen von der Realität ab (siehe `docs/focalx-api.md`,
Abschnitt 3.0).

> **Stand 02.09.2026 — die Mengenrechnung ist um Faktor 14 gefallen.** Drei
> frisch angelegte Inspektionen und die Auswertung aller 744 vollständigen
> lokalen Check-ins ergaben **11,9 MB je Check-in statt 164 MB**, also **2,15 TB
> statt 29,5 TB** über 90 Tage bei 2.000 Check-ins am Tag. Die 164 MB stammten
> aus einer einzelnen Mobile-App-Inspektion mit 7,5-Megapixel-Fotos; die Flotte
> besteht zu 86 % aus 1-Megapixel-Aufnahmen. Damit kostet der Speicher 10–50 $
> im Monat, nicht mehrere Hundert. Belegt mit `scripts/archive_probe.py`.
>
> **Und der Zeitdruck ist weg.** Ein erneuter Report-GET signiert die
> Vollbild-URLs neu: Ein kompletter Tag vom 04.08. ließ sich am 02.09.
> vollständig archivieren, 7.814 von 7.814 Vollbildern. Die 168-Stunden-Frist
> gilt der einzelnen URL, nicht dem Bild. Ein Ausfall des Dienstes über Tage
> ist damit nachholbar — nur Closeups bleiben unwiederbringlich.

---

## 1. Was bereits gesichert ist

Diese Punkte sind live nachgemessen und brauchen keine Rückfrage mehr.

| Frage | Ergebnis | Wie geprüft |
|---|---|---|
| Erfahren wir von neuen Inspektionen, ohne sie selbst anzulegen? | **Ja.** `GET …/inspections/inspectionswithdate/?start_date=&end_date=` liefert alle Inspektionen eines Zeitraums, auch fremde | 707 Einträge für 01.07.–02.09.2026; 690 von unserem Client, 17 aus der Mobile-App |
| Kommen wir an Reports fremder Inspektionen? | **Ja**, mit unserem normalen Bearer-Token | 5 App-Inspektionen live abgerufen |
| Steht ein Kennzeichen darin? | **Ja**, `RegistrationNumber` + `vehicleData.RegisterNumber` — auch bei App-Aufnahmen | 666/666 lokale Reports gefüllt und korrekt; live `M FN809`, `TSHNAM` |
| Sind die Vollbilder abrufbar? | **Ja**, HTTP 200 für alle 23 Orientierungen einer App-Inspektion | HEAD/GET gegen CloudFront |
| Wie groß ist ein Check-in? | **11,9 MB** im Flottenschnitt (8,8 MB Vollbilder + 3,1 MB Closeups + Report), 38 Objekte | 744 vollständige Check-ins ausgewertet, Aufbläh-Faktor an 57 Bildpaaren gemessen |
| Gibt es annotierte Vollbilder? | **Nein.** `ImageFileDrawURL` ist byte-identisch mit `ImageFileURL` | alle 23 Orientierungen verglichen |
| Wie weit reicht die Historie? | Nur bis **10.06.2026** — der Tenant ist jünger. Kein Altbestand zu retten | Abfrage 2024-01-01…2026-06-30 ⇒ 58 Einträge, alle Juni 2026 |
| Wie lange dauert ein Report-Abruf? | **~9 s** | 5 Abrufe in 45 s |
| Wie lange dauert die Analyse? | **17–35 s** je Fahrzeug — nicht mehr die 5,5 min aus den Läufen bis August | 3 frische Inspektionen am 02.09. |
| Verfallen auch die Closeup-URLs? | **Fast nie**, aber nicht garantiert: 10 von 6.673 (0,15 %) waren nach vier Wochen weg | ganzer Tag vom 04.08. am 02.09. archiviert |
| Ist ein Ausfall über 7 Tage ein Datenverlust? | **Nein.** Ein erneuter Report-GET signiert die Vollbild-URLs neu; 7.814 von 7.814 Vollbildern kamen vier Wochen nach der Analyse mit HTTP 200. Nur Closeups sind nicht erneuerbar | ganzer Tag vom 04.08. |
| Wie schnell läuft ein Tageslauf? | **9,9 min für 425 Inspektionen** bei 3 parallelen Autos — 1,4 s je Auto, 53,9 Mbit/s, 0 Fehler | `archive_probe.py day 2026-08-04` |
| Bekommen wir unsere Bilder unverändert zurück? | **Nein.** Gleiche Pixelmaße, aber **Faktor 2,73** mehr Bytes — FocalX kodiert neu | 57 Bildpaare Upload ↔ Archiv, Spanne 1,71–3,85 |
| Ist das Kennzeichenformat stabil? | **Nein.** Dasselbe Fahrzeug kam am 05.08. als `TXWCV5796`, am 02.09. als `TX-WCV5796` zurück | zwei Reports desselben Autos verglichen |

Werkzeug für Wiederholungsmessungen: `scripts/archive_probe.py`
(`local`, `day`, `survey`) und `scripts/archive_live_test.py` für frische
Inspektionen.

**Die wichtigste Konsequenz aus Zeile 6:** Der Wunsch „Bilder mit markierten
Schäden" ist mit FocalX-Bordmitteln nur über die **Closeup-Ausschnitte**
erfüllbar. Markierte Vollbilder existieren nicht und müssten wir selbst rendern
— aus `MarkerPoint`, das aber nur ein Punkt ist, kein Rechteck.

---

## 2. Mengen und Kosten als Grundlage für die Anträge

### 2.1 Woher die 11,9 MB kommen

Die Flotte kennt genau zwei Auflösungen, gemessen über 744 vollständige
Check-ins:

| Auflösung | Anteil | Upload je Check-in (19 Fotos) |
|---|---|---|
| 678 × 1440 (1,0 MP) | 86 % | ⌀ 1,8 MB |
| 1884 × 3998 (7,5 MP) | 12 % | ⌀ 13,8 MB |

Entscheidend ist, dass wir **nicht unsere eigenen Bytes archivieren**: FocalX
gibt die Fotos bei identischen Pixelmaßen um den Faktor 2,73 größer zurück
(gemessen an 57 Bildpaaren, Spanne 1,71–3,85). Der Speicherbedarf lässt sich
daher nicht aus der Upload-Größe ableiten, sondern nur aus dem Rückgabewert:

| Bestandteil | je Check-in |
|---|---|
| 19 Vollbilder (3,2 MB Upload × 2,73) | 8,8 MB |
| ⌀ 16,9 Closeups à 177 KB | 3,1 MB |
| Report + Manifest | 0,04 MB |
| **Summe** | **11,9 MB, 38 Objekte** |

### 2.2 Hochgerechnet

| | 1.000 Check-ins/Tag | 2.000 Check-ins/Tag |
|---|---|---|
| Datenmenge | 11,9 GB/Tag | 23,9 GB/Tag |
| Objekte | 38.000/Tag | 76.000/Tag |
| Nach 90 Tagen | ~1,07 TB, ~3,4 Mio. Objekte | ~2,15 TB, ~6,8 Mio. Objekte |
| Dauerlast Download | ~1,4 Mbit/s über 24 h | ~2,8 Mbit/s über 24 h |
| … bei 4-h-Fenster | ~8 Mbit/s | ~16 Mbit/s |

Speicherkosten für 2,15 TB, Größenordnung eu-central-1 — **vor dem Antrag mit
aktueller Preisliste gegenprüfen**:

| Klasse | ca. je Monat | Haken |
|---|---|---|
| S3 Standard | ~50 $ | bei dieser Menge vertretbar |
| Standard-IA | ~27 $ | 30 Tage Mindestlaufzeit |
| Glacier Instant Retrieval | ~9 $ | 90 Tage Mindestlaufzeit, sofort lesbar |
| Glacier Deep Archive | ~4 $ | 180 Tage Mindestlaufzeit, Abruf dauert Stunden |

**Die Speicherklasse ist damit keine wichtige Entscheidung mehr.** Zwischen der
teuersten und der billigsten Variante liegen 46 $ im Monat; dafür lohnt weder
eine Lifecycle-Mechanik noch das Bündeln der Closeups. Standard oder
Standard-IA genügt, und beide bleiben ohne Mindestlaufzeit-Fallen abrufbar.

Die alte Rechnung mit 30 TB legte das Gegenteil nahe — sie beruhte auf einer
einzelnen Mobile-App-Inspektion, die 14-mal so groß war wie ein durchschnittlicher
Check-in unserer Flotte.

Zwei Kostenpunkte bleiben trotzdem erwähnenswert:

- **Kleine Objekte in Glacier-Klassen.** Jedes Objekt kostet ~40 KB
  Verwaltungsaufschlag. Bei 6,8 Mio. Objekten sind das ~272 GB, also 13 % der
  Nutzdaten — der Aufschlag wiegt bei kleinen Dateien relativ schwerer. Ein
  weiteres Argument, die Glacier-Klassen hier zu lassen.
- **NAT-Gateway.** Wenn die Landing Zone verlangt, dass Lambda in einer VPC
  läuft, laufen alle Downloads durch ein NAT-Gateway: ~0,045 $/GB, bei 2,15 TB
  also **~97 $**. Nicht mehr der größte Posten des Vorhabens, aber immer noch
  das Doppelte der Speicherkosten. Die Frage in Abschnitt 4 bleibt berechtigt.

Eingehender Verkehr nach S3 ist kostenlos; die 1–2 TB Ausgang zahlt FocalX.

---

## 3. Anfrage an FocalX

Elf Punkte, nach Wichtigkeit. Die Spalte rechts sagt, was eine schlechte
Antwort für uns bedeutet — das macht die Nachfrage verhandelbar.

Die Reihenfolge hat sich am 02.09. verschoben: Das Rate-Limit ist weniger
dringend, seit die Analyse 17 s statt 5,5 min braucht und nur noch 24 GB
statt 328 GB am Tag zu ziehen sind. Dafür ist mit dem Aufbläh-Faktor eine
neue Frage dazugekommen (Nr. 4), die den Speicherbedarf halbieren würde.

| # | Frage | Wenn die Antwort schlecht ist |
|---|---|---|
| 1 | **Obergrenze der Inspektionsliste:** Der Endpoint hat keine Paginierung. Gibt es ein verstecktes Maximum an Einträgen je Antwort? Wir haben nur 707 gesehen | Ein stilles Abschneiden bei z. B. 1.000 würde bei 2.000 Check-ins am Tag **unbemerkt Fahrzeuge verlieren**. Der gefährlichste Punkt der Liste |
| 2 | **Aufbewahrung bei FocalX:** Wie lange liegen Bilder und Reports auf ihrer Seite? | **Seit dem 02.09. die entscheidende Frage.** Weil ein erneuter Report-GET die Vollbild-URLs neu signiert, ist nicht mehr unsere Frist von 7 Tagen der Engpass, sondern ihre Aufbewahrung. Sie allein bestimmt, wie lange ein Ausfall nachholbar bleibt |
| 2a | **Mandantenwechsel:** Ende Juli wurde von `sixt` auf `sixttwo` umgestellt. Inspektionen aus dem alten Mandanten liefern jetzt HTTP 500. War das angekündigt, kommt so etwas wieder, und gibt es einen Weg an Altbestände? | Ein Mandantenwechsel ist ein **harter Schnitt** — er macht die Nachladbarkeit der Vollbilder mit einem Schlag wertlos. Wenn das ohne Vorwarnung passieren kann, muss der Archiv-Dienst taggleich laufen und darf sich nicht auf Nachholbarkeit verlassen |
| 3 | **Closeup-URLs:** Sie werden beim erneuten Report-GET *nicht* neu signiert, und 10 von 6.673 waren nach vier Wochen weg. Ist das Absicht, und gibt es einen Weg, sie zu erneuern? | Closeups sind die einzigen Bilder mit eingezeichnetem Schaden. Was hier verloren geht, ist endgültig weg — auch mit Nachlauf |
| 4 | **Originalbytes:** Die zurückgelieferten Vollbilder sind bei identischen Pixelmaßen im Median 2,73-mal so groß wie die hochgeladenen. Wird neu kodiert? Können wir die Originaldatei bekommen? | Ohne Originale speichern wir dauerhaft 2,73-mal so viele Bytes wie nötig — 2,15 TB statt 1,14 TB je 90 Tage, ohne jeden Informationsgewinn |
| 5 | **Änderungen am Modell:** Dieselben 19 Fotos ergaben am 05.08. 32 Funde und am 02.09. 38 (nur 26 deckungsgleich), bei 20-fach kürzerer Laufzeit. Gibt es Versionshinweise oder eine Ankündigung bei Modellwechseln? | Ohne Vorwarnung ändern sich Ergebnisse unbemerkt. Für ein Archiv beherrschbar (der Erstreport bleibt), für jede Auswertung darauf nicht |
| 6 | **Dienstkonto:** Bitte eigene Zugangsdaten für den Archivdienst, möglichst nur lesend | Sonst läuft Produktion auf einem persönlichen Konto — bei Passwortwechsel oder Austritt steht das Archiv |
| 7 | **Produktivtenant:** Bleibt es bei `sixttwo`, oder gibt es für den Rollout einen anderen Slug/Tenant? | Falscher Tenant ⇒ wir archivieren die falsche Flotte |
| 8 | **Rate-Limit:** Wie viele parallele Report-Abrufe und Bild-Downloads sind erlaubt? Wir planen dauerhaft 2.000 Inspektionen und ~76.000 Bilder am Tag | Entschärft, seit die Analyse 17 s dauert. Erst unter ~5 parallelen Abrufen wird der Tageslauf wieder eng |
| 9 | **Filter:** Gibt es `status` oder „geändert seit"? | Ohne Filter holen wir täglich die volle Tagesliste und filtern selbst — machbar, aber Verschwendung |
| 10 | **`ImageFileDrawURL`:** Der Name verspricht ein Bild mit eingezeichneten Schäden, geliefert wird das unveränderte Original. Fehler oder Absicht? Gibt es annotierte Vollbilder? | Wenn nicht, müssen wir sie selbst rendern oder auf Closeups verzichten |
| 11 | **Webhook:** Wie wird die Ziel-URL hinterlegt, wie authentifiziert FocalX sich, wie oft wird wiederholt? | Nur noch Komfort, seit die Tagesliste funktioniert — aber sparsamer und aktueller |

Das Ausgangsvolumen ist mit ~24 GB/Tag keine Nachfrage mehr wert; mit 328 GB
wäre es eine gewesen.

<details>
<summary>Vorlage für die Mail an FocalX (englisch)</summary>

```
Subject: API questions before building a long-term archive (tenant: sixttwo)

Hi <name>,

we are building an archive on our own AWS account that stores, for every
inspection, the full-frame images, the close-up crops and the damage report
JSON. Expected volume in production is 1,000–2,000 inspections per day,
i.e. roughly 76,000 images and ~24 GB per day, pulled from your CDN.

Before we start, eleven questions:

1. inspectionswithdate has no pagination. Is there a maximum number of
   entries per response? We have only seen 707 so far, and we need to be
   certain nothing is silently truncated at 2,000/day.
2. Around end of July our tenant changed from `sixt` to `sixttwo`. Inspections
   created under the old tenant now return HTTP 500 on the damage report
   endpoint and no longer appear in the daily list, while inspections from
   26.07. and 28.07. under the new tenant work fine — so this is not a
   retention effect. Was the switch planned, can it happen again, and is
   there any way to reach data from the old tenant? A 403 or 404 instead of
   a 500 would also help us tell "not allowed" from "server problem".
3. How long do you retain images and reports on your side? We found that
   re-fetching the damage report re-signs the full-frame URLs — we pulled
   all 7,814 full-frame images of a day four weeks after the analysis, with
   no failures. So your retention, not the 168-hour URL lifetime, is what
   limits how far back we can recover. Is there a documented retention
   period, and is re-fetching for that purpose acceptable to you?
4. Close-up URLs behave differently: they are unsigned and are not re-issued
   by a new report fetch. 10 out of 6,673 were already gone after four weeks
   (HTTP 403). Is there a retention policy for them, and any way to
   regenerate a lost crop?
5. Image re-encoding: the full-frame images we download are pixel-identical
   in dimensions to what we uploaded, but 2.73x larger in bytes (median over
   57 pairs, range 1.71–3.85). Are the images re-encoded on your side, and
   is there a way to retrieve the original uploaded file? This would halve
   our long-term storage.
6. Model changes: the same 19 photos produced 32 findings on 2026-08-05 and
   38 on 2026-09-02, with only 26 in common, while analysis time dropped
   from 5.5 minutes to 17 seconds. Do you publish model versions or announce
   changes? Is the model version available per report?
7. Can we get a dedicated, read-only service account for the archive
   instead of using a personal account?
8. Will production use the tenant slug "sixttwo" or a different one?
9. Rate limits: how many concurrent damagereport calls and image downloads
   are we allowed to make? Is there a documented daily quota?
10. Is there a status filter or a "modified since" parameter?
11. ImageFileDrawURL returns bytes identical to ImageFileURL for all
    orientations we tested. Is that intended? Is there any way to get a
    full-frame image with the detected damages drawn on it?
12. Damage report webhook: how do we register the target URL, how do you
    authenticate to it, and what is the retry behaviour?

Thanks a lot,
<name>
```

</details>

---

## 4. Anfrage an das Cloud-/Landing-Zone-Team

Ausgangslage: Im Zugriffsportal sind drei Konten sichtbar, darunter
`mobile-damage-detection-dev` (180111006559). Ein **Produktionskonto fehlt**.

| # | Frage / Antrag | Warum es vorher geklärt sein muss |
|---|---|---|
| 1 | **Produktionskonto** für das Archiv beantragen (Gegenstück zu `mobile-damage-detection-dev`) | 1–2 TB echte Fahrzeugfotos gehören nicht in ein Entwicklungskonto: lockere Rechte, keine Sicherungspflicht, im Zweifel Neuaufsetzung. Solche Anträge dauern Wochen — deshalb Schritt 1 |
| 2 | **Muss Lambda in einer VPC laufen?** | Wenn ja: NAT-Gateway-Gebühren von ~97 $ auf 2,15 TB. Wenn nein (Lambda ohne VPC-Anbindung): 0 $. Seit die Mengen um Faktor 14 gefallen sind, ist das kein Projektrisiko mehr, aber immer noch das Doppelte der Speicherkosten |
| 3 | **Region** — ist `eu-central-1` gesetzt, und verbieten SCPs andere Regionen? | Datenschutz und Latenz; FocalX liegt selbst in `eu-central-1` |
| 4 | **Welche Rollen bekommen wir** (`AdministratorAccess`, `PowerUserAccess`, nur Lesen)? Dürfen wir S3-Buckets, Lambda-Funktionen und IAM-Rollen selbst anlegen? | Entscheidet, ob wir in Tagen oder in Ticketwochen bauen |
| 5 | **Vorgeschriebene Bucket-Einstellungen:** Verschlüsselung (SSE-S3 oder KMS mit eigenem Schlüssel), Versionierung, Zugriffsprotokollierung, „Block Public Access" | Versionierung verdoppelt im Zweifel die Kosten — bei 2 TB verkraftbar, aber vorher wissen, nicht nachher |
| 6 | **Erzwingt die Landing Zone Replikation oder Backup** in eine zweite Region? | Verdoppelt Speicherkosten, also ~30–50 $ im Monat. Für ein Archiv, dessen Quelle nach 7 Tagen verfällt, gut angelegt |
| 7 | **Object Lock / Unveränderbarkeit:** vorgeschrieben, erlaubt, verboten? | Kollidiert direkt mit einem Löschkonzept aus Abschnitt 5 — die beiden Antworten müssen zueinander passen |
| 8 | **Dienstkontingente:** Lambda-Parallelität, S3-Anfragen pro Sekunde und Präfix | 76.000 PUT/Tag sind unkritisch, aber die Parallelität muss zum Rate-Limit von FocalX passen |
| 9 | **Kostenstelle und Budgetfreigabe** für Größenordnung 30–100 $/Monat Speicher plus Einmalkosten | Ohne Kostenstelle kein Konto. Die Größenordnung ist seit der Messung vom 02.09. deutlich kleiner als ursprünglich veranschlagt (100–800 $) |
| 10 | **Wo dürfen Zugangsdaten liegen** (Secrets Manager, Parameter Store) und wer rotiert sie? | FocalX-Passwort ist der Schlüssel zu allen Fahrzeugfotos |
| 11 | **Löschrecht für den Archivdienst.** Unsere Rolle im Dev-Konto (`AWSReservedSSO_PowerUser-Restricted`) hat ein **explizites Verbot** auf `s3:DeleteObject` — am 02.09.2026 im Testbucket belegt. Der Dienst braucht eine eigene Rolle, die auf **ihrem** Präfix löschen darf, oder das Löschen läuft über Lifecycle-Regeln | Ohne Löschrecht ist der DSGVO-Löschweg aus Abschnitt 5 (Punkt 3) nicht ausführbar. Er funktioniert in unserem Test, aber gegen echtes S3 mit diesen Rechten nicht. `PutObject` ist erlaubt, Überschreiben also möglich — nur Entfernen nicht |

---

## 5. Anfrage an Datenschutz und Recht

Fahrzeugfotos mit lesbaren Kennzeichen sind personenbezogene Daten. Der Punkt
war schon als „mit Datenschutz klären" markiert — hier die konkreten Fragen.

| # | Frage | Auswirkung auf den Bau |
|---|---|---|
| 1 | **Zweck** des Archivs: Nachweis, Qualitätsmessung, KI-Training? Und ist er im Verarbeitungsverzeichnis gedeckt? | Zweckbindung entscheidet, ob wir überhaupt dauerhaft speichern dürfen |
| 2 | **Aufbewahrungsfrist**: wirklich unbegrenzt, oder gibt es eine Obergrenze? | Bestimmt Speicherklasse und Lebenszyklusregeln |
| 3 | **Löschbarkeit je Fahrzeug oder Fall** nötig? | Bestimmt die Ordnerstruktur im Bucket. Nachträglich umzubauen heißt: 14 Mio. Objekte umkopieren |
| 4 | **Personen auf den Bildern** (Mitarbeiter, Passanten): Unkenntlichmachung gefordert? | Wäre ein zusätzlicher Verarbeitungsschritt mit erheblichem Rechenaufwand |
| 5 | **Auftragsverarbeitungsvertrag mit FocalX** vorhanden und deckt er die Weitergabe an uns? | Die Daten liegen ohnehin schon dort; unser Abruf ist nur eine Kopie |
| 6 | **Wer darf lesen?** Namentliche Gruppen für den Lesezugriff | IAM-Rollen und Freigabewege im Plan |
| 7 | Kollidiert eine geforderte **Unveränderbarkeit** (Abschnitt 4, Punkt 7) mit dem Löschkonzept? | Muss vor dem Bucket-Anlegen entschieden sein — Object Lock ist nicht rückholbar |

---

## 6. Anfrage an Fachbereich und App-Team

| # | Punkt | Nutzen |
|---|---|---|
| 1 | **Kennzeichen sauber setzen.** In den App-Inspektionen stehen `TEST`, `TEST4`, `TEST006`. Das Feld ist Freitext ohne Prüfung | Ohne echte Kennzeichen ist das Archiv fachlich wertlos, technisch aber vollständig — der schlimmste Zustand, weil er lange unbemerkt bleibt |
| 2 | **`MetaData` füllen** mit Check-in- oder Fall-ID beim Anlegen der Inspektion | Verknüpfung zum Sixt-Vorgang statt nur zum Kennzeichen. Kostet dort wenige Zeilen, ist später nicht nachrüstbar |
| 3 | **Mengenverlauf:** Wann kommen wirklich 2.000/Tag, und wie schnell wächst es? | Bestimmt, wann Rate-Limit und Zeitfenster kritisch werden |
| 4 | **Wer nutzt das Archiv später und wie oft?** | Entscheidet zwischen Deep Archive (billig, Stunden Wartezeit) und Instant Retrieval (dreifacher Preis, sofort) |
| 5 | **Wer betreibt den Dienst** und wer wird nachts gerufen, wenn er drei Tage stillsteht? | Bei 7 Tagen URL-Gültigkeit ist ein unbemerkter Ausfall gleich Datenverlust |

---

## 7. Was wir selbst sicherstellen müssen, bevor gebaut wird

Erledigt (Abschnitt 1): Auffinden, Zuordnung, Abrufbarkeit, Mengen.

### 7.1 Am 02.09.2026 abgehakt

Der Ingest ist gebaut und lokal durchgespielt (`archive/`, aufgerufen über
`scripts/archive_probe.py`). Die Ablage-Schicht ist abstrahiert: Derselbe Code
schreibt in ein Verzeichnis oder nach S3, der Wechsel ist `--bucket <name>`.

1. **Ablauf der Bild-URLs belegt.** Drei Reports aus Juli und August erneut
   abgerufen: Vollbilder durchweg **403**, Closeups **200** — auch der sechs
   Wochen alte. Die 168 Stunden gelten nur für die Vollbilder; die Notiz „Close-up-URLs
   laufen später ab" ist für diesen Zeitraum widerlegt.
2. **Report-Wiederabruf nachgewiesen — schärfer als erwartet.** Dieselben
   19 Fotos ergaben am 05.08. 32 Funde und am 02.09. 38, davon nur 26
   deckungsgleich. Es ändert sich also nicht bloß die Reihenfolge, sondern das
   Ergebnis. Inhaltlich gilt deshalb der Erstabruf; ein abweichender
   Zweitabruf landet als `report.<zeitstempel>.json` daneben, nie darüber.
3. **Wiederaufnahme und Doppelabruf-Sicherheit gezeigt.** Zweiter Durchlauf
   über dieselben Inspektionen: `Report unverändert`, gleiche Bytezahl, 0,0 s,
   kein Schreibvorgang. Zustand steht in der Ablage, nicht in einer Buchhaltung
   — wie bei `scripts/batch.py`.
4. **Vollständigkeitsprüfung festgelegt und in Betrieb.** Vor dem ersten Byte
   entsteht die Sollliste aus dem Report; das Manifest führt jedes Objekt mit
   Prüfsumme, HTTP-Status und Grund des Fehlens. `archive_probe.py survey`
   verdichtet das über den Bestand.
5. **Löschbarkeit je Fahrzeug gebaut.** Ein Index-Objekt je Kennzeichen und
   Inspektion (`index/plate/<KEY>/<id>.json`) — bewusst ein Objekt je Paar,
   damit Anlegen ohne Lesen-Ändern-Schreiben auskommt und parallele Läufe sich
   nicht ins Gehege kommen.
6. **Kennzeichen-Normalisierung eingebaut.** Weil FocalX das Format geändert
   hat, führt das Manifest beides: `registration_number` wie geliefert und
   `plate_key` als eigener, schreibweisenfester Suchschlüssel.

### 7.2 Der Tageslauf vom 04.08., gemessen am 02.09.

Der vollständige Durchlauf über einen echten Produktionstag — die letzte
Prüfung, die ohne AWS möglich war:

| | |
|---|---|
| Inspektionen | 425 (Tagesliste live), davon 421 vollständig |
| Objekte | 14.477 — 7.814 Vollbilder, 6.673 Closeups |
| Umfang | **4,02 GB, ⌀ 9,5 MB je Inspektion** |
| Laufzeit | **9,9 min** bei 3 parallelen Autos = 1,4 s je Auto, 53,9 Mbit/s |
| Fehlgeschlagene Inspektionen | 0 |
| Fehlende Objekte | 10 Closeups (0,15 %), sonst nichts |

Die ⌀ 9,5 MB liegen unter der Schätzung von 11,9 MB aus Abschnitt 2 — dieser
Tag enthielt weniger 7,5-MP-Fahrzeuge als der Gesamtbestand. Für die Planung
bleibt die höhere Zahl die vorsichtigere.

Vier Dinge, die erst dieser Lauf zutage gefördert hat:

1. **Fremde Arbeitsabläufe im selben Tenant.** Eine Inspektion aus einer
   Bahnverladung trug Frachtpapiere im Dateinamen — 257 Zeichen, mehr als die
   255 Bytes, die Dateisysteme je Pfadbestandteil erlauben. S3 hätte das
   geschluckt (1.024 Bytes), jeder lokale Lauf bricht daran ab. Der Ingest
   kürzt jetzt mit angehängter Inhaltskennung.
2. **Ein Objekt darf nie den Tag mitreißen.** Genau dieser Dateiname brach den
   ersten Versuch nach 106 Sekunden ab. Fehler beim Ablegen landen jetzt im
   Manifest statt in einem Stacktrace.
3. **Ein leerer Report** (0 Ansichten) kam einmal vor und wird korrekt als
   unvollständig ausgewiesen statt als „nichts zu tun" durchgewunken.
4. **Ein `TEST`-Kennzeichen** in einem echten Produktionstag — die
   Plausibilitätsprüfung aus Abschnitt 6 ist keine Vorsichtsmaßnahme auf Vorrat.

### 7.3 Die S3-Anbindung, geprüft ohne AWS-Konto

Der Tageslauf schrieb auf die Festplatte — `S3Store` war nie ausgeführt.
`scripts/archive_s3_test.py` schließt das mit `moto` (S3 im Arbeitsspeicher,
gleiche boto3-Schnittstelle), gespeist aus echten FocalX-Reports und echten
Bildbytes: **94 Prüfungen, ohne Netz, in 17 Sekunden.**

Die tragende Prüfung ist der Gleichstand: derselbe Ingest einmal auf die Platte
und einmal nach S3, dann Schlüssel für Schlüssel verglichen — 171 Objekte,
byteweise identisch. Damit gilt alles, was der Tageslauf lokal bewiesen hat,
auch für S3. Dazu Paginierung über 1.000 Objekte hinaus (ein Tag hat 14.477;
wer den Paginator vergisst, verliert 93 % lautlos und meldet Erfolg), das
Präfix im geteilten Bucket, der Bestandsbericht und der Löschweg über den
Kennzeichen-Index.

Zwei Fehler hat erst dieser Test gefunden, beide behoben:

1. **Der Zeitpunkt der Ersterfassung ging verloren.** Das Manifest wird bei
   jedem Lauf neu geschrieben und überschrieb dabei `abgerufen_am`. Bei einem
   Archiv ist aber gerade das Datum der ersten Sicherung die belegkräftige
   Angabe — es steht jetzt als `erstmals_archiviert` daneben und überlebt jeden
   Nachlauf.
2. **Ein Planungslauf zerstörte gute Manifeste.** `--no-download` schrieb
   „0 von 57 vorhanden" über ein vollständiges Manifest; der Bestandsbericht
   meldete daraufhin Datenverlust, wo keiner war. Läufe ohne Downloads zählen
   jetzt, was bereits im Archiv liegt.

Am 02.09.2026 lief derselbe Ablauf zusätzlich **gegen echtes S3** im Dev-Konto
(`sixt-focalx-archiv-test-180111006559`, Präfix `focalx-neu`): fünf
Inspektionen vom 04.08., 166 Bilder, 30,6 MB, keine Lücke, 0 Fehler in
22 Sekunden. Drei Dinge, die nur ein echter Bucket zeigt:

- **Alle 261 Adressen in den fünf Reports treffen ein Objekt**, das wirklich im
  Bucket liegt — nachgeprüft durch Auflösen jeder Adresse gegen die
  Objektliste. Kein `cloudfront`, keine `Signature=` mehr in den Reports.
- **Der zweite Lauf meldete für alle fünf `Report unverändert`** und ließ die
  Objektzahl bei 181. Das ist der Beleg für den Umbau auf eine Reportfassung:
  Unser gespeicherter Report mit `s3://`-Adressen wird beim Wiederabruf als
  inhaltsgleich mit dem frischen FocalX-Bericht erkannt. Mit der alten,
  byteweisen Prüfung hätte jeder Zweitabruf fälschlich eine neue Fassung
  angelegt.
- **Löschen ist verboten** (s. Abschnitt 4, Punkt 11). `PutObject` geht,
  `DeleteObject` scheitert an einem expliziten Verbot in der Rollenrichtlinie.

Der echte Lauf hat noch einen teuren Fehler sichtbar gemacht, den kein
Mock-Test zeigen konnte: Der Wiederholungslauf **las jedes vorhandene Bild aus
S3 zurück**, nur um eine Prüfsumme neu auszurechnen, die längst im Manifest
stand — 30,6 MB für fünf Autos, hochgerechnet 19 GB Lesezugriff pro Tag für
nichts. Vorhandene Objekte werden jetzt anhand des Manifests übersprungen; der
zweite Lauf holt 0,0 MB. Die Kontrolle ist damit nicht weg, sondern eine
bewusste Entscheidung: `--pruefen` liest zurück, hält jedes Objekt gegen seine
Prüfsumme und meldet eine stille Veränderung als `pruefsumme_abweichend`.

### 7.4 Was je Inspektion abgelegt wird

```
v1/<jjjj>/<mm>/<tt>/<inspection-id>/
  report.json          der FocalX-Bericht, Adressen zeigen ins Archiv
  manifest.json        unsere Buchführung: erwartet, vorhanden, Prüfsummen
  images/              die Walkaround-Fotos
  closeups/            die Ausschnitte je Schaden
index/plate/<KENNZEICHEN>/<inspection-id>.json
```

Es gibt **eine** Reportdatei, und sie ist ohne FocalX benutzbar: In den drei
Feldern mit Adressen (`ImageFileURL`, `ImageFileDrawURL`,
`CloseUpImageFilename`) steht der Ort im Archiv statt der CloudFront-Adresse.
Alles Inhaltliche — Funde, Bauteile, Schadensarten, Koordinaten, Zeiten —
kommt unverändert von FocalX. Fünf Festlegungen dazu:

- **Die FocalX-Adressen werden nicht aufbewahrt.** Sie sind nach sieben Tagen
  tot und nach dem Mandantenwechsel ohnehin wertlos; sie mitzuführen hieße,
  Müll für einen Vergleich aufzuheben, den niemand führt. Im Manifest steht je
  Objekt der Herkunftspfad ohne Signatur — das genügt zur Nachverfolgung.
- **Nicht Archiviertes wird `null`.** Ein toter Link sieht gültig aus und
  liefert doch nur 403; eine Lücke ist ehrlicher. Die betroffenen Schlüssel
  stehen gesammelt in `Archiv.fehlend` und ausführlich im Manifest.
- **Lücken sind heilbar.** Bei jedem Lauf werden die Adressen neu gesetzt: Was
  ein Nachlauf doch noch holt, wird nachgetragen und `Archiv.fehlend` kürzer.
  Das ist der Grund, warum die verlorenen FocalX-Adressen nicht fehlen — der
  frische Abruf bringt sie mit.
- **Inhaltlich gilt der erste Abruf.** Weicht ein späterer ab, landet er als
  `report.<zeitstempel>.json` daneben; die Adressen in `report.json` blieben
  sonst bei einem Bericht, zu dem die Bilder nicht gehören.
- **Ohne Zeitstempel im Inhalt**, damit bei gleichem Bestand dieselbe Datei
  entsteht und nicht jeder Lauf ein neues Objekt schreibt.

Was das kostet: Ein Vergleich „sagt FocalX heute noch dasselbe wie damals?"
läuft nicht mehr byteweise gegen ein Original, sondern über `fingerprint()`,
das Adressen und Sortierung ausklammert. Genau diesen Vergleich führt jeder
Lauf ohnehin selbst durch, und ein Abweichen legt die neue Fassung daneben.

### 7.5 Weiter offen

1. **Durchsatz aus AWS messen, nicht vom Laptop.** Die 53,9 Mbit/s stammen über
   eine Büroleitung. Ein Probelauf im Dev-Konto liefert die echten Zahlen.
2. **Probelauf im Dev-Konto** über einen echten Tag, bevor das Produktionskonto
   beantragt ist. Erst damit sind Laufzeit, Fehlerquote und Kosten belastbar.
3. **Verhalten bei mehreren Tagen am Stück** und beim Nachholen eines
   versäumten Zeitraums — jetzt interessanter als vorher, weil die Vollbilder
   nachholbar sind.
4. **Was `moto` nicht nachstellt:** Berechtigungen, Verschlüsselung,
   Aufbewahrungssperre (Object Lock), Übergang nach Glacier, Versionierung und
   das echte Verhalten unter Last. Der Test beweist, dass unser Code richtig
   mit S3 spricht — nicht, dass der Bucket richtig eingerichtet ist.

---

## 8. Was das Vorhaben kippen würde

Diese Punkte früh klären, weil sie die Architektur nicht verändern, sondern
ersetzen:

- **Die Inspektionsliste schneidet still ab.** Ohne Paginierung und ohne
  zugesagte Obergrenze verlieren wir bei hohem Volumen unbemerkt Fahrzeuge.
  Ersatzweg wäre der Webhook — also die letzte Frage der FocalX-Anfrage doch
  nicht optional. **Seit dem 02.09. der einzige verbliebene Kipppunkt.**
- **Der Datenschutz verlangt Unkenntlichmachung.** Das ist ein eigenes
  Vorhaben, kein Zusatz.

Drei Punkte, die bis zum 01.09. auf dieser Liste standen und dort nicht mehr
hingehören:

- ~~FocalX erlaubt nur wenige parallele Abrufe.~~ Bei 17 s Analysedauer und
  24 GB am Tag reichen wenige parallele Abrufe aus. Ein ganzer Tag lief mit
  drei parallelen Autos in 9,9 Minuten durch. Aus einem Kipppunkt ist eine
  Randbedingung geworden.
- ~~Die Landing Zone erzwingt VPC-Lambda plus Replikation.~~ Beides zusammen
  kostet auf 2,15 TB etwa 150 $ im Monat statt der befürchteten Tausende. Das
  rechtfertigt keine Neubewertung des Vorhabens.
- ~~URLs sind nicht erneuerbar, die Frist bleibt bei 7 Tagen.~~ **Sie sind
  erneuerbar** — ein gewöhnlicher Report-GET signiert die Vollbild-URLs neu.
  7.814 von 7.814 Vollbildern eines vier Wochen alten Tages kamen mit HTTP 200.
  Damit ist der Dienst kein Verfügbarkeitsprojekt mit Bereitschaftsdienst mehr:
  Ein Ausfall über Tage lässt sich nachholen. Die Einschränkung bleibt für
  **Closeups** — deren URLs werden nicht neu signiert, und 0,15 % waren nach
  vier Wochen verschwunden. Und alles hängt daran, wie lange FocalX die Bilder
  auf seiner Seite behält (Frage 3).

---

## 9. Reihenfolge

| Wann | Was | Wer |
|---|---|---|
| Sofort | Mail an FocalX (Abschnitt 3) — längste Antwortzeit, blockiert die meisten Entscheidungen | du |
| Sofort | Antrag Produktionskonto anstoßen (Abschnitt 4, Punkt 1) — Wochen Vorlauf | du |
| Sofort | Datenschutz-Termin vereinbaren (Abschnitt 5) | du |
| Diese Woche | Rollen und Region im Dev-Konto nachsehen; Frage nach VPC-Pflicht stellen | du |
| ~~Diese Woche~~ | ~~Vorprüfungen aus Abschnitt 7~~ — am 02.09. erledigt, s. 7.1 und 7.2 (kompletter Tageslauf über 425 Inspektionen) | ich ✔ |
| Danach | Architekturplan schreiben (Bucket-Aufbau, Lambda-Zuschnitt, Überwachung, Kosten) | ich |
| Danach | Probelauf im Dev-Konto über einen echten Tag | gemeinsam |
| Erst dann | Aufbau im Produktionskonto | gemeinsam |

Der Architekturplan kann parallel entstehen: Die offenen Antworten ändern darin
Details — Speicherklasse, Parallelität, Ordnerstruktur — nicht die
Grundstruktur.
