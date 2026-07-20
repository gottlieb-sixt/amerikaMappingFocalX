#!/usr/bin/env python3
"""Ground Truth: GetDamageCasesByVehicle über das Lynx gRPC-web-Gateway.

Request-Message (Feldnummern werden automatisch ermittelt und gecacht):
  license_plate (string) + fetch_all_damage_cases (bool=true); alle anderen
  Felder bleiben auf Default und können im Protobuf weggelassen werden.

Nutzung:
  python3 scripts/fetch_damages.py "M  -KJ 2666"     # eine Platte
  python3 scripts/fetch_damages.py --all             # alle Platten aus data/raw/
Ablage: data/ground_truth/<PLATE_NORMALISIERT>.{bin,json}
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lynx_fetch import _string_field, _tag, _varint, call, decode_message  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ground_truth"
CFG = ROOT / "data" / "ground_truth_fields.json"

METHOD = "com.sixt.service.vehicle_damage.api.VehicleDamage/GetDamageCasesByVehicle"

# Kandidaten (plate_field, fetch_all_field) — JSON-Reihenfolge legt (2,6) nahe.
FIELD_CANDIDATES = [(2, 6), (1, 5), (2, 7), (1, 6), (3, 7), (2, 5), (4, 8), (2, 9)]


def bool_field(field: int, value: bool) -> bytes:
    return _tag(field, 0) + _varint(1 if value else 0)


def encode(plate: str, plate_f: int, fetch_f: int) -> bytes:
    return _string_field(plate_f, plate) + bool_field(fetch_f, True)


def looks_like_damage_response(decoded) -> bool:
    """Gültig = Schadensdaten ODER eine leere-aber-strukturierte Antwort
    (Auto ohne erfasste Schäden: nur der Meta-Block, Feld 1)."""
    if not decoded:
        return False
    text = json.dumps(decoded, default=str)
    if any(k in text for k in ("damage", "CLOSED", "SEVERITY", "TYPE_", "case", "SIDE")):
        return True
    # Meta-only: {"1": {…}} ohne Fallliste → valide 0-Schäden-Antwort
    return isinstance(decoded.get("1"), dict) and "2" not in decoded


def fetch(token: str, plate: str) -> dict | None:
    candidates = FIELD_CANDIDATES
    if CFG.exists():
        known = tuple(json.loads(CFG.read_text()))
        candidates = [known] + [c for c in FIELD_CANDIDATES if c != known]
    import urllib.request  # for patching METHOD into lynx_fetch.call
    import lynx_fetch
    lynx_fetch.METHOD = METHOD
    for plate_f, fetch_f in candidates:
        try:
            payload, _ = call(token, encode(plate, plate_f, fetch_f))
        except Exception as e:
            if any(t in str(e).lower() for t in ("expired", "jwt", "401", "unauthorized")):
                raise SystemExit("TOKEN ABGELAUFEN — mit frischem Token erneut starten (resumable)")
            print(f"  Felder ({plate_f},{fetch_f}): {str(e)[:100]}")
            continue
        decoded = decode_message(payload) or {}
        if payload and looks_like_damage_response(decoded):
            if not CFG.exists():
                CFG.parent.mkdir(parents=True, exist_ok=True)
                CFG.write_text(json.dumps([plate_f, fetch_f]))
                print(f"  Feldnummern erkannt: plate={plate_f}, fetch_all={fetch_f}")
            key = re.sub(r"[^A-Za-z0-9]", "", plate).upper()
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / f"{key}.bin").write_bytes(payload)
            (OUT / f"{key}.json").write_text(json.dumps(decoded, indent=1, default=str))
            return decoded
        print(f"  Felder ({plate_f},{fetch_f}): leer/unpassend ({len(payload)} bytes)")
    return None


def db_plate_variants(folder_plate: str) -> list[str]:
    """'FL-DX29HV' → Varianten wie 'FL -DX 29HV', 'FL-DX 29HV', …"""
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


def main():
    token = (ROOT / ".lynx_token").read_text().strip()
    if sys.argv[1:] == ["--all"]:
        plates = sorted({p.name.split("__")[0] for p in (ROOT / "data" / "raw").glob("*/*/")})
    else:
        plates = sys.argv[1:]
    print(f"{len(plates)} Kennzeichen")
    ok = fail = 0
    for plate in plates:
        key = re.sub(r"[^A-Za-z0-9]", "", plate).upper()
        if (OUT / f"{key}.json").exists():
            ok += 1
            continue
        got = None
        for variant in db_plate_variants(plate):
            print(f"{plate} → versuche '{variant}'")
            got = fetch(token, variant)
            if got:
                ncases = len(got.get("2", got.get("1", []) if isinstance(got.get("1"), list) else []))
                print(f"  OK ({key})")
                break
            time.sleep(0.5)
        if got:
            ok += 1
        else:
            fail += 1
            print(f"  KEINE DATEN: {plate}")
        time.sleep(0.5)
    print(f"FERTIG: {ok} ok, {fail} ohne Daten")


if __name__ == "__main__":
    main()
