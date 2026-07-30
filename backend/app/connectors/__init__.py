"""Source connectors.

Each portal gets its own package. Connectors are strictly isolated: none
imports another, none shares mutable state with another, and each runs in its
own Celery task. A connector that crashes, hangs, or starts returning garbage
degrades exactly one source.
"""

from app.connectors.base import BaseConnector, ConnectorContext
from app.connectors.models import ConnectorOutcome, NormalizedTender, RawRecord
from app.connectors.registry import ConnectorRegistry, get_registry

__all__ = [
    "BaseConnector",
    "ConnectorContext",
    "ConnectorOutcome",
    "ConnectorRegistry",
    "NormalizedTender",
    "RawRecord",
    "get_registry",
]
