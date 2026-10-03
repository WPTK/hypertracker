"""Network helpers shared by routers. `client_ip` is the single place that decides
which address a request is attributed to (rate limits, logging)."""
from fastapi import Request


def client_ip(request: Request) -> str:
    """TCP peer address. Proxy-header trust is layered on top of this by the
    backend core (TRUSTED_PROXY setting); callers should not read headers."""
    return request.client.host if request.client else "unknown"
