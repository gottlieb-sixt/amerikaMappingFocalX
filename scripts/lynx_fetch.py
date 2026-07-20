#!/usr/bin/env python3
"""Ruft GetCheckinPhotosByBranch über das Lynx gRPC-web-Gateway ab — ohne
Protobuf-Library (Request wird von Hand encodiert, Response generisch decodiert).

Nutzung:
  python3 scripts/lynx_fetch.py 40139 2026-07-19            # ein Tag
  python3 scripts/lynx_fetch.py 40139 2026-07-19 --preview  # nur Struktur zeigen

Token in .lynx_token (Projektroot), ~5 min gültig.
Rohantworten landen in data/lynx_raw/<branch>_<datum>.bin + .json (dekodiert).
"""
from __future__ import annotations

import base64
import json
import re
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "lynx_raw"

GATEWAY = "https://grpc-query-tool-prod.orange.sixt.com"
METHOD = "com.sixt.service.operations_checkin.api.OperationsCheckin/GetCheckinPhotosByBranch"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# ── protobuf hand-rolling ───────────────────────────────────────────────────

def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _string_field(field: int, value: str) -> bytes:
    data = value.encode()
    return _tag(field, 2) + _varint(len(data)) + data


def encode_request(branch_id: str, start: str, end: str) -> bytes:
    """GetCheckinPhotosByBranchRequest: branch_id=1, start_time=2, end_time=3
    (Standard-Nummerierung in Deklarationsreihenfolge)."""
    return (_string_field(1, branch_id) +
            _string_field(2, start) +
            _string_field(3, end))


def decode_message(data: bytes, depth: int = 0):
    """Generic protobuf walker → nested python structure (best effort)."""
    out: dict = {}
    i = 0
    while i < len(data):
        try:
            key, i = _read_varint(data, i)
        except Exception:
            return None
        field, wire = key >> 3, key & 7
        if field == 0 or field > 10_000:
            return None
        try:
            if wire == 0:
                val, i = _read_varint(data, i)
            elif wire == 1:
                val = struct.unpack("<d", data[i:i + 8])[0]; i += 8
            elif wire == 5:
                val = struct.unpack("<f", data[i:i + 4])[0]; i += 4
            elif wire == 2:
                ln, i = _read_varint(data, i)
                chunk = data[i:i + ln]
                if len(chunk) != ln:
                    return None
                i += ln
                val = None
                if depth < 12 and ln > 1:
                    val = decode_message(chunk, depth + 1)
                if val is None:
                    try:
                        val = chunk.decode("utf-8")
                    except UnicodeDecodeError:
                        val = f"<bytes {ln}>"
            else:
                return None
        except Exception:
            return None
        out.setdefault(str(field), []).append(val)
    return {k: (v[0] if len(v) == 1 else v) for k, v in out.items()}


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        b = data[i]
        n |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            raise ValueError("varint overflow")


# ── grpc-web transport ──────────────────────────────────────────────────────

def call(token: str, message: bytes) -> tuple[bytes, dict]:
    """POST grpc-web-text; returns (concatenated data frames, trailer headers)."""
    frame = b"\x00" + struct.pack(">I", len(message)) + message
    body = base64.b64encode(frame)
    req = urllib.request.Request(f"{GATEWAY}/{METHOD}", method="POST", data=body)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/grpc-web-text")
    req.add_header("Accept", "application/grpc-web-text")
    req.add_header("X-Grpc-Web", "1")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        headers = dict(r.headers)
    if headers.get("grpc-status", "0") not in ("", "0"):
        raise RuntimeError(f"grpc-status {headers.get('grpc-status')}: {headers.get('grpc-message')}")
    # grpc-web-text: Base64 (ggf. in konkatenierten Segmenten)
    try:
        binary = base64.b64decode(raw, validate=False)
    except Exception:
        binary = b"".join(base64.b64decode(seg + b"=" * (-len(seg) % 4))
                          for seg in re.split(rb"(?<==)(?=[A-Za-z0-9+/])", raw))
    payload, trailers = b"", {}
    i = 0
    while i + 5 <= len(binary):
        flags = binary[i]
        ln = struct.unpack(">I", binary[i + 1:i + 5])[0]
        chunk = binary[i + 5:i + 5 + ln]
        i += 5 + ln
        if flags & 0x80:
            for line in chunk.decode(errors="replace").split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    trailers[k.strip().lower()] = v.strip()
        else:
            payload += chunk
    status = trailers.get("grpc-status", "0")
    if status not in ("", "0"):
        raise RuntimeError(f"grpc-status {status}: {trailers.get('grpc-message', '')}")
    return payload, trailers


# ── main ────────────────────────────────────────────────────────────────────

def fetch_day(token: str, branch: str, day: str) -> dict | None:
    msg = encode_request(branch, f"{day}T00:00:00", f"{day}T23:59:59")
    payload, _ = call(token, msg)
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"{branch}_{day}.bin").write_bytes(payload)
    decoded = decode_message(payload) or {}
    (RAW / f"{branch}_{day}.json").write_text(json.dumps(decoded, indent=1, default=str))
    return decoded


def collect_strings(node, out: list):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            collect_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_strings(v, out)


def main():
    branch = sys.argv[1] if len(sys.argv) > 1 else "40139"
    day = sys.argv[2] if len(sys.argv) > 2 else "2026-07-19"
    token = (ROOT / ".lynx_token").read_text().strip()
    print(f"Hole {branch} / {day} …")
    decoded = fetch_day(token, branch, day)
    strings: list[str] = []
    collect_strings(decoded, strings)
    urls = [s for s in strings if s.startswith("http")]
    plates = sorted({s for s in strings
                     if re.fullmatch(r"[A-ZÄÖÜ]{1,3}[- ]?[A-Z]{1,2}[ -]?\d{1,5}[EH]?", s.strip())})
    print(f"Antwort: {len(strings)} Strings, davon {len(urls)} URLs")
    if plates:
        print("  Kennzeichen:", ", ".join(plates[:15]))
    for u in urls[:5]:
        print("  URL:", u[:120])
    print("Struktur-Preview gespeichert:", RAW / f"{branch}_{day}.json")


if __name__ == "__main__":
    main()
