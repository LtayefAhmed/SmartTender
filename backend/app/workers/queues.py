"""Queue topology.

Work is split by *resource profile*, not by feature. Queues exist so that
classes of work with different cost and latency characteristics can be scaled
independently and cannot starve one another:

``scraping``       network-bound, minutes long, bursty. Isolated so a slow
                   portal can never occupy the workers that ingest uploads.
``parsing``        CPU-bound, seconds. The main pipeline throughput queue.
``ocr``            very CPU- and memory-heavy. Its own workers, low concurrency,
                   because two concurrent OCR jobs on a small box will swap.
``ai``             LLM/embedding calls: slow, rate-limited, retry-prone.
``scoring``        cheap and fast; kept separate so a re-scoring sweep after a
                   weights change does not sit behind an hour of scraping.
``notifications``  I/O-bound on SMTP, must stay responsive.
``maintenance``    periodic housekeeping, lowest priority.

A deployment can run one worker across every queue, or eight specialised ones,
without a code change — see the compose file and the deployment guide.
"""

from __future__ import annotations

from kombu import Exchange, Queue

__all__ = ["DEFAULT_QUEUE", "QUEUES", "QUEUE_NAMES", "TASK_ROUTES"]

DEFAULT_QUEUE = "default"

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
    "app.workers.tasks.notifications.*": {"queue": "notifications"},
    "app.workers.tasks.maintenance.*": {"queue": "maintenance"},
}
