"""Transport layer: everything about *getting bytes*, nothing about tenders."""

from app.connectors.http.circuit_breaker import CircuitBreaker
from app.connectors.http.client import ResilientHttpClient
from app.connectors.http.proxy import ProxyPool
from app.connectors.http.rate_limiter import RateLimiter
from app.connectors.http.robots import RobotsPolicy
from app.connectors.http.user_agents import UserAgentPool

__all__ = [
    "CircuitBreaker",
    "ProxyPool",
    "RateLimiter",
    "ResilientHttpClient",
    "RobotsPolicy",
    "UserAgentPool",
]
