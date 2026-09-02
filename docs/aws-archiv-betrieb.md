# Vom Handbetrieb zum Dienst: Wie das Archiv von allein läuft

Voraussetzung ist [`aws-archiv-voraussetzungen.md`](aws-archiv-voraussetzungen.md)
— dort steht, was angefragt werden muss und was gemessen wurde. Dieses Dokument
beantwortet die Anschlussfrage: **Wie wird aus dem Programm, das wir von Hand
starten, ein Dienst, der jede Nacht von selbst läuft?**

Stand 02.09.2026: Der Kern funktioniert nachweislich gegen echtes S3 — fünf
Inspektionen, 166 Bilder, keine Lücke, 350 aufgelöste Adressen, 0 Fehler
(Abschnitt 7.3 im Voraussetzungs-Dokument). Was fehlt, ist ausschließlich das
Drumherum: Zeitsteuerung, Ausführungsumgebung, Zugangsdaten, Überwachung.

---

## 1. Die Zahl, die die Architektur festlegt

Der vollständige Tageslauf vom 04.08. brauchte **9,9 min für 425 Inspektionen**
bei drei parallelen Autos. Hochgerechnet auf die geplanten 2.000 Check-ins am
Tag sind das **rund 47 Minuten**.

**Lambda bricht nach 15 Minuten hart ab.** Ein einzelner Lambda-Aufruf für einen
ganzen Tag ist damit ausgeschlossen. Das ist keine Abwägung, sondern eine
Grenze — und sie ist der Grund, warum die Entscheidung überhaupt eine ist.

Drei Wege bleiben:

| Weg | Kern | Warum nicht / warum doch |
|---|---|---|
| **Fargate-Task nach Zeitplan** | Ein Container führt den Befehl aus, den wir heute von Hand starten | **Gewählt.** Keine Zeitgrenze, kein Umbau — Parallelität, Wiederaufnahme und Zustandshaltung stecken schon im Programm |
| Lambda-Fächer mit SQS | Eine Funktion listet den Tag, 2.000 Nachrichten, eine Funktion je Inspektion (~20 s) | Wiederholung und Fehlerablage gäbe es geschenkt. Aber: mehr bewegliche Teile, und **jeder Aufruf müsste sich neu bei FocalX anmelden** — 2.000 Anmeldungen am Tag statt einer |
| EC2 mit Cron | Eine Maschine, ein Cron-Eintrag | Am schnellsten gedacht, am teuersten gepflegt. Patchen, Überwachen, Plattenplatz — alles selbst |

**Entscheidung: Fargate.** Unser Programm ist bereits ein Stapelverarbeiter mit
Parallelität (`--cars`), Wiederaufnahme über die Ablage und Fehlerbehandlung je
Auto. Genau das braucht ein Container; für Lambda müsste man es erst zerlegen.
Der Fächer bleibt der Ausbauweg, falls die Laufzeit einmal drückt — umgekehrt
wäre es ein Rückbau.

---

## 2. Der Ablauf

```
EventBridge (nachts)
   └─> ECS-Fargate-Task  (Image aus ECR)
         ├─ Secrets Manager  → FocalX-Zugangsdaten
         ├─ FocalX           → Tagesliste (gefenstert) + Reports + Bilder
         └─ S3               → report.json · manifest.json · images/ · closeups/
                               index/plate/…
```

Der Task ist ein Aufruf von `archive_probe.py day` mit `--bucket`. Mehr nicht.
Die gesamte Fachlogik — Erstreport, Prüfsummen, Adressumschreibung,
Kennzeichen-Index — liegt unverändert in `archive/` und ist bereits geprüft
(94 Prüfungen in `scripts/archive_s3_test.py`).

---

## 3. Die Tagesliste muss gefenstert werden

**Das ist der einzige Punkt, an dem der Entwurf zwingend vom heutigen Verhalten
abweichen muss.**

`inspectionswithdate` kennt **keine Paginierung** (`archive/source.py`). Unsere
eigene Warnschwelle `day_guard` steht bei 900 Einträgen; der gemessene Tag hatte
425. Bei 2.000 Check-ins am Tag liefe der Dienst also **dauerhaft im
Warnbereich**, und ob FocalX still abschneidet, ist bei ihnen angefragt und
unbeantwortet (Abschnitt 3, Frage 8 im Voraussetzungs-Dokument).

Ein stilles Abschneiden ist der gefährlichste denkbare Fehler für dieses
Vorhaben: Das Archiv wäre technisch vollständig, fachlich lückenhaft, und es
würde monatelang niemandem auffallen.

Deshalb: Der Dienst fragt **in Fenstern** ab — stündlich oder in Sechs-Stunden-
Blöcken — und vergleicht die Summe der Fenster mit der Abfrage über den ganzen
Tag. Weichen sie ab, ist das Abschneiden bewiesen und der Tag gilt als
unvollständig. Nebeneffekt: Die Fenster sind eine natürliche Aufteilung der
Arbeit, falls später doch parallelisiert werden soll.

---

## 4. Zeitplan: überlappende Nachläufe statt Nachhol-Logik

Seit vorhandene Objekte nicht mehr zurückgelesen werden, kostet ein
Wiederholungslauf **0,0 MB** (Abschnitt 7.3). Das ändert die Betriebsstrategie:

- **Nachts D−1** — der Vortag, der eigentliche Lauf.
- **Dazu D−2 und D−3** — dieselbe Arbeit noch zweimal, praktisch gratis.

Das ist die einfachste denkbare Absicherung gegen verpasste oder verspätete
Inspektionen: billiger zu bauen und zu verstehen als jede Nachhol-Logik mit
eigenem Zustand. Was schon da ist, wird übersprungen; was fehlt, wird ergänzt;
Lücken schließen sich von selbst.

**Grenze der Nachholbarkeit:** Vollbilder lassen sich Wochen später neu
signieren, Closeups nicht — und ein Mandantenwechsel macht alles Ältere mit
einem Schlag unerreichbar. Drei Tage Überlappung sind großzügig genug für
Betriebsstörungen und ehrlich genug, sich nicht auf Nachholbarkeit zu verlassen.

---

## 5. Netzwerk und Kosten

Die Rechenkosten sind Rauschen: Fargate mit 1 vCPU / 2 GB für rund eine Stunde
am Tag kostet **etwa 1,50 $ im Monat** (der Lambda-Fächer läge bei ~10 $). Neben
10–50 $ Speicher fällt das nicht ins Gewicht.

**Der teure Posten ist das Netz, und er ist vermeidbar.** Läuft der Task in einer
VPC, kostet ein NAT-Gateway rund 97 $ auf 2,15 TB. Zwei Maßnahmen:

- **S3-Gateway-Endpoint** — kostenlos und nimmt den gesamten Ablage-Verkehr aus
  dem NAT heraus. Dann zahlt nur noch der Download von FocalX.
- Alternativ **öffentliches Subnetz mit öffentlicher IP** und ganz ohne NAT,
  falls die Landing Zone das zulässt. Das ist eine Frage an das Cloud-Team
  (Abschnitt 4, Frage 2).

---

## 6. Überwachung

Das Manifest je Inspektion sagt bereits, was vollständig ist und was fehlt; der
Bestandsbericht (`archive/survey.py`) fasst es zusammen. Daraus werden Alarme:

- **Fehlende Closeups — am selben Tag.** Sie sind unwiederbringlich; ein
  Wochenbericht wäre wertlos. Gemessene Grundrate: 0,15 %.
- **Fehlende Vollbilder** — nachholbar, deshalb genügt ein Tagesbericht.
- **Tageslauf gar nicht gelaufen** oder mit Fehler beendet.
- **Fensterprüfung aus Abschnitt 3 schlägt an** — Verdacht auf Abschneiden.
- **Kennzeichen unplausibel** (`TEST`, leer) — kam in einem echten
  Produktionstag vor.

Dazu regelmäßig, aber selten: `--pruefen` liest Objekte zurück und hält sie
gegen ihre Prüfsumme. Das kostet vollen Lesezugriff und gehört deshalb auf eine
Stichprobe, nicht in den Tageslauf.

---

## 7. Vorarbeiten — laufen ohne AWS

Diese vier Punkte brauchen kein Konto und sind der ehrliche nächste Schritt,
solange der Kontoantrag läuft.

1. **Fensterung der Tagesliste** (Abschnitt 3). Pflicht, nicht Kür.
2. **Anmeldung bei 401 erneuern.** `FocalxClient` fängt 401 heute nur im
   Report-Polling ab (`eval/focalx.py:_poll_report`), nicht auf dem Leseweg, den
   `FocalxSource.day()` und `.report()` benutzen. Bei einem Lauf von 47 Minuten
   läuft das Token ab und der Tag stirbt mittendrin.
3. **Zugangsdaten aus dem Secrets Manager** statt aus `.env`.
4. **Vom Benchmark-Code lösen.** `archive_probe.py` importiert `_env` aus
   `eval.pipeline` und zieht damit `ground_truth`, `mapping` und die
   LLM-Anbindung mit in das Image. Der Archivdienst braucht davon nichts außer
   `eval.focalx.FocalxClient` (reine Standardbibliothek).

---

## 8. Reihenfolge

1. **Produktivkonto beantragen** — Wochen Vorlauf, deshalb zuerst
   (Voraussetzungs-Dokument, Abschnitt 4, inklusive Frage 11 zum Löschrecht).
2. **Vorarbeiten 1–4** parallel dazu, ohne AWS.
3. **Container bauen und lokal gegen den Dev-Bucket laufen lassen** — derselbe
   Befehl, nur im Image.
4. **Task-Definition, Zeitplan, Rolle** im Dev-Konto; ein echter Tag als Probe.
5. **Alarme** nach Abschnitt 6.
6. **In Produktion umziehen**, sobald das Konto steht.

---

## 9. Offen

- **Rate-Limit von FocalX** unbeantwortet — bis dahin bleibt es bei drei
  parallelen Autos, was für 47 Minuten Laufzeit reicht.
- **Löschrecht** fehlt heute (explizites Verbot auf `s3:DeleteObject`). Ohne das
  ist der DSGVO-Löschweg nicht ausführbar.
- **VPC-Pflicht** entscheidet über die NAT-Kosten.
- **Schneidet `inspectionswithdate` ab?** Die Fensterung aus Abschnitt 3 macht
  uns davon unabhängig, beantwortet die Frage aber nicht.
