"""Network helpers shared by routers. `client_ip` is the single place that decides
which address a request is attributed to (rate limits, logging); `rate_limit`
is the shared sliding-window limiter."""
import ipaddress
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from . import config


def now() -> float:
    """Wall clock for rate limiting. Module-level so tests can patch it."""
    return time.time()


def _parse_ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _is_local_or_private(value: str | None) -> bool:
    ip = _parse_ip(value)
    return bool(ip and (ip.is_loopback or ip.is_private or ip.is_link_local))


def client_ip(request: Request) -> str:
    """The address this request is attributed to.

    TRUSTED_PROXY=none (default): the TCP peer; forwarding headers are ignored.
    cloudflare: CF-Connecting-IP, only when the peer is loopback/private.
    nginx: the right-most X-Forwarded-For hop that is not loopback/private,
    only when the peer is loopback/private. A client can prepend anything to
    X-Forwarded-For, but cannot influence the right-most hops added by our own
    proxy, so walking from the right is spoof-resistant.
    """
    peer = request.client.host if request.client else "unknown"
    mode = (config.TRUSTED_PROXY or "none").lower()
    if mode == "none" or not _is_local_or_private(peer):
        return peer
    h = request.headers
    if mode == "cloudflare":
        ip = _parse_ip(h.get("cf-connecting-ip"))
        return str(ip) if ip else peer
    if mode == "nginx":
        hops = [x.strip() for x in (h.get("x-forwarded-for") or "").split(",") if x.strip()]
        for hop in reversed(hops):
            ip = _parse_ip(hop)
            if ip is None:
                return peer  # malformed chain: don't guess
            if not (ip.is_loopback or ip.is_private or ip.is_link_local):
                return str(ip)
        return peer
    return peer


# ---------------- sliding-window rate limiting ----------------
_lock = threading.Lock()
_buckets: dict[str, dict[str, deque]] = {}
_MAX_KEYS = 2048


def reset_rate_limits() -> None:
    """Forget all rate-limit state (tests call this between cases)."""
    with _lock:
        _buckets.clear()


def hit(bucket: str, key: str, limit: int, window: float) -> bool:
    """Record a hit; return False (and do not record) when over the limit."""
    t = now()
    with _lock:
        table = _buckets.setdefault(bucket, {})
        q = table.setdefault(key, deque())
        while q and t - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        if len(table) > _MAX_KEYS:
            for k in [k for k, v in table.items() if not v or t - v[-1] > window]:
                table.pop(k, None)
        return True


def rate_limit(request: Request, bucket: str, limit: int, window: float,
               *, global_limit: int | None = None,
               message: str = "too many requests from this address, try again in a few minutes") -> None:
    """Raise 429 when this client (and, optionally, everyone together) is over
    the limit. The global cap stops address rotation from spamming."""
    ip = client_ip(request)
    if global_limit is not None:
        # Check the cheaper-to-exhaust per-IP bucket first so one noisy client
        # does not burn the global allowance with rejected requests.
        if not hit(bucket, ip, limit, window):
            raise HTTPException(429, message)
        if not hit(bucket + ":all", "*", global_limit, window):
            raise HTTPException(429, "the board is busy right now, try again in a few minutes")
        return
    if not hit(bucket, ip, limit, window):
        raise HTTPException(429, message)
