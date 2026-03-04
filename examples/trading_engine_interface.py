"""
🧀 CheeseDog — Unified Trading Engine Interface
=================================================

Defines the TradingEngine abstract base class, enabling seamless
swapping between SimulationEngine and LiveTradingEngine.

Core Design (inspired by NautilusTrader):
    - Strategy logic is engine-agnostic
    - Switching Simulation ↔ Live requires only swapping the engine instance
    - All engines share the unified Trade data structure

Usage:
    engine: TradingEngine = SimulationEngine()   # Paper trading
    engine: TradingEngine = LiveTradingEngine()  # Real money
    engine.start()
    trade = engine.execute_trade(signal, pm_state=pm)
    engine.auto_settle_expired(btc_start, btc_end)
"""

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Any

logger = logging.getLogger("cheesedog.trading.engine")


# ═══════════════════════════════════════════════════════════════
# Shared Data Structures
# ═══════════════════════════════════════════════════════════════

class TradeStatus(str, Enum):
    """Trade lifecycle status"""
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class EngineType(str, Enum):
    """Engine type identifier"""
    SIMULATION = "simulation"
    LIVE = "live"


@dataclass
class Trade:
    """
    Unified trade data structure.

    All trades (simulated or live) are represented by this structure,
    ensuring consistent data flow across the entire system.
    """
    trade_id: int
    direction: str              # "BUY_UP" | "SELL_DOWN"
    entry_price: float          # Contract price on Polymarket (0~1)
    quantity: float             # USDC notional amount
    signal_score: float         # Signal strength at entry
    trading_mode: str           # Active trading mode at entry
    market_title: str = "BTC 15m UP/DOWN"
    contract_price: float = 0.5
    entry_time: float = 0.0     # Unix timestamp
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    pnl: float = 0.0
    fee: float = 0.0
    status: TradeStatus = TradeStatus.OPEN

    # Live-only fields
    order_id: Optional[str] = None      # Polymarket CLOB order ID
    tx_hash: Optional[str] = None       # On-chain transaction hash
    token_amount: Optional[float] = None

    def __post_init__(self):
        if self.entry_time == 0.0:
            self.entry_time = time.time()

    @property
    def is_open(self) -> bool:
        return self.status == TradeStatus.OPEN

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since trade was opened"""
        return time.time() - self.entry_time

    @property
    def elapsed_minutes(self) -> float:
        return self.elapsed_seconds / 60

    def to_dict(self) -> dict:
        """Serialize to dict for API / WebSocket transmission"""
        return {
            "trade_id": self.trade_id,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 4),
            "quantity": round(self.quantity, 2),
            "pnl": round(self.pnl, 2),
            "fee": round(self.fee, 4),
            "status": self.status.value,
            "signal_score": round(self.signal_score, 2),
            "trading_mode": self.trading_mode,
            "market_title": self.market_title,
            "contract_price": round(self.contract_price, 4),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "exit_price": round(self.exit_price, 4) if self.exit_price else None,
            "elapsed_min": round(self.elapsed_minutes, 1),
            "order_id": self.order_id,
        }


# ═══════════════════════════════════════════════════════════════
# Abstract Base Class: TradingEngine
# ═══════════════════════════════════════════════════════════════

class TradingEngine(ABC):
    """
    Trading engine abstract base class.

    All engines (Simulation / Live) must implement this interface.
    Strategy logic (main.py, signal_generator) depends ONLY on this
    interface — never on concrete engine implementations.

    This enables:
    - Risk-free paper trading with identical logic
    - One-line switch to live trading
    - Backtesting with historical data replay
    """

    @property
    @abstractmethod
    def engine_type(self) -> EngineType:
        """Engine type identifier (simulation / live)"""
        ...

    # ── Lifecycle ─────────────────────────────────────────────

    @abstractmethod
    def start(self) -> None:
        """Start the engine"""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the engine"""
        ...

    @abstractmethod
    def is_running(self) -> bool:
        """Check if engine is actively running"""
        ...

    @abstractmethod
    def reset(self, new_balance: Optional[float] = None) -> None:
        """Reset engine state (clear positions, restore balance)"""
        ...

    # ── Trade Execution ───────────────────────────────────────

    @abstractmethod
    def execute_trade(
        self,
        signal: dict,
        amount: Optional[float] = None,
        pm_state: Optional[Any] = None,
    ) -> Optional[Trade]:
        """
        Execute a trade.

        Args:
            signal: Trading signal (direction, score, confidence, mode)
            amount: Trade amount in USDC (None = auto-calculate via RiskManager)
            pm_state: Current Polymarket market state

        Returns:
            Trade object (success) or None (filtered/failed)
        """
        ...

    @abstractmethod
    def auto_settle_expired(
        self, btc_price_start: float, btc_price_end: float
    ) -> None:
        """
        Auto-settle expired trades.

        For Polymarket 15m markets, this syncs internal state with
        on-chain settlement results.

        Args:
            btc_price_start: BTC price at period start
            btc_price_end: BTC price at period end
        """
        ...

    # ── Queries ────────────────────────────────────────────────

    @abstractmethod
    def get_balance(self) -> float:
        """Get current balance (USDC)"""
        ...

    @abstractmethod
    def get_open_trades(self) -> List[Trade]:
        """Get all open (unsettled) trades"""
        ...

    @abstractmethod
    def get_stats(self) -> dict:
        """Get trading statistics summary"""
        ...

    @abstractmethod
    def get_recent_trades(self, limit: int = 10) -> List[dict]:
        """Get recent trades (including open positions)"""
        ...

    @abstractmethod
    def get_pnl_curve(self) -> List[dict]:
        """Get PnL curve data points for charting"""
        ...

    # ── Emergency Control ─────────────────────────────────────

    def emergency_stop(self, reason: str = "Manual trigger") -> dict:
        """
        Emergency stop: halt engine + log reason.

        Subclasses may override to add extra behavior
        (e.g., cancel all pending orders on Polymarket).
        """
        self.stop()
        logger.warning(
            f"🚨 Emergency stop! Reason: {reason} | "
            f"Engine: {self.engine_type.value}"
        )
        return {
            "action": "emergency_stop",
            "engine": self.engine_type.value,
            "reason": reason,
            "timestamp": time.time(),
        }
