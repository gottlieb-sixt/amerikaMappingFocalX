#!/usr/bin/env python3
"""Lädt alle Check-in-Fotos aus den data/lynx_raw/*.json Abrufen herunter.

Struktur der Antwort (Feldnummern):
  1[] = Check-ins:  1=Kennzeichen  2=checkin_id  3=Zeitstempel
                    7={1: s3key, 2: url}          (Einzelfoto, z. B. Tacho)
                    11[] = {1: Position, 2: {1: s3key, 2: url}}   (19-Bilder-Flow)

Ablage: data/raw/<datum>/<PLATE>__<checkin8>/<POSITION>.jpg
Resumable: vorhandene Dateien werden übersprungen. Presigned-URLs leben nur
15 min — nach Ablauf einfach frisch fetchen (lynx_fetch.py) und erneut laufen.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "lynx_raw"
OUT = ROOT / "data" / "raw"
WORKERS = 24


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def jobs_from_file(path: Path):
    day = path.stem.split("_", 1)[1]
    data = json.loads(path.read_text())
    for ci in as_list(data.get("1")):
        if not isinstance(ci, dict):
            continue
        plate = re.sub(r"[^A-Za-z0-9-]", "", str(ci.get("1", "?")))
        cid = str(ci.get("2", "?"))[:8]
        folder = OUT / day / f"{plate}__{cid}"
        photo7 = ci.get("7")
        if isinstance(photo7, dict) and str(photo7.get("2", "")).startswith("http"):
            yield folder / "extra_0.jpg", photo7["2"]
        for slot in as_list(ci.get("11")):
            if not isinstance(slot, dict):
                continue
            pos = re.sub(r"[^A-Za-z0-9_-]", "", str(slot.get("1", "unknown")))
            img = slot.get("2")
            if isinstance(img, dict) and str(img.get("2", "")).startswith("http"):
                yield folder / f"{pos}.jpg", img["2"]


def download(job):
    dest, url = job
    if dest.exists() and dest.stat().st_size > 1000:
        return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as r:
            dest.write_bytes(r.read())
        return "ok"
    except Exception as e:
        return f"FAIL {dest.name}: {str(e)[:80]}"


def main():
    files = sorted(RAW.glob("*.json"))
    if len(sys.argv) > 1:
        files = [f for f in files if any(a in f.name for a in sys.argv[1:])]
    all_jobs = []
    for f in files:
        all_jobs.extend(jobs_from_file(f))
    print(f"{len(all_jobs)} Fotos aus {len(files)} Abrufen")
    ok = skip = fail = 0
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for res in ex.map(download, all_jobs):
            if res == "ok":
                ok += 1
            elif res == "skip":
                skip += 1
            else:
                fail += 1
                if fail <= 10:
                    print(" ", res)
    print(f"FERTIG: {ok} geladen, {skip} übersprungen, {fail} Fehler")


if __name__ == "__main__":
    main()
