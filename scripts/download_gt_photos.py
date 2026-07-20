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
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
GT = ROOT / "data" / "ground_truth"
OUT = ROOT / "data" / "gt_photos"


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


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
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r:
            dest.write_bytes(r.read())
        return "ok"
    except Exception as e:
        return f"FAIL {str(e)[:60]}"


def main():
    if "--refetch" in sys.argv:
        from fetch_damages import fetch
        token = (ROOT / ".lynx_token").read_text().strip()
        import time
        for f in sorted(GT.glob("*.json")):
            data = json.loads(f.read_text())
            # Kennzeichen aus der Antwort selbst (Case-Feld 3), sonst überspringen
            cases = _as_list(data.get("2"))
            plate = cases[0].get("3") if cases and isinstance(cases[0], dict) else None
            if not plate:
                continue
            # nur re-fetchen, wenn Fotos fehlen
            needed = [j for j in jobs_for(f) if not j[0].exists()]
            if not needed:
                continue
            print(f"refetch {f.stem} ({plate})")
            fetch(token, str(plate))
            time.sleep(0.3)

    jobs = []
    for f in sorted(GT.glob("*.json")):
        jobs.extend(jobs_for(f))
    print(f"{len(jobs)} Schadensfotos referenziert")
    ok = skip = fail = 0
    fails = []
    with cf.ThreadPoolExecutor(16) as ex:
        for res in ex.map(download, jobs):
            if res == "ok":
                ok += 1
            elif res == "skip":
                skip += 1
            else:
                fail += 1
                fails.append(res)
    print(f"FERTIG: {ok} geladen, {skip} übersprungen, {fail} Fehler")
    if fails:
        print("Beispiel-Fehler:", fails[0])


if __name__ == "__main__":
    main()
