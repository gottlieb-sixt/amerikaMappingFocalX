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

Der Schlüssel selbst liegt im Secrets Manager. Zwei gleichzeitig gültige
Schlüssel erlauben eine Rotation ohne Absprache im Minutentakt — ein Schlüssel,
der nie wechselt, steht irgendwann in einem Wiki.

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

1. **Wann** melden sie — sofort nach Abschluss der Analyse? Davon hängt ab, ob
   der Gewinn bei den Ausschnitten eintritt.
2. **Wiederholen sie**, wenn unser Endpoint nicht antwortet? Wie oft, wie lange?
   Das ist die wichtigste Frage, weil eine verlorene Meldung sonst endgültig
   verloren ist.
3. Melden sie **alle** Inspektionen oder nur die aus unserem Arbeitsablauf? Die
   Tagesliste enthielt auch fremde (Bahnverladung, Mobile-App).
4. Melden sie auch **unfertige** Reports? Wir würden sie abweisen.
5. Wer meldet sich bei wem, wenn nichts mehr ankommt?
6. Wie wird der **Schlüssel** übergeben und rotiert?

---

## 10. Vorarbeiten

Alle ohne AWS machbar, parallel zum Kontoantrag.

1. **Zugangsdaten aus dem Secrets Manager** statt aus `.env`.
2. **Vom Benchmark-Code lösen.** `archive_probe.py` importiert `_env` aus
   `eval.pipeline` und zieht damit `ground_truth`, `mapping` und die
   LLM-Anbindung mit. Der Archivdienst braucht davon nichts außer
   `eval.focalx.FocalxClient` (reine Standardbibliothek).
3. **Eingangsprüfung** nach Abschnitt 5 als eigene Funktion, damit Endpoint und
   Abholweg dieselbe benutzen.

Entfallen gegenüber dem Abhol-Entwurf: die Fensterung der Tagesliste und die
Erneuerung der Anmeldung bei 401 — beides betraf nur den Dauerbetrieb des
Abholens. Für das gelegentliche Nachziehen von Hand reicht der heutige Stand.

---

## 11. Offen

- **Produktivkonto** nicht beantragt — ohne das nichts davon.
- **VPC-Pflicht** entscheidet über die NAT-Kosten (~97 $ oder 0 $).
- **Löschrecht** fehlt (explizites Verbot auf `s3:DeleteObject`); betrifft den
  DSGVO-Löschweg, nicht mehr den Betrieb.
