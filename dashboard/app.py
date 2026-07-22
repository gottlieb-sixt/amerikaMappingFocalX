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

RESULTS = ROOT / "data" / "results"
GT_PHOTOS = ROOT / "data" / "gt_photos"
REVIEWS = ROOT / "data" / "reviews"

st.set_page_config(page_title="FocalX Evaluation", page_icon="🚗", layout="wide")

reports = sorted(RESULTS.glob("*.json"))
data = [json.loads(p.read_text()) for p in reports]
data = [d for d in data if not d.get("skipped")]
if not data:
    st.info("Noch keine Ergebnisse — erst `python -m eval.pipeline …` laufen lassen.")
    st.stop()

MODES = ["📊 Ergebnisse", "🔍 Review / manuelles Mapping", "📈 Metriken"]
# Navigation aus der Übersicht: VOR der Radio-Instanziierung verarbeiten
# (session_state eines gerenderten Widgets darf nicht mehr geändert werden).
if "nav_to_review" in st.session_state:
    st.session_state["mode_radio"] = MODES[1]
    st.session_state["review_checkin_sel"] = st.session_state.pop("nav_to_review")
mode = st.sidebar.radio("Modus", MODES, key="mode_radio")

GREEN, RED, ORANGE, BLUE = "#2e9e5b", "#d0433b", "#e8802a", "#3479c4"


# ── Hilfen ──────────────────────────────────────────────────────────────────

def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate).upper()


def gt_images(key: str, damage_id: str) -> list[Path]:
    return sorted((GT_PHOTOS / key).glob(f"{damage_id}_*.jpg"))


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
    st.title("🚗 FocalX Detection Evaluation")
    st.header("Übersicht")
    def _row(r: dict) -> dict:
        ph = r.get("physical") or {}
        pairs = ph.get("cluster_pairs") or []
        rep = (repaired_ids(plate_key(r["plate"]))
               | late_ids(plate_key(r["plate"]), r["checkin"]))
        n_rep = sum(1 for cp in pairs if cp["damage_ids"]
                    and all(d in rep for d in cp["damage_ids"]))
        tot = ph.get("gt_total")
        tot_adj = (tot - n_rep) if tot is not None else None
        found = ph.get("gt_found")
        return {
            "Kennzeichen": r["plate"],
            "Check-in": r["checkin"].split("__")[1],
            "Schäden (DB)": r["ground_truth_total"],
            "Physisch (DB)": tot_adj,
            "🔧/⏰ Nicht bewertbar": n_rep,
            "Gefunden (physisch)": found,
            "Recall": (found / tot_adj if found is not None and tot_adj else
                       ph.get("recall", r["recall"])),
            "Neue Schäden (unique)": ph.get("extras_unique", len(r["extra_findings"])),
            "Status": ("🟢 AI-Scan fertig — reviewbar" if ai_scan_done(r)
                       else "⏳ Mapping ausstehend" if r.get("mapping_pending")
                       else "🟡 AI-Scan läuft"),
            "Zeitpunkt": r["timestamp"],
        }

    rows = [_row(r) for r in data]
    df = pd.DataFrame(rows)
    mapped = df[df["Status"] != "⏳ Mapping ausstehend"]
    total_phys = int(mapped["Physisch (DB)"].fillna(0).sum())
    total_found = int(mapped["Gefunden (physisch)"].fillna(0).sum())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Check-ins", len(df))
    c2.metric("Physische Schäden (DB, gemappt)", total_phys)
    c3.metric("Davon gefunden", total_found)
    c4.metric("Neue Schäden (unique)", int(mapped["Neue Schäden (unique)"].fillna(0).sum()))
    c5.metric("Recall (physisch)", f"{total_found / total_phys:.0%}" if total_phys else "–")
    st.caption("Zeile anklicken → Review dieses Autos")
    _dfk = st.session_state.get("df_key_n", 0)
    _ready = [ai_scan_done(r) for r in data]
    _sty = (df.style
            .background_gradient(subset=["Recall"], cmap="RdYlGn", vmin=0, vmax=1)
            .apply(lambda row: ["background-color:#d9f2e0" if _ready[row.name] and c != "Recall"
                                else "" for c in df.columns], axis=1))
    ev = st.dataframe(
        _sty,
        use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row", key=f"overview_df_{_dfk}",
    )
    if ev.selection.rows:
        st.session_state["nav_to_review"] = data[ev.selection.rows[0]]["checkin"]
        st.session_state["df_key_n"] = _dfk + 1   # frische Tabelle ohne Alt-Auswahl
        st.rerun()

    st.header("Detail")
    sel = st.selectbox("Check-in", [r["checkin"] for r in data])
    r = next(x for x in data if x["checkin"] == sel)
    key = plate_key(r["plate"])
    truths = {str(t["damage_id"]): t for t in r["truths"]}
    findings = {f["key"]: f for f in r["findings"]}
    pair_by_truth = {p["damage_id"]: p for p in r.get("pairs", []) if p.get("findings")}

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
            triage = {"auto_match": "🟢 auto_match", "review": "🟡 review"}.get(
                (p or {}).get("triage") or "", "")
            if p and p.get("via") == "ai":
                nt = f"🧠 KI-Match ({p.get('confidence', '–')}) {triage} — {p.get('reason', '')}"
            else:
                nt = f"⚙️ {p.get('reason') if p else 'Heuristik-Match'} {triage}"
            ai_blocks = [ai_block(findings[k], BLUE, nt if i == 0 else "")
                         for i, k in enumerate(fkeys) if k in findings]
            cards.append(gallery.card(gt_block(key, tid, t, GREEN), *ai_blocks))
        if not r["found"]:
            st.warning("Kein DB-Schaden wurde gefunden (oder Mapping ausstehend).")
    elif category.startswith("❌"):
        for tid in r["missed"]:
            cards.append(gallery.card(gt_block(key, tid, truths.get(tid, {}), RED)))
        if not r["missed"]:
            st.success("Alle DB-Schäden wurden gefunden.")
    else:
        for k in r["extra_findings"]:
            cards.append(gallery.card(ai_block(findings.get(k, {}), ORANGE)))
    components.html(gallery.render(cards), height=820, scrolling=True)

# ════════════════════════════════════════════════════════════════════════════
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
    gcl = gt_clusters_of(r)
    review = load_review(r["checkin"])

    done = sum(1 for ids in gcl if "+".join(sorted(ids)) in review)
    pc1, pc2 = st.columns([4, 2])
    with pc1:
        st.progress(done / len(gcl) if gcl else 1.0,
                    text=f"{done}/{len(gcl)} Schäden reviewt")
    with pc2:
        is_done = review_done(review)
        new_done = st.toggle("✔️ Mit diesem Auto durch — in Statistik aufnehmen",
                             value=is_done, key=f"done_{sel}")
        if new_done != is_done:
            set_review_done(r["checkin"], new_done)
            st.rerun()
    if done < len(gcl) and review_done(review):
        st.warning(f"⚠️ Als abgeschlossen markiert, aber erst {done}/{len(gcl)} Schäden reviewt.")

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
                  else "✏️" if rev else "⬜")
        if excluded:
            st.markdown(f"""<style>
            div[class*="st-key-dmg_{sel}_{gi}"] {{ opacity: 0.4; }}
            </style>""", unsafe_allow_html=True)

        with st.container(border=True, key=f"dmg_{sel}_{gi}"):
            # ── Sticky GT-Kopf: Infos + Fotos, bleibt beim Scrollen stehen ──
            with st.container(key=f"sticky_{sel}_{gi}"):
                st.markdown('<span class="gt-sticky-marker"></span>', unsafe_allow_html=True)
                head = st.columns([3, 4])
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
                            imgs = [(k, ROOT / findings[k]["closeup"])
                                    for k in keys if findings[k].get("closeup")]
                            imgs = [(k, p_) for k, p_ in imgs if p_.exists()]
                            if len(imgs) == 1:
                                st.image(str(imgs[0][1]), use_container_width=True)
                            elif imgs:
                                # Klick aufs Bild schaltet zum nächsten Cluster-Mitglied.
                                # Optik identisch zu st.image; Chip nur bei Hover.
                                import base64 as _b64
                                _payload = json.dumps([
                                    {"k": k,
                                     "src": "data:image/jpeg;base64,"
                                            + _b64.b64encode(p_.read_bytes()).decode()}
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
                                st.rerun()
            none_current = (rev is not None and not rev["human"]
                            and rev["verdict"] != "excluded")
            bcols = st.columns([3, 2, 2])
            with bcols[0]:
                if st.button("✗ Kein Match — FocalX hat diesen Schaden nicht gefunden"
                             + (" (gewählt)" if none_current else ""),
                             key=f"none_{sel}_{gt_key}", disabled=none_current):
                    save_review(r["checkin"], gt_key, [], ai_keys, ai_available=ai_avail)
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
                        st.rerun()
                else:
                    if st.button("↩️ Wieder aufnehmen",
                                 key=f"unexcl_{sel}_{gt_key}", use_container_width=True):
                        rev_all = load_review(r["checkin"])
                        rev_all.pop(gt_key, None)
                        review_file(r["checkin"]).write_text(json.dumps(rev_all, indent=2))
                        st.rerun()

# ════════════════════════════════════════════════════════════════════════════
else:
    st.title("📈 Metriken")
    st.caption("Zwei getrennte Messgrößen — beide auf Basis deiner Reviews (Gold-Standard).")
    rev_files = sorted(REVIEWS.glob("*.json")) if REVIEWS.exists() else []
    if not rev_files:
        st.info("Noch keine Reviews — erst im Review-Modus Schäden bestätigen/mappen.")
        st.stop()

    total = confirmed = confirmed_empty = corrected = rejected = human_added = 0
    manual_only = 0
    ai_total = 0                       # nur Reviews, bei denen die AI mitspielte
    gt_matched = 0
    per_checkin = []
    excluded_total = 0
    skipped_cars = 0
    for f in rev_files:
        rev_raw = json.loads(f.read_text())
        if not review_done(rev_raw):
            skipped_cars += 1
            continue                          # nur abgeschlossene Autos
        dmg = review_damages(rev_raw)
        excluded_total += sum(1 for v in dmg.values() if v["verdict"] == "excluded")
        rev = {k: v for k, v in dmg.items() if v["verdict"] != "excluded"}
        n = len(rev)
        c_ok = sum(1 for v in rev.values() if v["verdict"] == "confirmed")
        c_ok_e = sum(1 for v in rev.values() if v["verdict"] == "confirmed_empty")
        c_corr = sum(1 for v in rev.values() if v["verdict"] == "corrected")
        c_rej = sum(1 for v in rev.values() if v["verdict"] == "rejected")
        c_add = sum(1 for v in rev.values() if v["verdict"] == "human_added")
        c_man = sum(1 for v in rev.values() if v["verdict"] == "manual_only")
        c_match = sum(1 for v in rev.values() if v["human"])
        total += n; confirmed += c_ok; confirmed_empty += c_ok_e
        corrected += c_corr; rejected += c_rej; human_added += c_add
        manual_only += c_man
        ai_total += n - c_man
        gt_matched += c_match
        per_checkin.append({
            "Check-in": f.stem, "Reviewt": n,
            "AI korrekt": c_ok + c_ok_e, "Korrigiert": c_corr + c_rej + c_add,
            "Nur manuell (AI lief nicht)": c_man,
            "FocalX-Treffer (validiert)": c_match,
        })

    st.caption(f"Basis: nur Autos mit ✔️-Abschluss-Haken"
               + (f" — {skipped_cars} Auto(s) mit Reviews, aber ohne Haken, zählen noch nicht." if skipped_cars else "."))
    st.header("1 · FocalX-Detection (validiert)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Reviewte DB-Schäden", total)
    c2.metric("Von FocalX gefunden (validiert)", gt_matched)
    c3.metric("Validierter Recall", f"{gt_matched / total:.0%}" if total else "–")
    c4.metric("🚫 Ausgeschlossen", excluded_total,
              help="Vom Reviewer aus der Statistik genommen (mit Grund geloggt)")

    st.header("2 · AI-Mapping-Qualität")
    st.caption(f"Basis: {ai_total} Reviews mit AI-Vorschlag "
               f"({manual_only} rein manuelle Mappings auf ungemappten Autos zählen hier nicht).")
    ai_ok = confirmed + confirmed_empty
    manual = corrected + rejected + human_added
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("AI korrekt", ai_ok, help="AI-Vorschlag exakt bestätigt (inkl. korrekt 'kein Match')")
    c2.metric("Manuell nötig", manual, help="korrigiert / abgelehnt / vom Menschen ergänzt")
    c3.metric("AI-Genauigkeit", f"{ai_ok / ai_total:.0%}" if ai_total else "–")
    c4.metric("Aufschlüsselung", f"✏️{corrected} ✗{rejected} ➕{human_added}")

    st.dataframe(pd.DataFrame(per_checkin), use_container_width=True, hide_index=True)

    # ── 3 · Aufschlüsselung nach Größe & Schwere ────────────────────────────
    st.header("3 · Gefunden nach Größe & Schwere")
    st.caption("Basis: alle Autos mit komplettem AI-Scan. Wo dein Review vorliegt, "
               "zählt das menschliche Urteil, sonst der AI-Match. "
               "🚫-ausgeschlossene Schäden zählen nicht.")

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

    size_stat: dict[str, list[int]] = {}
    depth_stat: dict[str, list[int]] = {}
    basis_cars = basis_damages = 0
    for r in data:
        if not ai_scan_done(r):
            continue
        basis_cars += 1
        rev = load_review(r["checkin"])
        truths_r = {str(t["damage_id"]): t for t in r["truths"]}
        rep = repaired_ids(plate_key(r["plate"])) | late_ids(plate_key(r["plate"]), r["checkin"])
        for cp in (r.get("physical") or {}).get("cluster_pairs") or []:
            gt_key = "+".join(sorted(cp["damage_ids"]))
            rv = rev.get(gt_key)
            if rv and rv.get("verdict") == "excluded":
                continue
            if all(d in rep for d in cp["damage_ids"]):
                continue   # repariert oder erst nach den Fotos erfasst
            if rv:
                found = bool(rv.get("human"))
            else:
                found = cp.get("via") == "ai" and bool(cp.get("finding_clusters"))
            sev = (truths_r.get(cp["damage_ids"][0]) or {}).get("severity")
            basis_damages += 1
            for bucket, stat in ((size_bucket(sev), size_stat),
                                 (depth_bucket(sev), depth_stat)):
                g, t = stat.get(bucket, (0, 0))
                stat[bucket] = [g + int(found), t + 1]

    st.caption(f"{basis_damages} physische DB-Schäden aus {basis_cars} Autos")

    def bucket_df(stat: dict, order: list[str], label: str) -> pd.DataFrame:
        rows = [{label: b, "Gefunden": stat[b][0], "Gesamt": stat[b][1],
                 "Recall": stat[b][0] / stat[b][1]}
                for b in order if b in stat]
        return pd.DataFrame(rows)

    col_s, col_d = st.columns(2)
    with col_s:
        st.subheader("Nach Größe")
        dfs = bucket_df(size_stat, SIZE_ORDER, "Größe")
        st.dataframe(dfs.style.format({"Recall": "{:.0%}"})
                     .background_gradient(subset=["Recall"], cmap="RdYlGn", vmin=0, vmax=1),
                     use_container_width=True, hide_index=True)
    with col_d:
        st.subheader("Nach Schwere / Tiefe")
        dfd = bucket_df(depth_stat, DEPTH_ORDER, "Schwere")
        st.dataframe(dfd.style.format({"Recall": "{:.0%}"})
                     .background_gradient(subset=["Recall"], cmap="RdYlGn", vmin=0, vmax=1),
                     use_container_width=True, hide_index=True)
