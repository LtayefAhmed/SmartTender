"""The referential of validated responses — the only loop that feeds itself.

    a dossier is approved  →  indexed here
                           →  recalled when the next tender resembles it
                           →  offered to the model as style
                           →  the next dossier is written better

A loop is also the easiest place to do lasting damage: anything wrong that goes
in comes back out, repeatedly, with the authority of "this is how we write".
So the tests are about what is allowed to enter, and about the fact that
*nothing* entering is a correct state rather than a broken one.
"""

from __future__ import annotations

import pytest

from app.services.memory import index_response, recall_responses


class TestNothingEnteringIsACorrectState:
    """The collection cannot predate the first validation. Everything
    downstream has to produce the same document it produced before."""

    def test_recall_without_a_collection_returns_nothing(self, monkeypatch):
        def _explode():
            raise RuntimeError("collection does not exist")

        monkeypatch.setattr("app.services.vectors.get_vector_store", _explode)

        assert recall_responses("tierce maintenance Java", tenant="inetum") == []

    def test_recall_of_an_empty_query_asks_nothing(self):
        assert recall_responses("", tenant="inetum") == []
        assert recall_responses("   ", tenant="inetum") == []

    def test_indexing_empty_text_writes_nothing(self):
        written = index_response(
            document_id="d1", text="", tenant="inetum", kind="cv"
        )

        assert written == 0

    def test_an_unreachable_store_never_undoes_an_approval(self, monkeypatch):
        """Capitalisation is derived data. Losing it must not cost a human
        the approval they just granted."""

        def _explode():
            raise RuntimeError("qdrant unreachable")

        monkeypatch.setattr("app.services.vectors.get_vector_store", _explode)

        assert index_response(
            document_id="d1", text="Un texte réel.", tenant="inetum", kind="cv"
        ) == 0


class TestWhatIsWritten:
    @pytest.fixture
    def store(self, monkeypatch):
        """A vector store that records what it was given."""
        written: dict[str, object] = {}

        class _Store:
            response_collection = "past_responses"

            def upsert(self, collection, points, *, dimensions):
                written["collection"] = collection
                written["points"] = points
                return len(points)

        class _Embedder:
            dimensions = 8

            def encode_many(self, texts):
                return [[0.1] * 8 for _ in texts]

        monkeypatch.setattr("app.services.vectors.get_vector_store", lambda: _Store())
        monkeypatch.setattr("app.services.embeddings.get_embedder", lambda: _Embedder())
        return written

    def test_it_goes_to_its_own_collection(self, store):
        """A third collection, not a `kind` field on an existing one: a past
        proposal surfacing inside a CV search would attribute one consultant's
        work to another."""
        index_response(
            document_id="d1",
            text="Notre équipe a conduit la migration du socle applicatif. " * 40,
            tenant="inetum",
            kind="cv",
        )

        assert store["collection"] == "past_responses"

    def test_the_payload_carries_its_provenance(self, store):
        index_response(
            document_id="d1",
            text="Notre équipe a conduit la migration du socle applicatif. " * 40,
            tenant="inetum",
            kind="matrice_conformite",
            tender_title="TMA du SI ARIA",
            label="Matrice — AO 01/2026",
        )
        payload = store["points"][0].payload

        assert payload["tenant_id"] == "inetum"
        assert payload["owner_id"] == "d1"
        assert payload["kind"] == "matrice_conformite"
        assert payload["tender_title"] == "TMA du SI ARIA"

    def test_reindexing_the_same_document_overwrites_rather_than_duplicates(self, store):
        """Approving twice, or re-approving after a correction, must not leave
        two copies of the same prose voting twice in a later recall."""
        text = "Notre équipe a conduit la migration du socle applicatif. " * 40
        index_response(document_id="d1", text=text, tenant="inetum", kind="cv")
        first = [point.id for point in store["points"]]

        index_response(document_id="d1", text=text, tenant="inetum", kind="cv")
        second = [point.id for point in store["points"]]

        assert first == second


class TestWhatComesBack:
    @pytest.fixture
    def hits(self, monkeypatch):
        class _Hit:
            def __init__(self, text, score):
                self.payload = {
                    "text": text,
                    "tender_title": "TMA du SI ARIA",
                    "label": "CV — Marie Dupont",
                }
                self.score = score

            @property
            def text(self):
                return self.payload["text"]

        class _Store:
            response_collection = "past_responses"
            captured: dict[str, object] = {}

            def search(self, collection, vector, *, tenant, limit, min_score):
                _Store.captured = {"limit": limit, "min_score": min_score, "tenant": tenant}
                return [_Hit("Conduite de la TMA sur trois ans.", 0.71)]

        class _Embedder:
            def encode(self, text):
                return [0.1] * 8

        monkeypatch.setattr("app.services.vectors.get_vector_store", lambda: _Store())
        monkeypatch.setattr("app.services.embeddings.get_embedder", lambda: _Embedder())
        return _Store

    def test_a_recalled_passage_keeps_what_it_came_from(self, hits):
        found = recall_responses("tierce maintenance", tenant="inetum")

        assert found[0].text.startswith("Conduite de la TMA")
        assert found[0].tender_title == "TMA du SI ARIA"
        assert found[0].score == pytest.approx(0.71)

    def test_a_floor_keeps_irrelevant_examples_out(self, hits):
        """An example about another mission is worse than none: it nudges the
        wording of a bid toward work it has nothing to do with."""
        recall_responses("tierce maintenance", tenant="inetum")

        assert hits.captured["min_score"] >= 0.3

    def test_the_recall_is_bounded(self, hits):
        """Three passages carry a house style. More would crowd out the CV,
        which is the only source the anti-invention guard can check against."""
        recall_responses("tierce maintenance", tenant="inetum")

        assert hits.captured["limit"] <= 3

    def test_the_search_is_scoped_to_the_organisation(self, hits):
        recall_responses("tierce maintenance", tenant="inetum")

        assert hits.captured["tenant"] == "inetum"
