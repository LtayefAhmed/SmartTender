"""Text similarity, with a pluggable backend.

Two implementations behind one interface:

``lexical`` (default)
    Pure Python. Cosine over a weighted bag of word tokens and character
    4-grams, on accent-stripped normalised text. No model file, no native
    dependency, no warm-up, and — crucially — **deterministic**, so a dedup
    test asserts an exact number instead of a fuzzy range. Character n-grams
    are what make it robust to the typos, truncations and re-orderings that
    portals actually produce.

``minilm``
    ONNX ``all-MiniLM-L6-v2`` on CPU. Better at genuine paraphrase ("expert
    Cloud AWS" ≈ "spécialiste Amazon Web Services"), at the cost of a model
    file and ~30× the latency.

The lexical backend is the default deliberately: for *duplicate detection* —
near-identical text — it is at least as good, and it cannot become a startup
dependency that takes the ingestion pipeline down.
"""

from __future__ import annotations

import math
import threading
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable
from functools import lru_cache

from app.core.config import get_settings
from app.core.identity import normalize_text
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "LexicalBackend",
    "MiniLmBackend",
    "SimilarityBackend",
    "get_similarity_backend",
    "reset_similarity_backend",
]

#: Very common French/English words carry no discriminative signal and would
#: inflate the similarity of two unrelated notices that share boilerplate.
_STOPWORDS = frozenset(
    # French articles, pronouns, prepositions and auxiliaries.
    "le la les un une des du de d au aux et ou a as en dans sur pour par avec "
    "sans sous vers chez ce cet cette ces son sa ses leur leurs il elle ils "
    "elles nous vous je tu on que qui quoi dont est sont etre avoir plus moins "
    "tres tout tous toute "
    # English equivalents, for portals that publish bilingually.
    "the an and or of in on for to with without at by from is are be been this "
    "that these those it its "
    # Procurement boilerplate: present in essentially every notice, so it
    # carries no signal about *which* notice this is.
    "appel offre offres avis marche marches public publics".split()
)


class SimilarityBackend(ABC):
    """Turns text into something comparable, and compares it."""

    name: str = "base"

    @abstractmethod
    def encode(self, text: str) -> dict[str, float] | list[float]:
        """Produce the comparable representation of a piece of text."""

    @abstractmethod
    def similarity(self, left: str, right: str) -> float:
        """Similarity in [0, 1]. 1.0 means "the same text"."""

    def similarity_encoded(self, left, right) -> float:  # type: ignore[no-untyped-def]
        """Compare two already-encoded vectors, avoiding re-encoding."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Lexical
# ---------------------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    return [
        token
        for token in normalize_text(text).split()
        if len(token) > 2 and token not in _STOPWORDS
    ]


def _char_ngrams(text: str, n: int = 4) -> Iterable[str]:
    compact = normalize_text(text).replace(" ", "_")
    if len(compact) < n:
        if compact:
            yield compact
        return
    for index in range(len(compact) - n + 1):
        yield compact[index : index + n]


class LexicalBackend(SimilarityBackend):
    """Deterministic sparse-vector similarity. No dependencies, no model."""

    name = "lexical"

    #: Word tokens carry most of the meaning; character n-grams supply the
    #: robustness to small edits. Weighted rather than either/or.
    TOKEN_WEIGHT = 0.65
    NGRAM_WEIGHT = 0.35

    def __init__(self, cache_size: int = 4096) -> None:
        self._encode_cached = lru_cache(maxsize=cache_size)(self._encode_uncached)

    def _encode_uncached(self, text: str) -> tuple[tuple[str, float], ...]:
        vector: Counter[str] = Counter()
        for token in _tokens(text):
            vector[f"w:{token}"] += self.TOKEN_WEIGHT
        for gram in _char_ngrams(text):
            vector[f"g:{gram}"] += self.NGRAM_WEIGHT

        # Sub-linear term weighting: a word repeated forty times in a long
        # notice should not dominate the vector.
        weighted = {key: 1.0 + math.log(value) for key, value in vector.items() if value > 0}
        norm = math.sqrt(sum(v * v for v in weighted.values())) or 1.0
        return tuple((key, value / norm) for key, value in weighted.items())

    def encode(self, text: str) -> dict[str, float]:
        return dict(self._encode_cached(text or ""))

    def similarity(self, left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        return self.similarity_encoded(
            dict(self._encode_cached(left)), dict(self._encode_cached(right))
        )

    def similarity_encoded(
        self, left: dict[str, float], right: dict[str, float]
    ) -> float:
        if not left or not right:
            return 0.0
        # Iterate the smaller vector: both are already L2-normalised, so the
        # dot product is the cosine.
        if len(left) > len(right):
            left, right = right, left
        score = sum(value * right.get(key, 0.0) for key, value in left.items())
        return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# MiniLM (optional)
# ---------------------------------------------------------------------------
class MiniLmBackend(SimilarityBackend):
    """ONNX all-MiniLM-L6-v2 sentence embeddings on CPU."""

    name = "minilm"

    def __init__(self, model_path: str, max_length: int = 256) -> None:
        self.model_path = model_path
        self.max_length = max_length
        self._session = None
        self._tokenizer = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            from pathlib import Path

            import numpy as np  # noqa: F401  (import checked here, used below)
            import onnxruntime as ort
            from tokenizers import Tokenizer

            root = Path(self.model_path)
            model_file = root / "model.onnx"
            tokenizer_file = root / "tokenizer.json"
            if not model_file.is_file() or not tokenizer_file.is_file():
                raise FileNotFoundError(
                    f"MiniLM model files not found under {root}. Expected "
                    "model.onnx and tokenizer.json."
                )
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1  # workers are already parallel
            self._session = ort.InferenceSession(
                str(model_file), options, providers=["CPUExecutionProvider"]
            )
            self._tokenizer = Tokenizer.from_file(str(tokenizer_file))
            self._tokenizer.enable_truncation(max_length=self.max_length)
            logger.info("similarity.minilm_loaded", model_path=str(root))

    def encode(self, text: str) -> list[float]:
        self._ensure_loaded()
        import numpy as np

        encoded = self._tokenizer.encode(normalize_text(text) or "")  # type: ignore[union-attr]
        ids = np.array([encoded.ids], dtype=np.int64)
        mask = np.array([encoded.attention_mask], dtype=np.int64)
        inputs = {"input_ids": ids, "attention_mask": mask}
        if any(i.name == "token_type_ids" for i in self._session.get_inputs()):  # type: ignore[union-attr]
            inputs["token_type_ids"] = np.zeros_like(ids)

        outputs = self._session.run(None, inputs)  # type: ignore[union-attr]
        hidden = outputs[0]
        # Mean pooling over non-padding tokens, then L2 normalisation — the
        # pooling all-MiniLM-L6-v2 was trained with.
        expanded = np.expand_dims(mask, -1).astype(np.float32)
        summed = (hidden * expanded).sum(axis=1)
        counts = np.clip(expanded.sum(axis=1), a_min=1e-9, a_max=None)
        pooled = summed / counts
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norm, 1e-9, None))[0].tolist()

    def similarity(self, left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        return self.similarity_encoded(self.encode(left), self.encode(right))

    def similarity_encoded(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        score = sum(a * b for a, b in zip(left, right, strict=True))
        return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_backend: SimilarityBackend | None = None
_backend_lock = threading.Lock()


def get_similarity_backend() -> SimilarityBackend:
    """Return the configured backend, falling back to lexical on any problem.

    A missing model file must degrade duplicate detection, never stop
    ingestion — a slightly less clever dedup pass is vastly preferable to a
    pipeline that refuses to accept tenders.
    """
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        settings = get_settings()
        choice = settings.semantic.backend.lower()
        if choice == "minilm":
            try:
                backend: SimilarityBackend = MiniLmBackend(
                    settings.semantic.model_path, settings.semantic.max_sequence_length
                )
                backend.encode("warmup")  # fail now, not mid-pipeline
                _backend = backend
                logger.info("similarity.backend_selected", backend="minilm")
                return _backend
            except Exception as exc:
                logger.warning(
                    "similarity.minilm_unavailable.falling_back",
                    error=str(exc),
                    backend="lexical",
                )
        _backend = LexicalBackend(settings.semantic.cache_size)
        logger.info("similarity.backend_selected", backend=_backend.name)
        return _backend


def reset_similarity_backend() -> None:
    """Drop the cached backend — used by tests that switch configuration."""
    global _backend
    with _backend_lock:
        _backend = None
