#!/usr/bin/env python3
"""Nachfeger: bessert Autos nach, deren fertiges Mapping noch Paare ohne echtes
KI-Urteil hat (via=None oder Heuristik-Fallback), bis alle "grün" sind.

Läuft parallel zum Haupt-Remap, fasst aber nur Autos an, die dort laut Log
schon abgeschlossen sind — so gibt es keine Schreib-Rennen. Endet, wenn der
Haupt-Lauf durch ist und nichts mehr zu fegen bleibt (max. 2 Versuche pro Auto).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.pipeline import RESULTS, _env
from scripts.remap import remap

ROOT = Path(__file__).resolve().parent.parent
# remap_3.log enthält oben noch alte Zeilen vom Vortages-Lauf → nicht nutzen;
# sein einziges Auto (FL-FKFB79) ist bereits sauber.
LOGS = [ROOT / "data" / "remap_1.log", ROOT / "data" / "remap_2.log"]
MAX_TRIES = 2


def completed_cars() -> set[str]:
    done: set[str] = set()
    for lg in LOGS:
        if lg.exists():
            done |= set(re.findall(r"^(\S+): Zeilen-Recall", lg.read_text(), re.M))
    return done


def needs_sweep(name: str) -> bool:
    f = RESULTS / f"{name}.json"
    if not f.exists():
        return False
    r = json.loads(f.read_text())
    if r.get("skipped"):
        return False
    pairs = (r.get("physical") or {}).get("cluster_pairs") or []
    return any(cp.get("via") in (None, "heuristic") for cp in pairs)


def workers_running() -> bool:
    return subprocess.run(["pgrep", "-f", "Python.*scripts/remap.py"],
                          capture_output=True).returncode == 0


def main() -> None:
    key = _env("LLM_GW_API_KEY")
    tries: dict[str, int] = {}
    while True:
        todo = [c for c in sorted(completed_cars())
                if needs_sweep(c) and tries.get(c, 0) < MAX_TRIES]
        for name in todo:
            tries[name] = tries.get(name, 0) + 1
            print(f"### SWEEP {name} (Versuch {tries[name]})", flush=True)
            try:
                remap(RESULTS / f"{name}.json", key)
                if not needs_sweep(name):
                    print(f"### SWEEP OK {name} — jetzt komplett", flush=True)
            except Exception as e:
                print(f"### SWEEP-FEHLER {name}: {e!r}", flush=True)
        if not todo and not workers_running():
            rest = [c for c in sorted(completed_cars()) if needs_sweep(c)]
            print(f"### SWEEP FERTIG — ohne volles Urteil verbleiben: {rest or 'keine'}",
                  flush=True)
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
