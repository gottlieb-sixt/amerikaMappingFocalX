"""Cluster-basiertes Mapping: physische Schäden statt Einzelzeilen.

Drei Stufen, alle bildgestützt (Gemini via Sixt-Gateway):

  1. FINDING-CLUSTERING   FocalX meldet denselben Schaden oft mehrfach (eine
     Meldung pro Foto/Box). Vorgruppierung nach (Bauteil, Typ), dann prüft die
     KI anhand der Close-ups, welche Meldungen derselbe physische Schaden sind.
  2. GT-CLUSTERING        Die DB enthält Duplikate (der Einfahrt-Scanner legt
     unreparierte Schäden pro Miete neu an). Gleiche Vorgruppierung + KI-Prüfung
     über die DB-Fotos.
  3. CLUSTER-MAPPING      Jeder physische GT-Schaden wird gegen die passenden
     Finding-Cluster geurteilt (GT-Fotos vs. Close-ups nebeneinander).

Nichts wird gelöscht — Cluster sind Gruppierungen; alle Zeilen/Boxen bleiben
im Report erhalten. Fail-soft: ohne KI bleibt die Vorgruppierung bestehen und
das Mapping fällt auf die strenge Heuristik zurück.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .judge import MAX_TOKENS, MODEL, _img_part, _post_with_retry, _text
from .matcher import (Truth, candidates_per_truth, heuristic_confident,
                      norm_type)

ROOT = Path(__file__).resolve().parent.parent
GT_PHOTOS = ROOT / "data" / "gt_photos"

AI_PAUSE_S = 2.0          # Drossel zwischen KI-Calls (Gateway: max 10 parallel)

# Teile, deren Meldungen pro Foto-Position getrennt bleiben MÜSSEN: jedes
# TYRE_RIM-Foto zeigt ein anderes Rad — "wheel scratch" von zwei Rädern ist
# nie derselbe Schaden.
PER_POSITION_PARTS = ("wheel", "rim", "tyre", "tire")

PHOTO_CONTEXT = """\
Note about the reference photos: they come from different sources —
(a) smartphone close-ups of the damage,
(b) automated ENTRY-GATE SCANNER images: low-resolution frames with a striped
    zebra pattern (deflectometry); a colored box marks the damage position,
(c) occasionally a license-plate or whole-car identification shot that has
    NOTHING to do with the damage — ignore those completely."""


def _ai_json(llm_key: str, system: str, content: list[dict]) -> dict | None:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    })
    raw = _post_with_retry(llm_key, body)
    if raw is None:
        return None
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if isinstance(obj, list):
        obj = next((v for v in obj if isinstance(v, dict)), {})
    return obj if isinstance(obj, dict) else None


def _parse_groups(obj: dict | None, n: int) -> list[list[int]] | None:
    """{"groups": [[1,2],[3]]} → 0-basierte Index-Gruppen; None bei Murks."""
    if not obj:
        return None
    groups = obj.get("groups")
    if not isinstance(groups, list):
        return None
    seen, out = set(), []
    for g in groups:
        if not isinstance(g, list):
            return None
        idx = [i - 1 for i in g if isinstance(i, int) and 1 <= i <= n and (i - 1) not in seen]
        seen.update(idx)
        if idx:
            out.append(idx)
    for i in range(n):                     # vergessene Einträge als Singletons
        if i not in seen:
            out.append([i])
    return out


CLUSTER_FINDINGS_PROMPT = """\
You are grouping damage detections from an AI vehicle inspection. All the
numbered detections below were reported on the SAME car and carry the SAME part
label and damage type — but they may stem from different photos of the same
physical damage, or they may be genuinely different damages on that part.

Look at the close-up images: group detections that show the SAME physical
damage (same marks/shape/spot, possibly from different angles or distances).
Keep genuinely different damages in separate groups. Detections whose photos
were taken at DIFFERENT wheels/corners of the car are always different damages.

Output ONLY JSON: {"groups": [[<detection numbers of group 1>], [<group 2>], …]}
Every detection number must appear in exactly one group."""


def cluster_findings(llm_key: str, findings_meta: list[dict],
                     closeup_path: dict[str, Path]) -> list[list[str]]:
    """findings_meta: [{key, part, type, position, orientation}] →
    Liste von Clustern (Listen von finding keys)."""
    pre: dict[tuple, list[dict]] = {}
    for f in findings_meta:
        part = (f.get("part") or "?").strip().lower()
        gk = (part, norm_type(f.get("type")))
        if any(w in part for w in PER_POSITION_PARTS):
            gk = gk + ((f.get("position") or "?"),)   # Räder: pro Foto-Position
        pre.setdefault(gk, []).append(f)

    clusters: list[list[str]] = []
    for group in pre.values():
        if len(group) == 1:
            clusters.append([group[0]["key"]])
            continue
        content: list[dict] = []
        for i, f in enumerate(group, start=1):
            content.append(_text(
                f"DETECTION {i}: part={f.get('part')}, type={f.get('type')}, "
                f"photo position={f.get('position')}"))
            img = _img_part(closeup_path.get(f["key"]))
            if img:
                content.append(_text(f"Detection {i} close-up:"))
                content.append(img)
            else:
                content.append(_text(f"(Detection {i} has no image.)"))
        content.append(_text("Group the detections by physical damage. Output ONLY the JSON object."))
        obj = _ai_json(llm_key, CLUSTER_FINDINGS_PROMPT, content)
        time.sleep(AI_PAUSE_S)
        groups = _parse_groups(obj, len(group))
        if groups is None:
            # Fail-soft: Vorgruppe als EIN Cluster behalten (optimistisch —
            # verhindert aufgeblähte Extras; Risiko: verschmilzt im Zweifel).
            clusters.append([f["key"] for f in group])
        else:
            for idx in groups:
                clusters.append([group[i]["key"] for i in idx])
    return clusters


CLUSTER_TRUTHS_PROMPT = """\
You are deduplicating a vehicle-damage database. The numbered records below
belong to the SAME car and have the same part, type and side. Rental-return
scanners often re-record the SAME unrepaired damage at every rental — so some
records may be duplicates of one physical damage, while others are genuinely
different damages on the same part.

""" + PHOTO_CONTEXT + """

Compare the reference photos: group records that show the SAME physical damage.

Output ONLY JSON: {"groups": [[<record numbers of group 1>], [<group 2>], …]}
Every record number must appear in exactly one group."""


def cluster_truths(llm_key: str, truths: list[Truth], gt_key: str) -> list[list[str]]:
    """→ Liste von Clustern (Listen von damage_ids)."""
    pre: dict[tuple, list[Truth]] = {}
    for t in truths:
        gk = ((t.part or "?").strip().lower(), norm_type(t.damage_type),
              (t.side_attr or "?").strip().lower(), (t.projection or "?"))
        pre.setdefault(gk, []).append(t)

    clusters: list[list[str]] = []
    for group in pre.values():
        if len(group) == 1:
            clusters.append([group[0].damage_id])
            continue
        content: list[dict] = []
        for i, t in enumerate(group, start=1):
            content.append(_text(
                f"RECORD {i}: part={t.part}, type={t.damage_type}, side={t.side_attr}, "
                f"segment={t.segment}, severity={t.severity}, case={t.case_number}"))
            added = 0
            for p in gt_images(gt_key, t.damage_id)[:2]:
                img = _img_part(p)
                if img:
                    content.append(_text(f"Record {i} reference photo:"))
                    content.append(img)
                    added += 1
            if not added:
                content.append(_text(f"(Record {i} has no accessible photo.)"))
        content.append(_text("Group the records by physical damage. Output ONLY the JSON object."))
        obj = _ai_json(llm_key, CLUSTER_TRUTHS_PROMPT, content)
        time.sleep(AI_PAUSE_S)
        groups = _parse_groups(obj, len(group))
        if groups is None:
            clusters.append([t.damage_id for t in group])
        else:
            for idx in groups:
                clusters.append([group[i].damage_id for i in idx])
    return clusters


MAP_PROMPT = """\
You are a meticulous vehicle-damage adjudicator. You get ONE physical damage
recorded in the fleet database (possibly recorded multiple times — all its
records and photos are shown), and a numbered list of candidate damages that an
AI inspection (FocalX) reported on the same car (each candidate may consist of
several detections of the same damage, with their close-up images).

Select EVERY candidate that shows the SAME physical damage as the database
damage. Use the IMAGES as primary evidence.

""" + PHOTO_CONTEXT + """

Decision standard (PANEL level, not pixel level):
- A candidate MATCHES when it shows the same TYPE of damage on the same PANEL
  and SIDE of the car as the database damage. The reference photos are often
  old, taken from other angles/lighting, or low-quality scanner frames — do NOT
  require the marks to look pixel-identical.
- Only exclude a candidate when the images CLEARLY prove it is a different
  damage: a different panel/side, or both damages visible as distinct marks in
  the same photo, or an obviously different damage category.
- Allow naming differences and type synonyms (scuff ≈ scratch, curb rash ≈ rim
  scratch, chip ≈ stone chip). Adjacent parts CAN be the same damage.
- If no candidate matches, return an empty list.

Output ONLY JSON:
{"matches": [<candidate numbers>], "confidence": 0.0-1.0,
 "reason": "<one sentence citing the visual evidence>"}"""


def gt_images(key: str, damage_id: str) -> list[Path]:
    return sorted((GT_PHOTOS / key).glob(f"{damage_id}_*.jpg"))


def run_mapping(llm_key: str, findings_meta: list[dict], truths: list[Truth],
                gt_key: str, closeup_path: dict[str, Path],
                log=lambda m: None) -> dict:
    """Komplettes Cluster-Mapping. Rückgabe enthält Zeilen- UND Cluster-Sicht."""
    by_key = {f["key"]: f for f in findings_meta}
    by_id = {t.damage_id: t for t in truths}

    log("Clustering FocalX-Findings …")
    f_clusters = cluster_findings(llm_key, findings_meta, closeup_path)
    cluster_of_key = {k: ci for ci, keys in enumerate(f_clusters) for k in keys}
    log(f"  {len(findings_meta)} Findings → {len(f_clusters)} physische Funde")

    log("Clustering Ground Truth …")
    t_clusters = cluster_truths(llm_key, truths, gt_key)
    log(f"  {len(truths)} DB-Zeilen → {len(t_clusters)} physische Schäden")

    # Kandidaten je GT-Zeile → auf Cluster-Ebene heben.
    cand_rows = candidates_per_truth(
        [(f["key"], f["position"], f["part"], f["type"]) for f in findings_meta], truths)

    matched_fc: dict[int, list[int]] = {}      # gt_cluster_idx → [finding_cluster_idx]
    cluster_pairs = []
    for gi, dmg_ids in enumerate(t_clusters):
        rep = by_id[dmg_ids[0]]
        # Kandidaten-Cluster = alle Cluster, die einen Kandidaten irgendeines Members enthalten
        cand_cluster_idx: list[int] = []
        for did in dmg_ids:
            for k, _s in cand_rows.get(did, []):
                ci = cluster_of_key[k]
                if ci not in cand_cluster_idx:
                    cand_cluster_idx.append(ci)
        if not cand_cluster_idx:
            cluster_pairs.append({"gt_cluster": gi, "damage_ids": dmg_ids, "finding_clusters": [],
                                  "via": None, "confidence": None,
                                  "reason": "keine Kandidaten in der Nähe", "candidates": []})
            continue

        # KI-Call: GT-Fotos (über alle Member gesammelt) vs. Kandidaten-Cluster.
        content: list[dict] = []
        content.append(_text("DATABASE DAMAGE (physical, possibly recorded multiple times):\n" + json.dumps({
            "part": rep.part, "type": rep.damage_type, "side": rep.side_attr,
            "projection": rep.projection, "segment": rep.segment,
            "severity": rep.severity, "records": len(dmg_ids),
        }, indent=2)))
        added = 0
        for did in dmg_ids:
            for p in gt_images(gt_key, did):
                if added >= 4:
                    break
                img = _img_part(p)
                if img:
                    content.append(_text("Database reference photo:"))
                    content.append(img)
                    added += 1
        if not added:
            content.append(_text("(No database reference photo available.)"))
        for num, ci in enumerate(cand_cluster_idx, start=1):
            keys = f_clusters[ci]
            f0 = by_key[keys[0]]
            content.append(_text(
                f"CANDIDATE {num} ({len(keys)} detection(s), e.g. {keys[0]}):\n" + json.dumps({
                    "part": f0.get("part"), "type": f0.get("type"),
                    "photo positions": [by_key[k].get("position") for k in keys],
                }, indent=2)))
            added_c = 0
            for k in keys:
                if added_c >= 2:
                    break
                img = _img_part(closeup_path.get(k))
                if img:
                    content.append(_text(f"Candidate {num} close-up:"))
                    content.append(img)
                    added_c += 1
            if not added_c:
                content.append(_text(f"(Candidate {num} has no image.)"))
        content.append(_text("Which candidate numbers show the SAME physical damage? Output ONLY the JSON object."))

        obj = _ai_json(llm_key, MAP_PROMPT, content)
        time.sleep(AI_PAUSE_S)

        chosen_ci, via, conf, reason = [], None, None, ""
        if obj is None:
            # Strenger Heuristik-Fallback auf dem besten Kandidaten-Cluster.
            best_ci = cand_cluster_idx[0]
            bf = by_key[f_clusters[best_ci][0]]
            if heuristic_confident(bf["position"], bf["part"], bf["type"], rep):
                chosen_ci, via = [best_ci], "heuristic"
                reason = "Heuristik: Seite+Typ+Bauteil (KI n/a)"
        else:
            nums = obj.get("matches", obj.get("match"))
            nums = [nums] if isinstance(nums, int) else (nums or [])
            chosen_ci = [cand_cluster_idx[n - 1] for n in nums
                         if isinstance(n, int) and 1 <= n <= len(cand_cluster_idx)]
            via = "ai" if chosen_ci else "ai_rejected"
            conf, reason = obj.get("confidence"), obj.get("reason", "")
        if chosen_ci:
            matched_fc[gi] = chosen_ci
        cluster_pairs.append({"gt_cluster": gi, "damage_ids": dmg_ids,
                              "finding_clusters": chosen_ci, "via": via,
                              "confidence": conf, "reason": reason,
                              "candidates": cand_cluster_idx})

    # Zeilen-Sicht ableiten (Dashboard-kompatibel).
    found_rows, pairs = [], []
    for cp in cluster_pairs:
        keys = [k for ci in cp["finding_clusters"] for k in f_clusters[ci]]
        for did in cp["damage_ids"]:
            if keys:
                found_rows.append(did)
            pairs.append({"damage_id": did, "findings": keys, "via": cp["via"],
                          "confidence": cp["confidence"], "reason": cp["reason"],
                          "candidates": [k for ci in cp["candidates"] for k in f_clusters[ci]],
                          "gt_cluster": cp["gt_cluster"],
                          "gt_cluster_size": len(cp["damage_ids"])})

    matched_cluster_keys = {k for cis in matched_fc.values() for ci in cis for k in f_clusters[ci]}
    all_keys = [f["key"] for f in findings_meta]
    extra_keys = [k for k in all_keys if k not in matched_cluster_keys]
    matched_ci_set = {ci for cis in matched_fc.values() for ci in cis}
    extra_clusters = [keys for ci, keys in enumerate(f_clusters) if ci not in matched_ci_set]

    return {
        "found": sorted(set(found_rows)),
        "missed": sorted(t.damage_id for t in truths if t.damage_id not in set(found_rows)),
        "extra_findings": sorted(extra_keys),
        "recall": round(len(set(found_rows)) / len(truths), 3) if truths else None,
        "pairs": pairs,
        "physical": {
            "gt_clusters": t_clusters,
            "finding_clusters": f_clusters,
            "gt_total": len(t_clusters),
            "gt_found": len(matched_fc),
            "recall": round(len(matched_fc) / len(t_clusters), 3) if t_clusters else None,
            "extras_unique": len(extra_clusters),
            "extra_clusters": extra_clusters,
            "cluster_pairs": cluster_pairs,
        },
    }
