# Der Archiv-Endpoint: FocalX liefert, wir ordnen ein

Voraussetzung ist [`aws-archiv-voraussetzungen.md`](aws-archiv-voraussetzungen.md)
— dort steht, was angefragt werden muss und was gemessen wurde. Dieses Dokument
beschreibt, **wie die Daten zu uns kommen und was daraus wird**.

> **Richtungswechsel am 03.09.2026.** Ursprünglich sollte ein nächtlicher Lauf
> die Daten bei FocalX abholen. Stattdessen stellen wir einen **Endpoint**
> bereit, in den FocalX hineinliefert — voraussichtlich direkt aus ihren
> internen S3-Buckets. Der Abschnitt „Was dadurch anders wird" hält fest, was
> das kostet und was es einbringt.

Stand 02.09.2026: Der Ordnungs-Kern funktioniert nachweislich gegen echtes S3 —
fünf Inspektionen, 166 Bilder, keine Lücke, 350 aufgelöste Adressen, 0 Fehler
(Abschnitt 7.3 im Voraussetzungs-Dokument).

---

## 1. Was der Richtungswechsel ändert — und was nicht

**Der Auslöser ändert sich, die Verarbeitung nicht.** Was wir gebaut haben, ist
nicht „Daten holen", sondern „Daten ordnen": ein Prefix je Inspektion, ein
Manifest mit Prüfsummen, ein Report mit Adressen ins Archiv statt zu FocalX,
ein Kennzeichen-Index, eine belastbare Vollständigkeitsaussage. Das alles muss
weiter entstehen — sonst liegt bei uns ein Haufen Objekte, den niemand einer
Inspektion zuordnen kann. Der Kern in `archive/` bleibt also; aus dem
Zeitplan wird ein Ereignis.

**Was dadurch besser wird.** Bisher war die Architektur von einer Zahl
bestimmt: 9,9 min für 425 Inspektionen, hochgerechnet **47 Minuten für einen
Tag** — mehr als die 15 Minuten, die Lambda erlaubt. Deshalb fiel die Wahl auf
einen Fargate-Task. Beim Push schrumpft die Arbeitseinheit von „ein ganzer Tag"
auf „eine Inspektion", also auf Sekunden. **Damit ist Lambda die richtige
Wahl** — die Entscheidung von gestern dreht sich um, weil sich ihre Grundlage
geändert hat.

**Was dadurch schlechter wird.** Wir wissen nicht mehr, was wir erwarten
müssten. Die Tagesliste sagte uns: gestern gab es 425 Inspektionen. Beim Push
kennen wir nur, was ankam. Fällt bei FocalX etwas aus, sieht das bei uns aus
wie ein ruhiger Tag. Ein Abgleich über die Tagesliste ist bewusst **nicht**
vorgesehen (Entscheidung vom 03.09.); ersatzweise überwachen wir die eigene
Eingangsrate, siehe Abschnitt 6.

---

## 2. Der Ablauf

```
FocalX
  │  ① POST /inspections   (API-Key)  ─ Report-JSON, ~35 KB
  ▼
API Gateway ──> Lambda "anmelden"
  │                 └─ legt den Report in incoming/ ab
  │                 └─ antwortet mit einer vorsignierten PUT-Adresse je Bild
  │
  │  ② PUT <vorsignierte Adresse>  ─ die Bilder, direkt nach S3
  ▼
S3  incoming/<inspection-id>/…        (nur schreibbar für FocalX)
  │
  │  ③ S3-Ereignis
  ▼
Lambda "einordnen"  ──>  S3  v1/<jjjj>/<mm>/<tt>/<inspection-id>/
                              report.json · manifest.json · images/ · closeups/
                          index/plate/<KENNZEICHEN>/<inspection-id>.json
```

Drei Aufrufe, drei getrennte Verantwortlichkeiten: anmelden, hochladen,
einordnen. Die Bilder laufen **nie durch unsere Rechenzeit** — sie gehen von
FocalX direkt nach S3.

---

## 3. Warum nicht einfach alles über den Endpoint

Der naheliegende Entwurf wäre ein einziger Aufruf mit allem darin. Dagegen
spricht eine harte Zahl: **API Gateway begrenzt eine Anfrage auf 10 MB.** Eine
Inspektion wiegt im Schnitt 9,5 MB, im Einzelfall deutlich mehr (eine
Mobile-App-Inspektion mit 7,5-Megapixel-Fotos brachte 164 MB). Das würde nicht
zuverlässig scheitern, sondern gelegentlich — die unangenehmste aller Varianten,
weil sie erst im Betrieb auffällt und dann nur sporadisch.

Deshalb die Zweiteilung: Der **Report** ist mit ~35 KB unkritisch und geht durch
den Endpoint. Die **Bilder** gehen an vorsignierten Adressen vorbei an unserer
Rechenzeit. Das ist zugleich billiger — Lambda zahlt keine Zeit fürs Warten auf
Uploads.

---

## 4. Die Landezone

FocalX schreibt nach `incoming/`, nicht ins Archiv. Ohne diese Trennung
bestimmt der Absender unser Ablageschema, und beim ersten Formatwechsel bei
FocalX ist das Archiv inkonsistent.

Regeln für den Bucket:

- **Nur schreiben.** Die vorsignierten Adressen erlauben genau ein `PutObject`
  auf genau einen Schlüssel, zeitlich begrenzt. Kein Lesen, kein Auflisten,
  kein Überschreiben fremder Objekte.
- **„Bucket owner enforced" bei Object Ownership.** Bei kontenübergreifenden
  Uploads gehört ein Objekt sonst dem **Absender** — wir könnten unsere eigenen
  Daten nicht lesen. Diese Einstellung muss beim Anlegen des Buckets sitzen.
- **Prüfsumme verpflichtend.** Die vorsignierte Adresse verlangt
  `x-amz-checksum-sha256`. Das prüft die Übertragung Ende zu Ende und füllt
  nebenbei unser Manifest, ohne dass wir ein einziges Byte lesen müssen.
- **Lifecycle-Regel statt Löschen.** Nach dem Einordnen ist die Landezone
  Ballast; sie doppelt zu bezahlen wäre unnötig. Da uns heute das Löschrecht
  fehlt (Voraussetzungs-Dokument, Abschnitt 4, Punkt 11), räumt eine
  Lifecycle-Regel nach wenigen Tagen auf.

---

## 5. Der Endpoint und der Schlüssel

`POST /inspections` mit dem Report-JSON im Körper, abgesichert über einen
**API-Key** im Header. API Gateway bringt dafür Usage Plans mit, die neben der
Prüfung des Schlüssels auch gleich eine Ratenbegrenzung liefern.

Dazu gehört:

- **Rotation von Anfang an mitdenken.** Ein Schlüssel, der nie wechselt, wird
  irgendwann in einem Wiki stehen. Zwei gleichzeitig gültige Schlüssel erlauben
  einen Wechsel ohne Absprache im Minutentakt.
- **Herkunft einschränken**, falls FocalX feste IP-Bereiche nennen kann — eine
  zweite Hürde, die nichts kostet.
- **Der Schlüssel liegt im Secrets Manager**, nicht im Code und nicht in einer
  Umgebungsvariablen des Deployments.

---

## 6. Vollständigkeit und Überwachung

**Je Inspektion bleibt die Prüfung erhalten**, und zwar ohne Zutun: Der Report,
den FocalX anmeldet, listet selbst alle zugehörigen Bilder. Daraus entsteht wie
bisher der Soll-Bestand, das Manifest vergleicht ihn mit dem Ist. Eine
unvollständige Lieferung fällt also weiterhin auf.

**Tagesweise fehlt uns die Kontrolle** — das ist der bewusst in Kauf genommene
Preis. Ersatz ist ein Alarm auf der **eigenen Eingangsrate**: Kommen statt der
üblichen ~2.000 Inspektionen plötzlich 300 oder gar keine, ist etwas kaputt,
ganz gleich wo. Das braucht keinen FocalX-Zugriff und keinen Schlüssel.

Weitere Alarme, unverändert aus dem Pull-Entwurf:

- **Fehlende Ausschnitte — am selben Tag.** Sie sind unwiederbringlich; ein
  Wochenbericht wäre wertlos. Gemessene Grundrate: 0,15 %.
- **Angemeldet, aber nie hochgeladen.** Eine Inspektion, deren Bilder nach
  einigen Stunden noch fehlen, ist ein abgebrochener Upload.
- **Prüfsumme abgelehnt** — S3 weist den PUT dann selbst zurück; das gehört
  gezählt und gemeldet.
- **Kennzeichen unplausibel** (`TEST`, leer) — kam in einem echten
  Produktionstag vor.

---

## 7. Kosten

Bei 2.000 Inspektionen und ~76.000 Bildern am Tag: 2.000 Endpoint-Aufrufe und
2.000 kurze Lambda-Läufe zum Einordnen sind **im Bereich weniger Euro im
Monat**. Weil die Bilder direkt nach S3 gehen, entfallen sowohl Lambda-Zeit
fürs Warten als auch NAT-Gebühren für den Download — der Posten von rund 97 $,
der den Pull-Entwurf belastet hätte, verschwindet ersatzlos.

Das Einordnen kopiert innerhalb von S3 (`CopyObject`), die Bytes laufen also
serverseitig. Zusammen mit der beim Upload berechneten SHA256 muss unsere
Lambda kein einziges Bild lesen.

---

## 8. Was mit FocalX vereinbart werden muss

Das ist jetzt eine **Schnittstelle zwischen zwei Häusern**, kein internes
Detail mehr. Zu klären:

1. Liefern sie aus AWS? Dann wäre eine kontenübergreifende Rolle der direktere
   Weg als vorsignierte Adressen — die Entscheidung fiel bewusst für den
   API-Key, aber die Frage gehört gestellt.
2. **Wann** melden sie an — sofort nach der Analyse oder gesammelt?
3. Wiederholen sie bei einem Fehler, und wie oft? Unser Ingest ist idempotent,
   ein zweiter Versuch schadet nie.
4. Liefern sie **alle** Inspektionen oder nur die eigenen? Die Tagesliste
   enthielt auch fremde Arbeitsabläufe (Bahnverladung, Mobile-App).
5. Wer meldet sich bei wem, wenn nichts mehr ankommt?

---

## 9. Vorarbeiten

1. **Bytes aus der Landezone statt aus dem Netz.** `_fetch` holt heute per
   HTTP; künftig liegt das Objekt schon in S3 und wird serverseitig kopiert.
   Die Erweiterung ist klein, weil `Store` die Ablage bereits abstrahiert — es
   fehlt das Gegenstück für die Herkunft.
2. **Zugangsdaten aus dem Secrets Manager** statt aus `.env`.
3. **Vom Benchmark-Code lösen.** `archive_probe.py` importiert `_env` aus
   `eval.pipeline` und zieht damit `ground_truth`, `mapping` und die
   LLM-Anbindung mit. Der Archivdienst braucht davon nichts.
4. Die **Anmeldung bei 401 erneuern** und die **Fensterung der Tagesliste**
   entfallen — beides betraf nur den Abholweg. Der bleibt als Werkzeug für
   Nachladen und Tests erhalten (`archive_probe.py day`), nicht als Dienst.

---

## 10. Offen

- **Löschrecht** fehlt heute (explizites Verbot auf `s3:DeleteObject`). Für die
  Landezone ist eine Lifecycle-Regel der Ausweg, für den DSGVO-Löschweg nicht.
- **Produktivkonto** weiterhin nicht beantragt — ohne das nichts davon.
- **Object Ownership** muss beim Anlegen des Buckets richtig stehen; das ist
  nachträglich unangenehm zu korrigieren.
