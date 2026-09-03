"""AWS-Lambda-Einstieg: FocalX-Report aus SQS ins Archiv einordnen.

API Gateway legt den von FocalX gesendeten Report unverändert als SQS-Body ab.
Eine Nachricht entspricht genau einer Inspektion. Der bestehende Ingest lädt
deren Bilder und schreibt Report, Manifest und Kennzeichen-Index nach S3.

Die Funktion antwortet mit partiellen Batch-Fehlern. Dadurch löscht Lambda nur
erfolgreiche Nachrichten aus SQS; unvollständige Inspektionen werden erneut
versucht und landen nach der in SQS eingestellten Zahl Versuche in der DLQ.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from .ingest import UUID_RE, ingest_report
from .store import S3Store, Store

DEFAULT_PREFIX = "focalx-push"
DEFAULT_WORKERS = 6
MAX_WORKERS = 16


class InvalidReport(ValueError):
    """Die Nachricht ist kein vollständiger FocalX-Report."""


class IncompleteArchive(RuntimeError):
    """Der Report ist gesichert, aber mindestens ein Bild fehlt."""


def _positive_int(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} muss eine ganze Zahl sein, nicht {raw!r}") from exc
    if not 1 <= value <= maximum:
        raise RuntimeError(f"{name} muss zwischen 1 und {maximum} liegen")
    return value


def configured_store() -> S3Store:
    bucket = os.environ.get("ARCHIVE_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("ARCHIVE_BUCKET fehlt")
    return S3Store(bucket, os.environ.get("ARCHIVE_PREFIX", DEFAULT_PREFIX))


def parse_report(body: Any) -> dict:
    """SQS-Body lesen und nur einen fertigen, eindeutig ablegbaren Report zulassen."""
    if not isinstance(body, str):
        raise InvalidReport("SQS body fehlt oder ist kein String")
    try:
        report = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidReport(f"ungültiges JSON: {exc.msg}") from exc
    if not isinstance(report, dict):
        raise InvalidReport("Report muss ein JSON-Objekt sein")

    inspection_id = report.get("InspectionId")
    if not isinstance(inspection_id, str) or UUID_RE.fullmatch(inspection_id) is None:
        raise InvalidReport("InspectionId fehlt oder ist keine UUID")
    if report.get("Completed") is not True:
        raise InvalidReport(f"{inspection_id}: Report ist nicht abgeschlossen")

    orientations = report.get("OrientationResults")
    if not isinstance(orientations, list) or not orientations:
        raise InvalidReport(f"{inspection_id}: OrientationResults ist leer")

    stamp = (report.get("StartTime") or report.get("EndTime") or "")[:10]
    try:
        date.fromisoformat(stamp)
    except (TypeError, ValueError) as exc:
        raise InvalidReport(f"{inspection_id}: Start-/Enddatum fehlt") from exc
    return report


def process_record(record: dict, store: Store, workers: int) -> dict:
    message_id = str(record.get("messageId") or "ohne-id")
    report = parse_report(record.get("body"))
    manifest = ingest_report(
        report,
        store,
        source=f"push:sqs:{message_id}",
        workers=workers,
    )
    if not manifest["vollstaendig"]:
        raise IncompleteArchive(
            f"{manifest['inspection_id']}: {len(manifest['fehlend'])} Objekt(e) fehlen"
        )
    return manifest


def handle_event(event: dict, store: Store, workers: int = DEFAULT_WORKERS) -> dict:
    """Testbarer Kern des Handlers mit injizierter Ablage."""
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list):
        raise ValueError("SQS event enthält keine Records-Liste")

    failures = []
    for index, record in enumerate(records):
        message_id = str(record.get("messageId") or index)
        try:
            manifest = process_record(record, store, workers)
            print(json.dumps({
                "level": "INFO",
                "event": "inspection_archived",
                "message_id": message_id,
                "inspection_id": manifest["inspection_id"],
                "objects": manifest["gespeichert"]["objekte"],
                "bytes": manifest["gespeichert"]["bytes"],
            }))
        except Exception as exc:
            print(json.dumps({
                "level": "ERROR",
                "event": "inspection_failed",
                "message_id": message_id,
                "error": type(exc).__name__,
                "detail": str(exc)[:300],
            }))
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def handler(event: dict, _context: Any) -> dict:
    """AWS-Einstiegspunkt: ``archive.lambda_handler.handler``."""
    return handle_event(
        event,
        configured_store(),
        workers=_positive_int("DOWNLOAD_WORKERS", DEFAULT_WORKERS, MAX_WORKERS),
    )
