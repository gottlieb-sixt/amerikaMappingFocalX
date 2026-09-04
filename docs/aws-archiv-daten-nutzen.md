# Mit den archivierten Daten arbeiten

Wie das Archiv gefüllt wird, steht in
[`aws-archiv-betrieb.md`](aws-archiv-betrieb.md). Dieses Dokument beantwortet
die andere Richtung: **Was kostet es, wieder heranzukommen, und wie stellt man
es an, ohne unnötig zu zahlen?**

Preise sind Näherungswerte für `eu-central-1`, Stand September 2026. Die
Größenordnungen halten, die Nachkommastellen nicht.

---

## 1. Wo was liegt

Die Grenze verläuft am Alter des einzelnen Objekts, nicht an einem Stichtag:

| | Klasse | Lagerung | Lesen |
| --- | --- | --- | --- |
| die jüngsten 90 Tage | S3 Standard | 0,0245 $/GB/Monat | gratis |
| alles Ältere | Glacier Instant Retrieval | 0,005 $/GB/Monat | 0,03 $/GB |
| Reports und Manifeste | S3 Standard, **immer** | 0,0245 $/GB/Monat | gratis |

Dass Reports und Manifeste dauerhaft in der schnellen Klasse bleiben, ist kein
Zufall: Die Umlagerungsregel gilt nur für Objekte über 128 KB. Das war
ursprünglich eine Kostenüberlegung — Glacier IR berechnet jedes Objekt mit
mindestens 128 KB, kleinere lägen dort teurer als in Standard —, hat aber eine
sehr nützliche Nebenwirkung, siehe Abschnitt 4.

**Glacier IR ist kein anderer Ort.** Derselbe Bucket, derselbe Pfad, dieselbe
Datei, dieselbe Haltbarkeit, dieselbe Verteilung über mindestens drei
Rechenzentren. Es ändert sich nur die Abrechnung. Vor allem: **kein Auftauen**.
Das unterscheidet Instant Retrieval von den anderen Glacier-Stufen, bei denen
man Minuten (Flexible) bis zwölf Stunden (Deep Archive) wartet. Hier kommt die
Datei in Millisekunden, mit demselben `GetObject`, ohne `restore`-Aufruf, ohne
Sonderbehandlung im Code.

In welcher Klasse ein Objekt gerade liegt, sagt das Feld `StorageClass`.

---

## 2. Die zwei Gebühren, die man auseinanderhalten muss

Bei jedem Lesen stellt AWS zwei **voneinander unabhängige** Fragen, und beide
haben einen eigenen Preis. Wer nur an eine denkt, verschätzt sich um den
Faktor drei.

**Aus welcher Klasse wird gelesen?** Aus Standard gratis, aus Glacier IR
0,03 $/GB. Das ist die Gegenleistung für die billige Lagerung — sie fällt an,
egal wohin die Bytes gehen, auch beim Kopieren innerhalb desselben Buckets.

**Wo landen die Bytes?** Auf einer Maschine in derselben Region gratis,
außerhalb der Region rund 0,09 $/GB.

Bezahlt wird immer die Summe:

| Gelesen aus … | → Maschine in `eu-central-1` | → Laptop, andere Region |
| --- | --- | --- |
| Standard | 0 $ | 0,09 $/GB |
| Glacier IR | 0,03 $/GB | 0,12 $/GB |

Die praktische Folge steht in der rechten Spalte: **Die Daten aus AWS
herauszuziehen ist dreimal teurer als der Glacier-Abruf selbst.** Wer größere
Mengen auswertet, rechnet in der Region — SageMaker, EC2, Lambda, alles in
`eu-central-1`. Ein Notebook in einer anderen Region kostet mehr als das ganze
Glacier zusammen.

Dazu kommen Anfragekosten, die man bei vielen kleinen Dateien nicht ignorieren
sollte: 0,01 $ je 1.000 GET aus Glacier IR gegenüber 0,0004 $ aus Standard, also
das 25-fache. Einmal über 2,3 Mio. Objekte zu gehen sind ~23 $ statt ~1 $.

---

## 3. Wann sich welche Klasse lohnt

Glacier IR spart 0,0195 $ je GB und Monat an Lagerung, ein voller Durchlauf
kostet 0,03 $ je GB. Der Umschlagpunkt:

> **Ab etwa acht kompletten Durchläufen pro Jahr wird Standard billiger.**

Auf 24 TB Altbestand: ein kompletter Durchlauf 720 $, dieselben Daten ein Jahr
in Standard zu halten 5.600 $ mehr. Für ein Archiv ist das nie auch nur in der
Nähe.

Die Entscheidung hängt an drei Dingen:

- **Lesehäufigkeit gegen Lagerdauer.** Lange lagern, selten lesen → Glacier.
  Täglich daran arbeiten → Standard.
- **Verteilt oder gebündelt.** Zwanzig Durchläufe in zwei Wochen und danach ein
  Jahr Ruhe → Glacier plus Arbeitskopie (Abschnitt 5). Gleichmäßige Zugriffe
  übers Jahr → Standard.
- **Ob wirklich alles gebraucht wird.** Meistens nicht — siehe Abschnitt 4.

Zurück nach Standard umlagern geht jederzeit und kostet eine Anfrage je Objekt,
keine Datenmenge. Die heutige Regel legt uns also nicht fest.

---

## 4. Der billigste Zugriff ist der, den man nicht macht

Weil Reports und Manifeste **dauerhaft** in Standard liegen, ist jede Auswertung
gratis, die nur Metadaten braucht: welche Schäden, welche Bauteile, welche
Schwere, welches Kennzeichen, welche Inspektion an welchem Tag. Das reicht, um
über den Gesamtbestand zu filtern.

Erst danach holt man gezielt die Bilder, die tatsächlich in die Analyse gehen.
Ein realistischer Arbeitsbestand sind ein paar tausend Autos — 500 Autos sind
etwa 3,5 GB, also gut zehn Cent Abruf. Nicht 24 TB und 720 $.

Diese Reihenfolge — erst kostenlos filtern, dann gezielt laden — ist der
wichtigste Kostenhebel überhaupt, und der Benchmark arbeitet ohnehin schon so.

---

## 5. Arbeitskopie: einmal abrufen, oft rechnen

Sobald dieselben Bilder mehr als einmal gebraucht werden, ist die Antwort nicht
„Standard oder Glacier", sondern: **einmal abrufen, Kopie anlegen, auf der Kopie
arbeiten.** Bei zehn Durchläufen ist das der Unterschied zwischen 720 $ und
7.200 $. Das Archiv bleibt derweil billig liegen, wo es hingehört.

Für wiederholtes Modelltraining tut man das ohnehin — nicht wegen der Kosten,
sondern wegen der Zeit. Jedes Bild in jeder Epoche einzeln über das Netz zu
ziehen ist unabhängig von der Klasse quälend langsam.

Wohin die Kopie gehört, hängt daran, wie heftig iteriert wird:

- **Gelegentliche Auswertung** → eigenes S3-Prefix in Standard. Einfach, nichts
  zu verwalten, ein paar Gigabyte kosten Cent-Beträge im Monat.
- **Echtes Training über viele Durchläufe** → lokale Platte an der Maschine.
  Deutlich schneller; S3 ist auch in Standard ein Netzwerkzugriff je Datei.

### Zwei Fallen

**Die Arbeitskopie darf nicht unter `focalx-push/` liegen.** Sonst greifen die
Archivregeln auch auf sie und schieben sie nach 90 Tagen zurück nach Glacier —
der Abruf wäre bezahlt und man stünde wieder am Anfang. Ein eigenes Prefix
daneben, etwa `arbeit/`, ist unberührt.

**Nicht am Original herumkopieren.** Die Klasse an Ort und Stelle zu ändern legt
technisch ein neues Objekt an, dessen Alter von vorn zählt. Damit verschiebt
sich auch die Drei-Jahres-Frist. Das Archiv bleibt unangetastet, gearbeitet wird
auf der Kopie.

### Und wieder wegräumen

An das Arbeits-Prefix gehört gleich eine Lifecycle-Regel mit 30 bis 60 Tagen,
sonst kostet die Kopie still weiter. Das erledigt auch das fehlende Löschrecht:
Eine Lifecycle-Regel läuft mit den Rechten von S3, nicht mit unseren.

```bash
export AWS_PROFILE=focalx-deployer
~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_lifecycle.py \
    --altlast arbeit/ --anwenden      # Vorsicht: löscht nach einem Tag
```

Für 30 statt 1 Tag die Frist im Skript anpassen — `--altlast` ist bewusst auf
die kürzestmögliche Frist festgelegt, weil es zum Wegräumen von Testdaten
gedacht ist.

---

## 6. Zusammenfassung der Vor- und Nachteile von Glacier IR

**Dafür:** ein Fünftel der Lagerkosten (170 statt 640 $ im Monat bei 26 TB);
gleiche Geschwindigkeit wie Standard, kein Auftauen; gleiche Haltbarkeit und
Redundanz; keine Codeänderung nötig.

**Dagegen:** 0,03 $ je abgerufenem GB, bei Standard frei; 25-fache
Anfragekosten; mindestens 90 Tage Lagerdauer werden berechnet (der Grund, warum
wir nicht früher umlagern); mindestens 128 KB je Objekt werden berechnet (daher
der Größenfilter); die Umlagerung selbst kostet ~0,02 $ je 1.000 Objekte, bei
uns rund 42 $ im Monat zusätzlich zu den 170 $.
