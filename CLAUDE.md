# CLAUDE.md

Anleitung für Claude Code in diesem Repository.

## Was dieses Projekt ist

Benchmark: **Wie viele der tatsächlichen DB-Schäden (SHARK) findet FocalX?**
Echte Check-in-Fotos (19-Positionen-Walkaround, Branch 40139) → FocalX-Analyse →
multimodales KI-Mapping gegen die SHARK-Ground-Truth → menschliche Validierung im
Streamlit-Dashboard. Eigenes Git-Repo **ohne Remote** (nur lokal). Getrennt vom
Android-Projekt `~/Documents/Code/VehicleCapture`.

Zwei getrennte Metriken, niemals vermischen:
1. **FocalX-Detection-Qualität** — validiert durch menschliches Review (Stand: 54 % Recall über 20 Autos)
2. **AI-Mapping-Genauigkeit** — KI-Vorschlag vs. menschliche Wahl (Stand: 71 %; Fehler fast nur falsch-negativ)

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
              · mapping.py (Clustering + Hybrid-Judge = Kern)
scripts/      lynx_fetch.py · fetch_damages.py · download_*.py · remap.py (Mapping neu,
              ohne FocalX) · sweep.py (Nachfeger für fehlgeschlagene Urteile) · export_gold.py
dashboard/    app.py (Übersicht/Review/Metriken) · gallery.py (Zoom-Lightbox)
gold/         mapping_gold.json — versionierter Gold-Standard (menschliche Urteile)
data/         GITIGNORED: raw/<datum>/<PLATE__checkin8>/ · ground_truth/ · gt_photos/
              · results/<checkin>.json (+<checkin>/closeups/, focalx_report.json) · reviews/
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
```

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
- Close-up-/Report-URLs (CloudFront) laufen ab (403). Frischen Report holen:
  `GET {base}/api/v2/service/inspections/{id}/damagereport/` — Achtung, Findings
  können sich dabei ändern (Anzahl/Reihenfolge) → über (position, part, type)
  matchen, nicht über Index.

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
