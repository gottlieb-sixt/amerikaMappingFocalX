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
  python3 -u scripts/run_strategy.py v08-… --run fl500 --all-cars   # ohne Gold

Bereits geurteilte Paare werden übersprungen (Resume nach Abbruch/Rate-Limit).
Am Ende wird der Lauf gegen den Gold-Standard gescort.

`--all-cars` löst die Bindung an den Gold-Standard: die Arbeitsliste kommt dann
aus den SHARK-Wahrheiten der Result-Dateien des Runs. Damit lassen sich beliebig
viele neue Autos vormappen (Ergebnis = Vorschläge fürs Review, kein Score).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import canonical
from eval import pictogram
from eval import strategy as strat
from eval.judge import MAX_TOKENS, MODEL, _img_part, _post_with_retry, _text
from eval.mapping import AI_PAUSE_S, _loads_lenient, gt_images
from eval.matcher import part_class
from eval.pipeline import RESULTS, ROOT, _env


def _deterministic_candidates(f_clusters: list[list[str]], by_key: dict,
                              rep: dict) -> list[int]:
    """Kandidaten-Cluster über ein Gate auf NUR die Bauteilklasse.

    Seite und Schadenstyp bewusst NICHT im Gate: in den Gold-Daten widersprechen
    sich FocalX und DB bei echten Matches regelmäßig bei der Seite (~25 % der
    Links/Rechts-Fälle gespiegelt) und beim Typ (scratch↔dent, „scratched except
    glass"→other). Diese räumliche/visuelle Disambiguierung übernimmt der Judge
    mit Nahaufnahmen + Piktogramm-Marker."""
    tc = part_class(rep.get("part"))
    if tc is None:
        return []
    return [ci for ci, keys in enumerate(f_clusters)
            if part_class(by_key.get(keys[0], {}).get("part")) == tc]


def _canonical_candidates(f_clusters: list[list[str]], by_key: dict, rep: dict,
                          damage_ids: list[str], plate: str,
                          meta: dict, fx_base: Path | None = None) -> list[dict]:
    """Piktogramm-Vorstufe: Cluster nach kanonischem Abstand zum DB-Ort filtern.

    Der DB-Punkt kommt deterministisch aus den SHARK-Koordinaten, die
    FocalX-Positionen aus dem eingefrorenen Lokalisierungs-Lauf
    (data/pictograms_llm_focalx). Mit canon_union_partclass=true kommen zusätzlich
    alle Cluster derselben Bauteilklasse dazu — die fängt die Fälle auf, in denen
    eine der beiden Seiten gar nicht verortet ist."""
    gt_pt = canonical.gt_point(plate, damage_ids, rep.get("projection"))
    fx_by_key = {k: r for r in canonical.fx_records(plate, base=fx_base)
                 for k in r["keys"]}
    extra = (_deterministic_candidates(f_clusters, by_key, rep)
             if meta.get("canon_union_partclass") else ())
    return canonical.shortlist(
        gt_pt, f_clusters, fx_by_key, extra_indices=extra,
        tau=float(meta.get("canon_tau", canonical.TAU)),
        k=int(meta.get("canon_k", canonical.K)))


def _canonical_distances(f_clusters: list[list[str]], damage_ids: list[str],
                         plate: str, rep: dict,
                         fx_base: Path | None = None) -> dict[int, float | None]:
    """Abstand JEDES Clusters zum DB-Ort — Geometrie als Evidenz im Prompt statt
    als Filter. None heißt: dieses Cluster ist nicht auf dem Piktogramm verortet."""
    gt_pt = canonical.gt_point(plate, damage_ids, rep.get("projection"))
    fx_by_key = {k: r for r in canonical.fx_records(plate, base=fx_base)
                 for k in r["keys"]}
    d = {c["index"]: c["dist"]
         for c in canonical.rank_clusters(gt_pt, f_clusters, fx_by_key)}
    return {i: d.get(i) for i in range(len(f_clusters))}


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


# Positive Verdicts: "es ist derselbe Schaden". Sowohl das alte
# Deduplizierungs-Vokabular (duplicate) als auch Matching-Varianten
# (same damage / match / yes) zählen als Treffer — so können Strategien den
# Judge von "Duplikat?" auf "gleicher Schaden?" umframen, ohne dass der Parser
# bricht.
_MATCH_VERDICTS = {"duplicate", "same damage", "same_damage", "same",
                   "match", "matches", "yes"}


def _is_match_verdict(v) -> bool:
    return str(v or "").strip().lower() in _MATCH_VERDICTS


def _candidate_details(obj: dict, cand_idx: list[int], f_clusters: list[list[str]],
                       dist_by_idx: dict[int, float | None],
                       by_key: dict) -> list[dict]:
    """ALLE Per-Kandidat-Urteile mitschreiben, nicht nur die akzeptierten.

    Ohne das lässt sich nachträglich keine Vorschlagsliste und keine
    Confidence-Schwelle auswerten — man müsste für jede Frage neu laufen."""
    entries = {e.get("id"): e for e in (obj.get("candidates") or [])
               if isinstance(e, dict)}
    out = []
    for num, ci in enumerate(cand_idx, start=1):
        e = entries.get(num) or {}
        keys = sorted(f_clusters[ci])
        conf = str(e.get("confidence") or "").strip().lower() or None
        d = dist_by_idx.get(ci)
        out.append({"keys": keys, "part": by_key[keys[0]].get("part"),
                    "distance": round(d, 1) if d is not None else None,
                    "verdict": e.get("verdict"), "confidence": conf,
                    "match": _is_match_verdict(e.get("verdict")),
                    "reason": e.get("reason") or ""})
    return out


def _parse_ranking(obj: dict, cand_idx: list[int],
                   f_clusters: list[list[str]]) -> list[list[str]]:
    """Die vom Judge gewünschte Reihenfolge (wahrscheinlichster Kandidat zuerst),
    unabhängig vom Ja/Nein-Verdict — Grundlage für Vorschlagslisten im Review."""
    nums = obj.get("ranking") or []
    seen, out = set(), []
    for n in nums:
        if isinstance(n, int) and 1 <= n <= len(cand_idx) and n not in seen:
            seen.add(n)
            out.append(sorted(f_clusters[cand_idx[n - 1]]))
    return out


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
        if _is_match_verdict(e.get("verdict")):
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
               if isinstance(e, dict) and _is_match_verdict(e.get("verdict"))
               and e.get("reason")]
    reason = reasons[0] if reasons else obj.get("reason", "")
    return chosen, (str(conf) if conf is not None else None), triage, reason


def records_from_results(results_dir: Path, car_filters: list[str]) -> list[dict]:
    """Arbeitsliste ohne Gold-Standard: ein Urteil pro physischem DB-Schaden.

    Für neue Autos gibt es keine menschlichen Urteile, also auch keinen
    Benchmark. Die GT-Seite hängt aber nicht am Gold: sie steht als `truths` in
    der Result-Datei, gruppiert von scripts/cluster_truths.py. Die
    Ausschlussregeln (🔧 repariert, ⏰ nach dem Check-in erfasst) gelten
    unverändert weiter, damit die Zahlen später mit dem Benchmark vergleichbar
    bleiben. `gold_keys` bleibt leer — nichts zu scoren, nur vorzuschlagen."""
    out: list[dict] = []
    for f in sorted(results_dir.glob("*.json")):
        r = json.loads(f.read_text())
        if r.get("skipped"):
            continue
        if car_filters and not any(a in r["checkin"] for a in car_filters):
            continue
        truths = r.get("truths") or []
        if not truths:
            continue
        by_id = {str(t["damage_id"]): t for t in truths}
        clusters = (r.get("gt_clusters")
                    or (r.get("physical") or {}).get("gt_clusters")
                    or [[str(t["damage_id"])] for t in truths])
        auto = strat.auto_excluded_ids(strat.plate_key(r["plate"]), r["checkin"])
        for ids in clusters:
            ids = [str(i) for i in ids if str(i) in by_id]
            if not ids or all(i in auto for i in ids):
                continue
            rep = by_id[ids[0]]
            out.append({"checkin": r["checkin"], "plate": r["plate"],
                        "gt_key": "+".join(sorted(ids)), "damage_ids": ids,
                        "gold_keys": [], "part": rep.get("part"),
                        "type": rep.get("damage_type"), "side": rep.get("side_attr"),
                        "severity": rep.get("severity"), "v01_keys": []})
    return out


def judge_record(llm_key: str, meta: dict, rec: dict, r: dict,
                 dry_run: bool, fx_base: Path | None = None) -> dict | None:
    """Baut den Judge-Call für ein Benchmark-Urteil (eingefrorene Kandidaten)
    und gibt das Proposal-Dict zurück. dry_run → nur zählen, kein Call."""
    by_key = {f["key"]: f for f in r["findings"]}
    by_id = {str(t["damage_id"]): t for t in r["truths"]}
    ph = r.get("physical") or {}
    # Reihenfolge wie in locate_pictograms: die Cluster aus einem Mapping-Lauf,
    # sonst die matching-freien aus scripts/cluster_findings.py. Ohne den
    # Top-Level-Rückgriff wäre jedes Einzel-Finding ein eigener Kandidat — bei
    # frischen Runs (ohne `physical`) bläht das die Kandidatenliste auf.
    f_clusters = (ph.get("finding_clusters") or r.get("finding_clusters")
                  or [[f["key"]] for f in r["findings"]])
    cp = next((c for c in ph.get("cluster_pairs") or []
               if "+".join(sorted(c["damage_ids"])) == rec["gt_key"]), None)
    if cp is None:
        # Ohne gerechnetes Mapping (z. B. frischer FocalX-Run) fehlen die
        # cluster_pairs. Die GT-Seite hängt aber nicht am FocalX-Lauf: die
        # Schadens-Gruppierung steht im Gold-Standard. Für die Modi, die ihre
        # Kandidaten selbst bestimmen (all/canonical), reicht das.
        if meta.get("candidates") in ("all", "canonical") and any(
                str(d) in by_id for d in rec["damage_ids"]):
            cp = {"damage_ids": [str(d) for d in rec["damage_ids"]
                                 if str(d) in by_id], "candidates": []}
        else:
            return {"finding_keys": [], "via": "no_cluster",
                    "reason": "GT-Cluster nicht in results gefunden"}

    rep = by_id.get(cp["damage_ids"][0], {})
    plate = strat.plate_key(r["plate"])
    mode = meta.get("candidates")
    dist_by_idx: dict[int, float | None] = {}
    if mode == "all":
        cand_idx = list(range(len(f_clusters)))
    elif mode == "deterministic":
        cand_idx = _deterministic_candidates(f_clusters, by_key, rep)
    elif mode == "canonical":
        short = _canonical_candidates(f_clusters, by_key, rep,
                                      cp["damage_ids"], plate, meta, fx_base)
        cand_idx = [c["index"] for c in short]
        dist_by_idx = {c["index"]: c["dist"] for c in short}
        if not cand_idx:
            if dry_run:
                print(f"    {rec['gt_key']}: 0 Kandidaten → leer ohne KI (dry-run)")
                return None
            # Piktogramm-Vorstufe schließt alles aus → kein Judge-Call nötig.
            # via bleibt "ai_rejected", damit Scoring und Resume unverändert
            # funktionieren; "stage" macht diese Urteile identifizierbar.
            return {"finding_keys": [], "via": "ai_rejected", "stage": "geometry",
                    "confidence": None, "triage": "not_duplicate",
                    "n_candidates": 0, "seconds": 0.0,
                    "reason": "kein Kandidat in Reichweite (Piktogramm-Vorstufe)"}
    else:
        cand_idx = list(cp.get("candidates") or [])
    if not cand_idx:
        return {"finding_keys": [], "via": "ai_rejected", "confidence": None,
                "triage": "not_duplicate", "reason": "keine Kandidaten in der Nähe"}
    if mode != "canonical" and meta.get("canon_distances"):
        dist_by_idx = _canonical_distances(f_clusters, cp["damage_ids"], plate,
                                           rep, fx_base)

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
    if meta.get("pictogram"):
        pj = pictogram.render(plate, str(cp["damage_ids"][0]))
        pimg = _img_part(pj) if pj else None
        if pimg:
            content.append(_text(
                "Database damage LOCATION on the car schematic "
                "(RED CIRCLE = where this damage sits on the vehicle):"))
            content.append(pimg)
    for num, ci in enumerate(cand_idx, start=1):
        keys = f_clusters[ci]
        f0 = by_key[keys[0]]
        info = {"part": f0.get("part"), "type": f0.get("type"),
                "photo positions": [by_key[k].get("position") for k in keys]}
        if ci in dist_by_idx:
            d = dist_by_idx[ci]
            # Eine Evidenz neben Region und Form — bewusst kein Ranking-Hinweis.
            info["schematic_distance_pct"] = (round(d, 1) if d is not None
                                              else "no location estimate")
        content.append(_text(
            f"CANDIDATE {num} ({len(keys)} detection(s), e.g. {keys[0]}):\n"
            + json.dumps(info, indent=2)))
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

    # Reine Antwortzeit des Judge (ohne die Höflichkeitspause danach) — sie ist
    # die Kostengröße, die mit Kandidatenzahl und Bildmenge skaliert.
    t0 = time.monotonic()
    obj = _ai_json(llm_key, meta, content)
    secs = round(time.monotonic() - t0, 1)
    n_img = sum(1 for c in content if c.get("type") == "image_url")
    time.sleep(AI_PAUSE_S)
    if obj is None:
        return {"finding_keys": [], "via": None, "seconds": secs,
                "n_candidates": len(cand_idx), "n_images": n_img,
                "reason": "KI-Call fehlgeschlagen"}
    nums, conf, triage, reason = _parse_choice(obj, len(cand_idx))
    keys = sorted({k for n in nums for k in f_clusters[cand_idx[n - 1]]})
    return {"finding_keys": keys, "via": "ai" if keys else "ai_rejected",
            "confidence": conf, "triage": triage, "reason": reason,
            "seconds": secs, "n_candidates": len(cand_idx), "n_images": n_img,
            "candidates": _candidate_details(obj, cand_idx, f_clusters,
                                            dist_by_idx, by_key),
            "ranking": _parse_ranking(obj, cand_idx, f_clusters)}


def main() -> None:
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    force = "--force" in argv
    # --run <id>: gegen die Findings eines anderen FocalX-Detection-Runs urteilen.
    # Dessen Finding-Keys sind eigene, also müssen Piktogramm-Lokalisierungen UND
    # Ausgabeverzeichnis mitwandern — und gescort wird nur v1, weil der
    # Gold-Standard auf v1-Keys beruht.
    run_id = "v1"
    if "--run" in argv:
        run_id = argv[argv.index("--run") + 1]
        argv = [a for a in argv if a not in ("--run", run_id)]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        sys.exit("Nutzung: run_strategy.py <strategie-name> [auto-filter …] "
                 "[--run <id>] [--dry-run] [--force]")
    name, car_filters = args[0], args[1:]

    results_dir, fx_base, out_root = RESULTS, None, strat.RUNS
    if run_id != "v1":
        from eval import runs as runs_mod
        results_dir = runs_mod.results_dir(run_id)
        fx_base = results_dir.parent / "pictograms_llm_focalx"
        out_root = results_dir.parent / "strategies"
        print(f"Run {run_id}: Findings aus {results_dir.relative_to(ROOT)} · "
              f"Lokalisierungen {fx_base.relative_to(ROOT)}")
        if not fx_base.exists():
            sys.exit(f"{fx_base.relative_to(ROOT)} fehlt — erst "
                     f"scripts/locate_pictograms.py --source focalx --run {run_id}")

    metas = strat.load_strategies()
    meta = metas.get(name)
    if meta is None:
        sys.exit(f"Strategie {name!r} nicht gefunden. Vorhanden: {', '.join(metas) or '—'}")
    if meta.get("baseline"):
        sys.exit("v01 ist die Baseline aus dem Gold-Standard — kein Lauf nötig.")
    if not meta.get("prompt"):
        sys.exit(f"strategies/{name}/prompt.txt fehlt.")

    # Geometrie-Strategien brauchen Pillow. Ohne PIL gibt eval.pictogram
    # fail-soft None zurück: kein Piktogramm im Prompt, jeder Abstand None —
    # der Lauf meldet Erfolg und war doch eine andere Strategie. Im Pilotlauf
    # fl500 sind so 46 245 Abstände still leer geblieben. Lieber hart abbrechen.
    if (meta.get("pictogram") or meta.get("canon_distances")
            or meta.get("candidates") == "canonical"):
        try:
            import PIL                                        # noqa: F401
        except ImportError:
            sys.exit(f"{name} baut auf die Geometrie (pictogram / "
                     f"canon_distances), aber {sys.executable} hat kein "
                     f"Pillow — mit .venv/bin/python starten.")

    llm_key = _env("LLM_GW_API_KEY")
    if not llm_key and not dry_run:
        sys.exit("Kein LLM_GW_API_KEY in .env")

    all_cars = "--all-cars" in sys.argv
    if all_cars:
        records = records_from_results(results_dir, car_filters)
        print(f"Arbeitsliste aus {results_dir.relative_to(ROOT)} (ohne Gold): "
              f"{len(records)} DB-Schäden in "
              f"{len({r['checkin'] for r in records})} Autos")
    else:
        records = strat.benchmark_records()
    by_car: dict[str, list[dict]] = {}
    for rec in records:
        if car_filters and not any(a in rec["checkin"] for a in car_filters):
            continue
        by_car.setdefault(rec["checkin"], []).append(rec)

    out_dir = out_root / name
    out_dir.mkdir(parents=True, exist_ok=True)
    n_done = n_skip = n_fail = 0
    timings: list[tuple[str, float, int, int]] = []   # gt_key, s, Kandidaten, Bilder
    t_start = time.monotonic()
    for checkin, recs in by_car.items():
        res_file = results_dir / f"{checkin}.json"
        if not res_file.exists():
            print(f"WARN: {checkin} hat keine results-Datei — übersprungen")
            continue
        r = json.loads(res_file.read_text())
        out_file = out_dir / f"{checkin}.json"
        run = (json.loads(out_file.read_text()) if out_file.exists()
               else {"checkin": checkin, "plate": r["plate"], "strategy": name,
                     "proposals": {}})
        print(f"=== {checkin} ({len(recs)} Urteile) ===")
        t_car = time.monotonic()
        car_t: list[float] = []
        for rec in recs:
            prev = run["proposals"].get(rec["gt_key"])
            if prev and prev.get("via") in ("ai", "ai_rejected") and not force:
                n_skip += 1
                continue
            p = judge_record(llm_key, meta, rec, r, dry_run, fx_base)
            if p is None or dry_run:  # dry-run schreibt nie
                continue
            run["proposals"][rec["gt_key"]] = p
            secs = float(p.get("seconds") or 0.0)
            nc, ni = p.get("n_candidates"), p.get("n_images")
            cost = (f"{secs:5.1f} s · {nc} Kand." + (f", {ni} Bilder" if ni else "")
                    if p.get("stage") != "geometry" else "  0.0 s · Gate, kein Call")
            car_t.append(secs)
            timings.append((f"{checkin}/{rec['gt_key']}", secs, nc or 0, ni or 0))
            if p.get("via") in ("ai", "ai_rejected"):
                n_done += 1
                print(f"    {rec['gt_key']}: {p['via']} → "
                      f"{p['finding_keys'] or '—'}  [{cost}]")
            else:
                n_fail += 1
                print(f"    {rec['gt_key']}: FEHLER ({p.get('reason')})  [{cost}]")
            out_file.write_text(json.dumps(run, indent=2, ensure_ascii=False))
        if car_t:
            print(f"  ⏱ {checkin}: {time.monotonic()-t_car:.0f} s Wanduhr · "
                  f"{sum(car_t):.0f} s Judge über {len(car_t)} Urteile · "
                  f"Ø {sum(car_t)/len(car_t):.1f} s")

    if dry_run:
        total = sum(len(v) for v in by_car.values())
        print(f"\nDry-Run: {total} Urteile in {len(by_car)} Autos — keine API-Calls.")
        return
    wall = time.monotonic() - t_start
    print(f"\nFertig: {n_done} geurteilt, {n_skip} übersprungen (schon da), {n_fail} Fehler.")

    # ── Zeitbilanz ──────────────────────────────────────────────────────────
    calls = [t for t in timings if t[3]]            # nur echte Judge-Calls
    print(f"\n⏱ ZEIT — Wanduhr {wall/60:.1f} min ({wall:.0f} s) für "
          f"{len(timings)} Urteile")
    if calls:
        ts = sorted(t[1] for t in calls)
        tot = sum(ts)
        med = ts[len(ts)//2] if len(ts) % 2 else (ts[len(ts)//2-1]+ts[len(ts)//2])/2
        print(f"  Judge-Calls          {len(calls)} · {tot/60:.1f} min "
              f"({100*tot/wall:.0f} % der Wanduhr)")
        print(f"  pro Call             Ø {tot/len(calls):.1f} s · median {med:.1f} s "
              f"· min {ts[0]:.1f} s · max {ts[-1]:.1f} s")
        print(f"  ohne Call (Gate)     {len(timings)-len(calls)} Urteile, 0 s")
        print(f"  pro Urteil gesamt    Ø {wall/len(timings):.1f} s Wanduhr "
              f"(inkl. {AI_PAUSE_S} s Pause pro Call)")
        by_n: dict[int, list[float]] = {}
        for _k, s, nc, _ni in calls:
            by_n.setdefault(nc, []).append(s)
        print("  nach Kandidatenzahl: " + " · ".join(
            f"{nc} Kand. → Ø {sum(v)/len(v):.1f} s ({len(v)}×)"
            for nc, v in sorted(by_n.items())))
        slow = sorted(calls, key=lambda t: -t[1])[:3]
        print("  langsamste: " + " · ".join(f"{k} {s:.0f} s" for k, s, _n, _i in slow))

    if run_id != "v1" or all_cars:
        print(f"Kein Scoring: der Gold-Standard beruht auf v1-Finding-Keys, "
              f"{run_id} hat eigene. Vorschläge liegen in "
              f"{out_dir.relative_to(ROOT)}/.")
        return
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
