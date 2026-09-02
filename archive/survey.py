"""Bestandsbericht: Was liegt im Archiv, und was fehlt?

Ohne diese Auswertung merkt niemand, wenn ein Bild fehlt — der Ordner sieht
gefüllt aus, der Fehler fällt erst auf, wenn jemand das Bild braucht. Der
Bericht liest ausschließlich die Manifeste, nicht die Bilder selbst, und ist
damit auch über Millionen Objekte bezahlbar.
"""
from __future__ import annotations

import json
from collections import Counter

from .store import Store


def collect(store: Store, prefix: str = "v1/") -> list[dict]:
    manifeste = []
    for key in store.list(prefix):
        if not key.endswith("/manifest.json"):
            continue
        payload = store.get(key)
        if payload:
            manifeste.append(json.loads(payload))
    return manifeste


def summarise(manifeste: list[dict]) -> dict:
    gruende: Counter = Counter()
    for m in manifeste:
        fehlend = set(m.get("fehlend") or [])
        for o in m.get("objects") or []:
            if o["key"] in fehlend:
                gruende[o.get("note") or str(o.get("status"))] += 1

    # Ein Archiv überlebt mehrere Fassungen seines eigenen Schemas. Felder, die
    # es beim Schreiben noch nicht gab, dürfen den Bericht nicht zum Absturz
    # bringen — sonst ist die Überwachung genau dann blind, wenn sich etwas
    # geändert hat.
    from .ingest import plate_key as _plate_key

    vollstaendig = [m for m in manifeste if m.get("vollstaendig")]
    unplausibel = [m for m in manifeste if not m.get("plate_plausibel", True)]
    nachtraeglich = [m for m in manifeste
                     if str(m.get("report", "")).startswith("abweichend")]
    bytes_ges = sum((m.get("gespeichert") or {}).get("bytes", 0) for m in manifeste)
    objekte = sum((m.get("gespeichert") or {}).get("objekte", 0) for m in manifeste)
    fahrzeuge = {m.get("plate_key") or _plate_key(m.get("registration_number", ""))
                 for m in manifeste}

    return {
        "inspektionen": len(manifeste),
        "vollstaendig": len(vollstaendig),
        "unvollstaendig": len(manifeste) - len(vollstaendig),
        "objekte": objekte,
        "bytes": bytes_ges,
        "mb_je_inspektion": (bytes_ges / len(manifeste) / 1e6) if manifeste else 0,
        "fehlgruende": dict(gruende.most_common()),
        "kennzeichen_unplausibel": [m.get("registration_number", "")
                                    for m in unplausibel],
        "report_abweichend": [m.get("inspection_id") for m in nachtraeglich],
        "fahrzeuge": len(fahrzeuge - {""}),
        "schemata": dict(Counter(m.get("schema", "unbekannt")
                                 for m in manifeste).most_common()),
    }


def render(bericht: dict) -> str:
    z = [
        f"Inspektionen   {bericht['inspektionen']}  "
        f"({bericht['vollstaendig']} vollständig, "
        f"{bericht['unvollstaendig']} unvollständig)",
        f"Fahrzeuge      {bericht['fahrzeuge']}",
        f"Objekte        {bericht['objekte']}",
        f"Umfang         {bericht['bytes'] / 1e9:.2f} GB  "
        f"(⌀ {bericht['mb_je_inspektion']:.1f} MB je Inspektion)",
    ]
    if bericht["fehlgruende"]:
        z.append("Fehlend        " + ", ".join(
            f"{k}: {v}" for k, v in bericht["fehlgruende"].items()))
    if bericht["kennzeichen_unplausibel"]:
        kz = bericht["kennzeichen_unplausibel"]
        z.append(f"Kennzeichen    {len(kz)} unplausibel: "
                 f"{', '.join(map(repr, kz[:5]))}"
                 f"{' …' if len(kz) > 5 else ''}")
    if bericht["report_abweichend"]:
        z.append(f"Reports        {len(bericht['report_abweichend'])} bei erneutem "
                 f"Abruf abweichend (Erstfassung unangetastet)")
    if len(bericht.get("schemata", {})) > 1:
        z.append("Schemafassungen " + ", ".join(
            f"{k}: {v}" for k, v in bericht["schemata"].items()))
    return "\n".join(z)
