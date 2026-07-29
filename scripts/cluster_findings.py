#!/usr/bin/env python3
"""Entdoppelt die FocalX-Findings eines Runs — OHNE Ground-Truth-Abgleich.

Dasselbe Finding taucht auf mehreren Ansichten auf (der Kratzer an der Tür ist
auf dem Diagonal- und auf dem Seitenfoto). `eval.mapping.cluster_findings`
gruppiert diese Mehrfachsichtungen zu physischen Schäden; das ist reine
Findings-Arithmetik und braucht die DB nicht.

Ergebnis landet als `finding_clusters` im Result-JSON (Top-Level, damit die
`physical`-Sicht des Mappings unberührt bleibt). Von dort holt sich
`locate_pictograms.py --source focalx` die Cluster.

  python3 scripts/cluster_findings.py --run v3            # alle Autos des Runs
  python3 scripts/cluster_findings.py --run v3 FL-07ELXT  # nur bestimmte
  python3 scripts/cluster_findings.py --run v3 --force    # neu rechnen
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.mapping import cluster_findings
from eval.pipeline import ROOT, RESULTS, _env


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

    tot_f = tot_c = 0
    for f in files:
        d = json.loads(f.read_text())
        if d.get("skipped"):
            continue
        if d.get("finding_clusters") and not force:
            n = len(d["finding_clusters"])
            print(f"{f.stem}: schon geclustert ({n}) — übersprungen", flush=True)
            tot_f += d.get("focalx_findings_total", 0)
            tot_c += n
            continue
        findings_meta = [{"key": x["key"], "part": x["part"], "type": x["type"],
                          "position": x["position"], "orientation": x.get("orientation")}
                         for x in d.get("findings", [])]
        if not findings_meta:
            print(f"{f.stem}: keine Findings", flush=True)
            continue
        closeup_path = {x["key"]: ROOT / x["closeup"]
                        for x in d["findings"] if x.get("closeup")}
        clusters = cluster_findings(llm_key, findings_meta, closeup_path)
        d["finding_clusters"] = clusters
        f.write_text(json.dumps(d, indent=2))
        tot_f += len(findings_meta)
        tot_c += len(clusters)
        print(f"{f.stem}: {len(findings_meta)} Findings → {len(clusters)} physische Schäden",
              flush=True)

    print(f"\nGesamt: {tot_f} Findings → {tot_c} physische Schäden", flush=True)


if __name__ == "__main__":
    main()
