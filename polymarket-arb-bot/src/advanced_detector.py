"""
Polymarket Advanced Arbitrage Strategies
========================================

The simple "sum of prices" approach rarely finds executable arbitrage because
market makers already capture the spread. Real profitable strategies include:

1. LIMIT ORDER ARBITRAGE (the real money maker):
   - Place BUY limit orders on all outcomes of a negRisk event at prices
     that sum to < 1.0
   - When all orders fill, you've spent < $1.00 total and are guaranteed
     a $1.00 payout (one outcome must win)
   - Profit = $1.00 - sum(fill prices)
   - Key insight: You don't need all fills simultaneously — you hedge
     as fills come in

2. DEPTH-AWARE ARBITRAGE:
   - Scan deeper into the orderbook (not just top of book)
   - Calculate profit at each depth level
   - Execute partially at multiple price levels

3. CROSS-MARKET CORRELATION:
   - Find markets that should be correlated but aren't
   - E.g., "Republican wins 2028" vs "J.D. Vance wins 2028"
   - The relationship between these should be predictable

4. MARKET MAKING (spread capture):
   - Place both bids and asks on liquid markets
   - Earn the spread when both sides fill
   - Manage inventory risk
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from scanner import MarketScanner, Market, Event
from detector import ArbitrageOpportunity, ArbitrageDetector

logger = logging.getLogger("polymarket-arb.advanced")


@dataclass
class LimitOrderArbOpportunity:
    """A limit-order arbitrage opportunity on a negRisk event."""
    event_title: str
    event_slug: str
    event_id: str
    # The strategy: place buy limit orders at these prices
    target_buy_prices: list = field(default_factory=list)  # per market
    sum_target_prices: float = 0.0  # should be < 1.0 for profit
    expected_profit_per_share: float = 0.0
    # How much below the current ask we're placing
    edge_per_market: list = field(default_factory=list)
    # Liquidity check
    total_liquidity_usd: float = 0.0
    # Market details
    markets: list = field(default_factory=list)
    detected_at: float = field(default_factory=time.time)

    def __repr__(self):
        return (f"LimitOrderArb({self.event_title[:40]}, "
                f"sum={self.sum_target_prices:.4f}, "
                f"profit/share={self.expected_profit_per_share:.4f})")


class AdvancedArbitrageDetector:
    """Advanced arbitrage detection using limit orders and depth analysis."""

    def __init__(self, scanner: MarketScanner, 
                 min_profit_per_share: float = 0.02,
                 max_spread: float = 0.15,
                 min_liquidity: float = 1000):
        """
        Args:
            scanner: Scanned MarketScanner
            min_profit_per_share: Minimum profit per share to target (0.02 = 2¢)
            max_spread: Max bid-ask spread to consider a market tradeable
            min_liquidity: Min liquidity for the event
        """
        self.scanner = scanner
        self.min_profit_per_share = min_profit_per_share
        self.max_spread = max_spread
        self.min_liquidity = min_liquidity

    def detect_limit_order_arb(self) -> list[LimitOrderArbOpportunity]:
        """
        Detect limit order arbitrage opportunities.
        
        Strategy: For each negRisk event, look at the best ask prices.
        If we place buy limit orders slightly below the asks on all outcomes,
        and those limit prices sum to < 1.0, we have a profitable strategy.
        
        The key insight: we don't need to hit asks. We place passive limit
        orders and wait for fills. When all outcomes are filled, we're
        guaranteed a $1.00 payout for less than $1.00 invested.
        """
        opportunities = []

        for evt in self.scanner.events.values():
            if not evt.neg_risk or len(evt.markets) < 2:
                continue
            if evt.volume < self.min_liquidity:
                continue

            # Collect best asks for all markets
            market_data = []
            sum_asks = 0.0
            sum_bids = 0.0
            all_tradeable = True

            for m in evt.markets:
                if not m.book:
                    all_tradeable = False
                    break

                bids = m.book.get("bids", [])
                asks = m.book.get("asks", [])
                
                if not bids or not asks:
                    all_tradeable = False
                    break

                best_bid = float(bids[0].get("price", 0))
                best_ask = float(asks[0].get("price", 0))
                spread = best_ask - best_bid

                if spread > self.max_spread:
                    all_tradeable = False
                    break

                sum_asks += best_ask
                sum_bids += best_bid

                market_data.append({
                    "question": m.question,
                    "condition_id": m.condition_id,
                    "token_id": m.tokens[0]["token_id"] if m.tokens else "",
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "bid_depth": float(bids[0].get("size", 0)),
                    "ask_depth": float(asks[0].get("size", 0)),
                    "book": m.book,
                })

            if not all_tradeable or len(market_data) < 2:
                continue

            # Strategy: place buy limit orders at best_bid + small edge
            # This sits between bid and ask, likely to fill
            # Target price = best_bid + (spread * 0.3) — closer to bid
            edge_factor = 0.3  # How far into the spread we place (0=at bid, 1=at ask)
            
            target_prices = []
            for md in market_data:
                target = md["best_bid"] + (md["spread"] * edge_factor)
                target_prices.append(target)
            
            sum_targets = sum(target_prices)
            profit_per_share = 1.0 - sum_targets

            if profit_per_share < self.min_profit_per_share:
                continue

            # Also check: what if we just hit the asks? (aggressive entry)
            aggressive_profit = 1.0 - sum_asks

            # Calculate total liquidity (sum of all bid depths * bid prices)
            total_liq = sum(md["bid_depth"] * md["best_bid"] for md in market_data)

            opp = LimitOrderArbOpportunity(
                event_title=evt.title,
                event_slug=evt.slug,
                event_id=evt.id,
                target_buy_prices=target_prices,
                sum_target_prices=sum_targets,
                expected_profit_per_share=profit_per_share,
                edge_per_market=[t - md["best_bid"] for t, md in zip(target_prices, market_data)],
                total_liquidity_usd=total_liq,
                markets=market_data,
            )
            opportunities.append(opp)

        # Sort by profit per share
        opportunities.sort(key=lambda o: o.expected_profit_per_share, reverse=True)
        
        logger.info(f"Found {len(opportunities)} limit-order arb opportunities")
        return opportunities

    def detect_depth_arb(self) -> list[ArbitrageOpportunity]:
        """
        Scan deeper into orderbooks for partial-fill arbitrage.
        
        Instead of just looking at top-of-book, walk through multiple
        price levels to find executable profit at any depth.
        """
        opportunities = []

        for evt in self.scanner.events.values():
            if not evt.neg_risk or len(evt.markets) < 2:
                continue

            # For underpriced events (sum of asks < 1.0 at some depth)
            # Build cumulative ask curves
            all_asks = []
            all_bids = []
            
            for m in evt.markets:
                if not m.book:
                    break
                asks = m.book.get("asks", [])
                bids = m.book.get("bids", [])
                if not asks or not bids:
                    break
                all_asks.append([(float(a["price"]), float(a["size"])) for a in asks[:10]])
                all_bids.append([(float(b["price"]), float(b["size"])) for b in bids[:10]])

            if len(all_asks) != len(evt.markets):
                continue

            # Calculate profit at top of book (aggressive)
            sum_top_asks = sum(asks[0][0] for asks in all_asks)
            aggressive_profit = 1.0 - sum_top_asks

            if aggressive_profit > 0.005:  # 0.5% profit at top of book
                # This is actually executable!
                min_depth = min(asks[0][1] for asks in all_asks)
                net_profit = aggressive_profit * min_depth

                if net_profit >= 0.50:
                    market_details = []
                    for i, m in enumerate(evt.markets):
                        market_details.append({
                            "question": m.question,
                            "token_id": m.tokens[0]["token_id"] if m.tokens else "",
                            "exec_price": all_asks[i][0][0],
                            "side": "BUY",
                            "available_size": all_asks[i][0][1],
                            "condition_id": m.condition_id,
                        })

                    opp = ArbitrageOpportunity(
                        event_title=evt.title,
                        event_slug=evt.slug,
                        event_id=evt.id,
                        arb_type="depth_aggressive_buy",
                        sum_prices=sum_top_asks,
                        expected_sum=1.0,
                        mispricing_pct=(1.0 - sum_top_asks) * 100,
                        gross_profit_pct=aggressive_profit * 100,
                        markets=market_details,
                        net_profit_usd=net_profit,
                        max_investment_usd=sum_top_asks * min_depth,
                        executable=True,
                    )
                    opportunities.append(opp)

            # Check sell side (overpriced)
            sum_top_bids = sum(bids[0][0] for bids in all_bids)
            sell_profit = sum_top_bids - 1.0

            if sell_profit > 0.005:
                min_depth = min(bids[0][1] for bids in all_bids)
                net_profit = sell_profit * min_depth

                if net_profit >= 0.50:
                    market_details = []
                    for i, m in enumerate(evt.markets):
                        market_details.append({
                            "question": m.question,
                            "token_id": m.tokens[0]["token_id"] if m.tokens else "",
                            "exec_price": all_bids[i][0][0],
                            "side": "SELL",
                            "available_size": all_bids[i][0][1],
                            "condition_id": m.condition_id,
                        })

                    opp = ArbitrageOpportunity(
                        event_title=evt.title,
                        event_slug=evt.slug,
                        event_id=evt.id,
                        arb_type="depth_aggressive_sell",
                        sum_prices=sum_top_bids,
                        expected_sum=1.0,
                        mispricing_pct=(sum_top_bids - 1.0) * 100,
                        gross_profit_pct=sell_profit * 100,
                        markets=market_details,
                        net_profit_usd=net_profit,
                        max_investment_usd=min_depth,  # collateral
                        executable=True,
                    )
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.net_profit_usd, reverse=True)
        logger.info(f"Found {len(opportunities)} depth arb opportunities "
                     f"({sum(1 for o in opportunities if o.executable)} executable)")
        return opportunities

    def format_limit_order_opp(self, opp: LimitOrderArbOpportunity) -> str:
        """Format a limit order arb opportunity for display."""
        lines = []
        lines.append(f"{'='*60}")
        lines.append(f"LIMIT ORDER ARB: {opp.event_title}")
        lines.append(f"Sum of target prices: {opp.sum_target_prices*100:.2f}%")
        lines.append(f"Expected profit/share: {opp.expected_profit_per_share*100:.2f}¢")
        lines.append(f"Total liquidity: ${opp.total_liquidity_usd:.0f}")
        lines.append(f"MARKETS ({len(opp.markets)}):")
        for i, md in enumerate(opp.markets[:15]):
            q = md["question"][:45]
            target = opp.target_buy_prices[i]
            lines.append(f"  BUY @ {target:.4f}  (bid={md['best_bid']:.4f} ask={md['best_ask']:.4f} spread={md['spread']:.4f})  | {q}")
        if len(opp.markets) > 15:
            lines.append(f"  ... and {len(opp.markets) - 15} more")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    scanner = MarketScanner()
    scanner.scan(fetch_books=True, min_volume=500, min_markets=2)
    
    adv = AdvancedArbitrageDetector(scanner, min_profit_per_share=0.01)
    
    print("\n" + "="*60)
    print("LIMIT ORDER ARBITRAGE OPPORTUNITIES")
    print("="*60 + "\n")
    
    limit_opps = adv.detect_limit_order_arb()
    for opp in limit_opps[:10]:
        print(adv.format_limit_order_opp(opp))
        print()
    
    print("\n" + "="*60)
    print("DEPTH ARBITRAGE (aggressive — hitting top of book)")
    print("="*60 + "\n")
    
    depth_opps = adv.detect_depth_arb()
    for opp in depth_opps[:10]:
        from detector import ArbitrageDetector
        det = ArbitrageDetector(scanner)
        print(det.format_opportunity(opp))
        print()
    
    if not depth_opps:
        print("No aggressive depth arb opportunities found.")
        print("(This is normal — efficient markets price these correctly.)")
