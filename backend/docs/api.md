# API reference

Interactive documentation: `/docs` (Swagger) and `/redoc`. OpenAPI schema:
`/openapi.json`.

## Conventions

**Authentication.** Every endpoint except `/health*` and `/metrics` requires
`X-API-Key`. `X-User-Id` identifies the acting user for notifications,
preferences and the audit trail.

```
X-API-Key: dev-local-key
X-User-Id: amine
```

**Heavy endpoints return `202`.** Nothing in this API waits on a portal, a
parser or a mail server. The response carries a resource to poll.

**Errors have one shape.** Branch on `code` (stable), show `message` (human).

```json
{
  "code": "suspicious_content",
  "message": "PDF contains active content (scripts, auto-actions or embedded files) and was refused.",
  "detail": {"field": "file", "marker": "/JavaScript"},
  "request_id": "8f2c1a...",
  "retryable": false
}
```

`X-Request-ID` is echoed on every response and appears in every log line for
that request — quote it in a bug report.

**Collections are paginated.** `?page=1&page_size=25` (max 200), returning
`{items, total, page, page_size}`.

---

## Health & metrics

| Endpoint | Purpose |
|---|---|
| `GET /health/live` | Liveness. **No dependency checks** — a liveness probe that fails when the database is down would restart every healthy API pod during a database incident. |
| `GET /health/ready` | Readiness. `503` removes the pod from the load balancer. Only the database is readiness-critical; storage and broker outages degrade rather than remove. |
| `GET /health` | Human-readable detail with per-dependency latency. |
| `GET /metrics` | Prometheus exposition. |

---

## Entry point A — scraping

### `POST /scrape`

```bash
curl -X POST localhost:8000/scrape \
  -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{
    "connectors": ["tuneps"],
    "filters": {
      "keywords": ["développement", "maintenance applicative"],
      "keywords_any": true,
      "countries": ["Tunisie"],
      "organizations": ["Ministère"],
      "procurement_types": ["open", "expression_of_interest"],
      "cpv_codes": ["72"],
      "budget_min": 100000,
      "published_within_days": 30,
      "min_days_until_deadline": 10,
      "max_pages": 5,
      "max_results_per_source": 200
    }
  }'
```

```json
{
  "accepted": true,
  "message": "Scraping started across 1 source(s).",
  "job_id": "3f6c...",
  "task_id": "b91e...",
  "poll_url": "/scrape/jobs/3f6c..."
}
```

`connectors: []` (or omitted) means *every currently available source*.
Unavailable ones are skipped, not fatal.

**Filter vocabulary** — every entry point speaks the same model. Anything a
portal cannot express as a query parameter is applied client-side after
normalisation, so the UI can always offer the full set.

| Group | Fields |
|---|---|
| Free text | `keywords`, `keywords_any`, `excluded_keywords` |
| Geography | `countries`, `locations` |
| Buyer | `organizations`, `ministries`, `funding_organizations` |
| Classification | `procurement_types`, `procurement_categories`, `sectors`, `cpv_codes`, `document_types` |
| Dates | `publication_date_from` / `_to`, `deadline_from` / `_to`, `published_within_days`, `min_days_until_deadline` |
| Money | `budget_min`, `budget_max`, `currency` |
| Other | `statuses`, `languages`, `source_websites` |
| Run control | `max_results_per_source`, `max_pages` |

Relative shorthands (`published_within_days`) are resolved to absolute dates at
launch, so every connector in the job sees the same window and a replay is
identical.

If no requested connector is available:

```json
{
  "code": "http_400",
  "message": "None of the requested connectors are currently available.",
  "detail": {
    "requested": ["j360"],
    "skipped": ["j360"],
    "available": ["tuneps", "fixture"]
  }
}
```

### `GET /scrape/jobs/{id}`

```json
{
  "id": "3f6c...",
  "status": "partial",
  "progress": 1.0,
  "is_terminal": true,
  "connectors_total": 3,
  "connectors_succeeded": 2,
  "connectors_failed": 1,
  "items_found": 84,
  "items_ingested": 61,
  "items_duplicate": 22,
  "errors": [
    {"connector": "j360", "error_type": "SourceUnavailableError", "message": "..."}
  ],
  "runs": [
    {
      "connector_key": "tuneps",
      "status": "succeeded",
      "items_found": 42,
      "items_ingested": 31,
      "pages_fetched": 3,
      "http_requests": 47,
      "http_retries": 2,
      "extra": {"filter_application": {"server_side": ["keywords"], "client_side": ["cpv_codes"]}}
    }
  ]
}
```

`status: "partial"` is a **success with a caveat**, not a failure: two sources
delivered while one did not. `filter_application` shows how much of the filter
was pushed down to the portal versus applied locally.

| Status | Meaning |
|---|---|
| `pending` / `running` | in flight |
| `succeeded` | every connector succeeded |
| `partial` | at least one succeeded and at least one failed |
| `failed` | every connector failed |
| `cancelled` / `timed_out` | operator action / abandoned by a dead worker |

### Other

| Endpoint | Purpose |
|---|---|
| `GET /scrape/jobs` | list, filterable by `status` and `trigger` |
| `POST /scrape/jobs/{id}/cancel` | revoke queued tasks; running connectors finish their current page rather than being killed mid-write |

---

## Entry point B — upload

### `POST /upload`

```bash
curl -X POST localhost:8000/upload \
  -H "X-API-Key: dev-local-key" \
  -F "file=@cahier_des_charges.pdf" \
  -F "title=Développement d'une plateforme documentaire" \
  -F "buyer=Ministère des Technologies" \
  -F "country=Tunisie"
```

Accepts **PDF, DOCX, HTML** up to 25 MB. Validation is synchronous so the user
gets a specific answer immediately; everything after it is asynchronous.

A rejected file is **not stored, not parsed, not queued and not given a UUID**.

| Status | `code` | Cause |
|---|---|---|
| `413` | `file_too_large` | over the limit (enforced while reading, not from a header) |
| `415` | `unsupported_media_type` | extension not allowed, or content disagrees with the extension |
| `422` | `corrupted_file` | truncated PDF, DOCX missing its document part, empty HTML |
| `422` | `suspicious_content` | macros, embedded JS, `/OpenAction`, script/iframe, executable |
| `409` | `duplicate_tender` | already known — the response names the canonical tender |

```json
{
  "code": "duplicate_tender",
  "message": "This document is already known to the platform.",
  "detail": {
    "canonical_tender_id": "9c1d...",
    "matched_by": "raw_hash"
  }
}
```

---

## Entry point C — schedules

| Endpoint | Purpose |
|---|---|
| `GET /schedules/presets` | available cadence presets |
| `GET /schedules` | list |
| `POST /schedules` | create |
| `GET /schedules/{id}` | read |
| `PUT /schedules/{id}` | partial update |
| `DELETE /schedules/{id}` | delete |
| `POST /schedules/{id}/toggle` | enable / disable |
| `POST /schedules/{id}/run` | run now, without shifting the cadence |

```bash
curl -X POST localhost:8000/schedules \
  -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{
    "name": "tuneps-every-2-hours",
    "preset": "every_2_hours",
    "connectors": ["tuneps"],
    "filters": {"published_within_days": 7},
    "skip_if_running": true,
    "expire_seconds": 3600
  }'
```

Presets: `every_15_minutes`, `every_30_minutes`, `hourly`, `every_2_hours`,
`every_4_hours`, `every_6_hours`, `every_12_hours`, `daily`, `weekly`. Any
`interval_seconds` value or a crontab is equally valid:

```json
{
  "name": "weekdays-7am",
  "kind": "crontab",
  "cron_minute": "0", "cron_hour": "7", "cron_day_of_week": "1-5",
  "timezone": "Africa/Tunis"
}
```

Changes take effect within one Beat sync interval (~30 s) with **nothing
restarted**.

| Field | Why it matters |
|---|---|
| `skip_if_running` | a portal that got slow cannot accumulate concurrent runs |
| `expire_seconds` | after downtime, discards stale runs instead of firing forty catch-up scrapes at once |
| `one_off` | disables itself after firing |

---

## Tenders

### `GET /tenders`

| Parameter | Notes |
|---|---|
| `q` | free text over title, buyer, reference |
| `connectors`, `countries`, `sectors` | repeatable |
| `bands` | `highly_relevant` \| `relevant` \| `low_relevance` |
| `min_score` | 0–1 |
| `only_open` | default `true` — hides passed deadlines |
| `sort` | `-relevance_score` (default), `deadline`, `created_at`, … |

`sort` accepts only an allowlist of columns; anything else is a `400`.
`out_of_scope` tenders are stored for auditability but never listed.

```json
{
  "items": [{
    "id": "9c1d...",
    "title": "Développement d'une plateforme de gestion documentaire",
    "buyer": "Ministère des Technologies de la Communication",
    "deadline": "2026-08-28T12:00:00Z",
    "days_until_deadline": 20.4,
    "is_urgent": false,
    "relevance_score": 0.88,
    "relevance_band": "highly_relevant",
    "duplicate_hits": 2,
    "seen_on_sources": ["tuneps", "j360"]
  }],
  "total": 143, "page": 1, "page_size": 25
}
```

`is_urgent` (deadline within 7 days) and `days_until_deadline` drive the
dashboard badges; `seen_on_sources` is the "seen on 3 portals" signal.

### Other tender endpoints

| Endpoint | Notes |
|---|---|
| `GET /tenders/{id}` | full detail, documents, latest score breakdown |
| `GET /tenders/{id}/download` | **presigned URL**, not the bytes — the API never proxies a 25 MB file |
| `GET /tenders/{id}/scores` | full scoring history, so a past ranking stays explainable |
| `GET /tenders/stats/overview` | dashboard counters and band metadata (labels + colours) |

The score breakdown is per-criterion and human-readable:

```json
{
  "score": 0.88, "band": "highly_relevant", "profile_version": "2026.1",
  "breakdown": {
    "field_of_work": {
      "value": 1.0, "weight": 0.4, "weighted": 0.4,
      "explanation": "Matches 'Développement logiciel & applications' on: développement d'applications."
    },
    "deadline_proximity": {
      "value": 1.0, "weight": 0.25, "weighted": 0.25,
      "explanation": "20 days left — ideal response window."
    },
    "budget": {
      "value": 1.0, "weight": 0.1, "weighted": 0.1,
      "explanation": "1,250,000 TND — in the target range."
    }
  }
}
```

---

## Sources

| Endpoint | Purpose |
|---|---|
| `GET /sources` | health, circuit state, success rate, duplicate ratio |
| `GET /sources/registry` | **why a source is not running** — distinguishes disabled, missing credentials and broken configuration |
| `POST /sources/{key}/toggle` | operator kill switch, independent of the YAML flag |
| `POST /sources/{key}/reset-circuit` | the "I fixed it" button |
| `POST /sources/sync` | re-read connector YAML — applies a selector fix without a restart |

```json
{
  "connectors": [
    {"key": "tuneps", "available": true,  "requires_credentials": false},
    {"key": "j360",   "available": false, "unavailable_reason": "credentials_missing"}
  ],
  "available": ["tuneps", "fixture"],
  "errors": {}
}
```

---

## Notifications & preferences

| Endpoint | Purpose |
|---|---|
| `GET /notifications?unread_only=true` | my in-app notifications |
| `POST /notifications/{id}/read` | mark read |
| `GET /preferences` | my targeting rules |
| `PUT /preferences` | create or replace them |

```json
{
  "email": "amine@example.tn",
  "sectors": ["Technologies de l'information"],
  "countries": ["Tunisie"],
  "keywords": ["développement", "infogérance"],
  "excluded_keywords": ["génie civil"],
  "cpv_codes": ["72"],
  "channels": ["in_app", "email"],
  "min_relevance_band": "relevant",
  "digest_frequency": "immediate",
  "max_notifications_per_day": 50
}
```

**Every list means "no restriction on this dimension" when empty**, not "match
nothing" — a new user receives everything above their relevance floor rather
than silence. `max_notifications_per_day` is a hard cap so a scraping burst
cannot mail-bomb anyone. Each notification records *why* the user matched, in
`match_reason`.

---

## Admin

| Endpoint | Purpose |
|---|---|
| `GET /admin/scoring/profile` | current weights, bands and criteria |
| `POST /admin/scoring/reload` | pick up an edited `scoring.yaml` |
| `POST /admin/scoring/rescore?limit=5000` | re-score existing tenders |
| `GET /admin/duplicates` | **why a tender is not in the list** |
| `GET /admin/logs?tender_id=…` | reconstruct any tender's full journey |

The audit trail is queryable by `tender_id`, `job_id`, `connector`, `event` and
`level` — that is how you answer "when did the TUNEPS selector break?" months
later.
