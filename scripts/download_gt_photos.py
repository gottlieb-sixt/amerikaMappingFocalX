#!/usr/bin/env python3
"""Lädt die Original-Schadensfotos aus den Ground-Truth-Antworten herunter.

Struktur: case(Feld 2 der Antwort) → damage(31) → coordinate(20) → photos(9),
Foto: 1=photo_id · 2=presigned URL (30 min gültig!) · 8=s3_key.

Ablage: data/gt_photos/<PLATEKEY>/<damage_number>_<i>.jpg  (resumable)

Nutzung:
  python3 scripts/download_gt_photos.py            # aus vorhandenen GT-JSONs
  python3 scripts/download_gt_photos.py --refetch  # GT frisch ziehen (Token!)
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
GT = ROOT / "data" / "ground_truth"
OUT = ROOT / "data" / "gt_photos"
TOKEN_F = ROOT / ".lynx_token"
TOKEN_TTL_S = 240      # Lynx-Token hält 5 min — mit Sicherheitsabstand
TOKEN_WAIT_MAX_S = 3600


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _key(plate) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(plate)).upper()


def checkin_plates() -> dict[str, str]:
    """Entschärfter Schlüssel → Kennzeichen, aus den Check-in-Ordnern."""
    out: dict[str, str] = {}
    if RAW.exists():
        for day in RAW.iterdir():
            if day.is_dir():
                for c in day.iterdir():
                    if c.is_dir():
                        out.setdefault(_key(c.name.split("__")[0]),
                                       c.name.split("__")[0])
    return out


def jobs_for(path: Path):
    key = path.stem
    data = json.loads(path.read_text())
    for case in _as_list(data.get("2")):
        if not isinstance(case, dict):
            continue
        for d in _as_list(case.get("31")):
            if not isinstance(d, dict):
                continue
            dmg_no = str(d.get("3") or d.get("1"))
            for coord in _as_list(d.get("20")):
                if not isinstance(coord, dict):
                    continue
                for i, ph in enumerate(_as_list(coord.get("9"))):
                    if isinstance(ph, dict) and str(ph.get("2", "")).startswith("http"):
                        yield OUT / key / f"{dmg_no}_{i}.jpg", ph["2"]


def download(job):
    dest, url = job
    if dest.exists() and dest.stat().st_size > 1000:
        return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # S3 wirft bei 16 parallelen Verbindungen vereinzelt „connection reset" —
    # zwei Wiederholungen retten das, ein Neulauf wäre sonst nötig.
    last = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r:
                dest.write_bytes(r.read())
            return "ok"
        except Exception as e:
            last = str(e)[:60]
            time.sleep(1.5 * (attempt + 1))
    return f"FAIL {last}"


def _token_age() -> float:
    return time.time() - TOKEN_F.stat().st_mtime if TOKEN_F.exists() else 1e9


def token_now(what: str, newer_than: float = 0.0) -> tuple[str, float] | None:
    """Frischen Token liefern, notfalls auf einen neuen warten.

    Ein Lauf über hunderte Kennzeichen sprengt jedes 5-Minuten-Fenster. Statt
    mitten im Abruf zu scheitern, hält er hier an und macht weiter, sobald ein
    neuer Token in .lynx_token liegt. `newer_than` verlangt einen echt neueren
    Token als den angegebenen — sonst würde ein gerade abgelaufener, dateiseitig
    aber noch „frischer" Token endlos wiederverwendet."""
    asked = False
    deadline = time.time() + TOKEN_WAIT_MAX_S
    while True:
        if TOKEN_F.exists():
            mtime = TOKEN_F.stat().st_mtime
            if mtime > newer_than and time.time() - mtime < TOKEN_TTL_S:
                if asked:
                    print("   Token erkannt, weiter.", flush=True)
                return TOKEN_F.read_text().strip(), mtime
        if not asked:
            print(f"\n🔑 Frischer Lynx-Token nötig ({what}).\n"
                  "   Token in .lynx_token ablegen — ich mache dann von selbst "
                  "weiter.", flush=True)
            asked = True
        if time.time() > deadline:
            print("   Kein Token gekommen — Abbruch.", flush=True)
            return None
        time.sleep(5)


def refetch_plate(fetch, plate: str, stale_mtime: float) -> tuple[str, float]:
    """Ein Kennzeichen frisch ziehen → ("ok"|"fail"|"no-token", stale_mtime).

    Läuft der Token mitten im Aufruf ab, wirft fetch() SystemExit. Dann wartet
    dieser Aufruf auf einen echt neueren Token und wiederholt dasselbe
    Kennzeichen, statt es zu verlieren."""
    while True:
        got = token_now(f"GT-Fotos {plate}", newer_than=stale_mtime)
        if not got:
            return "no-token", stale_mtime
        token, mtime = got
        try:
            fetch(token, plate)
            return "ok", stale_mtime
        except SystemExit:
            print("  Token mitten im Abruf abgelaufen — warte auf einen neuen.",
                  flush=True)
            stale_mtime = mtime
        except Exception as e:
            print(f"  ✗ refetch {plate}: {str(e)[:80]}", flush=True)
            return "fail", stale_mtime


def run_jobs(jobs: list) -> tuple[int, int, list[str]]:
    ok = skip = 0
    fails: list[str] = []
    if not jobs:
        return 0, 0, fails
    with cf.ThreadPoolExecutor(16) as ex:
        for res in ex.map(download, jobs):
            if res == "ok":
                ok += 1
            elif res == "skip":
                skip += 1
            else:
                fails.append(res)
    return ok, skip, fails


def main() -> int:
    bad_plates: list[str] = []
    stale_mtime = 0.0
    if "--refetch" in sys.argv:
        from fetch_damages import fetch
        known = checkin_plates()
        for f in sorted(GT.glob("*.json")):
            # Maßgeblich ist der Dateiname: Feld 3 im Fall trägt das Kennzeichen
            # ZUR ZEIT DES SCHADENS, und US-Autos werden umgemeldet. Danach zu
            # fetchen holte das falsche Auto — die Datei behielt ihre abgelaufenen
            # URLs und ihre Fotos waren nie zu laden.
            plate = known.get(f.stem.upper())
            if not plate:
                cases = [c for c in _as_list(json.loads(f.read_text()).get("2"))
                         if isinstance(c, dict)]
                match = [c.get("3") for c in cases
                         if _key(c.get("3")) == f.stem.upper()]
                plate = (match or [c.get("3") for c in cases] or [None])[0]
            if not plate or not _key(plate):
                continue
            # nur re-fetchen, wenn Fotos fehlen
            needed = [j for j in jobs_for(f) if not j[0].exists()]
            if not needed:
                continue
            state, stale_mtime = refetch_plate(fetch, str(plate), stale_mtime)
            if state == "no-token":
                break
            if state == "fail":
                bad_plates.append(str(plate))
                continue
            # Frische URLs sofort einlösen: presigned Links halten nur ~30 min,
            # und bei Token-Wartezeiten wäre ein Sammel-Download am Ende zu spät.
            got, _, fails = run_jobs([j for j in jobs_for(f) if not j[0].exists()])
            note = f" · {len(fails)} Fehler" if fails else ""
            print(f"refetch {f.stem} ({plate}) · {got} Fotos{note}", flush=True)
            time.sleep(0.3)

    jobs = []
    for f in sorted(GT.glob("*.json")):
        jobs.extend(jobs_for(f))
    print(f"{len(jobs)} Schadensfotos referenziert")
    ok, skip, fails = run_jobs(jobs)
    print(f"FERTIG: {ok} geladen, {skip} übersprungen, {len(fails)} Fehler")
    if fails:
        print("Beispiel-Fehler:", fails[0])
    if bad_plates:
        print(f"Kennzeichen ohne frische URLs ({len(bad_plates)}): "
              f"{', '.join(bad_plates[:10])}")
    return 1 if bad_plates else 0


if __name__ == "__main__":
    sys.exit(main())
