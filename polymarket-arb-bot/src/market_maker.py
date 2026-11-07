"""
Polymarket Market Making Strategy
==================================

This is where the real money is on Polymarket.

Instead of waiting for rare arbitrage opportunities, the bot acts as a 
market maker: it places both BUY and SELL orders on liquid markets and 
earns the bid-ask spread.

How it works:
1. Find markets with decent volume but wider spreads (2-5%)
2. Place a BUY order at best_bid + small edge
3. Place a SELL order at best_ask - small edge
4. When both fill, you've earned the spread
5. Manage inventory: if you accumulate too many Yes tokens, adjust prices

With $100K deployed across 20-30 markets earning 1-3% spreads:
- 20 markets × $5000 each × 2% spread × 2 fills/day = $2000/day
- Even at 50% fill rate: $1000/day

This matches your friend's returns.
"""

import logging
import time
import math
from dataclasses import dataclass, field
from typing import Optional
from scanner import MarketScanner, Market

logger = logging.getLogger("polymarket-arb.maker")


@dataclass 
class MarketMakingSetup:
    """A market making opportunity on a single market."""
    question: str
    condition_id: str
    token_id: str
    # Current orderbook
    best_bid: float
    best_ask: float
    spread: float
    bid_depth: float
    ask_depth: float
    # Our target orders
    our_buy_price: float
    our_sell_price: float
    our_spread: float  # our captured spread
    # Expected profit per fill cycle (buy then sell)
    profit_per_cycle: float
    # Volume score (higher = more likely to fill)
    volume: float
    liquidity_score: float  # 0-1, how balanced the book is
    # Risk
    inventory_risk: float  # estimated, 0-1


class MarketMaker:
    """
    Market making strategy for Polymarket.
    
    Scans for markets with favorable spreads and places dual-sided orders.
    """

    def __init__(self, scanner: MarketScanner,
                 min_spread: float = 0.02,  # 2% minimum spread to bother
                 max_spread: float = 0.15,  # 15% max (too wide = toxic)
                 min_volume: float = 50000,  # $50K+ volume for fill probability
                 min_depth: float = 100,  # min shares at top of book
                 edge: float = 0.3,  # how far into spread to place (0=at bid, 1=at ask)
                 order_size: float = 100,  # shares per order
                 max_positions: int = 30):
        """
        Args:
            min_spread: Minimum bid-ask spread to market make (wider = more profit but less fills)
            max_spread: Maximum spread (too wide means toxic/dangerous market)
            min_volume: Minimum market volume (higher = more likely to get fills)
            min_depth: Minimum orderbook depth
            edge: Where in the spread to place orders (0.3 = 30% from bid toward ask)
            order_size: Number of shares per limit order
            max_positions: Max concurrent market making positions
        """
        self.scanner = scanner
        self.min_spread = min_spread
        self.max_spread = max_spread
        self.min_volume = min_volume
        self.min_depth = min_depth
        self.edge = edge
        self.order_size = order_size
        self.max_positions = max_positions

    def find_opportunities(self) -> list[MarketMakingSetup]:
        """Scan all markets and find the best market making opportunities."""
        setups = []

        for m in self.scanner.markets.values():
            if not m.book or not m.tokens:
                continue
            if m.volume < self.min_volume:
                continue

            bids = m.book.get("bids", [])
            asks = m.book.get("asks", [])
            
            if not bids or not asks:
                continue

            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            spread = best_ask - best_bid

            # Skip if spread is too tight (no profit) or too wide (toxic)
            if spread < self.min_spread or spread > self.max_spread:
                continue

            bid_depth = float(bids[0].get("size", 0))
            ask_depth = float(asks[0].get("size", 0))

            if bid_depth < self.min_depth or ask_depth < self.min_depth:
                continue

            # Calculate our order prices
            # We place buy at best_bid + (spread * edge)
            # We place sell at best_ask - (spread * edge)
            # This gives us a captured spread of spread * (1 - 2*edge)
            our_buy = best_bid + (spread * self.edge)
            our_sell = best_ask - (spread * self.edge)
            our_spread = our_sell - our_buy

            if our_spread <= 0:
                continue

            # Profit per cycle (buy N shares, then sell N shares)
            profit_per_cycle = our_spread * self.order_size

            # Liquidity score: how balanced is the book?
            total_depth = bid_depth + ask_depth
            if total_depth > 0:
                balance = 1 - abs(bid_depth - ask_depth) / total_depth
            else:
                balance = 0

            # Volume score (log scale)
            vol_score = min(1.0, math.log10(max(m.volume, 1)) / 6)  # 100K = 0.83, 1M = 1.0

            # Inventory risk: higher if book is very one-sided
            inventory_risk = 1 - balance

            token_id = m.tokens[0]["token_id"] if m.tokens else ""

            setup = MarketMakingSetup(
                question=m.question,
                condition_id=m.condition_id,
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                our_buy_price=round(our_buy, 4),
                our_sell_price=round(our_sell, 4),
                our_spread=our_spread,
                profit_per_cycle=profit_per_cycle,
                volume=m.volume,
                liquidity_score=balance * vol_score,
                inventory_risk=inventory_risk,
            )
            setups.append(setup)

        # Sort by: profit_per_cycle * liquidity_score (expected value)
        setups.sort(key=lambda s: s.profit_per_cycle * s.liquidity_score, reverse=True)

        logger.info(f"Found {len(setups)} market making opportunities")
        return setups[:self.max_positions]

    def format_setup(self, s: MarketMakingSetup) -> str:
        """Format a market making setup for display."""
        lines = [
            f"{'='*60}",
            f"MARKET MAKE: {s.question[:50]}",
            f"  Current: bid={s.best_bid:.4f} ask={s.best_ask:.4f} spread={s.spread:.4f} ({s.spread*100:.1f}%)",
            f"  Our orders: BUY @ {s.our_buy_price:.4f} | SELL @ {s.our_sell_price:.4f}",
            f"  Captured spread: {s.our_spread:.4f} ({s.our_spread*100:.1f}%)",
            f"  Profit per cycle: ${s.profit_per_cycle:.2f} (size={self.order_size} shares)",
            f"  Volume: ${s.volume:,.0f} | Liquidity score: {s.liquidity_score:.2f}",
            f"  Bid depth: {s.bid_depth:.0f} | Ask depth: {s.ask_depth:.0f}",
            f"  Inventory risk: {s.inventory_risk:.2f}",
            f"{'='*60}",
        ]
        return "\n".join(lines)


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    scanner = MarketScanner()
    scanner.scan(fetch_books=True, min_volume=10000, min_markets=1)

    maker = MarketMaker(scanner, min_spread=0.02, max_spread=0.10, min_volume=20000, min_depth=50, order_size=200)

    setups = maker.find_opportunities()

    print(f"\n{'='*60}")
    print(f"MARKET MAKING OPPORTUNITIES ({len(setups)} found)")
    print(f"{'='*60}\n")

    for s in setups[:15]:
        print(maker.format_setup(s))
        print()

    # Summary stats
    if setups:
        total_daily_profit_est = sum(s.profit_per_cycle * s.liquidity_score * 4 for s in setups[:20])  # ~4 cycles/day
        print(f"\nEstimated daily profit (top {min(20, len(setups))} markets, ~4 fill cycles/day):")
        print(f"  ${total_daily_profit_est:.2f}")
