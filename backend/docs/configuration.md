# Configuration guide

Two layers, separated by lifetime and by audience.

| Layer | Location | Holds | Changed by |
|---|---|---|---|
| **Environment** | `.env`, container env | anything deployment-specific or secret | ops, at deploy time |
| **YAML policy** | `config/*.yaml` | operational policy a non-developer tunes | operators, at runtime |

Nothing in the codebase hardcodes a value that belongs in either. Base URLs,
selectors, timeouts, retry counts, weights, schedules, headers, user agents and
rate limits are all configuration.

---

## 1. Environment

All variables are prefixed `SMARTTENDER_`; nested settings use `__`.

### Runtime

| Variable | Default | Notes |
|---|---|---|
| `SMARTTENDER_ENV` | `development` | `production` enables stricter startup checks |
| `SMARTTENDER_DEBUG` | `false` | |
| `SMARTTENDER_LOG_LEVEL` | `INFO` | |
| `SMARTTENDER_LOG_FORMAT` | `console` | use `json` in production |
| `SMARTTENDER_CONFIG_DIR` | `./config` | where the YAML lives |

### API

| Variable | Default | Notes |
|---|---|---|
| `SMARTTENDER_API__API_KEYS` | `[]` | **production refuses to boot when empty** |
| `SMARTTENDER_API__ALLOW_ANONYMOUS` | `false` | |
| `SMARTTENDER_API__CORS_ORIGINS` | localhost | JSON list |
| `SMARTTENDER_API__ROOT_PATH` | `""` | set when behind a path-prefixing proxy |

### Database

| Variable | Default |
|---|---|
| `SMARTTENDER_DB__HOST` / `__PORT` / `__USER` / `__PASSWORD` / `__NAME` | localhost:5432 |
| `SMARTTENDER_DB__POOL_SIZE` | `10` |
| `SMARTTENDER_DB__MAX_OVERFLOW` | `20` |
| `SMARTTENDER_DB__STATEMENT_TIMEOUT_MS` | `30000` |

`statement_timeout` is belt-and-braces alongside Celery's limits: a lock wait
inside PostgreSQL is invisible to the worker's own timer, and an uncancelled one
holds a pool connection until the pool starves.

### Redis, storage, uploads, notifications, proxy, semantic backend

See `.env.example`, which documents every key inline. The ones most often
adjusted:

| Variable | Default | Notes |
|---|---|---|
| `SMARTTENDER_UPLOAD__MAX_BYTES` | `26214400` (25 MB) | enforced *while reading* |
| `SMARTTENDER_UPLOAD__REJECT_MACROS` | `true` | |
| `SMARTTENDER_UPLOAD__REJECT_ACTIVE_PDF_CONTENT` | `true` | |
| `SMARTTENDER_STORAGE__PRESIGNED_TTL_SECONDS` | `900` | download link lifetime |
| `SMARTTENDER_SEMANTIC__BACKEND` | `lexical` | or `minilm` |
| `SMARTTENDER_PROXY__ENABLED` | `false` | proxies are never mandatory |

### Worker limits

| Variable | Default | Notes |
|---|---|---|
| `SMARTTENDER_WORKER__TASK_SOFT_TIME_LIMIT_SECONDS` | `600` | raises inside the task so it can record its own failure |
| `SMARTTENDER_WORKER__TASK_TIME_LIMIT_SECONDS` | `900` | hard kill |
| `SMARTTENDER_WORKER__SCRAPING_SOFT_TIME_LIMIT_SECONDS` | `1800` | scraping runs longer |
| `SMARTTENDER_WORKER__BEAT_SYNC_INTERVAL_SECONDS` | `30` | how quickly a schedule edit takes effect |

### Document extraction

| Variable | Default | Notes |
|---|---|---|
| `SMARTTENDER_EXTRACTION__ENABLED` | `true` | |
| `SMARTTENDER_EXTRACTION__OCR_ENABLED` | `true` | degrades to a warning if Tesseract is absent |
| `SMARTTENDER_EXTRACTION__TESSERACT_CMD` | `""` | blank = on PATH (the container case) |
| `SMARTTENDER_EXTRACTION__OCR_LANGUAGES` | `fra+eng` | |
| `SMARTTENDER_EXTRACTION__MIN_CHARS_BEFORE_OCR` | `120` | below this, a page is treated as a scan |
| `SMARTTENDER_EXTRACTION__MAX_OCR_PAGES` | `20` | OCR costs ~1s/page, so it is bounded |
| `SMARTTENDER_EXTRACTION__MAX_PDF_PAGES` | `120` | |
| `SMARTTENDER_EXTRACTION__MAX_CHARS_PER_TENDER` | `500000` | |

On Windows, install Tesseract and set
`SMARTTENDER_EXTRACTION__TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe`.

### Connector credentials

| Variable | Source |
|---|---|
| `SMARTTENDER_CONNECTOR_TUNEPS_CERT_PATH` / `_KEY_PATH` / `_KEY_PASSWORD` | TUNEPS — TUNTRUST client certificate |
| `SMARTTENDER_CONNECTOR_TUNEPS_USERNAME` / `_PASSWORD` | TUNEPS — only if `form_login_required` |
| `SMARTTENDER_CONNECTOR_J360_USERNAME` / `_PASSWORD` | J360 (paid) |
| `SMARTTENDER_CONNECTOR_J360_API_KEY` | J360 (paid), alternative |

Blank is treated as unset, so an empty `.env` line disables a source rather than
attempting a login with an empty password. A certificate *path* that does not
exist is likewise treated as missing — a typo skips the source instead of
surfacing as an opaque SSL error mid-run.

`GET /sources/registry` names the specific variables still needed, so
`credentials_missing` is an instruction rather than a riddle.

> **In Docker**, these are interpolated from the repository root's `.env` by
> Compose, which reads the `.env` next to its own file. Hardcoding them
> as empty strings in `docker-compose.yml` would silently override real values
> and leave every paid source reporting `credentials_missing` with no visible
> cause.

---

## 2. `config/http.yaml` — fetching policy

Global defaults for every connector. Any block can be overridden per connector
under that connector's `http:` key; connector settings win, absent keys fall
back here.

```yaml
timeouts:
  connect_seconds: 10.0
  read_seconds: 30.0
  total_seconds: 120.0    # whole operation including retries

retry:
  max_attempts: 4              # 1 initial + 3 retries
  initial_backoff_seconds: 1.0
  backoff_multiplier: 2.0      # 1s → 2s → 4s → abort
  max_backoff_seconds: 30.0
  jitter_ratio: 0.25           # ±25%
  retry_on_status: [408, 425, 429, 500, 502, 503, 504]
  respect_retry_after: true

rate_limit:
  requests_per_second: 1.0
  burst: 3
  jitter_seconds: [0.2, 0.8]

robots:
  enabled: true
  allow_on_fetch_failure: true

user_agents:
  strategy: sticky             # fixed | random | round_robin | sticky | transparent
  pool: [...]

circuit_breaker:
  failure_threshold: 5
  recovery_timeout_seconds: 600
  success_threshold: 2

browser:
  headless: true
  block_resource_types: [image, media, font]

proxy:
  enabled: false
```

**Why jitter matters.** Five hundred tasks that fail together against a
struggling dependency would otherwise retry in perfect lockstep, reproducing
the exact spike that caused the failure.

**Why the rate limiter is in Redis.** Politeness is a property of the *portal*,
not of a process. Ten workers scraping TUNEPS must together stay under 1 req/s;
a per-process limiter silently multiplies the configured rate by the worker
count — the classic way a well-behaved scraper gets banned right after a
scale-up.

**Why `sticky` is the default UA strategy.** A session that changes its browser
identity mid-crawl is exactly what anti-bot heuristics look for.

---

## 3. `config/scoring.yaml` — relevance weights

The score is a weighted aggregation of independent criteria, each returning
0–1 plus a human-readable explanation.

```yaml
version: "2026.1"          # BUMP when you edit weights — it is persisted per score
weights:
  field_of_work: 0.40
  deadline_proximity: 0.25
  keywords: 0.15
  budget: 0.10
  location: 0.05
  organization: 0.05
  cpv_similarity: 0.00     # zero disables the criterion entirely
  historical_success: 0.00
  procurement_type: 0.00

bands:
  highly_relevant: { min_score: 0.75, label: "Highly Relevant", color: "#1BD3BC" }
  relevant:        { min_score: 0.50, label: "Relevant",        color: "#FFB454" }
  low_relevance:   { min_score: 0.00, label: "Low Relevance",   color: "#6C7BB8" }
```

Weights need not sum to 1: the engine normalises over the criteria that
**actually produced a value**. That is deliberate — a criterion with no data
(unknown budget, missing CPV codes) is excluded from the denominator rather than
counted as zero, so a portal that never publishes budgets does not have all its
tenders permanently penalised for that editorial habit.

### Criteria worth understanding

**`field_of_work`** — the highest-weighted criterion, deciding whether the
opportunity is yours at all. Edit `profiles` to describe what your company
actually does. An exact term occurrence short-circuits to a full match; failing
that, semantic proximity is used.

**`deadline_proximity`** — deliberately **not monotonic**. A deadline in two
days is nearly worthless because you cannot assemble a bid; one in eight months
is worth little because it is speculative. The curve peaks between
`ideal_min_days` and `ideal_max_days`.

**`keywords.exclude`** — a **veto**, not a weight. A civil-engineering tender is
not "slightly relevant" to a software integrator; it is out of scope, and goes
to the `out_of_scope` band so it can be excluded from the dashboard without
being confused with genuinely low-scoring IT work.

### Applying a change

```bash
curl -X POST localhost:8000/admin/scoring/reload -H "X-API-Key: ..."   # new tenders
curl -X POST "localhost:8000/admin/scoring/rescore?limit=5000" -H "X-API-Key: ..."   # existing
```

Past scores are never rewritten. Each execution is stored with its profile
version and full breakdown, so a ranking from three months ago is still
explainable and reproducible after the weights change.

---

## 4. `config/dedup.yaml` — duplicate policy

```yaml
canonical_url:
  strip_query_params: [utm_source, sessionid, gclid, ...]

content_hash:
  hash_raw_bytes: true
  hash_normalised_text: true

semantic:
  threshold: 0.92
  fields: { title: 1.0, buyer: 0.6, reference: 0.8, description: 0.4 }
  candidate_filter:
    lookback_days: 120
    deadline_tolerance_days: 3
    max_candidates: 200
    same_country: true
```

Tuning `threshold`:

- **too low** → distinct tenders from the same buyer get merged and silently
  disappear from the dashboard;
- **too high** → re-published notices appear twice.

`0.92` is calibrated for the `lexical` backend. Check the effect of a change
against `GET /admin/duplicates`, which shows what was rejected and why.

`candidate_filter` is what keeps stage 3 affordable. Widening `max_candidates`
or `lookback_days` increases per-record cost linearly.

---

## 5. `config/connectors/<key>.yaml` — one file per source

This is where a portal change is fixed. Full reference in the
[developer guide](developer-guide.md); the important blocks:

| Block | Purpose |
|---|---|
| `strategy` | `api` \| `static` \| `dynamic` — prefer them in that order |
| `base_url`, `endpoints` | where to fetch |
| `pagination` | mode, ceiling, early-stop conditions |
| `filter_mapping` | canonical filter name → the portal's query parameter |
| `filter_values` | canonical enum → the portal's vocabulary |
| `selectors` | **every** CSS/XPath selector, including the guard selector |
| `parsing` | date formats, decimal separator, default currency, boilerplate to strip |
| `required_fields` | what a record must have to be accepted |
| `http` | per-source overrides of `http.yaml` |
| `health` | expected item counts, alert thresholds |

### Selector mini-syntax

```
td.title                    text of the first match
td.title a@href             attribute value
h1.new, h1.old              fallbacks, tried left to right
xpath://td[@class='x']      XPath escape hatch
```

The comma-fallback form is what makes a portal redesign survivable: add the new
selector next to the old one and the connector works against both.

### Applying a selector fix without a restart

```bash
# edit config/connectors/tuneps.yaml
smarttender-admin dry-run tuneps --pages 1     # verify, writes nothing
curl -X POST localhost:8000/sources/sync -H "X-API-Key: ..."
```

In Docker the `config/` directory is bind-mounted read-only, so the edit is
visible to every container immediately.

---

## Precedence

```
environment variable
      ↓ overrides
connector YAML  (config/connectors/<key>.yaml)
      ↓ overrides
global YAML     (config/http.yaml, scoring.yaml, dedup.yaml)
      ↓ overrides
code defaults
```

Mappings merge key by key; **lists are replaced wholesale**, because a
partially-overridden selector list is never what the author meant.
