"""
🧀 CheeseDog — Polymarket Fee Model
=====================================

Implements Polymarket's exact fee structure for 15-minute crypto markets.

Reference: NautilusTrader Polymarket integration documentation
  - Most Polymarket markets are fee-free
  - 15-minute crypto markets are the exception
  - Buy fee: 0.2% – 1.6% (deducted from Token)
  - Sell fee: 0.8% – 3.7% (deducted from USDC)
  - Fees rounded to 4 decimal places (minimum 0.0001 USDC)

Fee Rate Model:
  Uses a quadratic mapping based on contract price deviation from 0.50.
  - Price near 0.50 → lowest fee rate (most liquid price point)
  - Price near 0.00 or 1.00 → highest fee rate (least liquid)
"""

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("cheesedog.strategy.fees")


# ═══════════════════════════════════════════════════════════════
# Fee Configuration (Polymarket 15m Crypto Markets)
# ═══════════════════════════════════════════════════════════════
FEE_BUY_RANGE = (0.002, 0.016)    # Buy: 0.2% – 1.6%
FEE_SELL_RANGE = (0.008, 0.037)   # Sell: 0.8% – 3.7%
FEE_BUY_DEFAULT = 0.016           # Conservative: use max rate
FEE_SELL_DEFAULT = 0.037           # Conservative: use max rate


@dataclass
class FeeResult:
    """Fee calculation result"""
    gross_amount: float       # Original amount
    fee_amount: float         # Fee charged
    net_amount: float         # Amount after fee deduction
    fee_rate: float           # Effective fee rate
    fee_deducted_in: str      # "token" or "usdc"
    side: str                 # "buy" or "sell"


class PolymarketFeeModel:
    """
    Polymarket 15-minute market fee model.

    Implements the exact quadratic fee formula used by Polymarket
    for their crypto binary options markets.

    Key insight: Fee rate is NOT flat — it varies with contract price.
    Contracts trading near 0.50 have the lowest fees (most liquid),
    while extreme prices (near 0 or 1) incur the highest fees.
    """

    def __init__(self):
        self.buy_range = FEE_BUY_RANGE
        self.sell_range = FEE_SELL_RANGE
        self.buy_default = FEE_BUY_DEFAULT
        self.sell_default = FEE_SELL_DEFAULT
        self.min_fee = 0.0001  # Minimum fee: 0.0001 USDC

    def calculate_buy_fee(
        self,
        amount: float,
        contract_price: float = 0.5,
    ) -> FeeResult:
        """
        Calculate Buy-side fee.

        Buy fees are deducted from Token amount:
        - Price ≈ 0.50 → ~0.5% fee
        - Price ≈ 0.90 → ~0.2% fee
        - Price ≈ 0.10 → ~1.6% fee

        Args:
            amount: Purchase amount (USDC)
            contract_price: Current contract price (0~1)
        """
        fee_rate = self._estimate_fee_rate(
            contract_price,
            self.buy_range[0],
            self.buy_range[1],
            self.buy_default,
        )
        fee_amount = max(round(amount * fee_rate, 4), self.min_fee)
        net_amount = amount - fee_amount

        return FeeResult(
            gross_amount=amount,
            fee_amount=fee_amount,
            net_amount=net_amount,
            fee_rate=fee_rate,
            fee_deducted_in="token",
            side="buy",
        )

    def calculate_sell_fee(
        self,
        amount: float,
        contract_price: float = 0.5,
    ) -> FeeResult:
        """
        Calculate Sell-side fee.

        Sell fees are deducted from USDC proceeds (typically higher than buy):
        - Price ≈ 0.50 → ~1.5% fee
        - Price ≈ 0.90 → ~0.8% fee
        - Price ≈ 0.10 → ~3.7% fee

        Args:
            amount: Sell amount (USDC equivalent)
            contract_price: Current contract price (0~1)
        """
        fee_rate = self._estimate_fee_rate(
            contract_price,
            self.sell_range[0],
            self.sell_range[1],
            self.sell_default,
        )
        fee_amount = max(round(amount * fee_rate, 4), self.min_fee)
        net_amount = amount - fee_amount

        return FeeResult(
            gross_amount=amount,
            fee_amount=fee_amount,
            net_amount=net_amount,
            fee_rate=fee_rate,
            fee_deducted_in="usdc",
            side="sell",
        )

    def estimate_round_trip_cost(
        self,
        amount: float,
        buy_price: float = 0.5,
        sell_price: float = 0.5,
    ) -> dict:
        """
        Estimate total round-trip fee cost (Buy → Sell).

        This is critical for the profit filter — a trade must generate
        enough profit to cover BOTH buy AND sell fees to be worthwhile.

        Returns:
            Dict with total cost breakdown and break-even percentage.
        """
        buy_fee = self.calculate_buy_fee(amount, buy_price)
        sell_fee = self.calculate_sell_fee(amount, sell_price)

        total_fee = buy_fee.fee_amount + sell_fee.fee_amount
        total_rate = total_fee / amount if amount > 0 else 0

        return {
            "amount": amount,
            "buy_fee": buy_fee.fee_amount,
            "buy_rate": buy_fee.fee_rate,
            "sell_fee": sell_fee.fee_amount,
            "sell_rate": sell_fee.fee_rate,
            "total_fee": round(total_fee, 4),
            "total_rate": round(total_rate, 4),
            "break_even_pct": round(total_rate * 100, 2),
        }

    @staticmethod
    def _estimate_fee_rate(
        price: float,
        min_rate: float,
        max_rate: float,
        default_rate: float,
    ) -> float:
        """
        Estimate fee rate based on contract price.

        Uses a quadratic mapping:
        - deviation = |price - 0.50| * 2  (normalized to 0~1)
        - factor = deviation ^ 1.5  (smooth curve)
        - fee = min_rate + factor * (max_rate - min_rate)

        This produces a U-shaped fee curve centered at price=0.50.
        """
        price = max(0.01, min(0.99, price))
        deviation = abs(price - 0.5) * 2  # Normalize to 0~1
        factor = deviation ** 1.5          # Quadratic-ish curve
        fee_rate = min_rate + factor * (max_rate - min_rate)
        return round(fee_rate, 6)


# ═══════════════════════════════════════════════════════════════
# Usage Example
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    model = PolymarketFeeModel()

    # Example: $100 USDC trade at contract price 0.45
    buy = model.calculate_buy_fee(100, contract_price=0.45)
    print(f"Buy $100 @ cp=0.45 → Fee: ${buy.fee_amount:.4f} ({buy.fee_rate*100:.2f}%)")

    sell = model.calculate_sell_fee(100, contract_price=0.45)
    print(f"Sell $100 @ cp=0.45 → Fee: ${sell.fee_amount:.4f} ({sell.fee_rate*100:.2f}%)")

    # Round-trip cost analysis
    rt = model.estimate_round_trip_cost(100, buy_price=0.45, sell_price=1.0)
    print(f"\nRound-trip cost: ${rt['total_fee']:.4f} ({rt['break_even_pct']:.2f}%)")
    print(f"  Buy fee:  ${rt['buy_fee']:.4f} ({rt['buy_rate']*100:.2f}%)")
    print(f"  Sell fee: ${rt['sell_fee']:.4f} ({rt['sell_rate']*100:.2f}%)")
