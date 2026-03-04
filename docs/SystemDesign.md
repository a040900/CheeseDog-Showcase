# CheeseDog — System Architecture Deep Dive

## Design Philosophy

CheeseDog follows four core architectural principles:

### 1. Event-Driven Architecture (EDA)

All data feeds (Binance, Polymarket, Chainlink) publish events to a central
`MessageBus` rather than directly calling downstream modules. This provides:

- **Temporal decoupling**: Producers don't wait for consumers
- **Spatial decoupling**: Components don't need to know about each other
- **Millisecond-level signal reactivity** (vs. traditional 10s polling loops)
- **50,000-event queue capacity** — stress-tested against backtest Parquet replay at full speed

```
Feed → publish("binance.trade", data) → MessageBus → subscribe handlers
```

### 2. Dependency Inversion Principle (DIP)

The `TradingEngine` abstract base class defines the contract that ALL engines
must implement. Strategy code (`main.py`, `signal_generator`) depends ONLY on
this interface — never on `SimulationEngine` or `LiveTradingEngine` directly.

```python
# One-line switch between simulation and live:
engine: TradingEngine = SimulationEngine()   # Paper trading
engine: TradingEngine = LiveTradingEngine()  # Real money ← same interface
```

This enables:
- Risk-free paper trading with identical logic
- Backtesting with historical data replay using the same strategy code
- Future multi-exchange support (Predict.fun, etc.) via adapter pattern

### 3. Component State Machines

Every system component follows a strict state machine:

```
INITIALIZING → READY → RUNNING → STOPPED
                  ↓         ↓
              DEGRADED   FAULTED
```

- **DEGRADED**: Component is functional but experiencing issues (e.g., high latency, RPC failures)
- **FAULTED**: Component has failed and needs manual intervention
- Dashboard displays component health (not just connected/disconnected)

### 4. Fail-Fast Configuration

`ConfigValidator` runs at the earliest stage of `lifespan()`, before any data feed or trading logic starts:

- **Mode-aware validation**: Different required vars for Simulation vs. Live vs. Telegram-enabled
- **Numeric sanity checks**: Warns if TTL is too short, profit filter too loose, etc.
- **Actionable errors**: Each missing var includes a specific fix suggestion, not just a variable name
- This eliminates entire classes of production `KeyError` / silent misconfiguration bugs

---

## Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        CheeseDog System                         │
│                                                                 │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                        │
│  │ Binance  │  │Polymarket│  │Chainlink │   DATA LAYER           │
│  │   WS     │  │  CLOB   │  │ Oracle   │                        │
│  └────┬─────┘  └────┬────┘  └────┬─────┘                        │
│       │              │            │                              │
│       └──────────────┼────────────┘                              │
│                      ▼                                           │
│           ┌──────────────────┐                                   │
│           │    MessageBus    │         EVENT LAYER                │
│           │   (Pub/Sub)      │         50K queue                 │
│           └────────┬─────────┘                                   │
│                    │                                             │
│       ┌────────────┼────────────────┐                            │
│       ▼            ▼                ▼                            │
│  ┌─────────┐ ┌──────────┐  ┌────────────┐                      │
│  │ Signal   │ │ Market   │  │ Sentiment  │   INTELLIGENCE LAYER  │
│  │Generator │ │ Regime   │  │  Factor    │                      │
│  │ (12+     │ │ (ADX/ATR)│  │ (PM ↔ TA) │                      │
│  │indicators)│ └──────────┘  └────────────┘                      │
│  └────┬─────┘                                                    │
│       ▼                                                          │
│  ┌──────────────┐  ┌──────────────┐                              │
│  │ Smart Order   │  │ Risk Manager │   EXECUTION LAYER           │
│  │ Router + EV   │  │ (4 breakers, │                             │
│  │ Filter        │  │  Kelly, DV2) │                             │
│  └──────┬───────┘  └──────────────┘                              │
│         │                                                        │
│    ┌────┴─────┐                                                  │
│    ▼          ▼                                                  │
│ [Simulation] [Live]         TRADING ENGINE LAYER                 │
│  (Paper)   (py-clob-client)  (Swappable via DIP)                │
│                                                                  │
│  ┌──────────┐  ┌──────────┐                                     │
│  │ Telegram  │  │Dashboard │   CONTROL PLANE                     │
│  │ (HITL)    │  │ (Web UI) │                                     │
│  └──────────┘  └──────────┘                                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Smart Order Router

The system dynamically chooses between Taker and Maker execution:

| Condition | Decision | Rationale |
|-----------|----------|-----------| 
| ADX > 25 (strong trend) | **Taker (FOK)** | Chase momentum, fill immediately |
| ADX < 25 (ranging) | **Maker (GTC)** | Earn spread + rebate in quiet markets |
| EV > 0 after Taker fees | **Taker allowed** | Positive expected value confirmed |
| EV > 0 only with Maker fees (0%) | **Maker only** | Only profitable with zero maker fee |
| EV < 0 for both | **Reject trade** | No profitable execution path |

The EV filter uses Bayesian-calibrated win probability, not naive signal confidence.

---

## Risk Management Stack

```
Trade Signal
    │
    ▼
┌──────────────────────┐
│ Profit Filter         │  Is the expected gross profit > 2.5× round-trip fees?
│ (Break-even check)    │  → No: REJECT
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Circuit Breaker #1    │  Daily loss > 5% of balance?
│ (Daily loss limit)    │  → Yes: HALT until next day
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Circuit Breaker #2    │  3 consecutive losses?
│ (Streak breaker)      │  → Yes: 30-minute cooldown
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Circuit Breaker #3    │  Max drawdown > 10%?
│ (Drawdown limit)      │  → Yes: Emergency stop
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Circuit Breaker #4    │  > 20 trades today?
│ (Daily trade cap)     │  → Yes: Wait until tomorrow
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Kelly Criterion       │  Position size = f(win_prob, odds, balance)
│ (1/5 Kelly fraction)  │  Conservative sizing
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Capital Management    │  Compounding / House Money / Watermark
│ (Protected floor)     │  Never risk the protected capital base
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Double Verification   │  Virtual ledger balance == Polymarket API balance?
│ (Live mode only)      │  Guard against any deposit/withdrawal interference
└──────────┬───────────┘
           ▼
      EXECUTE TRADE
```

---

## Simulation Fidelity

> *"If your backtest shows orders filling at 0.02 contract price where $67,000 in orders are queued ahead of you — your fill model is broken, not your strategy."*

Three hardening improvements to prevent the simulation-to-live performance gap:

### Fill Model Hardening

| Defect | Root Cause | Fix |
|--------|-----------|-----|
| OTM contracts filling instantly | No price floor on quoting | `MM_QUOTE_FLOOR = 0.05` — hard floor |
| Ignoring queue position | Fill decided by price touch only | `ahead_depth > MM_FILL_DEPTH_RATIO × order_size` → no fill |
| Disconnected from real order book | Fill logic used synthetic depth | `check_pending_fills` reads live L2 CLOB snapshot |

### Wind Tunnel Backtester

- **Physical Settlement Mode**: Uses real Binance 1m K-line closes to determine 15m market outcomes — no oracle lookahead
- **Monkey Patching (Hacker Mode)**: Replays historical CLOB depth snapshots into the live signal engine with zero code changes
- **Grid Search**: Exhaustively validates `MM_OFFSET` parameter space to identify optimal Maker spread configuration (current optimum: Offset=0)

---

## HITL Supervision (Human-in-the-Loop)

The `supervisor` module operates as a permission gatekeeper for all AI-generated proposals:

```
AI Proposal
    │
    ▼
┌──────────────────┐
│  AuthorizationGate│  Current mode: AUTO / HITL / MONITOR
└────────┬─────────┘
    ┌────┴────┐
    ▼         ▼
  AUTO       HITL → Telegram Bot inline buttons
 execute      ↓         (/approve or /reject)
         ProposalQueue
         (state machine)
         TTL: configurable expiry → auto-reject if no response
```

- **8 Telegram commands**: `/status`, `/proposals`, `/mode`, `/setnavigator`, `/setauth`, etc.
- **Inline Buttons**: One-tap approve/reject from mobile
- **Emergency Safety Valve**: Forced prominent reminder in any dangerous proposal
- **Dynamic configuration**: `POST /api/telegram/configure` — no restart required

---

## Quant Desk Simulation Research (Phase 2.5)

> Inspired by institutional-grade quant simulation methodology — *"How to Simulate Like a Quant Desk"* ([@gemchange_ltd](https://x.com/gemchange_ltd/status/2027744530124951831))

We identified that naive simulation models (price-touch = instant fill, independent trade sequence) mask critical real-world market microstructure effects. Phase 2.5 is our research roadmap to close this gap.

### Agent-Based Simulation (Microstructure Modeling)

Current simulators assume an idealized, frictionless order book. Real Polymarket 15m markets exhibit:

- **Random clustered order arrival** — not uniform Poisson processes
- **Heterogeneous trader behavior** — Informed Traders (acting on signal) vs. Noise Traders (random)
- **Non-linear price impact** — large orders deplete nearby liquidity and shift the mid-price

Our planned approach introduces **Zero-Intelligence Agents** — budget-constrained random order senders — to simulate realistic order book dynamics and spread evolution, replacing the naive "price touches my quote = fill" assumption.

```
Informed Traders (AI signal)          Noise Traders (random)
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
             Simulated Order Book
         (realistic spread dynamics,
          price impact, queue depth)
                       │
                       ▼
          Fill Probability Model
       (replaces binary touch/no-touch)
```

### Brier Score — Prediction Calibration Metric

Win rate alone is a misleading metric for probability prediction systems:

- A signal that predicts 90% confidence but wins 55% of the time is **overconfident and dangerous**
- A signal that predicts 55% confidence and wins 55% of the time is **well-calibrated and trustworthy**

**Brier Score** = `mean((predicted_prob - actual_outcome)²)`

- Lower is better (0.0 = perfect, 0.25 = random)
- Penalizes confident wrong predictions quadratically
- Planned integration: `PerformanceTracker` reports Brier Score alongside win rate, and the parameter optimizer uses it as a secondary objective function

### Sequential Monte Carlo (Particle Filter)

A step beyond the current `BayesianUpdater` for tracking non-stationary probability dynamics:

- **State-Space Model**: Treat the "true" event probability as a hidden state that evolves over time
- **Particle Cloud**: Maintain N weighted hypotheses about the current true probability, constantly resampled against observed market prices
- **Advantage over Bayesian update**: Handles abrupt regime changes (e.g., breaking news mid-cycle) that would take many samples for a static Bayesian model to adjust to

This is planned as an A/B test module against the current `BayesianUpdater`, not a wholesale replacement.

### Geometric Brownian Motion + Jump Diffusion

For generating realistic synthetic price paths in simulation:

| Model | Use Case | Limitation |
|-------|---------|------------|
| Simple random walk | Baseline | No calibration to real vol |
| **GBM** | Realistic continuous price drift | No sudden jumps |
| **GBM + Jump Diffusion** | Models news-driven price spikes | More params to calibrate |

Prediction markets frequently exhibit jump behavior — a single news headline moves contracts from 0.40 to 0.80 in seconds. Jump Diffusion (Merton model) captures this where pure GBM cannot.

Planned as a PoC (`simulator_gbm.py`)  with A/B comparison against current deterministic replay.

---

## Polymarket Fee Model

Implements the exact quadratic fee formula for 15-minute crypto markets:

- Fee rate follows a U-shaped curve centered at contract price = 0.50
- Prices near 0 or 1 incur the highest fees (low liquidity premium)
- Buy fees deducted from Token; Sell fees deducted from USDC
- **Critical edge case**: Settlement redeems winning tokens at $1.00, so the sell fee must be calculated using `contract_price=1.0` — not the entry price. This correction changes the fee by up to 3× for contracts bought cheap.

See [`examples/fee_model.py`](../examples/fee_model.py) for the full implementation.

---

## Testing

| Module | Tests | Coverage Focus |
|--------|-------|---------------|
| `test_fees.py` | 27 | Quadratic fee curves, round-trip costs, settlement at $1.00 edge case |
| `test_risk_manager.py` | 20 | Circuit breakers, Kelly sizing, capital modes, BayesianUpdater bucket routing |
| `test_signal_generator.py` | 37 | Bias scores, regime detection, sentiment factor, strike price parsing |
| `test_simulator.py` | 38 | Trade execution, settlement, maker quotes, TTL, BayesianUpdater end-to-end |
| **Total** | **122** | **All passing in < 0.35s** |

---

## Roadmap

| Phase | Status | Focus |
|-------|--------|-------|
| Phase 1–4 | ✅ Complete | Data pipeline, simulation, live trading, HITL supervision |
| Phase 5 | 🟡 In Progress | Maker strategy: dynamic spread, inventory management, cancel-replace loop |
| Phase 6 | ⏳ Up Next | Frontend modularization (ES Modules, state management) |
| Phase 7 | ✅ Complete | Testing framework (122 unit tests), Config Validator (fail-fast) |
| Phase 8 | ✅ Complete | Simulation fidelity: Fill Model Hardening, Queue Position simulation |
| Future | 📋 Planned | Sub-100ms HFT loop, multi-exchange (Predict.fun) adapter |
