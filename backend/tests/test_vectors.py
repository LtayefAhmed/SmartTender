"""The vector index, and the two things it must never get wrong.

**Isolation.** A CV belongs to one organisation. Filtering results after the
search would mean another firm's passages had already been read into this
process; a top-k that returns ten foreign CVs and then discards them has still
exposed them. The filter therefore lives in the query, and these tests assert
it from the outside — by asking for someone else's data and getting nothing.

**Degradation.** A vector store is a derived index: everything in it can be
rebuilt from ``extracted_text``. Losing it costs a re-index, never a document,
which is why nothing here raises. A search that fails returns no results and
the platform keeps ingesting, scoring and notifying while matching is degraded.

Run against Qdrant's in-process local mode. A test that needs a container to
run is a test that stops being run.
"""

from __future__ import annotations

import pytest

from app.services.vectors import VectorPoint, VectorStore


@pytest.fixture()
def store(monkeypatch) -> VectorStore:
    monkeypatch.setenv("SMARTTENDER_VECTOR__URL", ":memory:")
    from app.core.config import reset_settings_cache as reset_settings

    reset_settings()
    instance = VectorStore()
    yield instance
    reset_settings()


def _point(uuid_tail: int, vector: list[float], **payload) -> VectorPoint:
    return VectorPoint(id=f"{uuid_tail:08d}-0000-0000-0000-000000000000", vector=vector,
                       payload=payload)


@pytest.fixture()
def populated(store: VectorStore) -> VectorStore:
    store.upsert(
        "essai",
        [
            _point(1, [1.0, 0.0, 0.0], tenant_id="inetum", owner_id="cv-a",
                   text="WSO2 API Manager", priority=0),
            _point(2, [0.9, 0.1, 0.0], tenant_id="inetum", owner_id="cv-b",
                   text="integration d'APIs", priority=1),
            _point(3, [1.0, 0.0, 0.0], tenant_id="concurrent", owner_id="cv-x",
                   text="profil du concurrent", priority=0),
        ],
        dimensions=3,
    )
    return store


class TestIsolationHoldsInsideTheEngine:
    def test_a_search_scoped_to_one_firm_never_returns_another(self, populated):
        hits = populated.search("essai", [1.0, 0.0, 0.0], tenant="inetum", limit=10)

        assert {hit.payload["tenant_id"] for hit in hits} == {"inetum"}

    def test_the_competitors_best_match_is_excluded_even_when_it_ranks_first(self, populated):
        """The excluded point is an *exact* match on the query vector. If the
        filter were applied after ranking, this is the one that would leak."""
        unscoped = populated.search("essai", [1.0, 0.0, 0.0], limit=10)
        scoped = populated.search("essai", [1.0, 0.0, 0.0], tenant="inetum", limit=10)

        assert any(hit.payload["owner_id"] == "cv-x" for hit in unscoped)
        assert all(hit.payload["owner_id"] != "cv-x" for hit in scoped)

    def test_counting_is_scoped_too(self, populated):
        """A count that ignores the tenant tells one firm how many candidates
        another has — small, and still a leak."""
        assert populated.count("essai", tenant="inetum") == 2
        assert populated.count("essai", tenant="concurrent") == 1


class TestReindexingReplacesRatherThanAccumulates:
    def test_the_same_id_overwrites(self, store):
        """Ids are derived from the owning row and the passage position, so a
        re-extraction overwrites. A random id would leave the previous
        generation searchable forever, and nothing would notice."""
        store.upsert("essai", [_point(1, [1.0, 0.0, 0.0], tenant_id="a", text="version 1")],
                     dimensions=3)
        store.upsert("essai", [_point(1, [1.0, 0.0, 0.0], tenant_id="a", text="version 2")],
                     dimensions=3)

        hits = store.search("essai", [1.0, 0.0, 0.0], limit=10)

        assert len(hits) == 1
        assert hits[0].text == "version 2"

    def test_dropping_one_owner_leaves_the_others(self, populated):
        """Called before re-indexing and on deletion: a shortened document
        would otherwise keep its old passages matchable."""
        populated.delete_owner("essai", "cv-a")

        remaining = {hit.payload["owner_id"] for hit in populated.search(
            "essai", [1.0, 0.0, 0.0], limit=10
        )}

        assert remaining == {"cv-b", "cv-x"}


class TestFailureDegradesInsteadOfBreaking:
    """Everything indexed can be rebuilt from PostgreSQL. Losing the index
    costs a re-index, never a document — so nothing here is allowed to raise."""

    def test_searching_an_unknown_collection_returns_nothing(self, store):
        assert store.search("jamais_creee", [1.0, 0.0, 0.0]) == []

    def test_an_unreachable_server_does_not_raise_on_search(self, monkeypatch):
        monkeypatch.setenv("SMARTTENDER_VECTOR__URL", "http://127.0.0.1:1")
        from app.core.config import reset_settings_cache as reset_settings

        reset_settings()
        broken = VectorStore()
        broken.timeout = 0.5

        assert broken.search("essai", [1.0, 0.0, 0.0]) == []
        assert broken.healthy() is False
        assert broken.count("essai") == 0
        reset_settings()

    def test_an_unreachable_server_does_not_raise_on_upsert(self, monkeypatch):
        monkeypatch.setenv("SMARTTENDER_VECTOR__URL", "http://127.0.0.1:1")
        from app.core.config import reset_settings_cache as reset_settings

        reset_settings()
        broken = VectorStore()
        broken.timeout = 0.5

        written = broken.upsert("essai", [_point(1, [1.0, 0.0, 0.0])], dimensions=3)

        assert written == 0
        reset_settings()

    def test_indexing_nothing_is_not_an_error(self, store):
        assert store.upsert("essai", [], dimensions=3) == 0


class TestSearchShape:
    def test_a_hit_exposes_its_passage_and_document(self, store):
        store.upsert(
            "essai",
            [_point(1, [1.0, 0.0, 0.0], tenant_id="a", text="prestations de TMA",
                    document="CCTP.pdf")],
            dimensions=3,
        )

        hit = store.search("essai", [1.0, 0.0, 0.0])[0]

        assert hit.text == "prestations de TMA"
        assert hit.document == "CCTP.pdf"
        assert 0.0 <= hit.score <= 1.0

    def test_a_score_threshold_drops_weak_matches(self, store):
        store.upsert(
            "essai",
            [
                _point(1, [1.0, 0.0, 0.0], tenant_id="a", text="proche"),
                _point(2, [0.0, 1.0, 0.0], tenant_id="a", text="orthogonal"),
            ],
            dimensions=3,
        )

        hits = store.search("essai", [1.0, 0.0, 0.0], min_score=0.5)

        assert [hit.text for hit in hits] == ["proche"]
