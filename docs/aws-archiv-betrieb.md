# Der Archiv-Endpoint: FocalX meldet, wir holen und ordnen ein

Voraussetzung ist [`aws-archiv-voraussetzungen.md`](aws-archiv-voraussetzungen.md)
— dort steht, was angefragt werden muss und was gemessen wurde. Dieses Dokument
beschreibt, **wie die Daten zu uns kommen und was daraus wird**.

> **Stand 03.09.2026 — zugesagt und geklärt.** FocalX liefert künftig von sich
> aus, statt dass wir nachts abholen. Geliefert wird **das Report-JSON, genau
> wie es ihr GET zurückgibt** — also die Beschreibung mit Adressen auf ihr
> CloudFront, **nicht die Bilddateien**. Der Push ist damit eine
> *Benachrichtigung*, kein Datentransport. Die Bilder laden wir weiterhin
> selbst, nur eben sofort statt in der Nacht darauf.

Stand 02.09.2026: Der Ordnungs-Kern funktioniert nachweislich gegen echtes S3 —
fünf Inspektionen, 166 Bilder, keine Lücke, 350 aufgelöste Adressen, 0 Fehler
(Abschnitt 7.3 im Voraussetzungs-Dokument).

---

## 1. Was sich ändert — erstaunlich wenig

Es ändert sich **der Auslöser, nicht die Verarbeitung**. Bisher fragten wir
nachts die Tagesliste und holten Report für Report; künftig kommt der Report zu
uns. Was danach passiert, ist identisch: Bilder laden, Prefix je Inspektion,
Manifest mit Prüfsummen, Report mit Adressen ins Archiv, Kennzeichen-Index.

**Unser Code kann das bereits.** `ingest_report()` nimmt genau dieses JSON
entgegen und lädt die Bilder selbst. Der Endpoint muss nichts weiter tun, als
den Schlüssel zu prüfen und das JSON weiterzureichen.

**Was dadurch besser wird — und das ist mehr als Bequemlichkeit.** Heute
verlieren wir 0,15 % der Ausschnitte (10 von 6.673 an einem Tag), weil ihre
Adressen beim erneuten Report-Abruf **nicht** neu signiert werden und wir erst
Stunden später abholen. Bei einer Benachrichtigung laden wir innerhalb von
Sekunden nach der Analyse. Dieser Verlust sollte damit gegen null gehen — und
er ist der einzige unwiederbringliche im ganzen System.

---

## 2. Der Ablauf

```
FocalX
  │  POST /inspections   (API-Key im Header, Report-JSON ~35 KB)
  ▼
API Gateway ──────> SQS ──────> Lambda "einordnen"
  (Schlüssel,        (nimmt an,    ├─ lädt die Bilder von CloudFront
   Ratenlimit)        puffert)     └─ schreibt nach S3
                                        v1/<jjjj>/<mm>/<tt>/<inspection-id>/
                                          report.json · manifest.json
                                          images/ · closeups/
                                        index/plate/<KENNZEICHEN>/…
```

Drei Teile, jeder mit einer Aufgabe. API Gateway prüft den Schlüssel und legt
die Meldung direkt in die Warteschlange — **ohne Lambda dazwischen**, das
beherrscht es als eigene Integration.

---

## 3. Warum eine Warteschlange dazwischen gehört

Zwei Gründe, beide handfest.

**Eine verpasste Meldung ist weg.** Beim Abholen konnten wir die Arbeitsliste
jederzeit aus der Tagesliste neu ableiten. Eine Benachrichtigung, die wir nicht
annehmen, kommt nicht wieder — es sei denn, FocalX wiederholt sie. Die
Warteschlange trennt *Annehmen* von *Verarbeiten*: Solange sie erreichbar ist,
ist die Lieferung sicher, auch wenn unsere Verarbeitung gerade klemmt.
Wiederholungen und eine Fehlerablage gibt es von SQS geschenkt.

**FocalX soll nicht auf uns warten.** Das Einordnen dauert rund **7 Sekunden je
Inspektion** (gemessen: 6,3 bis 8,2 s beim echten S3-Lauf), weil dabei ~34
Bilder geladen werden. Bei 2.000 Meldungen am Tag wäre es unhöflich und
fehleranfällig, die Verbindung so lange offenzuhalten. Mit Warteschlange
antwortet der Endpoint in Millisekunden.

---

## 4. Warum jetzt Lambda genügt

Gegen Lambda sprach im Abhol-Entwurf eine Zahl: 47 Minuten für einen ganzen Tag
gegen eine harte Grenze von 15 Minuten. Beim Push schrumpft die Arbeitseinheit
von „ein ganzer Tag" auf „eine Inspektion" — also von 47 Minuten auf 7
Sekunden. Der Fargate-Task entfällt.

**Kein VPC, kein NAT.** Eine Lambda außerhalb einer VPC hat direkten
Internetzugang und verursacht keine NAT-Gebühren. Nur wenn die Landing Zone
eine VPC erzwingt, kommen die ~97 $ auf 2,15 TB zurück — dann nimmt ein
**S3-Gateway-Endpoint** (kostenlos) wenigstens den Ablage-Verkehr heraus. Das
ist die einzige offene Kostenfrage.

---

## 5. Was der Endpoint prüfen muss

Nicht alles, was ankommt, gehört ins Archiv. Der Ingest bringt die Prüfungen
schon mit, sie müssen nur angewandt werden:

- **Nur fertige Reports.** Ein Report ohne `Completed` hat unvollständige
  `OrientationResults`; ihn zu archivieren hieße, eine Lücke als Ergebnis
  festzuschreiben. Der Abhol-Lauf filtert das bereits, der Endpoint muss es
  ebenso tun.
- **Plausible Kennzeichen.** `TEST`, `TEST4`, leer — alles in einem echten
  Produktionstag vorgekommen. Wird archiviert, aber markiert.
- **Doppelte Meldungen sind unkritisch.** Der Ingest ist idempotent; eine
  Wiederholung schreibt kein Objekt doppelt. Ein inhaltlich abweichender
  Zweitabruf landet als `report.<zeitstempel>.json` daneben.

Der Schlüssel wird von API Gateway über einen Usage Plan verwaltet; die Lambda
sieht ihn nie. Zwei gleichzeitig gültige Schlüssel erlauben eine Rotation ohne
Unterbrechung — ein Schlüssel, der nie wechselt, steht irgendwann in einem
Wiki.

---

## 6. Vollständigkeit und Überwachung

**Je Inspektion bleibt die Prüfung erhalten**, ohne Zutun: Der gelieferte Report
listet selbst alle zugehörigen Bilder, daraus entsteht der Soll-Bestand, das
Manifest vergleicht ihn mit dem Ist.

**Tagesweise fehlt uns die Kontrolle.** Ein Abgleich über die Tagesliste ist
bewusst nicht vorgesehen (Entscheidung vom 03.09.). Ersatz ist ein Alarm auf der
**eigenen Eingangsrate**: Kommen statt der üblichen ~2.000 Meldungen plötzlich
300 oder gar keine, ist etwas kaputt — gleich wo, und ohne dass wir FocalX dafür
befragen müssten.

Weitere Alarme:

- **Fehlende Ausschnitte — am selben Tag.** Unwiederbringlich, siehe oben.
- **Nachrichten in der Fehlerablage.** Was dreimal scheiterte, braucht einen
  Menschen.
- **Alter der ältesten Nachricht in der Warteschlange.** Läuft die Verarbeitung
  der Lieferung davon, merkt man es hier zuerst.
- **Kennzeichen unplausibel.**

Als Notnagel bleibt der Abholweg erhalten — nicht als Dienst, aber als Werkzeug:
`archive_probe.py day <datum>` kann jederzeit einen Zeitraum nachziehen. Das
funktioniert für Vollbilder auch Wochen später, weil ein erneuter Report-Abruf
sie neu signiert; für Ausschnitte nicht.

---

## 7. Aufbewahrung: drei Jahre, dann weg

**Festgelegt am 03.09.2026.** Begründung ist die regelmäßige Verjährung: So
lange können Ansprüche aus dem Mietvertrag geltend gemacht werden, danach gibt
es keinen Anlass mehr, ein Foto anzusehen. Das Archiv wächst damit nicht
unbegrenzt, sondern pendelt sich bei rund **26 TB** ein.

Vier Punkte, die man beim Bauen kennen muss:

- **Rollierend, nicht stichtagsbezogen.** Jedes Objekt verschwindet drei Jahre
  nach *seiner* Ablage. Es fällt nicht zum Jahreswechsel ein ganzer Jahrgang
  weg, sondern täglich hinten so viel, wie vorne dazukommt.
- **Zwei Stufen, nicht drei.** 90 Tage S3 Standard, danach Glacier Instant
  Retrieval. Eine weitere Umlagerung ins Deep Archive spart zwar Speicher,
  kostet aber ungefähr dasselbe an Umlagerungsgebühren — die werden **je
  Objekt** berechnet, und wir lagern 2,3 Mio. Objekte im Monat um.
- **Reports und Manifeste bleiben in Standard.** Sie sind winzig (~13 KB) und
  sollen jederzeit sofort lesbar sein. Ein Größenfilter in der Lifecycle-Regel
  (`ObjectSizeGreaterThan`) trennt sie von den Bildern, ohne dass das
  Ablageschema geändert werden müsste.
- **Ausnahme für strittige Fälle.** Läuft zu einem Fahrzeug ein Verfahren,
  dürfen dessen Bilder nicht mitten darin automatisch verschwinden. Eine
  Markierung, die die Löschregel überspringt, ist technisch einfach — sie muss
  nur stehen, **bevor** die Regel scharf geschaltet wird. Danach ist nichts
  zurückzuholen.

Das Löschen selbst kostet nichts und umgeht nebenbei, dass uns AWS heute das
Löschen verbietet: Eine Lifecycle-Regel braucht kein `s3:DeleteObject`-Recht
für unsere Rolle. Für den DSGVO-Löschweg auf Verlangen gilt das **nicht** —
der bleibt offen.

---

## 8. Kosten

2.000 Endpoint-Aufrufe und 2.000 Lambda-Läufe à 7 Sekunden am Tag liegen im
Bereich **weniger Euro im Monat**, dazu SQS im Cent-Bereich. Neben den
170–190 $ Speicher im Beharrungszustand (Abschnitt 7) fällt das nicht ins
Gewicht. Der einzige Posten mit Gewicht wäre ein
NAT-Gateway (~97 $), und der ist vermeidbar, solange Lambda ohne VPC laufen
darf.

---

## 9. Was mit FocalX vereinbart werden muss

Das ist jetzt eine Schnittstelle zwischen zwei Häusern.

Am 03.09.2026 hat FocalX drei wesentliche Punkte zugesagt:

1. Sie melden **sofort nach Abschluss der Analyse**.
2. Sie **wiederholen**, wenn unser Endpoint nicht antwortet.
3. Sie melden **nur unsere Inspektionen**, nicht fremde Arbeitsabläufe.

Noch abzustimmen:

1. Melden sie auch **unfertige** Reports? Wir würden sie abweisen.
2. Wie oft und wie lange wiederholen sie?
3. Wer meldet sich bei wem, wenn nichts mehr ankommt?
4. Wie wird der **API-Key** übergeben und rotiert?

---

## 10. Vorarbeiten

1. **Vom Benchmark-Code lösen.** `archive_probe.py` importiert `_env` aus
   `eval.pipeline` und zieht damit `ground_truth`, `mapping` und die
   LLM-Anbindung mit. Der Archivdienst braucht davon nichts außer
   `eval.focalx.FocalxClient` (reine Standardbibliothek).
2. **Eingangsprüfung** nach Abschnitt 5 als eigene Funktion, damit Endpoint und
   Abholweg dieselbe benutzen.
3. **Lambda-Paket bauen:** eine SQS-Nachricht entspricht einem Report und
   damit genau einer Inspektion.

Der API-Key wird von API Gateway über API-Key und Usage Plan geprüft. Die
Lambda braucht ihn nicht und erhält deshalb bewusst keinen Zugriff auf Secrets
Manager.

Entfallen gegenüber dem Abhol-Entwurf: die Fensterung der Tagesliste und die
Erneuerung der Anmeldung bei 401 — beides betraf nur den Dauerbetrieb des
Abholens. Für das gelegentliche Nachziehen von Hand reicht der heutige Stand.

---

## 11. Im Dev-Konto bereits angelegt

Am 03.09.2026 wurden mit den temporären Administratorrechten angelegt:

- SQS `focalx-archive`, verschlüsselt, 14 Tage Aufbewahrung, 15 Minuten
  Sichtbarkeit.
- Fehlerwarteschlange `focalx-archive-dlq`, ebenfalls verschlüsselt und 14
  Tage aufbewahrt. Nach drei fehlgeschlagenen Versuchen wandert eine Nachricht
  dorthin.
- `focalx-archive-lambda`: Laufzeitrolle mit Logs, Lesen/Schreiben im
  Testbucket und Konsumieren der Hauptwarteschlange.
- `focalx-archive-apigw-sqs`: darf ausschließlich Nachrichten in die
  Hauptwarteschlange schreiben.
- `focalx-archive-deployer`: PowerUser plus `iam:PassRole` ausschließlich für
  die beiden vorigen Rollen. Die normale SSO-Rolle kann sie annehmen; ein
  direkter `PassRole`-Eintrag auf der verwalteten SSO-Rolle wurde von der
  zentralen Sixt-SCP ausdrücklich blockiert.

Lokales AWS-Profil für weitere Deployments:

```bash
export AWS_PROFILE=focalx-deployer
```

Der Rundtest Senden → Empfangen → Löschen auf SQS war erfolgreich. Die
PassRole-Simulation erlaubt genau die beiden Laufzeitrollen und verweigert die
Deployment-Rolle selbst.

Die Lambda `focalx-archive` ist ebenfalls aktiv:

- Python 3.12 auf ARM64, 1.024 MB, 120 Sekunden Timeout.
- Ein Report je Aufruf (`BatchSize=1`), höchstens drei Inspektionen parallel.
- Partielle Batch-Antworten: Nur eine vollständig archivierte Inspektion wird
  aus SQS entfernt. Bei einer Bildlücke bleibt die Nachricht liegen; der
  nächste Versuch übernimmt vorhandene Objekte und lädt nur die Lücke.
- Kein VPC-Anschluss. Der Download von FocalX funktioniert damit direkt und
  verursacht im Dev-Aufbau keine NAT-Gebühren.

Reproduzierbares Deployment, ohne Administratorrechte:

```bash
export AWS_PROFILE=focalx-deployer
~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_lambda.py
```

Der Live-Test schickte einen echten 49.706-Byte-Report durch SQS. Die Lambda
archivierte **57/57 Objekte, 39,5 MB, in 2,98 Sekunden**, verwendete maximal
138 MB Arbeitsspeicher und schrieb ein vollständiges Manifest. Danach waren
Haupt- und Fehlerwarteschlange leer. Ziel:
`s3://sixt-focalx-archiv-test-180111006559/focalx-push/`.

### Der Endpoint

```
POST https://i0lum1ub7j.execute-api.eu-central-1.amazonaws.com/v1/inspections
Header: x-api-key, Content-Type: application/json
Rumpf:  das Report-JSON, unverändert
Antwort: 200 {"status":"accepted"}
```

Regional, kein Lambda dazwischen: API Gateway prüft den Schlüssel und legt den
Rumpf unverändert in die Warteschlange. Drosselung 20 Anfragen je Sekunde
(Spitze 40), Tageskontingent 20.000 — bei erwarteten 2.000 also Luft nach oben,
aber eine Obergrenze, falls jemand in eine Schleife gerät.

```bash
export AWS_PROFILE=focalx-deployer
~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_api.py
```

Der Schlüsselwert wird bewusst **nicht** ausgegeben; das Skript nennt nur die
Schlüssel-ID (`utfyecy0oa`). Abrufen für die Übergabe an FocalX:

```bash
aws apigateway get-api-key --api-key utfyecy0oa --include-value \
  --query value --output text
```

**Bewusst ohne Schema-Prüfung am Tor.** Wer mit 4xx abgewiesen wird, versucht
es nicht erneut — ein zu Unrecht abgelehnter Report wäre endgültig verloren.
Beanstandet dagegen erst die Lambda, liegt die Nachricht in der Fehlerablage
und ist nachholbar. Solange FocalX' genaues Push-Format nicht beobachtet ist,
ist das die richtige Richtung zu irren. Nachschärfen kann man später.

Geprüfter Stand:

| Fall | Antwort |
| --- | --- |
| ohne Schlüssel | 403 Forbidden |
| falscher Schlüssel | 403 Forbidden |
| `Content-Type: text/plain` | 415 Unsupported Media Type |
| echter Report | 200 in **0,18 s**, danach 38/38 Objekte archiviert |

**Eine Obergrenze, die man kennen muss:** SQS nimmt höchstens 256 KB je
Nachricht. Der größte bisher gesehene Report hat 67 KB, das ist knapp das
Vierfache Luft. Wird sie je gesprengt, antwortet der Endpoint mit 5xx statt
still zu verwerfen — dafür gibt es einen Alarm. Der Ausweg wäre dann, den
Report zuerst nach S3 zu legen und nur den Verweis in die Warteschlange zu
geben.

### Alarme

```bash
~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_alarms.py
```

Meldeweg ist das SNS-Thema `focalx-archive-alerts`. Zwei der Messwerte kommen
aus den Lambda-Protokollen (`InspectionFailures`, `InspectionsArchived`), damit
ein Fehlschlag **sofort** auffällt. Über die Fehlerablage allein bekäme man ihn
erst **45 Minuten** später zu sehen: drei Versuche mal 15 Minuten
Sichtbarkeitsdauer, dann erst der Umzug (gemessen 17:03:53 → 17:49).

| Alarm | schlägt an, wenn |
| --- | --- |
| `fehlerablage` | irgendetwas endgültig gescheitert in der Fehlerablage liegt |
| `stau` | die älteste Nachricht länger als 30 Minuten wartet |
| `einordnen-gescheitert` | die Lambda eine Inspektion nicht einordnen konnte |
| `lambda-fehler` | die Lambda abstürzt, statt einen Fehler zu melden |
| `lambda-gedrosselt` | die Parallelitätsgrenze dauerhaft greift |
| `endpoint-fehler` | FocalX Serverfehler bekommt (dabei geht Datenverkehr verloren) |
| `nichts-angekommen` | 24 Stunden lang keine Inspektion archiviert wurde |

Der letzte ist der wichtigste und deshalb der einzige, der im Dev-Konto
**stummgeschaltet** ist: Solange FocalX nicht liefert, stünde er dauerhaft auf
Rot und würde alle anderen mit abstumpfen. Vor dem Produktivgang:

```bash
aws cloudwatch enable-alarm-actions --alarm-names focalx-archive-nichts-angekommen
```

Geprüft mit einer absichtlich kaputten Nachricht, einmal den ganzen Weg
entlang: Die Lambda wies sie ab (`InvalidReport: InspectionId fehlt oder ist
keine UUID`), der Frühwarn-Alarm schlug binnen Minuten an, nach genau drei
Abweisungen zog SQS die Nachricht in die Fehlerablage — einen vierten
Lambda-Aufruf gab es nicht mehr. Danach wurde die Fehlerablage geleert.

---

## 12. Offen

- **Dev statt Produktion:** Für den Start bewusst akzeptiert. Überwachung und
  Betrieb bauen wir selbst; ein späterer Umzug bleibt möglich.
- **Zweiter API-Key** für eine unterbrechungsfreie Rotation. Der Usage Plan
  kann mehrere gleichzeitig führen; angelegt ist bisher einer.
- **Anmeldung am Meldeweg bestätigen** — die Mail an gottlieb.dinh@sixt.com ist
  raus, ohne Klick darin bleiben die Alarme stumm.
- **Lifecycle:** 90 Tage Standard, danach Glacier Instant Retrieval, nach drei
  Jahren löschen.
- **Löschrecht** fehlt (explizites Verbot auf `s3:DeleteObject`); betrifft den
  DSGVO-Löschweg, nicht mehr den Betrieb.
- **Herkunft einschränken:** Der Endpoint ist öffentlich erreichbar und nur
  durch den Schlüssel geschützt. Sobald FocalX' Absenderadressen feststehen,
  gehört eine Ressourcen-Richtlinie davor, die alles andere abweist.
