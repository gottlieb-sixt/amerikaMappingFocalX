"""Ablage-Schicht: derselbe Ingest schreibt lokal oder nach S3.

Schlüssel sind immer `/`-getrennte Strings ohne führenden Schrägstrich — also
genau das, was S3 einen Key nennt. Der lokale Speicher bildet sie auf
Verzeichnisse ab. Dadurch ist der Wechsel nach AWS eine Zeile Konfiguration
und kein Umbau, und der Trockenlauf auf der Platte prüft denselben Code, der
später in der Wolke läuft.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Protocol


class Store(Protocol):
    def exists(self, key: str) -> bool: ...
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, payload: bytes) -> None: ...
    def list(self, prefix: str) -> Iterator[str]: ...
    def uri(self, key: str = "") -> str: ...


class LocalStore:
    """Ein Verzeichnis als Bucket-Ersatz."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    def put(self, key: str, payload: bytes) -> None:
        # Atomar: ein abgebrochener Lauf hinterlässt keine halbe Datei, die beim
        # nächsten Mal als "schon vorhanden" durchgeht.
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, dest)

    def list(self, prefix: str) -> Iterator[str]:
        base = self._path(prefix)
        if not base.exists():
            return
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.endswith(".tmp"):
                yield str(p.relative_to(self.root))

    def uri(self, key: str = "") -> str:
        # Bewusst zusammengesetzt statt über Path: `uri("")` muss wie bei S3 auf
        # einen Schrägstrich enden, sonst ergeben abgeleitete Adressen einmal
        # `…/archiv/v1/x` und einmal `…/archiv//v1/x`.
        return f"{self.root}/{key}"


class S3Store:
    """Der spätere Produktivspeicher. Ungetestet, solange kein Konto existiert.

    Absichtlich schmal gehalten: `put` ist ein einzelner PutObject-Aufruf, also
    von sich aus atomar — halbe Objekte kann S3 gar nicht zeigen. Damit
    entfällt der `.tmp`-Umweg des lokalen Speichers."""

    def __init__(self, bucket: str, prefix: str = "", client=None):
        try:
            import boto3  # noqa: PLC0415 — optionale Abhängigkeit
        except ImportError as e:
            raise RuntimeError(
                "S3Store braucht boto3 — `pip install boto3`. Für den "
                "Trockenlauf genügt LocalStore."
            ) from e
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = client or boto3.client("s3")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # noqa: PLC0415
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError  # noqa: PLC0415
        try:
            return self._s3.get_object(Bucket=self.bucket,
                                       Key=self._key(key))["Body"].read()
        except ClientError:
            return None

    def put(self, key: str, payload: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=payload)

    def list(self, prefix: str) -> Iterator[str]:
        cut = len(self.prefix) + 1 if self.prefix else 0
        pages = self._s3.get_paginator("list_objects_v2")
        for page in pages.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            for obj in page.get("Contents") or []:
                yield obj["Key"][cut:]

    def uri(self, key: str = "") -> str:
        return f"s3://{self.bucket}/{self._key(key)}"
