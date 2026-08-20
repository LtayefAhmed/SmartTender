"""
Build a Qdrant vector index from Resume.csv for similarity search.

Each résumé is split into overlapping chunks (see qdrant_utils.chunk_text)
before embedding, because the embedding model only looks at the first 256
tokens and résumés average ~1,050 -- embedding the whole résumé as one
vector would silently discard most of it. Every chunk is stored as its own
point; qdrant_utils.search_resumes collapses chunks back to one best score
per résumé at query time.
"""

import argparse
from pathlib import Path

import pandas as pd
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

from qdrant_utils import BASE_DIR, QDRANT_PATH, COLLECTION_NAME, MODEL_NAME, chunk_text

# Not committed to the repo (56MB, third-party dataset) -- download it
# yourself and drop it here, or pass --csv to point elsewhere. See README.
DEFAULT_CSV_PATH = BASE_DIR / "Resume.csv"
BATCH_SIZE = 64


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(DEFAULT_CSV_PATH), help="path to Resume.csv")
    args = parser.parse_args()

    if not Path(args.csv).exists():
        raise SystemExit(
            f"Résumé CSV not found at {args.csv}\n"
            "Download the dataset (Kaggle: 'Resume Dataset') and place Resume.csv "
            f"next to this script, or run with --csv <path>."
        )

    df = pd.read_csv(args.csv)
    df["Resume_str"] = df["Resume_str"].astype(str)

    model = SentenceTransformer(MODEL_NAME)
    vector_size = model.get_sentence_embedding_dimension()

    client = QdrantClient(path=QDRANT_PATH)

    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )

    # Build the full chunk list up front so we can embed in efficient batches
    # instead of one résumé at a time.
    chunk_texts = []
    chunk_meta = []  # (point_id, resume_id, category, resume_snippet)
    for row in df.itertuples(index=False):
        resume_id = int(row.ID)
        snippet = row.Resume_str[:2000]
        chunks = chunk_text(row.Resume_str)
        for i, chunk in enumerate(chunks):
            chunk_texts.append(chunk)
            chunk_meta.append((resume_id * 1000 + i, resume_id, row.Category, snippet, chunk))

    print(f"{len(df)} résumés -> {len(chunk_texts)} chunks "
          f"({len(chunk_texts) / len(df):.1f} chunks/résumé on average)")

    embeddings = model.encode(
        chunk_texts,
        batch_size=BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,
    )

    points = [
        PointStruct(
            id=point_id,
            vector=embeddings[i].tolist(),
            payload={
                "resume_id": resume_id,
                "category": category,
                "resume_text": snippet,
                "chunk_text": chunk,
            },
        )
        for i, (point_id, resume_id, category, snippet, chunk) in enumerate(chunk_meta)
    ]

    for i in range(0, len(points), BATCH_SIZE):
        client.upsert(collection_name=COLLECTION_NAME, points=points[i : i + BATCH_SIZE])

    print(f"Indexed {len(points)} chunks from {len(df)} résumés into "
          f"collection '{COLLECTION_NAME}' at {QDRANT_PATH}")


if __name__ == "__main__":
    main()
