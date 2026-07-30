# Architecture

## Guiding principle

> **The pipeline must never block.**

Everything below is a consequence of that sentence. When a decision looks
unusual, it is almost always because the obvious alternative introduces a way
for one slow or broken thing to hold up everything else.

Three corollaries drive the concrete design:

1. **No request waits on work it does not have to.** Every heavy endpoint
   returns `202 Accepted` with a resource to poll.
2. **No failure propagates upward.** Failures are converted into recorded
   outcomes at the boundary where they occur.
3. **No wait is unbounded.** Every timeout, retry, page count and run has a
   ceiling.

---

## Component diagram

```mermaid
graph TB
    subgraph clients[Clients]
        UI[React dashboard]
    end

    subgraph gateway[API layer]
        API[FastAPI<br/>routers · validation · auth]
    end

    subgraph domain[Services]
        VAL[Validation<br/>MIME · structure · active content]
        DED[Deduplication<br/>URL → hash → semantic]
        ING[Ingestion<br/>UUID · store · persist · enqueue]
        SCO[Scoring engine<br/>weighted · explainable]
        NOT[Notifications<br/>targeting · delivery]
    end

    subgraph connectors[Connector framework]
        REG[Registry<br/>discovery · credential gating]
        BASE[BaseConnector<br/>fetch/parse/validate/normalize]
        HTTP[Resilient HTTP<br/>retry · backoff · rate limit<br/>robots · UA · proxy · breaker]
        BROW[Playwright<br/>dynamic sources only]
        TUN[tuneps]
        J36[j360]
        FIX[fixture]
    end

    subgraph async[Asynchronous processing]
        BEAT[Celery Beat<br/>database-backed scheduler]
        Q{{Redis queues<br/>scraping · parsing · ocr<br/>ai · scoring · notifications}}
        W1[worker-scraping]
        W2[worker-pipeline]
        W3[worker-support]
    end

    subgraph data[Persistence]
        PG[(PostgreSQL<br/>metadata · audit · schedules)]
        S3[(MinIO<br/>original documents)]
    end

    subgraph obs[Observability]
        PROM[Prometheus]
        LOGS[Structured logs]
    end

    UI --> API
    API --> VAL --> ING
    API -->|enqueue only| Q
    W2 --> EXT[Text extraction<br/>digital → OCR per page]
    EXT --> SCO
    BEAT -->|reads schedules| PG
    BEAT --> Q
    Q --> W1 & W2 & W3
    W1 --> REG --> BASE
    BASE --> HTTP --> TUN & J36
    BASE --> BROW
    BASE --> FIX
    W1 --> ING
    ING --> DED --> PG
    ING --> S3
    W2 --> SCO --> PG
    W3 --> NOT --> PG
    API --> PG
    API -.presigned URL.-> S3
    API & W1 & W2 & W3 --> PROM
    API & W1 & W2 & W3 --> LOGS

    classDef store fill:#16214C,stroke:#5B8CFF,color:#EAF0FF
    class PG,S3 store
```

---

## The three entry points

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Q as Redis
    participant W as Worker

    rect rgba(91,140,255,.08)
    note over U,W: A — manual scraping
    U->>API: POST /scrape {connectors, filters}
    API->>API: resolve availability (skip unavailable)
    API->>DB: INSERT scraping_job (pending)
    API->>DB: COMMIT
    API->>Q: publish run_scraping_job
    API-->>U: 202 {job_id, poll_url}
    note right of API: total server time ≈ milliseconds
    end

    rect rgba(27,211,188,.08)
    note over U,W: B — manual upload
    U->>API: POST /upload (multipart)
    API->>API: validate (size, MIME, structure, active content)
    alt rejected
        API-->>U: 4xx with an explicit reason
        note right of API: nothing stored, queued, or given a UUID
    else accepted
        API->>DB: dedup + INSERT tender
        API->>Q: publish process_tender (after commit)
        API-->>U: 202 {tender_id}
    end
    end

    rect rgba(255,180,84,.08)
    note over U,W: C — scheduled scraping
    participant B as Beat
    B->>DB: poll change sentinel
    DB-->>B: schedules (only if changed)
    B->>B: due? not expired? previous run finished?
    B->>Q: publish run_scraping_job
    end
```

**Why publishing happens after `COMMIT`:** Redis is faster than a PostgreSQL
commit. Publishing inside the transaction lets a worker start, query for the
row, and find nothing — intermittently, and only under load. `ingestion.py`
attaches the publish to SQLAlchemy's `after_commit` event for exactly this
reason.

---

## Connector fan-out — where isolation is enforced

```mermaid
sequenceDiagram
    autonumber
    participant J as run_scraping_job
    participant DB as PostgreSQL
    participant Q as Redis
    participant C1 as run_connector(tuneps)
    participant C2 as run_connector(j360)
    participant C3 as run_connector(other)

    J->>DB: create one connector_run per source
    J->>Q: publish N independent tasks
    note over J: returns immediately — never waits

    par isolated
        C1->>C1: run() → 42 tenders
        C1->>DB: ingest + fold counters
    and
        C2->>C2: run() → SourceUnavailableError
        C2->>DB: record failure + fold counters
        note right of C2: returns an outcome,<br/>does not raise
    and
        C3->>C3: run() → skipped (no credentials)
        C3->>DB: fold counters
    end

    C1->>DB: all runs terminal? → derive job status
    note over DB: 1 success + 1 failure + 1 skip = PARTIAL
```

There is deliberately **no chord** and no result-dependency between the tasks. A
chord's callback is skipped when any member fails, which would mean one broken
portal suppressing the results of every healthy one — precisely the coupling
this architecture exists to prevent. Aggregation happens in the database, so
the fan-out survives restarts, retries and partial failures.

---

## Inside one connector run

```mermaid
flowchart TD
    START([run]) --> PRE{enabled?<br/>right env?<br/>credentials?}
    PRE -->|no| SKIP[skipped<br/>succeeded=true]
    PRE -->|yes| CB{circuit closed?}
    CB -->|open| SKIP
    CB -->|closed| SETUP[setup: HTTP client, session]

    SETUP --> AUTH{needs auth?}
    AUTH -->|yes| LOGIN[authenticate]
    AUTH -->|no| FETCH
    LOGIN --> FETCH[fetch: yield pages]

    FETCH --> DEADLINE{time left?}
    DEADLINE -->|no| DONE
    DEADLINE -->|yes| PARSE[parse: page → records]

    PARSE --> GUARD{guard selector matched?}
    GUARD -->|no| BROKEN[SelectorBrokenError<br/>ALERT · abort run]
    GUARD -->|yes| ITEM[per record]

    ITEM --> ENRICH[enrich: detail page<br/>best-effort]
    ENRICH --> VALIDATE{required fields?}
    VALIDATE -->|no| ITEMFAIL[record failure<br/>continue]
    VALIDATE -->|yes| NORM[normalize → canonical model]
    NORM --> SEEN{seen in this run?}
    SEEN -->|yes| ITEM
    SEEN -->|no| FILTER{matches filters?}
    FILTER -->|no| ITEM
    FILTER -->|yes| COLLECT[collect]
    COLLECT --> ITEM
    ITEMFAIL --> ITEM

    ITEM --> FETCH
    FETCH --> DONE([outcome])
    BROKEN --> DONE

    style SKIP fill:#FFB454,color:#0A102B
    style BROKEN fill:#FF2D87,color:#fff
    style DONE fill:#1BD3BC,color:#0A102B
```

Note that **every terminal state is an outcome object**, including the failures.
`run()` has no path that raises.

---

## Duplicate detection — three stages, cheapest first

```mermaid
flowchart LR
    IN[incoming record] --> S1{canonical URL<br/>indexed equality}
    S1 -->|hit| REJ[reject + record evidence<br/>attach source to canonical]
    S1 -->|miss| S1B{portal external_id}
    S1B -->|hit| REJ
    S1B -->|miss| S2{content hash<br/>raw + normalised text}
    S2 -->|hit| REJ
    S2 -->|miss| S3[candidate window<br/>lookback · same country<br/>deadline ±3d · max 200]
    S3 --> SIM{similarity ≥ 0.92?}
    SIM -->|yes| REJ
    SIM -->|no| ACC[accept]

    style REJ fill:#FF2D87,color:#fff
    style ACC fill:#1BD3BC,color:#0A102B
```

Ordering is the performance design, not an implementation detail. Stages 1 and
2 are single indexed lookups and resolve the large majority of records; stage 3
runs only on survivors, and only against a bounded candidate window. That keeps
duplicate detection **O(1) per record** even when 500 tenders arrive at once,
instead of O(n²) across the corpus.

A confirmed duplicate is rejected but never discarded: the evidence goes to
`duplicate_records` and the new source is attached to the canonical tender, so
the dashboard can say *"seen on 3 portals"* and an operator can always answer
*"why is this tender not showing up?"*.

---

## Data model

```mermaid
erDiagram
    SOURCES ||--o{ CONNECTOR_RUNS : "produces"
    SOURCES ||--o{ TENDERS : "discovers"
    SCRAPING_JOBS ||--o{ CONNECTOR_RUNS : "fans out to"
    SCHEDULES ||--o{ SCRAPING_JOBS : "triggers"
    TENDERS ||--o{ TENDER_DOCUMENTS : "has"
    TENDERS ||--o{ TENDER_SCORES : "scored by"
    TENDERS ||--o{ DUPLICATE_RECORDS : "absorbed"
    TENDERS ||--o{ NOTIFICATIONS : "announced by"
    USER_PREFERENCES ||--o{ NOTIFICATIONS : "targets"

    SOURCES {
        int id PK
        string key UK
        bool enabled
        string health
        string circuit_state
        int consecutive_failures
        int consecutive_empty_runs
        string config_checksum
    }
    SCRAPING_JOBS {
        uuid id PK
        string trigger
        string status
        jsonb filters
        int connectors_succeeded
        int connectors_failed
        int connectors_skipped
    }
    CONNECTOR_RUNS {
        uuid id PK
        string connector_key
        string status
        int items_found
        int items_ingested
        string error_type
    }
    TENDERS {
        uuid id PK "master identifier"
        string source_key
        string canonical_url UK
        string raw_sha256
        string text_sha256
        timestamptz deadline
        numeric estimated_budget
        float relevance_score
        string relevance_band
        string storage_key
        string pipeline_state
    }
    TENDER_SCORES {
        uuid id PK
        string profile_version
        float score
        jsonb breakdown
        jsonb weights
    }
    SCHEDULES {
        uuid id PK
        string name UK
        string kind
        int interval_seconds
        bool skip_if_running
        timestamptz last_run_at
    }
    EXECUTION_LOGS {
        bigint id PK
        string event
        uuid tender_id
        uuid job_id
        jsonb context
    }
```

### The master UUID

`tenders.id` is a UUID4 minted at ingestion, and it is deliberately the *same*
value used as:

- the object key prefix in MinIO,
- the sole argument to every downstream Celery task,
- the correlation key in logs and in `execution_logs`,
- the idempotency token that makes a replayed task overwrite rather than
  duplicate.

That single identity is what makes at-least-once delivery (which a Redis broker
absolutely will produce) result in at-most-once effect.

### Two stores, one rule

**Bytes in MinIO, facts in PostgreSQL.** A 20 MB PDF in a database column
bloats every backup, defeats connection pooling and makes the table
unqueryable. Downloads are served as short-lived presigned URLs — the API never
proxies file bytes, because streaming a 25 MB file would occupy a request
worker for the whole transfer.

---

## Document understanding

```mermaid
flowchart LR
    IN[stored document] --> TYPE{real type?}
    TYPE -->|PDF| PAGES[per page: text layer]
    TYPE -->|DOCX| DOCX[paragraphs + tables]
    TYPE -->|HTML| HTML[strip chrome, take text]

    PAGES --> ENOUGH{>= min chars?}
    ENOUGH -->|yes| KEEP[keep digital text]
    ENOUGH -->|no| OCR[rasterise → clean → Tesseract]
    OCR --> KEEP

    KEEP --> CLEAN[rejoin hyphens<br/>normalise ligatures]
    DOCX --> CLEAN
    HTML --> CLEAN
    CLEAN --> STORE[(extracted_text<br/>deferred column)]
    STORE --> SCORE[scoring reads it]

    style OCR fill:#FFB454,color:#0A102B
    style SCORE fill:#1BD3BC,color:#0A102B
```

Three decisions carry the value:

**The OCR fallback is per page, not per document.** Tender files are routinely
hybrid — a digital cover page followed by a scanned, stamped annex. Choosing one
strategy for the whole file either wastes ~1s/page on pages that did not need
it, or silently loses the pages that did.

**`extracted_text` is a deferred column.** It holds hundreds of kilobytes per
tender, and the dashboard's listing query selects whole `Tender` rows. Loading
it eagerly would turn a 200-row page into tens of megabytes on the wire.

**Extracted text feeds scoring but never deduplication.** `full_text` is
excluded from `comparison_text()`, because two unrelated tenders that attach the
same boilerplate annex would otherwise look identical.

Measured on one document in the running stack: **0.24 (low relevance) without
extracted text, 0.79 (highly relevant) with it.**

---

## Failure design

Every exception carries three attributes, and the retry policy reads them
instead of matching on messages:

| Attribute | Meaning | Consequence |
|---|---|---|
| `retryable` | may succeed if attempted again | exponential backoff with jitter |
| `alerting` | a human must look at it | raises an alert; does not retry blindly |
| `terminal` | definitively unusable | recorded and dropped, no alarm |

```
SmartTenderError
├── ConfigurationError            alerting, terminal
├── ConnectorError
│   ├── SourceUnavailableError    retryable
│   ├── RateLimitedError          retryable, honours Retry-After
│   ├── CircuitOpenError          alerting — skipped, not retried
│   ├── AuthenticationError       alerting, terminal — never retried
│   ├── CredentialsMissingError   terminal, NOT alerting (expected for J360)
│   ├── RobotsDisallowedError     terminal
│   ├── DownloadError             retryable
│   └── ParsingError
│       ├── SelectorBrokenError   alerting ← the highest-value alert
│       └── NormalizationError    terminal
├── ValidationError               terminal
│   ├── FileTooLargeError
│   ├── UnsupportedMediaTypeError
│   ├── CorruptedFileError
│   └── SuspiciousContentError    alerting
├── DuplicateTenderError          terminal, NOT alerting (high-volume normal)
├── StorageError                  retryable, alerting
├── NotificationError             retryable
└── ScoringError                  degrades to remaining criteria
```

Two of these deserve emphasis:

**`CredentialsMissingError` is not alerting.** Running without a J360
subscription is a normal configuration, not an incident. The source is skipped
quietly and every other connector runs.

**`SelectorBrokenError` is the alert that matters most.** It is the difference
between *"this portal published nothing today"* and *"we have been silently
blind to this portal for a week"*.

---

## Bounded waits

| Boundary | Mechanism |
|---|---|
| Single HTTP request | connect / read / write / pool timeouts |
| Request including retries | `total_seconds` budget, checked before each attempt |
| Retry backoff | exponential, capped, jittered ±25% |
| Attempts | `max_attempts` (default 4), then a typed failure |
| Repeatedly failing source | circuit breaker opens, calls refused without network |
| Pagination | `max_pages`, plus early stop on empty or already-seen pages |
| Connector run | `deadline_seconds`, stops cleanly and returns partial results |
| Celery task | soft limit (records its own failure) + hard limit (killed) |
| Database statement | `statement_timeout` |
| Rate limit sleep | capped per acquisition |
| Notification volume | per-user daily cap |

---

## Queue topology

Work is split by **resource profile**, not by feature, so classes of work with
different cost characteristics scale independently and cannot starve each
other.

| Queue | Profile | Why separate |
|---|---|---|
| `scraping` | network-bound, minutes, bursty | a slow portal must never occupy the workers that ingest uploads |
| `parsing` | CPU-bound, seconds | main throughput queue |
| `ocr` | very CPU/memory heavy | needs low concurrency; two concurrent jobs will swap a small box |
| `ai` | slow, rate-limited, retry-prone | isolates LLM/embedding latency |
| `scoring` | cheap and fast | a re-scoring sweep must not sit behind an hour of scraping |
| `notifications` | I/O-bound on SMTP | must stay responsive |
| `maintenance` | periodic housekeeping | lowest priority |

---

## Scheduling

Celery's stock scheduler reads a Python dict frozen at process start, so
changing a cadence means editing code and restarting Beat. `DatabaseScheduler`
treats the `schedules` table as the source of truth and polls a single-row
sentinel, reloading only when that timestamp moves — so "check for changes"
costs one trivial query per interval regardless of how many schedules exist,
and an edit made through the API takes effect within one interval.

Two production protections:

- **Expiry** — after downtime, Beat would otherwise fire every missed run at
  once. `expire_seconds` discards stale runs; a stampede of forty catch-up
  scrapes is worse than skipping them.
- **Overlap** — `skip_if_running` refuses to start a schedule whose previous job
  is still going, so a portal that got slow cannot accumulate concurrent runs.

---

## Self-healing

A distributed pipeline that only moves forward accumulates state nobody owns.
Four reconciliation loops repair it:

| Task | Cadence | Repairs |
|---|---|---|
| `reconcile_stuck_jobs` | 10 min | jobs whose worker died — otherwise `skip_if_running` blocks that schedule forever |
| `requeue_stalled_tenders` | 15 min | tenders committed but never enqueued (broker blip) |
| `flush_pending_notifications` | 10 min | notifications created during a broker outage |
| `check_connector_health` | hourly | sources that have gone silent or degraded |

---

## Security

| Concern | Control |
|---|---|
| Upload validation | extension + magic-byte MIME + agreement between them + structural integrity |
| Active content | PDF `/JavaScript` `/OpenAction` `/Launch` `/EmbeddedFile`; DOCX `vbaProject.bin`; HTML script/iframe/handlers |
| Zip bombs | uncompressed-size ratio check on DOCX |
| Path traversal | `sanitize_filename` strips both separator conventions; `safe_object_key` refuses `..` |
| SSRF | scraped links are checked against private/link-local ranges before fetching |
| Secrets | read from the environment at call time; never persisted, logged or echoed — `redact()` masks leaves recursively |
| Storage | encrypted at rest (SSE), private bucket, time-limited presigned URLs only |
| API | constant-time key comparison; production refuses to boot without keys |
| Transport | TLS verification always on; security headers on every response |

---

## Performance

Handling 500+ simultaneous arrivals rests on four properties:

1. **The request path does no heavy work** — ingestion is a dedup lookup, one
   object PUT and one INSERT.
2. **Duplicate detection is O(1) per record** — indexed equality first, bounded
   candidate window last.
3. **Fan-out is horizontal** — one task per connector, one task per tender,
   scaled by adding workers.
4. **Nothing serialises on a shared lock** — counters are incremented, not
   read-modify-written, and job status is derived rather than coordinated.
