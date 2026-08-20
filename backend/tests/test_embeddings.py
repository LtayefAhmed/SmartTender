"""Encoding passages, and the properties matching depends on.

These tests need the ONNX model on disk and are skipped without it, so a
checkout with no models still runs green. What they pin is not the model's
quality — that is measured, not asserted — but the contract around it: unit
vectors, order preserved, batching invisible, a missing model failing loudly
rather than returning something plausible.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.services.embeddings import EmbeddingModel

MODEL = Path("./models/paraphrase-multilingual-MiniLM-L12-v2")
needs_model = pytest.mark.skipif(
    not (MODEL / "model.onnx").is_file(),
    reason="embedding model not downloaded; run scripts/fetch_models.py",
)


@pytest.fixture(scope="module")
def model() -> EmbeddingModel:
    return EmbeddingModel(model_path=str(MODEL))


class TestTheModelIsLoadedFromItsFiles:
    def test_a_missing_model_fails_loudly(self):
        """Silence here would be worse than a crash: a matching run with no
        model must not quietly return zeros that rank as "nothing matches"."""
        with pytest.raises(FileNotFoundError):
            EmbeddingModel(model_path="./models/inexistant").encode("texte")

    @needs_model
    def test_the_dimension_comes_from_the_graph(self, model):
        """Configured dimensions drift. A mismatch only surfaces as a Qdrant
        rejection hundreds of passages into an indexing run."""
        assert model.dimensions == 384


@needs_model
class TestVectorsAreUsableAsCosineDistances:
    def test_every_vector_is_unit_length(self, model):
        """The collection is configured for cosine, and normalised vectors are
        what make a dot product equal to one."""
        for vector in model.encode_many(["prestations de TMA", "développement Java"]):
            assert math.isclose(sum(x * x for x in vector), 1.0, rel_tol=1e-4)

    def test_identical_texts_score_one(self, model):
        assert model.similarity("maintenance applicative", "maintenance applicative") > 0.999

    def test_related_french_beats_unrelated_french(self, model):
        """The single property matching rests on. Not a quality claim — a
        sanity check that the model is wired up at all."""
        related = model.similarity(
            "conception et implémentation d'APIs d'intégration",
            "orchestration de flux entre applications",
        )
        unrelated = model.similarity(
            "conception et implémentation d'APIs d'intégration",
            "restauration collective et hygiène alimentaire",
        )

        assert related > unrelated + 0.2


@needs_model
class TestBatchingIsInvisible:
    def test_order_is_preserved(self, model):
        """The caller pairs vectors with passages by position. A reordering
        would attach every passage to its neighbour's meaning, and nothing
        downstream could detect it."""
        texts = [f"exigence numéro {index}" for index in range(40)]

        batched = model.encode_many(texts)
        one_by_one = [model.encode(text) for text in texts]

        for left, right in zip(batched, one_by_one, strict=True):
            assert math.isclose(
                sum(a * b for a, b in zip(left, right, strict=True)), 1.0, rel_tol=1e-3
            )

    def test_a_batch_larger_than_the_batch_size_is_complete(self, model):
        texts = [f"passage {index}" for index in range(70)]

        assert len(model.encode_many(texts)) == 70

    def test_an_empty_passage_yields_a_zero_vector_not_a_missing_one(self, model):
        """Skipping it would shorten the list and shift every pairing after
        it — the worst kind of bug, because the output still looks valid."""
        vectors = model.encode_many(["texte réel", "   ", "autre texte"])

        assert len(vectors) == 3
        assert all(value == 0.0 for value in vectors[1])

    def test_encoding_nothing_returns_nothing(self, model):
        assert model.encode_many([]) == []
