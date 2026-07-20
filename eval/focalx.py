"""FocalX inspection client — battle-tested port of the VehicleCapture flow.

Two hard-won lessons are baked in (2026-07-20 debugging session):

1. POSITION LABELS ARE A FIXED VOCABULARY. Uploads whose `position` doesn't
   match a slot label from the tenant's `custom_data` catalogue are silently
   ignored by the AI pipeline — the inspection then "completes" with an EMPTY
   report. (The integration doc claims positions are free-form; that is wrong.)
   Use `valid_labels()` / VALID_WALKAROUND to check before uploading.

2. submitImages CAN BE A SILENT NO-OP when it races the server-side image
   ingestion (HTTP 200, but `is_images_submitted` stays false and the AI never
   starts). `_ensure_submitted` verifies the flag and re-submits until it flips.
"""
from __future__ import annotations

import json
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DOMAIN = "tenant.focalx.ai"
DEFAULT_PROCESS_ID = "7BAQMZBAHUYK"
POLL_INTERVAL_S = 20
POLL_TIMEOUT_S = 1800

# Core walk-around labels confirmed working against process 7BAQMZBAHUYK.
VALID_WALKAROUND = [
    "front", "afront", "front-left", "afront-left", "afront-left-wheel",
    "aleft-front", "aleft-rear", "left-rear", "arear-left-wheel", "rear-left",
    "rear", "rear-right", "arear-right-wheel", "right-rear", "abcright-rear",
    "aright-front", "afront-right-wheel", "right-front", "front-right",
]


@dataclass
class Finding:
    position: str          # position label the finding was seen on
    orientation: str | None
    part: str | None
    damage_type: str
    close_up_url: str | None
    raw: dict = field(repr=False, default_factory=dict)


@dataclass
class InspectionResult:
    inspection_id: str
    completed: bool
    findings: list[Finding]
    orientations: int
    raw_report: dict = field(repr=False, default_factory=dict)


class FocalxClient:
    def __init__(self, username: str, password: str, process_id: str = DEFAULT_PROCESS_ID):
        self._user = username
        self._pw = password
        self.process_id = process_id
        self._token: str | None = None
        self._base: str | None = None

    # ── public API ─────────────────────────────────────────────────────────

    def valid_labels(self) -> list[str]:
        """All position labels the tenant defines (from user-profile custom_data)."""
        prof = json.loads(self._get(f"https://{DOMAIN}/api/v1/customer/user-profile/"))
        seen: list[str] = []
        for entry in prof.get("custom_data", []):
            label = entry.get("label")
            if label and label not in seen:
                seen.append(label)
        return seen

    def inspect(
        self,
        plate: str,
        images: list[tuple[str, Path]],
        on_progress: Callable[[str], None] = lambda msg: None,
        on_partial: Callable[[list[Finding]], None] | None = None,
    ) -> InspectionResult:
        """Full inspection: create → upload → submit(+verify) → poll to completion.

        `images` = list of (position_label, file_path). Blocking (~10 min).
        """
        base = self._ensure_base()
        insp = self._create_inspection(plate)
        on_progress(f"inspection {insp} — uploading {len(images)} image(s)")
        for pos, path in images:
            self._upload(insp, pos, path)
            on_progress(f"uploaded {pos}")
        self._post(f"{base}/api/v2/service/inspections/{insp}/submitImages/")
        self._ensure_submitted(insp, on_progress)
        report = self._poll_report(insp, on_progress, on_partial)
        findings = _map_report(report)
        return InspectionResult(
            inspection_id=insp,
            completed=report.get("Completed", False),
            findings=findings,
            orientations=len(report.get("OrientationResults") or []),
            raw_report=report,
        )

    def download(self, url: str, dest: Path) -> bool:
        """Fetch an image URL (e.g. a finding's close-up) to disk. Fail-soft."""
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as r:
                dest.write_bytes(r.read())
            return True
        except Exception:
            return False

    # ── steps ──────────────────────────────────────────────────────────────

    def _login(self) -> str:
        body = json.dumps({"username": self._user, "password": self._pw})
        text = self._raw("POST", f"https://{DOMAIN}/api/v1/token/", body, auth=False)
        self._token = json.loads(text)["access"]
        return self._token

    def _ensure_base(self) -> str:
        if self._base:
            return self._base
        self._login()
        prof = json.loads(self._get(f"https://{DOMAIN}/api/v1/customer/user-profile/"))
        self._base = f"https://{prof['client']['slug']}.{DOMAIN}"
        return self._base

    def _create_inspection(self, plate: str) -> str:
        base = self._ensure_base()
        form = {
            "make": "unknown", "model": "unknown", "year": "2000",
            "registration_number": plate, "mileage": "0", "vin": "",
            "body_type": "station", "color": "unknown", "energy_type": "petrol",
            "body_size": "medium", "process_id": self.process_id,
            "platform": "IOS", "meta_data": "",
        }
        enc = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in form.items())
        text = self._raw("POST", f"{base}/api/v1/service/create-inspection/", enc,
                         ctype="application/x-www-form-urlencoded")
        return json.loads(text)["inspection"]["id"]

    def _upload(self, insp: str, position: str, path: Path) -> None:
        base = self._ensure_base()
        img = path.read_bytes()
        remote = f"{insp}_{position}.jpg"
        body = json.dumps({"images": [{"name": remote, "position": position}]})
        text = self._post(f"{base}/api/v2/service/inspections/{insp}/signedurls/", body)
        item = json.loads(text)[0]
        signed = item["genarated_url"]  # (sic — API typo)
        name = item.get("image_name", remote)
        boundary = f"----eval{uuid.uuid4().hex}"
        parts = b""
        for k, v in signed["fields"].items():
            parts += (f"--{boundary}\r\nContent-Disposition: form-data; "
                      f'name="{k}"\r\n\r\n{v}\r\n').encode()
        parts += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  f'filename="{name}"\r\nContent-Type: image/jpeg\r\n\r\n').encode()
        parts += img + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(signed["url"], method="POST", data=parts)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=180) as r:  # S3: no auth header!
            assert r.status in (200, 201, 204), f"S3 upload HTTP {r.status}"

    def _ensure_submitted(self, insp: str, on_progress: Callable[[str], None]) -> None:
        base = self._ensure_base()
        deadline = time.time() + 180
        while True:
            detail = json.loads(self._get(f"{base}/api/v1/service/inspections/{insp}/"))
            if detail.get("is_images_submitted"):
                on_progress("is_images_submitted=true — AI processing started")
                return
            if time.time() > deadline:
                raise RuntimeError("images never registered as submitted")
            on_progress("is_images_submitted=false — re-submitting")
            time.sleep(5)
            self._post(f"{base}/api/v2/service/inspections/{insp}/submitImages/")

    def _poll_report(self, insp, on_progress, on_partial) -> dict:
        base = self._ensure_base()
        url = f"{base}/api/v2/service/inspections/{insp}/damagereport/"
        deadline = time.time() + POLL_TIMEOUT_S
        while time.time() < deadline:
            try:
                text = self._get(url)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self._login()
                    continue
                if e.code == 404:
                    time.sleep(POLL_INTERVAL_S)
                    continue
                raise
            obj = json.loads(text) if text.strip().startswith("{") else {}
            orients = obj.get("OrientationResults") or []
            ndmg = sum(len(o.get("Damages") or []) for o in orients)
            on_progress(f"poll: completed={obj.get('Completed')} orientations={len(orients)} damages={ndmg}")
            if obj.get("Completed"):
                return obj
            if on_partial:
                on_partial(_map_report(obj))
            time.sleep(POLL_INTERVAL_S)
        raise TimeoutError("FocalX report not completed within timeout")

    # ── http plumbing ──────────────────────────────────────────────────────

    def _get(self, url: str) -> str:
        return self._raw("GET", url)

    def _post(self, url: str, body: str | None = None) -> str:
        return self._raw("POST", url, body)

    def _raw(self, method, url, body=None, ctype="application/json", auth=True) -> str:
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/json")
        if auth and self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        data = None
        if body is not None or method == "POST":
            data = (body or "").encode()
            req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, data=data, timeout=90) as r:
            return r.read().decode()


def _map_report(report: dict) -> list[Finding]:
    insp_id = report.get("InspectionId", "")
    prefix = f"{insp_id}_"
    out: list[Finding] = []
    for orient in report.get("OrientationResults") or []:
        remote = (orient.get("ImageFileName") or "").split("/")[-1]
        position = remote.removeprefix(prefix).rsplit(".", 1)[0]
        for dmg in orient.get("Damages") or []:
            out.append(Finding(
                position=position,
                orientation=orient.get("OrientationName") or None,
                part=(dmg.get("PartName") or "").strip() or None,
                damage_type=(dmg.get("DamageTypeName") or "other").strip().lower(),
                close_up_url=(dmg.get("CloseUpImageFilename") or "").strip() or None,
                raw=dmg,
            ))
    return out
