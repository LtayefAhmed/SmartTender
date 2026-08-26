# Setup guide

## Requirements

| | Minimum | Notes |
|---|---|---|
| Python | 3.10 | 3.11 in the container image |
| Docker | 24+ | with Compose v2 |
| PostgreSQL | 14+ | 16 in Compose |
| Redis | 6+ | broker, results, rate limiter, breaker state |
| MinIO | any recent | or any S3-compatible store |

Playwright is **optional**. Install it only if you enable a connector with
`strategy: dynamic`; neither TUNEPS nor J360 needs it.

---

## Option 1 — the whole stack in Docker (fastest)

```bash
cd backend
cp .env.example .env
make up
```

That builds the image and starts PostgreSQL, Redis, MinIO, Mailpit, the API,
three specialised workers, Beat, Flower, Prometheus and Grafana. Migrations run
automatically as a one-shot `migrate` service before anything else starts.

```bash
make seed          # default schedules
make logs          # tail everything
```

| Service | URL | Credentials |
|---|---|---|
| API + Swagger | <http://localhost:8000/docs> | `X-API-Key: dev-local-key` |
| Celery dashboard | <http://localhost:5555> | — |
| MinIO console | <http://localhost:9001> | `minioadmin` / `minioadmin` |
| Notification inbox | <http://localhost:8025> | — |
| Prometheus | <http://localhost:9090> | — |
| Grafana | <http://localhost:3001> | `admin` / `admin` |

Verify:

```bash
curl -s localhost:8000/health | python -m json.tool
curl -s -H "X-API-Key: dev-local-key" localhost:8000/sources/registry | python -m json.tool
```

---

## Option 2 — application on the host, infrastructure in Docker

Better for development: fast reload and a real debugger.

```bash
cp .env.example .env   # at the repository root, next to docker-compose.yml

cd backend
# infrastructure only
docker compose -f ../docker-compose.yml up -d postgres redis minio mailpit

make install
make migrate
make seed
```

Then run the processes you need, each in its own terminal:

```bash
make api            # http://localhost:8000
make worker         # one worker across every queue
make beat           # the scheduler
```

> Run **exactly one** Beat instance. Two would double-fire every schedule.

---

## Option 3 — tests only, no infrastructure at all

The full suite runs with no database, broker, object store or network:

```bash
cd backend
make install
make test           # 281 tests, ~6 seconds
```

This is a deliberate property, not a limitation — see the
[developer guide](developer-guide.md#testing-strategy) for the seams that make
it possible.

---

## Configuration

`.env` covers deployment-specific and secret values; `config/*.yaml` covers
operational policy. Full reference: [configuration guide](configuration.md).

The values worth checking first:

```bash
SMARTTENDER_ENV=development
SMARTTENDER_API__API_KEYS=["dev-local-key"]   # production refuses to boot without these
SMARTTENDER_DB__PASSWORD=change-me
SMARTTENDER_STORAGE__SECRET_KEY=change-me
```

### Enabling TUNEPS (TUNTRUST certificate)

`tuneps.tn` is the transactional Tunisian e-procurement system and has **no
anonymous access**: suppliers authenticate with a TUNTRUST electronic
certificate presented as a TLS client certificate.

TUNTRUST issues PKCS#12; httpx needs PEM. Convert once:

```bash
mkdir -p certs
openssl pkcs12 -in tuntrust.p12 -clcerts -nokeys -out certs/tuneps-cert.pem
openssl pkcs12 -in tuntrust.p12 -nocerts -nodes  -out certs/tuneps-key.pem
```

Then in `.env` — paths as seen **inside** the container, since `./certs` is
mounted at `/app/certs`:

```bash
SMARTTENDER_CONNECTOR_TUNEPS_CERT_PATH=/app/certs/tuneps-cert.pem
SMARTTENDER_CONNECTOR_TUNEPS_KEY_PATH=/app/certs/tuneps-key.pem
SMARTTENDER_CONNECTOR_TUNEPS_KEY_PASSWORD=your-pin
```

A path that does not exist is treated as *missing*, so a typo skips the source
cleanly rather than failing with an opaque SSL error mid-run.

### Enabling J360 (paid subscription, browser-based)

J360 sits behind an anti-bot layer — plain HTTP returns a challenge page with no
form fields — so the connector drives a real browser.

```bash
SMARTTENDER_CONNECTOR_J360_USERNAME=your-account@example.com
SMARTTENDER_CONNECTOR_J360_PASSWORD=your-password
# or, if your plan grants API access (far cheaper and stabler):
SMARTTENDER_CONNECTOR_J360_API_KEY=your-api-key
```

It needs browsers available:

```bash
make install-browser                                   # host
docker build --target runtime-browser -t smarttender:browser .   # container
```

### Confirm what is runnable

```bash
smarttender-admin connectors
```

`credentials_missing` names the exact variables still needed — check
`GET /sources/registry` for the list.

> **First authenticated run:** the selectors in `config/connectors/*.yaml` for
> both real sources are **placeholders** — neither portal's live markup could be
> inspected without credentials. Run `smarttender-admin dry-run <key> --pages 1`
> (which writes nothing) and correct the YAML until it returns real tenders.
> A `SelectorBrokenError` there is the guard selector doing its job.

---

## First run

```bash
# 1. Which sources are runnable, and why not?
smarttender-admin connectors

# 2. Exercise the whole connector pipeline offline
smarttender-admin dry-run fixture

# 3. Try a real source without writing anything
smarttender-admin dry-run tuneps --pages 1 --limit 5

# 4. Launch a real scrape
curl -X POST localhost:8000/scrape \
  -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{"connectors":["tuneps"],"filters":{"keywords":["développement"],"published_within_days":30}}'

# 5. Poll it
curl -s -H "X-API-Key: dev-local-key" localhost:8000/scrape/jobs/<job_id> | python -m json.tool

# 6. Read the results
curl -s -H "X-API-Key: dev-local-key" "localhost:8000/tenders?bands=highly_relevant" | python -m json.tool
```

---

## Optional: the MiniLM semantic backend

The default `lexical` backend is pure Python, deterministic and dependency-free,
and for *duplicate detection* it is at least as good as embeddings. Switch only
if you want genuine paraphrase matching.

```bash
pip install -e ".[semantic]"
# place model.onnx and tokenizer.json under ./models/all-MiniLM-L6-v2/
SMARTTENDER_SEMANTIC__BACKEND=minilm
```

If the model files are missing the platform logs a warning and falls back to
`lexical`. A missing model degrades duplicate detection; it never stops
ingestion.

---

## Optional: Playwright

Only for connectors declaring `strategy: dynamic`:

```bash
make install-browser
# or in Docker:
docker build --target runtime-browser -t smarttender:browser .
```

---

## Troubleshooting

**`make test` fails on import**
Reinstall the package in editable mode: `make install`.

**A connector returns zero tenders**
Run `smarttender-admin dry-run <key> --pages 1`. If it raises
`SelectorBrokenError`, the portal's markup changed — fix the selectors in
`config/connectors/<key>.yaml` and run `POST /sources/sync`. No rebuild needed.

**`credentials_missing` for a source you configured**
The variable must be non-empty; a blank value is treated as unset on purpose,
so an empty `.env` line disables a source rather than attempting a login with an
empty password.

**Beat is not firing schedules**
Check exactly one Beat process is running, that the schedule is `enabled`, and
that its previous job is not still `running` (`skip_if_running`). The
`reconcile_stuck_jobs` task clears jobs abandoned by a dead worker within
10 minutes.

**Redis is unavailable**
Rate limiting and the circuit breaker degrade to per-process state and log a
warning once. Degraded politeness beats a stalled pipeline — but fix it, because
N workers then apply the configured rate N times over.
