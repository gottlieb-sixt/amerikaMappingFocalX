#!/usr/bin/env python3
"""GetDamageCasesByVehicle ERNEUT holen, diesmal mit include_pictogram=true.

Nutzt das gRPC-JSON-Transcoding des Lynx-Gateways (Content-Type: application/json)
— dieselbe Methode wie damage_benchmarking/getDamageCases.py, mit BENANNTEN
Feldern (kein handgerolltes Protobuf nötig). Die volle JSON-Antwort inklusive
Pictogram wird abgelegt.

WICHTIG: schreibt NICHT nach data/ground_truth/ (das ist der eingefrorene
GT-Stand), sondern nach data/ground_truth_pictogram/<KEY>.json.

Token: .lynx_token (Projektroot), nur ~5 min gültig → direkt nach Anfordern
starten. Resümierbar: vorhandene <KEY>.json werden übersprungen.

Nutzung:
  python3 scripts/fetch_pictograms.py            # die 20 Gold-Autos (Standard)
  python3 scripts/fetch_pictograms.py --all      # alle Platten aus data/raw/
  python3 scripts/fetch_pictograms.py "FL-07ELXT"  # einzelne Platte(n)
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ground_truth_pictogram"
GOLD = ROOT / "gold" / "mapping_gold.json"
TOKEN_FILE = ROOT / ".lynx_token"

URL = ("https://grpc-query-tool-prod.orange.sixt.com/"
       "com.sixt.service.vehicle_damage.api.VehicleDamage/GetDamageCasesByVehicle")
# Browser-UA nötig, sonst blockt Cloudflare mit Error 1010 (browser_signature_banned).
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate).upper()


def plate_variants(folder_plate: str) -> list[str]:
    """'FL-DX29HV' → Varianten wie 'FL -DX 29HV', 'FL-DX 29HV', … (DB-Schreibweise
    ist uneinheitlich; wir probieren mehrere durch)."""
    raw = folder_plate.strip().upper()
    m = re.match(r"^([A-ZÄÖÜ]{1,3})-?([A-Z]{1,2})\s?(\w+)$", raw)
    out = [raw]
    if m:
        a, b, c = m.groups()
        out += [f"{a:<3}-{b} {c}", f"{a}-{b} {c}", f"{a} -{b} {c}", f"{a}{b}{c}"]
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def call(token: str, plate: str) -> dict:
    body = json.dumps({
        "license_plate": plate,
        "include_repaired_damages": False,
        "fetch_all_damage_cases": True,
        "include_pictogram": True,
        "include_default_damages": False,
        "include_inactive_damages": False,
    }).encode()
    req = urllib.request.Request(URL, method="POST", data=body)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://lynx.orange.sixt.com")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def gold_plates() -> list[str]:
    gold = json.loads(GOLD.read_text())
    return [c.get("plate", "") for c in gold.get("cars", []) if c.get("plate")]


def main() -> None:
    if not TOKEN_FILE.exists():
        sys.exit("Kein .lynx_token — frischen Lynx-Token dort ablegen (gilt ~5 min).")
    token = TOKEN_FILE.read_text().strip()

    args = sys.argv[1:]
    if args == ["--all"]:
        plates = sorted({p.name.split("__")[0] for p in (ROOT / "data" / "raw").glob("*/*/")})
    elif args:
        plates = args
    else:
        plates = gold_plates()

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{len(plates)} Kennzeichen · include_pictogram=true → {OUT.relative_to(ROOT)}/")
    ok = skip = fail = 0
    for plate in plates:
        key = _key(plate)
        dest = OUT / f"{key}.json"
        if dest.exists():
            skip += 1
            continue
        got = None
        for variant in plate_variants(plate):
            try:
                d = call(token, variant)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    sys.exit(f"\nTOKEN ABGELAUFEN/UNGÜLTIG ({e.code}) — frischen Token "
                             f"in .lynx_token legen und erneut starten (resümierbar). "
                             f"Stand: {ok} ok, {skip} übersprungen.")
                continue
            except Exception as e:
                print(f"  {plate} '{variant}': {str(e)[:100]}")
                continue
            if d.get("damage_cases"):
                got = d
                break
        if got is not None:
            dest.write_text(json.dumps(got, indent=1, ensure_ascii=False))
            n_pic = json.dumps(got).lower().count("pictogram")
            print(f"  OK {key}: {len(got['damage_cases'])} cases (pictogram-Treffer: {n_pic})")
            ok += 1
        else:
            print(f"  KEINE DATEN: {plate}")
            fail += 1
        time.sleep(0.3)
    print(f"\nFERTIG: {ok} neu · {skip} übersprungen · {fail} ohne Daten")


if __name__ == "__main__":
    main()
