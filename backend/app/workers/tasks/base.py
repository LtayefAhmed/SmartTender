"""Shared task behaviour.

``PipelineTask`` centralises the retry policy so that no individual task has to
get it right. The rule it encodes comes straight from the exception hierarchy:

* ``retryable`` errors (network blips, 503s, storage hiccups) retry with
  exponential backoff and jitter;
* ``terminal`` errors (invalid file, duplicate, missing credentials) fail
  immediately, because retrying cannot change the answer;
* anything unrecognised gets one cautious retry, then stops.

Jitter is not decoration. Five hundred tenders arriving at once produce five
hundred tasks that fail together against a struggling dependency and would
otherwise retry in perfect lockstep, reproducing the exact spike that caused
the failure.
"""

from __future__ import annotations

import random
from typing import Any

from celery import Task

from app.core.exceptions import SmartTenderError
from app.core.logging import bind_context, get_logger

logger = get_logger(__name__)

__all__ = ["PipelineTask", "backoff_seconds"]


def backoff_seconds(retries: int, *, base: float = 4.0, cap: float = 600.0) -> float:
    """Exponential backoff with full jitter."""
    raw = min(cap, base * (2**retries))
    return random.uniform(raw / 2, raw)


class PipelineTask(Task):
    """Base class for every task in the platform."""

    #: Retries are requested explicitly by ``on_failure``-aware code paths
    #: rather than by blanket ``autoretry_for``, so the decision always passes
    #: through the exception's own metadata.
    max_retries = 3
    default_retry_delay = 30
    acks_late = True
    reject_on_worker_lost = True
    track_started = True

    def before_start(self, task_id: str, args: tuple, kwargs: dict) -> None:
        bind_context(task_id=task_id)
        for key in ("job_id", "tender_id", "connector_key"):
            value = kwargs.get(key)
            if value:
                bind_context(
                    **{
                        {"tender_id": "tender_uuid", "connector_key": "connector"}.get(key, key):
                        str(value)
                    }
                )

    # ------------------------------------------------------------------
    def retry_or_fail(self, exc: BaseException, **retry_kwargs: Any) -> None:
        """Apply the hierarchy's retry policy to an exception.

        Always raises: either ``Retry`` or the original exception.
        """
        retries = self.request.retries or 0

        if isinstance(exc, SmartTenderError):
            if exc.terminal or not exc.retryable:
                logger.warning(
                    "task.not_retrying",
                    task=self.name,
                    error_type=type(exc).__name__,
                    reason="terminal" if exc.terminal else "not_retryable",
                )
                raise exc
            countdown = retry_kwargs.pop("countdown", None)
            if countdown is None:
                explicit = getattr(exc, "retry_after_seconds", None)
                countdown = explicit if explicit else backoff_seconds(retries)
            logger.info(
                "task.retrying",
                task=self.name,
                attempt=retries + 1,
                max_retries=self.max_retries,
                countdown=round(float(countdown), 1),
                error_type=type(exc).__name__,
            )
            raise self.retry(exc=exc, countdown=countdown, **retry_kwargs)

        # Unknown failure: one cautious retry in case it was environmental.
        if retries < 1:
            logger.warning(
                "task.retrying_unknown_error",
                task=self.name,
                error_type=type(exc).__name__,
            )
            raise self.retry(exc=exc, countdown=backoff_seconds(retries), **retry_kwargs)
        raise exc

    def on_failure(
        self,
        exc: BaseException,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        logger.error(
            "task.final_failure",
            task=self.name,
            task_id=task_id,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            retries=self.request.retries,
        )
