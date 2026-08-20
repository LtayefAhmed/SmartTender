"""
Similarity search over the resumes Qdrant collection built by build_index.py.

Usage:
    python search.py "senior python backend developer with AWS experience"
    python search.py "hotel front desk manager" --limit 10 --category HR
"""

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sentence_transformers import SentenceTransformer, CrossEncoder

from qdrant_utils import (
    QDRANT_PATH,
    COLLECTION_NAME,
    MODEL_NAME,
    RERANK_MODEL_NAME,
    RERANK_SHORTLIST_SIZE,
    search_resumes,
    rerank,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="free-text search query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--category", default=None, help="optional category filter, e.g. HR")
    args = parser.parse_args()

    model = SentenceTransformer(MODEL_NAME)
    cross_encoder = CrossEncoder(RERANK_MODEL_NAME)
    client = QdrantClient(path=QDRANT_PATH)

    query_filter = None
    if args.category:
        query_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=args.category.upper()))]
        )

    shortlist_size = max(RERANK_SHORTLIST_SIZE, args.limit)
    shortlist = search_resumes(client, model, args.query, limit=shortlist_size, query_filter=query_filter)
    results = rerank(cross_encoder, args.query, shortlist, top_n=args.limit)

    for hit in results:
        print(f"\nID={hit.id}  relevance={hit.score:.4f}  (retrieval={hit.retrieval_score:.4f})  category={hit.payload['category']}")
        # The chunk that actually matched, not the résumé's generic opening.
        print(hit.payload["chunk_text"][:300].replace("\n", " ").strip(), "...")


if __name__ == "__main__":
    main()
