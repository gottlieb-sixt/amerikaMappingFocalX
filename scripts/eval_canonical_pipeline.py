"""Welche Schwellen für eine ZWEISTUFIGE Pipeline? Erst Piktogramm, dann KI.

Stufe 1 rein metrisch (kanonischer Fahrzeugrahmen, eval/canonical.py), Stufe 2
KI nur noch auf der Vorauswahl. Gemessen wird über ALLE Benchmark-Urteile —
auch die ohne Gold-Match, weil nur die die Präzision bestimmen.

Drei Entscheidungen werden getrennt vermessen:

  A  AUTO-MATCH   d1 ≤ τ (evtl. + Vorsprung zu Platz 2) → ohne KI übernehmen
  B  AUTO-LEER    d1 > τ → ohne KI als "FocalX hat den Schaden nicht" abhaken
  C  SHORTLIST    Kandidaten mit d ≤ τ, höchstens k → gehen an die KI

Nur lesend. Mit .venv/bin/python laufen (PIL für die Templategröße).

  .venv/bin/python scripts/eval_canonical_pipeline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import canonical as canon  # noqa: E402
from eval import strategy as strat  # noqa: E402
from eval.matcher import part_class  # noqa: E402

RESULTS = ROOT / "data" / "results"
GOLD = ROOT / "gold" / "mapping_gold.json"


def build() -> list[dict]:
    """Ein Eintrag pro Benchmark-Urteil mit kanonisch sortierten Kandidaten.

    Kandidat = Finding-Cluster aus data/results (genau das, was ein Strategie-Lauf
    dem Judge vorlegt); Abstand = kürzester Abstand seiner verorteten Mitglieder."""
    recs = strat.benchmark_records()
    gold = json.loads(GOLD.read_text())
    proj_by = {(c["checkin"], d["gt_key"]): d.get("projection")
               for c in gold["cars"] for d in c["damages"]}
    out = []
    for checkin in sorted({r["checkin"] for r in recs}):
        rf = RESULTS / f"{checkin}.json"
        if not rf.exists():
            continue
        r = json.loads(rf.read_text())
        pk = canon.plate_key(r["plate"])
        ph = r.get("physical") or {}
        clusters = ph.get("finding_clusters") or [[f["key"]] for f in r["findings"]]
        find_by_key = {f["key"]: f for f in r["findings"]}
        fx = canon.fx_records(pk)
        by_key = {k: rec for rec in fx for k in rec["keys"]}
        for rec in [x for x in recs if x["checkin"] == checkin]:
            proj = proj_by.get((checkin, rec["gt_key"]))
            gp = canon.gt_point(pk, rec["damage_ids"], proj) if proj else None
            cands = []
            if gp is not None:
                for ks in clusters:
                    ds = [canon.dist(gp, by_key[k]["point"]) for k in ks
                          if k in by_key and by_key[k]["localized"]]
                    if ds:
                        cands.append({"keys": sorted(ks), "dist": 100 * min(ds)})
                cands.sort(key=lambda c: c["dist"])
            tc = part_class(rec["part"])
            pc = [sorted(ks) for ks in clusters
                  if tc and part_class(find_by_key.get(ks[0], {}).get("part")) == tc]
            out.append({
                "plate": r["plate"], "gt_key": rec["gt_key"],
                "gold": sorted(rec["gold_keys"]), "cands": cands,
                "part_class": pc,
                "has_geo": gp is not None, "n_clusters": len(clusters),
            })
    return out


def _pct(a, b):
    return f"{100*a/b:>3.0f}%" if b else "  —"


def main() -> None:
    rows = build()
    n = len(rows)
    mapp = [r for r in rows if r["gold"]]
    empty = [r for r in rows if not r["gold"]]
    no_geo = [r for r in rows if not r["has_geo"]]
    print("=" * 74)
    print("ZWEISTUFIGE PIPELINE — Schwellen für die Piktogramm-Vorstufe")
    print("=" * 74)
    print(f"Benchmark: {n} Urteile · {len(mapp)} mit Gold-Match · "
          f"{len(empty)} ohne · {len(no_geo)} ohne DB-Geometrie")
    print(f"Ø Kandidaten-Cluster ohne Gate: "
          f"{sum(r['n_clusters'] for r in rows)/n:.1f}")

    # ── A: AUTO-MATCH (Platz 1 ohne KI übernehmen) ───────────────────────────
    print("\n" + "─" * 74)
    print("A  AUTO-MATCH: Platz 1 ohne KI übernehmen, wenn d1 ≤ τ")
    print("─" * 74)
    print(f"  {'τ':>5} {'feuert':>7} {'enthält Gold':>13} {'exakt = Gold':>13} "
          f"{'davon Gold leer':>16}")
    for t in (2, 3, 4, 5, 6, 8, 10, 12, 15):
        fired = [r for r in rows if r["cands"] and r["cands"][0]["dist"] <= t]
        if not fired:
            continue
        contains = sum(1 for r in fired
                       if set(r["cands"][0]["keys"]) & set(r["gold"]))
        exact = sum(1 for r in fired
                    if set(r["cands"][0]["keys"]) == set(r["gold"]))
        wrong_empty = sum(1 for r in fired if not r["gold"])
        print(f"  {t:>4}% {len(fired):>7} {_pct(contains,len(fired)):>13} "
              f"{_pct(exact,len(fired)):>13} {wrong_empty:>16}")

    print("\n  mit Vorsprung: zusätzlich d2 − d1 ≥ m (Platz 1 klar vor Platz 2)")
    print(f"  {'τ':>5} {'m':>5} {'feuert':>7} {'enthält Gold':>13} {'exakt':>7}")
    for t in (4, 6, 8, 10):
        for m in (3, 5, 8):
            fired = [r for r in rows if r["cands"]
                     and r["cands"][0]["dist"] <= t
                     and (len(r["cands"]) < 2
                          or r["cands"][1]["dist"] - r["cands"][0]["dist"] >= m)]
            if not fired:
                continue
            contains = sum(1 for r in fired
                           if set(r["cands"][0]["keys"]) & set(r["gold"]))
            exact = sum(1 for r in fired
                        if set(r["cands"][0]["keys"]) == set(r["gold"]))
            print(f"  {t:>4}% {m:>4}% {len(fired):>7} "
                  f"{_pct(contains,len(fired)):>13} {_pct(exact,len(fired)):>7}")

    # ── B: AUTO-LEER (ohne KI als "nicht gefunden" abhaken) ──────────────────
    print("\n" + "─" * 74)
    print("B  AUTO-LEER: ohne KI abhaken, wenn der NÄCHSTE Kandidat weiter als τ weg ist")
    print("─" * 74)
    print(f"  {'τ':>5} {'feuert':>7} {'davon korrekt leer':>19} "
          f"{'verlorene Matches':>18}")
    for t in (15, 20, 25, 30, 40, 50, 60):
        fired = [r for r in rows if not r["cands"] or r["cands"][0]["dist"] > t]
        if not fired:
            continue
        ok = sum(1 for r in fired if not r["gold"])
        lost = sum(1 for r in fired if r["gold"])
        print(f"  {t:>4}% {len(fired):>7} {_pct(ok,len(fired)):>19} {lost:>18}")

    # ── C: SHORTLIST für die KI ─────────────────────────────────────────────
    print("\n" + "─" * 74)
    print("C  SHORTLIST an die KI: Kandidaten mit d ≤ τ, höchstens k")
    print("─" * 74)
    print(f"  {'τ':>5} {'k':>3} {'Gate-Recall':>12} {'Ø Kandidaten':>13} "
          f"{'leere Liste':>12} {'davon zu Recht':>15}")
    for t in (15, 20, 25, 30, 40, 100):
        for k in (2, 3, 4):
            short = [[c for c in r["cands"] if c["dist"] <= t][:k] for r in rows]
            recall = sum(1 for r, s in zip(rows, short)
                         if r["gold"] and any(set(c["keys"]) & set(r["gold"])
                                              for c in s))
            n_empty = sum(1 for s in short if not s)
            empty_ok = sum(1 for r, s in zip(rows, short) if not s and not r["gold"])
            print(f"  {t:>4}% {k:>3} {_pct(recall,len(mapp)):>12} "
                  f"{sum(len(s) for s in short)/n:>13.1f} {n_empty:>12} "
                  f"{_pct(empty_ok,n_empty):>15}")

    # ── Konkrete Konfigurationen durchgerechnet ─────────────────────────────
    print("\n" + "═" * 74)
    print("DURCHGERECHNET — was die KI in Stufe 2 vorgelegt bekäme")
    print("═" * 74)

    def shortlist(r, tau, k, with_pc):
        s = [c for c in r["cands"] if c["dist"] <= tau][:k]
        if with_pc:
            have = {tuple(c["keys"]) for c in s}
            s = s + [{"keys": ks, "dist": None} for ks in r["part_class"]
                     if tuple(ks) not in have]
        return s

    configs = [
        ("Bauteilklasse allein (v05 heute)", None, 0, True),
        ("kanonisch τ=25 %, k=3", 25, 3, False),
        ("kanonisch τ=40 %, k=4", 40, 4, False),
        ("kanonisch τ=40 %, k=4  ∪ Bauteilklasse", 40, 4, True),
        ("alle Kandidaten (v03)", 10 ** 6, 10 ** 6, False),
    ]
    print(f"  {'Konfiguration':<40} {'Recall':>7} {'Ø Kand.':>8} {'KI-Calls':>9} "
          f"{'Obergr.':>8}")
    print("  " + "-" * 74)
    for label, tau, k, with_pc in configs:
        lists = [shortlist(r, tau if tau is not None else -1, k, with_pc) for r in rows]
        reach = sum(1 for r, s in zip(rows, lists)
                    if r["gold"] and any(set(c["keys"]) & set(r["gold"]) for c in s))
        calls = sum(1 for s in lists if s)
        avg = sum(len(s) for s in lists) / max(1, calls)
        # Obergrenze: KI wählt in der Shortlist perfekt und lehnt sonst korrekt ab
        ceil_ = (reach + sum(1 for r in rows if not r["gold"])) / n
        print(f"  {label:<40} {_pct(reach,len(mapp)):>7} {avg:>8.1f} "
              f"{calls:>4}/{n:<4} {100*ceil_:>7.0f}%")
    print("\n  Recall = Anteil der 48 Gold-Matches, die in der Shortlist stehen.")
    print("  Obergrenze = Genauigkeit, wenn die KI innerhalb der Shortlist perfekt")
    print("  wählt und alle Urteile ohne Kandidaten korrekt leer bleiben.")


if __name__ == "__main__":
    main()
