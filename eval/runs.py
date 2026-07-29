"""FocalX-Detection-Runs: versionierte Ergebnis-/Review-Stände.

Ein *Run* = ein FocalX-Detection-Durchlauf. Weil ein neuer FocalX-Lauf eigene
Finding-Keys (F1, F2, …) vergibt, hat jeder Run seine EIGENEN Ergebnisse UND
seine eigenen Reviews — sonst würden v1-Urteile gegen v2-Funde angezeigt.

Nicht-destruktiv: der Original-Run v1 bleibt an seinem angestammten Ort
(data/results + data/reviews). Weitere Runs liegen unter data/runs/<id>/.
Eine Registry (data/runs.json) hält Labels und den aktiven Run.

stdlib-only, damit sowohl das Dashboard (.venv) als auch die Skripte
(System-python3) es nutzen können.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REG = ROOT / "data" / "runs.json"

# v1 ist speziell: zeigt auf die bestehenden (unantastbaren) Verzeichnisse.
_V1 = {"id": "v1", "label": "v1 — Original-Modell",
       "results": "data/results", "reviews": "data/reviews"}

_DEFAULT = {"active": "v1", "runs": [dict(_V1)]}


def load_registry() -> dict:
    if REG.exists():
        reg = json.loads(REG.read_text())
        if reg.get("runs"):
            return reg
    return json.loads(json.dumps(_DEFAULT))


def save_registry(reg: dict) -> None:
    REG.parent.mkdir(parents=True, exist_ok=True)
    REG.write_text(json.dumps(reg, indent=2, ensure_ascii=False))


def runs() -> list[dict]:
    return load_registry()["runs"]


def run_ids() -> list[str]:
    return [r["id"] for r in runs()]


def get_run(run_id: str) -> dict | None:
    return next((r for r in runs() if r["id"] == run_id), None)


def active_run_id() -> str:
    reg = load_registry()
    act = reg.get("active", "v1")
    return act if any(r["id"] == act for r in reg["runs"]) else reg["runs"][0]["id"]


def set_active(run_id: str) -> None:
    reg = load_registry()
    if any(r["id"] == run_id for r in reg["runs"]):
        reg["active"] = run_id
        save_registry(reg)


def results_dir(run_id: str | None = None) -> Path:
    run_id = run_id or active_run_id()
    run = get_run(run_id) or _V1
    return ROOT / run["results"]


def reviews_dir(run_id: str | None = None) -> Path:
    run_id = run_id or active_run_id()
    run = get_run(run_id) or _V1
    return ROOT / run["reviews"]


def label(run_id: str) -> str:
    run = get_run(run_id)
    return run.get("label", run_id) if run else run_id


def ensure_run(run_id: str, label: str | None = None) -> dict:
    """Registriert einen Run (falls neu) und legt seine Verzeichnisse an.
    v1 bleibt an den Legacy-Pfaden; neue Runs unter data/runs/<id>/."""
    reg = load_registry()
    run = next((r for r in reg["runs"] if r["id"] == run_id), None)
    if run is None:
        run = {"id": run_id, "label": label or run_id,
               "results": f"data/runs/{run_id}/results",
               "reviews": f"data/runs/{run_id}/reviews"}
        reg["runs"].append(run)
        save_registry(reg)
    elif label and run.get("label") != label:
        run["label"] = label
        save_registry(reg)
    (ROOT / run["results"]).mkdir(parents=True, exist_ok=True)
    (ROOT / run["reviews"]).mkdir(parents=True, exist_ok=True)
    return run
