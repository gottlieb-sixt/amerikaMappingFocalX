"""FocalX evaluation dashboard.

Run:  .venv/bin/streamlit run dashboard/app.py

Drei Modi (Sidebar):
  Ergebnisse   — Übersicht + Detailkarten (zoombare Galerie)
  Review       — manuelles Mapping: GT oben, FocalX-Funde sortiert darunter;
                 AI-Vorschlag bestätigen (✓), korrigieren oder leer lassen.
                 Jede Entscheidung wird nach data/reviews/ geloggt.
  Metriken     — zwei getrennte Messgrößen aus den Reviews:
                 (1) FocalX-Detection (validierte Überschneidung mit GT)
                 (2) AI-Mapping-Qualität (bestätigt vs. korrigiert)
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import gallery

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval.matcher import Truth, score as match_score  # noqa: E402

from eval import runs as runs_mod  # noqa: E402

GT_PHOTOS = ROOT / "data" / "gt_photos"

st.set_page_config(page_title="FocalX Evaluation", page_icon="🚗", layout="wide")

# ── FocalX-Run wählen (versionierte Detection-Stände) ───────────────────────
# Ein Run = ein FocalX-Durchlauf mit eigenen Findings UND eigenen Reviews.
# Der Umschalter setzt RESULTS/REVIEWS für Review- und 📊-Seite; Gold-Standard
# und Strategien (🧠) bleiben immer an v1 gepinnt.
_run_opts = runs_mod.run_ids()
_active = runs_mod.active_run_id()
_run_id = st.sidebar.selectbox(
    "FocalX-Run", _run_opts,
    index=_run_opts.index(_active) if _active in _run_opts else 0,
    format_func=runs_mod.label, key="run_sel")
st.sidebar.caption("📊 & 🔍 folgen dem Run · 🧠 AI-Mapping/Gold bleibt an v1.")
RESULTS = runs_mod.results_dir(_run_id)
REVIEWS = runs_mod.reviews_dir(_run_id)
# Piktogramme liegen neben den Results des Runs — dieselbe Ableitung wie in
# scripts/locate_pictograms.py. Für v1 (results = data/results) ergibt das
# data/pictograms_llm_focalx. Ein fester Pfad zeigte hier v1-Bilder zu
# v3-Findings: gleiche Key-Namen (F4, F6 …), völlig andere Schäden.
FX_PICTOS = RESULTS.parent / "pictograms_llm_focalx"

reports = sorted(RESULTS.glob("*.json"))
data = [json.loads(p.read_text()) for p in reports]
data = [d for d in data if not d.get("skipped")]
if not data:
    st.info(f"Run **{runs_mod.label(_run_id)}**: noch keine FocalX-Ergebnisse. "
            f"Befüllen mit `python3 -m eval.pipeline --run {_run_id} --only-damaged` "
            f"— oder oben links einen anderen Run wählen.")
    st.stop()

MODES = ["📊 Ergebnisse", "📈 Massenauswertung", "🔍 Review / manuelles Mapping",
         "🧠 AI-Mapping", "🧩 Piktogramm-Mapping", "🔗 Gold-Vergleich",
         "📐 Kanonisch"]
# Navigation aus der Übersicht: VOR der Radio-Instanziierung verarbeiten
# (session_state eines gerenderten Widgets darf nicht mehr geändert werden).
if "nav_to_review" in st.session_state:
    st.session_state["mode_radio"] = next(m for m in MODES if m.startswith("🔍"))
    st.session_state["review_checkin_sel"] = st.session_state.pop("nav_to_review")
mode = st.sidebar.radio("Modus", MODES, key="mode_radio")

GREEN, RED, ORANGE, BLUE = "#2e9e5b", "#d0433b", "#e8802a", "#3479c4"


# ── Hilfen ──────────────────────────────────────────────────────────────────

def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate).upper()


SIZE_ORDER = ["≤ 0,5 Zoll", "≤ 1 Zoll", "> 1 Zoll", "< 2 Zoll", "2–4 Zoll",
              "> 4 Zoll", "komplett", "ohne Angabe"]
DEPTH_ORDER = ["Delle ohne Lackschaden", "Delle mit Lackschaden",
               "Kratzer oberflächlich", "Kratzer bis Grundierung",
               "komplett", "ohne Angabe"]


def size_bucket(sev: str | None) -> str:
    s = (sev or "").lower()
    if "0.5 inch" in s:
        return "≤ 0,5 Zoll"
    if "up to 1 inch" in s:
        return "≤ 1 Zoll"
    if "> 1 inch" in s:
        return "> 1 Zoll"
    if "< 2 inch" in s:
        return "< 2 Zoll"
    if "2-4 inch" in s:
        return "2–4 Zoll"
    if "> 4 inch" in s:
        return "> 4 Zoll"
    if "complete" in s:
        return "komplett"
    return "ohne Angabe"


def depth_bucket(sev: str | None) -> str:
    s = (sev or "").lower()
    if "without paint" in s:
        return "Delle ohne Lackschaden"
    if "with paint" in s:
        return "Delle mit Lackschaden"
    if "superficial" in s:
        return "Kratzer oberflächlich"
    if "down to primer" in s:
        return "Kratzer bis Grundierung"
    if "complete" in s:
        return "komplett"
    return "ohne Angabe"


def gt_images(key: str, damage_id: str) -> list[Path]:
    return sorted((GT_PHOTOS / key).glob(f"{damage_id}_*.jpg"))


@st.cache_data(show_spinner=False)
def _thumb_b64(path: str, max_w: int = 900) -> str:
    """Bild als kompaktes Base64-Data-URI (runterskaliert fürs Inline-Einbetten)."""
    import base64
    import io
    from PIL import Image
    im = Image.open(path)
    im = im.convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@st.cache_data(show_spinner=False)
def position_photo(checkin: str, focalx_pos: str) -> str | None:
    """Original-Check-in-Foto zu einem FocalX-Positionslabel (Fallback,
    wenn ein Finding kein Close-up hat)."""
    from eval.pipeline import POSITION_MAP
    inv = {v: k for k, v in POSITION_MAP.items()}
    pos_name = inv.get(focalx_pos)
    dirs = sorted((ROOT / "data" / "raw").glob(f"*/{checkin}"))
    if not pos_name or not dirs:
        return None
    f = dirs[0] / f"{pos_name}.jpg"
    return str(f) if f.exists() else None


@st.cache_data(show_spinner=False)
def late_ids(key: str, checkin: str) -> set[str]:
    """Schadensnummern, deren Fall erst NACH dem Check-in-Tag angelegt wurde
    (Fall-Feld 34 = damage_created_at) — konnte auf den Fotos nicht zu sehen sein."""
    import datetime as _dt
    dirs = sorted((ROOT / "data" / "raw").glob(f"*/{checkin}"))
    f = ROOT / "data" / "ground_truth" / f"{key}.json"
    if not dirs or not f.exists():
        return set()
    try:
        cutoff = _dt.datetime.fromisoformat(
            dirs[0].parent.name + "T23:59:59+02:00").timestamp()
    except ValueError:
        return set()
    out: set[str] = set()
    d = json.loads(f.read_text())
    cases = d.get("2") or []
    if isinstance(cases, dict):
        cases = [cases]
    for c in cases:
        v = c.get("34") or c.get("32")
        try:
            ts = int(str(v.get("1"))) if isinstance(v, dict) else None
        except (TypeError, ValueError):
            ts = None
        if not ts or ts <= cutoff:
            continue
        dms = c.get("31") or []
        if isinstance(dms, dict):
            dms = [dms]
        for dm in dms:
            if isinstance(dm, dict):
                out.add(str(dm.get("3")))
    return out


@st.cache_data(show_spinner=False)
def source_map(key: str) -> dict[str, int]:
    """damage_number → source_system des Falls (Case-Feld 11; 10 = Damage Gate)."""
    f = ROOT / "data" / "ground_truth" / f"{key}.json"
    if not f.exists():
        return {}
    out: dict[str, int] = {}
    d = json.loads(f.read_text())
    cases = d.get("2") or []
    if isinstance(cases, dict):
        cases = [cases]
    for c in cases:
        try:
            src = int(str(c.get("11")))
        except (TypeError, ValueError):
            src = -1
        dms = c.get("31") or []
        if isinstance(dms, dict):
            dms = [dms]
        for dm in dms:
            if isinstance(dm, dict):
                out[str(dm.get("3"))] = src
    return out


@st.cache_data(show_spinner=False)
def repaired_ids(key: str) -> set[str]:
    """Schadensnummern, die laut SHARK schon repariert sind (GT-Feld 31 = 1).
    Reparierte Schäden sind nicht mehr am Auto → zählen nicht als FocalX-Miss."""
    f = ROOT / "data" / "ground_truth" / f"{key}.json"
    if not f.exists():
        return set()
    out: set[str] = set()
    d = json.loads(f.read_text())
    cases = d.get("2") or []
    if isinstance(cases, dict):
        cases = [cases]
    for c in cases:
        dms = c.get("31") or []
        if isinstance(dms, dict):
            dms = [dms]
        for dm in dms:
            if isinstance(dm, dict) and dm.get("31") == 1:
                out.add(str(dm.get("3")))
    return out


def finding_clusters_of(r: dict) -> list[list[str]]:
    ph = r.get("physical") or {}
    if ph.get("finding_clusters"):
        return ph["finding_clusters"]
    return [[f["key"]] for f in r["findings"]]


def gt_clusters_of(r: dict) -> list[list[str]]:
    ph = r.get("physical") or {}
    if ph.get("gt_clusters"):
        return ph["gt_clusters"]
    return [[str(t["damage_id"])] for t in r["truths"]]


def ai_choice_for(r: dict, dmg_ids: list[str]) -> list[str]:
    """AI-gematchte Finding-Keys für einen GT-Cluster (leer wenn keins/pending)."""
    return ai_info_for(r, dmg_ids)[0]


def ai_info_for(r: dict, dmg_ids: list[str]) -> tuple[list[str], str | None]:
    """(gematchte Keys, via) — via=None heißt: KI-Call fehlgeschlagen."""
    ph = r.get("physical") or {}
    fcl = finding_clusters_of(r)
    for cp in ph.get("cluster_pairs") or []:
        if set(cp["damage_ids"]) == set(dmg_ids):
            keys = sorted(k for ci in cp.get("finding_clusters", []) for k in fcl[ci])
            return keys, cp.get("via")
    return [], None


def ai_scan_done(r: dict) -> bool:
    """True = KI-Scan komplett: Mapping gelaufen und JEDES GT-Paar hat ein
    echtes KI-Urteil (kein Heuristik-Fallback, kein fehlgeschlagener Call)."""
    if r.get("mapping_pending"):
        return False
    ph = r.get("physical") or {}
    pairs = ph.get("cluster_pairs")
    if pairs is None:
        return False
    return all(cp.get("via") in ("ai", "ai_rejected") for cp in pairs)


META_KEY = "_meta"


def review_damages(rev: dict) -> dict:
    """Nur die Schadens-Einträge (ohne Meta-Flag)."""
    return {k: v for k, v in rev.items() if k != META_KEY}


def review_done(rev: dict) -> bool:
    return bool((rev.get(META_KEY) or {}).get("done"))


def set_review_done(checkin: str, done: bool) -> None:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    rev = load_review(checkin)
    rev[META_KEY] = {"done": done, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    review_file(checkin).write_text(json.dumps(rev, indent=2))


def review_file(checkin: str) -> Path:
    return REVIEWS / f"{checkin}.json"


def load_review(checkin: str) -> dict:
    f = review_file(checkin)
    return json.loads(f.read_text()) if f.exists() else {}


def save_review(checkin: str, gt_key: str, human: list[str], ai: list[str],
                ai_available: bool = True, exclude: bool = False,
                reason: str = "") -> None:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    rev = load_review(checkin)
    if exclude:
        rev[gt_key] = {"human": [], "ai": sorted(ai), "verdict": "excluded",
                       "reason": reason, "ai_available": ai_available,
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        review_file(checkin).write_text(json.dumps(rev, indent=2))
        return
    if not ai_available:
        verdict = "manual_only"       # Auto war ungemappt — zählt nicht gegen die AI
    elif set(human) == set(ai):
        verdict = "confirmed" if human else "confirmed_empty"
    elif not human:
        verdict = "rejected"          # AI hatte gematcht, Mensch sagt: kein Match
    elif not ai:
        verdict = "human_added"       # AI leer, Mensch hat gemappt
    else:
        verdict = "corrected"
    rev[gt_key] = {"human": sorted(human), "ai": sorted(ai), "verdict": verdict,
                   "ai_available": ai_available,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    review_file(checkin).write_text(json.dumps(rev, indent=2))


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


# ════════════════════════════════════════════════════════════════════════════
if mode.startswith("📊"):
    st.title("📊 Ergebnisse — FocalX Detection")
    st.caption("Detection-Analyse auf Basis deiner Reviews (Gold-Standard). "
               "Die Live-Zähler zu FocalX/AI stehen oben im Review-Modus.")
    rev_files = sorted(REVIEWS.glob("*.json")) if REVIEWS.exists() else []
    if not rev_files:
        st.info("Noch keine Reviews — erst im Review-Modus Schäden bestätigen/mappen.")
        st.stop()

    # ── Detection nach Größe & Schwere (validiert) ──────────────────────────
    st.header("Detection nach Größe & Schwere (validiert)")
    st.caption("Basis: nur ✔️-abgeschlossene Autos und ausschließlich dein "
               "menschliches Urteil. Ausgeschlossene Schäden (🚫 manuell, "
               "🔧 repariert, ⏰ zu spät erfasst) zählen nicht.")

    size_stat: dict[str, tuple[int, int]] = {}
    depth_stat: dict[str, tuple[int, int]] = {}
    cell_stat: dict[tuple[str, str], tuple[int, int]] = {}
    gate_stat: dict[tuple[str, str], tuple[int, int]] = {}   # (Größe, gate/other)
    basis_cars = basis_damages = 0
    for r in data:
        rev_all = load_review(r["checkin"])
        if not review_done(rev_all):
            continue
        basis_cars += 1
        auto = (repaired_ids(plate_key(r["plate"]))
                | late_ids(plate_key(r["plate"]), r["checkin"]))
        srcs = source_map(plate_key(r["plate"]))
        truths_r = {str(t["damage_id"]): t for t in r["truths"]}
        for gt_key, v in review_damages(rev_all).items():
            ids = gt_key.split("+")
            if all(d in auto for d in ids):
                continue
            if v.get("verdict") == "excluded":
                continue
            found = bool(v.get("human"))
            sev = (truths_r.get(ids[0]) or {}).get("severity")
            sb, db_ = size_bucket(sev), depth_bucket(sev)
            basis_damages += 1
            g, t_ = size_stat.get(sb, (0, 0)); size_stat[sb] = (g + found, t_ + 1)
            g, t_ = depth_stat.get(db_, (0, 0)); depth_stat[db_] = (g + found, t_ + 1)
            gk = "gate" if srcs.get(ids[0]) == 10 else "other"
            g, t_ = cell_stat.get((sb, db_, gk), (0, 0))
            cell_stat[(sb, db_, gk)] = (g + found, t_ + 1)
            g, t_ = gate_stat.get((sb, gk), (0, 0)); gate_stat[(sb, gk)] = (g + found, t_ + 1)

    st.caption(f"{basis_damages} validierte Schäden aus {basis_cars} Autos")

    def bucket_df(stat: dict, order: list[str], label: str) -> pd.DataFrame:
        return pd.DataFrame([
            {label: b, "Gefunden": stat[b][0], "Nicht gefunden": stat[b][1] - stat[b][0],
             "Gesamt": stat[b][1], "Recall": stat[b][0] / stat[b][1]}
            for b in order if b in stat])

    import matplotlib
    _cmap = matplotlib.colormaps["RdYlGn"]
    _MASTER = ["≤ 0,5 Zoll", "≤ 1 Zoll", "> 1 Zoll", "< 2 Zoll", "2–4 Zoll", "> 4 Zoll"]

    st.subheader("Matrix: Größe × Erfassungsquelle (kumuliert)")
    _src_cols = [("mit Damage Gate", {"gate", "other"}), ("ohne Damage Gate", {"other"})]
    _gsizes = [b for b in _MASTER if any(k[0] == b for k in gate_stat)]
    _g_rows = [f"≥ {b}" for b in _gsizes]
    gtext = pd.DataFrame("–", index=_g_rows, columns=[c for c, _ in _src_cols])
    grecall = pd.DataFrame(float("nan"), index=_g_rows, columns=[c for c, _ in _src_cols])
    for i, sb in enumerate(_gsizes):
        bigger = set(_gsizes[i:])
        for cname, gset in _src_cols:
            g = sum(v[0] for k, v in gate_stat.items()
                    if k[0] in bigger and k[1] in gset)
            t_ = sum(v[1] for k, v in gate_stat.items()
                     if k[0] in bigger and k[1] in gset)
            if t_:
                gtext.loc[f"≥ {sb}", cname] = f"{g}/{t_} ({g / t_:.0%})"
                grecall.loc[f"≥ {sb}", cname] = g / t_

    def _gbg(col: pd.Series) -> list[str]:
        out = []
        for i in col.index:
            v = grecall.loc[i, col.name]
            if pd.isna(v):
                out.append("color: #bbb")
            else:
                r_, g_, b_, _a = _cmap(v)
                out.append(f"background-color: rgba({int(r_ * 255)},{int(g_ * 255)},"
                           f"{int(b_ * 255)},0.55)")
        return out

    st.dataframe(gtext.style.apply(_gbg, axis=0), use_container_width=True)
    st.caption("Erfassungsquelle des DB-Schadens (Case-Feld source_system): "
               "**mit Damage Gate** = alle Schäden (inkl. automatischem Scan-Portal) · "
               "**ohne Damage Gate** = nur Agent-App & übrige Systeme (Scan-Portal "
               "ausgeschlossen) · Zeilen kumuliert nach Größe (≥ Zeile), alle Schadenstypen.")
    st.subheader("Matrix: Größe × Schwere (beidseitig kumuliert)")
    def _cum_matrix(sev_cols: list[tuple[str, set, set]], all_sizes: bool = False) -> None:
        all_sev = set().union(*[d for _, d, _src in sev_cols])
        sizes = (_MASTER if all_sizes else
                 [b for b in _MASTER
                  if any(k[0] == b and k[1] in all_sev for k in cell_stat)])
        rows_lbl = [f"≥ {b}" for b in sizes]
        text = pd.DataFrame("–", index=rows_lbl, columns=[c for c, _, _s in sev_cols])
        recall = pd.DataFrame(float("nan"), index=rows_lbl,
                              columns=[c for c, _, _s in sev_cols])
        for i, sb in enumerate(sizes):
            bigger = set(sizes[i:])
            for cname, dset, srcset in sev_cols:
                g = sum(v[0] for k, v in cell_stat.items()
                        if k[0] in bigger and k[1] in dset and k[2] in srcset)
                t_ = sum(v[1] for k, v in cell_stat.items()
                         if k[0] in bigger and k[1] in dset and k[2] in srcset)
                if t_:
                    text.loc[f"≥ {sb}", cname] = f"{g}/{t_} ({g / t_:.0%})"
                    recall.loc[f"≥ {sb}", cname] = g / t_

        def _bg(col: pd.Series) -> list[str]:
            out = []
            for i in col.index:
                v = recall.loc[i, col.name]
                if pd.isna(v):
                    out.append("color: #bbb")
                else:
                    r_, g_, b_, _ = _cmap(v)
                    out.append(f"background-color: rgba({int(r_ * 255)},"
                               f"{int(g_ * 255)},{int(b_ * 255)},0.55)")
            return out

        st.dataframe(text.style.apply(_bg, axis=0), use_container_width=True)

    ALLQ = {"gate", "other"}
    OHNE = {"other"}
    K_T = {"Kratzer bis Grundierung"}
    K_ALL = {"Kratzer oberflächlich", "Kratzer bis Grundierung"}
    D_ALL = {"Delle ohne Lackschaden", "Delle mit Lackschaden"}

    st.markdown("**Kratzer** — Größe × Tiefe, jeweils mit / ohne Damage Gate")
    _cum_matrix([
        ("alle Kratzer", K_ALL, ALLQ),
        ("alle Kratzer · ohne Gate", K_ALL, OHNE),
        ("tief", K_T, ALLQ),
        ("tief · ohne Gate", K_T, OHNE),
    ], all_sizes=True)

    st.markdown("**Delle** — Größe × Lackschaden, jeweils mit / ohne Damage Gate")
    _cum_matrix([
        ("alle Dellen", D_ALL, ALLQ),
        ("alle Dellen · ohne Gate", D_ALL, OHNE),
    ], all_sizes=True)
    st.caption("Zeilen kumuliert nach Größe (**≥ Zeile**, je Typ eigene Leiter), "
               "Spalten kumuliert nach Schwere (**inkl. leichterer**) · "
               "ohne Gate = nur menschlich erfasste Schäden · "
               "Zelle: gefunden/gesamt (Recall).")



elif mode.startswith("🔍"):
    st.title("🔍 Review / manuelles Mapping")

    # ── Live-Zähler über ALLE Reviews (aktualisiert sich mit jedem Speichern) ──
    _tot = _fx_ok = _ai_base = _ai_ok = _cars_done = 0
    if REVIEWS.exists():
        for _f in REVIEWS.glob("*.json"):
            _rev = json.loads(_f.read_text())
            if not review_done(_rev):
                continue                      # nur abgeschlossene Autos zählen
            _cars_done += 1
            for _v in review_damages(_rev).values():
                if _v.get("verdict") == "excluded":
                    continue
                _tot += 1
                if _v.get("human"):
                    _fx_ok += 1                       # FocalX hat den Schaden wirklich
                if _v.get("ai_available", True) and _v.get("verdict") != "manual_only":
                    _ai_base += 1
                    if _v.get("verdict") in ("confirmed", "confirmed_empty"):
                        _ai_ok += 1
    # Klickbare Kennzahlen: Klick schaltet zwischen Bruch und Prozent um.
    st.markdown("""<style>
    [class*="st-key-mtoggle_"] button {
        padding: 0 !important; border: none !important; background: none !important;
        color: inherit !important; min-height: 0 !important;
    }
    [class*="st-key-mtoggle_"] button p {
        font-size: 36px !important; font-weight: 400 !important;
        line-height: normal !important; padding-bottom: 4px;
    }
    [class*="st-key-mtoggle_"] button:hover { color: #e8802a !important; }
    </style>""", unsafe_allow_html=True)
    pct = st.session_state.get("pct_mode", False)

    def _fmt(ok, base):
        if pct:
            return f"{ok / base:.0%}" if base else "–"
        return f"{ok} / {base}"

    c0, c1, c2, c3 = st.columns(4)
    c0.metric("Abgeschlossene Autos", _cars_done,
              help="Nur Autos mit gesetztem ✔️-Haken zählen in die Statistik")
    with c1:
        st.caption("Richtige Mappings FocalX (validiert)")
        if st.button(_fmt(_fx_ok, _tot), key="mtoggle_fx",
                     help="Klick: Bruch ↔ Prozent"):
            st.session_state["pct_mode"] = not pct
            st.rerun()
    with c2:
        st.caption("Richtige Mappings durch AI")
        if st.button(_fmt(_ai_ok, _ai_base), key="mtoggle_ai",
                     help="Klick: Bruch ↔ Prozent"):
            st.session_state["pct_mode"] = not pct
            st.rerun()
    c3.metric("AI-Genauigkeit", f"{_ai_ok / _ai_base:.0%}" if _ai_base else "–")
    st.divider()

    st.caption("Pro DB-Schaden: AI-Vorschlag prüfen (✓ bestätigen), anderen Fund wählen "
               "oder leer lassen. Alles wird geloggt und speist die Metriken.")
    _by_checkin = {x["checkin"]: x for x in data}

    def _car_label(c: str) -> str:
        scan = "🟢" if ai_scan_done(_by_checkin[c]) else "🟡"
        tick = " ✔️" if review_done(load_review(c)) else ""
        return f"{scan} {c}{tick}"

    st.caption("🟢 = AI-Scan fertig → bereit zum Review & Abhaken · "
               "🟡 = AI-Scan läuft noch · ✔️ = Auto abgeschlossen")
    sel = st.selectbox("Check-in", [r["checkin"] for r in data],
                       key="review_checkin_sel", format_func=_car_label)
    r = _by_checkin[sel]
    repaired = repaired_ids(plate_key(r["plate"]))
    late = late_ids(plate_key(r["plate"]), r["checkin"])
    if not ai_scan_done(r):
        st.warning("🟡 Für dieses Auto läuft der AI-Scan noch — Vorschläge "
                   "können sich gleich noch ändern. Grüne Autos zuerst reviewen.")
    key = plate_key(r["plate"])
    truths = {str(t["damage_id"]): t for t in r["truths"]}
    findings = {f["key"]: f for f in r["findings"]}
    fcl = finding_clusters_of(r)

    # FocalX-Piktogramm-Lokalisierungen: Einzel-Key → Record (Kombi 'F4+F6' split).
    _fx_loc: dict[str, dict] = {}
    for _jf in sorted((FX_PICTOS / key).glob("*.json")):
        try:
            _rec = json.loads(_jf.read_text())
        except Exception:
            continue
        for _k in str(_rec.get("id") or "").split("+"):
            if _k.strip():
                _fx_loc[_k.strip()] = _rec
    gcl = gt_clusters_of(r)
    review = load_review(r["checkin"])

    _auto = repaired | late
    gcl_open = [ids for ids in gcl if not all(d in _auto for d in ids)]
    n_auto = len(gcl) - len(gcl_open)
    done = sum(1 for ids in gcl_open if "+".join(sorted(ids)) in review)
    pc1, pc2 = st.columns([4, 2])
    with pc1:
        st.progress(done / len(gcl_open) if gcl_open else 1.0,
                    text=f"{done}/{len(gcl_open)} Schäden reviewt"
                         + (f" · {n_auto} automatisch ausgeschlossen (🔧/⏰)" if n_auto else ""))
    with pc2:
        is_done = review_done(review)
        new_done = st.toggle("✔️ Mit diesem Auto durch — in Statistik aufnehmen",
                             value=is_done, key=f"done_{sel}")
        if new_done != is_done:
            set_review_done(r["checkin"], new_done)
            st.rerun()
    if done < len(gcl_open) and review_done(review):
        st.warning(f"⚠️ Als abgeschlossen markiert, aber erst {done}/{len(gcl_open)} Schäden reviewt.")

    # Alle Original-Fotos des Check-ins (die an FocalX gingen) — zum Selbst-Prüfen
    _photo_dirs = sorted((ROOT / "data" / "raw").glob(f"*/{sel}"))
    with st.expander("📷 Alle Check-in-Fotos des Autos (Original, wie an FocalX geschickt)"):
        _shots = sorted(_photo_dirs[0].glob("*.jpg")) if _photo_dirs else []
        if _shots:
            st.caption(f"{len(_shots)} Fotos · Klick aufs Bild: groß & zoombar "
                       "(Mausrad zoomt, Ziehen verschiebt, Esc schließt)")
            _thumbs = [gallery.thumb(pth, pth.stem, size=170) for pth in _shots]
            components.html(gallery.render([gallery.imgrow(*_thumbs)]),
                            height=620, scrolling=True)
        else:
            st.caption("Keine Roh-Fotos zu diesem Check-in gefunden.")

    # Sticky-Header: der gerade gescrollte Schaden bleibt oben sichtbar,
    # bis seine Kachel-Sektion endet (CSS auf st.container(key=…)).
    # Sticky per JS: findet die Marker in den GT-Karten, macht deren Container
    # sticky und schaltet overflow:hidden in der Eltern-Kette frei. Läuft im
    # Intervall, damit es Streamlits Re-Renders übersteht.
    components.html("""<script>
    const doc = window.parent.document;
    function stickify() {
      doc.querySelectorAll('.gt-sticky-marker').forEach(m => {
        let block = m.closest('div[data-testid="stVerticalBlock"]');
        // Sticky braucht Spielraum: nicht der Block selbst, sondern sein
        // Layout-Wrapper (dessen Parent = ganzer Schadens-Container) muss kleben.
        const par = block && block.parentElement;
        if (par && par.getAttribute('data-testid') === 'stLayoutWrapper') block = par;
        if (!block || block.dataset.stickified === '1') return;
        block.dataset.stickified = '1';
        Object.assign(block.style, {
          position: 'sticky', top: '3.4rem', zIndex: '999',
          background: '#ffffff', borderBottom: '2px solid #e8802a',
          boxShadow: '0 4px 10px rgba(0,0,0,.08)',
          padding: '0.3rem 0.5rem 0.4rem 0.5rem',
          borderRadius: '0 0 10px 10px'
        });
        let a = block.parentElement;
        while (a && a.tagName !== 'SECTION' && !(a.dataset && a.dataset.testid === 'stMain')) {
          const cs = getComputedStyle(a);
          if (cs.overflow !== 'visible' || cs.overflowY !== 'visible') {
            a.style.overflow = 'visible';
          }
          a = a.parentElement;
        }
      });
    }
    stickify();
    setInterval(stickify, 800);
    </script>""", height=0)

    for gi, dmg_ids in enumerate(gcl):
        gt_key = "+".join(sorted(dmg_ids))
        t = truths[dmg_ids[0]]
        rev = review.get(gt_key)
        ai_keys, ai_via = ai_info_for(r, dmg_ids)
        ai_avail = not r.get("mapping_pending")
        excluded = bool(rev and rev["verdict"] == "excluded")
        was_repaired = all(d in repaired for d in dmg_ids)
        was_late = not was_repaired and all(d in (repaired | late) for d in dmg_ids)
        if was_repaired or was_late:
            excluded = True   # zählt wie ausgeschlossen, nur automatisch
        status = ("🔧" if was_repaired
                  else "⏰" if was_late
                  else "🚫" if excluded
                  else "✅" if rev and rev["verdict"].startswith("confirmed")
                  else "✏️" if rev else "🟡")
        open_key = f"open_{sel}_{gt_key}"
        reviewed_row = rev is not None or excluded
        # Default: erledigte Karten zu, offene auf — manuell umschaltbar
        is_open = st.session_state.get(open_key, not reviewed_row)
        if not is_open:
            with st.container(border=True, key=f"dmgc_{sel}_{gi}"):
                cc = st.columns([11, 2])
                if was_repaired:
                    res = "🔧 repariert — automatisch ausgeschlossen"
                elif was_late:
                    res = "⏰ erst nach den Fotos erfasst — automatisch ausgeschlossen"
                elif rev and rev["verdict"] == "excluded":
                    res = f"🚫 ausgeschlossen ({rev.get('reason') or 'ohne Grund'})"
                elif rev and rev["human"]:
                    res = "gemappt: **" + ", ".join(rev["human"]) + "**"
                elif rev:
                    res = "kein Match"
                else:
                    res = "🟡 **noch offen**"
                cc[0].markdown(f"{status} **#{'+#'.join(dmg_ids)}** · {t['part']} · "
                               f"{t['damage_type']} · {t['side_attr']} — {res}")
                _lbl = "⤵️ aufklappen" if (was_repaired or was_late or rev is None) else "✏️ ändern"
                if cc[1].button(_lbl, key=f"edit_{sel}_{gt_key}",
                                use_container_width=True):
                    st.session_state[open_key] = True
                    st.rerun()
            continue

        if excluded:
            st.markdown(f"""<style>
            div[class*="st-key-dmg_{sel}_{gi}"] {{ opacity: 0.4; }}
            </style>""", unsafe_allow_html=True)
        elif rev is None:
            st.markdown(f"""<style>
            div[class*="st-key-dmg_{sel}_{gi}"] {{
                background: #fffbe8; border-radius: 12px;
                outline: 2px solid #f0d264; outline-offset: -2px;
            }}
            </style>""", unsafe_allow_html=True)

        with st.container(border=True, key=f"dmg_{sel}_{gi}"):
            # ── Sticky GT-Kopf: Infos + Fotos, bleibt beim Scrollen stehen ──
            with st.container(key=f"sticky_{sel}_{gi}"):
                st.markdown('<span class="gt-sticky-marker"></span>', unsafe_allow_html=True)
                head = st.columns([2.9, 3.6, 0.5])
                with head[2]:
                    if st.button("⤴️", key=f"close_{sel}_{gt_key}",
                                 help="Karte einklappen", use_container_width=True):
                        st.session_state[open_key] = False
                        st.rerun()
                with head[0]:
                    st.markdown(
                        f"### {status} #{'+#'.join(dmg_ids)} · {t['part']} · "
                        f"{t['damage_type']}\n"
                        f"{t['side_attr']} · {t.get('severity') or '–'} · "
                        f"{t.get('projection')}/{t.get('segment')}"
                        + (f" · {len(dmg_ids)} DB-Einträge" if len(dmg_ids) > 1 else ""))
                    if ai_keys:
                        st.markdown(f"🧠 **AI:** {', '.join(ai_keys)}"
                                    + (" _(Heuristik-Fallback)_" if ai_via == "heuristic" else ""))
                    elif not ai_avail:
                        st.markdown("🧠 **AI:** Mapping lief noch nicht")
                    elif ai_via is None or ai_via == "heuristic":
                        st.markdown("🧠 **AI:** ⚠️ kein Urteil — KI-Call fehlgeschlagen (Reparatur-Lauf aktiv)")
                    else:
                        st.markdown("🧠 **AI:** kein Match")
                    if was_repaired:
                        st.markdown("🔧 **Laut SHARK bereits repariert** — nicht mehr am "
                                    "Auto, zählt automatisch nicht in die Statistik")
                    elif was_late:
                        st.markdown("⏰ **Erst nach dem Check-in erfasst** — konnte auf "
                                    "den Fotos nicht zu sehen sein, zählt automatisch "
                                    "nicht in die Statistik")
                    elif excluded:
                        st.markdown(f"🚫 **Ausgeschlossen** — {rev.get('reason') or 'ohne Grund'} "
                                    f"(zählt nicht in die Statistik)")
                    elif rev:
                        st.markdown(f"📝 `{rev['verdict']}` → {', '.join(rev['human']) or 'kein Match'}")
                    # GT-Piktogramm mit deterministischer DB-Position — zeigt, WO
                    # der Schaden laut Datenbank sitzt (Kontrolle des Mappings).
                    try:
                        from eval import pictogram as _picto
                        _gt_png = _picto.render_gt(key, list(dmg_ids),
                                                   t.get("projection"),
                                                   f"rev_{sel}_{gt_key}")
                        if _gt_png:
                            st.image(str(_gt_png), width=260,
                                     caption=f"🟢 GT-Position · {t.get('projection')}/"
                                             f"{t.get('segment')}")
                        else:
                            st.caption("🟢 GT-Piktogramm: keine DB-Koordinaten")
                    except Exception:
                        pass
                with head[1]:
                    imgs = [pth for did in dmg_ids for pth in gt_images(key, did)][:4]
                    if imgs:
                        ic = st.columns(len(imgs))
                        for c, img in zip(ic, imgs):
                            c.image(str(img), use_container_width=True)
                    else:
                        st.caption("📷 kein DB-Foto")

            # ── ALLE FocalX-Funde als klickbare Kacheln, beste zuerst ──
            truth_obj = Truth(
                damage_id=dmg_ids[0], part=t["part"], damage_type=t["damage_type"],
                side_attr=t["side_attr"], projection=t["projection"],
                segment=t["segment"], severity=t.get("severity"))
            scored = []
            for ci, keys in enumerate(fcl):
                sc = max(match_score(findings[k]["position"], findings[k]["part"],
                                     findings[k]["type"], truth_obj) for k in keys)
                scored.append((sc, ci, keys))
            scored.sort(key=lambda x: (-x[0], x[1]))

            PER_ROW = 4
            for start_i in range(0, len(scored), PER_ROW):
                cols = st.columns(PER_ROW)
                for col, (sc, ci, keys) in zip(cols, scored[start_i:start_i + PER_ROW]):
                    f0 = findings[keys[0]]
                    is_ai = bool(set(keys) & set(ai_keys))
                    is_current = rev is not None and set(rev["human"]) == set(keys)
                    with col:
                        with st.container(border=True):
                            # ALLE Mitglieder des Clusters zeigen — eine falsche
                            # Gruppierung muss sichtbar sein, nicht versteckt.
                            imgs = []
                            for k in keys:
                                cu_ = (ROOT / findings[k]["closeup"]
                                       if findings[k].get("closeup") else None)
                                if cu_ and cu_.exists():
                                    imgs.append((k, cu_))
                                else:
                                    alt = position_photo(sel, findings[k]["position"])
                                    if alt:
                                        imgs.append((f"{k} · Originalfoto", Path(alt)))
                            if len(imgs) == 1:
                                st.image(str(imgs[0][1]), use_container_width=True)
                            elif imgs:
                                # Klick aufs Bild schaltet zum nächsten Cluster-Mitglied.
                                # Optik identisch zu st.image; Chip nur bei Hover.
                                _payload = json.dumps([
                                    {"k": k, "src": _thumb_b64(str(p_))}
                                    for k, p_ in imgs])
                                components.html(f"""
                                <body style="margin:0">
                                <div id="cyc" style="position:relative;cursor:pointer;
                                     padding-bottom:16px">
                                  <img id="im" style="width:100%;display:block;
                                       border-radius:0.5rem">
                                  <div id="chip" style="position:absolute;bottom:6px;
                                       right:6px;background:rgba(0,0,0,.65);color:#fff;
                                       padding:2px 8px;border-radius:10px;font-size:12px;
                                       font-family:sans-serif;opacity:0;
                                       transition:opacity .15s"></div>
                                </div>
                                <script>
                                  const imgs = {_payload}; let i = 0;
                                  const im = document.getElementById('im');
                                  const chip = document.getElementById('chip');
                                  const cyc = document.getElementById('cyc');
                                  function fit() {{
                                    if (window.frameElement)
                                      window.frameElement.style.height =
                                          document.body.scrollHeight + 'px';
                                  }}
                                  function show() {{
                                    im.src = imgs[i].src;
                                    chip.textContent = imgs[i].k + ' (' + (i+1) + '/'
                                        + imgs.length + ')';
                                  }}
                                  im.onload = fit;
                                  cyc.onclick = () => {{ i = (i+1) % imgs.length; show(); }};
                                  cyc.onmouseenter = () => chip.style.opacity = 1;
                                  cyc.onmouseleave = () => chip.style.opacity = 0;
                                  show();
                                </script></body>""", height=240)
                            # FocalX-Piktogramm mit AI-Box: WO FocalX den Schaden
                            # verortet — zum direkten Abgleich mit der GT-Position.
                            _lrec = next((_fx_loc[k] for k in keys if k in _fx_loc), None)
                            if _lrec and _lrec.get("box"):
                                try:
                                    from eval import pictogram as _picto
                                    _fp = _picto.render_llm(
                                        key, _lrec.get("projection"), _lrec["box"],
                                        f"revfx_{sel}_{gt_key}_{ci}",
                                        src_size=_lrec.get("pictogram_size"))
                                    if _fp:
                                        st.image(str(_fp), use_container_width=True,
                                                 caption=f"🟥 FocalX-Position · "
                                                         f"{_lrec.get('projection')}")
                                except Exception:
                                    pass
                            elif _lrec is not None:
                                st.caption("🟥 FocalX-Piktogramm: nicht lokalisierbar")
                            st.caption(f"**{'+'.join(keys)}** · {f0['part']} · {f0['type']}"
                                       + (" · 🧠 **AI-Vorschlag**" if is_ai else ""))
                            label = ("✅ Gewählt" if is_current
                                     else "✓ AI bestätigen" if is_ai else "Diesen mappen")
                            if st.button(label, key=f"pick_{sel}_{gt_key}_{ci}",
                                         use_container_width=True,
                                         type="primary" if is_ai and rev is None else "secondary",
                                         disabled=is_current):
                                save_review(r["checkin"], gt_key, list(keys), ai_keys,
                                            ai_available=ai_avail)
                                st.session_state[open_key] = False
                                st.rerun()
            none_current = (rev is not None and not rev["human"]
                            and rev["verdict"] != "excluded")
            bcols = st.columns([3, 2, 2])
            with bcols[0]:
                if st.button("✗ Kein Match — FocalX hat diesen Schaden nicht gefunden"
                             + (" (gewählt)" if none_current else ""),
                             key=f"none_{sel}_{gt_key}", disabled=none_current):
                    save_review(r["checkin"], gt_key, [], ai_keys, ai_available=ai_avail)
                    st.session_state[open_key] = False
                    st.rerun()
            with bcols[1]:
                excl_reason = st.text_input("Grund", key=f"exclreason_{sel}_{gt_key}",
                                            placeholder="Grund für Ausschluss…",
                                            label_visibility="collapsed",
                                            disabled=excluded)
            with bcols[2]:
                if not excluded:
                    if st.button("🚫 Aus Statistik ausschließen",
                                 key=f"excl_{sel}_{gt_key}", use_container_width=True):
                        save_review(r["checkin"], gt_key, [], ai_keys,
                                    ai_available=ai_avail, exclude=True,
                                    reason=excl_reason)
                        st.session_state[open_key] = False
                        st.rerun()
                else:
                    if st.button("↩️ Wieder aufnehmen",
                                 key=f"unexcl_{sel}_{gt_key}", use_container_width=True):
                        rev_all = load_review(r["checkin"])
                        rev_all.pop(gt_key, None)
                        review_file(r["checkin"]).write_text(json.dumps(rev_all, indent=2))
                        st.rerun()

# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
if mode.startswith("🧠"):
    from eval import strategy as strat

    st.title("🧠 AI-Mapping-Qualität")

    strategies = strat.load_strategies()
    bench = strat.benchmark_records()

    scored: dict[str, dict] = {}
    for _name, _meta in strategies.items():
        _props = strat.proposals_for(_name, _meta)
        if _props:
            scored[_name] = strat.score(bench, _props)

    st.caption(f"Benchmark: **{len(bench)} menschlich validierte Urteile** aus dem "
               "Gold-Standard (✔️-Autos, ohne 🚫/🔧/⏰-Ausschlüsse). Jede Strategie "
               "wird auf exakt denselben Urteilen gemessen — Cluster und Kandidaten "
               "sind aus v01 eingefroren, nur der Judge (Prompt / Modell / Parameter) "
               "variiert. Neue Strategie: Ordner unter `strategies/` anlegen, dann "
               "`python3 -u scripts/run_strategy.py <name>` starten.")

    if len(scored) > 1:
        st.subheader("Strategie-Vergleich")
        comp = []
        for _name, s_ in scored.items():
            c_ = s_["counts"]
            comp.append({
                "Strategie": strategies[_name].get("title", _name),
                "Abdeckung": f"{s_['covered']}/{s_['total']}",
                "✅ exakt": c_["confirmed"],
                "✏️ falsch gewählt": c_["corrected"],
                "➕ übersehen": c_["human_added"],
                "✅ korrekt leer": c_["confirmed_empty"],
                "✗ fälschlich": c_["rejected"],
                "Präzision": s_["precision"],
                "Genauigkeit": s_["accuracy"],
            })
        st.dataframe(pd.DataFrame(comp)
                     .style.format({"Präzision": "{:.0%}", "Genauigkeit": "{:.0%}"},
                                   na_rep="–")
                     .background_gradient(subset=["Genauigkeit"], cmap="RdYlGn",
                                          vmin=0, vmax=1),
                     use_container_width=True, hide_index=True)

    sel_strat = st.selectbox(
        "Strategie", list(scored),
        format_func=lambda n: strategies.get(n, {}).get("title", n))
    if sel_strat not in scored:
        st.info("Noch kein Strategie-Lauf vorhanden.")
        st.stop()
    sel_meta, s = strategies[sel_strat], scored[sel_strat]
    if sel_meta.get("description"):
        st.caption(sel_meta["description"])
    if s["missing"]:
        st.warning(f"Lauf unvollständig: erst {s['covered']}/{s['total']} "
                   "Benchmark-Urteile abgedeckt — alle Kennzahlen beziehen sich nur "
                   "auf die abgedeckten. Weiter mit "
                   f"`python3 -u scripts/run_strategy.py {sel_strat}`.")

    def _size_b(sev: str | None) -> str:
        t = (sev or "").lower()
        for pat, b in (("0.5 inch", "≤ 0,5 Zoll"), ("up to 1 inch", "≤ 1 Zoll"),
                       ("> 1 inch", "> 1 Zoll"), ("< 2 inch", "< 2 Zoll"),
                       ("2-4 inch", "2–4 Zoll"), ("> 4 inch", "> 4 Zoll")):
            if pat in t:
                return b
        return "ohne Angabe"

    counts = s["counts"]
    acc: dict[tuple[str, str], list[int]] = {}   # (Größe, gate/other) -> [ok, tot]
    _rows_car: dict[str, dict] = {}
    for rec in s["records"]:
        ok = rec["verdict"] in ("confirmed", "confirmed_empty")
        sb = _size_b(rec.get("severity"))
        srcs = source_map(plate_key(rec["plate"]))
        gk = "gate" if srcs.get(rec["damage_ids"][0]) == 10 else "other"
        a = acc.setdefault((sb, gk), [0, 0])
        a[0] += ok; a[1] += 1
        row = _rows_car.setdefault(rec["checkin"], {v: 0 for v in strat.VERDICTS})
        row[rec["verdict"]] += 1
    per_car = []
    for _ck, row in sorted(_rows_car.items()):
        n = sum(row.values())
        okc = row["confirmed"] + row["confirmed_empty"]
        per_car.append({
            "Check-in": _ck, "Urteile": n, "korrekt": okc,
            "✏️ korrigiert": row["corrected"], "✗ fälschlich": row["rejected"],
            "➕ übersehen": row["human_added"], "Genauigkeit": okc / n,
        })

    tot = s["covered"]
    ok_total = counts["confirmed"] + counts["confirmed_empty"]
    mappable, nonmap, prec = s["mappable"], s["nonmap"], s["precision"]

    st.subheader("Wie gehen die KI-Urteile aus?")
    import altair as alt

    def _stack(title: str, segs: list[tuple[str, int, str]],
               scale_max: int) -> None:
        total = sum(n for _, n, _c in segs)
        if not total:
            st.markdown(f"**{title}** — noch keine Urteile")
            return
        df = pd.DataFrame([{"Ausgang": f"{lbl} ({n})", "n": n, "farbe": col, "o": i,
                            "pct": f"{n / total:.0%}"}
                           for i, (lbl, n, col) in enumerate(segs) if n > 0])
        bars = alt.Chart(df).mark_bar(height=34, cornerRadius=3).encode(
            x=alt.X("n:Q", title=None, axis=None,
                    scale=alt.Scale(domain=[0, scale_max])),
            color=alt.Color("Ausgang:N",
                            sort=alt.SortField("o"),
                            scale=alt.Scale(domain=list(df["Ausgang"]),
                                            range=list(df["farbe"])),
                            legend=alt.Legend(orient="bottom", title=None,
                                              labelFontSize=13, symbolType="square")),
            order=alt.Order("o:Q"),
            tooltip=[alt.Tooltip("Ausgang:N"), alt.Tooltip("n:Q", title="Anzahl")],
        )
        labels = alt.Chart(df).mark_text(color="white", fontSize=13,
                                         fontWeight="bold", align="right",
                                         dx=-8, baseline="middle").encode(
            x=alt.X("n:Q", stack="zero", scale=alt.Scale(domain=[0, scale_max])),
            detail="Ausgang:N",
            order=alt.Order("o:Q"),
            text=alt.condition("datum.n >= 3", alt.Text("pct:N"), alt.value("")),
        )
        st.markdown(f"**{title}**")
        st.altair_chart((bars + labels).properties(height=44),
                        use_container_width=True)

    _scale_max = max(mappable, nonmap, 1)
    _stack(f"Match existiert ({mappable}) — kann die KI ihn finden?", [
        ("exakt richtig gemappt", counts["confirmed"], "#2e9e5b"),
        ("falsches Finding gewählt", counts["corrected"], "#e8c14d"),
        ("übersehen", counts["human_added"], "#e8802a"),
    ], _scale_max)
    _stack(f"Kein Match existiert ({nonmap}) — erkennt die KI das?", [
        ("korrekt: kein Match", counts["confirmed_empty"], "#8fd0a0"),
        ("fälschlich gemappt", counts["rejected"], "#d0433b"),
    ], _scale_max)
    _p = f"{prec:.0%}" if prec is not None else "–"
    _a = f"{ok_total / tot:.0%}" if tot else "–"
    st.caption(f"Präzision der KI-Matches: **{_p}** (wenn die KI mappte, war es so "
               f"oft exakt der richtige Fund) · Gesamt-Genauigkeit über alle {tot} "
               f"Urteile: **{_a}**.")

    st.subheader("Genauigkeit nach Größe × Erfassungsquelle (kumuliert)")
    import matplotlib as _mpl2
    _cm2 = _mpl2.colormaps["RdYlGn"]
    _ML = ["≤ 0,5 Zoll", "≤ 1 Zoll", "> 1 Zoll", "< 2 Zoll", "2–4 Zoll", "> 4 Zoll"]
    _gs = [b for b in _ML if any(k[0] == b for k in acc)]
    _cols = [("mit Damage Gate", {"gate", "other"}), ("ohne Damage Gate", {"other"})]
    atext = pd.DataFrame("–", index=[f"≥ {b}" for b in _gs],
                         columns=[c for c, _ in _cols])
    arec = pd.DataFrame(float("nan"), index=[f"≥ {b}" for b in _gs],
                        columns=[c for c, _ in _cols])
    for i, sb in enumerate(_gs):
        bigger = set(_gs[i:])
        for cname, gset in _cols:
            g = sum(v[0] for k, v in acc.items() if k[0] in bigger and k[1] in gset)
            t_ = sum(v[1] for k, v in acc.items() if k[0] in bigger and k[1] in gset)
            if t_:
                atext.loc[f"≥ {sb}", cname] = f"{g}/{t_} ({g / t_:.0%})"
                arec.loc[f"≥ {sb}", cname] = g / t_

    def _abg(col: pd.Series) -> list[str]:
        out = []
        for i in col.index:
            v = arec.loc[i, col.name]
            if pd.isna(v):
                out.append("color: #bbb")
            else:
                r_, g_, b_, _a = _cm2(v)
                out.append(f"background-color: rgba({int(r_ * 255)},{int(g_ * 255)},"
                           f"{int(b_ * 255)},0.55)")
        return out

    st.dataframe(atext.style.apply(_abg, axis=0), use_container_width=True)
    st.caption("Zeilen kumuliert (≥ Größe) · Zelle: exakt korrekte KI-Urteile / gesamt.")

    if per_car:
        with st.expander("Pro Auto (Detail)"):
            st.dataframe(pd.DataFrame(per_car)
                         .style.format({"Genauigkeit": "{:.0%}"})
                         .background_gradient(subset=["Genauigkeit"], cmap="RdYlGn",
                                              vmin=0, vmax=1),
                         use_container_width=True, hide_index=True)

    _LBL = {"corrected": "✏️ falsches Finding gewählt",
            "human_added": "➕ Match übersehen",
            "rejected": "✗ fälschlich gemappt"}
    _errs = [x for x in s["records"] if x["verdict"] in _LBL]
    with st.expander(f"Fehler im Detail ({len(_errs)}) — Futter fürs Prompt-Tuning"):
        if _errs:
            st.dataframe(pd.DataFrame([{
                "Check-in": x["checkin"],
                "Schaden": "#" + "+#".join(x["damage_ids"]),
                "Bauteil": x.get("part"), "Typ": x.get("type"),
                "Größe": _size_b(x.get("severity")),
                "Ausgang": _LBL[x["verdict"]],
                "KI wählte": ", ".join(x["proposal_keys"]) or "—",
                "Gold (Mensch)": ", ".join(x["gold_keys"]) or "—",
            } for x in _errs]), use_container_width=True, hide_index=True)
        else:
            st.write("Keine Fehler — perfekter Lauf.")


# ══ 🧩 Piktogramm-Mapping: LLM-Box vs. DB-Wahrheit ══════════════════════════
if mode.startswith("🧩"):
    import math
    from eval import pictogram as picto

    st.title("🧩 Piktogramm-Mapping — LLM vs. Ground Truth")
    st.caption("Für GT-Schäden kennen wir die ECHTE Position (DB-Koordinaten → "
               "🟢 grüner Punkt). Der LLM bekam Bauteil/Seite/Projektion + Fotos "
               "und hat eine 🟥 rote Box gesetzt. So sieht man, wie weit das "
               "Prompt-Mapping von der Wahrheit abweicht. Grau = Abstand.")

    LLM_GT_DIR = ROOT / "data" / "pictograms_llm"
    car_dirs = sorted(d for d in LLM_GT_DIR.glob("*")
                      if d.is_dir() and not d.name.startswith("_")
                      and any(d.glob("*.json")))
    if not car_dirs:
        st.info("Noch keine LLM-Piktogramme. Erzeugen mit "
                "`.venv/bin/python scripts/locate_pictograms.py --source gt`.")
        st.stop()

    def _load_recs(cdir: Path) -> list[dict]:
        out = []
        for jf in sorted(cdir.glob("*.json")):
            try:
                out.append(json.loads(jf.read_text()))
            except Exception:
                pass
        return out

    def _metrics(rec: dict):
        """→ (hit, dist_px, dist_pct) oder (None, None, None)."""
        box = rec.get("box")
        size = rec.get("pictogram_size") or [0, 0]
        w, h = size
        if not box or not w:
            return None, None, None
        pt = picto.db_point(rec["plate_key"], rec.get("damage_ids", []), w, h)
        if pt is None:
            return None, None, None
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        hit = (box["x"] <= pt[0] <= box["x"] + box["width"]
               and box["y"] <= pt[1] <= box["y"] + box["height"])
        dist = math.hypot(cx - pt[0], cy - pt[1])
        return hit, dist, 100 * dist / math.hypot(w, h)

    # ── Gesamtüberblick über alle Autos ──────────────────────────────────────
    all_m = []
    for cdir in car_dirs:
        for rec in _load_recs(cdir):
            hit, _d, dpct = _metrics(rec)
            if dpct is not None:
                all_m.append((hit, dpct))
    if all_m:
        hr = 100 * sum(1 for h, _ in all_m if h) / len(all_m)
        w10 = 100 * sum(1 for _, d in all_m if d <= 10) / len(all_m)
        med = sorted(d for _, d in all_m)[len(all_m) // 2]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Bewertbare GT-Schäden", len(all_m))
        c2.metric("Punkt-in-Box-Treffer", f"{hr:.0f}%")
        c3.metric("Mittelpunkt ≤10% weg", f"{w10:.0f}%")
        c4.metric("Median-Abstand", f"{med:.1f}%")

    st.divider()

    _pk_to_plate = {d.name: d.name for d in car_dirs}
    for cdir in car_dirs:
        recs = _load_recs(cdir)
        plate = recs[0]["plate"] if recs else cdir.name
        _pk_to_plate[cdir.name] = plate
    sel = st.selectbox("Auto", [d.name for d in car_dirs],
                       format_func=lambda k: _pk_to_plate.get(k, k), key="picto_car")
    only_diff = st.checkbox("nur größere Abweichungen (>10% Diagonale)", value=False)
    show_nolocal = st.checkbox("not_localizable zeigen", value=False)

    cdir = LLM_GT_DIR / sel
    recs = _load_recs(cdir)
    shown = 0
    for rec in recs:
        box = rec.get("box")
        hit, dist, dpct = _metrics(rec)
        if not box:
            if not show_nolocal:
                continue
        elif only_diff and (dpct is not None and dpct <= 10):
            continue

        ids = rec.get("damage_ids", [])
        proj = rec.get("projection", "")
        cid = re.sub(r"[^A-Za-z0-9+_-]", "_", rec.get("gt_key") or rec.get("id") or "x")

        head = f"**{rec.get('part','?')}** · {rec.get('side','?')} · {proj} · #{'+#'.join(ids)}"
        if dpct is not None:
            badge = "✅ Treffer" if hit else "❌ daneben"
            head += f"  —  {badge} · Abstand **{dpct:.1f}%** ({dist:.0f}px)"
        elif not box:
            head += f"  —  ⚪ not_localizable: {rec.get('not_localizable_reason') or '—'}"
        st.markdown(head)

        src_size = rec.get("pictogram_size")
        gt_png = picto.render_gt(rec["plate_key"], ids, proj, cid)
        llm_png = (picto.render_llm(rec["plate_key"], proj, box, cid, src_size=src_size)
                   if box else None)
        ov = picto.render_overlay(rec["plate_key"], ids, proj, box or {}, cid,
                                  src_size=src_size)
        ov_png = ov[0] if ov else None

        col1, col2, col3 = st.columns(3)
        with col1:
            st.caption("🟢 Ground Truth (DB)")
            if gt_png:
                st.image(str(gt_png), use_container_width=True)
            else:
                st.caption("— keine DB-Koordinaten")
        with col2:
            st.caption("🟥 LLM-Prompt")
            if llm_png:
                st.image(str(llm_png), use_container_width=True)
            else:
                st.caption("— keine Box")
        with col3:
            st.caption("Overlay (beides)")
            if ov_png:
                st.image(str(ov_png), use_container_width=True)
        if box:
            st.caption(f"Selbsteinschätzung: part={box.get('part_match','?')} · "
                       f"side={box.get('side_match','?')} · "
                       f"photo={box.get('photo_evidence','?')} · "
                       f"sub={box.get('sub_part_localized','?')}"
                       + (f" · „{box.get('label')}”" if box.get('label') else ""))
        st.divider()
        shown += 1

    if shown == 0:
        st.info("Keine Schäden mit den aktuellen Filtern.")


# ══ 🔗 Gold-Vergleich: GT-Piktogramm vs FocalX-Piktogramm (Gold-Matches) ══════
if mode.startswith("🔗"):
    from eval import pictogram as picto

    st.title("🔗 Gold-Vergleich — GT vs FocalX (menschlich gemappte Paare)")
    st.caption("Nur die im Gold-Standard als Match bestätigten GT↔FocalX-Paare. "
               "Links GT (🟢 DB-Position + Foto), rechts FocalX (🟥 AI-Box + Close-up). "
               "So sieht man visuell, wie gut Position und Bild zusammenpassen.")

    GOLD_FILE = ROOT / "gold" / "mapping_gold.json"
    FX_LLM = ROOT / "data" / "pictograms_llm_focalx"
    RESULTS_DIR = ROOT / "data" / "results"
    if not GOLD_FILE.exists():
        st.info("Kein gold/mapping_gold.json.")
        st.stop()
    gold = json.loads(GOLD_FILE.read_text())

    def _gpk(p):
        return re.sub(r"[^A-Za-z0-9]", "", p or "").upper()

    cars = []
    for c in gold.get("cars", []):
        n = sum(1 for d in c.get("damages", [])
                if d.get("finding_keys") and not d.get("excluded"))
        if n:
            cars.append((c, n))
    if not cars:
        st.info("Keine Gold-Matches vorhanden.")
        st.stop()

    total_matches = sum(n for _, n in cars)
    st.caption(f"{len(cars)} Autos · {total_matches} Gold-Matches gesamt.")
    sel = st.selectbox(
        "Auto", list(range(len(cars))),
        format_func=lambda i: f"{cars[i][0]['plate']} ({cars[i][1]} Matches)",
        key="goldcmp_car")
    car = cars[sel][0]
    plate = car["plate"]
    pk = _gpk(plate)

    # FocalX-Lokalisierungen: Einzel-Key → Record (Kombi-Keys 'F7+F9' aufgesplittet)
    fx_by_key: dict[str, dict] = {}
    for jf in sorted((FX_LLM / pk).glob("*.json")):
        try:
            r = json.loads(jf.read_text())
        except Exception:
            continue
        for k in str(r.get("id") or "").split("+"):
            if k.strip():
                fx_by_key[k.strip()] = r

    # FocalX-Findings aus results: key → finding (Close-up, part, type, position)
    fx_find: dict[str, dict] = {}
    res_file = RESULTS_DIR / f"{car.get('checkin')}.json"
    if not res_file.exists():
        res_file = None
        for f in RESULTS_DIR.glob("*.json"):
            try:
                if _gpk(json.loads(f.read_text()).get("plate", "")) == pk:
                    res_file = f
                    break
            except Exception:
                pass
    if res_file and res_file.exists():
        for fi in json.loads(res_file.read_text()).get("findings", []):
            if fi.get("key"):
                fx_find[fi["key"]] = fi

    shown = 0
    for dm in car.get("damages", []):
        keys = dm.get("finding_keys") or []
        if not keys or dm.get("excluded"):
            continue
        shown += 1
        proj = dm.get("projection", "")
        ids = dm.get("damage_ids", [])
        cid = re.sub(r"[^A-Za-z0-9+_-]", "_",
                     dm.get("gt_key") or "+".join(ids) or "x")
        st.markdown(f"### {dm.get('part','?')} · {dm.get('type','?')} · "
                    f"{dm.get('side','?')} · {proj or '—'}  →  {', '.join(keys)}")

        gcol, fcol = st.columns(2)
        with gcol:
            st.markdown("**🟢 Ground Truth**")
            gt_png = picto.render_gt(pk, ids, proj, cid) if proj else None
            if gt_png:
                st.image(str(gt_png), use_container_width=True)
            else:
                st.caption("— kein GT-Piktogramm (keine Projektion/Koordinaten)")
            gphotos = []
            for did in ids:
                gphotos += sorted((GT_PHOTOS / pk).glob(f"{did}_*.jpg"))
            if gphotos:
                st.image([str(p) for p in gphotos[:3]], use_container_width=True)
            else:
                st.caption("— kein GT-Foto")
        with fcol:
            st.markdown("**🟥 FocalX (AI-gemappt)**")
            for k in keys:
                rec = fx_by_key.get(k)
                box = rec.get("box") if rec else None
                if rec and box:
                    fp = picto.render_llm(pk, rec.get("projection", proj), box,
                                          f"{cid}__{k}",
                                          src_size=rec.get("pictogram_size"))
                    if fp:
                        st.image(str(fp), use_container_width=True)
                elif rec:
                    st.caption(f"{k}: nicht lokalisierbar "
                               f"({rec.get('not_localizable_reason') or '—'})")
                else:
                    st.caption(f"{k}: keine Lokalisierung vorhanden")
                fi = fx_find.get(k)
                if fi and fi.get("closeup"):
                    cu = ROOT / fi["closeup"]
                    if cu.exists():
                        st.image(str(cu), use_container_width=True)
                if fi:
                    st.caption(f"{k}: {fi.get('part','?')} · {fi.get('type','?')} · "
                               f"{fi.get('position','?')}")
        st.divider()

    if shown == 0:
        st.info("Keine Gold-Matches für dieses Auto.")


# ══ 📐 Kanonisch: rein metrisches Matching im Fahrzeugrahmen ═════════════════
if mode.startswith("📐"):
    import altair as alt

    from eval import canonical as canon
    from eval import pictogram as picto
    from eval import strategy as strat
    from eval.matcher import part_class

    st.title("📐 Kanonisch — rein metrisches Matching")
    st.caption(
        "Jeder Piktogramm-Punkt wird in einen Fahrzeugrahmen (L = Front→Heck, "
        "W = Fahrer→Beifahrer, H = oben→unten) übersetzt. Dadurch liegen Ecken "
        "über Ansichtsgrenzen hinweg nah beieinander — Heck unten-links ist "
        "dasselbe wie Fahrerseite unten-hinten. 🟢 DB-Schaden (SHARK-Koordinaten, "
        "deterministisch) · 🟥 FocalX-Finding (AI-lokalisiert).")

    RES_V1 = ROOT / "data" / "results"

    @st.cache_data(show_spinner="Kanonische Abstände rechnen …")
    def canon_rows(_gold_mtime: float):
        """Ein Eintrag pro Benchmark-Urteil mit kanonischer Kandidaten-Rangliste.
        Alles JSON-nah (Listen statt Sets), damit es cachebar bleibt."""
        rows = []
        recs = strat.benchmark_records()
        gold = json.loads((ROOT / "gold" / "mapping_gold.json").read_text())
        proj_by = {(c["checkin"], d["gt_key"]): d.get("projection")
                   for c in gold["cars"] for d in c["damages"]}
        for checkin in sorted({r["checkin"] for r in recs}):
            rf = RES_V1 / f"{checkin}.json"
            if not rf.exists():
                continue
            r = json.loads(rf.read_text())
            pk = canon.plate_key(r["plate"])
            by_key = {f["key"]: f for f in r["findings"]}
            ph = r.get("physical") or {}
            clusters = ph.get("finding_clusters") or [[f["key"]] for f in r["findings"]]
            fx = canon.fx_records(pk)
            fx_by_key = {k: rec for rec in fx for k in rec["keys"]}
            for rec in [x for x in recs if x["checkin"] == checkin]:
                proj = proj_by.get((checkin, rec["gt_key"]))
                gold_keys = set(rec["gold_keys"])
                gp = canon.gt_point(pk, rec["damage_ids"], proj) if proj else None
                sp = canon.gt_strip(pk, rec["damage_ids"], proj) if proj else None
                # Kandidat = Finding-Cluster aus data/results (genau die Menge, die
                # ein Strategie-Lauf dem Judge vorlegt) — dieselbe Rangfolge, die
                # das v06-Gate benutzt.
                cands = [{
                    "id": "+".join(c["keys"]), "keys": c["keys"], "index": c["index"],
                    "projection": c["projection"], "strip": c["strip"],
                    "closest_key": c["closest_key"], "dist": round(c["dist"], 1),
                    "is_gold": bool(set(c["keys"]) & gold_keys),
                } for c in canon.rank_clusters(gp, clusters, fx_by_key)]
                # Bauteilklassen-Gate (das v05 heute benutzt) zum Vergleich
                tc = part_class(rec["part"])
                pc_clusters = [ks for ks in clusters if tc and part_class(
                    by_key.get(ks[0], {}).get("part")) == tc]
                pc_keys = {k for ks in pc_clusters for k in ks}
                gold_rank = next((i for i, c in enumerate(cands, 1) if c["is_gold"]), None)
                rows.append({
                    "checkin": checkin, "plate": r["plate"], "plate_key": pk,
                    "gt_key": rec["gt_key"], "damage_ids": rec["damage_ids"],
                    "part": rec["part"], "type": rec["type"], "side": rec["side"],
                    "projection": proj, "gold_keys": sorted(gold_keys),
                    "gt_strip": sp, "has_geo": gp is not None,
                    "cands": cands, "gold_rank": gold_rank,
                    # Cluster-Indizes, die laut Gold zu diesem Schaden gehören —
                    # Bezugsgröße für „Liste exakt = Gold“ und die Paar-Bilanz.
                    "gold_ci": sorted(i for i, ks in enumerate(clusters)
                                      if gold_keys & set(ks)),
                    "n_clusters": len(clusters),
                    "n_localized": sum(1 for ks in clusters if any(
                        fx_by_key.get(k, {}).get("localized") for k in ks)),
                    "gold_localized": bool(gold_keys) and any(
                        fx_by_key.get(k, {}).get("localized") for k in gold_keys),
                    "pc_hit": bool(gold_keys & pc_keys),
                    "pc_size": len(pc_clusters),
                    "pc_ids": ["+".join(sorted(ks)) for ks in pc_clusters],
                    "fx_all": [{"id": c["id"], "keys": sorted(c["keys"]),
                                "projection": c["projection"], "strip": c["strip"],
                                "part": by_key.get(sorted(c["keys"])[0], {}).get("part"),
                                "type": by_key.get(sorted(c["keys"])[0], {}).get("type")}
                               for c in fx if c["localized"]],
                })
        return rows

    rows = canon_rows((ROOT / "gold" / "mapping_gold.json").stat().st_mtime)
    mapp = [r for r in rows if r["gold_keys"]]
    if not mapp:
        st.info("Keine mappbaren Gold-Urteile.")
        st.stop()

    # ── Kennzahlen: taugt die Rangliste als Kandidaten-Vorauswahl? ───────────
    n = len(mapp)
    ceiling = sum(1 for r in mapp if r["gold_rank"] is not None)
    hit_at = {k: sum(1 for r in mapp if r["gold_rank"] and r["gold_rank"] <= k)
              for k in (1, 2, 3, 4)}
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Mappbare Gold-Urteile", n)
    c2.metric("Geometrisch erreichbar", f"{100*ceiling/n:.0f}%",
              help="Echtes Finding ist lokalisiert und im kanonischen Rahmen "
                   "vergleichbar — die Obergrenze für jedes Geometrie-Gate.")
    c3.metric("Treffer auf Platz 1", f"{100*hit_at[1]/n:.0f}%")
    c4.metric("unter den Top 2", f"{100*hit_at[2]/n:.0f}%")
    c5.metric("unter den Top 4", f"{100*hit_at[4]/n:.0f}%")

    def gate_row(label, tau, k, with_pc):
        """Recall und Größe eines Gates — τ/k genau wie in canonical.shortlist.
        Ø Kandidaten zählt über ALLE Urteile, nicht nur die mappbaren, weil das
        Bildbudget auch für die Urteile ohne Match anfällt."""
        hit = size = 0
        for r in rows:
            sel = [c for c in r["cands"] if c["dist"] <= tau][:k]
            ids = {c["id"] for c in sel}
            keysets = [set(c["keys"]) for c in sel]
            if with_pc:
                for cid in r["pc_ids"]:
                    if cid not in ids:
                        ids.add(cid)
                        keysets.append(set(cid.split("+")))
            size += len(ids)
            if r["gold_keys"] and any(ks & set(r["gold_keys"]) for ks in keysets):
                hit += 1
        return {"Gate": label, "Recall": f"{100*hit/n:.0f}%",
                "Ø Kandidaten": f"{size/len(rows):.1f}"}

    gate_tbl = [{"Gate": "Bauteilklasse (v05 heute)",
                 "Recall": f"{100*sum(1 for r in mapp if r['pc_hit'])/n:.0f}%",
                 "Ø Kandidaten": f"{sum(r['pc_size'] for r in rows)/len(rows):.1f}"}]
    for k in (2, 3, 4):
        gate_tbl.append(gate_row(
            f"kanonisch τ={canon.TAU:.0f} %, Top-{k}"
            + ("  ← v06" if k == canon.K else ""), canon.TAU, k, False))
    gate_tbl.append(gate_row(
        f"kanonisch τ={canon.TAU:.0f} %, Top-{canon.K} ∪ Bauteilklasse",
        canon.TAU, canon.K, True))
    st.markdown("**Als Kandidaten-Vorauswahl** — enthält das Gate das menschlich "
                "bestätigte Finding überhaupt?")
    st.table(pd.DataFrame(gate_tbl))

    with st.expander(f"❌ {n - ceiling} Urteile, die auch kanonisch nicht erreichbar sind"):
        for r in mapp:
            if r["gold_rank"] is None:
                why = ("FocalX-Finding nicht lokalisiert" if not r["gold_localized"]
                       else "kein DB-Punkt (Projektion/Koordinaten fehlen)"
                       if not r["has_geo"] else "Finding nicht in der Rangliste")
                st.markdown(f"- **{r['plate']}** · {r['part']} / {r['type']} · "
                            f"{r['projection'] or '—'} → Gold `{', '.join(r['gold_keys'])}` "
                            f"— {why}")

    st.divider()

    # ── Vollbilanz des Gates: was die Vorauswahl liefert und was sie kostet ──
    # Alles deterministisch, kein LLM. Zwei Ebenen strikt getrennt:
    #   URTEIL = ein GT-Schaden · PAAR = ein vorgeschlagener Finding-Cluster.
    st.markdown("### Vollbilanz der deterministischen Zuordnung")
    st.caption("Kein LLM — allein der kanonische Abstand entscheidet. "
               "Regler verschieben, um andere Schwellen zu prüfen.")
    sc1, sc2 = st.columns([2, 1])
    tau_s = sc1.slider("τ — maximaler Abstand (% Fahrzeuglänge)", 5, 120,
                       int(canon.TAU), 5, key="canon_tau")
    k_s = sc2.slider("k — höchstens so viele Kandidaten", 1, 8, int(canon.K),
                     key="canon_k")

    shorts = [(r, [c for c in r["cands"] if c["dist"] <= tau_s][:k_s]) for r in rows]
    short_by = {(r["checkin"], r["gt_key"]): s for r, s in shorts}
    empt = [r for r in rows if not r["gold_keys"]]
    n_all = len(rows)

    tp = sum(1 for _, s in shorts for c in s if c["is_gold"])
    fp = sum(1 for _, s in shorts for c in s if not c["is_gold"])
    fp_mapp = sum(1 for r, s in shorts if r["gold_keys"]
                  for c in s if not c["is_gold"])
    gold_pairs = sum(len(r["gold_ci"]) for r in rows)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    recl = tp / gold_pairs if gold_pairs else 0.0
    f1 = 2 * prec * recl / (prec + recl) if (prec + recl) else 0.0

    hit = [r for r, s in shorts if r["gold_keys"] and any(c["is_gold"] for c in s)]
    exact = [r for r, s in shorts
             if r["gold_ci"] and sorted(c["index"] for c in s) == r["gold_ci"]]
    lost = [r for r, s in shorts if r["gold_keys"] and not s]
    only_wrong = [r for r, s in shorts
                  if r["gold_keys"] and s and not any(c["is_gold"] for c in s)]
    ok_empty = [r for r, s in shorts if not r["gold_keys"] and not s]
    noise = [r for r, s in shorts if not r["gold_keys"] and s]
    calls = [r for r, s in shorts if s]
    n_cand = sum(len(s) for _, s in shorts)
    pool = sum(r["n_clusters"] for r in rows) / n_all

    m = st.columns(5)
    m[0].metric("Ø Kandidaten pro Schaden", f"{n_cand/n_all:.2f}",
                f"−{100-100*n_cand/(pool*n_all):.0f}% vs. {pool:.1f} ohne Gate",
                delta_color="inverse",
                help="Bezugsgröße ist der volle Kandidatenpool des Autos "
                     "(alle Finding-Cluster).")
    m[1].metric("Gate-Recall", f"{100*len(hit)/n:.0f}%",
                help=f"Bei so vielen der {n} mappbaren Schäden steht das "
                     "menschlich bestätigte Finding in der Liste.")
    m[2].metric("Präzision (Paar-Ebene)", f"{100*prec:.0f}%",
                help="Anteil richtiger Vorschläge an allen Vorschlägen. Niedrig, "
                     "weil die Mehrheit der GT-Schäden gar kein FocalX-Pendant "
                     "hat — dort ist jeder Kandidat zwangsläufig falsch.")
    m[3].metric("ohne KI erledigt", f"{len(ok_empty) + len(exact)}",
                f"{len(ok_empty)} leer + {len(exact)} eindeutig",
                delta_color="off",
                help="Leere Liste bei einem Schaden ohne Gold-Match = korrekt "
                     "abgehakt. Liste exakt gleich Gold = direkt übernehmbar.")
    m[4].metric("KI-Calls", f"{len(calls)}/{n_all}",
                f"−{100-100*len(calls)/n_all:.0f}%", delta_color="inverse")

    def pcs(a: int, b: int) -> str:
        """Anteil als Prozenttext — die Bezugsgröße steht immer daneben im Text."""
        return f"{100*a/b:.0f}%" if b else "—"

    b1, b2 = st.columns(2)
    with b1:
        st.markdown(
            f"**Paar-Ebene** — {tp+fp} Vorschläge insgesamt\n\n"
            f"- ✅ richtig (TP) **{tp}** · {100*prec:.0f}% aller Vorschläge\n"
            f"- ❌ falsch (FP) **{fp}** · {pcs(fp, tp+fp)} aller Vorschläge · davon "
            f"{fp_mapp} als Konkurrenz bei Schäden mit Match, {fp-fp_mapp} bei "
            f"Schäden ohne\n"
            f"- ⛔ verpasste Gold-Paare (FN) **{gold_pairs-tp}** von {gold_pairs} · "
            f"{pcs(gold_pairs-tp, gold_pairs)}\n"
            f"- Ø Fehlvorschläge pro Schaden **{fp/n_all:.2f}**\n\n"
            f"Precision {100*prec:.0f}% · Recall {100*recl:.0f}% · F1 {100*f1:.0f}%")
    with b2:
        st.markdown(
            f"**Urteils-Ebene** — {n_all} Schäden ({n} mit Gold-Match, "
            f"{len(empt)} ohne)\n\n"
            f"- ✅ richtiges Finding in der Liste **{len(hit)}** · "
            f"{pcs(len(hit), n)} der {n} mit Match, davon Liste exakt = Gold "
            f"**{len(exact)}** · {pcs(len(exact), n)}\n"
            f"- ⛔ Liste leer, obwohl es ein Match gab **{len(lost)}** · "
            f"{pcs(len(lost), n)}\n"
            f"- ❌ Liste voll, aber nur Falsches drin **{len(only_wrong)}** · "
            f"{pcs(len(only_wrong), n)}\n"
            f"- ✅ korrekt leer (FocalX hat den Schaden nicht) **{len(ok_empty)}** "
            f"von {len(empt)} · {pcs(len(ok_empty), len(empt))}\n"
            f"- 🤖 Liste nicht leer, KI muss ablehnen **{len(noise)}** · "
            f"{pcs(len(noise), len(empt))} der {len(empt)} ohne Match\n\n"
            f"Obergrenze mit perfekter Stufe 2: "
            f"**{100*(len(hit)+len(empt))/n_all:.0f}%** Genauigkeit")

    # Reject = Liste nicht leer, aber KEIN richtiger Kandidat drin: alles, was Stufe 2
    # komplett ablehnen müsste. Zwei Ursachen, beide hier drin, weil die KI sie nicht
    # unterscheiden kann — sie sieht nur falsche Kandidaten:
    #   „kein Match“  Gold sagt, FocalX hat den Schaden gar nicht
    #   „Gold verpasst“ es gibt ein echtes Finding, aber τ/k haben es rausgefiltert
    # Sortiert nach Abstand des nächsten Fehlvorschlags — die engsten sind die
    # gefährlichsten, weil sie einem Auto-Match am nächsten kommen.
    rej = sorted(((r, s) for r, s in shorts if s and not any(c["is_gold"] for c in s)),
                 key=lambda x: x[1][0]["dist"])
    rej_pairs = sum(len(s) for _, s in rej)

    def why_reject(r) -> str:
        if not r["gold_keys"]:
            return "kein Match"
        return ("Gold außerhalb τ/k" if r["gold_rank"]
                else "Gold nicht verortet")

    if rej:
        rej_head = (
            f"{len(noise)} × „kein Match“ ({pcs(len(noise), len(empt))} der "
            f"{len(empt)} Schäden ohne Gold-Match) · {len(only_wrong)} × "
            f"„Gold verpasst“ ({pcs(len(only_wrong), n)} der {n} mit Gold-Match) · "
            f"zusammen {rej_pairs} abzulehnende Vorschläge "
            f"({pcs(rej_pairs, tp + fp)} aller Vorschläge, "
            f"Ø {rej_pairs/len(rej):.1f} pro Fall).  \n")
    else:
        rej_head = ""

    with st.expander(
            f"✗ {len(rej)} Reject-Fälle · {pcs(len(rej), n_all)} aller {n_all} "
            f"Urteile — Liste voll, aber nichts Richtiges drin"):
        st.caption(
            rej_head
            + "Hier müsste Stufe 2 jeden einzelnen Vorschlag ablehnen. "
            "„kein Match“ = FocalX hat den Schaden laut Mensch nicht, ohne "
            "Kandidaten wäre das Urteil ohne KI korrekt leer geblieben. "
            "„Gold nicht verortet“ = das echte Finding ist im Piktogramm nicht "
            "lokalisiert, steht also in keiner Rangliste. „Gold außerhalb τ/k“ "
            "= es wäre wählbar, die Schwellen schneiden es weg.")
        if rej:
            st.dataframe(pd.DataFrame([{
                "Kennzeichen": r["plate"],
                "Schaden": "#" + "+#".join(r["damage_ids"]),
                "Bauteil": r["part"], "Typ": r["type"],
                "Seite": r["side"] or "—", "Ansicht": r["projection"] or "—",
                "Ursache": why_reject(r),
                "Gold": ", ".join(r["gold_keys"]) or "—",
                "Gold-Platz": f"{r['gold_rank']}" if r["gold_rank"] else "—",
                "nächster": f"{s[0]['dist']} %",
                "Vorschläge": ", ".join(f"{c['id']} ({c['dist']} %)" for c in s),
            } for r, s in rej]), use_container_width=True, hide_index=True)
        else:
            st.write("Keine — bei diesen Schwellen enthält jede nicht-leere Liste "
                     "auch das richtige Finding.")

    # Recall-Trichter: die Schwellen sind selten das Problem, die Lokalisierung ist es.
    g_loc = sum(1 for r in rows for c in r["cands"] if c["is_gold"])
    g_tau = sum(1 for r in rows for c in r["cands"]
                if c["is_gold"] and c["dist"] <= tau_s)
    st.caption(
        f"Recall-Trichter: {gold_pairs} Gold-Paare → {g_loc} mit verortetem "
        f"FocalX-Cluster ({100*g_loc/gold_pairs:.0f}% = geometrische Obergrenze) "
        f"→ {g_tau} innerhalb τ={tau_s} % → {tp} in den Top-{k_s}. "
        "Was fehlt, fehlt fast nur an der Lokalisierung, nicht an den Schwellen.")

    ch1, ch2, ch3 = st.columns(3)
    len_df = pd.DataFrame([{"Länge": str(i) if i < k_s else f"{k_s} (Limit)",
                            "Schäden": sum(1 for _, s in shorts if len(s) == i)}
                           for i in range(k_s + 1)])
    with ch1:
        st.markdown("**Länge der Kandidatenliste**")
        st.altair_chart(alt.Chart(len_df).mark_bar(color=BLUE).encode(
            x=alt.X("Länge:N", title=None), y=alt.Y("Schäden:Q", title=None),
            tooltip=["Länge", "Schäden"]).properties(height=200),
            use_container_width=True)
    with ch2:
        st.markdown("**Recall nach Listenplatz**")
        # Platz innerhalb der τ-gefilterten Rangliste — sonst würde der Regler
        # die Balken nicht mitbewegen.
        tau_rank = [next((i for i, c in enumerate(
            [c for c in r["cands"] if c["dist"] <= tau_s], 1) if c["is_gold"]), None)
            for r in mapp]
        cum = [{"Platz": f"@{i}",
                "Recall": round(100 * sum(1 for p in tau_rank if p and p <= i) / n)}
               for i in range(1, k_s + 1)]
        st.altair_chart(alt.Chart(pd.DataFrame(cum)).mark_bar(color=GREEN).encode(
            x=alt.X("Platz:N", title=None),
            y=alt.Y("Recall:Q", title=None, scale=alt.Scale(domain=[0, 100])),
            tooltip=["Platz", "Recall"]).properties(height=200),
            use_container_width=True)
    with ch3:
        st.markdown("**Abstandsverteilung**")
        d_df = pd.DataFrame(
            [{"Abstand": c["dist"], "Paar": "echt" if c["is_gold"] else "falsch"}
             for r in rows for c in r["cands"]])
        st.altair_chart(alt.Chart(d_df).mark_bar(opacity=0.75).encode(
            x=alt.X("Abstand:Q", bin=alt.Bin(step=5), title="Abstand %"),
            y=alt.Y("count()", stack=None, title=None,
                    scale=alt.Scale(type="symlog")),
            color=alt.Color("Paar:N", scale=alt.Scale(
                domain=["echt", "falsch"], range=[GREEN, "#c9ced6"]),
                legend=alt.Legend(orient="top", title=None)),
            tooltip=["Paar:N", "count()"]).properties(height=200),
            use_container_width=True)
        st.caption("Log-Skala — echte Paare liegen fast alle unter 25 %.")

    with st.expander("Pro Auto"):
        per_car = []
        for p in sorted({r["plate"] for r in rows}):
            rs = [(r, s) for r, s in shorts if r["plate"] == p]
            m_ = [(r, s) for r, s in rs if r["gold_keys"]]
            h_ = sum(1 for r, s in m_ if any(c["is_gold"] for c in s))
            per_car.append({
                "Kennzeichen": p, "Urteile": len(rs), "mit Gold": len(m_),
                "Ø Kandidaten": round(sum(len(s) for _, s in rs) / len(rs), 1),
                "Recall": f"{100*h_/len(m_):.0f}%" if m_ else "—",
                "Fehlvorschläge": sum(1 for _, s in rs
                                      for c in s if not c["is_gold"]),
            })
        st.dataframe(pd.DataFrame(per_car), use_container_width=True,
                     hide_index=True)

    st.divider()

    # ── Abgewickelter Fahrzeugstreifen pro Auto ──────────────────────────────
    plates = sorted({r["plate"] for r in rows})
    sel = st.selectbox("Auto", plates, key="canon_car")
    car_rows = [r for r in rows if r["plate"] == sel]
    scope = st.radio("Welche DB-Schäden", ["nur mit Gold-Match",
                                           "+ Reject-Fälle", "alle"],
                     horizontal=True, key="canon_scope")
    link = st.radio("Verbindungslinien", ["Gold-Match", "nächster Kandidat", "keine"],
                    horizontal=True, key="canon_link")

    def reject_short(r) -> list[dict]:
        """Vorschläge, die Stufe 2 komplett ablehnen müsste: Liste nicht leer, aber
        kein richtiger Kandidat drin. Leer heißt: nichts zu widerrufen."""
        s = short_by.get((r["checkin"], r["gt_key"]), [])
        return [] if any(c["is_gold"] for c in s) else s

    def in_scope(r) -> bool:
        if r["gold_keys"] or scope == "alle":
            return True
        return scope.startswith("+") and bool(reject_short(r))

    faces = [{"s": i + 0.5, "h": 0.02, "label": lab} for i, lab in enumerate(
        ["Front", "Fahrerseite", "Heck", "Beifahrerseite"])]

    gt_pts, fx_pts, links = [], [], []
    seen_fx = set()
    for r in car_rows:
        if not in_scope(r):
            continue
        rs = reject_short(r)
        sel_ = short_by.get((r["checkin"], r["gt_key"]), [])
        if any(c["is_gold"] for c in sel_):
            cls, note = "ja", f"Gold: {', '.join(r['gold_keys'])}"
        elif rs:
            cls = "reject"
            note = (("Gold verpasst" if r["gold_keys"] else "kein Match")
                    + " — Gate schlägt "
                    + ", ".join(f"{c['id']} ({c['dist']} %)" for c in rs) + " vor")
        elif r["gold_keys"]:
            cls = "nein"
            note = f"Gold {', '.join(r['gold_keys'])} nicht in der Vorauswahl"
        else:
            cls, note = "nein", "kein Match, kein Kandidat"
        if r["gt_strip"]:
            gt_pts.append({
                "s": r["gt_strip"][0], "h": r["gt_strip"][1],
                "label": f"{r['part']} / {r['type']}",
                "info": f"{r['side'] or '—'} · {r['projection'] or '—'} · {note}",
                "gemappt": cls,
            })
        for c in r["fx_all"]:
            if c["id"] in seen_fx or not c["strip"]:
                continue
            seen_fx.add(c["id"])
            fx_pts.append({"s": c["strip"][0], "h": c["strip"][1], "label": c["id"],
                           "info": f"{c['part']} / {c['type']} · {c['projection']}"})
        if not r["gt_strip"]:
            continue
        target = None
        if link == "Gold-Match":
            target = next((c for c in r["cands"] if c["is_gold"]), None)
        elif link == "nächster Kandidat":
            target = r["cands"][0] if r["cands"] else None
        if target and target["strip"]:
            links.append({"s": r["gt_strip"][0], "h": r["gt_strip"][1],
                          "s2": target["strip"][0], "h2": target["strip"][1],
                          "dist": target["dist"], "label": target["id"],
                          "treffer": "ja" if target["is_gold"] else "nein"})

    # Farben bewusst fest pro Ebene statt über Farbskalen: in einem Layer-Chart
    # teilen sich alle Ebenen EINE Farbskala pro Kanal, zwei Skalen mit gleicher
    # Domain ("ja"/"nein") kollidieren und Vega rendert dann gar nichts.
    xs = alt.X("s:Q", scale=alt.Scale(domain=[0, 4], nice=False), axis=None, title=None)
    ys = alt.Y("h:Q", scale=alt.Scale(domain=[1, 0], nice=False), axis=None, title=None)
    tip = ["label:N", "info:N"]

    def pts(rows, **mark):
        return alt.Chart(pd.DataFrame(rows)).mark_point(
            filled=True, **mark).encode(x=xs, y=ys, tooltip=tip)

    layers = [
        alt.Chart(pd.DataFrame({"s": [1.0, 2.0, 3.0]})).mark_rule(
            color="#ccc", strokeDash=[4, 4]).encode(
            x=alt.X("s:Q", scale=alt.Scale(domain=[0, 4], nice=False), axis=None)),
        alt.Chart(pd.DataFrame(faces)).mark_text(
            align="center", baseline="top", color="#888", fontSize=12).encode(
            x=alt.X("s:Q", scale=alt.Scale(domain=[0, 4], nice=False), axis=None),
            y=alt.Y("h:Q", scale=alt.Scale(domain=[1, 0], nice=False), axis=None),
            text="label:N"),
    ]
    for hit, color in (("ja", GREEN), ("nein", ORANGE)):
        sub = [x for x in links if x["treffer"] == hit]
        if sub:
            layers.append(alt.Chart(pd.DataFrame(sub)).mark_rule(
                strokeWidth=1.6, opacity=0.8, color=color).encode(
                x=xs, y=ys, x2="s2:Q", y2="h2:Q",
                tooltip=["label:N", alt.Tooltip("dist:Q", title="Abstand %")]))
    if fx_pts:
        layers.append(pts(fx_pts, shape="square", size=110, color=RED, opacity=0.85))
    for mapped, color in (("ja", GREEN), ("reject", ORANGE), ("nein", "#9aa5b1")):
        sub = [x for x in gt_pts if x["gemappt"] == mapped]
        if sub:
            layers.append(pts(sub, shape="circle", size=160, color=color, opacity=0.9))
    st.altair_chart(alt.layer(*layers).properties(height=320),
                    use_container_width=True)
    st.caption("Abgewickeltes Auto: die vier Ansichten liegen so nebeneinander, "
               "wie sie am Fahrzeug aneinandergrenzen (der Streifen schließt sich "
               "rechts wieder an die Front an). Oben = Dachkante, unten = Schweller. "
               "🟢 DB-Schaden, dessen Gold-Match in der Vorauswahl steht · orange = "
               "Reject: Vorauswahl voll, aber nichts Richtiges drin · grau = kein "
               "Kandidat in Reichweite · 🟥 FocalX-Finding. Linie grün = Gold-Match, "
               "orange = nächster Kandidat ist der falsche.")

    st.divider()

    # ── Ranglisten pro Schaden ──────────────────────────────────────────────
    st.markdown("### Rangliste pro DB-Schaden")
    for r in car_rows:
        if not in_scope(r):
            continue
        rank = r["gold_rank"]
        rs = reject_short(r)
        if r["gold_keys"]:
            badge = ("✅ Platz 1" if rank == 1 else f"🟡 Platz {rank}" if rank
                     else "❌ nicht in der Rangliste")
            if rs:
                badge += (f" · ✗ Reject: Vorauswahl (τ={tau_s} %, k={k_s}) bietet "
                          f"nur `{rs[0]['id']}` an — falsch")
        elif rs:
            badge = (f"✗ Reject: kein Match, Gate schlägt `{rs[0]['id']}` vor "
                     f"({rs[0]['dist']} %)")
        else:
            badge = "– kein Gold-Match, kein Kandidat"
        st.markdown(f"**{r['part']} / {r['type']}** · {r['side'] or '—'} · "
                    f"{r['projection'] or '—'} · #{'+#'.join(r['damage_ids'])}  —  "
                    f"Gold `{', '.join(r['gold_keys']) or '—'}` · {badge}")
        if not r["cands"]:
            st.caption("— keine lokalisierten FocalX-Findings "
                       + ("(kein DB-Punkt)" if not r["has_geo"] else ""))
            st.divider()
            continue
        rej_ids = {c["id"] for c in rs}
        lines = []
        for i, c in enumerate(r["cands"][:6], 1):
            mark = "✅" if c["is_gold"] else "✗" if c["id"] in rej_ids else "·"
            lines.append(f"{mark} **{i}.** `{c['id']}` · {c['projection']} · "
                         f"Abstand **{c['dist']} %**")
        st.markdown("  \n".join(lines))
        with st.expander("Piktogramme (DB-Punkt vs. die zwei nächsten Kandidaten)"):
            cid = re.sub(r"[^A-Za-z0-9+_-]", "_", r["gt_key"])
            cols = st.columns(3)
            with cols[0]:
                st.caption("🟢 DB-Schaden")
                g = (picto.render_gt(r["plate_key"], r["damage_ids"], r["projection"], cid)
                     if r["projection"] else None)
                if g:
                    st.image(str(g), use_container_width=True)
                else:
                    st.caption("— kein DB-Punkt")
            fx = canon.fx_records(r["plate_key"])
            by_key = {k: rec for rec in fx for k in rec["keys"]}
            for slot, c in zip(cols[1:], r["cands"][:2]):
                rec = by_key.get(c.get("closest_key"))
                with slot:
                    st.caption(f"🟥 {c['id']} · {c['dist']} %"
                               + (" · Gold" if c["is_gold"] else ""))
                    p = (picto.render_llm(r["plate_key"], rec["projection"], rec["box"],
                                         f"{cid}__{re.sub(r'[^A-Za-z0-9]', '_', c['id'])}",
                                         src_size=rec["size"])
                         if rec and rec["box"] else None)
                    if p:
                        st.image(str(p), use_container_width=True)
        st.divider()


if mode.startswith("📈"):
    st.title("📈 Massenauswertung — wie viele DB-Schäden findet FocalX?")
    st.caption("Alle Autos des aktiven Runs, gemappt von der KI. Diese Seite ist "
               "**nicht menschlich validiert** — sie ist die Hochrechnung auf "
               "große Stückzahl. Die geprüfte Wahrheit steht weiterhin auf der "
               "📊-Seite (20 Autos, 127 Schäden, rein manuell gemappt).")

    STRATS = RESULTS.parent / "strategies"
    s_opts = sorted(p.name for p in STRATS.iterdir() if p.is_dir()) if STRATS.exists() else []
    if not s_opts:
        st.info(f"Für Run **{runs_mod.label(_run_id)}** gibt es noch keine "
                f"Mapping-Läufe. Erzeugen mit "
                f"`python3 -u scripts/run_strategy.py v08-all-distance "
                f"--run {_run_id} --all-cars`.")
        st.stop()
    s_sel = st.selectbox("Mapping-Strategie", s_opts,
                         index=s_opts.index("v08-all-distance")
                         if "v08-all-distance" in s_opts else 0)

    def _stamp(d: Path) -> tuple[int, float]:
        fs = list(d.glob("*.json"))
        return len(fs), max((f.stat().st_mtime for f in fs), default=0.0)

    @st.cache_data(show_spinner="Mappings werden ausgewertet …")
    def mass_rows(run_id: str, strategy: str, stamp: tuple[int, float]):
        """Ein Datensatz je physischem DB-Schaden aus den fertigen Mappings.

        Nur Autos, deren Mapping vollständig ist — ein halb gelaufenes Auto
        würde sonst wie ein Auto mit lauter Nicht-Treffern aussehen."""
        rows, cars, teilweise, offen = [], [], 0, 0
        for f in sorted((RESULTS.parent / "strategies" / strategy).glob("*.json")):
            res = RESULTS / f.name
            if not res.exists():
                continue
            r = json.loads(res.read_text())
            if r.get("skipped"):
                continue
            props = json.loads(f.read_text()).get("proposals") or {}
            want = gt_clusters_of(r) or [[str(t["damage_id"])]
                                         for t in r.get("truths") or []]
            fertig = {k: p for k, p in props.items()
                      if p.get("via") in ("ai", "ai_rejected")}
            if len(fertig) < len(want):
                teilweise += 1
                offen += len(want) - len(fertig)
                continue
            key = plate_key(r["plate"])
            srcs = source_map(key)
            by_id = {str(t["damage_id"]): t for t in r.get("truths") or []}
            getroffen: set[str] = set()
            for gt_key, p in fertig.items():
                ids = gt_key.split("+")
                t = by_id.get(ids[0], {})
                sev = t.get("severity")
                hit = bool(p.get("finding_keys"))
                getroffen.update(p.get("finding_keys") or [])
                rows.append({
                    "Auto": r["plate"], "checkin": r["checkin"],
                    "Teil": t.get("part") or "ohne Angabe",
                    "Typ": t.get("damage_type") or "ohne Angabe",
                    "Größe": size_bucket(sev), "Tiefe": depth_bucket(sev),
                    "Quelle": "Damage Gate" if srcs.get(ids[0]) == 10 else "übrige",
                    "Gefunden": hit,
                    "Kandidaten": p.get("n_candidates") or 0,
                    "Sekunden": p.get("seconds") or 0.0,
                })
            cl = finding_clusters_of(r) or [[f_["key"]] for f_ in r.get("findings", [])]
            cars.append({
                "Auto": r["plate"], "checkin": r["checkin"],
                "DB-Schäden": len(fertig),
                "davon gefunden": sum(1 for p in fertig.values() if p.get("finding_keys")),
                "FocalX-Meldungen": len(cl),
                "ohne DB-Gegenstück": sum(1 for c in cl
                                          if not (set(c) & getroffen)),
            })
        return pd.DataFrame(rows), pd.DataFrame(cars), teilweise, offen

    df, cars_df, teilweise, offen = mass_rows(_run_id, s_sel, _stamp(STRATS / s_sel))
    if df.empty:
        st.info("Noch kein Auto vollständig gemappt.")
        st.stop()

    ges, tref = len(df), int(df["Gefunden"].sum())
    roh = tref / ges

    # Die KI mappt vorsichtiger als ein Mensch: auf dem Gold-Benchmark meldet sie
    # weniger Matches, als tatsächlich da sind. Der Faktor daraus hebt die rohe
    # Quote auf das zu erwartende menschliche Urteil.
    faktor = korr = None
    try:
        from eval import strategy as _strat
        _recs = _strat.benchmark_records()
        _props = _strat.proposals_for(s_sel)
        _mensch = sum(1 for r_ in _recs if r_.get("gold_keys"))
        _ki = sum(1 for r_ in _recs
                  if (_props.get((r_["checkin"], r_["gt_key"])) or {}).get("finding_keys"))
        if _ki:
            faktor = _mensch / _ki
            korr = min(1.0, roh * faktor)
    except Exception:
        pass

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Autos ausgewertet", f"{len(cars_df)}")
    c2.metric("Physische DB-Schäden", f"{ges}")
    c3.metric("Von FocalX gefunden", f"{tref}", f"{roh:.1%} Recall")
    c4.metric("Korrigiert (KI-Bias)",
              f"{korr:.1%}" if korr else "—",
              f"×{faktor:.2f}" if faktor else "kein Gold-Vergleich")
    if teilweise:
        st.caption(f"{teilweise} Auto(s) mit noch laufendem Mapping sind "
                   f"ausgeklammert ({offen} offene Urteile).")
    if faktor:
        st.caption(f"Korrektur: Auf dem Gold-Benchmark (123 Urteile) meldet "
                   f"**{s_sel}** {_ki} Matches, wo Menschen {_mensch} sehen — die "
                   f"KI übersieht also Treffer. Die rohe Quote ist damit eine "
                   f"Untergrenze, die korrigierte eine Schätzung des menschlichen "
                   f"Urteils.")

    def quote_df(spalte: str, order: list[str] | None = None) -> pd.DataFrame:
        g = df.groupby(spalte)["Gefunden"].agg(["sum", "count"])
        g = g.rename(columns={"sum": "Gefunden", "count": "Gesamt"})
        g["Nicht gefunden"] = g["Gesamt"] - g["Gefunden"]
        g["Recall"] = g["Gefunden"] / g["Gesamt"]
        if order:
            g = g.reindex([o for o in order if o in g.index])
        else:
            g = g.sort_values("Gesamt", ascending=False)
        return g[["Gefunden", "Nicht gefunden", "Gesamt", "Recall"]]

    def zeige(g: pd.DataFrame) -> None:
        st.dataframe(g.style.format({"Recall": "{:.1%}"})
                     .background_gradient(cmap="RdYlGn", subset=["Recall"],
                                          vmin=0, vmax=1),
                     use_container_width=True)

    st.header("Nach Schadensgröße")
    zeige(quote_df("Größe", SIZE_ORDER))
    st.header("Nach Schadenstiefe")
    zeige(quote_df("Tiefe", DEPTH_ORDER))

    st.header("Nach Erfassungsquelle")
    st.caption("**Damage Gate** = automatisches Scan-Portal (erfasst auch "
               "kleinste Schäden) · **übrige** = Agent-App und andere Systeme.")
    zeige(quote_df("Quelle"))

    st.header("Nach Bauteil")
    teile = quote_df("Teil")
    zeige(teile.head(25))

    st.header("Pro Auto")
    st.caption("„ohne DB-Gegenstück“ = FocalX meldet einen Schaden, dem kein "
               "DB-Eintrag zugeordnet wurde — entweder ein Fund, den die "
               "Datenbank nicht kennt, oder ein Fehlalarm.")
    cars_df = cars_df.sort_values("DB-Schäden", ascending=False)
    st.dataframe(cars_df, use_container_width=True, hide_index=True)
    st.caption(f"FocalX meldet insgesamt {int(cars_df['FocalX-Meldungen'].sum())} "
               f"physische Schäden, davon "
               f"{int(cars_df['ohne DB-Gegenstück'].sum())} ohne DB-Gegenstück.")

    st.download_button("Alle Urteile als CSV", df.to_csv(index=False).encode(),
                       file_name=f"{_run_id}_{s_sel}_schaeden.csv", mime="text/csv")
