"""
Polymarket Arbitrage Detector
=============================
Scans for arbitrage opportunities in Polymarket events:

1. Sum-of-probabilities arbitrage: In multi-outcome events (e.g. "Who will win 
   the World Cup?"), the Yes prices of all outcomes should sum to 1.0 (100%).
   If they sum to >1.0, you can SELL all outcomes for a risk-free profit.
   If they sum to <1.0, you can BUY all outcomes for a risk-free profit.

2. Orderbook-based arbitrage: Uses actual ask/bid prices from the orderbook
   to calculate real executable profit accounting for slippage and depth.

3. Yes/No spread arbitrage: Within a single market, if Yes ask + No ask < 1.0,
   you can buy both for a guaranteed payout.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from scanner import MarketScanner, Market, Event

logger = logging.getLogger("polymarket-arb.detector")


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""
    event_title: str
    event_slug: str
    event_id: str
    arb_type: str  # "sum_overpriced", "sum_underpriced", "yes_no_spread"
    # Sum of prices
    sum_prices: float
    expected_sum: float  # always 1.0 for proper events
    mispricing_pct: float  # how far off from 100%
    # Estimated profit (before fees)
    gross_profit_pct: float
    # Per-market details
    markets: list = field(default_factory=list)  # [{"question", "token_id", "price", "side", "size"}, ...]
    # Orderbook-based profit (if available)
    net_profit_usd: float = 0.0
    max_investment_usd: float = 0.0
    executable: bool = False
    # Timestamp
    detected_at: float = field(default_factory=time.time)

    def __repr__(self):
        return (f"ArbOpportunity({self.event_title[:40]}, "
                f"type={self.arb_type}, mispricing={self.mispricing_pct:.2f}%, "
                f"net_profit=${self.net_profit_usd:.2f})")


class ArbitrageDetector:
    """Detects arbitrage opportunities from scanned market data."""

    def __init__(self, scanner: MarketScanner, min_mispricing: float = 0.01,
                 min_net_profit: float = 0.50, fee_bps: float = 0.0):
        """
        Args:
            scanner: A MarketScanner that has been scanned
            min_mispricing: Minimum mispricing (as decimal, 0.01 = 1%) to report
            min_net_profit: Minimum net profit in USD to flag as executable
            fee_bps: Trading fees in basis points (Polymarket is currently 0 bps for makers)
        """
        self.scanner = scanner
        self.min_mispricing = min_mispricing
        self.min_net_profit = min_net_profit
        self.fee_bps = fee_bps
        self.fee_rate = fee_bps / 10000.0

    def detect_all(self) -> list[ArbitrageOpportunity]:
        """Run all arbitrage detection strategies and return opportunities."""
        opportunities = []
        
        # Strategy 1: Sum-of-probabilities in multi-outcome events
        opportunities.extend(self._detect_sum_arbitrage())
        
        # Strategy 2: Yes/No spread within individual markets
        opportunities.extend(self._detect_yes_no_spread())

        # Sort by profit potential
        opportunities.sort(key=lambda o: o.net_profit_usd, reverse=True)
        
        logger.info(f"Detected {len(opportunities)} opportunities "
                     f"({sum(1 for o in opportunities if o.executable)} executable)")
        return opportunities

    def _detect_sum_arbitrage(self) -> list[ArbitrageOpportunity]:
        """
        Detect sum-of-probabilities arbitrage in multi-outcome events.
        
        Only runs on negRisk events (mutually exclusive outcomes) where
        the sum of Yes prices should equal 1.0.
        
        For events like "Who will win X?" with N markets:
        - If sum(Yes prices) > 1.0 → sell all Yes sides (overpriced)
        - If sum(Yes prices) < 1.0 → buy all Yes sides (underpriced)
        
        Uses orderbook asks/bids for real executable prices.
        """
        opportunities = []
        
        for evt in self.scanner.events.values():
            # Only negRisk events have mutually exclusive outcomes
            if not evt.neg_risk or len(evt.markets) < 2:
                continue

            # Get Yes prices (index 0 of outcome_prices)
            yes_prices = []
            for m in evt.markets:
                if m.outcome_prices and len(m.outcome_prices) > 0:
                    yes_prices.append(m.outcome_prices[0])
                else:
                    yes_prices = []
                    break

            if len(yes_prices) < 2:
                continue

            total = sum(yes_prices)
            mispricing = abs(total - 1.0)

            if mispricing < self.min_mispricing:
                continue

            arb_type = "sum_overpriced" if total > 1.0 else "sum_underpriced"
            
            # Build market details
            market_details = []
            for i, m in enumerate(evt.markets):
                token_id = m.tokens[0]["token_id"] if m.tokens else ""
                market_details.append({
                    "question": m.question,
                    "token_id": token_id,
                    "yes_price": yes_prices[i],
                    "condition_id": m.condition_id,
                    "book": m.book,
                })

            # Calculate executable profit using orderbooks
            net_profit, max_investment, executable = self._calc_sum_arb_profit(
                market_details, arb_type
            )

            opp = ArbitrageOpportunity(
                event_title=evt.title,
                event_slug=evt.slug,
                event_id=evt.id,
                arb_type=arb_type,
                sum_prices=total,
                expected_sum=1.0,
                mispricing_pct=mispricing * 100,
                gross_profit_pct=mispricing * 100,
                markets=market_details,
                net_profit_usd=net_profit,
                max_investment_usd=max_investment,
                executable=executable,
            )
            opportunities.append(opp)

        return opportunities

    def _calc_sum_arb_profit(self, market_details: list, arb_type: str) -> tuple:
        """
        Calculate actual executable profit using orderbook data.
        
        For overpriced (sum > 1.0): We want to SELL Yes on each market.
        - We hit the best BID on each market (sell at bid price)
        - Revenue = sum(bid_prices) * size
        - Payout = 1.0 * size (one outcome wins, we pay out $1 per share)
        - Profit = (revenue - payout) * size - fees
        
        For underpriced (sum < 1.0): We want to BUY Yes on each market.
        - We hit the best ASK on each market (buy at ask price)
        - Cost = sum(ask_prices) * size
        - Payout = 1.0 * size (one outcome wins, we collect $1 per share)
        - Profit = (payout - cost) * size - fees
        
        Filters out markets with wide spreads (> max_spread) since those
        midpoints are unreliable and not executable.
        """
        max_spread = 0.10  # Skip markets where bid-ask spread > 10%
        total_exec_price = 0.0
        min_depth = float('inf')
        
        for md in market_details:
            book = md.get("book")
            if not book:
                return 0.0, 0.0, False
            
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if not bids or not asks:
                return 0.0, 0.0, False
            
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            spread = best_ask - best_bid
            
            # Skip markets with unreasonable spreads
            if spread > max_spread:
                md["exec_price"] = None
                md["side"] = "SKIP"
                md["available_size"] = 0
                md["spread"] = spread
                continue
            
            md["spread"] = spread
            
            if arb_type == "sum_overpriced":
                # We sell Yes → hit the best bid
                bid_depth = float(bids[0].get("size", 0))
                total_exec_price += best_bid
                min_depth = min(min_depth, bid_depth)
                md["exec_price"] = best_bid
                md["side"] = "SELL"
                md["available_size"] = bid_depth
            else:
                # We buy Yes → hit the best ask
                ask_depth = float(asks[0].get("size", 0))
                total_exec_price += best_ask
                min_depth = min(min_depth, ask_depth)
                md["exec_price"] = best_ask
                md["side"] = "BUY"
                md["available_size"] = ask_depth

        # If any markets were skipped, we can't execute the full arb
        if any(md.get("side") == "SKIP" for md in market_details):
            return 0.0, 0.0, False

        if min_depth <= 0:
            return 0.0, 0.0, False

        # Calculate profit per share
        if arb_type == "sum_overpriced":
            # Revenue from selling all Yes sides, minus $1 payout when one wins
            profit_per_share = total_exec_price - 1.0
        else:
            # Cost of buying all Yes sides, collect $1 when one wins
            profit_per_share = 1.0 - total_exec_price

        if profit_per_share <= 0:
            # Orderbook spreads eat the mispricing — not actually executable
            return 0.0, 0.0, False

        # Max shares we can execute (limited by thinnest book)
        max_shares = min_depth
        
        # Investment: cost of buying all shares (for underpriced)
        # or margin/collateral for selling (for overpriced)
        if arb_type == "sum_overpriced":
            investment = 1.0 * max_shares  # collateral needed for payout
        else:
            investment = total_exec_price * max_shares

        # Net profit after fees
        gross_profit = profit_per_share * max_shares
        fees = gross_profit * self.fee_rate * len(market_details)  # fee per market
        net_profit = gross_profit - fees

        executable = net_profit >= self.min_net_profit

        return net_profit, investment, executable

    def _detect_yes_no_spread(self) -> list[ArbitrageOpportunity]:
        """
        Detect Yes/No spread arbitrage within individual markets.
        
        If Yes ask + No ask < 1.0, buy both for guaranteed $1 payout.
        If Yes bid + No bid > 1.0, sell both for guaranteed profit.
        """
        opportunities = []
        
        for m in self.scanner.markets.values():
            if not m.book or len(m.tokens) < 2:
                continue

            # We only have Yes orderbook; need to fetch No separately
            # For now, use the outcome_prices as a rough filter
            if not m.outcome_prices or len(m.outcome_prices) < 2:
                continue

            yes_price = m.outcome_prices[0]
            no_price = m.outcome_prices[1]
            total = yes_price + no_price
            mispricing = abs(total - 1.0)

            if mispricing < 0.02:  # Need bigger mispricing for Yes/No (fees eat it)
                continue

            arb_type = "yn_overpriced" if total > 1.0 else "yn_underpriced"

            opp = ArbitrageOpportunity(
                event_title=m.question,
                event_slug=m.slug,
                event_id=m.event_id,
                arb_type=arb_type,
                sum_prices=total,
                expected_sum=1.0,
                mispricing_pct=mispricing * 100,
                gross_profit_pct=mispricing * 100,
                markets=[{
                    "question": m.question,
                    "token_id": m.tokens[0]["token_id"],
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "condition_id": m.condition_id,
                }],
                net_profit_usd=0.0,
                executable=False,  # Would need both Yes and No orderbooks
            )
            opportunities.append(opp)

        return opportunities

    def format_opportunity(self, opp: ArbitrageOpportunity) -> str:
        """Format an opportunity for display/logging."""
        lines = []
        lines.append(f"{'='*60}")
        lines.append(f"EVENT: {opp.event_title}")
        lines.append(f"TYPE: {opp.arb_type}")
        lines.append(f"MISPRICING: {opp.mispricing_pct:.2f}%  (sum={opp.sum_prices*100:.1f}%)")
        lines.append(f"GROSS PROFIT: {opp.gross_profit_pct:.2f}%")
        lines.append(f"NET PROFIT: ${opp.net_profit_usd:.2f}")
        lines.append(f"INVESTMENT: ${opp.max_investment_usd:.2f}")
        lines.append(f"EXECUTABLE: {'✅ YES' if opp.executable else '❌ NO (spread too wide)'}")
        lines.append(f"MARKETS ({len(opp.markets)}):")
        for md in opp.markets[:10]:
            q = md.get("question", "?")[:50]
            price = md.get("exec_price", md.get("yes_price", 0))
            side = md.get("side", "")
            size = md.get("available_size", 0)
            if side:
                lines.append(f"  {side:4} @ {price:.4f}  size={size:.0f}  | {q}")
            else:
                lines.append(f"  price={price:.4f}  | {q}")
        if len(opp.markets) > 10:
            lines.append(f"  ... and {len(opp.markets) - 10} more")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    scanner = MarketScanner()
    summary = scanner.scan(fetch_books=True, min_volume=500, min_markets=2)
    print(json.dumps(summary, indent=2))
    
    detector = ArbitrageDetector(scanner, min_mispricing=0.005, min_net_profit=0.10)
    opportunities = detector.detect_all()
    
    print(f"\n\n{'='*60}")
    print(f"FOUND {len(opportunities)} ARBITRAGE OPPORTUNITIES")
    print(f"{'='*60}\n")
    
    for opp in opportunities[:15]:
        print(detector.format_opportunity(opp))
        print()
