"""Turning passages into vectors.

Separate from :mod:`app.services.similarity` on purpose, even though both end
up running a sentence-transformer. They do different jobs and want opposite
inputs:

``similarity``
    compares two texts to decide whether they are the *same notice*. It feeds
    the model ``normalize_text`` output — accent-stripped, lowercased,
    punctuation removed — because that is what makes a re-export of the same
    tender collide with the original.

``embeddings`` (here)
    encodes meaning for retrieval, and feeds the model **raw text**.

    The reason is not the one you would expect. Measured over ten French
    procurement pairs, normalising first changes almost nothing: separation
    between matching and unrelated pairs was 0.410 raw against 0.402
    normalised — noise at that sample size — and the weakest true positive was
    actually *better* normalised (0.375 against 0.258). The intuition that
    stripping accents must hurt a multilingual model did not survive contact
    with a measurement.

    Raw text is kept for a weaker but sounder reason: normalisation is
    irreversible. Whatever case, accents and punctuation carry, the model was
    trained on text that had them, and we can always normalise later — we
    cannot put back what we dropped before encoding. If a future measurement
    on a larger sample shows normalising wins, this is a one-line change.

Two things here exist because of scale rather than correctness. Encoding is
**batched**, since a tender yields 280 passages and one forward pass over 32 of
them costs far less than 32 passes. And the session is created once per
process, because loading a 448 MB model per call is not a performance problem,
it is an outage.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["EmbeddingModel", "get_embedder", "reset_embedder"]


@dataclass(slots=True)
class _Loaded:
    session: Any
    tokenizer: Any
    dimensions: int


class EmbeddingModel:
    """A sentence-transformer served through ONNX Runtime on CPU.

    Model-agnostic by construction: everything specific to a checkpoint — the
    vocabulary, the dimension, whether it wants ``token_type_ids`` — is read
    from the files at load time. Swapping an English model for a multilingual
    one is a path change, not a code change.
    """

    def __init__(self, model_path: str | None = None, max_length: int | None = None) -> None:
        settings = get_settings().semantic
        self.model_path = model_path or settings.embedding_model_path
        self.max_length = max_length or settings.max_sequence_length
        self.batch_size = 32
        self._loaded: _Loaded | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    @property
    def dimensions(self) -> int:
        return self._ensure().dimensions

    def _ensure(self) -> _Loaded:
        if self._loaded is not None:
            return self._loaded
        with self._lock:
            if self._loaded is not None:
                return self._loaded

            from pathlib import Path

            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer

            root = Path(self.model_path)
            model_file = root / "model.onnx"
            tokenizer_file = root / "tokenizer.json"
            if not model_file.is_file() or not tokenizer_file.is_file():
                raise FileNotFoundError(
                    f"No embedding model under {root}. Expected model.onnx and "
                    "tokenizer.json — see scripts/fetch_models.py."
                )

            options = ort.SessionOptions()
            # Workers are already parallel; letting each session spawn threads
            # oversubscribes the CPU and makes every request slower.
            options.intra_op_num_threads = 1
            session = ort.InferenceSession(
                str(model_file), options, providers=["CPUExecutionProvider"]
            )
            tokenizer = Tokenizer.from_file(str(tokenizer_file))
            tokenizer.enable_truncation(max_length=self.max_length)
            tokenizer.enable_padding(length=None)

            # Read from the graph rather than configured: a wrong dimension
            # only shows up as a Qdrant error hundreds of passages later.
            dimensions = int(session.get_outputs()[0].shape[-1])
            self._loaded = _Loaded(session=session, tokenizer=tokenizer, dimensions=dimensions)
            logger.info(
                "embeddings.model_loaded",
                path=str(root),
                dimensions=dimensions,
                max_length=self.max_length,
            )
            _ = np  # imported here so a missing numpy fails at load, not mid-batch
            return self._loaded

    # ------------------------------------------------------------------
    def encode(self, text: str) -> list[float]:
        return self.encode_many([text])[0]

    def encode_many(self, texts: list[str]) -> list[list[float]]:
        """Encode a list of passages, in batches.

        Empty strings are encoded as zero vectors rather than skipped: the
        caller pairs results with inputs by position, and silently returning a
        shorter list is how a passage ends up attached to its neighbour's
        vector.
        """
        if not texts:
            return []

        import numpy as np

        loaded = self._ensure()
        wants_token_types = any(
            spec.name == "token_type_ids" for spec in loaded.session.get_inputs()
        )
        vectors: list[list[float]] = []

        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            # A tokenizer given "" produces a zero-length sequence, which ONNX
            # refuses. A single space keeps the shape valid and the result is
            # discarded below anyway.
            encoded = loaded.tokenizer.encode_batch([t if t.strip() else " " for t in batch])

            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            inputs: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
            if wants_token_types:
                inputs["token_type_ids"] = np.zeros_like(ids)

            hidden = loaded.session.run(None, inputs)[0]

            # Mean pooling over non-padding tokens, then L2 normalisation —
            # the pooling these checkpoints were trained with. Normalising is
            # what makes a dot product a cosine, which is the distance the
            # collection is configured for.
            expanded = np.expand_dims(mask, -1).astype(np.float32)
            summed = (hidden * expanded).sum(axis=1)
            counts = np.clip(expanded.sum(axis=1), a_min=1e-9, a_max=None)
            pooled = summed / counts
            norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            normalised = pooled / norms

            for position, row in enumerate(normalised):
                original = batch[position]
                vectors.append(
                    [0.0] * loaded.dimensions if not original.strip() else row.tolist()
                )

        return vectors

    def similarity(self, left: str, right: str) -> float:
        """Cosine between two texts. Vectors are already unit-length, so this
        is a dot product."""
        a, b = self.encode_many([left, right])
        return float(sum(x * y for x, y in zip(a, b, strict=True)))


_embedder: EmbeddingModel | None = None
_embedder_lock = threading.Lock()


def get_embedder() -> EmbeddingModel:
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = EmbeddingModel()
    return _embedder


def reset_embedder() -> None:
    """Drop the cached model. Tests and configuration reloads only."""
    global _embedder
    _embedder = None
