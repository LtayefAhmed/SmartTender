# Résumé Similarity Search — How It Works

This explains, from first principles, everything that was built in this folder:
turning 2,484 résumés into vectors, storing them in Qdrant, and searching them
by meaning instead of by keyword.

---

## 0. Setup (fresh clone, any machine)

```
git clone <this repo> && cd qdrant_resumes
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then get the dataset — it's not committed to the repo (56MB, third-party):
download **"Resume Dataset"** from Kaggle and place `Resume.csv` in this
folder (or pass `--csv <path>` to `build_index.py` if you keep it elsewhere).

Build the index once (takes a few minutes — downloads the embedding model on
first run, then embeds every résumé chunk):
```
python build_index.py
```

This creates `qdrant_db/` locally (also not committed — it's ~140MB of
generated data, rebuild it any time from the CSV). From here:
```
python search.py "senior python backend developer with AWS"
python match_ao.py sample_ao_project.json
```

No Qdrant server, Docker, or API keys needed — everything runs locally
(`qdrant_utils.py` resolves all paths relative to the project folder, so this
works the same on Windows/Mac/Linux regardless of where you clone it).

---

## 1. The data

Source file: `Resume.csv` (see Setup above for how to get it)

| Column | Contents |
|---|---|
| `ID` | Unique numeric ID per résumé |
| `Resume_str` | Plain-text résumé (what we embed) |
| `Resume_html` | HTML version (unused) |
| `Category` | Job category label, e.g. `HR`, `ENGINEERING` (24 categories, 2,484 rows total) |

---

## 2. What "vectorization" actually means

A computer can't compare the *meaning* of two pieces of text directly — it can
only compare numbers. **Embedding** is the process of converting text into a
list of numbers (a **vector**) such that:
         
- Texts with similar *meaning* end up as vectors that are close together in
  space.
- Texts with different meaning end up far apart.

Concretely, our model (`all-MiniLM-L6-v2`) turns each résumé into a list of
**384 floating-point numbers**, e.g.:

```
"Senior Python backend developer with AWS experience"
  -> [0.0123, -0.0871, 0.0456, ..., 0.0219]   (384 numbers)
```

That list of 384 numbers is a single point in 384-dimensional space. You can't
visualize 384 dimensions, but the 2D/3D intuition transfers exactly: just like
two points close together on a map are "near" each other, two 384-dimensional
vectors close together represent texts with similar meaning.

### How does the model know what "similar meaning" is?

`all-MiniLM-L6-v2` is a **transformer neural network** (a distilled version of
BERT) that was pre-trained on hundreds of millions of sentence pairs (e.g.
duplicate questions, paraphrases, search query/result pairs). During training,
its internal weights were adjusted so that sentences known to be semantically
related get pushed to nearby points, and unrelated ones get pushed apart. That
training happened once, by the model's authors — we're just *using* the
already-trained model (this is called **inference**), not training it
ourselves.

Roughly, the model:
1. Splits text into subword **tokens** (`"developer"` → maybe `["develop",
   "##er"]`).
2. Passes tokens through several transformer layers, where each token's
   representation gets updated based on the other tokens around it (this is
   how it captures context — "Java" near "island" vs. "Java" near "backend"
   would land differently).
3. Averages ("pools") all the token vectors into one single 384-number vector
   representing the whole résumé. This pooled vector is the **embedding**.

---

## 3. What we did, step by step

### Step 1 — Install the tools
```
pip install -r requirements.txt
```
- `sentence-transformers`: loads the embedding model and turns text into
  vectors (also provides the `CrossEncoder` reranker used by `match_ao.py`/`search.py`).
- `qdrant-client`: talks to Qdrant, the vector database.

### Step 2 — `build_index.py`: build the index

Walking through the file:

```python
df = pd.read_csv(CSV_PATH)
```
Loads all 2,484 rows into memory.

```python
model = SentenceTransformer("all-MiniLM-L6-v2")
```
Downloads (first run only, then cached) and loads the embedding model
described above.

```python
client = QdrantClient(path=QDRANT_PATH)
```
Opens Qdrant in **local file mode** — it runs embedded in the Python process
and writes its data to `qdrant_resumes/qdrant_db/` on disk. No server, no
Docker, no network calls. (Trade-off: only one process can have that folder
open at a time.)

```python
client.create_collection(
    collection_name="resumes",
    vectors_config=VectorParams(size=384, distance=Distance.COSINE),
)
```
A **collection** in Qdrant is like a table. We declare up front:
- every vector will have exactly 384 numbers (must match the model's output
  size),
- similarity between vectors will be measured with **cosine distance**
  (explained in §4).

```python
embeddings = model.encode(texts, normalize_embeddings=True)
```
This is the actual vectorization step — every one of the 2,484 résumé texts
is run through the model and converted into its 384-number vector.
`normalize_embeddings=True` rescales every vector to length 1 (see §4 for why).

```python
points = [PointStruct(id=..., vector=..., payload={"category": ..., "resume_text": ...}) ...]
client.upsert(collection_name="resumes", points=points)
```
Each résumé becomes a **point**: its vector, plus a **payload** (ordinary
metadata stored alongside it — here, the category and a text snippet, so
search results are human-readable). `upsert` writes all points into the
collection.

Running this script once did the one-time work: read → embed → store. That's
why the index only needs rebuilding when the CSV changes.

### Step 3 — `search.py`: query the index

```python
query_vector = model.encode(args.query, normalize_embeddings=True)
```
The **same model** embeds your search text into a 384-number vector, the same
way every résumé was embedded. This is critical: query and documents must be
embedded with the same model, or their vectors live in incompatible spaces
and distances are meaningless.

```python
results = client.query_points(
    collection_name="resumes",
    query=query_vector,
    limit=args.limit,
)
```
Qdrant compares your query vector against all 2,484 stored vectors and
returns the closest ones — this is the **similarity search**.

```python
query_filter = Filter(must=[FieldCondition(key="category", match=MatchValue(...))])
```
Optional: `--category` narrows the search to only points whose payload
matches, combining exact metadata filtering with vector similarity in one
call.

---

## 4. What "similarity" means mathematically

We used **cosine similarity**. For two vectors A and B, it's:

```
cosine_similarity(A, B) = (A · B) / (|A| * |B|)
```

Intuition: ignore how *long* the vectors are, and only measure the **angle**
between them.
- Same direction (angle 0°) → cosine similarity = 1 (identical meaning)
- Perpendicular (angle 90°) → cosine similarity = 0 (unrelated)
- Opposite direction (angle 180°) → cosine similarity = -1 (opposite meaning)

Why angle rather than raw distance? A short résumé and a long résumé
discussing the exact same skills should count as similar — but a long
document naturally produces a "bigger" vector than a short one if you just
compare magnitudes. Cosine similarity cancels that out, comparing direction
(topic/meaning) only, not length.

`normalize_embeddings=True` rescales every vector to length 1 (`|A| = 1`)
*before* storing it. Once every vector has length 1, cosine similarity
simplifies to a plain dot product (`A · B`), which is cheaper for Qdrant to
compute at scale — that's why we normalize at index time, not just at query
time.

The `score` printed by `search.py` is exactly this cosine similarity, from
-1 to 1 (in practice usually 0 to 1 for real text). Higher = more similar.

---

## 5. How Qdrant finds the closest vectors fast

The naive approach — compare the query vector to *all* 2,484 stored vectors,
one by one — is called **brute-force / exact search**, and it's what
actually happens here since our dataset is small (Qdrant just does the full
scan; it's milliseconds at this scale).

At larger scale (millions of vectors), comparing against every single one
becomes too slow, so vector databases like Qdrant build an index structure
called **HNSW** (Hierarchical Navigable Small World graph): vectors are
connected into a multi-layer graph where similar vectors are linked, so a
search can "hop" toward the closest matches without visiting every point —
approximate, but orders of magnitude faster. You don't need to configure this
for our dataset size, but it's why Qdrant (rather than a plain Python loop)
is the right tool once data grows.

---

## 6. Why this beats keyword search

A keyword search for `"AWS backend developer"` only matches résumés
containing those literal words. Vector search instead matches on *meaning*:
a résumé saying "built cloud infrastructure and REST services on Amazon Web
Services" can score highly even without the word "backend" appearing at all,
because the model learned that these phrases are semantically related during
training.

That's exactly what we saw in the test query — the top result was an
"AWS Admin Intern" résumé describing cloud infrastructure work, not
necessarily an exact keyword match.

---

## 7. File summary

| File | Purpose |
|---|---|
| `build_index.py` | One-time (or re-run on data change): CSV → embeddings → Qdrant collection |
| `search.py` | Repeatable: query text → embedding → nearest résumés from Qdrant |
| `qdrant_db/` | The actual vector database files on disk (created by `build_index.py`) |

## 8. Quick glossary

- **Embedding / vector**: a list of numbers representing a piece of text's meaning.
- **Dimension**: how many numbers are in the vector (384 here).
- **Collection**: Qdrant's term for a table of vectors + payloads.
- **Payload**: ordinary metadata (non-vector fields) stored with each point.
- **Point**: one entry in a collection — an id + vector + payload.
- **Cosine similarity**: angle-based similarity measure between two vectors, range -1 to 1.
- **HNSW**: the graph-based index Qdrant uses to search large collections quickly.
- **Inference**: using an already-trained model to produce an output (as opposed to training it).
