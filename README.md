# focalx-eval

Benchmark: Wie gut findet FocalX die tatsächlichen (in der DB hinterlegten) Schäden?

```
Bilder je Auto ──► FocalX-Inspektion ──► Findings
                                            │  Matching (Heuristik + LLM-Judge)
DB-Schadensfälle ──► Ground Truth ──────────┤
                                            ▼
                              data/results/<PLATE>.json ──► Streamlit-Dashboard
```

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Zugangsdaten eintragen
```

## Nutzung
```bash
# Daten je Auto ablegen (bis die Download-API angebunden ist):
#   data/cars/<PLATE>/images/<focalx-label>.jpg   (z. B. front.jpg, rear-left.jpg)
#   data/cars/<PLATE>/ground_truth.json           (SHARK damage-case Response)

python -m eval.pipeline MKB3317          # ein Auto auswerten (~10 min FocalX)
python -m eval.pipeline --all            # alle Autos unter data/cars/

streamlit run dashboard/app.py           # Dashboard öffnen
```

## Hart erarbeitete Erkenntnisse (siehe eval/focalx.py)
1. **Positions-Labels sind ein festes Vokabular** — falsche Labels ⇒ AI ignoriert
   die Bilder still und der Report kommt LEER zurück.
2. **submitImages kann wirkungslos verpuffen** (Race) — das Flag
   `is_images_submitted` muss verifiziert und ggf. erneut submitted werden.

## TODO
- [ ] Download-API für Fahrzeugbilder anbinden (`eval/pipeline.py: load_local_images`)
- [ ] Damage-Case-API für Ground Truth anbinden (`load_local_ground_truth`)
- [ ] Parallelisierung über mehrere Autos (FocalX erlaubt ~4 parallele Inspektionen)
