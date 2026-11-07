"""
Polymarket Statistical Arbitrage Detector
==========================================

Reverse-engineered from the friend's actual bot strategy.

STRATEGY: Instead of riskless arbitrage (which doesn't exist in practice),
this detector finds UNDERPRICED low-probability Yes positions across diverse
markets. The edge comes from:

1. RETAIL BIAS: Polymarket traders systematically overprice "No" and underprice
   "Yes" on low-probability events. A market priced at 4¢ might have a true
   probability of 6-8¢, giving 50-100% edge per position.

2. DIVERSIFICATION: Holding 15-20 positions across different categories
   (politics, sports, crypto, weather, etc.) smooths variance. Each position
   earns $0.02-$0.28/day from small price drifts → ~$700/day total.

3. ENTRY CRITERIA (from video analysis):
   - Entry prices: 4¢-15¢ for most positions (low-probability Yes)
   - Some positions at 28¢-92¢ (higher probability events with edge)
   - Position size: ~$800-$1,300 each
   - Categories: diverse (politics, sports, crypto, weather, tech, geopolitics)
   - ~15-20 concurrent positions

SCORING: Each market gets an "edge score" based on:
   - Price level (cheaper = higher potential edge)
   - Volume/liquidity (higher = more reliable pricing, easier to exit)
   - Spread (tighter = better execution)
   - Time to resolution (longer = more time for price to drift)
   - Category diversity bonus (prefer categories not already in portfolio)
"""

import logging
import math
import time
import json
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
from scanner import MarketScanner, Market, Event

logger = logging.getLogger("polymarket-arb.stat_detector")


@dataclass
class StatArbOpportunity:
    """A statistical arbitrage opportunity — an underpriced position to buy."""
    question: str
    condition_id: str
    token_id: str  # Yes token to buy
    side: str = "YES"  # Always buying Yes
    
    # Pricing
    current_price: float = 0.0  # Current Yes ask price
    our_price_target: float = 0.0  # Price we'd place our buy order at
    
    # Liquidity
    volume: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    ask_depth: float = 0.0  # Shares available at ask
    
    # Scoring
    edge_score: float = 0.0  # Higher = better expected value
    liquidity_score: float = 0.0
    diversification_bonus: float = 0.0
    total_score: float = 0.0
    
    # Position sizing
    suggested_shares: int = 0
    suggested_investment: float = 0.0
    
    # Metadata
    category: str = ""
    event_title: str = ""
    end_date: str = ""
    neg_risk: bool = False
    
    # Expected daily P&L (estimated based on observed patterns)
    expected_daily_pnl: float = 0.0


class StatisticalArbDetector:
    """
    Detects underpriced positions for statistical arbitrage.
    
    Scans all markets, scores them by expected edge, and returns the best
    opportunities for portfolio construction.
    """

    def __init__(self, scanner: MarketScanner,
                 min_volume: float = 5000,
                 max_price: float = 0.95,  # Don't buy positions > 95¢
                 min_price: float = 0.01,   # Don't buy positions < 1¢
                 max_spread: float = 0.10,  # Max 10% spread
                 min_ask_depth: float = 100,  # Min shares available
                 target_position_size: float = 1000,  # $1000 per position
                 max_positions: int = 20,
                 min_positions: int = 15,
                 max_per_category: int = 5):
        self.scanner = scanner
        self.min_volume = min_volume
        self.max_price = max_price
        self.min_price = min_price
        self.max_spread = max_spread
        self.min_ask_depth = min_ask_depth
        self.target_position_size = target_position_size
        self.max_positions = max_positions
        self.min_positions = min_positions
        self.max_per_category = max_per_category
        
        # Track what categories are already in portfolio
        self.portfolio_categories: dict[str, int] = defaultdict(int)
        self.portfolio_condition_ids: set = set()

    def update_portfolio(self, positions: list[dict]):
        """Update the detector with current portfolio state.
        
        Args:
            positions: List of dicts with 'condition_id', 'category', 'side', 'shares'
        """
        self.portfolio_categories = defaultdict(int)
        self.portfolio_condition_ids = set()
        for pos in positions:
            cat = pos.get("category", "unknown")
            self.portfolio_categories[cat] += 1
            self.portfolio_condition_ids.add(pos.get("condition_id", ""))

    def _classify_category(self, event: Event, market: Market) -> str:
        """Classify a market into a category for diversification."""
        tags = event.tags if hasattr(event, 'tags') else []
        title = (event.title + " " + market.question).lower()
        
        categories = {
            "politics": ["politics", "election", "president", "congress", "senate", 
                        "governor", "trump", "biden", "democrat", "republican", "parliament",
                        "prime minister", "cabinet", "pardon", "impeach", "supreme court"],
            "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
                      "baseball", "hockey", "fifa", "world cup", "tennis", "golf", "ufc",
                      "boxing", "cricket", "rugby", "olympics", "game", "match", "tournament",
                      "championship", "league", "score", "vs", "over", "under"],
            "crypto": ["bitcoin", "ethereum", "crypto", "btc", "eth", "solana", "sol",
                      "token", "defi", "nft", "blockchain", "stablecoin", "usdt", "usdc",
                      "market cap", "fdv", "coin", "binance", "coinbase"],
            "geopolitics": ["russia", "ukraine", "china", "iran", "israel", "war", "invasion",
                           "nuclear", "sanctions", "nato", "eu", "un security", "ceasefire",
                           "putin", "xi jinping", "netanyahu", "korea", "taiwan", "venezuela",
                           "coup", "regime", "capture", "offensive", "military"],
            "economy": ["fed", "interest rate", "inflation", "gdp", "recession", "unemployment",
                       "cpi", "treasury", "yield", "bank", "economic", "market cap",
                       "s&p", "nasdaq", "dow", "recession", "soft landing"],
            "tech": ["ai", "artificial intelligence", "openai", "anthropic", "google",
                    "apple", "microsoft", "meta", "nvidia", "gpt", "gemini", "claude",
                    "model", "llm", "ipo", "startup", "unicorn", "vision pro", "iphone",
                    "release", "launch", "product"],
            "weather": ["temperature", "hurricane", "earthquake", "storm", "heat", "cold",
                       "snow", "rainfall", "climate", "hottest", "coldest", "flood",
                       "drought", "wildfire", "tornado"],
            "entertainment": ["movie", "album", "music", "artist", "spotify", "netflix",
                            "disney", "warner", "paramount", "acquisition", "merger",
                            "streaming", "box office", "grammy", "oscar", "award",
                            "celebrity", "tour", "concert"],
            "science": ["space", "moon", "mars", "nasa", "spacex", "rocket", "launch",
                       "satellite", "asteroid", "fusion", "quantum", "breakthrough",
                       "research", "study", "clinical trial", "fda"],
        }
        
        for category, keywords in categories.items():
            for kw in keywords:
                if kw in title:
                    return category
        
        return "other"

    def _calc_edge_score(self, price: float, volume: float, spread: float, 
                         ask_depth: float, end_date: str) -> float:
        """
        Calculate edge score for a market.
        
        Higher score = better expected value.
        
        The edge in low-probability markets comes from:
        1. Retail bias: cheap Yes positions tend to be underpriced
        2. Price drift: markets slowly correct toward true probability
        3. Volatility: low-probability events have high info sensitivity
        """
        score = 0.0
        
        # Price edge: cheaper positions have higher relative edge
        # A 4¢ position that should be 6¢ = 50% edge
        # A 60¢ position that should be 62¢ = 3% edge
        if price < 0.02:
            score += 6.0  # Extremely cheap = highest potential edge
        elif price < 0.05:
            score += 5.0  # Very cheap = very high potential edge
        elif price < 0.10:
            score += 3.5
        elif price < 0.15:
            score += 2.5
        elif price < 0.30:
            score += 1.5
        elif price < 0.50:
            score += 0.8
        elif price < 0.70:
            score += 0.3
        else:
            score += 0.1  # High-probability positions have less edge
        
        # Volume score: higher volume = more reliable pricing and easier exit
        # Use log scale for differentiation
        if volume > 0:
            score += min(3.0, math.log10(volume) / 3.0)  # 1M vol = 2.0, 100M = 3.0
        
        # Spread penalty: tighter spread = better execution
        spread_penalty = min(spread / 0.10, 3.0)  # Cap at -3
        score -= spread_penalty
        
        # Depth bonus: more liquidity at ask = can fill larger orders
        # Use log scale for differentiation
        if ask_depth > 0:
            score += min(2.0, math.log10(ask_depth) / 2.0)  # 10K depth = 2.0
        
        return max(score, 0.0)

    def _calc_diversification_bonus(self, category: str) -> float:
        """Bonus for markets in underrepresented categories."""
        count = self.portfolio_categories.get(category, 0)
        if count == 0:
            return 2.0  # New category = high bonus
        elif count == 1:
            return 1.0
        elif count == 2:
            return 0.3
        else:
            return 0.0  # Already have 3+ in this category

    def find_opportunities(self) -> list[StatArbOpportunity]:
        """
        Scan all markets and return ranked statistical arbitrage opportunities.
        """
        opportunities = []
        
        for m in self.scanner.markets.values():
            # Skip if already in portfolio
            if m.condition_id in self.portfolio_condition_ids:
                continue
            
            # Skip if no orderbook or tokens
            if not m.book or not m.tokens:
                continue
            
            # Volume filter
            if m.volume < self.min_volume:
                continue
            
            # Get Yes token data
            yes_token = m.tokens[0]["token_id"] if m.tokens else ""
            if not yes_token:
                continue
            
            # Get orderbook data
            bids = m.book.get("bids", [])
            asks = m.book.get("asks", [])
            if not bids or not asks:
                continue
            
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            ask_depth = float(asks[0].get("size", 0))
            spread = best_ask - best_bid
            
            # Price filters
            if best_ask < self.min_price or best_ask > self.max_price:
                continue
            
            # Spread filter
            if spread > self.max_spread:
                continue
            
            # Depth filter
            if ask_depth < self.min_ask_depth:
                continue
            
            # Find the parent event for category classification
            event = None
            for evt in self.scanner.events.values():
                if any(mkt.condition_id == m.condition_id for mkt in evt.markets):
                    event = evt
                    break
            
            category = self._classify_category(event, m) if event else "other"
            
            # Calculate scores
            edge_score = self._calc_edge_score(
                best_ask, m.volume, spread, ask_depth, ""
            )
            diversification_bonus = self._calc_diversification_bonus(category)
            
            # Liquidity score (0-1)
            liquidity_score = min(1.0, math.log10(max(m.volume, 1)) / 7)
            
            # Total score
            total_score = edge_score + diversification_bonus
            
            # Position sizing
            # Target ~$1000 per position, scale slightly by score but keep within bounds
            size_multiplier = min(1.3, max(0.8, total_score / 8.0))
            suggested_investment = self.target_position_size * size_multiplier
            suggested_shares = int(suggested_investment / best_ask)
            
            # Expected daily P&L (based on observed pattern: ~0.01-0.03% per position per day)
            expected_daily_pnl = suggested_investment * 0.0015  # ~0.15% daily
            
            opp = StatArbOpportunity(
                question=m.question,
                condition_id=m.condition_id,
                token_id=yes_token,
                side="YES",
                current_price=best_ask,
                our_price_target=best_bid + 0.001,  # Place limit order just above best bid
                volume=m.volume,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                ask_depth=ask_depth,
                edge_score=edge_score,
                liquidity_score=liquidity_score,
                diversification_bonus=diversification_bonus,
                total_score=total_score,
                suggested_shares=suggested_shares,
                suggested_investment=suggested_investment,
                category=category,
                event_title=event.title if event else "",
                neg_risk=event.neg_risk if event else False,
                expected_daily_pnl=expected_daily_pnl,
            )
            opportunities.append(opp)
        
        # Sort by total score (best first)
        opportunities.sort(key=lambda x: x.total_score, reverse=True)
        
        logger.info(f"Found {len(opportunities)} stat arb opportunities "
                    f"(top score: {opportunities[0].total_score:.2f})" if opportunities else
                    f"Found 0 stat arb opportunities")
        
        return opportunities[:self.max_positions * 3]  # Return 3x for selection

    def build_portfolio(self, available_capital: float) -> list[StatArbOpportunity]:
        """
        Build a diversified portfolio of positions within capital constraints.
        
        Args:
            available_capital: Total capital to deploy (e.g. $10000)
        
        Returns:
            List of selected opportunities to execute
        """
        all_opps = self.find_opportunities()
        
        selected = []
        deployed_capital = 0.0
        category_counts = defaultdict(int)
        
        for opp in all_opps:
            if len(selected) >= self.max_positions:
                break
            
            # Check capital constraint
            if deployed_capital + opp.suggested_investment > available_capital:
                # Try to fit a smaller position
                remaining = available_capital - deployed_capital
                if remaining < 200:  # Not enough for a meaningful position
                    break
                opp.suggested_investment = remaining
                opp.suggested_shares = int(remaining / opp.current_price)
            
            # Diversification: max per category (default 5)
            if category_counts[opp.category] >= self.max_per_category:
                continue
            
            selected.append(opp)
            deployed_capital += opp.suggested_investment
            category_counts[opp.category] += 1
        
        logger.info(f"Built portfolio: {len(selected)} positions, "
                    f"${deployed_capital:.2f} deployed, "
                    f"{len(category_counts)} categories")
        
        return selected

    def format_opp(self, opp: StatArbOpportunity) -> str:
        """Format opportunity for display."""
        lines = [
            f"{'='*60}",
            f"📊 {opp.question[:55]}",
            f"  Category: {opp.category} | Price: {opp.current_price:.4f} ({opp.current_price*100:.1f}¢)",
            f"  Spread: {opp.spread:.4f} | Volume: ${opp.volume:,.0f} | Depth: {opp.ask_depth:.0f}",
            f"  Score: {opp.total_score:.2f} (edge={opp.edge_score:.1f}, div={opp.diversification_bonus:.1f})",
            f"  Buy: {opp.suggested_shares:,} shares @ {opp.current_price:.4f} = ${opp.suggested_investment:.2f}",
            f"  Expected daily P&L: ${opp.expected_daily_pnl:.2f}",
            f"{'='*60}",
        ]
        return "\n".join(lines)


if __name__ == "__main__":
    import logging
    import time
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    from hybrid_scanner import HybridScanner
    
    # Use hybrid scanner: REST for discovery, WebSocket for books
    scanner = HybridScanner(max_ws_assets=400)  # 200 markets
    scanner.discover_markets(min_volume=1000)
    scanner.subscribe_top_markets(min_volume=5000)
    scanner.connect_ws()
    time.sleep(15)  # Wait for WS books to populate
    
    # Build a simple adapter so StatDetector can use hybrid scanner data
    class HybridAdapter:
        def __init__(self, hybrid):
            self.hybrid = hybrid
            self.markets = {}
            self.events = {}
            self._populate()
        
        def _populate(self):
            for mkt, evt, book in self.hybrid.get_markets_with_ws_books():
                bid, bid_size = book.get_best_bid()
                ask, ask_size = book.get_best_ask()
                mkt.book = {
                    "bids": [{"price": str(bid), "size": str(bid_size)}],
                    "asks": [{"price": str(ask), "size": str(ask_size)}],
                }
                self.markets[mkt.condition_id] = mkt
                self.events[evt.id] = evt
    
    adapter = HybridAdapter(scanner)
    print(f"Loaded {len(adapter.markets)} markets with WebSocket books")
    
    detector = StatisticalArbDetector(adapter, target_position_size=1000, max_positions=20,
                                      min_volume=5000, max_spread=0.10, min_ask_depth=100,
                                      max_per_category=5)
    
    print("\nFinding opportunities...")
    opps = detector.find_opportunities()
    
    print(f"\n{'='*60}")
    print(f"TOP 20 STATISTICAL ARBITRAGE OPPORTUNITIES")
    print(f"{'='*60}\n")
    
    for opp in opps[:20]:
        print(detector.format_opp(opp))
        print()
    
    # Build a $100K portfolio (matching friend's setup)
    print(f"\n{'='*60}")
    print(f"PORTFOLIO FOR $100,000 CAPITAL (matching friend's setup)")
    print(f"{'='*60}\n")
    
    detector2 = StatisticalArbDetector(adapter, target_position_size=5000, max_positions=20,
                                       min_volume=5000, max_spread=0.10, min_ask_depth=100,
                                       max_per_category=5)
    portfolio = detector2.build_portfolio(available_capital=100000)
    
    total_daily_pnl = sum(o.expected_daily_pnl for o in portfolio)
    total_invested = sum(o.suggested_investment for o in portfolio)
    
    for opp in portfolio:
        print(detector.format_opp(opp))
        print()
    
    print(f"\n{'='*60}")
    print(f"PORTFOLIO SUMMARY")
    print(f"{'='*60}")
    print(f"  Positions: {len(portfolio)}")
    print(f"  Capital deployed: ${total_invested:,.2f}")
    print(f"  Expected daily P&L: ${total_daily_pnl:.2f}")
    print(f"  Expected monthly P&L: ${total_daily_pnl*30:.2f}")
    print(f"  Categories: {', '.join(set(o.category for o in portfolio))}")
    
    scanner.ws_client.disconnect()
