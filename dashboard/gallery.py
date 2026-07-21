"""Zoombare Bild-Galerie als selbstständige HTML-Komponente für das Dashboard.

Klick auf ein Thumbnail → Vollflächen-Lightbox im Panel mit Mausrad-Zoom,
Ziehen zum Verschieben, Doppelklick/Esc/Klick-daneben zum Schließen.
Bilder werden als data-URI eingebettet (lokales Dashboard, überschaubare Mengen).
"""
from __future__ import annotations

import base64
import html
from pathlib import Path


def _data_uri(path: Path) -> str | None:
    try:
        b = path.read_bytes()
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


def thumb(path: Path, caption: str = "", size: int = 150) -> str:
    uri = _data_uri(path)
    if not uri:
        return ""
    cap = html.escape(caption)
    return (
        f'<figure class="thumb">'
        f'<img src="{uri}" loading="lazy" data-cap="{cap}" '
        f'style="height:{size}px" onclick="lb(this)">'
        f'<figcaption>{cap}</figcaption></figure>'
    )


def info_table(title: str, badge: str, rows: list[tuple[str, str]], accent: str) -> str:
    trs = "".join(
        f"<tr><td class='k'>{html.escape(k)}</td><td>{html.escape(str(v) if v is not None else '–')}</td></tr>"
        for k, v in rows
    )
    return (
        f'<div class="info">'
        f'<div class="title"><span class="dot" style="background:{accent}"></span>{html.escape(title)}'
        f'<span class="badge" style="border-color:{accent};color:{accent}">{html.escape(badge)}</span></div>'
        f'<table>{trs}</table></div>'
    )


def note(text: str) -> str:
    return f'<div class="note">{html.escape(text)}</div>' if text else ""


def card(*blocks: str) -> str:
    return '<div class="card">' + "".join(blocks) + "</div>"


def column(*blocks: str) -> str:
    return '<div class="col">' + "".join(b for b in blocks if b) + "</div>"


def imgrow(*thumbs: str) -> str:
    inner = "".join(t for t in thumbs if t)
    if not inner:
        inner = '<span class="noimg">— kein Bild —</span>'
    return f'<div class="imgrow">{inner}</div>'


def render(cards: list[str], height: int = 820) -> str:
    body = "".join(cards) or '<div class="empty">Keine Einträge.</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,Roboto,sans-serif; color:#1a1a1a; background:#fafafa; }}
.wrap {{ padding:4px 2px 40px; }}
.card {{ display:flex; gap:22px; padding:16px 12px; border-bottom:1px solid #eee; }}
.col {{ flex:1; min-width:0; }}
.info .title {{ font-weight:800; font-size:15px; display:flex; align-items:center; gap:8px; margin-bottom:8px; }}
.dot {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
.badge {{ font-size:10px; font-weight:800; letter-spacing:.5px; border:1.5px solid; border-radius:20px; padding:2px 9px; margin-left:auto; }}
.info table {{ border-collapse:collapse; font-size:13px; width:100%; }}
.info td {{ padding:3px 8px 3px 0; vertical-align:top; }}
.info td.k {{ color:#888; font-weight:600; white-space:nowrap; width:130px; }}
.imgrow {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
.thumb {{ margin:0; cursor:zoom-in; }}
.thumb img {{ border-radius:8px; border:1px solid #ddd; display:block; transition:transform .1s; }}
.thumb img:hover {{ transform:scale(1.03); box-shadow:0 4px 14px rgba(0,0,0,.18); }}
.thumb figcaption {{ font-size:10px; color:#999; text-align:center; margin-top:3px; max-width:150px; }}
.note {{ font-size:12px; color:#555; background:#f0f0f0; border-radius:8px; padding:6px 10px; margin-top:8px; }}
.noimg,.empty {{ color:#bbb; font-size:13px; padding:10px; }}
#lb {{ position:fixed; inset:0; background:rgba(0,0,0,.92); display:none; align-items:center; justify-content:center;
      z-index:999; overflow:hidden; cursor:grab; }}
#lb.on {{ display:flex; }}
#lb img {{ max-width:92%; max-height:88%; transform-origin:center; user-select:none; -webkit-user-drag:none; }}
#lbcap {{ position:absolute; bottom:14px; left:0; right:0; text-align:center; color:#eee; font-size:13px; }}
#lbx {{ position:absolute; top:12px; right:18px; color:#fff; font-size:30px; cursor:pointer; font-weight:300; line-height:1; }}
#lbhint {{ position:absolute; top:14px; left:18px; color:#aaa; font-size:11px; }}
</style></head><body>
<div class="wrap">{body}</div>
<div id="lb"><span id="lbhint">Mausrad: Zoom · Ziehen: verschieben · Doppelklick: Reset · Esc: schließen</span>
<span id="lbx">×</span><img id="lbimg"><div id="lbcap"></div></div>
<script>
const lbEl=document.getElementById('lb'), im=document.getElementById('lbimg'), cap=document.getElementById('lbcap');
let s=1,tx=0,ty=0,drag=false,px=0,py=0;
function upd(){{im.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{s}})`;}}
function lb(t){{s=1;tx=0;ty=0;upd();im.src=t.src;cap.textContent=t.dataset.cap||'';lbEl.classList.add('on');}}
function close(){{lbEl.classList.remove('on');}}
document.getElementById('lbx').onclick=close;
lbEl.addEventListener('click',e=>{{if(e.target===lbEl)close();}});
lbEl.addEventListener('wheel',e=>{{e.preventDefault();s=Math.min(8,Math.max(1,s*(e.deltaY<0?1.12:0.89)));if(s===1){{tx=0;ty=0;}}upd();}},{{passive:false}});
im.addEventListener('mousedown',e=>{{drag=true;px=e.clientX-tx;py=e.clientY-ty;lbEl.style.cursor='grabbing';e.preventDefault();}});
window.addEventListener('mousemove',e=>{{if(drag){{tx=e.clientX-px;ty=e.clientY-py;upd();}}}});
window.addEventListener('mouseup',()=>{{drag=false;lbEl.style.cursor='grab';}});
im.addEventListener('dblclick',()=>{{s=1;tx=0;ty=0;upd();}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')close();}});
</script></body></html>"""
