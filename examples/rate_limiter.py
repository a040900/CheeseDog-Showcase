"""
🧀 CheeseDog — API Rate Limiter & Deadband Filter
===================================================

Two-layer rate control architecture for Polymarket CLOB API:

Layer 1: Deadband Filter
    Before sending a cancel-and-replace, check if the market
    has moved enough to justify a re-quote. If price deviation
    is below the threshold, skip entirely (zero API calls).

Layer 2: Token Bucket Rate Limiter
    Enforces a hard cap on API calls per unit of time.
    All CLOB API calls (post_order, cancel, cancel_all) must
    acquire a token before executing. If tokens are exhausted,
    requests queue up instead of triggering HTTP 429.

Combined Effect:
    - Deadband reduces 60-80% of unnecessary re-quotes in ranging markets
    - Token Bucket prevents accidental API quota exhaustion
    - Together they keep the system well within Verified tier limits
"""

import time
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("cheesedog.rate_limiter")


class DeadbandFilter:
    """
    Deadband (Hysteresis) Filter for Market Making.

    Prevents unnecessary cancel-and-replace cycles when the market
    hasn't moved enough to justify updating quotes.

    In ranging markets, this alone reduces API calls by 60-80%.
    """

    def __init__(self, threshold_pct: float = 0.02):
        """
        Args:
            threshold_pct: Minimum price deviation to trigger re-quote.
                           Default 2% — if mid_price hasn't moved more
                           than 2% since last quote, skip the re-quote.
        """
        self._threshold = threshold_pct
        self._last_quoted_price: Optional[float] = None
        self._skip_count = 0
        self._pass_count = 0

    def should_requote(self, current_mid_price: float) -> bool:
        """
        Check if current price deviation justifies a re-quote.

        Returns:
            True if re-quote needed, False if market hasn't moved enough.
        """
        if self._last_quoted_price is None:
            # First quote — always execute
            self._last_quoted_price = current_mid_price
            self._pass_count += 1
            return True

        deviation = abs(current_mid_price - self._last_quoted_price)
        deviation_pct = deviation / self._last_quoted_price if self._last_quoted_price > 0 else 0

        if deviation_pct < self._threshold:
            self._skip_count += 1
            logger.debug(
                f"⏸️ Deadband skip | Δ={deviation_pct*100:.3f}% "
                f"< {self._threshold*100:.1f}% threshold"
            )
            return False

        # Price has moved enough — update and allow re-quote
        self._last_quoted_price = current_mid_price
        self._pass_count += 1
        return True

    def on_quote_sent(self, quoted_price: float):
        """Update the last quoted price after successfully sending quotes."""
        self._last_quoted_price = quoted_price

    def get_stats(self) -> dict:
        total = self._skip_count + self._pass_count
        return {
            "skipped": self._skip_count,
            "passed": self._pass_count,
            "total": total,
            "skip_rate": round(
                self._skip_count / total * 100, 1
            ) if total > 0 else 0,
        }


class TokenBucketRateLimiter:
    """
    Token Bucket algorithm for API rate limiting.

    Ensures we never exceed the Polymarket API rate limits,
    even under high-frequency market-making conditions.

    Parameters:
        rate: Tokens added per second (sustained rate)
        burst: Maximum tokens that can accumulate (peak burst)

    Example for Verified Tier (3,000 tx/day):
        rate = 3000 / 86400 ≈ 0.035 tx/sec ≈ 2.08 tx/min
        burst = 10 (allow short bursts for cancel+replace pairs)
    """

    def __init__(self, rate: float, burst: int = 10):
        """
        Args:
            rate: Tokens replenished per second
            burst: Maximum token capacity
        """
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)  # Start with full bucket
        self._last_refill = time.monotonic()
        self._total_acquired = 0
        self._total_waited = 0

    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._burst,
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

    def try_acquire(self) -> bool:
        """
        Try to acquire a token without waiting.

        Returns:
            True if token acquired, False if rate limit exceeded.
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            self._total_acquired += 1
            return True
        return False

    async def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire a token, waiting if necessary.

        Args:
            timeout: Maximum seconds to wait for a token.

        Returns:
            True if token acquired, False if timed out.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self.try_acquire():
                return True

            # Calculate wait time until next token
            self._refill()
            tokens_needed = 1.0 - self._tokens
            wait_time = tokens_needed / self._rate if self._rate > 0 else 1.0
            wait_time = min(wait_time, deadline - time.monotonic(), 1.0)

            if wait_time > 0:
                self._total_waited += 1
                logger.debug(
                    f"⏳ Rate limit — waiting {wait_time:.2f}s for token"
                )
                await asyncio.sleep(wait_time)

        logger.warning("⚠️ Rate limit timeout — token not acquired")
        return False

    def get_stats(self) -> dict:
        self._refill()
        return {
            "available_tokens": round(self._tokens, 2),
            "rate_per_sec": self._rate,
            "rate_per_min": round(self._rate * 60, 1),
            "burst_capacity": self._burst,
            "total_acquired": self._total_acquired,
            "total_waited": self._total_waited,
        }


# ═══════════════════════════════════════════════════════════════
# Combined Rate Guard (Deadband + Token Bucket)
# ═══════════════════════════════════════════════════════════════

class RateGuard:
    """
    Combined rate control pipeline for market-making API calls.

    Pipeline:
        Market Move? → Deadband Filter → Token Available? → Token Bucket → API

    Usage:
        guard = RateGuard(
            deadband_threshold=0.02,  # 2% price change to trigger
            daily_limit=3000,         # Verified tier limit
            burst=10,                 # Allow short bursts
        )

        if guard.should_send(current_mid_price=0.45):
            await guard.acquire()
            # ... send API call ...
            guard.on_sent(quoted_price=0.45)
    """

    def __init__(
        self,
        deadband_threshold: float = 0.02,
        daily_limit: int = 3000,
        burst: int = 10,
    ):
        self.deadband = DeadbandFilter(threshold_pct=deadband_threshold)
        # Convert daily limit to per-second rate
        rate_per_sec = daily_limit / 86400
        self.limiter = TokenBucketRateLimiter(
            rate=rate_per_sec, burst=burst
        )

    def should_send(self, current_mid_price: float) -> bool:
        """Layer 1: Check if market moved enough to justify API call."""
        return self.deadband.should_requote(current_mid_price)

    async def acquire(self, timeout: float = 30.0) -> bool:
        """Layer 2: Acquire rate limit token."""
        return await self.limiter.acquire(timeout)

    def on_sent(self, quoted_price: float):
        """Update state after successfully sending quotes."""
        self.deadband.on_quote_sent(quoted_price)

    def get_stats(self) -> dict:
        return {
            "deadband": self.deadband.get_stats(),
            "rate_limiter": self.limiter.get_stats(),
        }


# ═══════════════════════════════════════════════════════════════
# Usage Example
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    guard = RateGuard(
        deadband_threshold=0.02,  # 2% threshold
        daily_limit=3000,         # Verified tier
        burst=10,
    )

    # Simulate market-making loop
    prices = [0.45, 0.451, 0.452, 0.449, 0.448, 0.42, 0.38, 0.39, 0.45]

    for price in prices:
        if guard.should_send(price):
            print(f"  📤 Re-quoting at mid_price={price:.3f}")
        else:
            print(f"  ⏸️ Skipped (Δ too small) at mid_price={price:.3f}")

    print(f"\nStats: {guard.get_stats()}")
