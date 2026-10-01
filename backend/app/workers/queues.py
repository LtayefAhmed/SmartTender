"""Queue topology.

Work is split by *resource profile*, not by feature. Queues exist so that
classes of work with different cost and latency characteristics can be scaled
independently and cannot starve one another:

``scraping``       network-bound, minutes long, bursty. Isolated so a slow
                   portal can never occupy the workers that ingest uploads.
``parsing``        CPU-bound, seconds. The main pipeline throughput queue.
``ocr``            very CPU- and memory-heavy. Its own workers, low concurrency,
                   because two concurrent OCR jobs on a small box will swap.
``ai``             LLM and embedding work. Memory-heavy for a reason that is
                   easy to miss: Celery preforks, so *every worker process*
                   loads its own copy of the model. Eight processes and a
                   470 MB encoder is 3.7 GB of resident memory, which is how a
                   laptop stops responding. This queue is served by one worker
                   at concurrency 1 — the model is loaded once and reused.
``scoring``        cheap and fast; kept separate so a re-scoring sweep after a
                   weights change does not sit behind an hour of scraping.
``notifications``  I/O-bound on SMTP, must stay responsive.
``maintenance``    periodic housekeeping, lowest priority.

A deployment can run one worker across every queue, or eight specialised ones,
without a code change — see the compose file and the deployment guide.
"""

from __future__ import annotations

from kombu import Exchange, Queue

__all__ = [
    "DEFAULT_QUEUE",
    "QUEUES",
    "QUEUE_NAMES",
    "TASK_ROUTES",
    "redis_keys_for",
]

DEFAULT_QUEUE = "default"

#: Kombu's default separator between a queue name and its priority. Two
#: non-printing control characters, which is a trap worth naming: a Redis key
#: listing shows `scraping\x06\x165` as "scraping5", and `LLEN scraping5` then
#: queries a *different*, empty key. An hour was spent on that.
PRIORITY_SEPARATOR = "\x06\x16"

_exchange = Exchange("smarttender", type="direct", durable=True)

QUEUE_NAMES = (
    DEFAULT_QUEUE,
    "scraping",
    "parsing",
    "ocr",
    "ai",
    "scoring",
    "notifications",
    "maintenance",
)

QUEUES = tuple(
    Queue(name, _exchange, routing_key=name, durable=True) for name in QUEUE_NAMES
)


def redis_keys_for(
    name: str, *, steps: list[int] | None = None, sep: str | None = None
) -> list[str]:
    """Every Redis key one logical queue is spread across.

    Celery emulates priorities on Redis by giving each level its own list:
    priority 0 keeps the bare queue name, and every other level appends the
    separator and the number. So a single queue called ``scraping`` is really
    up to ten keys.

    This exists because the queue-depth metric counted only the bare name. Once
    priorities were switched on, the default priority became 5 — so almost
    every message went to a key nobody was measuring, and the dashboard read
    zero while work piled up. "The platform's single best saturation signal"
    was blind to most of the traffic, which is how three scraping jobs sat
    untouched for an hour with nothing anywhere reporting a problem.
    """
    from app.workers.celery_app import celery_app

    options = celery_app.conf.broker_transport_options or {}
    levels = steps if steps is not None else (options.get("priority_steps") or [0])
    separator = sep if sep is not None else options.get("sep", PRIORITY_SEPARATOR)
    return [name] + [f"{name}{separator}{level}" for level in levels if level]

#: Glob patterns, evaluated in order by Celery. Keeping routing declarative
#: here (rather than as a decorator argument on each task) means the topology
#: is reviewable in one place.
TASK_ROUTES = {
    "app.workers.tasks.scraping.*": {"queue": "scraping"},
    # Enrichment opens a portal page — for TUNEPS that means rendering an
    # Angular app in Chromium, which only the scraping worker's image carries.
    # Routed by *capability*, not by which stage of the pipeline it belongs to.
    "app.workers.tasks.pipeline.enrich_tender": {"queue": "scraping"},
    "app.workers.tasks.pipeline.parse_*": {"queue": "parsing"},
    "app.workers.tasks.pipeline.ocr_*": {"queue": "ocr"},
    "app.workers.tasks.pipeline.extract_*": {"queue": "ai"},
    "app.workers.tasks.pipeline.score_*": {"queue": "scoring"},
    "app.workers.tasks.pipeline.*": {"queue": "parsing"},
    # Indexing loads the embedding model. It belongs on `ai` with the rest of
    # the model-bound work, never on a high-concurrency queue: routed to
    # `scoring` (concurrency 8) it put eight copies of a 470 MB encoder in
    # memory and took the machine down.
    "app.workers.tasks.indexing.*": {"queue": "ai"},
    "app.workers.tasks.matching.*": {"queue": "ai"},
    "app.workers.tasks.profiles.*": {"queue": "ai"},
    "app.workers.tasks.cvs.*": {"queue": "ocr"},
    "app.workers.tasks.notifications.*": {"queue": "notifications"},
    "app.workers.tasks.maintenance.*": {"queue": "maintenance"},
}
