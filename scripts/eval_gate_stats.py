"""Vollstatistik der DETERMINISTISCHEN Piktogramm-Zuordnung (τ=40 %, k=4).

Kein LLM, keine Heuristik auf Text — allein der kanonische Abstand zwischen dem
DB-Punkt des GT-Schadens (SHARK-Koordinaten) und den verorteten FocalX-Findings
(eval/canonical.py). Gemessen gegen gold/mapping_gold.json auf demselben
Benchmark, gegen den auch die Strategien gescort werden.

Gezählt wird auf zwei Ebenen, die nie vermischt werden:
  URTEIL   ein GT-Schaden (n = Benchmark-Urteile)
  PAAR     ein (GT-Schaden, Finding-Cluster)-Vorschlag — hier leben TP/FP/FN

Nur lesend. Mit .venv/bin/python laufen (PIL für die Templategröße).

  .venv/bin/python scripts/eval_gate_stats.py            # τ=40 %, k=4
  .venv/bin/python scripts/eval_gate_stats.py 25 3       # andere Schwellen
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import canonical as canon  # noqa: E402
from eval import strategy as strat  # noqa: E402

RESULTS = ROOT / "data" / "results"
GOLD = ROOT / "gold" / "mapping_gold.json"


# ── Datenbasis ───────────────────────────────────────────────────────────────

def build() -> list[dict]:
    """Ein Eintrag pro Benchmark-Urteil.

    cands  = ALLE verorteten Finding-Cluster dieses Autos, nach kanonischem
             Abstand zum DB-Punkt sortiert (das Rohmaterial des Gates)
    gold_ci = Cluster-Indizes, die laut Gold zu diesem Schaden gehören
    """
    recs = strat.benchmark_records()
    gold = json.loads(GOLD.read_text())
    proj_by = {(c["checkin"], d["gt_key"]): d.get("projection")
               for c in gold["cars"] for d in c["damages"]}
    out: list[dict] = []
    for checkin in sorted({r["checkin"] for r in recs}):
        rf = RESULTS / f"{checkin}.json"
        if not rf.exists():
            continue
        r = json.loads(rf.read_text())
        pk = canon.plate_key(r["plate"])
        ph = r.get("physical") or {}
        clusters = ph.get("finding_clusters") or [[f["key"]] for f in r["findings"]]
        fx = canon.fx_records(pk)
        by_key = {k: rec for rec in fx for k in rec["keys"]}
        n_loc = sum(1 for i, ks in enumerate(clusters)
                    if any(k in by_key and by_key[k]["localized"] for k in ks))
        for rec in [x for x in recs if x["checkin"] == checkin]:
            proj = proj_by.get((checkin, rec["gt_key"]))
            gp = canon.gt_point(pk, rec["damage_ids"], proj) if proj else None
            goldk = set(rec["gold_keys"])
            gold_ci = {i for i, ks in enumerate(clusters) if goldk & set(ks)}
            cands = canon.rank_clusters(gp, clusters, by_key) if gp else []
            for c in cands:
                c["true"] = c["index"] in gold_ci
            out.append({
                "plate": r["plate"], "checkin": checkin, "gt_key": rec["gt_key"],
                "part": rec.get("part"), "severity": rec.get("severity"),
                "gold_keys": sorted(goldk), "gold_ci": gold_ci,
                "cands": cands, "has_geo": gp is not None,
                "n_clusters": len(clusters), "n_localized": n_loc,
                "clusters": clusters,
            })
    return out


# ── Kleine Helfer ────────────────────────────────────────────────────────────

def _pct(a, b) -> str:
    return f"{100*a/b:.0f}%" if b else "—"


def _med(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _p(xs, q):
    s = sorted(xs)
    if not s:
        return None
    return s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))]


def _auc(pos, neg):
    """P(Abstand echter Kandidat < Abstand falscher Kandidat)."""
    if not pos or not neg:
        return None
    wins = ties = 0
    for a in pos:
        for b in neg:
            if a < b:
                wins += 1
            elif a == b:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def _bar(n, unit=1):
    return "█" * int(round(n / unit))


def main() -> None:
    tau = float(sys.argv[1]) if len(sys.argv) > 1 else canon.TAU
    k = int(sys.argv[2]) if len(sys.argv) > 2 else canon.K

    rows = build()
    n = len(rows)
    # Shortlist = das Gate-Ergebnis pro Urteil.
    for r in rows:
        r["short"] = [c for c in r["cands"] if c["dist"] <= tau][:k]

    mapp = [r for r in rows if r["gold_keys"]]         # Urteile MIT Gold-Match
    empt = [r for r in rows if not r["gold_keys"]]     # Urteile OHNE Gold-Match

    print("=" * 76)
    print(f"DETERMINISTISCHE PIKTOGRAMM-ZUORDNUNG  ·  τ = {tau:g} %, k = {k}")
    print("=" * 76)

    # ── 1 Datenbasis ─────────────────────────────────────────────────────────
    print("\n1  DATENBASIS")
    print(f"  Urteile (GT-Schäden im Benchmark)      {n:>6}")
    print(f"    mit Gold-Match (FocalX hat gefunden) {len(mapp):>6}   "
          f"{_pct(len(mapp), n)}")
    print(f"    ohne Gold-Match (FocalX übersehen)   {len(empt):>6}   "
          f"{_pct(len(empt), n)}")
    print(f"    ohne DB-Geometrie (nicht gatebar)    "
          f"{sum(1 for r in rows if not r['has_geo']):>6}")
    print(f"  Autos                                  "
          f"{len({r['plate'] for r in rows}):>6}")
    print(f"  Ø FocalX-Finding-Cluster pro Auto      "
          f"{sum(r['n_clusters'] for r in rows)/n:>6.1f}"
          "   (= Kandidatenpool ohne Gate)")
    print(f"  Ø davon verortbar auf dem Piktogramm   "
          f"{sum(r['n_localized'] for r in rows)/n:>6.1f}   "
          f"{_pct(sum(r['n_localized'] for r in rows), sum(r['n_clusters'] for r in rows))}")

    # ── 2 Was das Gate ausgibt ───────────────────────────────────────────────
    tot_cand = sum(len(r["short"]) for r in rows)
    nonempty = [r for r in rows if r["short"]]
    tot_find = sum(len(c["keys"]) for r in rows for c in r["short"])
    sizes = Counter(len(r["short"]) for r in rows)
    print("\n2  WAS DAS GATE PRO SCHADEN AUSGIBT")
    print(f"  Ø zugeordnete Cluster pro Schaden      {tot_cand/n:>6.2f}")
    print(f"  Ø nur über Schäden mit ≥1 Kandidat     "
          f"{tot_cand/max(1,len(nonempty)):>6.2f}   ({len(nonempty)} Schäden)")
    print(f"  Ø zugeordnete Einzel-Findings          {tot_find/n:>6.2f}   "
          f"(Cluster können mehrere Findings bündeln)")
    print(f"  Reduktion des Kandidatenpools          "
          f"{sum(r['n_clusters'] for r in rows)/n:>6.1f} → {tot_cand/n:.1f}   "
          f"−{100-100*tot_cand/sum(r['n_clusters'] for r in rows):.0f}%")
    print("\n  Verteilung der Listenlänge:")
    for s in range(0, k + 1):
        print(f"    {s} Kandidaten  {sizes.get(s,0):>4}  {_pct(sizes.get(s,0), n):>4}  "
              f"{_bar(sizes.get(s,0), 1)}")

    # ── 3 Paar-Ebene: TP / FP / FN ───────────────────────────────────────────
    tp = sum(1 for r in rows for c in r["short"] if c["true"])
    fp = sum(1 for r in rows for c in r["short"] if not c["true"])
    fp_on_mapp = sum(1 for r in mapp for c in r["short"] if not c["true"])
    fp_on_empt = sum(1 for r in empt for c in r["short"] if not c["true"])
    gold_ci_tot = sum(len(r["gold_ci"]) for r in rows)
    fn = gold_ci_tot - tp
    prec = tp / (tp + fp) if (tp + fp) else 0
    recl = tp / gold_ci_tot if gold_ci_tot else 0
    f1 = 2 * prec * recl / (prec + recl) if (prec + recl) else 0
    print("\n3  PAAR-EBENE  (ein Paar = ein vorgeschlagener Finding-Cluster)")
    print(f"  Vorgeschlagene Paare gesamt            {tp+fp:>6}")
    print(f"    ✅ richtig  (TP)                     {tp:>6}   {_pct(tp, tp+fp)} "
          f"aller Vorschläge")
    print(f"    ❌ falsch   (FP)                     {fp:>6}   {_pct(fp, tp+fp)}")
    print(f"        davon bei Schäden MIT Gold-Match {fp_on_mapp:>6}   "
          f"(Konkurrenz zum richtigen)")
    print(f"        davon bei Schäden OHNE Match     {fp_on_empt:>6}   "
          f"(müssen von Stufe 2 abgelehnt werden)")
    print(f"  Echte Gold-Paare gesamt                {gold_ci_tot:>6}")
    print(f"    ⛔ verpasst (FN)                     {fn:>6}   {_pct(fn, gold_ci_tot)}")
    print(f"\n  Precision {100*prec:.0f}%   Recall {100*recl:.0f}%   F1 {100*f1:.0f}%")
    print(f"  Ø Fehlvorschläge pro Schaden           {fp/n:>6.2f}")

    # ── 4 Urteils-Ebene ──────────────────────────────────────────────────────
    hit = [r for r in mapp if any(c["true"] for c in r["short"])]
    exact = [r for r in mapp
             if {c["index"] for c in r["short"]} == r["gold_ci"] and r["gold_ci"]]
    miss_empty = [r for r in mapp if not r["short"]]
    miss_wrong = [r for r in mapp if r["short"] and not any(c["true"] for c in r["short"])]
    ok_empty = [r for r in empt if not r["short"]]
    noise = [r for r in empt if r["short"]]
    print("\n4  URTEILS-EBENE  (ein Urteil = ein GT-Schaden)")
    print(f"  Schäden MIT Gold-Match ({len(mapp)}):")
    print(f"    richtiges Finding in der Liste       {len(hit):>6}   "
          f"{_pct(len(hit), len(mapp))}  ← Gate-Recall")
    print(f"      davon Liste EXAKT = Gold           {len(exact):>6}   "
          f"{_pct(len(exact), len(mapp))}  ← ohne KI direkt verwendbar")
    print(f"    Liste leer → Match verloren          {len(miss_empty):>6}")
    print(f"    Liste voll, aber nur Falsches drin   {len(miss_wrong):>6}")
    print(f"  Schäden OHNE Gold-Match ({len(empt)}):")
    print(f"    Liste leer → korrekt ohne KI erledigt{len(ok_empty):>6}   "
          f"{_pct(len(ok_empty), len(empt))}")
    print(f"    Liste nicht leer → KI muss ablehnen  {len(noise):>6}   "
          f"{_pct(len(noise), len(empt))}")

    # ── 5 Recall-Trichter: wo geht Recall verloren ───────────────────────────
    g_tot = gold_ci_tot
    g_loc = sum(1 for r in rows for c in r["cands"] if c["true"])
    g_tau = sum(1 for r in rows for c in r["cands"] if c["true"] and c["dist"] <= tau)
    print("\n5  RECALL-TRICHTER  (wo verliert die Geometrie die echten Paare?)")
    print(f"  Gold-Paare                             {g_tot:>6}  100%")
    print(f"    FocalX-Cluster verortet (Box da)     {g_loc:>6}  {_pct(g_loc, g_tot)}"
          f"   ← geometrische Obergrenze")
    print(f"    Abstand ≤ τ={tau:g} %                    {g_tau:>6}  {_pct(g_tau, g_tot)}")
    print(f"    zusätzlich unter den Top-{k}            {tp:>6}  {_pct(tp, g_tot)}")

    # ── 6 Ränge ──────────────────────────────────────────────────────────────
    ranks = []
    for r in mapp:
        pos = next((i for i, c in enumerate(r["cands"], 1) if c["true"]), None)
        if pos:
            ranks.append(pos)
    print("\n6  RANG DES ECHTEN FINDINGS  (in der vollen Abstandsliste)")
    cum = 0
    for i in range(1, k + 1):
        cnt = sum(1 for p in ranks if p == i)
        cum += cnt
        print(f"    Platz {i}            {cnt:>4}   Recall@{i} = "
              f"{_pct(cum, len(mapp))}  ({cum}/{len(mapp)})")
    later = sum(1 for p in ranks if p > k)
    print(f"    Platz >{k}            {later:>4}   (durch k abgeschnitten)")
    print(f"    gar nicht verortet {len(mapp)-len(ranks):>4}")

    # ── 7 Trennschärfe der Distanz ───────────────────────────────────────────
    pos_d = [c["dist"] for r in rows for c in r["cands"] if c["true"]]
    neg_d = [c["dist"] for r in rows for c in r["cands"] if not c["true"]]
    print("\n7  TRENNSCHÄRFE DES ABSTANDS  (% der Fahrzeuglänge, kanonisch)")
    if pos_d:
        print(f"  echte Paare    n={len(pos_d):>4}  median={_med(pos_d):>5.1f}  "
              f"p25={_p(pos_d,25):>5.1f}  p75={_p(pos_d,75):>5.1f}  "
              f"p90={_p(pos_d,90):>5.1f}")
    if neg_d:
        print(f"  falsche Paare  n={len(neg_d):>4}  median={_med(neg_d):>5.1f}  "
              f"p25={_p(neg_d,25):>5.1f}  p75={_p(neg_d,75):>5.1f}  "
              f"p90={_p(neg_d,90):>5.1f}")
    a = _auc(pos_d, neg_d)
    if a is not None:
        print(f"  AUC (klein = Match)  {a:.3f}   (0.5 = Zufall, 1 = perfekt)")

    # ── 8 Gate als eigenständiger Matcher (ohne KI) ──────────────────────────
    print("\n8  GATE ALS EIGENSTÄNDIGER MATCHER — nur Platz 1 nehmen, wenn d ≤ τ")
    top1 = [(r, r["short"][0] if r["short"] else None) for r in rows]
    t1_tp = sum(1 for r, c in top1 if c and c["true"])
    t1_fp = sum(1 for r, c in top1 if c and not c["true"])
    t1_ok_empty = sum(1 for r, c in top1 if not c and not r["gold_keys"])
    t1_fn = sum(1 for r, c in top1 if not c and r["gold_keys"])
    correct = sum(1 for r, c in top1
                  if ({c["index"]} if c else set()) == r["gold_ci"])
    print(f"  Urteile exakt richtig entschieden      {correct:>6}   "
          f"{_pct(correct, n)}  ← Genauigkeit ohne jedes LLM")
    print(f"    Platz 1 war richtig                  {t1_tp:>6}")
    print(f"    Platz 1 war falsch                   {t1_fp:>6}")
    print(f"    korrekt leer gelassen                {t1_ok_empty:>6}")
    print(f"    leer, obwohl es ein Match gab        {t1_fn:>6}")

    ceil_ = (len(hit) + len(empt)) / n
    print("\n  Vergleich: Gate + perfekte Stufe 2 (KI wählt in der Liste fehlerfrei)")
    print(f"    Obergrenze Genauigkeit               {100*ceil_:>5.0f}%")
    print(f"    KI-Calls                             {len(nonempty):>4}/{n}   "
          f"−{100-100*len(nonempty)/n:.0f}% gegenüber „alles an die KI“")
    print(f"    Ø Kandidaten pro Call                "
          f"{tot_cand/max(1,len(nonempty)):>6.2f}   "
          f"(statt {sum(r['n_clusters'] for r in rows)/n:.1f})")

    # ── 9 Pro Auto ───────────────────────────────────────────────────────────
    print("\n9  PRO AUTO")
    print(f"  {'Kennzeichen':<14}{'Urteile':>8}{'m. Gold':>8}{'Ø Kand.':>9}"
          f"{'Recall':>8}{'FP':>5}")
    for plate in sorted({r["plate"] for r in rows}):
        rs = [r for r in rows if r["plate"] == plate]
        m = [r for r in rs if r["gold_keys"]]
        h = sum(1 for r in m if any(c["true"] for c in r["short"]))
        f_ = sum(1 for r in rs for c in r["short"] if not c["true"])
        print(f"  {plate:<14}{len(rs):>8}{len(m):>8}"
              f"{sum(len(r['short']) for r in rs)/len(rs):>9.1f}"
              f"{_pct(h, len(m)):>8}{f_:>5}")

    print("\nLesart: Stufe 1 ist eine Vorauswahl, kein Urteil. Sie hält "
          f"{_pct(len(hit), len(mapp))} der echten")
    print(f"Matches bei {tot_cand/n:.1f} Kandidaten pro Schaden — die Entscheidung "
          "innerhalb der Liste")
    print("bleibt Stufe 2 (KI) oder dem Menschen.")


if __name__ == "__main__":
    main()
