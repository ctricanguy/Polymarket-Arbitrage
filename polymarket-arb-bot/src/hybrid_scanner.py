"""
Polymarket Hybrid Scanner
==========================

Uses REST API for market discovery (events, metadata) and WebSocket for
real-time orderbook data. This solves the problem where REST API orderbooks
show 99% spreads while the real books are tight.

Flow:
1. Fetch all events/markets from Gamma REST API (metadata, volumes, etc.)
2. Subscribe to WebSocket for real-time orderbook data
3. Use WebSocket books for spread/depth analysis
4. Fall back to REST API books only if WebSocket data unavailable
"""

import json
import logging
import time
import threading
from typing import Optional
from collections import defaultdict

from scanner import MarketScanner, Market, Event
from ws_client import MarketWebSocketClient, OrderBook

logger = logging.getLogger("polymarket-arb.hybrid_scanner")


class HybridScanner:
    """
    Scans Polymarket using REST for metadata + WebSocket for orderbooks.
    """

    def __init__(self, max_ws_assets: int = 200):
        """
        Args:
            max_ws_assets: Max tokens to subscribe to via WebSocket
                          (each market has 2 tokens, so 200 tokens = 100 markets)
        """
        self.rest_scanner = MarketScanner()
        self.ws_client = MarketWebSocketClient()
        self.max_ws_assets = max_ws_assets
        
        # Map: token_id -> condition_id (for looking up market from book)
        self.token_to_market: dict[str, Market] = {}
        self.token_to_event: dict[str, Event] = {}
        
        # All discovered markets (from REST)
        self.all_markets: dict[str, Market] = {}
        self.all_events: dict[str, Event] = {}
        
        # Stats
        self.ws_books_received = 0
        self.last_scan_time = 0

    def discover_markets(self, min_volume: float = 1000) -> int:
        """
        Phase 1: Discover all markets via REST API (metadata only, no books).
        Returns number of markets found.
        """
        logger.info("Phase 1: Discovering markets via REST API...")
        self.rest_scanner.scan(fetch_books=False, min_volume=min_volume, min_markets=1)
        
        self.all_markets = self.rest_scanner.markets
        self.all_events = self.rest_scanner.events
        
        # Build token -> market mapping
        self.token_to_market.clear()
        self.token_to_event.clear()
        
        for evt in self.all_events.values():
            for mkt in evt.markets:
                for token in mkt.tokens:
                    tid = token.get("token_id", "")
                    if tid:
                        self.token_to_market[tid] = mkt
                        self.token_to_event[tid] = evt
        
        logger.info(f"Discovered {len(self.all_markets)} markets, "
                    f"{len(self.token_to_market)} tokens")
        
        return len(self.all_markets)

    def subscribe_top_markets(self, min_volume: float = 5000, 
                               max_price: float = 0.95,
                               min_price: float = 0.01) -> int:
        """
        Phase 2: Subscribe to WebSocket for the most promising markets.
        
        Selects markets across ALL categories (not just top volume) to ensure
        diversification. Picks top N per category by volume to get a spread of
        sports, politics, crypto, tech, weather, geopolitics, etc.
        
        Returns number of tokens subscribed.
        """
        logger.info("Phase 2: Selecting markets for WebSocket subscription...")
        
        # Simple category classification (same logic as stat_detector)
        category_keywords = {
            "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
                      "baseball", "hockey", "fifa", "world cup", "tennis", "golf", "ufc",
                      "boxing", "cricket", "rugby", "olympics", "game", "match", "tournament",
                      "championship", "league", "score", "vs", "over", "under"],
            "crypto": ["bitcoin", "ethereum", "crypto", "btc", "eth", "solana", "sol",
                      "token", "defi", "nft", "blockchain", "stablecoin", "usdt", "usdc",
                      "market cap", "fdv", "coin", "binance", "coinbase"],
            "geopolitics": ["russia", "ukraine", "china", "iran", "israel", "war", "invasion",
                           "nuclear", "sanctions", "nato", "ceasefire", "putin", "korea",
                           "taiwan", "venezuela", "coup", "regime", "capture", "military"],
            "economy": ["fed", "interest rate", "inflation", "gdp", "recession", "unemployment",
                       "cpi", "treasury", "yield", "bank", "economic", "s&p", "nasdaq"],
            "tech": ["ai", "artificial intelligence", "openai", "anthropic", "google",
                    "apple", "microsoft", "meta", "nvidia", "gpt", "gemini", "claude",
                    "model", "llm", "ipo", "startup", "unicorn", "vision pro", "release",
                    "launch"],
            "weather": ["temperature", "hurricane", "earthquake", "storm", "heat", "cold",
                       "snow", "rainfall", "climate", "hottest", "coldest", "flood",
                       "drought", "wildfire"],
            "entertainment": ["movie", "album", "music", "artist", "spotify", "netflix",
                            "disney", "warner", "paramount", "acquisition", "merger",
                            "streaming", "box office", "grammy", "oscar"],
            "science": ["space", "moon", "mars", "nasa", "spacex", "rocket", "satellite",
                       "fusion", "quantum", "fda", "clinical trial"],
            "politics": ["election", "president", "congress", "senate", "governor",
                        "trump", "biden", "democrat", "republican", "parliament",
                        "prime minister", "cabinet", "pardon", "impeach", "supreme court",
                        "nomination", "primary"],
        }
        
        def classify(title):
            t = title.lower()
            for cat, keywords in category_keywords.items():
                for kw in keywords:
                    if kw in t:
                        return cat
            return "other"
        
        # Group markets by category
        by_category: dict[str, list[tuple[float, Market]]] = defaultdict(list)
        for mkt in self.all_markets.values():
            if mkt.volume < min_volume:
                continue
            
            prices = mkt.outcome_prices if hasattr(mkt, 'outcome_prices') and mkt.outcome_prices else []
            if not prices:
                continue
            
            yes_price = prices[0] if len(prices) > 0 else 0
            if yes_price < min_price or yes_price > max_price:
                continue
            
            cat = classify(mkt.question + " " + mkt.event_title)
            by_category[cat].append((mkt.volume, mkt))
        
        # Sort each category by volume
        for cat in by_category:
            by_category[cat].sort(key=lambda x: x[0], reverse=True)
        
        # Select markets: round-robin across categories to ensure diversity
        # Pick top N from each category, cycling through
        selected_tokens = []
        selected_markets = set()
        max_per_category = max(10, self.max_ws_assets // 20)  # ~20 markets per category
        
        cat_dist = ", ".join(f"{c}={len(v)}" for c, v in sorted(by_category.items(), key=lambda x: -len(x[1])))
        logger.info(f"Category distribution: {cat_dist}")
        
        # Round-robin: pick 1 from each category in turn, repeat
        round_idx = 0
        while len(selected_tokens) < self.max_ws_assets:
            added_any = False
            for cat in sorted(by_category.keys(), key=lambda c: len(by_category[c]), reverse=True):
                if round_idx >= len(by_category[cat]):
                    continue
                if len(selected_tokens) >= self.max_ws_assets:
                    break
                vol, mkt = by_category[cat][round_idx]
                if mkt.condition_id not in selected_markets:
                    for token in mkt.tokens:
                        tid = token.get("token_id", "")
                        if tid and len(selected_tokens) < self.max_ws_assets:
                            selected_tokens.append(tid)
                    selected_markets.add(mkt.condition_id)
                    added_any = True
            round_idx += 1
            if not added_any:
                break
        
        logger.info(f"Subscribing to {len(selected_tokens)} tokens "
                    f"({len(selected_markets)} markets) via WebSocket")
        
        self.ws_client.subscribe(selected_tokens)
        
        # Actually connect the WebSocket (subscribe only registers, doesn't connect)
        if not self.ws_client.connected:
            self.ws_client.connect()
        
        return len(selected_tokens)

    def connect_ws(self):
        """Connect to WebSocket."""
        self.ws_client.connect()

    def wait_for_books(self, timeout: float = 15.0) -> int:
        """Wait for WebSocket books to populate with actual data. Returns number of books with data."""
        logger.info(f"Waiting {timeout}s for WebSocket books to populate...")
        start = time.time()
        while time.time() - start < timeout:
            non_empty = sum(1 for b in self.ws_client.books.values() if b.bids and b.asks)
            if non_empty >= 20:  # At least 20 books with real data
                break
            time.sleep(0.5)
        
        self.ws_books_received = sum(1 for b in self.ws_client.books.values() if b.bids and b.asks)
        logger.info(f"Received {self.ws_books_received} WebSocket books with data")
        return self.ws_books_received

    def get_market_book(self, token_id: str) -> Optional[OrderBook]:
        """Get real-time orderbook for a token."""
        return self.ws_client.get_book(token_id)

    def get_markets_with_ws_books(self) -> list[tuple[Market, Event, OrderBook]]:
        """
        Get all markets that have WebSocket orderbook data.
        
        Returns list of (market, event, orderbook) tuples.
        """
        results = []
        for tid, book in self.ws_client.get_all_books().items():
            mkt = self.token_to_market.get(tid)
            evt = self.token_to_event.get(tid)
            if mkt and evt and book:
                # Only include Yes tokens (first token)
                if tid == mkt.tokens[0].get("token_id", ""):
                    results.append((mkt, evt, book))
        return results

    def get_scan_stats(self) -> dict:
        """Get scan statistics."""
        return {
            "markets_discovered": len(self.all_markets),
            "tokens_mapped": len(self.token_to_market),
            "ws_subscribed": len(self.ws_client.subscribed_assets),
            "ws_books": len(self.ws_client.books),
            "ws_messages": self.ws_client.messages_received,
            "last_scan_ago": time.time() - self.last_scan_time if self.last_scan_time else -1,
        }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    scanner = HybridScanner(max_ws_assets=200)
    
    # Phase 1: Discover markets
    scanner.discover_markets(min_volume=1000)
    
    # Phase 2: Subscribe to WebSocket
    scanner.subscribe_top_markets(min_volume=5000)
    scanner.connect_ws()
    scanner.wait_for_books(timeout=20)
    
    # Check results
    stats = scanner.get_scan_stats()
    print(f"\nScan stats: {stats}")
    
    # Show markets with tight spreads
    markets_with_books = scanner.get_markets_with_ws_books()
    print(f"\nMarkets with WebSocket books: {len(markets_with_books)}")
    
    tight_markets = []
    for mkt, evt, book in markets_with_books:
        bid, bid_size = book.get_best_bid()
        ask, ask_size = book.get_best_ask()
        spread = ask - bid
        if spread < 0.05 and bid > 0.01 and ask < 0.95:
            tight_markets.append((mkt, evt, book, bid, ask, spread, bid_size, ask_size))
    
    tight_markets.sort(key=lambda x: x[5])  # Sort by spread
    
    print(f"\nMarkets with tight spreads (<5%):")
    for mkt, evt, book, bid, ask, spread, bid_size, ask_size in tight_markets[:20]:
        print(f"  {mkt.question[:55]}")
        print(f"    bid={bid:.4f}x{bid_size:.0f} ask={ask:.4f}x{ask_size:.0f} "
              f"spread={spread*100:.1f}% vol=${mkt.volume:,.0f}")
    
    scanner.ws_client.disconnect()
