"""
🧀 CheeseDog — Lightweight Async MessageBus (Pub/Sub)
======================================================

Inspired by NautilusTrader's MessageBus architecture.
Implements a fire-and-forget event system for decoupling
data producers (feeds) from consumers (strategy, trading).

Event Topics:
    binance.trade       — Each Binance BTC trade tick
    binance.kline       — Kline bar update / close
    binance.orderbook   — Order book snapshot update
    polymarket.price    — Polymarket contract price update
    chainlink.price     — On-chain oracle price update
    signal.generated    — New trading signal produced
    trade.opened        — Trade opened (sim or live)
    trade.settled       — Trade settled (win/loss)

Design Decisions:
    - asyncio.Queue for backpressure (50K capacity, drop on overflow)
    - Supports both sync and async handlers transparently
    - Single-threaded dispatch loop guarantees event ordering
    - Global singleton pattern for system-wide event routing
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("cheesedog.core.bus")


# ═══════════════════════════════════════════════════════════════
# Event Data Structure
# ═══════════════════════════════════════════════════════════════

@dataclass
class Event:
    """Immutable event object"""
    topic: str          # Event topic, e.g. "binance.trade"
    data: Any           # Event payload
    timestamp: float = field(default_factory=time.time)
    source: str = ""    # Source component name


# Handler type: accepts Event, returns None (sync or async)
EventHandler = Callable[[Event], Any]


# ═══════════════════════════════════════════════════════════════
# MessageBus Implementation
# ═══════════════════════════════════════════════════════════════

class MessageBus:
    """
    Lightweight async event bus (Pub/Sub).

    Features:
    - Supports sync / async handlers transparently
    - Fire-and-forget publish (never blocks the publisher)
    - Built-in event queue with backpressure protection
    - Guarantees FIFO processing order
    - Throughput statistics for monitoring
    """

    def __init__(self, max_queue_size: int = 10000):
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._running = False
        self._worker: Optional[asyncio.Task] = None

        # Statistics
        self._published_count = 0
        self._processed_count = 0
        self._error_count = 0

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self):
        """Start the event dispatch loop"""
        if self._running:
            return
        self._running = True
        self._worker = asyncio.create_task(self._dispatch_loop())
        logger.info("🚌 MessageBus started")

    async def stop(self):
        """Gracefully stop the event dispatch loop"""
        self._running = False
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        logger.info(
            f"🛑 MessageBus stopped "
            f"(published: {self._published_count}, "
            f"processed: {self._processed_count}, "
            f"errors: {self._error_count})"
        )

    # ── Subscribe / Publish ───────────────────────────────────

    def subscribe(self, topic: str, handler: EventHandler):
        """Subscribe a handler to an event topic"""
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)
            handler_name = getattr(handler, "__name__", repr(handler))
            logger.debug(f"📬 Subscribed: {topic} → {handler_name}")

    def unsubscribe(self, topic: str, handler: EventHandler):
        """Unsubscribe a handler from an event topic"""
        try:
            self._subscribers[topic].remove(handler)
        except ValueError:
            pass

    def publish(self, topic: str, data: Any = None, source: str = ""):
        """
        Publish an event (non-blocking).

        If the bus is not running or the queue is full,
        the event will be silently dropped.
        """
        if not self._running:
            return

        event = Event(topic=topic, data=data, source=source)
        try:
            self._queue.put_nowait(event)
            self._published_count += 1
        except asyncio.QueueFull:
            logger.warning(f"⚠️ Event queue full! Dropping: {topic}")

    # ── Internal Dispatch Loop ────────────────────────────────

    async def _dispatch_loop(self):
        """Main event dispatch loop — FIFO ordering guaranteed"""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            handlers = self._subscribers.get(event.topic, [])
            if not handlers:
                self._queue.task_done()
                continue

            for handler in handlers:
                try:
                    result = handler(event)
                    # If handler returns a coroutine, await it
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    self._error_count += 1
                    handler_name = getattr(handler, "__name__", repr(handler))
                    logger.error(
                        f"❌ Handler error: {event.topic} → "
                        f"{handler_name}: {e}"
                    )

            self._processed_count += 1
            self._queue.task_done()

    # ── Statistics / Debug ────────────────────────────────────

    def get_stats(self) -> dict:
        """Get MessageBus throughput statistics"""
        return {
            "running": self._running,
            "published": self._published_count,
            "processed": self._processed_count,
            "errors": self._error_count,
            "queue_size": self._queue.qsize(),
            "subscriber_count": {
                topic: len(handlers)
                for topic, handlers in self._subscribers.items()
                if handlers
            },
        }


# ═══════════════════════════════════════════════════════════════
# Usage Example
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import asyncio

    async def demo():
        bus = MessageBus(max_queue_size=1000)
        await bus.start()

        # Subscribe handlers
        def on_price(event: Event):
            print(f"  Price update: {event.data}")

        async def on_signal(event: Event):
            print(f"  Signal received: {event.data}")
            # Simulate async processing
            await asyncio.sleep(0.01)

        bus.subscribe("binance.trade", on_price)
        bus.subscribe("signal.generated", on_signal)

        # Publish events
        bus.publish("binance.trade", {"price": 67250.50}, source="binance")
        bus.publish("signal.generated", {"direction": "BUY_UP", "score": 72})

        # Wait for processing
        await asyncio.sleep(0.5)
        print(f"\nStats: {bus.get_stats()}")

        await bus.stop()

    asyncio.run(demo())
