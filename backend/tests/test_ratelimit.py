"""Rate limiting tests (docs/code-plan.md sections 5.4 and 9).

Unit level drives ``SlidingWindowLimiter`` directly, including with a frozen and
manually advanced clock so that window sliding and the rejected-acquires-also-count
semantics are asserted deterministically, without sleeping.

HTTP level hammers ``POST /auth/login`` with wrong credentials until 429. The login
route acquires ``login_ip`` before ``login_user`` and both share the same
``login_max_attempts`` budget, so these tests assert only the 429 behaviour and never
which of the two keys tripped first. Usernames are unique per test and each test gets
a fresh app, so limiter state cannot leak between tests.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import API, SettingsFactory

from ownpanel.config import Settings
from ownpanel.errors import RateLimitedError
from ownpanel.security.ratelimit import SlidingWindowLimiter

LOGIN_MAX_ATTEMPTS = 3  # small budget keeps the HTTP burst tests fast
WRONG_PASSWORD = "definitely-wrong-pw-1!"


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    """Module override of the shared settings fixture: a small login budget."""
    return make_settings(limits={"login_max_attempts": LOGIN_MAX_ATTEMPTS})


class Clock:
    """Frozen monotonic time source advanced manually by tests."""

    def __init__(self) -> None:
        self.now = 10_000.0  # arbitrary monotonic origin (seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Replace ``monotonic`` as seen by the ratelimit module with a frozen clock."""
    frozen = Clock()
    monkeypatch.setattr("ownpanel.security.ratelimit.monotonic", lambda: frozen.now)
    return frozen


class TestSlidingWindowLimiterUnit:
    def test_burst_within_budget_passes_then_rejects(self) -> None:
        limiter = SlidingWindowLimiter(3, 60)
        key = ("login_user", "unit-basic")
        for _ in range(3):
            limiter.acquire(key)  # within budget: no exception
        with pytest.raises(RateLimitedError) as exc_info:
            limiter.acquire(key)
        err = exc_info.value
        assert err.status == 429
        assert err.code == "auth.rate_limited"
        assert isinstance(err.retry_after_s, int)
        assert 1 <= err.retry_after_s <= 60
        assert err.extra["retry_after"] == err.retry_after_s
        # Hammering a limited key keeps failing: rejected acquires also count.
        for _ in range(3):
            with pytest.raises(RateLimitedError):
                limiter.acquire(key)

    def test_reset_restores_capacity(self) -> None:
        limiter = SlidingWindowLimiter(3, 60)
        key = ("login_user", "unit-reset")
        for _ in range(3):
            limiter.acquire(key)
        with pytest.raises(RateLimitedError):
            limiter.acquire(key)
        limiter.reset(key)
        for _ in range(3):
            limiter.acquire(key)  # full budget again after reset
        with pytest.raises(RateLimitedError):
            limiter.acquire(key)
        limiter.reset(("login_user", "never-seen-key"))  # unknown key: silent no-op

    def test_keys_are_independent(self) -> None:
        limiter = SlidingWindowLimiter(2, 60)
        for _ in range(2):
            limiter.acquire(("login_user", "user-a"))
        with pytest.raises(RateLimitedError):
            limiter.acquire(("login_user", "user-a"))
        limiter.acquire(("login_user", "user-b"))  # other username unaffected
        limiter.acquire(("login_ip", "user-a"))  # same value under another scope too

    def test_instant_burst_retry_after_is_full_window(self, clock: Clock) -> None:
        limiter = SlidingWindowLimiter(3, 60)
        key = ("login_ip", "10.9.8.7")
        for _ in range(3):
            limiter.acquire(key)
        with pytest.raises(RateLimitedError) as exc_info:
            limiter.acquire(key)
        assert exc_info.value.retry_after_s == 60
        clock.advance(60)
        limiter.acquire(key)  # the window slid past all recorded events

    def test_rejected_acquires_extend_the_block(self, clock: Clock) -> None:
        limiter = SlidingWindowLimiter(2, 60)
        key = ("login_user", "frozen-clock-user")
        limiter.acquire(key)  # t+0
        limiter.acquire(key)  # t+0

        clock.advance(30)  # t+30
        with pytest.raises(RateLimitedError) as exc_info:
            limiter.acquire(key)  # rejected, but recorded as an event
        assert exc_info.value.retry_after_s == 30

        clock.advance(15)  # t+45
        with pytest.raises(RateLimitedError) as exc_info:
            limiter.acquire(key)
        # Blocked by the rejected event from t+30, not by the t+0 successes.
        assert exc_info.value.retry_after_s == 45

        clock.advance(16)  # t+61: both t+0 events have left the window by now
        with pytest.raises(RateLimitedError) as exc_info:
            limiter.acquire(key)  # still rejected: the rejected acquires kept counting
        assert exc_info.value.retry_after_s == 44

        clock.advance(61)  # t+122: every recorded event has expired
        limiter.acquire(key)

    def test_window_is_driven_by_monotonic_clock_only(self, clock: Clock) -> None:
        """Regression: the window slides only with monotonic time, so a wall-clock
        jump (NTP step, operator change, VM restore) cannot refill the budget."""
        limiter = SlidingWindowLimiter(3, 60)
        key = ("login_ip", "monotonic-only")
        for _ in range(3):
            limiter.acquire(key)
        with pytest.raises(RateLimitedError):
            limiter.acquire(key)
        # No monotonic time has passed: still blocked even though wall-clock may have
        # leapt forward. Only advancing the (mocked) monotonic clock past the window frees it.
        clock.advance(59)
        with pytest.raises(RateLimitedError):
            limiter.acquire(key)
        clock.advance(2)  # now strictly past the 60s window since the oldest event
        limiter.acquire(key)


async def _login_attempt(client: httpx.AsyncClient, username: str) -> httpx.Response:
    return await client.post(
        f"{API}/auth/login",
        json={"username": username, "password": WRONG_PASSWORD},
    )


class TestLoginRateLimitHttp:
    async def test_login_burst_returns_429_with_retry_after(
        self, client: httpx.AsyncClient
    ) -> None:
        username = "rl-burst-user"
        for attempt in range(LOGIN_MAX_ATTEMPTS):
            resp = await _login_attempt(client, username)
            assert resp.status_code == 401, f"attempt {attempt + 1}: {resp.text}"
            assert resp.json()["code"] == "auth.invalid_credentials"

        resp = await _login_attempt(client, username)
        assert resp.status_code == 429
        assert resp.headers["content-type"].startswith("application/problem+json")

        retry_header = resp.headers.get("Retry-After")
        assert retry_header is not None
        assert retry_header.isascii() and retry_header.isdigit()
        assert 1 <= int(retry_header) <= 60

        body = resp.json()
        assert body["code"] == "auth.rate_limited"
        assert body["title"] == "Too Many Attempts"
        assert body["status"] == 429
        assert body["type"] == "about:blank"
        assert isinstance(body["retry_after"], int)
        assert body["retry_after"] == int(retry_header)

    async def test_username_rotation_does_not_evade_the_limit(
        self, client: httpx.AsyncClient
    ) -> None:
        for _ in range(LOGIN_MAX_ATTEMPTS):
            resp = await _login_attempt(client, "rl-rotate-first")
            assert resp.status_code == 401
        resp = await _login_attempt(client, "rl-rotate-first")
        assert resp.status_code == 429

        # A fresh username from the same client IP must still be rejected; whether
        # the ip or the user key trips first is deliberately not asserted.
        resp = await _login_attempt(client, "rl-rotate-second")
        assert resp.status_code == 429
        body = resp.json()
        assert body["code"] == "auth.rate_limited"
        assert body["retry_after"] >= 1
        assert int(resp.headers["Retry-After"]) >= 1
