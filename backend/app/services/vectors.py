"""The vector index: where embedded passages live and are searched.

One idea runs through this module. **A vector store is a derived index, not a
source of truth.** Every passage in it can be rebuilt from ``extracted_text``
in PostgreSQL, which means losing Qdrant costs a re-index and never a document.
That is what licenses the error handling here: a search that fails returns no
results rather than raising, and an index that fails is logged and retried. The
platform keeps ingesting, scoring and notifying while matching is degraded,
because matching is the newest capability and must not become the most fragile
part of the pipeline.

Two collections rather than one with a ``kind`` field. They are searched
separately, they grow at very different rates — a tender dossier yields 280
passages, a CV yields 6 — and a CV surfacing in a tender search would be a
silent correctness bug rather than a visible one.

Tenant isolation is enforced *inside* the query, not after it. Filtering
results in Python would mean fetching another organisation's passages into our
process first, and a top-k that returns ten of someone else's CVs and then
discards them has still read them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["SearchHit", "VectorPoint", "VectorStore", "get_vector_store", "reset_vector_store"]


@dataclass(slots=True)
class VectorPoint:
    """One embedded passage, ready to be indexed."""

    #: Deterministic, derived from the owning row and the passage position, so
    #: re-indexing a tender overwrites its passages instead of duplicating
    #: them. A random id would leave the previous generation behind on every
    #: re-extraction, and nothing would ever notice.
    id: str
    vector: list[float]
    #: Filterable metadata. Keep it small: the payload is returned with every
    #: hit, and the passage text is the only large field worth carrying.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchHit:
    id: str
    score: float
    payload: dict[str, Any]

    @property
    def text(self) -> str:
        return str(self.payload.get("text") or "")

    @property
    def document(self) -> str | None:
        value = self.payload.get("document")
        return str(value) if value else None


class VectorStore:
    """A thin, failure-tolerant wrapper over Qdrant."""

    def __init__(self) -> None:
        settings = get_settings().vector
        self.url = settings.url
        self.timeout = settings.timeout_seconds
        self.tender_collection = settings.tender_collection
        self.cv_collection = settings.cv_collection
        self.batch_size = settings.upsert_batch_size
        self._client: Any = None
        self._ready: set[str] = set()
        # Reentrant, and that is load-bearing. Two lazy initialisations guard
        # themselves with this lock — the client and the per-collection setup —
        # and the second legitimately touches the first: ``ensure_collection``
        # holds the lock, then asks for ``self.client``, whose property takes
        # the lock again. With a plain ``Lock`` that is a deadlock, and it
        # presents as a hang with no error and no traceback.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    @property
    def client(self) -> Any:
        """Connected lazily: importing this module must not require a server."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from qdrant_client import QdrantClient

                    if self.url == ":memory:":
                        # Local mode: the same API, backed by an in-process
                        # index. It is what makes these behaviours testable
                        # without a container — and a test that needs a server
                        # to run is a test that stops being run.
                        self._client = QdrantClient(location=":memory:")
                    else:
                        self._client = QdrantClient(url=self.url, timeout=self.timeout)
                    logger.info("vectors.client_created", url=self.url)
        return self._client

    def healthy(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception as exc:
            logger.warning("vectors.unavailable", error=str(exc)[:200])
            return False

    # ------------------------------------------------------------------
    def ensure_collection(self, name: str, *, dimensions: int) -> None:
        """Create the collection if it does not exist, and index its filters.

        Idempotent, and cached per process: this runs before every batch, and
        a round trip to check what we already know would double the cost of
        indexing a tender.

        The payload indexes matter more than they look. Without them Qdrant
        filters by scanning, so a tenant-scoped CV search degrades from
        milliseconds to a full pass over every passage — which is exactly the
        shape of query matching makes on every run.
        """
        if name in self._ready:
            return
        with self._lock:
            if name in self._ready:
                return
            from qdrant_client.models import Distance, VectorParams

            if not self.client.collection_exists(name):
                self.client.create_collection(
                    collection_name=name,
                    # Cosine because sentence embeddings encode meaning in
                    # direction, not magnitude: a long CV and a short one
                    # describing the same skills must land close together.
                    vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
                )
                logger.info("vectors.collection_created", name=name, dimensions=dimensions)

            # Local mode has no payload indexes and warns on every attempt;
            # it also scans so little data that they would change nothing.
            fields = (
                ()
                if self.url == ":memory:"
                else (("tenant_id", "keyword"), ("owner_id", "keyword"), ("priority", "integer"))
            )
            for field_name, schema in fields:
                try:
                    self.client.create_payload_index(
                        collection_name=name, field_name=field_name, field_schema=schema
                    )
                except Exception:
                    # Already indexed. Qdrant has no "create if absent" for
                    # payload indexes, and the alternative is a listing call
                    # per collection per process start.
                    pass
            self._ready.add(name)

    def upsert(self, collection: str, points: list[VectorPoint], *, dimensions: int) -> int:
        """Index passages. Returns how many were written.

        Never raises: a tender whose passages failed to index is still a tender
        that was collected, extracted and scored. It is simply not yet
        matchable, and the row that owns it records enough to try again.
        """
        if not points:
            return 0
        try:
            self.ensure_collection(collection, dimensions=dimensions)
        except Exception as exc:
            logger.warning("vectors.ensure_failed", collection=collection, error=str(exc)[:200])
            return 0

        from qdrant_client.models import PointStruct

        written = 0
        for start in range(0, len(points), self.batch_size):
            batch = points[start : start + self.batch_size]
            try:
                self.client.upsert(
                    collection_name=collection,
                    points=[
                        PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in batch
                    ],
                    wait=True,
                )
                written += len(batch)
            except Exception as exc:
                logger.warning(
                    "vectors.upsert_failed",
                    collection=collection,
                    batch_size=len(batch),
                    error=str(exc)[:200],
                )
        return written

    def search(
        self,
        collection: str,
        vector: list[float],
        *,
        tenant: str | None = None,
        limit: int = 20,
        min_score: float = 0.0,
        extra_filter: Any = None,
    ) -> list[SearchHit]:
        """Nearest passages, filtered inside the engine.

        ``tenant`` is applied by Qdrant rather than by us. Filtering afterwards
        would mean another organisation's passages had already been read into
        this process — a top-k that returns ten foreign CVs and then discards
        them has still exposed them.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = []
        if tenant is not None:
            conditions.append(
                FieldCondition(key="tenant_id", match=MatchValue(value=tenant))
            )
        query_filter = extra_filter
        if conditions:
            inherited = list(extra_filter.must) if extra_filter else []
            query_filter = Filter(must=conditions + inherited)

        try:
            found = self.client.query_points(
                collection_name=collection,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                score_threshold=min_score or None,
                with_payload=True,
            ).points
        except Exception as exc:
            # Degraded, not broken: the caller shows "matching unavailable"
            # rather than a stack trace, and everything else keeps working.
            logger.warning("vectors.search_failed", collection=collection, error=str(exc)[:200])
            return []

        return [
            SearchHit(id=str(point.id), score=float(point.score), payload=dict(point.payload or {}))
            for point in found
        ]

    def delete_owner(self, collection: str, owner_id: str) -> None:
        """Drop every passage belonging to one tender or CV.

        Called before re-indexing and on deletion. Without it, shortening a
        document leaves its old passages searchable forever — they match, they
        rank, and nothing in the interface hints they no longer exist.
        """
        from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

        try:
            self.client.delete(
                collection_name=collection,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="owner_id", match=MatchValue(value=owner_id))]
                    )
                ),
                wait=True,
            )
        except Exception as exc:
            logger.warning("vectors.delete_failed", collection=collection, error=str(exc)[:200])

    def count(self, collection: str, *, tenant: str | None = None) -> int:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        try:
            query_filter = None
            if tenant is not None:
                query_filter = Filter(
                    must=[FieldCondition(key="tenant_id", match=MatchValue(value=tenant))]
                )
            return int(
                self.client.count(
                    collection_name=collection, count_filter=query_filter, exact=True
                ).count
            )
        except Exception:
            return 0


_store: VectorStore | None = None
_store_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = VectorStore()
    return _store


def reset_vector_store() -> None:
    """Drop the cached instance. Tests and configuration reloads only."""
    global _store
    _store = None
