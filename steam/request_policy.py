"""Process-wide Steam Community rate-limit coordination.

A 429 from any Steam Community endpoint must stop market reads and writes from
other parts of the application as well.
"""
import math
import threading
import time
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

import requests

DEFAULT_RATE_LIMIT_SECONDS = 60.0
MAX_CONSECUTIVE_MARKET_429 = 3
DEFAULT_STEAM_REQUEST_INTERVAL_SECONDS = 0.5


class SteamMarketRateLimitExceeded(RuntimeError):
    """A buying task exhausted its bounded attempts to recover from HTTP 429."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(
            f"Steam 市场连续 {MAX_CONSECUTIVE_MARKET_429} 次 HTTP 429，自动重试已停止；"
            f"仍处于限流状态（冷却剩余约 {retry_after:.0f} 秒），请稍后手动重试"
        )


class SteamMarketCooldownActive(requests.RequestException):
    """A request was blocked locally before opening a Steam connection."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Steam 市场 HTTP 429 冷却中，剩余约 {retry_after:.0f} 秒；本次未发送请求")


def parse_retry_after(value: object, *, now: Optional[float] = None) -> float:
    """Accept delta seconds or an HTTP date; unknown limits get a short cooldown."""
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0.0, date.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError, OSError):
            return DEFAULT_RATE_LIMIT_SECONDS
    if not math.isfinite(delay) or delay < 0:
        return DEFAULT_RATE_LIMIT_SECONDS
    return max(1.0, delay)


class MarketCooldown:
    """Share an endpoint's 429 cooldown across tasks, without blocking a thread."""

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.monotonic
        self._until = 0.0
        self._failures = 0
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - self._clock())

    def defer(self, retry_after: object = None) -> float:
        delay = parse_retry_after(retry_after)
        with self._lock:
            self._failures += 1
            if retry_after is None or not str(retry_after).strip():
                delay = min(900.0, DEFAULT_RATE_LIMIT_SECONDS * (2 ** min(self._failures - 1, 4)))
            now = self._clock()
            self._until = max(self._until, now + delay)
            return self._until - now

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0


# One cooldown covers all Steam Community API paths, including write endpoints.
steam_market_cooldown = MarketCooldown()


def check_steam_market_cooldown() -> None:
    remaining = steam_market_cooldown.remaining()
    if remaining > 0:
        raise SteamMarketCooldownActive(remaining)


def record_steam_market_response(response, *, cooldown: Optional[MarketCooldown] = None) -> Optional[float]:
    """Record a 429 once, even if both the transport and caller inspect it."""
    target = cooldown or steam_market_cooldown
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        if bool(getattr(response, "steam_rate_limit_recorded", False)):
            return target.remaining()
        headers = getattr(response, "headers", {}) or {}
        remaining = target.defer(headers.get("Retry-After"))
        try:
            response.steam_rate_limit_recorded = True
        except Exception:
            pass
        return remaining
    if status_code == 200:
        target.record_success()
    return None


class SteamRequestGate:
    """Serialize Steam Community calls and keep a small gap between requests."""

    def __init__(
        self,
        min_interval: float = DEFAULT_STEAM_REQUEST_INTERVAL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._lock = threading.RLock()
        self._next_request_at = 0.0

    @contextmanager
    def slot(self, *, check_cooldown: bool = True):
        with self._lock:
            wait = self._next_request_at - self._clock()
            if wait > 0:
                self._sleep(wait)
            if check_cooldown:
                check_steam_market_cooldown()
            try:
                yield
            finally:
                self._next_request_at = self._clock() + self._min_interval


steam_request_gate = SteamRequestGate()
