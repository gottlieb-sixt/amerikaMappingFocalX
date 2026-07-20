#!/usr/bin/env python3
"""Findet die echte API-Route hinter dem Lynx service-query-tool.

Nutzung:  python3 scripts/probe_lynx.py   (Token in .lynx_token im Projektroot)
Testet mehrere Routen-Varianten und zeigt Status + Response-Anfang.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = (ROOT / ".lynx_token").read_text().strip()

BASE = "https://lynx.orange.sixt.com"
SERVICE = "com.sixt.service.operations-checkin"
ENDPOINT = "com.sixt.service.operations_checkin.api.OperationsCheckin/GetCheckinPhotosByBranch"
BODY = {"branch_id": "40139", "start_time": "2026-07-19T00:00:00", "end_time": "2026-07-19T23:59:59"}

qs = urllib.parse.urlencode({"endpoint": ENDPOINT, "type": "grpc", "body": json.dumps(BODY)})

CANDIDATES = [
    ("GET UI-Route+JSON-Accept", "GET", f"{BASE}/service-query-tool/query/{SERVICE}?{qs}", None),
    ("GET api/query", "GET", f"{BASE}/service-query-tool/api/query/{SERVICE}?{qs}", None),
    ("POST api/query obj-body", "POST", f"{BASE}/service-query-tool/api/query/{SERVICE}",
     {"endpoint": ENDPOINT, "type": "grpc", "body": json.dumps(BODY)}),
    ("POST api/query full", "POST", f"{BASE}/service-query-tool/api/query",
     {"service": SERVICE, "endpoint": ENDPOINT, "type": "grpc", "body": json.dumps(BODY)}),
    ("POST api/v1/query", "POST", f"{BASE}/service-query-tool/api/v1/query/{SERVICE}",
     {"endpoint": ENDPOINT, "type": "grpc", "body": json.dumps(BODY)}),
    ("GET query JSON accept", "GET", f"{BASE}/api/service-query-tool/query/{SERVICE}?{qs}", None),
]

for name, method, url, body in CANDIDATES:
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            text = r.read().decode(errors="replace")
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
        text = e.read().decode(errors="replace")
    except Exception as e:
        code, text = "ERR", str(e)
    kind = "JSON" if str(text).lstrip().startswith(("{", "[")) else "HTML/other"
    print(f"\n=== {name} → {code} ({kind}) ===")
    print(str(text)[:300].replace("\n", " "))
    if code == 200 and kind == "JSON":
        out = ROOT / "data" / "lynx_probe_response.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(text)
        print(f"\n*** TREFFER — Response gespeichert: {out} ***")
        break
