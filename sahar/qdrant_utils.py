"""
Shared helpers for the résumés Qdrant collection.

Résumés average ~1,050 tokens; the embedding model (all-MiniLM-L6-v2) only
looks at the first 256. Embedding a whole résumé as one vector silently
truncates ~99% of them to their opening paragraph -- work history and skills
further down are never seen by the model. To fix this without losing
information, each résumé is split into overlapping chunks that each fit
under the model's limit, and every chunk is stored as its own point. At
query time we search all chunks and keep each résumé's single best-scoring
chunk, so scoring reflects the résumé's most relevant section instead of
just its header.
"""

import types
from pathlib import Path

import numpy as np

# Relative to this file, not the current working directory or a hardcoded
# machine-specific path, so the project works after `git clone` on any OS.
BASE_DIR = Path(__file__).resolve().parent
QDRANT_PATH = str(BASE_DIR / "qdrant_db")
COLLECTION_NAME = "resumes"
MODEL_NAME = "all-MiniLM-L6-v2"

# The bi-encoder above (MODEL_NAME) embeds query and résumé independently,
# then compares vectors with cosine similarity -- fast enough to score the
# whole pool, but it can't "look" at query and résumé together, so it's
# easily fooled by generic résumé-speak vocabulary overlap (e.g. a Sales
# Associate résumé scoring 0.64 against a "Veterinary Surgeon" query just
# because both use polished professional-summary language). A cross-encoder
# reads the query and one candidate TOGETHER through the same transformer,
# which is much more accurate at judging genuine relevance -- but too slow
# to run against the whole pool. So: bi-encoder retrieves a shortlist fast,
# cross-encoder re-scores and re-orders just that shortlist for display.
RERANK_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_SHORTLIST_SIZE = 30

# ~150 words is a safe margin under the model's 256-token limit for typical
# résumé prose (English averages ~1.3 tokens/word). 30-word overlap keeps a
# skill/role mentioned right at a chunk boundary from being split in half.
CHUNK_WORDS = 150
OVERLAP_WORDS = 30


def chunk_text(text, chunk_words=CHUNK_WORDS, overlap_words=OVERLAP_WORDS):
    words = text.split()
    if not words:
        return [text]
    step = chunk_words - overlap_words
    chunks = []
    for start in range(0, len(words), step):
        chunk = words[start : start + chunk_words]
        if not chunk:
            break
        chunks.append(" ".join(chunk))
        if start + chunk_words >= len(words):
            break
    return chunks


def search_resumes(client, model, query_text, limit=None, query_filter=None):
    """Embed query_text and return one best-scoring hit per résumé (deduped
    across that résumé's chunks), sorted by score descending. Each returned
    object has .id (résumé ID), .score, .payload -- matching the shape of a
    plain Qdrant ScoredPoint so calling code doesn't need to know chunking
    happened underneath."""
    vector = model.encode(query_text, normalize_embeddings=True).tolist()
    total_chunks = client.count(collection_name=COLLECTION_NAME, count_filter=query_filter).count

    raw_hits = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        query_filter=query_filter,
        limit=total_chunks,
    ).points

    best_per_resume = {}
    for hit in raw_hits:
        rid = hit.payload["resume_id"]
        if rid not in best_per_resume or hit.score > best_per_resume[rid].score:
            best_per_resume[rid] = types.SimpleNamespace(id=rid, score=hit.score, payload=hit.payload)

    ranked = sorted(best_per_resume.values(), key=lambda h: h.score, reverse=True)
    return ranked[:limit] if limit else ranked


def rerank(cross_encoder, query_text, candidates, top_n=None):
    """Re-score and re-order `candidates` (résumé-level hits from
    search_resumes, each needs payload["chunk_text"]) with a cross-encoder
    for accurate relevance -- fixes cases where the bi-encoder's cosine
    similarity was fooled by superficial vocabulary overlap. Returns new
    objects with .id/.payload preserved and .score replaced by the
    cross-encoder's relevance probability (0-1, via sigmoid); original
    bi-encoder score is kept as .retrieval_score."""
    if not candidates:
        return []
    pairs = [(query_text, c.payload["chunk_text"]) for c in candidates]
    logits = cross_encoder.predict(pairs)
    relevance = 1.0 / (1.0 + np.exp(-logits))

    reranked = [
        types.SimpleNamespace(id=c.id, score=float(rel), retrieval_score=c.score, payload=c.payload)
        for c, rel in zip(candidates, relevance)
    ]
    reranked.sort(key=lambda h: h.score, reverse=True)
    return reranked[:top_n] if top_n else reranked
