#!/usr/bin/env python3
"""Wie gut wäre das Mapping, wenn der Mensch aus einer Vorschlagsliste wählt?

Die harte Metrik (`eval.strategy.score`) fragt: hat die KI von allein genau das
Richtige vorgeschlagen? Hier geht es um die Frage danach — liegt das richtige
Finding überhaupt unter den Vorschlägen, und wie viele Kandidaten muss ein Mensch
dafür ansehen? Ausgewertet wird ausschließlich aus gespeicherten Läufen, ohne
einen einzigen neuen KI-Call.

    python3 scripts/eval_suggestions.py v09-multi-ranked [weitere …]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import strategy as strat  # noqa: E402

CONF_ORDER = {"high": 3, "medium": 2, "low": 1}


def suggestions(p: dict) -> list[list[str]]:
    """Vorschläge in der Reihenfolge, in der ein Mensch sie sehen würde.

    Erste Wahl ist die explizite Rangfolge des Judge. Fehlt sie (alte Läufe),
    wird aus den Per-Kandidat-Urteilen eine gebaut: Match-Verdicts nach
    Confidence zuerst, dann alles, was nicht klar ausgeschlossen wurde, nach
    Piktogramm-Abstand."""
    if p.get("ranking"):
        return [sorted(k) for k in p["ranking"]]
    cands = p.get("candidates")
    if not cands:
        return [sorted(p.get("finding_keys") or [])] if p.get("finding_keys") else []
    def key(c):
        v = str(c.get("verdict") or "").strip().lower()
        excluded = v.startswith("not")
        return (not c.get("match"), excluded,
                -CONF_ORDER.get(c.get("confidence") or "", 0),
                c["distance"] if c.get("distance") is not None else 999)
    return [sorted(c["keys"]) for c in sorted(cands, key=key)
            if not str(c.get("verdict") or "").strip().lower().startswith("not")]


def report(name: str, meta: dict, records: list[dict]) -> None:
    props = strat.proposals_for(name, meta)
    if not props:
        print(f"{name}: kein Lauf vorhanden")
        return
    s = strat.score(records, props)
    mapp = [r for r in s["records"] if r["gold_keys"]]
    has_rank = sum(1 for p in props.values() if p.get("ranking"))
    has_det = sum(1 for p in props.values() if p.get("candidates"))

    ex = sum(1 for r in mapp if set(r["proposal_keys"]) == set(r["gold_keys"]))
    part = sum(1 for r in mapp if set(r["proposal_keys"]) != set(r["gold_keys"])
               and set(r["proposal_keys"]) & set(r["gold_keys"]))
    print(f"\n=== {name} — {meta.get('title', '')}")
    print(f"  Datenlage: {len(props)} Urteile · {has_det} mit Per-Kandidat-Urteilen "
          f"· {has_rank} mit Rangfolge")
    print(f"  Harte Metrik: exakt {ex} · teilweise {part} · übersehen "
          f"{len(mapp)-ex-part} von {len(mapp)} mappbaren "
          f"→ Trefferquote {(ex+part)/len(mapp):.0%}")

    # Mehrfach-Matches: ein DB-Schaden, mehrere FocalX-Findings
    multi = [r for r in mapp if len(r["gold_keys"]) > 1]
    if multi:
        full = sum(1 for r in multi if set(r["proposal_keys"]) == set(r["gold_keys"]))
        some = sum(1 for r in multi if set(r["proposal_keys"]) & set(r["gold_keys"]))
        print(f"  Mehrfach-Matches im Gold: {len(multi)} · vollständig getroffen "
              f"{full} · mindestens eins {some}")

    # Vorschlagsliste: liegt das Gold unter den ersten N?
    print(f"  {'Vorschläge':<14}{'Trefferquote':>14}{'Ø Liste':>10}"
          f"{'Ø je Urteil':>13}")
    for n in (1, 2, 3, 5, None):
        hit = shown = shown_all = 0
        for r in s["records"]:
            sug = suggestions(props[(r["checkin"], r["gt_key"])])
            top = sug if n is None else sug[:n]
            flat = {k for ks in top for k in ks}
            shown_all += len(top)
            if r["gold_keys"]:
                shown += len(top)
                if flat & set(r["gold_keys"]):
                    hit += 1
        label = "alle" if n is None else f"Top {n}"
        print(f"  {label:<14}{hit/len(mapp):>13.0%}{shown/len(mapp):>10.1f}"
              f"{shown_all/len(s['records']):>13.1f}")


def main() -> None:
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    metas = strat.load_strategies()
    records = strat.benchmark_records()
    if not names:
        names = [n for n in sorted(metas) if strat.proposals_for(n, metas[n])]
    print(f"Benchmark: {len(records)} Urteile, "
          f"{sum(1 for r in records if r['gold_keys'])} davon mappbar")
    for n in names:
        if n not in metas:
            print(f"{n}: unbekannte Strategie")
            continue
        report(n, metas[n], records)


if __name__ == "__main__":
    main()
