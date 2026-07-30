"""Object storage (MinIO / S3-compatible).

Original documents live here; PostgreSQL holds only metadata and the object
key. Putting a 20 MB PDF in a database column bloats every backup, defeats
connection pooling and makes the table unqueryable — so the rule is absolute:
**bytes in MinIO, facts in PostgreSQL.**

Object keys are derived from the tender's master UUID and are therefore
deterministic. A Celery task replayed after a crash recomputes the same key and
overwrites the same object rather than leaving an orphan behind — idempotency
by construction.

Downloads are always served through short-lived presigned URLs. The bucket is
never public, and the API never proxies file bytes through itself.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Any, BinaryIO

from app.core.config import get_settings
from app.core.exceptions import StorageError
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.core.metrics import storage_operations_total
from app.core.security import safe_object_key, sanitize_filename

logger = get_logger(__name__)

__all__ = ["ObjectStorage", "StoredObject", "get_storage", "reset_storage"]


@dataclass(slots=True)
class StoredObject:
    bucket: str
    key: str
    size_bytes: int
    content_type: str
    etag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "etag": self.etag,
        }


class ObjectStorage:
    """Thin, typed wrapper over the MinIO client."""

    def __init__(self) -> None:
        settings = get_settings()
        self.bucket = settings.storage.bucket
        self.presigned_ttl = settings.storage.presigned_ttl_seconds
        self.server_side_encryption = settings.storage.server_side_encryption
        self.part_size = settings.storage.part_size_bytes
        self._client: Any = None
        self._bucket_checked = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from minio import Minio

                    settings = get_settings()
                    self._client = Minio(
                        settings.storage.endpoint,
                        access_key=settings.storage.access_key,
                        secret_key=settings.storage.secret_key,
                        secure=settings.storage.secure,
                        region=settings.storage.region,
                    )
                    logger.info(
                        "storage.client_created",
                        endpoint=settings.storage.endpoint,
                        bucket=self.bucket,
                        secure=settings.storage.secure,
                    )
        return self._client

    def ensure_bucket(self) -> None:
        """Create the bucket on first use. Idempotent and race-tolerant."""
        if self._bucket_checked:
            return
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
                logger.info("storage.bucket_created", bucket=self.bucket)
            self._bucket_checked = True
        except Exception as exc:
            # Two workers starting together will race here; the loser sees an
            # "already owned" error, which is a success in disguise.
            if "already" in str(exc).lower():
                self._bucket_checked = True
                return
            raise StorageError(
                "Could not ensure the storage bucket exists.",
                context={"bucket": self.bucket},
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    @staticmethod
    def build_key(
        tender_uuid: str,
        filename: str | None,
        *,
        prefix: str = "tenders",
        subpath: str | None = None,
    ) -> str:
        """Deterministic, traversal-safe object key.

        Date-partitioned so lifecycle rules and bulk exports can operate on a
        time range without scanning the whole bucket.
        """
        now = utc_now()
        safe_name = sanitize_filename(filename, default="document")
        parts = [prefix, f"{now:%Y}", f"{now:%m}", f"{now:%d}", tender_uuid]
        if subpath:
            parts.append(subpath)
        parts.append(safe_name)
        return safe_object_key(*parts)

    # ------------------------------------------------------------------
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        return self.put_stream(
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
            metadata=metadata,
        )

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        length: int,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        self.ensure_bucket()
        safe_key = safe_object_key(key)
        try:
            from minio.sse import SseS3

            result = self.client.put_object(
                self.bucket,
                safe_key,
                stream,
                length=length,
                content_type=content_type,
                metadata=metadata or {},
                part_size=self.part_size,
                sse=SseS3() if self.server_side_encryption else None,
            )
        except Exception as exc:
            storage_operations_total.labels(operation="put", outcome="failure").inc()
            detail = str(exc)
            context: dict[str, Any] = {
                "bucket": self.bucket,
                "key": safe_key,
                # The underlying cause belongs in the log. Without it the error
                # says only *that* storage failed, never *why* — and since
                # ingestion deliberately continues without the original, the
                # symptom is "documents are silently never archived".
                "cause": detail[:500],
            }
            if "KMS is not configured" in detail or "NotImplemented" in detail:
                context["hint"] = (
                    "Server-side encryption is on (the secure default) but the "
                    "object store has no KMS. Configure KMS, or set "
                    "SMARTTENDER_STORAGE__SERVER_SIDE_ENCRYPTION=false if this "
                    "deployment encrypts at the volume level instead."
                )
            raise StorageError(
                "Failed to store the document.", context=context, cause=exc
            ) from exc

        storage_operations_total.labels(operation="put", outcome="success").inc()
        logger.info("storage.object_stored", key=safe_key, size_bytes=length)
        return StoredObject(
            bucket=self.bucket,
            key=safe_key,
            size_bytes=length,
            content_type=content_type,
            etag=getattr(result, "etag", None),
        )

    def get_bytes(self, key: str) -> bytes:
        self.ensure_bucket()
        response = None
        try:
            response = self.client.get_object(self.bucket, safe_object_key(key))
            data = response.read()
        except Exception as exc:
            storage_operations_total.labels(operation="get", outcome="failure").inc()
            raise StorageError(
                "Failed to read the document from storage.",
                context={"bucket": self.bucket, "key": key},
                cause=exc,
            ) from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()
        storage_operations_total.labels(operation="get", outcome="success").inc()
        return data

    def presigned_url(self, key: str, *, ttl_seconds: int | None = None) -> str:
        """Time-limited download URL. The only way bytes leave the platform."""
        self.ensure_bucket()
        try:
            return self.client.presigned_get_object(
                self.bucket,
                safe_object_key(key),
                expires=timedelta(seconds=ttl_seconds or self.presigned_ttl),
            )
        except Exception as exc:
            storage_operations_total.labels(operation="presign", outcome="failure").inc()
            raise StorageError(
                "Failed to generate a download link.",
                context={"bucket": self.bucket, "key": key},
                cause=exc,
            ) from exc

    def exists(self, key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, safe_object_key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            self.client.remove_object(self.bucket, safe_object_key(key))
            storage_operations_total.labels(operation="delete", outcome="success").inc()
        except Exception as exc:
            storage_operations_total.labels(operation="delete", outcome="failure").inc()
            raise StorageError(
                "Failed to delete the document.",
                context={"bucket": self.bucket, "key": key},
                cause=exc,
            ) from exc

    def health(self) -> bool:
        try:
            self.client.bucket_exists(self.bucket)
            return True
        except Exception as exc:
            logger.warning("storage.healthcheck_failed", error=str(exc))
            return False


_storage: ObjectStorage | None = None
_storage_lock = threading.Lock()


def get_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        with _storage_lock:
            if _storage is None:
                _storage = ObjectStorage()
    return _storage


def reset_storage() -> None:
    global _storage
    with _storage_lock:
        _storage = None
