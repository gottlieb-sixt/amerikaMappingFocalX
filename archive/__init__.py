"""FocalX-Archiv: alles, was FocalX zu einer Inspektion erzeugt, dauerhaft sichern.

FocalX schickt den fertigen Report an unseren Endpoint. Der Dienst lädt die
darin genannten Vollbilder und Closeups sofort und legt sie samt Manifest ab.
Die Report-Adressen werden auf unser Archiv umgeschrieben; nicht geladene
Bilder stehen ehrlich als ``null`` darin. Der manuelle Abholweg über die
Tagesliste bleibt nur für Tests und Nachläufe erhalten.

    from archive.ingest import ingest_report
    from archive.store import LocalStore, S3Store

    store = LocalStore(Path("data/archive"))     # später: S3Store("bucket")
    ingest_report(report, store, source="api:<id>")
"""
from .ingest import ingest_report, plan_objects, plate_key, prefix_for
from .store import LocalStore, S3Store, Store

__all__ = ["ingest_report", "plan_objects", "plate_key", "prefix_for",
           "LocalStore", "S3Store", "Store"]
