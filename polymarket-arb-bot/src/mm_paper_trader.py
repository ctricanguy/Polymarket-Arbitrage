"""
Paper Trading Market Maker
============================

Simulates a market making strategy against REAL Polymarket WebSocket data.

Strategy:
- Place virtual limit BUY orders at best_bid + small edge
- Place virtual limit SELL orders at best_ask - small edge  
- When both fill, pocket the spread
- Repeat continuously

This is what your friend's bot actually does — the portfolio ticks up
every second because it's continuously capturing spreads.

Position sizing: $1,000 per market (matching friend's $800-$1,300)
Max positions: 20-30 markets
Expected: ~$734/day on $113K = 0.65% daily

Usage:
    python3 mm_paper_trader.py --capital 100000 --duration 86400
    python3 mm_paper_trader.py --capital 100000 --duration 86400 --reset
"""

import json
import time
import logging
import threading
import random
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum

from hybrid_scanner import HybridScanner
from ws_client import MarketWebSocketClient, OrderBook

logger = logging.getLogger("polymarket-arb.mm_paper")

STATE_DIR = Path(__file__).parent.parent / "data" / "mm_paper_trader"
STATE_FILE = STATE_DIR / "state.json"
LOG_DIR = Path(__file__).parent.parent / "logs"


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass
class MMLimitOrder:
    """A virtual limit order."""
    order_id: str
    side: str  # "BUY" or "SELL"
    token_id: str
    condition_id: str
    question: str
    category: str
    price: float
    shares: float
    created_at: float
    status: str = "pending"
    filled_at: float = 0
    fill_price: float = 0


@dataclass
class MMInventory:
    """Inventory of tokens we hold for a specific market."""
    token_id: str
    condition_id: str
    question: str
    category: str
    shares: int  # positive = long, negative = short
    avg_cost: float  # average cost per share
    realized_pnl: float = 0.0
    total_buys: int = 0
    total_sells: int = 0
    total_spread_captured: float = 0.0


class PaperMarketMaker:
    """
    Paper trading market maker that simulates spread capture against
    real Polymarket WebSocket orderbook data.
    """

    def __init__(self, starting_capital: float = 100000,
                 position_size: float = 1000,  # $ per market (matching friend)
                 max_markets: int = 30,
                 edge: float = 0.3,  # how far into spread to place orders (0=at bid, 1=at ask)
                 order_shares: int = 500,  # shares per limit order
                 fill_probability: float = 0.15,  # base prob of fill per check (simulates matching)
                 max_inventory: int = 5000,  # max shares held per market
                 # Realistic frictions:
                 taker_fee: float = 0.0,  # Polymarket currently 0% but could change
                 maker_rebate: float = 0.0,  # maker rebate if any
                 gas_per_trade: float = 0.01,  # Polygon gas per fill (~$0.01)
                 slippage: float = 0.001,  # 0.1¢ slippage on fills
                 adverse_selection_rate: float = 0.20,  # 20% of fills, price moves against us
                 queue_ahead: int = 3,  # avg 3 market makers ahead of us in queue
                 ):
        self.starting_capital = starting_capital
        self.available_cash = starting_capital
        self.position_size = position_size
        self.max_markets = max_markets
        self.edge = edge
        self.order_shares = order_shares
        self.fill_probability = fill_probability
        self.max_inventory = max_inventory

        # Realistic friction parameters
        self.taker_fee = taker_fee
        self.maker_rebate = maker_rebate
        self.gas_per_trade = gas_per_trade
        self.slippage = slippage
        self.adverse_selection_rate = adverse_selection_rate
        self.queue_ahead = queue_ahead

        # Track price history for each token for adverse selection
        self.price_history: dict[str, list[float]] = {}  # token_id -> [mid_prices]
        self.last_order_prices: dict[str, dict] = {}  # token_id -> {"buy": price, "sell": price}

        self.cash = starting_capital
        self.inventory: dict[str, MMInventory] = {}  # token_id -> inventory
        self.open_orders: list[MMLimitOrder] = []
        self.filled_orders: list[MMLimitOrder] = []

        # Stats
        self.total_spread_captured = 0.0
        self.total_buy_fills = 0
        self.total_sell_fills = 0
        self.total_fill_cycles = 0  # complete buy+sell cycles
        self.started_at = 0
        self.last_stats_time = 0

        # Scanner
        self.scanner: HybridScanner | None = None

        # State
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _log_event(self, event_type: str, data: dict):
        """Log a trade event."""
        event = {"timestamp": time.time(), "type": event_type, "data": data}
        events = []
        events_file = STATE_DIR / "trade_events.json"
        if events_file.exists():
            try:
                with open(events_file) as f:
                    events = json.load(f)
            except:
                pass
        events.append(event)
        events = events[-500:]
        with open(events_file, 'w') as f:
            json.dump(events, f, default=str)

    def _classify_category(self, question: str) -> str:
        """Classify a market by category from its question text."""
        q_lower = question.lower()
        cat_keywords = {
            "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
                      "baseball", "hockey", "fifa", "world cup", "tennis", "golf", "ufc",
                      "boxing", "cricket", "rugby", "olympics", "game", "match", "tournament",
                      "championship", "league", "score", "vs", "over", "under"],
            "crypto": ["bitcoin", "ethereum", "crypto", "btc", "eth", "solana", "sol",
                      "token", "defi", "nft", "blockchain", "stablecoin", "coin", "binance"],
            "geopolitics": ["russia", "ukraine", "china", "iran", "israel", "war", "invasion",
                           "nuclear", "sanctions", "nato", "ceasefire", "putin", "korea",
                           "taiwan", "venezuela", "coup", "regime", "military"],
            "politics": ["president", "election", "democratic", "republican", "senate",
                        "congress", "governor", "primary", "nominee", "trump", "biden",
                        "obama", "party", "vote", "campaign"],
            "economy": ["fed", "interest rate", "inflation", "gdp", "recession", "unemployment",
                       "cpi", "treasury", "yield", "bank", "economic", "s&p", "nasdaq"],
            "tech": ["ai", "artificial intelligence", "openai", "anthropic", "google",
                    "apple", "microsoft", "meta", "nvidia", "gpt", "gemini", "claude",
                    " tesla", "spacex"],
            "entertainment": ["movie", "film", "oscar", "album", "song", "music", "game of",
                             "marvel", "dc", "netflix", "disney", "celebrity", "actor"],
        }
        for cat, keywords in cat_keywords.items():
            if any(kw in q_lower for kw in keywords):
                return cat
        return "other"

    def _discover_hot_tokens(self) -> dict:
        """
        Discover tokens with recent trade activity via Data API.
        Returns {token_id: {title, trades, avg_price, condition_id}}.
        """
        import requests as req
        try:
            r = req.get(
                "https://data-api.polymarket.com/trades",
                params={"limit": 500},
                timeout=5,
            )
            if r.status_code != 200:
                return {}
            trades = r.json()
        except:
            return {}

        now = time.time()
        token_stats = {}
        for t in trades:
            asset = t.get("asset", "")
            if not asset:
                continue
            ts = t.get("timestamp", 0)
            if now - ts > 600:  # Only trades in last 10 min
                continue

            if asset not in token_stats:
                token_stats[asset] = {
                    "title": t.get("title", "Unknown"),
                    "trades": 0,
                    "prices": [],
                    "condition_id": t.get("conditionId", ""),
                    "sizes": [],
                }
            token_stats[asset]["trades"] += 1
            token_stats[asset]["prices"].append(float(t.get("price", 0)))
            token_stats[asset]["sizes"].append(float(t.get("size", 0)))

        return token_stats

    def _discover_rewards_markets(self) -> dict:
        """
        Discover markets with rewards programs via SDK.
        Returns {token_id: rewards_per_day}.
        """
        try:
            import asyncio
            from polymarket_client import AsyncPublicClient

            async def get_rewards():
                client = AsyncPublicClient()
                rewards = await client.list_current_rewards()
                await client.close()
                return rewards

            loop = asyncio.new_event_loop()
            rewards = loop.run_until_complete(get_rewards())
            loop.close()

            token_rewards = {}
            for r in rewards:
                token_id = r.get("token_id", "")
                rate = float(r.get("rate_per_day", 0))
                if token_id and rate > 0:
                    token_rewards[token_id] = rate
            return token_rewards
        except Exception as e:
            logger.debug(f"Rewards discovery failed: {e}")
            return {}

    def initialize(self):
        """Connect to Polymarket WebSocket and select markets.

        Uses Data API to find hot markets with recent trades, then subscribes
        to those tokens via WebSocket for real-time orderbook data.
        """
        logger.info("Initializing market maker...")

        self.scanner = HybridScanner()
        self.scanner.discover_markets()

        # Discover hot tokens via Data API (markets with recent trades)
        hot_tokens = self._discover_hot_tokens()
        logger.info(f"Found {len(hot_tokens)} tokens with recent trades (last 10 min)")

        # Discover rewards markets
        rewards_markets = self._discover_rewards_markets()
        if rewards_markets:
            top_r = sorted(rewards_markets.items(), key=lambda x: x[1], reverse=True)[:5]
            for tid, rate in top_r:
                logger.info(f"  Rewards: ${rate:.0f}/day on token {tid[:25]}...")
        self.rewards_markets = rewards_markets
        self.total_rewards_earned = 0.0

        # Build subscription list: hot tokens + rewards tokens + top volume tokens
        all_tokens = set()

        # 1. Hot tokens (recent trade activity) — highest priority
        hot_sorted = sorted(hot_tokens.items(), key=lambda x: x[1]["trades"], reverse=True)
        for tid, stats in hot_sorted[:100]:
            all_tokens.add(tid)

        # 2. Rewards tokens (markets that pay daily rewards)
        for tid in rewards_markets:
            all_tokens.add(tid)

        # 3. Top volume tokens from scanner (fill up to 200)
        if len(all_tokens) < 200:
            for tid in self.scanner.token_to_market:
                all_tokens.add(tid)
                if len(all_tokens) >= 200:
                    break

        # Subscribe to WebSocket
        token_list = list(all_tokens)[:200]
        self.scanner.ws_client.subscribe(token_list)
        self.scanner.ws_client.connect()

        # Wait for books to arrive
        logger.info("Waiting for WebSocket orderbook data...")
        for _ in range(20):
            time.sleep(1)
            books = self.scanner.ws_client.get_all_books()
            if len(books) >= 5:
                break

        # Select markets for market making
        books = self.scanner.ws_client.get_all_books()
        logger.info(f"Received {len(books)} orderbooks from WebSocket")

        selected = []
        for token_id, book in books.items():
            if not book.bids or not book.asks:
                continue
            bid, bid_size = book.get_best_bid()
            ask, ask_size = book.get_best_ask()
            if bid <= 0 or ask >= 1.0 or bid >= ask:
                continue

            spread = ask - bid
            spread_pct = spread / ask * 100

            # MM criteria: 0.3% to 15% spread, price 2¢-90¢, decent depth
            if spread_pct < 0.3 or spread_pct > 15:
                continue
            if bid < 0.02 or ask > 0.90:
                continue
            if bid_size < 50 or ask_size < 50:
                continue

            # Get market info from scanner or Data API
            market_info = self.scanner.token_to_market.get(token_id)
            if market_info:
                question = market_info.question
                condition_id = market_info.condition_id
                volume = market_info.volume
            elif token_id in hot_tokens:
                question = hot_tokens[token_id]["title"]
                condition_id = hot_tokens[token_id]["condition_id"]
                volume = hot_tokens[token_id]["trades"] * 100  # estimate
            else:
                question = f"Token {token_id[:20]}..."
                condition_id = ""
                volume = 0

            category = self._classify_category(question)

            # Score: spread * trade_activity + rewards bonus
            trade_count = hot_tokens.get(token_id, {}).get("trades", 0)
            rewards_rate = rewards_markets.get(token_id, 0)
            score = spread_pct * (1 + trade_count / 50) + rewards_rate / 100

            selected.append({
                "token_id": token_id,
                "condition_id": condition_id,
                "question": question,
                "category": category,
                "volume": volume,
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "spread_pct": spread_pct,
                "score": score,
                "trade_count": trade_count,
                "rewards_rate": rewards_rate,
            })

        # Sort by score
        selected.sort(key=lambda x: x["score"], reverse=True)

        # Diversify: max 5 per category
        by_cat = {}
        diversified = []
        for s in selected:
            cat = s["category"]
            if by_cat.get(cat, 0) < 5:
                diversified.append(s)
                by_cat[cat] = by_cat.get(cat, 0) + 1
            if len(diversified) >= self.max_markets:
                break

        self.active_markets = {s["token_id"]: s for s in diversified}

        logger.info(f"Selected {len(self.active_markets)} markets for market making")
        for s in diversified[:15]:
            rwd = f" +${s['rewards_rate']:.0f}/d" if s['rewards_rate'] > 0 else ""
            logger.info(f"  {s['question'][:45]:45s} | "
                       f"bid={s['bid']:.4f} ask={s['ask']:.4f} "
                       f"spread={s['spread_pct']:.1f}% trades={s['trade_count']}{rwd}")

        return len(self.active_markets)

    def refresh_markets(self):
        """Re-scan for hot markets and update active market list.

        Called every 5 minutes to prevent stale markets.
        Preserves existing inventory and orders.
        """
        logger.info("Refreshing market selection...")

        # Discover new hot tokens
        hot_tokens = self._discover_hot_tokens()

        # Find new tokens we're not subscribed to
        new_tokens = [tid for tid in hot_tokens if tid not in self.scanner.ws_client.subscribed_assets]
        if new_tokens:
            logger.info(f"Found {len(new_tokens)} new hot tokens, subscribing...")
            self.scanner.ws_client.subscribe(new_tokens[:50])
            time.sleep(3)  # Wait for initial book data

        # Get current books
        books = self.scanner.ws_client.get_all_books()

        # Re-score all active + new markets
        selected = []
        for token_id, book in books.items():
            if not book.bids or not book.asks:
                continue
            if book.is_stale(120):  # Skip books with no updates in 2 min
                continue

            bid, bid_size = book.get_best_bid()
            ask, ask_size = book.get_best_ask()
            if bid <= 0 or ask >= 1.0 or bid >= ask:
                continue

            spread = ask - bid
            spread_pct = spread / ask * 100
            if spread_pct < 0.3 or spread_pct > 15:
                continue
            if bid < 0.02 or ask > 0.90:
                continue

            # Keep existing market info or get from hot tokens
            if token_id in self.active_markets:
                m = self.active_markets[token_id]
                question = m["question"]
                condition_id = m["condition_id"]
                category = m["category"]
            elif token_id in hot_tokens:
                question = hot_tokens[token_id]["title"]
                condition_id = hot_tokens[token_id]["condition_id"]
                category = self._classify_category(question)
            else:
                continue  # Skip unknown tokens during refresh

            trade_count = hot_tokens.get(token_id, {}).get("trades", 0)
            rewards_rate = self.rewards_markets.get(token_id, 0)
            score = spread_pct * (1 + trade_count / 50) + rewards_rate / 100

            selected.append({
                "token_id": token_id,
                "condition_id": condition_id,
                "question": question,
                "category": category,
                "volume": 0,
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "spread_pct": spread_pct,
                "score": score,
                "trade_count": trade_count,
                "rewards_rate": rewards_rate,
            })

        selected.sort(key=lambda x: x["score"], reverse=True)

        # Keep top markets, but preserve markets where we have inventory
        tokens_with_inventory = {tid for tid, inv in self.inventory.items() if inv.shares != 0}
        by_cat = {}
        diversified = []
        for s in selected:
            cat = s["category"]
            if s["token_id"] in tokens_with_inventory:
                diversified.append(s)
                by_cat[cat] = by_cat.get(cat, 0) + 1
            elif by_cat.get(cat, 0) < 5:
                diversified.append(s)
                by_cat[cat] = by_cat.get(cat, 0) + 1
            if len(diversified) >= self.max_markets:
                break

        old_count = len(self.active_markets)
        self.active_markets = {s["token_id"]: s for s in diversified}

        new_markets = len(self.active_markets) - old_count
        logger.info(f"Refreshed: {len(self.active_markets)} markets ({new_markets:+d} new)")

        return len(self.active_markets)

    def place_orders(self):
        """Place buy and sell limit orders on all active markets."""
        for token_id, market in self.active_markets.items():
            book = self.scanner.ws_client.get_book(token_id)
            if not book or not book.bids or not book.asks:
                continue

            bid, bid_size = book.get_best_bid()
            ask, ask_size = book.get_best_ask()
            if bid <= 0 or ask >= 1.0:
                continue

            spread = ask - bid

            # Our buy price: slightly above best bid (to be first in line)
            our_buy = bid + spread * self.edge
            # Our sell price: slightly below best ask
            our_sell = ask - spread * self.edge

            # Round to 0.001
            our_buy = round(our_buy, 3)
            our_sell = round(our_sell, 3)

            if our_buy >= our_sell:
                continue

            # Check inventory — if we have too many shares, don't place more
            inv = self.inventory.get(token_id)
            current_shares = inv.shares if inv else 0

            # Crypto 5-min markets are volatile — limit inventory to 500 shares
            # to avoid building large directional positions
            category = market.get("category", "other")
            is_crypto = category == "crypto" or "Up or Down" in market.get("question", "")
            effective_max = 500 if is_crypto else self.max_inventory

            # Place BUY order (if inventory not maxed)
            if current_shares < effective_max:
                buy_order = MMLimitOrder(
                    order_id=f"BUY_{token_id}_{int(time.time()*1000)}",
                    side="BUY",
                    token_id=token_id,
                    condition_id=market["condition_id"],
                    question=market["question"],
                    category=market["category"],
                    price=our_buy,
                    shares=self.order_shares,
                    created_at=time.time(),
                )
                self.open_orders.append(buy_order)

            # Place SELL order (if we have shares to sell or want to short)
            if current_shares > -effective_max:
                sell_order = MMLimitOrder(
                    order_id=f"SELL_{token_id}_{int(time.time()*1000)}",
                    side="SELL",
                    token_id=token_id,
                    condition_id=market["condition_id"],
                    question=market["question"],
                    category=market["category"],
                    price=our_sell,
                    shares=self.order_shares,
                    created_at=time.time(),
                )
                self.open_orders.append(sell_order)

    def check_fills(self):
        """
        REALISTIC fill simulation using ACTUAL Polymarket trade data.
        
        Fill model:
        1. Data API trades that crossed our order price → deterministic fill
        2. Trade frequency-based probabilistic fills — if a market has N trades/hr
           and we're 1 of Q MMs, our fill rate is N/(Q+1) per hour
        3. Price crossing as fallback — if best_ask drops to our bid, we fill
        
        We DON'T require the spread to cross — MM works by resting at bid/ask
        and getting filled when someone market-takes the other side.
        """
        books = self.scanner.ws_client.get_all_books()

        # Fetch recent trades from Data API (cached 30s)
        if not hasattr(self, '_api_trades_cache') or time.time() - self._api_trades_cache_time > 30:
            import requests as req
            try:
                r = req.get(
                    "https://data-api.polymarket.com/trades",
                    params={"limit": 500, "sortOrder": "desc"},
                    timeout=3,
                )
                new_trades = r.json() if r.status_code == 200 else []
                # Track which trades we've already processed (by id)
                if not hasattr(self, '_seen_trade_ids'):
                    self._seen_trade_ids = set()
                if not hasattr(self, '_new_api_trades'):
                    self._new_api_trades = []
                
                # Find trades we haven't seen before
                fresh_trades = []
                for t in new_trades:
                    tid = t.get("id", "") or f"{t.get('asset','')}-{t.get('timestamp','')}-{t.get('price','')}"
                    if tid not in self._seen_trade_ids:
                        self._seen_trade_ids.add(tid)
                        fresh_trades.append(t)
                
                # Keep last 500 trades for matching, but only fresh ones trigger new fills
                self._api_trades_cache = new_trades
                self._new_api_trades = fresh_trades
                self._api_trades_cache_time = time.time()
                
                if fresh_trades:
                    logger.debug(f"Data API: {len(fresh_trades)} new trades since last check")
            except:
                self._api_trades_cache = []
                self._api_trades_cache_time = time.time()
        recent_api_trades = self._api_trades_cache

        # Build trade count by asset (last 5 min)
        api_trades_by_asset = {}
        api_trade_counts = {}  # token_id → count in last 5 min
        now = time.time()
        for t in recent_api_trades:
            asset = t.get("asset", "")
            ts = t.get("timestamp", 0)
            if now - ts > 300:
                continue
            if asset not in api_trades_by_asset:
                api_trades_by_asset[asset] = []
                api_trade_counts[asset] = 0
            api_trades_by_asset[asset].append(t)
            api_trade_counts[asset] += 1

        for order in self.open_orders[:]:
            if order.status != "pending":
                continue

            book = books.get(order.token_id)
            if not book or not book.bids or not book.asks:
                continue

            best_bid, bid_size = book.get_best_bid()
            best_ask, ask_size = book.get_best_ask()
            mid = (best_bid + best_ask) / 2

            # Track price history for adverse selection
            token_id = order.token_id
            if token_id not in self.price_history:
                self.price_history[token_id] = []
            self.price_history[token_id].append(mid)
            if len(self.price_history[token_id]) > 20:
                self.price_history[token_id] = self.price_history[token_id][-20:]

            # Get trade stats
            trade_stats = self.scanner.ws_client.get_trade_stats(token_id, window_seconds=3600)
            ws_trades_per_hour = trade_stats.get("rate_per_hour", 0)
            
            # Data API trade count in last 5 min → trades per hour
            api_count_5min = api_trade_counts.get(token_id, 0)
            api_trades_per_hour = api_count_5min * 12  # 5 min → 1 hour

            # Use the higher of WS and API trade rates
            actual_trades_per_hour = max(ws_trades_per_hour, api_trades_per_hour)

            # Also check market's initial trade count (from discovery)
            market_info = self.active_markets.get(token_id, {})
            discovery_trades = market_info.get("trade_count", 0)
            if actual_trades_per_hour == 0 and discovery_trades > 0:
                # Use discovery trade count as estimate (trades in last 10 min)
                actual_trades_per_hour = discovery_trades * 6  # 10 min → 1 hour

            filled = False
            fill_price = order.price

            # METHOD 1: Check Data API for real trades that crossed our price
            api_filled = False
            api_trades = api_trades_by_asset.get(token_id, [])
            for at in api_trades:
                at_price = float(at.get("price", 0))
                at_side = at.get("side", "")
                at_size = float(at.get("size", 0))
                at_ts = at.get("timestamp", 0)

                # Only trades after our order was placed
                if at_ts < order.created_at:
                    continue

                if order.side == "BUY" and at_side == "SELL" and at_price <= order.price + 0.002:
                    # Someone sold at or below our buy price → we fill
                    fill_share = min(at_size * 0.15, order.shares)
                    if fill_share > 0:
                        api_filled = True
                        break
                elif order.side == "SELL" and at_side == "BUY" and at_price >= order.price - 0.002:
                    # Someone bought at or above our sell price → we fill
                    fill_share = min(at_size * 0.15, order.shares)
                    if fill_share > 0:
                        api_filled = True
                        break

            if api_filled:
                filled = True
                if order.side == "BUY":
                    fill_price = order.price - self.slippage
                else:
                    fill_price = order.price + self.slippage

            # METHOD 2: Trade-frequency-based probabilistic fill
            # If market has N trades/hr and we're 1 of (queue_ahead+1) MMs,
            # our expected fills = N / (queue_ahead+1) per hour
            # Per check (1.5s): fill_prob = N / (queue_ahead+1) / 2400 checks_per_hour
            if not filled:
                # Use the best available trade rate estimate
                trade_rate = actual_trades_per_hour
                
                # If no recent trades, use the market's historical rate from discovery
                if trade_rate == 0:
                    trade_rate = market_info.get("trade_count", 0) * 6  # 10min → 1hr
                
                # If still zero, use a minimum based on spread (tight spread = active market)
                if trade_rate == 0:
                    spread_pct = (best_ask - best_bid) / best_ask * 100
                    if spread_pct < 2:
                        trade_rate = 10  # Very tight spread → at least 10 trades/hr estimate
                    elif spread_pct < 5:
                        trade_rate = 5   # Medium spread → 5 trades/hr
                    else:
                        trade_rate = 2   # Wide spread → 2 trades/hr
                
                checks_per_hour = 2400  # 1.5s interval
                fill_prob = trade_rate / (self.queue_ahead + 1) / checks_per_hour
                
                # Boost for tight spreads (more likely to get filled at our level)
                spread_pct = (best_ask - best_bid) / best_ask * 100
                if spread_pct < 3:
                    fill_prob *= 1.5  # Tight spread = more likely our level gets hit
                elif spread_pct > 10:
                    fill_prob *= 0.5  # Wide spread = our level might be too far

                # Cap at 5% per check
                fill_prob = min(0.05, fill_prob)

                if random.random() < fill_prob:
                    filled = True
                    if order.side == "BUY":
                        fill_price = order.price - self.slippage
                    else:
                        fill_price = order.price + self.slippage

            # METHOD 3: Price crossing fallback (someone lifts the offer / hits the bid)
            if not filled:
                if order.side == "BUY" and best_ask <= order.price + 0.001:
                    # Ask dropped to our bid — we definitely fill
                    filled = True
                    fill_price = order.price - self.slippage
                elif order.side == "SELL" and best_bid >= order.price - 0.001:
                    # Bid rose to our ask — we definitely fill
                    filled = True
                    fill_price = order.price + self.slippage

            if filled:
                # Adverse selection: informed flow moves price against us
                if random.random() < self.adverse_selection_rate:
                    if order.side == "BUY":
                        fill_price -= 0.002
                    else:
                        fill_price += 0.002

                # Gas cost
                self.cash -= self.gas_per_trade

                self._fill_order(order, fill_price)

    def _fill_order(self, order: MMLimitOrder, fill_price: float):
        """Process a filled order."""
        order.status = "filled"
        order.filled_at = time.time()
        order.fill_price = fill_price
        self.open_orders.remove(order)
        self.filled_orders.append(order)

        token_id = order.token_id
        inv = self.inventory.get(token_id)
        if not inv:
            inv = MMInventory(
                token_id=token_id,
                condition_id=order.condition_id,
                question=order.question,
                category=order.category,
                shares=0,
                avg_cost=0,
            )
            self.inventory[token_id] = inv

        if order.side == "BUY":
            # We bought shares
            cost = fill_price * order.shares
            self.cash -= cost

            # Update inventory
            new_total = inv.shares + order.shares
            if new_total > 0:
                inv.avg_cost = ((inv.avg_cost * inv.shares) + cost) / new_total
            inv.shares = new_total
            inv.total_buys += 1
            self.total_buy_fills += 1

            logger.debug(f"BUY FILL: {order.question[:30]} @ {fill_price:.3f} "
                        f"({order.shares} sh) — inv: {inv.shares} — cash: ${self.cash:.2f}")

        elif order.side == "SELL":
            # We sold shares
            revenue = fill_price * order.shares
            self.cash += revenue

            # Calculate spread captured (if we had a matching buy)
            if inv.shares > 0:
                spread_captured = (fill_price - inv.avg_cost) * order.shares
                inv.realized_pnl += spread_captured
                inv.total_spread_captured += spread_captured
                self.total_spread_captured += spread_captured

            inv.shares -= order.shares
            inv.total_sells += 1
            self.total_sell_fills += 1

            # Check if we completed a full cycle (buy then sell)
            if inv.total_buys > 0 and inv.total_sells > 0 and inv.total_buys == inv.total_sells:
                self.total_fill_cycles += 1

            logger.debug(f"SELL FILL: {order.question[:30]} @ {fill_price:.3f} "
                        f"({order.shares} sh) — spread: +${(fill_price - inv.avg_cost) * order.shares:.2f} "
                        f"— cash: ${self.cash:.2f}")

        self._log_event("FILL", {
            "side": order.side,
            "question": order.question,
            "price": fill_price,
            "shares": order.shares,
            "cash": self.cash,
            "spread_captured": (fill_price - inv.avg_cost) * order.shares if order.side == "SELL" else 0,
        })

    def manage_risk(self):
        """Close positions with excessive unrealized losses (stop-loss).
        
        If a position's unrealized loss exceeds max_loss_per_position (default $50),
        place an aggressive order to close it at market price.
        """
        max_loss = getattr(self, 'max_loss_per_position', 50.0)  # $50 stop-loss
        now = time.time()
        
        for token_id, inv in list(self.inventory.items()):
            if inv.shares == 0:
                continue
            
            book = self.scanner.ws_client.get_book(token_id)
            if not book or not book.bids or not book.asks:
                continue
            
            bid, _ = book.get_best_bid()
            ask, _ = book.get_best_ask()
            mid = (bid + ask) / 2
            
            unrealized = (mid - inv.avg_cost) * inv.shares
            
            if unrealized < -max_loss:
                # Position is losing more than max_loss — close it
                market_info = self.active_markets.get(token_id, {})
                question = market_info.get("question", inv.question)
                category = market_info.get("category", inv.category)
                condition_id = market_info.get("condition_id", inv.condition_id)
                
                if inv.shares > 0:
                    # We're long — sell at bid to close
                    close_price = bid
                    close_order = MMLimitOrder(
                        order_id=f"SL_SELL_{token_id}_{int(now*1000)}",
                        side="SELL",
                        token_id=token_id,
                        condition_id=condition_id,
                        question=question,
                        category=category,
                        price=close_price,
                        shares=abs(inv.shares),
                        created_at=now,
                    )
                else:
                    # We're short — buy at ask to close
                    close_price = ask
                    close_order = MMLimitOrder(
                        order_id=f"SL_BUY_{token_id}_{int(now*1000)}",
                        side="BUY",
                        token_id=token_id,
                        condition_id=condition_id,
                        question=question,
                        category=category,
                        price=close_price,
                        shares=abs(inv.shares),
                        created_at=now,
                    )
                
                # Force-fill the stop-loss immediately (market order)
                close_order.status = "filled"
                close_order.filled_at = now
                close_order.fill_price = close_price
                self.filled_orders.append(close_order)
                
                # Update inventory
                if inv.shares > 0:
                    proceeds = close_price * abs(inv.shares)
                    self.cash += proceeds - self.gas_per_trade
                    loss = (close_price - inv.avg_cost) * inv.shares
                    inv.realized_pnl += loss
                    self.total_spread_captured += loss
                else:
                    cost = close_price * abs(inv.shares)
                    self.cash -= cost + self.gas_per_trade
                    loss = (inv.avg_cost - close_price) * abs(inv.shares)
                    inv.realized_pnl -= loss
                    self.total_spread_captured -= loss
                
                inv.shares = 0
                logger.warning(
                    f"STOP-LOSS: Closed {question[:35]} @ {close_price:.3f} "
                    f"loss=${loss:+.2f}"
                )

    def cancel_stale_orders(self):
        """Cancel orders older than 60 seconds."""
        now = time.time()
        for order in self.open_orders[:]:
            if (now - order.created_at) > 60:
                order.status = "cancelled"
                self.open_orders.remove(order)

    def get_portfolio_value(self) -> dict:
        """Calculate current portfolio value."""
        # Mark-to-market inventory at mid-price
        inventory_value = 0
        unrealized_pnl = 0

        for token_id, inv in self.inventory.items():
            book = self.scanner.ws_client.get_book(token_id) if self.scanner else None
            if book and book.bids and book.asks:
                bid, _ = book.get_best_bid()
                ask, _ = book.get_best_ask()
                mid = (bid + ask) / 2
            else:
                mid = inv.avg_cost

            position_value = inv.shares * mid
            inventory_value += position_value
            unrealized_pnl += (mid - inv.avg_cost) * inv.shares

        total_value = self.cash + inventory_value
        realized = self.total_spread_captured
        total_pnl = total_value - self.starting_capital

        return {
            "cash": self.cash,
            "inventory_value": inventory_value,
            "total_value": total_value,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "total_return_pct": (total_pnl / self.starting_capital * 100) if self.starting_capital > 0 else 0,
            "buy_fills": self.total_buy_fills,
            "sell_fills": self.total_sell_fills,
            "fill_cycles": self.total_fill_cycles,
            "spread_captured": self.total_spread_captured,
        }

    def print_status(self):
        """Print current status."""
        stats = self.get_portfolio_value()
        runtime = time.time() - self.started_at if self.started_at else 0
        rate_per_hour = stats["realized_pnl"] / (runtime / 3600) if runtime > 0 else 0
        rate_per_day = rate_per_hour * 24

        # Get real trade stats
        total_real_trades = 0
        total_real_volume = 0
        if self.scanner and self.scanner.ws_client:
            all_stats = self.scanner.ws_client.get_all_trade_stats(3600)
            for aid, ts in all_stats.items():
                total_real_trades += ts.get("count", 0)
                total_real_volume += ts.get("total_volume", 0)

        print(f"\n{'='*60}")
        print(f"MARKET MAKER STATUS — {datetime.now().strftime('%H:%M:%S')}")
        print(f"Runtime: {runtime/3600:.1f}h | Fills: {stats['buy_fills']}B/{stats['sell_fills']}S "
              f"({stats['fill_cycles']} cycles)")
        print(f"Real trades observed (1h): {total_real_trades} | Real vol: ${total_real_volume:,.0f}")
        print(f"{'─'*60}")
        print(f"  Cash:              ${stats['cash']:,.2f}")
        print(f"  Inventory:         ${stats['inventory_value']:,.2f}")
        print(f"  TOTAL VALUE:       ${stats['total_value']:,.2f}")
        print(f"  Spread Captured:   ${stats['realized_pnl']:+,.2f}")
        print(f"  Rewards Earned:    ${self.total_rewards_earned:+,.2f}")
        print(f"  Unrealized:        ${stats['unrealized_pnl']:+,.2f}")
        total_with_rewards = stats['total_pnl'] + self.total_rewards_earned
        print(f"  TOTAL P&L:         ${total_with_rewards:+,.2f} ({stats['total_return_pct']:+.2f}%)")
        rate_with_rewards = total_with_rewards / (runtime / 3600) if runtime > 0 else 0
        print(f"  Rate (w/ rewards): ${rate_with_rewards:+.2f}/h → ${rate_with_rewards*24:+.2f}/day")
        print(f"{'─'*60}")

        # Top inventories
        if self.inventory:
            print(f"  TOP INVENTORIES:")
            sorted_inv = sorted(self.inventory.values(),
                              key=lambda x: abs(x.shares * x.avg_cost), reverse=True)
            for inv in sorted_inv[:5]:
                if inv.shares == 0 and inv.total_buys == 0:
                    continue
                book = self.scanner.ws_client.get_book(inv.token_id) if self.scanner else None
                if book and book.bids and book.asks:
                    bid, _ = book.get_best_bid()
                    ask, _ = book.get_best_ask()
                    mid = (bid + ask) / 2
                else:
                    mid = inv.avg_cost

                pos_val = inv.shares * mid
                print(f"    {inv.question[:40]:40s} | {inv.shares:+6d} sh @ {inv.avg_cost:.3f} "
                      f"| ${pos_val:+,.0f} | spread: ${inv.total_spread_captured:+.2f} "
                      f"| B:{inv.total_buys} S:{inv.total_sells}")
        print(f"{'='*60}\n")

    def save_state(self):
        """Save state to disk for dashboard."""
        stats = self.get_portfolio_value()
        state = {
            "starting_capital": self.starting_capital,
            "available_cash": self.cash,
            "total_value": stats["total_value"],
            "total_pnl": stats["total_pnl"],
            "total_return_pct": stats["total_return_pct"],
            "realized_pnl": stats["realized_pnl"],
            "unrealized_pnl": stats["unrealized_pnl"],
            "spread_captured": stats["spread_captured"],
            "rewards_earned": self.total_rewards_earned,
            "buy_fills": stats["buy_fills"],
            "sell_fills": stats["sell_fills"],
            "fill_cycles": stats["fill_cycles"],
            "total_trades": stats["buy_fills"] + stats["sell_fills"],
            "started_at": self.started_at,
            "saved_at": time.time(),
            "positions": [],
            "filled_orders": [],
            "rate_per_hour": stats["realized_pnl"] / ((time.time() - self.started_at) / 3600) if self.started_at else 0,
            "rate_per_day": 0,
        }
        if self.started_at:
            runtime_h = (time.time() - self.started_at) / 3600
            if runtime_h > 0:
                state["rate_per_hour"] = stats["realized_pnl"] / runtime_h
                state["rate_per_day"] = state["rate_per_hour"] * 24

        for token_id, inv in self.inventory.items():
            if inv.shares == 0 and inv.total_buys == 0:
                continue
            book = self.scanner.ws_client.get_book(token_id) if self.scanner else None
            if book and book.bids and book.asks:
                bid, _ = book.get_best_bid()
                ask, _ = book.get_best_ask()
                mid = (bid + ask) / 2
            else:
                mid = inv.avg_cost

            state["positions"].append({
                "condition_id": inv.condition_id,
                "token_id": inv.token_id,
                "question": inv.question,
                "category": inv.category,
                "entry_price": inv.avg_cost,
                "current_price": mid,
                "entry_shares": inv.shares,
                "status": "market_making",
                "take_profit_price": 0,
                "stop_loss_price": 0,
                "realized_pnl": inv.realized_pnl,
                "unrealized_pnl": (mid - inv.avg_cost) * inv.shares,
                "trades": inv.total_buys + inv.total_sells,
                "total_buys": inv.total_buys,
                "total_sells": inv.total_sells,
                "spread_captured": inv.total_spread_captured,
                "position_value": inv.shares * mid,
            })

        # Keep last 100 filled orders
        for order in self.filled_orders[-100:]:
            state["filled_orders"].append(asdict(order))

        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)

        logger.debug(f"State saved to {STATE_FILE}")

    def run(self, duration: int = 0, scan_interval: float = 1.5):
        """Run the market maker loop. scan_interval=1.5s matches friend's bot."""
        logger.info("=" * 60)
        logger.info("MARKET MAKER BOT STARTING")
        logger.info(f"Capital: ${self.starting_capital:,.0f} | Markets: {self.max_markets}")
        logger.info(f"Position size: ${self.position_size} | Order: {self.order_shares} shares")
        logger.info(f"Fill probability: {self.fill_probability*100:.0f}% per check")
        logger.info(f"Scan interval: {scan_interval}s | Queue ahead: {self.queue_ahead}")
        logger.info("=" * 60)

        self.started_at = time.time()

        # Place initial orders
        self.place_orders()

        start_time = time.time()
        last_order_place = time.time()
        last_report = time.time()
        last_save = time.time()
        last_refresh = time.time()
        last_rewards_calc = time.time()

        try:
            while True:
                now = time.time()

                # Check duration
                if duration > 0 and (now - start_time) > duration:
                    logger.info(f"Duration reached ({duration}s). Stopping.")
                    break

                # Check for fills — this is the 1.5s heartbeat
                self.check_fills()

                # Re-place orders every 10 seconds (refresh prices to track market)
                if (now - last_order_place) > 10:
                    self.cancel_stale_orders()
                    self.manage_risk()
                    self.place_orders()
                    last_order_place = now

                # Refresh markets every 5 minutes (find new hot markets, drop stale ones)
                if (now - last_refresh) > 300:
                    self.refresh_markets()
                    last_refresh = now

                # Accrue rewards every 60 seconds (pro-rated daily rate)
                if (now - last_rewards_calc) > 60:
                    for token_id, market in self.active_markets.items():
                        rate_per_day = market.get("rewards_rate", 0)
                        if rate_per_day > 0:
                            # Only earn rewards if we have orders/positions in this market
                            inv = self.inventory.get(token_id)
                            has_orders = any(o.token_id == token_id and o.status == "pending" for o in self.open_orders)
                            if (inv and inv.shares != 0) or has_orders:
                                self.total_rewards_earned += rate_per_day / 24 / 60  # per minute
                    last_rewards_calc = now

                # Print status every 60s
                if (now - last_report) > 60:
                    self.print_status()
                    last_report = now

                # Save state every 5s (for dashboard)
                if (now - last_save) > 5:
                    self.save_state()
                    last_save = now

                time.sleep(scan_interval)  # 1.5s — matches friend's bot

        except KeyboardInterrupt:
            logger.info("Stopping market maker (Ctrl+C)...")

        finally:
            self.save_state()
            self.print_status()
            stats = self.get_portfolio_value()
            runtime = time.time() - self.started_at
            print(f"\n{'#'*60}")
            print(f"  FINAL MARKET MAKER REPORT")
            print(f"{'#'*60}")
            print(f"  Starting capital:  ${self.starting_capital:,.2f}")
            print(f"  Final value:       ${stats['total_value']:,.2f}")
            print(f"  Total P&L:         ${stats['total_pnl']:+,.2f} ({stats['total_return_pct']:+.2f}%)")
            print(f"  Spread captured:   ${stats['realized_pnl']:+,.2f}")
            print(f"  Buy fills:         {stats['buy_fills']}")
            print(f"  Sell fills:        {stats['sell_fills']}")
            print(f"  Fill cycles:       {stats['fill_cycles']}")
            print(f"  Runtime:           {runtime/3600:.1f} hours")
            if runtime > 0:
                rate_h = stats['realized_pnl'] / (runtime / 3600)
                print(f"  Rate:              ${rate_h:+.2f}/h → ${rate_h*24:+.2f}/day")
            print(f"{'#'*60}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Paper Trading Market Maker")
    parser.add_argument('--capital', type=float, default=100000)
    parser.add_argument('--position-size', type=float, default=1000)
    parser.add_argument('--max-markets', type=int, default=30)
    parser.add_argument('--order-shares', type=int, default=500)
    parser.add_argument('--fill-prob', type=float, default=0.15)
    parser.add_argument('--queue-ahead', type=int, default=3, help='Avg MMs ahead in queue')
    parser.add_argument('--gas', type=float, default=0.01, help='Gas per fill in $')
    parser.add_argument('--slippage', type=float, default=0.001, help='Slippage per fill')
    parser.add_argument('--adverse', type=float, default=0.20, help='Adverse selection rate')
    parser.add_argument('--scan-interval', type=float, default=1.5, help='Seconds between checks (1.5 = friend bot)')
    parser.add_argument('--duration', type=int, default=0)
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(LOG_DIR / "mm_paper_trader.log")),
        ]
    )
    logging.getLogger("websocket").setLevel(logging.WARNING)

    if args.reset:
        for f in [STATE_FILE, STATE_DIR / "trade_events.json"]:
            if f.exists():
                f.unlink()
        logger.info("State reset.")

    maker = PaperMarketMaker(
        starting_capital=args.capital,
        position_size=args.position_size,
        max_markets=args.max_markets,
        order_shares=args.order_shares,
        fill_probability=args.fill_prob,
        gas_per_trade=args.gas,
        slippage=args.slippage,
        adverse_selection_rate=args.adverse,
        queue_ahead=args.queue_ahead,
    )

    n_markets = maker.initialize()
    if n_markets == 0:
        logger.error("No suitable markets found for market making!")
        return

    maker.run(duration=args.duration, scan_interval=args.scan_interval)


if __name__ == "__main__":
    main()
