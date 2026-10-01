"""The reformulation pass, on the worker that owns the encoder.

The adaptation needs two models: Mistral to reword, and the multilingual
encoder to check that the rewording still says what the source said. The
encoder is 470 MB resident, and loading it in the API would put a copy in every
request process — the mistake that took the host down when indexing ran on an
eight-process queue.

So the pass runs here, on ``ai``, served by a single worker that already holds
one warm copy and vouches for it at boot. The API asks a question and waits,
exactly as it does for a matching run.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger, log_context
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["adapt_cv_experiences"]


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.generation.adapt_cv_experiences",
    queue="ai",
    max_retries=0,
)
def adapt_cv_experiences(
    self: PipelineTask,
    experiences: list[dict[str, Any]],
    requirements: list[str] | None = None,
    limit: int = 8,
    past_responses: list[str] | None = None,
    tenant: str = "default",
) -> dict[str, Any]:
    """Reword and reorder one career against one tender's requirements."""
    from app.services.adaptation import adapt_experiences
    from app.services.embeddings import get_embedder
    from app.services.memory import recall_responses

    with log_context(roles=len(experiences)):
        embedder = get_embedder()

        # The referential, consulted here because this worker already holds
        # the encoder the search needs. Empty until a first dossier is
        # approved, and empty is a correct answer: the prompt then carries no
        # examples and the document comes out as it did before.
        recalled = past_responses or [
            item.text
            for item in recall_responses(" ".join(requirements or [])[:2_000], tenant=tenant)
        ]

        result = adapt_experiences(
            experiences,
            requirements=requirements,
            limit=limit,
            past_responses=recalled,
            # The semantic net. Injected rather than imported by the service so
            # that the service stays usable — and testable — without a model.
            similarity=embedder.similarity,
        )
        logger.info(
            "generation.adaptation_done",
            status=result.status,
            reformulated=result.reformulated,
            rejected=result.rejected,
            recalled=len(recalled),
        )
        return {"experiences": result.experiences, "report": result.to_dict()}
