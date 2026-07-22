#!/usr/bin/env python3
"""Lässt eine Mapping-Strategie (strategies/<name>/) über die Gold-Autos laufen
und speichert die Vorschläge nach data/strategies/<name>/<checkin>.json.

Cluster und Kandidatenlisten sind aus dem v01-Lauf eingefroren — die Strategie
variiert NUR das Judge-Urteil (Prompt, Modell, Parameter). Nur die 126
Benchmark-Urteile werden geurteilt; data/results/ und data/reviews/ bleiben
unangetastet.

  python3 -u scripts/run_strategy.py v02-xyz              # alle Gold-Autos
  python3 -u scripts/run_strategy.py v02-xyz FL-07ELXT    # Substring-Filter
  python3 -u scripts/run_strategy.py v02-xyz --dry-run    # zählen, keine Calls
  python3 -u scripts/run_strategy.py v02-xyz --force      # vorhandene Urteile neu

Bereits geurteilte Paare werden übersprungen (Resume nach Abbruch/Rate-Limit).
Am Ende wird der Lauf gegen den Gold-Standard gescort.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import strategy as strat
from eval.judge import MAX_TOKENS, MODEL, _img_part, _post_with_retry, _text
from eval.mapping import AI_PAUSE_S, _loads_lenient, gt_images
from eval.pipeline import RESULTS, ROOT, _env


def _ai_json(llm_key: str, meta: dict, content: list[dict]) -> dict | None:
    """Wie mapping._ai_json, aber mit Strategie-Parametern (Modell, Temp, …)."""
    body = json.dumps({
        "model": meta.get("model") or MODEL,
        "messages": [
            {"role": "system", "content": meta["prompt"]},
            {"role": "user", "content": content},
        ],
        "temperature": meta.get("temperature", 0),
        "max_tokens": meta.get("max_tokens", MAX_TOKENS),
        "response_format": {"type": "json_object"},
    })
    raw = _post_with_retry(llm_key, body)
    if raw is None:
        return None
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        obj = _loads_lenient(raw)
    except Exception:
        print(f"    JSON-Parse-Fehler, Anfang: {raw[:80]!r}", file=sys.stderr)
        return None
    if isinstance(obj, list):
        obj = next((v for v in obj if isinstance(v, dict)), {})
    return obj if isinstance(obj, dict) else None


def _parse_choice(obj: dict, n_cand: int) -> tuple[list[int], str | None, str | None, str]:
    """(gewählte Kandidaten-Nummern 1-basiert, confidence, triage, reason) —
    versteht beide Ausgabeformate: Per-Kandidat-Verdicts UND {"matches": [...]}."""
    chosen: list[int] = []
    best_conf = None
    entries = obj.get("candidates") or []
    for e in entries:
        n = e.get("id") if isinstance(e, dict) else None
        if not (isinstance(n, int) and 1 <= n <= n_cand):
            continue
        if str(e.get("verdict") or "").strip().lower() == "duplicate":
            chosen.append(n)
            c = str(e.get("confidence") or "").strip().lower()
            if best_conf != "high":
                best_conf = c if best_conf is None or c == "high" else best_conf
    if not entries:
        nums = obj.get("matches", obj.get("match"))
        nums = [nums] if isinstance(nums, int) else (nums or [])
        chosen = [n for n in nums if isinstance(n, int) and 1 <= n <= n_cand]
    conf = obj.get("confidence") or best_conf
    triage = obj.get("recommendation")
    reasons = [e.get("reason") for e in entries
               if isinstance(e, dict) and str(e.get("verdict") or "").lower() == "duplicate"
               and e.get("reason")]
    reason = reasons[0] if reasons else obj.get("reason", "")
    return chosen, (str(conf) if conf is not None else None), triage, reason


def judge_record(llm_key: str, meta: dict, rec: dict, r: dict,
                 dry_run: bool) -> dict | None:
    """Baut den Judge-Call für ein Benchmark-Urteil (eingefrorene Kandidaten)
    und gibt das Proposal-Dict zurück. dry_run → nur zählen, kein Call."""
    by_key = {f["key"]: f for f in r["findings"]}
    by_id = {str(t["damage_id"]): t for t in r["truths"]}
    ph = r.get("physical") or {}
    f_clusters = ph.get("finding_clusters") or [[f["key"]] for f in r["findings"]]
    cp = next((c for c in ph.get("cluster_pairs") or []
               if "+".join(sorted(c["damage_ids"])) == rec["gt_key"]), None)
    if cp is None:
        return {"finding_keys": [], "via": "no_cluster",
                "reason": "GT-Cluster nicht in results gefunden"}

    if meta.get("candidates") == "all":
        cand_idx = list(range(len(f_clusters)))
    else:
        cand_idx = list(cp.get("candidates") or [])
    if not cand_idx:
        return {"finding_keys": [], "via": "ai_rejected", "confidence": None,
                "triage": "not_duplicate", "reason": "keine Kandidaten in der Nähe"}

    rep = by_id.get(cp["damage_ids"][0], {})
    plate = strat.plate_key(r["plate"])
    content: list[dict] = []
    content.append(_text(
        "DATABASE DAMAGE (physical, possibly recorded multiple times):\n" + json.dumps({
            "part": rep.get("part"), "type": rep.get("damage_type"),
            "side": rep.get("side_attr"), "projection": rep.get("projection"),
            "segment": rep.get("segment"), "severity": rep.get("severity"),
            "records": len(cp["damage_ids"]),
        }, indent=2)))
    added = 0
    for did in cp["damage_ids"]:
        for p in gt_images(plate, did):
            if added >= meta.get("gt_images", 4):
                break
            img = _img_part(p)
            if img:
                content.append(_text("Database reference photo:"))
                content.append(img)
                added += 1
    if not added:
        content.append(_text("(No database reference photo available.)"))
    for num, ci in enumerate(cand_idx, start=1):
        keys = f_clusters[ci]
        f0 = by_key[keys[0]]
        content.append(_text(
            f"CANDIDATE {num} ({len(keys)} detection(s), e.g. {keys[0]}):\n" + json.dumps({
                "part": f0.get("part"), "type": f0.get("type"),
                "photo positions": [by_key[k].get("position") for k in keys],
            }, indent=2)))
        added_c = 0
        for k in keys:
            if added_c >= meta.get("cand_images", 2):
                break
            cu = by_key[k].get("closeup")
            img = _img_part(ROOT / cu) if cu else None
            if img:
                content.append(_text(f"Candidate {num} close-up:"))
                content.append(img)
                added_c += 1
        if not added_c:
            content.append(_text(f"(Candidate {num} has no image.)"))
    content.append(_text("Which candidate numbers show the SAME physical damage? "
                         "Output ONLY the JSON object."))

    if dry_run:
        n_img = sum(1 for c in content if c.get("type") == "image_url")
        print(f"    {rec['gt_key']}: {len(cand_idx)} Kandidaten, {n_img} Bilder (dry-run)")
        return None

    obj = _ai_json(llm_key, meta, content)
    time.sleep(AI_PAUSE_S)
    if obj is None:
        return {"finding_keys": [], "via": None, "reason": "KI-Call fehlgeschlagen"}
    nums, conf, triage, reason = _parse_choice(obj, len(cand_idx))
    keys = sorted({k for n in nums for k in f_clusters[cand_idx[n - 1]]})
    return {"finding_keys": keys, "via": "ai" if keys else "ai_rejected",
            "confidence": conf, "triage": triage, "reason": reason}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    if not args:
        sys.exit("Nutzung: run_strategy.py <strategie-name> [auto-filter …] [--dry-run] [--force]")
    name, car_filters = args[0], args[1:]

    metas = strat.load_strategies()
    meta = metas.get(name)
    if meta is None:
        sys.exit(f"Strategie {name!r} nicht gefunden. Vorhanden: {', '.join(metas) or '—'}")
    if meta.get("baseline"):
        sys.exit("v01 ist die Baseline aus dem Gold-Standard — kein Lauf nötig.")
    if not meta.get("prompt"):
        sys.exit(f"strategies/{name}/prompt.txt fehlt.")

    llm_key = _env("LLM_GW_API_KEY")
    if not llm_key and not dry_run:
        sys.exit("Kein LLM_GW_API_KEY in .env")

    records = strat.benchmark_records()
    by_car: dict[str, list[dict]] = {}
    for rec in records:
        if car_filters and not any(a in rec["checkin"] for a in car_filters):
            continue
        by_car.setdefault(rec["checkin"], []).append(rec)

    out_dir = strat.RUNS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    n_done = n_skip = n_fail = 0
    for checkin, recs in by_car.items():
        res_file = RESULTS / f"{checkin}.json"
        if not res_file.exists():
            print(f"WARN: {checkin} hat keine results-Datei — übersprungen")
            continue
        r = json.loads(res_file.read_text())
        out_file = out_dir / f"{checkin}.json"
        run = (json.loads(out_file.read_text()) if out_file.exists()
               else {"checkin": checkin, "plate": r["plate"], "strategy": name,
                     "proposals": {}})
        print(f"=== {checkin} ({len(recs)} Urteile) ===")
        for rec in recs:
            prev = run["proposals"].get(rec["gt_key"])
            if prev and prev.get("via") in ("ai", "ai_rejected") and not force:
                n_skip += 1
                continue
            p = judge_record(llm_key, meta, rec, r, dry_run)
            if p is None:            # dry-run
                continue
            run["proposals"][rec["gt_key"]] = p
            if p.get("via") in ("ai", "ai_rejected"):
                n_done += 1
                print(f"    {rec['gt_key']}: {p['via']} → {p['finding_keys'] or '—'}")
            else:
                n_fail += 1
                print(f"    {rec['gt_key']}: FEHLER ({p.get('reason')})")
            out_file.write_text(json.dumps(run, indent=2, ensure_ascii=False))

    if dry_run:
        total = sum(len(v) for v in by_car.values())
        print(f"\nDry-Run: {total} Urteile in {len(by_car)} Autos — keine API-Calls.")
        return
    print(f"\nFertig: {n_done} geurteilt, {n_skip} übersprungen (schon da), {n_fail} Fehler.")

    s = strat.score(records, strat.run_proposals(name))
    c = s["counts"]
    print(f"\nScore {name} vs. Gold ({s['covered']}/{s['total']} Urteile abgedeckt):")
    print(f"  exakt {c['confirmed']} · korrigiert {c['corrected']} · übersehen {c['human_added']}"
          f" · korrekt-leer {c['confirmed_empty']} · fälschlich {c['rejected']}")
    if s["accuracy"] is not None:
        print(f"  Gesamt-Genauigkeit {s['accuracy']:.0%} · "
              f"Präzision {s['precision']:.0%}" if s["precision"] is not None
              else f"  Gesamt-Genauigkeit {s['accuracy']:.0%}")


if __name__ == "__main__":
    main()
