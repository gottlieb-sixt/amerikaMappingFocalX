"""FocalX evaluation dashboard.

Run:  .venv/bin/streamlit run dashboard/app.py
Liest alle data/results/<CHECKIN>.json der Pipeline; Ground-Truth-Fotos aus
data/gt_photos/ (scripts/download_gt_photos.py --refetch).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "results"
GT_PHOTOS = ROOT / "data" / "gt_photos"

st.set_page_config(page_title="FocalX Evaluation", page_icon="🚗", layout="wide")
st.title("🚗 FocalX Detection Evaluation")

reports = sorted(RESULTS.glob("*.json"))
data = [json.loads(p.read_text()) for p in reports]
data = [d for d in data if not d.get("skipped")]
if not data:
    st.info("Noch keine Ergebnisse — erst `python -m eval.pipeline …` laufen lassen.")
    st.stop()


# ── Hilfen ──────────────────────────────────────────────────────────────────

def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate).upper()


def gt_images(key: str, damage_id: str) -> list[Path]:
    return sorted((GT_PHOTOS / key).glob(f"{damage_id}_*.jpg"))


def gt_info(t: dict, tid: str) -> None:
    """Strukturierter Info-Block eines DB-Schadens."""
    st.markdown(
        f"**DB-Schaden #{tid}** &nbsp; `{(t.get('damage_type') or '?').upper()}`\n\n"
        f"| | |\n|---|---|\n"
        f"| **Bauteil** | {t.get('part') or '–'} |\n"
        f"| **Seite** | {t.get('side_attr') or '–'} |\n"
        f"| **Schwere** | {t.get('severity') or '–'} |\n"
        f"| **Ansicht/Segment** | {t.get('projection') or '–'} / {t.get('segment') or '–'} |\n"
        f"| **Fall-Nr.** | {t.get('case_number') or '–'} |"
    )


def gt_photo_strip(key: str, tid: str, width: int = 230) -> None:
    imgs = gt_images(key, tid)
    if imgs:
        cols = st.columns(min(len(imgs), 3))
        for c, img in zip(cols, imgs[:3]):
            c.image(str(img), width=width)
    else:
        st.caption("📷 DB-Foto noch nicht geladen — `python3 scripts/download_gt_photos.py --refetch`")


def finding_info(f: dict) -> None:
    st.markdown(
        f"**AI-Fund {f['key']}** &nbsp; `{(f.get('type') or '?').upper()}`\n\n"
        f"| | |\n|---|---|\n"
        f"| **Bauteil (AI)** | {f.get('part') or '–'} |\n"
        f"| **Aufnahme-Position** | {f.get('position') or '–'} |\n"
        f"| **Ansicht (AI)** | {f.get('orientation') or '–'} |"
    )


# ── Übersicht ───────────────────────────────────────────────────────────────

st.header("Übersicht")
rows = [{
    "Kennzeichen": r["plate"],
    "Check-in": r["checkin"].split("__")[1],
    "Bilder": r["images"],
    "Schäden (DB)": r["ground_truth_total"],
    "Gefunden": len(r["found"]),
    "Nicht gefunden": len(r["missed"]),
    "Zusätzlich (AI)": len(r["extra_findings"]),
    "Recall": r["recall"],
    "Zeitpunkt": r["timestamp"],
} for r in data]
df = pd.DataFrame(rows)

total_gt = int(df["Schäden (DB)"].sum())
total_found = int(df["Gefunden"].sum())
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Check-ins", len(df))
c2.metric("Schäden in DB", total_gt)
c3.metric("Gefunden", total_found)
c4.metric("Zusätzliche AI-Funde", int(df["Zusätzlich (AI)"].sum()))
c5.metric("Gesamt-Recall", f"{total_found / total_gt:.0%}" if total_gt else "–")

st.dataframe(
    df.style.background_gradient(subset=["Recall"], cmap="RdYlGn", vmin=0, vmax=1),
    use_container_width=True, hide_index=True,
)

# ── Detail pro Check-in ─────────────────────────────────────────────────────

st.header("Detail")
sel = st.selectbox("Check-in", [r["checkin"] for r in data])
r = next(x for x in data if x["checkin"] == sel)
key = plate_key(r["plate"])
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
    st.caption("Links der Schaden aus der Datenbank (mit Original-Fotos), rechts was FocalX dazu gefunden hat.")
    for tid in r["found"]:
        t = truths.get(tid, {})
        p = pair_by_truth.get(tid)
        f = findings.get(p["finding"]) if p else None
        left, right = st.columns(2, gap="large")
        with left:
            gt_info(t, tid)
            gt_photo_strip(key, tid)
        with right:
            if f:
                finding_info(f)
                if f.get("closeup") and (ROOT / f["closeup"]).exists():
                    st.image(str(ROOT / f["closeup"]), width=340)
                judge = (p.get("judge") or {})
                if judge:
                    st.markdown(f"🧠 **LLM-Judge:** {'✔ gleicher Schaden' if judge.get('same_damage') else '✘ abgelehnt'} "
                                f"(Konfidenz {judge.get('confidence', '–')}) — _{judge.get('reason', '')}_")
                else:
                    st.markdown(f"⚙️ **Heuristik-Match** (Score {p.get('score')})")
        st.divider()
    if not r["found"]:
        st.warning("Kein DB-Schaden wurde gefunden.")

with tab_missed:
    st.caption("Diese Schäden stehen in der Datenbank, FocalX hat sie NICHT gemeldet.")
    for tid in r["missed"]:
        t = truths.get(tid, {})
        left, right = st.columns(2, gap="large")
        with left:
            gt_info(t, tid)
        with right:
            gt_photo_strip(key, tid, width=260)
        st.divider()
    if not r["missed"]:
        st.success("Alle DB-Schäden wurden gefunden.")

with tab_extra:
    st.caption("Von FocalX gemeldet, ohne Gegenstück in der DB — potenziell neue Schäden oder False Positives.")
    for k in r["extra_findings"]:
        f = findings.get(k, {})
        left, right = st.columns(2, gap="large")
        with left:
            finding_info(f)
        with right:
            if f.get("closeup") and (ROOT / f["closeup"]).exists():
                st.image(str(ROOT / f["closeup"]), width=340)
            else:
                st.caption("Kein AI-Bildausschnitt verfügbar")
        st.divider()
