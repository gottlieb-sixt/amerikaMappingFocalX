# CLAUDE.md

Anleitung für Claude Code in diesem Repository.

## Was dieses Projekt ist

Benchmark: **Wie viele der tatsächlichen DB-Schäden (SHARK) findet FocalX?**
Echte Check-in-Fotos (19-Positionen-Walkaround, Branch 40139) → FocalX-Analyse →
multimodales KI-Mapping gegen die SHARK-Ground-Truth → menschliche Validierung im
Streamlit-Dashboard. Eigenes Git-Repo mit Remote
`github.com/gottlieb-sixt/amerikaMappingFocalX`. Getrennt vom Android-Projekt
`~/Documents/Code/VehicleCapture`.

Zwei getrennte Metriken, niemals vermischen:
1. **FocalX-Detection-Qualität** — validiert durch menschliches Review (Stand: 54 % Recall über 20 Autos)
2. **AI-Mapping-Genauigkeit** — KI-Vorschlag vs. menschliche Wahl (v01: 71 %;
   Fehler fast nur falsch-negativ). Wird aktuell über **Strategien** getunt —
   siehe Abschnitt "Aktueller Fokus" unten.

**Die 📊-Ergebnisseite im Dashboard ist final und rein menschlich validiert.**
Alle 127 Schäden der 20 abgeschlossenen Autos wurden komplett manuell gemappt —
keine Zahl dort hängt von der KI ab. Remaps, neue Judge-Prompts oder
FocalX-Neuläufe dürfen diese Zahlen NICHT verändern; sie ändern sich nur durch
neue/geänderte Reviews des Users. Neue Check-ins erweitern die Basis, die
bestehenden Urteile bleiben unangetastet (eingefroren in `gold/mapping_gold.json`).

## Layout

```
eval/         focalx.py (API-Client) · pipeline.py (Ablauf/Filter) · ground_truth.py
              (SHARK-Parser) · matcher.py (Heuristik, Truth) · judge.py (LLM-Client)
              · mapping.py (Clustering + Hybrid-Judge = Kern) · strategy.py
              (Strategie-Framework: Benchmark + Scoring gegen Gold)
scripts/      lynx_fetch.py · fetch_damages.py · download_*.py · remap.py (Mapping neu,
              ohne FocalX) · sweep.py (Nachfeger für fehlgeschlagene Urteile)
              · export_gold.py · run_strategy.py (Strategie-Lauf gegen Gold-Autos)
dashboard/    app.py (Übersicht/Review/Metriken) · gallery.py (Zoom-Lightbox)
gold/         mapping_gold.json — versionierter Gold-Standard (menschliche Urteile)
strategies/   committete Judge-Varianten: <name>/meta.json + prompt.txt
              (v01-baseline = eingefrorener Original-Judge, kein Lauf nötig)
data/         GITIGNORED: raw/<datum>/<PLATE__checkin8>/ · ground_truth/ · gt_photos/
              · results/<checkin>.json (+<checkin>/closeups/, focalx_report.json)
              · reviews/ · strategies/<name>/<checkin>.json (Strategie-Läufe)
.env          GITIGNORED: FOCALX_PRECISE_USERNAME/PASSWORD, LLM_GW_API_KEY
```

## Befehle

```bash
# Dashboard (läuft meist schon auf :8501)
.venv/bin/streamlit run dashboard/app.py

# FocalX-Analyse für Check-ins (blockiert ~10 min/Auto)
python3 -m eval.pipeline --only-damaged --limit 20

# Mapping neu rechnen ohne FocalX-Neulauf (Autos als Substring-Filter)
python3 -u scripts/remap.py FL-07ELXT

# Nachfeger: holt fehlgeschlagene KI-Urteile nach, bis alle Autos "grün"
python3 -u scripts/sweep.py

# Gold-Standard nach neuen Reviews aktualisieren (nur ✔️-Autos; --all für alle)
python3 scripts/export_gold.py

# Mapping-Strategie gegen die Gold-Autos laufen lassen (s. Abschnitt unten)
python3 -u scripts/run_strategy.py v02-name --dry-run   # erst zählen, ohne API
python3 -u scripts/run_strategy.py v02-name             # echter Lauf (resümierbar)

# AWS-Archiv: Ingest + Lambda prüfen (moto, ohne Netz, 101 Prüfungen, ~17 s)
~/.cache/focalx-s3venv/bin/python scripts/archive_s3_test.py

# Echter Lauf gegen S3 (braucht boto3 → dasselbe venv, nicht system-python3)
export AWS_PROFILE=focalx-dev
~/.cache/focalx-s3venv/bin/python -u scripts/archive_probe.py \
  --bucket sixt-focalx-archiv-test-180111006559 --prefix focalx-neu \
  day 2026-08-04 --limit 5

# Endpoint-Ressourcen deployen/ändern (nimmt eine eigene, begrenzte Rolle an)
export AWS_PROFILE=focalx-deployer
~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_lambda.py

# Adressen in den Reports neu setzen (nach Umzug/Bucket-Wechsel, ohne Downloads)
python3 scripts/archive_probe.py --root archive rebuild

# Großer Stapel, unbeaufsichtigt (s. Abschnitt "Stapelläufe")
python3 -u scripts/batch.py --run fl500 status
python3 -u scripts/batch.py --run fl500 all --limit 30
```

## Stapelläufe für viele Autos (`scripts/batch.py`)

Für Größenordnungen, bei denen Zuschauen keine Option ist (500 Autos ≈ 10 Tage
Rechenzeit: ~10 min FocalX + ~12 min Piktogramme + ~8 min v08 pro Auto). Der
Orchestrator arbeitet **phasenweise** über alle Autos:

`fetch` (Lynx) → `focalx` → `cluster` (Findings entdoppeln) →
`truths` (DB-Zeilen gruppieren) → `pictograms` → `mapping` (v08)

```bash
python3 -u scripts/batch.py --run fl500 fetch --days 2026-07-21..2026-08-30
python3 -u scripts/batch.py --run fl500 all              # focalx … mapping
python3 -u scripts/batch.py --run fl500 pictograms --workers 2
python3 -u scripts/batch.py --run fl500 status           # Fortschritt + Zeiten
python3 -u scripts/batch.py --run fl500 all --retry-failed   # Quarantäne leeren
```

Warum es unbeaufsichtigt sicher ist:

- **Zustand steht in den Ablagen, nicht in einer Buchhaltung.** `phase_done()`
  prüft die Artefakte (Result-JSON, Piktogramm-Dateien, Strategie-Vorschläge).
  Ein verlorenes `manifest.json` kostet nur die Zeitstatistik, keine Arbeit.
- **Ein Auto je Unterprozess.** Absturz, Timeout, kaputtes JSON treffen nie die
  Warteschlange. Nach `--max-attempts` (3) wandert das Auto in die Quarantäne
  und wird im Statusbericht ausgewiesen; Logs je Auto unter
  `data/runs/<id>/logs/<phase>/<checkin>.log`.
- **Erfolg wird nachgeprüft:** Rückgabewert 0 genügt nicht, es muss auch
  wirklich etwas in der Ablage stehen.
- **Atomare Schreibvorgänge** (`.tmp` + `os.replace`) und **eine Sperre je Run**
  (`batch.lock` mit PID) — zwei Orchestratoren auf einem Verzeichnis haben schon
  einmal Ergebnisse vermischt.
- **Strg-C beendet nach dem laufenden Auto**, nicht mitten im Schreiben.
- `--run v1` ist gesperrt: der Original-Run bleibt eingefroren.
- Zeiten: je Auto und Phase im `manifest.json`; zusätzlich `seconds` je Schaden
  in den Piktogramm-Records und je Urteil in den Strategie-Vorschlägen.

Die `fetch`-Phase kann nicht wirklich unbeaufsichtigt laufen — der Lynx-Token
lebt ~5 min. Sie **wartet** deshalb vor jedem Lynx-Aufruf auf eine frische
`.lynx_token` und macht von selbst weiter, sobald eine daliegt (`--token-timeout`
begrenzt das Warten). Fotos werden je Tag sofort nach dem Abruf geladen
(presigned, ~15 min), GT-Fotos direkt nach dem GT-Abruf (~30 min).

Ohne Gold-Standard gibt es für neue Autos **keinen Score**, nur Vorschläge:
`run_strategy.py --all-cars` leitet seine Arbeitsliste aus den `gt_clusters` der
Result-Dateien ab (statt aus `gold/mapping_gold.json`) und respektiert dabei
weiter die Ausschlüsse 🔧 repariert und ⏰ nach dem Check-in erfasst.

## Aktueller Fokus: Mapping-Strategien tunen (v02, v03, …)

Das AI-Mapping (v01: 71 % Genauigkeit, Schwäche = übersehene Matches: nur 32/67
mappbare exakt getroffen) soll durch Prompt-/Parameter-Varianten verbessert
werden. Der Gold-Standard ist das Messlineal; jede Variante ist eine
**Strategie** mit eigener Metrik-Ansicht im Dashboard (🧠-Seite).

Feste Regeln des Frameworks (`eval/strategy.py`):

- **Benchmark = 126 Urteile**, deterministisch aus `gold/mapping_gold.json`
  abgeleitet (✔️-Autos, ohne 🚫-Ausschlüsse, ohne 🔧/⏰-Auto-Ausschlüsse, ohne
  das eine Urteil ohne KI-Verfügbarkeit). Alle Strategien werden auf exakt
  dieser Menge verglichen — niemals die Filter ändern.
- **Cluster + Kandidaten sind aus v01 eingefroren** (in `data/results/*.json`).
  Strategien variieren NUR den Judge (System-Prompt, Modell, Temperatur,
  Bildanzahl, Kandidatenmenge). Sonst passen die `gt_key`s nicht mehr zum Gold.
- **v01-baseline** braucht keinen Lauf — ihre Vorschläge stehen als
  `ai_proposal_at_review` im Gold-Standard.
- Läufe schreiben NUR nach `data/strategies/<name>/` — `data/results/`,
  `data/reviews/` und `gold/` bleiben unangetastet.

Neue Strategie anlegen:

1. `strategies/<name>/` erstellen (Namensschema `v02-kurzbeschreibung`):
   `meta.json` (title, description, model, temperature, max_tokens, gt_images,
   cand_images, candidates: "stored"|"all") + `prompt.txt` (System-Prompt des
   Judge; Ausgabeformat mit Per-Kandidat-Verdicts ODER `{"matches": […]}` —
   der Parser versteht beide).
2. `python3 -u scripts/run_strategy.py <name> --dry-run` → Anzahl Calls prüfen.
3. `python3 -u scripts/run_strategy.py <name>` → läuft alle 126 Urteile,
   speichert inkrementell, ist nach Abbruch/Rate-Limit resümierbar und scort
   am Ende gegen Gold.
4. Dashboard → 🧠 AI-Mapping: Strategie-Vergleichstabelle + Detailansicht pro
   Strategie; der Expander "Fehler im Detail" listet jeden Fehlgriff
   (übersehen/falsch/fälschlich) als Futter für die nächste Prompt-Iteration.

Erste Tuning-Hebel (aus der v01-Fehleranalyse): der Judge ist zu streng —
28 übersehene Matches vs. nur 1 fälschliches. Kandidaten: weniger strenge
Confidence-Anker, "Insufficient Evidence" seltener, inklusivere Formulierung
wie im alten judge.py-SYSTEM_PROMPT, oder `candidates: "all"` gegen zu enge
Kandidaten-Vorauswahl.

Systemweites `python3` für Skripte (stdlib-only); `.venv` nur für Dashboard +
Playwright-Verifikation. UI-Änderungen headless mit Playwright gegen
`http://localhost:8501` verifizieren (Selectbox-Dropdown ist virtualisiert →
scrollen statt `has_text` über alle Optionen; `st.dataframe` ist Canvas →
Klicks über Koordinaten, Zellen nicht im DOM).

## API-Fallen (hart erarbeitet — nicht neu entdecken)

**FocalX** (`eval/focalx.py`):
- Positionslabels MÜSSEN aus dem Tenant-Vokabular stammen (`valid_labels()`, z. B.
  `front`, `aleft-rear`, `afront-right-wheel`) — falsche Namen ⇒ AI ignoriert Bilder
  still ⇒ leerer Report nach 25 min.
- `submitImages` liefert 200, setzt aber manchmal `is_images_submitted` nicht →
  `_ensure_submitted` verifiziert und wiederholt.
- **Die 168-h-Frist der Vollbild-URLs gilt der URL, nicht dem Bild.** Eine
  gespeicherte URL ist danach tot (403), aber ein frischer
  `GET {base}/api/v2/service/inspections/{id}/damagereport/` signiert neu:
  7.814/7.814 Vollbilder eines Tages waren vier Wochen später wieder ladbar.
  **Close-ups dagegen werden nicht neu signiert** — 10 von 6.673 (0,15 %) waren
  nach vier Wochen endgültig weg. Also: Close-ups sofort laden, Vollbilder sind
  nachholbar.
- **Beim Nachladen ändern sich Findings — aber zwei Effekte trennen.** Über
  Wochen ändert sich das Ergebnis (dasselbe Auto: August 32, September 38 Funde,
  nur 26 deckungsgleich) ⇒ über (position, part, type) matchen, nie über Index.
  Innerhalb von Minuten ändert sich dagegen nur die Reihenfolge im `Damages`-Feld
  (8 von 425 Reports) — plus bei **jedem** Abruf neue `Expires`/`Signature`.
  Reports nie über Bytes vergleichen; `archive/ingest.py:fingerprint` rechnet
  beides heraus.
- **Der Mandant hat gewechselt: `sixt` → `sixttwo`.** Reports vor Ende Juli tragen
  `Account: "sixt"` und liegen unter `cloudfront.net/sixt/v2/…`; alles ab August
  unter `sixttwo`. Mit den heutigen Zugangsdaten sind die alten **nicht mehr
  abrufbar** (Report-GET ⇒ HTTP 500, nicht 403/404 — leicht mit einem Serverfehler
  zu verwechseln). Die Nachladbarkeit der Vollbilder gilt also nur **innerhalb des
  aktuellen Mandanten**; ein Mandantenwechsel ist ein harter Schnitt. Das ist kein
  Aufbewahrungsproblem: Reports vom 28.07. (36 Tage alt) im neuen Mandanten laufen
  einwandfrei.
- **FocalX ändert sich unangekündigt.** Zwischen 05.08. und 02.09.2026:
  Analysedauer 5,5 min → 17 s, anderes Modell (s. o.), Kennzeichenformat
  `TXWCV5796` → `TX-WCV5796`. Kennzeichen immer selbst normalisieren; die
  eingefrorenen Benchmark-Zahlen sind historische Messungen, keine Gegenwart.

**AWS-Archiv** (`archive/`): Je Inspektion liegen **eine** `report.json` und ein
`manifest.json` im Bucket. Die Reportdatei trägt unsere `s3://`-Adressen, die
FocalX-Adressen werden nicht aufbewahrt; nicht Archiviertes steht als `null` in
den drei Adressfeldern und gesammelt in `Archiv.fehlend`. Weil damit unsere
Fassung und ein frischer FocalX-Bericht nie byteweise gleich sind, vergleicht
`ingest.fingerprint` **ohne** Adressen und ohne Sortierung — wer das aufweicht,
legt bei jedem Zweitabruf eine falsche `report.<zeitstempel>.json` an. Adressen
werden bei jedem Lauf neu gesetzt, ein Nachlauf schließt also Lücken von selbst.
Vorhandene Objekte werden **nicht** zurückgelesen — die Prüfsumme kommt aus dem
letzten Manifest; nur `--pruefen` liest wirklich nach und meldet Abweichungen
als `pruefsumme_abweichend`. Der Durchsatz im Lauf zählt daher nur frisch
Geholtes (`frisch_bytes`), sonst meldete ein Leerlauf 20 Mbit/s.
Im Dev-Konto ist `s3:DeleteObject` per **explizitem Verbot** gesperrt
(`PutObject` geht): Testdaten lassen sich nicht wegräumen, und der
DSGVO-Löschweg braucht eine eigene Rolle.

**AWS-Endpoint, Stand 03.09.2026:** Im Konto 180111006559 stehen die
verschlüsselten SQS-Queues `focalx-archive` + `focalx-archive-dlq` (14 Tage,
DLQ nach 3 Versuchen) sowie die Rollen `focalx-archive-lambda`,
`focalx-archive-apigw-sqs` und `focalx-archive-deployer`. Direkter `PassRole`
auf der verwalteten SSO-Rolle ist durch eine zentrale SCP verboten; deshalb
nimmt das lokale Profil `focalx-deployer` die dritte Rolle an. Sie ist
PowerUser plus `PassRole` ausschließlich für die zwei Laufzeitrollen und kann
keine IAM-Rollen erzeugen. Die Lambda `focalx-archive` ist aktiv (Python 3.12
ARM64, 1 GB, 120 s, BatchSize 1, max. 3 parallel, kein VPC) und schreibt nach
`s3://sixt-focalx-archiv-test-180111006559/focalx-push/`. Live-Test: 57/57
Objekte, 39,5 MB, 2,98 s, 138 MB RAM. Deployment ausschließlich über
`scripts/deploy_archive_lambda.py`; das Paket enthält nur `archive/`, keinen
Benchmark-Code.

Davor hängt der öffentliche Eingang: `POST https://i0lum1ub7j.execute-api.
eu-central-1.amazonaws.com/v1/inspections` mit `x-api-key` (Schlüssel-ID
`utfyecy0oa`, Wert nie ins Protokoll schreiben). API Gateway schiebt den Rumpf
**ohne Lambda dazwischen** nach SQS; eine Schema-Prüfung am Tor ist bewusst
unterblieben, weil ein 4xx den Report endgültig verlöre, eine Beanstandung
durch die Lambda dagegen in der DLQ nachholbar bleibt. **256 KB je
SQS-Nachricht** ist die harte Decke (größter gesehener Report: 67 KB).
Sieben Alarme melden über SNS `focalx-archive-alerts`; zwei Messwerte kommen
per Metrikfilter aus den Lambda-Protokollen, damit ein Fehlschlag sofort
auffällt statt erst nach den Wiederholungen. `focalx-archive-nichts-angekommen`
ist im Dev-Konto stummgeschaltet — ohne echten Push stünde er dauerhaft rot.
Deployment: `scripts/deploy_archive_api.py`, `scripts/deploy_archive_alarms.py`
(beide wiederholbar). Details: `docs/aws-archiv-betrieb.md`, Abschnitt 11.

**Positionslabels sind eine geteilte Vokabel** — nicht nur ein FocalX-Detail:
`eval/matcher.LABEL_SIDE_ZONE` leitet daraus die Fahrzeugseite ab. Ein Label, das
dort fehlt, ergibt `side=None` ⇒ `nearness=1` ⇒ **jedes** Finding gilt als
geografisch plausibel (Kandidatenpool unbrauchbar) und die Piktogramm-Projektion
bleibt leer (`not_localizable`). Bei neuen Slot-Namen immer beide Stellen pflegen;
`normalize_label` schluckt `custom_`-Präfix, `.jpg` und Zähler-Suffixe wie `-1`.

**Foto-Seite ≠ Schaden-Seite.** `LABEL_SIDE_ZONE` sagt, von wo das Foto blickt —
nicht, wo der Schaden sitzt. Ein Heck-Diagonalfoto zeigt beides: von 86 Funden
darauf gehörten 40 auf die Seiten- und 46 auf die Heckansicht. Die
Piktogramm-Ansicht kommt daher aus dem **Bauteilnamen**
(`locate_pictograms._projection_for`), das Foto ist nur Rückfall für Namen ohne
Richtung wie „fuel lid". Front-/Heckteile (`END_PARTS`) schlagen dabei das
Seitenwort: „tail light right" gehört aufs Heck, nicht auf die Beifahrerseite.

**Piktogramm-Lokalisierung braucht Pillow** → `.venv/bin/python`, nicht das
System-`python3` (sonst `ModuleNotFoundError: PIL`). Die Pipeline ruft sie nach
jedem fertigen Auto selbst auf (`--no-pictograms` schaltet das ab).

**LLM-Gateway** (`llm.orange.sixt.com`, `eval/judge.py`):
- Modell `vertex_ai/gemini-3.1-pro`. Reasoning-Tokens zählen ins `max_tokens`-Budget:
  zu klein ⇒ Antwort mit **leerem `choices[]`**. `MAX_TOKENS = 20000` nicht senken.
- Gemini liefert oft minimal kaputtes JSON (Klammer zu viel/zu wenig, Prosa davor) —
  `mapping._loads_lenient` repariert das. Bei neuen Fehlformen: Testfall ergänzen.
- Org-weites Rate-Limit (max ~10 parallel): Retry mit Backoff ist drin; nicht mehr
  als 2–3 parallele Worker starten. Fehlgeschlagene Urteile ⇒ `via=None`/`heuristic`
  im Ergebnis; `sweep.py` räumt sie auf. „Grün" = jedes Paar hat `via ∈ {ai, ai_rejected}`.

**Lynx gRPC-Web** (`scripts/lynx_fetch.py`): Tokens ~5 min gültig → vom User frisch
anfordern; Browser-User-Agent nötig (Cloudflare 1010); Protobuf handkodiert.
GT-Foto-URLs (presigned) halten 15–30 min → nach `--refetch` sofort laden.

## SHARK-Protobuf-Feldkarte (verifiziert gegen Klartext-Referenzen)

- Antwort: `2` = cases[] · Case: `2` = case_number, `11` = source_system
  (**2=OPERATION_APP/Agent, 10=DAMAGE GATE** — per Foto-Wasserzeichen verifiziert —,
  14=ASSURED, 7/8=unbekannt), `31` = damages[], `32` = damage_occurred_at,
  `34` = damage_created_at (`{1: seconds}`), `47` = kls_status (Case-Ebene)
- Damage: `3` = damage_number · `20` = coordinates (`3`=projection, `4`=segment,
  `6/7`=x/y, `9`=Fotos mit presigned URLs) · `24` = kls_status (3=BIG, 4=SMALL) ·
  **`31` = is_repaired (nur TRUE=1 serialisiert!)** · `39` = localized_values
  (`1`=part, `2`=type, `3`=severity, `4`=side, `5`=group)

## Statistik-Regeln (im Dashboard und in jeder Auswertung einhalten)

- Nur Autos mit ✔️-Abschluss-Haken zählen in die validierten Metriken.
- Automatisch ausgeschlossen (zählen nie): 🔧 reparierte Schäden (Feld 31) und
  ⏰ erst nach dem Check-in-Tag erfasste (Feld 34 vs. `data/raw/<datum>/`).
- Scope beidseitig: nur Exterior ohne Glas/Interior (`is_exterior_non_glass`).
- Kennzeichen-Fotos in GT-Sets gehören nicht zum Schaden (Prompt weist KI darauf hin).
- Physische Zählung: Cluster, nicht DB-Zeilen (Scanner erfasst Schäden mehrfach).

## Unantastbar

- `data/reviews/` = menschliche Arbeit. Niemals löschen oder generieren.
- `gold/mapping_gold.json` nur über `scripts/export_gold.py` aktualisieren.
- `.env` und `data/` bleiben gitignored; committet werden Code und `gold/`.
