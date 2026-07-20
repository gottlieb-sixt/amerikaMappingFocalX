"""FocalX evaluation dashboard.

Run:  streamlit run dashboard/app.py
Reads every data/results/<PLATE>.json the pipeline produced.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "results"

st.set_page_config(page_title="FocalX Evaluation", page_icon="🚗", layout="wide")
st.title("🚗 FocalX Detection Evaluation")

reports = sorted(RESULTS.glob("*.json"))
if not reports:
    st.info("Noch keine Ergebnisse — erst `python -m eval.pipeline <PLATE>` laufen lassen.")
    st.stop()

data = [json.loads(p.read_text()) for p in reports]

# ── Overview ────────────────────────────────────────────────────────────────
st.header("Übersicht")
rows = []
for r in data:
    rows.append({
        "Kennzeichen": r["plate"],
        "Bilder": r["images"],
        "Schäden (DB)": r["ground_truth_total"],
        "Gefunden": len(r["found"]),
        "Nicht gefunden": len(r["missed"]),
        "Zusätzlich (AI)": len(r["extra_findings"]),
        "Recall": r["recall"],
        "Zeitpunkt": r["timestamp"],
    })
df = pd.DataFrame(rows)

total_gt = int(df["Schäden (DB)"].sum())
total_found = int(df["Gefunden"].sum())
c1, c2, c3, c4 = st.columns(4)
c1.metric("Autos ausgewertet", len(df))
c2.metric("Schäden in DB", total_gt)
c3.metric("Davon gefunden", total_found)
c4.metric("Gesamt-Recall", f"{total_found / total_gt:.0%}" if total_gt else "–")

st.dataframe(
    df.style.background_gradient(subset=["Recall"], cmap="RdYlGn", vmin=0, vmax=1),
    use_container_width=True, hide_index=True,
)

# ── Per-car detail ──────────────────────────────────────────────────────────
st.header("Detail pro Auto")
plate = st.selectbox("Kennzeichen", [r["plate"] for r in data])
r = next(x for x in data if x["plate"] == plate)
truths = {str(t["damage_id"]): t for t in r["truths"]}
findings = {f["key"]: f for f in r["findings"]}
pair_by_truth = {p["damage_id"]: p for p in r["pairs"]
                 if p["heuristic_matched"] or (p["judge"] or {}).get("same_damage")}

tab_found, tab_missed, tab_extra = st.tabs([
    f"✅ Gefunden ({len(r['found'])})",
    f"❌ Nicht gefunden ({len(r['missed'])})",
    f"➕ Zusätzliche AI-Funde ({len(r['extra_findings'])})",
])

with tab_found:
    for tid in r["found"]:
        t = truths.get(tid, {})
        p = pair_by_truth.get(tid)
        f = findings.get(p["finding"]) if p else None
        cols = st.columns([3, 3, 2])
        with cols[0]:
            st.markdown(f"**DB #{tid}** · {t.get('part')} · {t.get('damage_type')} "
                        f"· {t.get('side_attr')} ({t.get('severity') or '–'})")
        with cols[1]:
            if f:
                judge = (p.get("judge") or {})
                via = "LLM-Judge" if judge.get("same_damage") else f"Heuristik (Score {p['score']})"
                st.markdown(f"↔ **{f['key']}** · {f.get('part')} · {f.get('type')} "
                            f"· {f.get('position')} — _{via}_")
        with cols[2]:
            if f and f.get("closeup") and (ROOT / f["closeup"]).exists():
                st.image(str(ROOT / f["closeup"]), width=220)
        st.divider()

with tab_missed:
    for tid in r["missed"]:
        t = truths.get(tid, {})
        st.markdown(f"**DB #{tid}** · {t.get('part')} · {t.get('damage_type')} "
                    f"· {t.get('side_attr')} · {t.get('projection')}/{t.get('segment')} "
                    f"({t.get('severity') or '–'})")
    if not r["missed"]:
        st.success("Alle DB-Schäden wurden gefunden.")

with tab_extra:
    st.caption("Von FocalX gefunden, aber ohne Gegenstück in der DB — potenziell neue Schäden oder False Positives.")
    for key in r["extra_findings"]:
        f = findings.get(key, {})
        cols = st.columns([4, 2])
        with cols[0]:
            st.markdown(f"**{key}** · {f.get('part')} · {f.get('type')} · Ansicht: {f.get('position')}")
        with cols[1]:
            if f.get("closeup") and (ROOT / f["closeup"]).exists():
                st.image(str(ROOT / f["closeup"]), width=220)
        st.divider()
