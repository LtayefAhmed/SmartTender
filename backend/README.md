# SmartTender AI — Tender Detection & Ingestion Platform

Module 1 of SmartTender AI: a resilient, fully asynchronous platform that
detects public procurement opportunities across multiple sources, validates and
deduplicates them, scores their relevance, and notifies the right people.

The whole design serves one invariant:

> **A failure in one source, one connector, one request, or one worker must
> never stop the rest of the pipeline.**

---

## Three entry points, one pipeline

| Entry point | Endpoint | Behaviour |
|---|---|---|
| **A — Manual scraping** | `POST /scrape` | Advanced filters, fans out one isolated task per source, returns `202` in milliseconds |
| **B — Manual upload** | `POST /upload` | PDF / DOCX / HTML, validated synchronously with an explicit reason on rejection |
| **C — Scheduled scraping** | `/schedules` | Operator-editable cadences that take effect without restarting anything |

All three converge on the same sequence — deduplicate, mint UUID, store, persist,
enqueue, **extract**, score, notify — so behaviour never depends on how a
tender arrived.

```
   Manual upload      Manual scrape      Scheduled scrape
         │                  │                    │
         └──────────────────┼────────────────────┘
                            ▼
                    Validation layer          reject → explicit error, nothing stored
                            ▼
                  Duplicate detection         3 stages, cheapest first
                            ▼
                     UUID4 generation         the master identifier
                            ▼
                     MinIO (encrypted)        bytes
                            ▼
                    PostgreSQL metadata       facts
                            ▼
                       Celery queues          scraping · parsing · ocr · ai · scoring · notifications
                            ▼
    Attachments → Text extraction (+OCR) → Scoring → Notifications
                            ▼
                        Dashboard
```

**Why extraction sits before scoring.** `field_of_work` and `keywords` together
carry 55% of the scoring weight and both read the tender's text. A PDF tender
without extraction is judged on its title alone. Measured on the same document
in the running stack: **0.24 (low relevance) without extracted text, 0.79
(highly relevant) with it.**

---

## Quick start

```bash
cd backend
cp .env.example .env

make install          # virtualenv + dependencies
make test             # 281 tests, no infrastructure required, ~6 seconds

make up               # full stack: postgres, redis, minio, api, 3 workers, beat
make seed             # default schedules
```

- **Dashboard (the UI): <http://localhost:3000>**
- API and docs: <http://localhost:8000/docs>
- Celery dashboard: <http://localhost:5555>
- MinIO console: <http://localhost:9001>
- Notification inbox: <http://localhost:8025>
- Grafana: <http://localhost:3001>

Try it without touching a real portal:

```bash
make dry-run c=fixture      # runs the whole connector pipeline offline
make connectors             # which sources are runnable, and why not
```

---

## What makes it resilient

**Connector isolation.** Every source is its own package, its own Celery task
and its own configuration file. `BaseConnector.run()` never raises: it converts
every failure — transport error, broken selector, or an outright bug — into a
recorded outcome. A job with three healthy sources and one broken one is
`partial`, not `failed`.

**Bounded everything.** Connect, read, write, pool, whole-request budget, retry
backoff, page count, item count and run deadline all have ceilings. No code
path can wait forever.

**Circuit breakers.** After N consecutive failures a source is skipped without
touching the network, so a portal that has been down for six hours costs
nothing instead of four timeouts per run per worker.

**Failure classification, not string matching.** Every exception carries
`retryable` / `alerting` / `terminal`, and the retry policy reads those
attributes. A 503 retries with jittered backoff; a 401 never does.

**Self-healing.** Periodic reconciliation closes jobs whose worker died,
re-queues tenders whose task was never published, and flags sources that have
gone quiet.

**Silence is an alert.** The most dangerous failure is a portal that returns
HTTP 200 with an empty listing because a selector broke. Guard selectors turn
that into `SelectorBrokenError`, and `consecutive_empty_runs` turns a slow
version of it into a degraded-health warning.

---

## Document understanding

Text extraction is cheapest-first, the same principle as duplicate detection:

1. **Digital text layer** — pypdf / python-docx / BeautifulSoup. Pure Python,
   milliseconds, no system dependency. Handles most tender documents.
2. **OCR** — pypdfium2 rasterises (no Poppler needed, and BSD-licensed unlike
   PyMuPDF's AGPL), OpenCV cleans the page, Tesseract `fra+eng` reads it.

The fallback is decided **per page**, not per document, because tender files are
routinely hybrid: a digital cover page followed by a scanned, stamped annex.
Choosing one strategy for the whole file either wastes a second per page on
pages that did not need it, or silently loses the pages that did.

Tesseract ships in the container image. Without it the platform logs a warning
naming the fix and skips OCR — a scanned document then yields no text, never an
error.

---

## The operator dashboard

A React + TypeScript dashboard lives in [`../frontend`](../frontend) and ships
in the compose stack at <http://localhost:3000>. It is served by nginx, which
also reverse-proxies `/api` to the backend so the browser stays same-origin.

Every Module 1 feature has a screen:

| Page | What it does |
|---|---|
| **Tableau de bord** | Stat tiles, relevance-band distribution, volume by source, urgent-deadline list, source health |
| **Appels d'offres** | Filterable/sortable tender list; click a row for the detail drawer — full **score breakdown**, extracted document text, presigned download, attachments, "seen on N portals" |
| **Lancer un scraping** | Launch a filtered job across sources; **live per-connector progress** (ingested / found / duplicates / retries) |
| **Import manuel** | Drag-and-drop upload with synchronous, explicit validation feedback |
| **Planifications** | Create/edit/toggle/run schedules; presets and cadence, effective without a restart |
| **Sources & santé** | Health, success rate, duplicate ratio, circuit state; reset a breaker, reload config, see *why* a source is unavailable |
| **Notifications** | In-app feed + targeting preferences (sectors, keywords, veto terms, digest cadence) |
| **Administration** | Scoring weights + reload/re-score, rejected duplicates ("why isn't this showing?"), queryable audit log |

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to :8000
```

---

## Documentation

| Document | Contents |
|---|---|
| [Architecture](docs/architecture.md) | Components, sequence diagrams, data model, failure design |
| [Setup guide](docs/setup.md) | Local development from zero |
| [Configuration guide](docs/configuration.md) | Every knob: env, HTTP policy, scoring weights, dedup, connectors |
| [Deployment guide](docs/deployment.md) | Production topology, scaling, backups, security |
| [Developer guide](docs/developer-guide.md) | Adding a connector, conventions, testing strategy |
| [API reference](docs/api.md) | Every endpoint, with examples |

---

## Project layout

```
backend/
├── app/
│   ├── api/                REST layer — routers, dependencies, error translation
│   ├── connectors/         one package per source + the shared framework
│   │   ├── base.py         the contract: fetch / parse / validate / normalize
│   │   ├── registry.py     discovery, credential gating, availability
│   │   ├── generic/        config-driven HTML and JSON API bases
│   │   ├── http/           retries, backoff, rate limiting, robots, breaker, proxies
│   │   ├── browser/        Playwright, only for `strategy: dynamic`
│   │   ├── parsing/        selectors and normalisers (pure, no I/O)
│   │   ├── tuneps/         free public portal
│   │   ├── j360/           paid aggregator (skipped without credentials)
│   │   └── fixture/        deterministic local source for CI
│   ├── core/               config, logging, exceptions, metrics, security, identity
│   ├── db/                 models, session management
│   ├── schemas/            API contract and transfer objects
│   ├── services/           validation, dedup, storage, ingestion, scoring, notifications
│   └── workers/            Celery app, queues, tasks, database-backed Beat
├── config/                 YAML policy: http, scoring, dedup, connectors/
├── alembic/                migrations
├── deploy/                 Prometheus scrape config and alert rules
├── tests/                  281 tests, zero infrastructure
└── docs/
```

---

## Sources

Both real sources are credential-gated, and neither is anonymously scrapable.
That is a finding, not an oversight — see below.

| Source | Access | Status |
|---|---|---|
| **TUNEPS** | Public listing (Angular SPA) | ✅ **Working live** — rendered in Playwright, ~20 tenders/run. No credentials. |
| **J360** | Paid subscription, anti-bot | Implemented as a Playwright browser session. Skipped until credentials are configured. |
| **fixture** | Local files | Development and CI only. |

### TUNEPS — public listing, rendered from an Angular SPA

The "Avis A.O" listing at `tuneps.tn/portail/offres` is **publicly readable**:
no credentials are needed to browse it (a TUNTRUST certificate is only required
to *submit a bid*). But the page is an Angular single-page app that fetches an
encrypted API and renders a Material table client-side — there is no static HTML
and no stable JSON endpoint. So the connector uses the `dynamic` strategy:
Playwright renders the page, lets the app fetch and decrypt the data, and reads
the table, driving the paginator to walk the pages.

It is **verified working live** — a scrape returns real Tunisian tenders
(French and Arabic titles), keyed on the portal's own `epBidMasterId`. The
Angular `cdk-column-*` selectors are bound to the app's data model rather than
to styling, so they are unusually durable; when the table changes, the fix is a
selector edit in `config/connectors/tuneps.yaml`.

> The mutual-TLS machinery built for the certificate assumption still exists on
> `ConnectorConfig` and is ready for any future mTLS source; it is simply not
> needed for browsing TUNEPS.

### J360 needs a browser

Every plain HTTP request to `j360.info` returns HTTP 202 and an interstitial
titled *"Vérification que vous n'êtes pas un robot !"* with no form fields, so
static scraping cannot work at all. The connector drives Playwright, signs in
with the subscriber's account, and reuses that session for the run. It is
deliberately slow — 0.2 req/s, `max_pages: 5` — and is a supplementary source,
not a bulk feed. Requires the `runtime-browser` image or `make install-browser`.

### Selectors for both are unverified

Neither portal's live markup could be inspected without credentials, so the
selectors in `config/connectors/*.yaml` are placeholders. **Expect to correct
them on the first authenticated run** — a YAML edit plus `POST /sources/sync`,
no code change and no redeploy:

```bash
smarttender-admin dry-run tuneps --pages 1    # runs it, writes nothing
```

A `SelectorBrokenError` there means the guard selector did its job.

Credentials are read from the environment at call time and never logged,
persisted, or echoed in an error.

---

## Commands

```bash
make check                    # lint + tests
make test-cov                 # coverage report
make migration m="add x"      # autogenerate a migration
make dry-run c=tuneps         # run a connector, write nothing
smarttender-admin score <id>  # explain how a tender was scored
smarttender-admin health      # probe every dependency
```
