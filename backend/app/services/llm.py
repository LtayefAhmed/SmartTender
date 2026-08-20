"""Mistral, used to refine what the deterministic pipeline could not.

An *improvement*, never a dependency. Every call site keeps its deterministic
result and only replaces it when the model returns something better-formed, so
a missing key, an exhausted quota, a timeout or an outage costs quality and
never availability. That is not defensive habit — it is what lets this be
switched on for one document type and off for another without the pipeline
noticing.

Three locks stand between a document and the network, and each catches what the
others do not:

1. **Scope.** ``LlmSettings.allows(kind)`` decides whether this *kind* of
   document may be sent at all, and defaults to tenders only.
2. **Redaction.** Whatever is sent passes through :mod:`app.services.redaction`
   first. Public notices lose nothing; a CV loses its identity and keeps its
   competence.
3. **Bounds.** A character ceiling per call and a timeout, because a model that
   silently costs more on a long dossier is how a free tier becomes a bill, and
   a slow one is how ingestion stops.

The redaction step is not optional even for tenders. A notice names its buyer's
contact — a person, with an email and a phone — and that person did not consent
to anything either.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["LlmResult", "MistralClient", "get_llm", "reset_llm"]


@dataclass(slots=True)
class LlmResult:
    """What a call produced, and what it cost.

    ``ok`` is false for every failure mode — disabled, out of scope, refused,
    timed out, malformed. The caller checks one flag and keeps its own result
    otherwise; it never has to distinguish "the model is off" from "the model
    broke", because the correct response is identical.
    """

    ok: bool
    content: str = ""
    reason: str | None = None
    #: Placeholders substituted before sending. Surfaced so a run can be shown
    #: to have been anonymised, without recording what was removed.
    redactions: dict[str, int] | None = None

    def as_json(self) -> Any | None:
        """Parse the content as JSON, tolerating a fenced code block.

        Models wrap JSON in ``` fences even when told not to. Failing on that
        would discard a correct answer over its packaging.
        """
        if not self.ok or not self.content:
            return None
        body = self.content.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[-1]
            body = body.rsplit("```", 1)[0]
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            logger.info("llm.non_json_response", head=body[:120])
            return None


class MistralClient:
    """A minimal, failure-tolerant client. No SDK — one endpoint, one shape."""

    def __init__(self) -> None:
        settings = get_settings().llm
        self.settings = settings

    # ------------------------------------------------------------------
    def complete(
        self,
        *,
        system: str,
        user: str,
        kind: str,
        known_names: list[str] | None = None,
        max_tokens: int = 1200,
    ) -> LlmResult:
        """Send one prompt. Returns a result rather than raising, always.

        ``kind`` is ``tender`` or ``cv`` and is checked against the configured
        scope before anything is prepared — the cheapest possible refusal, and
        one that cannot be skipped by a caller that forgot.
        """
        settings = self.settings
        if not settings.enabled:
            return LlmResult(ok=False, reason="disabled")
        if not settings.allows(kind):
            return LlmResult(ok=False, reason=f"out_of_scope:{kind}")

        from app.services.redaction import redact

        report = redact(user, known_names=known_names)
        payload_text = report.text[: settings.max_input_chars]
        truncated = len(report.text) > settings.max_input_chars

        try:
            content = self._post(system, payload_text, max_tokens)
        except Exception as exc:
            # Every failure lands here and looks the same to the caller, which
            # is the point: the deterministic result is kept either way.
            logger.warning(
                "llm.call_failed", kind=kind, error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            return LlmResult(ok=False, reason=type(exc).__name__)

        logger.info(
            "llm.call_completed",
            kind=kind,
            chars_sent=len(payload_text),
            truncated=truncated,
            # Counts only. Logging the values would move the personal data
            # rather than remove it.
            redactions=report.counts,
            chars_received=len(content),
        )
        return LlmResult(ok=True, content=content, redactions=report.counts)

    # ------------------------------------------------------------------
    def _post(self, system: str, user: str, max_tokens: int) -> str:
        import httpx

        settings = self.settings
        response = httpx.post(
            f"{settings.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.mistral_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.model,
                "temperature": settings.temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=settings.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        return str(body["choices"][0]["message"]["content"] or "")

    def health(self) -> LlmResult:
        """One cheap call, to prove the key works before anything depends on it."""
        return self.complete(
            system="Réponds exactement: OK",
            user="ping",
            kind="tender",
            max_tokens=8,
        )


_client: MistralClient | None = None
_lock = threading.Lock()


def get_llm() -> MistralClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = MistralClient()
    return _client


def reset_llm() -> None:
    """Drop the cached client. Tests and configuration reloads only."""
    global _client
    _client = None
