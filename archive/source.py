"""Woher die Reports kommen: die Tagesliste bei FocalX oder die lokale Platte.

Die Tagesliste ist der Grund, warum ein Archiv-Dienst ohne eigenen Upload
auskommt: `inspectionswithdate` liefert auch Inspektionen, die wir nicht
angelegt haben (gemessen: 690 von unserem Client, 17 aus der Mobile-App).
Das Kennzeichen steht allerdings NICHT in der Liste, nur im Report — die
Zuordnung Inspektion → Fahrzeug läuft deshalb zwingend über den Report.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from eval.focalx import DOMAIN, FocalxClient


class FocalxSource:
    """Lesender Zugriff: Tagesliste und Report. Legt nichts an."""

    def __init__(self, username: str, password: str):
        self._client = FocalxClient(username, password)

    @property
    def base(self) -> str:
        return self._client._ensure_base()

    def day(self, start: str, end: str | None = None) -> list[dict]:
        """Alle Inspektionen eines Zeitraums (beidseitig inklusiv).

        Gefiltert wird nach `created_on` in FocalX, nicht nach dem Check-in-Tag
        des Fahrzeugs. Der Endpoint kennt keine Paginierung — ob er bei großen
        Mengen still abschneidet, ist bei FocalX angefragt und unbeantwortet.
        Bis dahin prüft `day_guard` die Zahl gegen eine Erwartung."""
        end = end or start
        url = (f"{self.base}/api/v2/service/inspections/inspectionswithdate/"
               f"?start_date={start}&end_date={end}")
        return json.loads(self._client._get(url)).get("inspection") or []

    def report(self, inspection_id: str) -> dict:
        url = (f"{self.base}/api/v2/service/inspections/"
               f"{inspection_id}/damagereport/")
        return json.loads(self._client._get(url))


def day_guard(eintraege: list[dict], warnschwelle: int = 900) -> str | None:
    """Ohne Paginierung ist ein stilles Abschneiden der gefährlichste Fehler:
    Das Archiv wäre technisch vollständig und fachlich lückenhaft. Ein runder
    Wert nahe einer Grenze ist das einzige Signal, das wir dafür haben."""
    n = len(eintraege)
    if n >= warnschwelle:
        return (f"{n} Einträge — nahe einer möglichen Obergrenze. Zeitraum "
                f"halbieren und die Summe vergleichen, bevor der Tag als "
                f"vollständig gilt.")
    return None


def local_reports(data_dir: Path, filt: str | None = None) -> Iterator[tuple[dict, str]]:
    """Gespeicherte Reports als Quelle — für Trockenläufe ohne Zugangsdaten.
    Sie haben dieselben Felder wie eine frische Antwort; nur die Vollbild-URLs
    sind nach sieben Tagen tot."""
    archiv = data_dir / "archive"
    for p in sorted(data_dir.rglob("focalx_report.json")):
        if archiv in p.parents:
            continue
        if filt and filt.lower() not in str(p).lower():
            continue
        yield json.loads(p.read_text()), f"lokal:{p}"


__all__ = ["FocalxSource", "day_guard", "local_reports", "DOMAIN"]
