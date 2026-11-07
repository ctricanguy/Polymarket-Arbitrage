"""
Polymarket Market Scanner
=========================
Polls Gamma + CLOB APIs for all active markets, fetches orderbooks,
and builds a cached view of the market state for the arbitrage detector.
"""

import json
import time
import logging
import requests
from typing import Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("polymarket-arb.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Be polite but fast
MAX_WORKERS = 20
REQUEST_TIMEOUT = 10


@dataclass
class Market:
    """Represents a single Polymarket binary market."""
    condition_id: str
    question: str
    slug: str
    tokens: list  # [{"token_id": ..., "outcome": "Yes"/"No"}, ...]
    outcome_prices: list  # [yes_price, no_price] as floats
    volume: float
    liquidity: float
    active: bool
    closed: bool
    end_date: str
    category: str
    # Orderbook data (filled by fetch_orderbooks)
    book: Optional[dict] = field(default=None)
    # Parent event info
    event_id: str = ""
    event_title: str = ""
    event_slug: str = ""
    neg_risk: bool = False  # True = mutually exclusive outcomes (arb-eligible)


@dataclass
class Event:
    """Represents a Polymarket event containing one or more markets."""
    id: str
    title: str
    slug: str
    volume: float
    neg_risk: bool = False  # True = mutually exclusive outcomes (arb-eligible)
    markets: list = field(default_factory=list)


class MarketScanner:
    """Scans Polymarket for active markets and builds the market state cache."""

    def __init__(self, max_workers: int = MAX_WORKERS):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "polymarket-arb-bot/1.0"})
        self.max_workers = max_workers
        self.events: dict[str, Event] = {}
        self.markets: dict[str, Market] = {}  # condition_id -> Market
        self.last_scan_time: float = 0

    def _get(self, url: str, params: dict = None) -> any:
        """GET request with retry."""
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Failed GET {url}: {e}")
                    return None
                time.sleep(0.5 * (attempt + 1))
        return None

    def fetch_active_events(self, limit: int = 500) -> list:
        """Fetch all active events from Gamma API, paginating if needed."""
        all_events = []
        offset = 0
        batch = 100

        while offset < limit:
            data = self._get(f"{GAMMA_API}/events", params={
                "limit": batch,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
            })
            if not data or not isinstance(data, list):
                break
            all_events.extend(data)
            if len(data) < batch:
                break
            offset += batch

        logger.info(f"Fetched {len(all_events)} active events from Gamma")
        return all_events

    def fetch_active_markets(self, limit: int = 500) -> list:
        """Fetch all active markets from Gamma API."""
        all_markets = []
        offset = 0
        batch = 100

        while offset < limit:
            data = self._get(f"{GAMMA_API}/markets", params={
                "limit": batch,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
            })
            if not data or not isinstance(data, list):
                break
            all_markets.extend(data)
            if len(data) < batch:
                break
            offset += batch

        logger.info(f"Fetched {len(all_markets)} active markets from Gamma")
        return all_markets

    def _parse_json_field(self, val):
        """Parse double-encoded JSON fields from Gamma API."""
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        return val

    def build_market_from_gamma(self, m: dict, event: dict = None) -> Market:
        """Convert a Gamma API market dict into a Market dataclass."""
        clob_tokens = self._parse_json_field(m.get("clobTokenIds", "[]"))
        outcomes = self._parse_json_field(m.get("outcomes", "[]"))
        prices = self._parse_json_field(m.get("outcomePrices", "[]"))

        tokens = []
        if isinstance(clob_tokens, list):
            for i, tid in enumerate(clob_tokens):
                label = outcomes[i] if isinstance(outcomes, list) and i < len(outcomes) else f"Outcome {i}"
                tokens.append({"token_id": tid, "outcome": label})

        price_floats = []
        if isinstance(prices, list):
            price_floats = [float(p) for p in prices]

        return Market(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            tokens=tokens,
            outcome_prices=price_floats,
            volume=float(m.get("volume", 0) or 0),
            liquidity=float(m.get("liquidity", 0) or 0),
            active=m.get("active", False),
            closed=m.get("closed", False),
            end_date=m.get("endDate", ""),
            category=m.get("category", ""),
            event_id=event.get("id", "") if event else "",
            event_title=event.get("title", "") if event else "",
            event_slug=event.get("slug", "") if event else "",
        )

    def fetch_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch orderbook for a single token from CLOB API."""
        data = self._get(f"{CLOB_API}/book", params={"token_id": token_id})
        if not data:
            return None
        return data

    def scan(self, fetch_books: bool = True, min_volume: float = 1000, min_markets: int = 2) -> dict:
        """
        Full scan: fetch events (with nested markets), optionally fetch orderbooks.
        Returns a summary dict.
        
        Args:
            fetch_books: If True, fetch orderbooks for all markets (slower but complete)
            min_volume: Skip markets below this volume threshold
            min_markets: For arbitrage detection, only keep events with >= this many markets
        """
        start = time.time()
        self.events.clear()
        self.markets.clear()

        # Fetch events — Gamma API returns nested markets in each event
        raw_events = self.fetch_active_events(limit=1000)
        
        for evt_data in raw_events:
            evt = Event(
                id=str(evt_data.get("id", "")),
                title=evt_data.get("title", ""),
                slug=evt_data.get("slug", ""),
                volume=float(evt_data.get("volume", 0) or 0),
                neg_risk=bool(evt_data.get("negRisk", False)),
            )
            
            # Parse nested markets
            for m_data in evt_data.get("markets", []):
                m = self.build_market_from_gamma(m_data, evt_data)
                if not m.condition_id or m.closed:
                    continue
                if m.volume < min_volume:
                    continue
                
                evt.markets.append(m)
                self.markets[m.condition_id] = m
            
            self.events[evt.id] = evt

        # Filter events: only keep ones with enough markets for arbitrage
        arb_events = {
            eid: evt for eid, evt in self.events.items()
            if len(evt.markets) >= min_markets
        }

        # Fetch orderbooks in parallel
        if fetch_books:
            self._fetch_all_orderbooks()

        self.last_scan_time = time.time()
        elapsed = self.last_scan_time - start

        summary = {
            "events_total": len(self.events),
            "events_with_arb_potential": len(arb_events),
            "markets_total": len(self.markets),
            "markets_with_books": sum(1 for m in self.markets.values() if m.book is not None),
            "scan_time_seconds": round(elapsed, 2),
        }
        logger.info(f"Scan complete: {summary}")
        return summary

    def _fetch_all_orderbooks(self):
        """Fetch orderbooks for all markets' Yes tokens in parallel."""
        # Collect all Yes token IDs (index 0)
        token_to_market = {}
        for m in self.markets.values():
            if m.tokens and len(m.tokens) > 0:
                token_to_market[m.tokens[0]["token_id"]] = m

        logger.info(f"Fetching {len(token_to_market)} orderbooks...")
        completed = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.fetch_orderbook, tid): tid
                for tid in token_to_market
            }
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    book = future.result()
                    if book:
                        token_to_market[tid].book = book
                        completed += 1
                except Exception as e:
                    logger.debug(f"Orderbook fetch failed for {tid[:20]}...: {e}")

        logger.info(f"Fetched {completed}/{len(token_to_market)} orderbooks")

    def get_multi_outcome_events(self) -> list[Event]:
        """Return events with 3+ markets — best arbitrage candidates."""
        return [evt for evt in self.events.values() if len(evt.markets) >= 3]

    def get_market(self, condition_id: str) -> Optional[Market]:
        return self.markets.get(condition_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    scanner = MarketScanner()
    summary = scanner.scan(fetch_books=True, min_volume=500, min_markets=2)
    print(json.dumps(summary, indent=2))
    
    # Show top multi-outcome events
    multi = scanner.get_multi_outcome_events()
    print(f"\n=== Top {min(10, len(multi))} Multi-Outcome Events (arb candidates) ===")
    multi.sort(key=lambda e: e.volume, reverse=True)
    for evt in multi[:10]:
        print(f"\n  {evt.title}")
        print(f"  Volume: ${evt.volume:,.0f}  |  Markets: {len(evt.markets)}")
        prices = []
        for m in evt.markets:
            if m.outcome_prices:
                prices.append(m.outcome_prices[0])
        if prices:
            total = sum(prices)
            print(f"  Sum of Yes prices: {total:.4f} ({total*100:.1f}%)")
            if abs(total - 1.0) > 0.05:
                print(f"  ⚠️  MISPRICING DETECTED: {'OVERPRICED' if total > 1.0 else 'UNDERPRICED'} by {abs(total - 1.0)*100:.1f}%")
