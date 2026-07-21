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
import streamlit.components.v1 as components

import gallery

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

GREEN, RED, ORANGE, BLUE = "#2e9e5b", "#d0433b", "#e8802a", "#3479c4"


def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate).upper()


def gt_images(key: str, damage_id: str) -> list[Path]:
    return sorted((GT_PHOTOS / key).glob(f"{damage_id}_*.jpg"))


def gt_block(key: str, tid: str, t: dict, accent: str) -> str:
    info = gallery.info_table(
        f"DB-Schaden #{tid}", (t.get("damage_type") or "?").upper(),
        [("Bauteil", t.get("part")), ("Seite", t.get("side_attr")),
         ("Schwere", t.get("severity")),
         ("Ansicht/Segment", f"{t.get('projection') or '–'} / {t.get('segment') or '–'}"),
         ("Fall-Nr.", t.get("case_number"))],
        accent,
    )
    imgs = gt_images(key, tid)
    row = gallery.imgrow(*[gallery.thumb(p, f"#{tid} · {i + 1}") for i, p in enumerate(imgs)])
    if not imgs:
        row = gallery.note("📷 DB-Foto gesperrt oder nicht geladen")
    return gallery.column(info, row)


def ai_block(f: dict, accent: str, note_text: str = "") -> str:
    info = gallery.info_table(
        f"AI-Fund {f['key']}", (f.get("type") or "?").upper(),
        [("Bauteil (AI)", f.get("part")), ("Aufnahme-Position", f.get("position")),
         ("Ansicht (AI)", f.get("orientation"))],
        accent,
    )
    closeup = ROOT / f["closeup"] if f.get("closeup") else None
    row = gallery.imgrow(gallery.thumb(closeup, f"{f['key']} · AI-Ausschnitt") if closeup and closeup.exists() else "")
    return gallery.column(info, row, gallery.note(note_text))


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
pair_by_truth = {p["damage_id"]: p for p in r["pairs"] if p.get("findings")}

category = st.radio(
    "Kategorie",
    [f"✅ Gefunden ({len(r['found'])})",
     f"❌ Nicht gefunden ({len(r['missed'])})",
     f"➕ Zusätzliche AI-Funde ({len(r['extra_findings'])})"],
    horizontal=True, label_visibility="collapsed",
)
st.caption("Klick auf ein Bild öffnet es groß — Mausrad zoomt, Ziehen verschiebt, Esc schließt.")

cards: list[str] = []
if category.startswith("✅"):
    for tid in r["found"]:
        t, p = truths.get(tid, {}), pair_by_truth.get(tid)
        fkeys = p.get("findings", []) if p else []
        if p and p.get("via") == "ai":
            nt = (f"🧠 KI-Match (Konfidenz {p.get('confidence', '–')}) — {p.get('reason', '')}"
                  + (f"  ·  {len(fkeys)} FocalX-Funde diesem Schaden zugeordnet"
                     if len(fkeys) > 1 else "")
                  + (f"  ·  aus {len(p.get('candidates', []))} Kandidaten"
                     if len(p.get("candidates", [])) > 1 else ""))
        else:
            nt = f"⚙️ {p.get('reason') if p else 'Heuristik-Match'}"
        ai_blocks = [ai_block(findings[k], BLUE, nt if i == 0 else "")
                     for i, k in enumerate(fkeys) if k in findings]
        cards.append(gallery.card(gt_block(key, tid, t, GREEN), *ai_blocks))
    if not r["found"]:
        st.warning("Kein DB-Schaden wurde gefunden.")
elif category.startswith("❌"):
    for tid in r["missed"]:
        cards.append(gallery.card(gt_block(key, tid, truths.get(tid, {}), RED)))
    if not r["missed"]:
        st.success("Alle DB-Schäden wurden gefunden.")
else:
    for k in r["extra_findings"]:
        cards.append(gallery.card(ai_block(findings.get(k, {}), ORANGE)))

components.html(gallery.render(cards), height=820, scrolling=True)
