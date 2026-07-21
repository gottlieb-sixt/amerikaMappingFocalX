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

mode = st.sidebar.radio("Modus", ["📊 Ergebnisse", "🔍 Review / manuelles Mapping", "📈 Metriken"])

GREEN, RED, ORANGE, BLUE = "#2e9e5b", "#d0433b", "#e8802a", "#3479c4"


# ── Hilfen ──────────────────────────────────────────────────────────────────

def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate).upper()


def gt_images(key: str, damage_id: str) -> list[Path]:
    return sorted((GT_PHOTOS / key).glob(f"{damage_id}_*.jpg"))


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
    ph = r.get("physical") or {}
    fcl = finding_clusters_of(r)
    for cp in ph.get("cluster_pairs") or []:
        if set(cp["damage_ids"]) == set(dmg_ids):
            return sorted(k for ci in cp.get("finding_clusters", []) for k in fcl[ci])
    return []


def review_file(checkin: str) -> Path:
    return REVIEWS / f"{checkin}.json"


def load_review(checkin: str) -> dict:
    f = review_file(checkin)
    return json.loads(f.read_text()) if f.exists() else {}


def save_review(checkin: str, gt_key: str, human: list[str], ai: list[str],
                ai_available: bool = True) -> None:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    rev = load_review(checkin)
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
    rows = [{
        "Kennzeichen": r["plate"],
        "Check-in": r["checkin"].split("__")[1],
        "Schäden (DB)": r["ground_truth_total"],
        "Physisch (DB)": (r.get("physical") or {}).get("gt_total"),
        "Gefunden (physisch)": (r.get("physical") or {}).get("gt_found"),
        "Recall": (r.get("physical") or {}).get("recall", r["recall"]),
        "Neue Schäden (unique)": (r.get("physical") or {}).get("extras_unique",
                                                               len(r["extra_findings"])),
        "Status": "⏳ Mapping ausstehend" if r.get("mapping_pending") else "gemappt",
        "Zeitpunkt": r["timestamp"],
    } for r in data]
    df = pd.DataFrame(rows)
    mapped = df[df["Status"] == "gemappt"]
    total_phys = int(mapped["Physisch (DB)"].fillna(0).sum())
    total_found = int(mapped["Gefunden (physisch)"].fillna(0).sum())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Check-ins", len(df))
    c2.metric("Physische Schäden (DB, gemappt)", total_phys)
    c3.metric("Davon gefunden", total_found)
    c4.metric("Neue Schäden (unique)", int(mapped["Neue Schäden (unique)"].fillna(0).sum()))
    c5.metric("Recall (physisch)", f"{total_found / total_phys:.0%}" if total_phys else "–")
    st.dataframe(
        df.style.background_gradient(subset=["Recall"], cmap="RdYlGn", vmin=0, vmax=1),
        use_container_width=True, hide_index=True,
    )

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
    _tot = _fx_ok = _ai_base = _ai_ok = 0
    if REVIEWS.exists():
        for _f in REVIEWS.glob("*.json"):
            for _v in json.loads(_f.read_text()).values():
                _tot += 1
                if _v.get("human"):
                    _fx_ok += 1                       # FocalX hat den Schaden wirklich
                if _v.get("ai_available", True) and _v.get("verdict") != "manual_only":
                    _ai_base += 1
                    if _v.get("verdict") in ("confirmed", "confirmed_empty"):
                        _ai_ok += 1
    c1, c2, c3 = st.columns(3)
    c1.metric("Richtige Mappings FocalX (validiert)", f"{_fx_ok} / {_tot}",
              help="Reviewte DB-Schäden, für die FocalX laut DIR wirklich einen Fund hat")
    c2.metric("Richtige Mappings durch AI", f"{_ai_ok} / {_ai_base}",
              help="AI-Vorschläge, die du exakt bestätigt hast — sinkt relativ, wenn du korrigierst")
    c3.metric("AI-Genauigkeit", f"{_ai_ok / _ai_base:.0%}" if _ai_base else "–")
    st.divider()

    st.caption("Pro DB-Schaden: AI-Vorschlag prüfen (✓ bestätigen), anderen Fund wählen "
               "oder leer lassen. Alles wird geloggt und speist die Metriken.")
    sel = st.selectbox("Check-in", [r["checkin"] for r in data])
    r = next(x for x in data if x["checkin"] == sel)
    key = plate_key(r["plate"])
    truths = {str(t["damage_id"]): t for t in r["truths"]}
    findings = {f["key"]: f for f in r["findings"]}
    fcl = finding_clusters_of(r)
    gcl = gt_clusters_of(r)
    review = load_review(r["checkin"])

    done = sum(1 for ids in gcl if "+".join(sorted(ids)) in review)
    st.progress(done / len(gcl) if gcl else 1.0,
                text=f"{done}/{len(gcl)} Schäden reviewt")

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
        ai_keys = ai_choice_for(r, dmg_ids)
        ai_avail = not r.get("mapping_pending")
        status = ("✅" if rev and rev["verdict"].startswith("confirmed")
                  else "✏️" if rev else "⬜")

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
                        st.markdown(f"🧠 **AI:** {', '.join(ai_keys)}")
                    else:
                        st.markdown("🧠 **AI:** kein Match" + ("" if ai_avail else " (Mapping lief noch nicht)"))
                    if rev:
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
                            cu = ROOT / f0["closeup"] if f0.get("closeup") else None
                            if cu and cu.exists():
                                st.image(str(cu), use_container_width=True)
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
            none_current = rev is not None and not rev["human"]
            if st.button("✗ Kein Match — FocalX hat diesen Schaden nicht gefunden"
                         + (" (gewählt)" if none_current else ""),
                         key=f"none_{sel}_{gt_key}", disabled=none_current):
                save_review(r["checkin"], gt_key, [], ai_keys, ai_available=ai_avail)
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
    for f in rev_files:
        rev = json.loads(f.read_text())
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

    st.header("1 · FocalX-Detection (validiert)")
    c1, c2, c3 = st.columns(3)
    c1.metric("Reviewte DB-Schäden", total)
    c2.metric("Von FocalX gefunden (validiert)", gt_matched)
    c3.metric("Validierter Recall", f"{gt_matched / total:.0%}" if total else "–")

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
