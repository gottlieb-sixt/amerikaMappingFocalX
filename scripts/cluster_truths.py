#!/usr/bin/env python3
"""Gruppiert die SHARK-DB-Zeilen eines Runs zu physischen Schäden.

Der Scanner erfasst denselben Schaden mehrfach (mehrere Zeilen, gleiche Stelle).
`eval.mapping.cluster_truths` fasst sie zusammen: gleiche Signatur aus Bauteil,
Typ, Seite und Projektion wird vorgruppiert, und nur bei mehreren Zeilen je
Signatur entscheidet die KI anhand der DB-Fotos. Das ist die GT-Seite und hängt
nicht am FocalX-Lauf.

Ergebnis landet als `gt_clusters` im Result-JSON (Top-Level, damit die
`physical`-Sicht eines Mapping-Laufs unberührt bleibt). Von dort holt sich
`run_strategy.py --all-cars` seine Arbeitsliste — ohne diese Gruppierung bekäme
jede DB-Zeile ein eigenes Urteil, also mehr Calls als es Schäden gibt.

  python3 scripts/cluster_truths.py --run fl500            # alle Autos des Runs
  python3 scripts/cluster_truths.py --run fl500 FL-07ELXT  # nur bestimmte
  python3 scripts/cluster_truths.py --run fl500 --force    # neu rechnen
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.ground_truth import Truth
from eval.mapping import cluster_truths
from eval.pipeline import RESULTS, ROOT, _env
from eval.strategy import plate_key

TRUTH_FIELDS = {f for f in Truth.__dataclass_fields__}


def _truth(d: dict) -> Truth:
    """Result-JSON-Zeile zurück in ein Truth-Objekt (nur bekannte Felder)."""
    return Truth(**{k: v for k, v in d.items() if k in TRUTH_FIELDS})


def _write(path: Path, data: dict) -> None:
    """Atomar schreiben — ein Abbruch mitten im Schreiben darf die Ergebnisse
    eines fertigen Autos nicht zerstören."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def main() -> None:
    argv = sys.argv[1:]
    force = "--force" in argv
    results = RESULTS
    run_id = ""
    if "--run" in argv:
        run_id = argv[argv.index("--run") + 1]
        from eval import runs as runs_mod
        results = runs_mod.results_dir(run_id)
    filters = [a for a in argv if not a.startswith("--") and a != run_id]

    llm_key = _env("LLM_GW_API_KEY")
    if not llm_key:
        print("FEHLER: LLM_GW_API_KEY fehlt (.env).", file=sys.stderr)
        sys.exit(1)

    files = sorted(results.glob("*.json"))
    if filters:
        files = [f for f in files if any(a in f.name for a in filters)]
    print(f"{len(files)} Auto(s) in {results.relative_to(ROOT)}", flush=True)

    tot_rows = tot_cl = n_fail = 0
    for f in files:
        d = json.loads(f.read_text())
        if d.get("skipped"):
            continue
        if d.get("gt_clusters") and not force:
            n = len(d["gt_clusters"])
            print(f"{f.stem}: schon gruppiert ({n}) — übersprungen", flush=True)
            tot_rows += len(d.get("truths") or [])
            tot_cl += n
            continue
        truths = [_truth(t) for t in (d.get("truths") or [])]
        if not truths:
            print(f"{f.stem}: keine DB-Schäden", flush=True)
            continue
        try:
            clusters = cluster_truths(llm_key, truths, plate_key(d["plate"]))
        except Exception as e:                       # ein Auto darf nie den Rest stoppen
            n_fail += 1
            print(f"{f.stem}: FEHLER — {e}", flush=True)
            continue
        d["gt_clusters"] = clusters
        _write(f, d)
        tot_rows += len(truths)
        tot_cl += len(clusters)
        print(f"{f.stem}: {len(truths)} DB-Zeilen → {len(clusters)} physische Schäden",
              flush=True)

    print(f"\nGesamt: {tot_rows} DB-Zeilen → {tot_cl} physische Schäden"
          + (f" · {n_fail} Auto(s) mit Fehler" if n_fail else ""), flush=True)


if __name__ == "__main__":
    main()
