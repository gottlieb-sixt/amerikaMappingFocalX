"""FocalX-Archiv: alles, was FocalX zu einer Inspektion erzeugt, dauerhaft sichern.

Der Dienst lädt nichts hoch. Er findet Inspektionen über die Tagesliste — auch
fremde —, zieht den Report und legt Vollbilder, Closeups und Report unverändert
ab. Zeitkritisch ist nur der Anfang: Die signierten Vollbild-URLs leben
168 Stunden, danach sind die Bilder endgültig verloren.

    from archive.ingest import ingest_report
    from archive.store import LocalStore, S3Store

    store = LocalStore(Path("data/archive"))     # später: S3Store("bucket")
    ingest_report(report, store, source="api:<id>")
"""
from .ingest import ingest_report, plan_objects, plate_key, prefix_for
from .store import LocalStore, S3Store, Store

__all__ = ["ingest_report", "plan_objects", "plate_key", "prefix_for",
           "LocalStore", "S3Store", "Store"]
