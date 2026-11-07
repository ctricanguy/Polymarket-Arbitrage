"""
Real-Time Arbitrage Engine
==========================

Uses WebSocket orderbook data to detect and execute arbitrage in real-time.

Two strategies:
1. YES/NO ARBITRAGE: For each market, if Yes_ask + No_ask < 1.0, buy both.
   If Yes_bid + No_bid > 1.0, sell both.

2. MULTI-OUTCOME ARBITRAGE: For negRisk events, if sum of all Yes_asks < 1.0,
   buy all outcomes. Guaranteed $1.00 payout.

The engine monitors orderbook updates in real-time and triggers execution
the instant an opportunity appears.
"""

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable
from collections import defaultdict

from ws_client import OrderBook, MarketWebSocketClient

logger = logging.getLogger("polymarket-arb.engine")


@dataclass
class RealTimeArbOpportunity:
    """Real-time arbitrage opportunity detected from WebSocket data."""
    market_question: str
    arb_type: str  # "yes_no_buy", "yes_no_sell", "multi_outcome_buy", "multi_outcome_sell"
    # For Yes/No arb
    yes_token: str = ""
    no_token: str = ""
    yes_price: float = 0.0  # ask (buy) or bid (sell)
    no_price: float = 0.0
    sum_prices: float = 0.0
    profit_per_share: float = 0.0
    max_shares: float = 0.0  # min depth across legs
    estimated_profit: float = 0.0
    # For multi-outcome
    event_title: str = ""
    outcome_tokens: list = field(default_factory=list)
    outcome_prices: list = field(default_factory=list)
    # Timing
    detected_at: float = field(default_factory=time.time)
    book_age_ms: float = 0  # How old is the book data


class RealTimeArbEngine:
    """
    Real-time arbitrage detection engine.
    
    Listens to WebSocket orderbook updates and detects arbitrage opportunities
    the instant they appear. Designed for low-latency execution on AWS.
    """

    def __init__(self, ws_client: MarketWebSocketClient,
                 on_opportunity: Optional[Callable] = None,
                 min_profit_per_share: float = 0.005,  # 0.5¢ minimum
                 min_depth: float = 5.0,
                 max_book_age_ms: float = 5000):
        """
        Args:
            ws_client: Connected MarketWebSocketClient
            on_opportunity: Callback when arb opportunity is detected
            min_profit_per_share: Minimum profit per share to trigger (0.005 = 0.5%)
            min_depth: Minimum depth (shares) at the required price levels
            max_book_age_ms: Max age of book data to consider valid (ms)
        """
        self.ws = ws_client
        self.on_opportunity = on_opportunity
        self.min_profit = min_profit_per_share
        self.min_depth = min_depth
        self.max_book_age_ms = max_book_age_ms
        
        # Market metadata: token_id -> {question, event_title, is_yes, pair_token, condition_id}
        self.token_metadata: dict[str, dict] = {}
        # Event metadata: event_id -> {title, neg_risk, outcome_tokens: [(yes_token, no_token), ...]}
        self.event_metadata: dict[str, dict] = {}
        # Pair mapping: yes_token -> no_token and vice versa
        self.token_pairs: dict[str, str] = {}
        
        # Stats
        self.opportunities_detected = 0
        self.last_opportunity: Optional[RealTimeArbOpportunity] = None
        self._lock = threading.RLock()

    def register_market(self, yes_token: str, no_token: str, 
                        question: str, event_title: str = "",
                        event_id: str = "", condition_id: str = "",
                        neg_risk: bool = False):
        """Register a market's token pair for monitoring."""
        with self._lock:
            self.token_metadata[yes_token] = {
                "question": question,
                "event_title": event_title,
                "event_id": event_id,
                "is_yes": True,
                "pair_token": no_token,
                "condition_id": condition_id,
            }
            self.token_metadata[no_token] = {
                "question": question,
                "event_title": event_title,
                "event_id": event_id,
                "is_yes": False,
                "pair_token": yes_token,
                "condition_id": condition_id,
            }
            self.token_pairs[yes_token] = no_token
            self.token_pairs[no_token] = yes_token
            
            # Register with event
            if event_id:
                if event_id not in self.event_metadata:
                    self.event_metadata[event_id] = {
                        "title": event_title,
                        "neg_risk": neg_risk,
                        "markets": [],
                    }
                self.event_metadata[event_id]["markets"].append({
                    "yes_token": yes_token,
                    "no_token": no_token,
                    "question": question,
                    "condition_id": condition_id,
                })

    def check_yes_no_arb(self, yes_token: str) -> Optional[RealTimeArbOpportunity]:
        """
        Check for Yes/No arbitrage on a single market.
        
        Buy arb: if Yes_ask + No_ask < 1.0, buy both for guaranteed $1 payout.
        Sell arb: if Yes_bid + No_bid > 1.0, sell both for guaranteed profit.
        """
        no_token = self.token_pairs.get(yes_token)
        if not no_token:
            return None
        
        yes_book = self.ws.get_book(yes_token)
        no_book = self.ws.get_book(no_token)
        
        if not yes_book or not no_book:
            return None
        
        # Check book age
        now = time.time()
        yes_age = (now - yes_book.last_update) * 1000
        no_age = (now - no_book.last_update) * 1000
        if yes_age > self.max_book_age_ms or no_age > self.max_book_age_ms:
            return None
        
        yes_bid, yes_bid_size = yes_book.get_best_bid()
        yes_ask, yes_ask_size = yes_book.get_best_ask()
        no_bid, no_bid_size = no_book.get_best_bid()
        no_ask, no_ask_size = no_book.get_best_ask()
        
        meta = self.token_metadata.get(yes_token, {})
        question = meta.get("question", "?")
        
        # Check BUY arbitrage: buy Yes at ask + buy No at ask < $1.00
        buy_sum = yes_ask + no_ask
        buy_profit = 1.0 - buy_sum
        buy_depth = min(yes_ask_size, no_ask_size)
        
        if buy_profit > self.min_profit and buy_depth >= self.min_depth:
            opp = RealTimeArbOpportunity(
                market_question=question,
                arb_type="yes_no_buy",
                yes_token=yes_token,
                no_token=no_token,
                yes_price=yes_ask,
                no_price=no_ask,
                sum_prices=buy_sum,
                profit_per_share=buy_profit,
                max_shares=buy_depth,
                estimated_profit=buy_profit * buy_depth,
                book_age_ms=max(yes_age, no_age),
            )
            return opp
        
        # Check SELL arbitrage: sell Yes at bid + sell No at bid > $1.00
        sell_sum = yes_bid + no_bid
        sell_profit = sell_sum - 1.0
        sell_depth = min(yes_bid_size, no_bid_size)
        
        if sell_profit > self.min_profit and sell_depth >= self.min_depth:
            opp = RealTimeArbOpportunity(
                market_question=question,
                arb_type="yes_no_sell",
                yes_token=yes_token,
                no_token=no_token,
                yes_price=yes_bid,
                no_price=no_bid,
                sum_prices=sell_sum,
                profit_per_share=sell_profit,
                max_shares=sell_depth,
                estimated_profit=sell_profit * sell_depth,
                book_age_ms=max(yes_age, no_age),
            )
            return opp
        
        return None

    def check_multi_outcome_arb(self, event_id: str) -> Optional[RealTimeArbOpportunity]:
        """
        Check for multi-outcome arbitrage on a negRisk event.
        
        If sum of all Yes asks < 1.0, buy all outcomes for guaranteed $1 payout.
        If sum of all Yes bids > 1.0, sell all outcomes for guaranteed profit.
        """
        evt = self.event_metadata.get(event_id)
        if not evt or not evt.get("neg_risk") or len(evt["markets"]) < 2:
            return None
        
        now = time.time()
        
        # Check BUY: sum of Yes asks
        sum_asks = 0
        min_ask_depth = float('inf')
        tokens_to_buy = []
        prices_to_buy = []
        all_fresh = True
        all_have_book = True
        
        for mkt in evt["markets"]:
            yes_token = mkt["yes_token"]
            book = self.ws.get_book(yes_token)
            if not book:
                all_have_book = False
                break
            
            age = (now - book.last_update) * 1000
            if age > self.max_book_age_ms:
                all_fresh = False
                break
            
            _, ask_size = book.get_best_ask()
            ask_price, ask_size = book.get_best_ask()
            sum_asks += ask_price
            min_ask_depth = min(min_ask_depth, ask_size)
            tokens_to_buy.append(yes_token)
            prices_to_buy.append(ask_price)
        
        if all_have_book and all_fresh and min_ask_depth >= self.min_depth:
            buy_profit = 1.0 - sum_asks
            if buy_profit > self.min_profit:
                return RealTimeArbOpportunity(
                    market_question=evt["title"],
                    arb_type="multi_outcome_buy",
                    event_title=evt["title"],
                    outcome_tokens=tokens_to_buy,
                    outcome_prices=prices_to_buy,
                    sum_prices=sum_asks,
                    profit_per_share=buy_profit,
                    max_shares=min_ask_depth,
                    estimated_profit=buy_profit * min_ask_depth,
                )
        
        # Check SELL: sum of Yes bids
        sum_bids = 0
        min_bid_depth = float('inf')
        tokens_to_sell = []
        prices_to_sell = []
        all_fresh = True
        all_have_book = True
        
        for mkt in evt["markets"]:
            yes_token = mkt["yes_token"]
            book = self.ws.get_book(yes_token)
            if not book:
                all_have_book = False
                break
            
            age = (now - book.last_update) * 1000
            if age > self.max_book_age_ms:
                all_fresh = False
                break
            
            bid_price, bid_size = book.get_best_bid()
            sum_bids += bid_price
            min_bid_depth = min(min_bid_depth, bid_size)
            tokens_to_sell.append(yes_token)
            prices_to_sell.append(bid_price)
        
        if all_have_book and all_fresh and min_bid_depth >= self.min_depth:
            sell_profit = sum_bids - 1.0
            if sell_profit > self.min_profit:
                return RealTimeArbOpportunity(
                    market_question=evt["title"],
                    arb_type="multi_outcome_sell",
                    event_title=evt["title"],
                    outcome_tokens=tokens_to_sell,
                    outcome_prices=prices_to_sell,
                    sum_prices=sum_bids,
                    profit_per_share=sell_profit,
                    max_shares=min_bid_depth,
                    estimated_profit=sell_profit * min_bid_depth,
                )
        
        return None

    def scan_all(self) -> list[RealTimeArbOpportunity]:
        """Scan all registered markets for arbitrage. Call on each book update."""
        opportunities = []
        
        # Check all Yes/No pairs
        checked_pairs = set()
        for yes_token in list(self.token_pairs.keys()):
            if yes_token in checked_pairs:
                continue
            no_token = self.token_pairs[yes_token]
            checked_pairs.add(yes_token)
            checked_pairs.add(no_token)
            
            opp = self.check_yes_no_arb(yes_token)
            if opp:
                opportunities.append(opp)
        
        # Check all multi-outcome events
        for event_id in self.event_metadata:
            opp = self.check_multi_outcome_arb(event_id)
            if opp:
                opportunities.append(opp)
        
        if opportunities:
            self.opportunities_detected += len(opportunities)
            self.last_opportunity = opportunities[0]
            
            if self.on_opportunity:
                for opp in opportunities:
                    self.on_opportunity(opp)
        
        return opportunities

    def format_opp(self, opp: RealTimeArbOpportunity) -> str:
        """Format opportunity for display."""
        lines = [
            f"🔥 ARB DETECTED: {opp.market_question[:50]}",
            f"  Type: {opp.arb_type}",
            f"  Profit/share: {opp.profit_per_share*100:.2f}¢ (${opp.estimated_profit:.2f} total)",
            f"  Sum: {opp.sum_prices*100:.2f}% | Max shares: {opp.max_shares:.0f}",
            f"  Book age: {opp.book_age_ms:.0f}ms",
        ]
        if opp.yes_token:
            lines.append(f"  Yes @ {opp.yes_price:.4f} + No @ {opp.no_price:.4f} = {opp.sum_prices:.4f}")
        elif opp.outcome_tokens:
            lines.append(f"  {len(opp.outcome_tokens)} outcomes, sum={opp.sum_prices:.4f}")
        return "\n".join(lines)
