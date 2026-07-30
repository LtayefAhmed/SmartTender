# Developer guide

## Adding a connector

A new source is normally **one YAML file plus a ten-line class**. The shared
control flow — pagination, retries, rate limiting, per-item error isolation,
normalisation, metrics — is implemented once in the generic bases.

### 1. Choose a strategy

In strict order of preference:

| Strategy | When | Cost |
|---|---|---|
| `api` | the portal documents a JSON API | cheapest, stable, explicit semantics |
| `static` | server-rendered HTML | cheap |
| `dynamic` | the listing genuinely needs JavaScript | ~100× the CPU and memory of an httpx GET |

A rendered page is expensive enough that the choice is a reviewable per-source
YAML setting rather than something a connector decides at runtime.

### 2. Write the configuration

`config/connectors/example.yaml`:

```yaml
key: "example"
name: "Example — marchés publics"
enabled: true
country: "Tunisie"
language: "fr"
timezone: "Africa/Tunis"
strategy: "static"

base_url: "https://example.tn"
endpoints:
  search: "/appels-offres"

pagination:
  mode: "query"            # query | path | cursor | offset
  page_param: "page"
  max_pages: 20
  stop_on_empty_page: true
  stop_after_consecutive_known: 2

filter_mapping:            # canonical name → the portal's parameter
  keywords: "q"
  organization: "acheteur"
  deadline_to: "dateLimiteFin"
  budget_min: null         # null = applied client-side after normalisation

filter_values:             # canonical enum → the portal's vocabulary
  procurement_type:
    open: "AO"
    expression_of_interest: "AMI"

selectors:
  list_container: "table.results"     # GUARD — see below
  list_item: "table.results tbody tr"
  no_results: "p.empty"
  item:
    reference: "td.ref"
    title: "td.objet a"
    detail_url: "td.objet a@href"
    buyer: "td.acheteur"
    deadline: "td.date-limite"
  detail:
    description: "div.description"
    estimated_budget: "dd.montant"
    documents: "ul.docs a@href"

parsing:
  date_formats: ["%d/%m/%Y %H:%M", "%d/%m/%Y"]
  decimal_separator: ","
  thousands_separator: " "
  default_currency: "TND"

required_fields: ["title", "source_url"]

http:
  rate_limit: { requests_per_second: 0.8 }

health:
  min_expected_items: 1
  empty_run_alert_threshold: 3
```

> **`list_container` is the guard selector and it is not optional.** If the page
> loads but that selector matches nothing, the connector raises
> `SelectorBrokenError` and alerts. Without it, a portal redesign produces
> HTTP 200 with zero rows and the platform is silently blind for a week.

### 3. Write the connector

`app/connectors/example/connector.py`:

```python
from app.connectors.generic.html_connector import HtmlListingConnector
from app.connectors.models import NormalizedTender, RawRecord
from app.connectors.registry import register


@register("example")
class ExampleConnector(HtmlListingConnector):
    """Only what is genuinely portal-specific belongs here."""

    def normalize(self, record: RawRecord) -> NormalizedTender:
        tender = super().normalize(record)
        tender.country = tender.country or "Tunisie"
        return tender
```

Also add an empty `app/connectors/example/__init__.py`. Nothing else changes:
discovery is automatic, and the registry, the API, the scheduler and the metrics
all pick it up.

For a JSON API, subclass `JsonApiConnector` and provide `response_mapping`
instead of `selectors`.

### 4. Save a page fixture and pin it with a test

```bash
curl -s "https://example.tn/appels-offres" > tests/fixtures/pages/example_listing.html
```

```python
def test_configured_selectors_match_the_snapshot(page_bytes):
    config = load_connector_config("example")
    engine = SelectorEngine(parse_html(page_bytes("example_listing.html")))

    engine.require(config.selectors["list_container"], what="listing")
    rows = engine.nodes(config.selectors["list_item"])
    assert len(rows) > 0
    assert rows[0].get(config.selectors["item"]["title"])
```

This is the regression test that matters: if a selector edit stops matching real
markup, it fails in CI rather than in production.

### 5. Verify

```bash
smarttender-admin connectors                    # is it registered and runnable?
smarttender-admin dry-run example --pages 1     # run it, write nothing
make test
```

---

## The connector contract

```python
fetch(filters)   -> AsyncIterator[FetchedPage]   # where bytes come from
parse(page)      -> list[RawRecord]              # where strings are; pure, no I/O
validate(record) -> None | raises ValidationError
normalize(record)-> NormalizedTender             # canonical vocabulary
```

Optional hooks: `setup`, `teardown`, `authenticate`, `enrich`,
`matches_filters`.

Three rules:

1. **Never do I/O in `parse`.** The downloader/parser seam is what lets a parser
   be tested against a saved fixture with no network at all.
2. **Never catch broadly in your own code.** `run()` already converts every
   failure into an outcome; swallowing errors yourself hides them from the
   metrics and the health tracker.
3. **Never let `run()` raise.** You get this for free by not overriding it.

### Why `run()` never raises

```python
outcome = await connector.run(filters)

outcome.succeeded      # False on failure — but no exception escaped
outcome.skipped        # disabled / no credentials / circuit open
outcome.tenders        # whatever was recovered before the failure
outcome.item_failures  # per-record failures that did not abort the run
outcome.error_type     # the exception class, as data
```

That single property is what makes "one broken source cannot stop the others"
true by construction rather than by discipline.

---

## Conventions

**Configuration, never constants.** If a value could differ between portals,
environments or months, it belongs in YAML or the environment.

**Structured logging only.** `print()` is banned. Use stable dotted event names
(`dedup.rejected`, `connector.selector_broken`) — they are queried far more
often than the free-text message.

```python
logger.info("connector.finished", connector=key, items=42, duration_seconds=3.1)
```

**Typed exceptions.** Raise from the hierarchy so the retry policy can read
`retryable` / `alerting` / `terminal` instead of matching on strings.

**Time is UTC and aware.** `utc_now()` is the only clock; `as_utc()` coerces
anything a driver hands back. A naive datetime that reaches arithmetic is a bug
that shifts a submission deadline by up to a day.

**Money and dates are locale-aware.** `1.234,56` is one thousand in Tunisia and
one point two in the US. Separators are configured per portal; guessing wrong is
a factor-of-1000 error on a budget.

**Idempotency by identity.** Tasks take a UUID and re-read state. At-least-once
delivery must produce at-most-once effect.

---

## Testing strategy

The whole suite runs with **no infrastructure and no network**, in about six
seconds. That is a design constraint: a suite needing docker-compose is a suite
people stop running, and a suite reaching a live portal fails whenever that
portal has a bad afternoon.

Four seams make it possible:

| Seam | Enables |
|---|---|
| Every column type has a SQLite variant | the real ORM, repositories and API run in memory |
| Parsers take bytes | parser tests run against saved fixtures |
| The default similarity backend is deterministic | dedup asserts exact values, not ranges |
| The `fixture` connector reads from disk | the whole pipeline runs end to end offline |

| Module | Covers |
|---|---|
| `test_core.py` | canonicalisation, hashing, filename safety, SSRF, redaction, config merging |
| `test_parsing.py` | selectors, guard-selector alerts, dates, money, JSON paths, **TUNEPS markup regression** |
| `test_validation.py` | every accept and reject path, streaming size enforcement |
| `test_deduplication.py` | all three stages, evidence recording, backend determinism |
| `test_scoring.py` | weighting, bands, missing-data neutrality, explainability, degradation |
| `test_connectors.py` | **the isolation invariant**, registry, credential gating, fixture end-to-end |
| `test_http_client.py` | retries, backoff, jitter, `Retry-After`, breaker, robots, UA rotation |
| `test_pipeline.py` | ingestion ordering, source health, job aggregation, notification targeting |
| `test_api.py` | every endpoint, error contract, `202` semantics |

```bash
make test
make test-cov
.venv/bin/pytest tests/test_connectors.py -k isolation -v
```

### Writing a good test here

Assert the *behaviour that protects the invariant*, not the implementation:

```python
def test_a_transport_failure_is_returned_not_raised(self):
    probe = _Probe(_config())
    probe.fetch_error = SourceUnavailableError("portal is down")
    outcome = _run(probe)               # no pytest.raises — that is the point

    assert outcome.succeeded is False
    assert outcome.error_type == "SourceUnavailableError"
```

---

## Database changes

```bash
# 1. edit app/db/models/*.py
make migration m="add tender.award_date"
# 2. READ the generated file — autogenerate misses server defaults,
#    index concurrency and data backfills
make migrate
```

New columns should be nullable or carry a server default; a `NOT NULL` column
without one fails on a table that already has rows.

---

## Adding a scoring criterion

```python
from app.services.scoring import CriterionResult, CriterionScorer, register_scorer


@register_scorer
class SubmissionLanguageScorer(CriterionScorer):
    name = "submission_language"

    def score(self, tender, config, context) -> CriterionResult:
        if not tender.language:
            return CriterionResult(None, "Language unknown.")   # None = not applicable
        preferred = config.get("preferred") or []
        matched = tender.language in preferred
        return CriterionResult(
            1.0 if matched else 0.3,
            f"Submission language is '{tender.language}'.",
        )
```

Then give it a weight and its config block in `scoring.yaml`. Return `None`
rather than `0.0` when the criterion does not apply — that is what keeps missing
data neutral instead of punitive.

---

## Debugging

```bash
smarttender-admin connectors                # why is a source not running?
smarttender-admin dry-run tuneps --pages 1  # run it, write nothing
smarttender-admin score <tender-id>         # why did it score that?
smarttender-admin health                    # which dependency is down?
```

Reconstruct any tender's full journey:

```bash
curl -s -H "X-API-Key: ..." "localhost:8000/admin/logs?tender_id=<uuid>" | python -m json.tool
```

Or find out why one is missing:

```bash
curl -s -H "X-API-Key: ..." "localhost:8000/admin/duplicates" | python -m json.tool
```
