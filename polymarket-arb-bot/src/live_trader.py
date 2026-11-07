"""
Live Trading Client for Polymarket
====================================

Uses the official polymarket-client Python SDK for:
- Real order placement (limit orders at bid/ask)
- Order cancellation
- Position management
- Portfolio monitoring
- Rewards program integration

Architecture:
  - LiveTrader handles all authenticated operations
  - Market data comes from AsyncPublicClient (public, no geoblock)
  - Order placement uses AsyncSecureClient (needs non-US server for execution)
  - Falls back to paper mode when geoblocked

The relayer key + wallet address are used for L2 authentication.
"""
import asyncio
import json
import time
import logging
import os
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("live_trader")

# Polymarket SDK
from polymarket import (
    AsyncPublicClient,
    AsyncSecureClient,
    OrderSide,
    RelayerApiKey,
)


@dataclass
class MarketMakingOrder:
    """Represents a pair of limit orders (buy at bid, sell at ask) for one market."""
    token_id: str
    question: str
    condition_id: str
    buy_price: float
    sell_price: float
    size: int  # shares
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    buy_filled: float = 0
    sell_filled: float = 0
    placed_at: float = 0
    last_refresh: float = 0
    spread_captured: float = 0
    reward_earned: float = 0


@dataclass
class RewardsConfig:
    """Rewards program config for a market."""
    condition_id: str
    max_spread: float  # max spread in cents for rewards eligibility
    min_size: int  # minimum order size in shares
    rate_per_day: float  # daily reward rate in USD
    token_ids: list = field(default_factory=list)


class LiveTrader:
    """
    Live trading client using the official Polymarket SDK.
    
    Handles:
    - Market discovery and filtering
    - Rewards-aware market selection
    - Dual-sided limit order placement (buy at bid, sell at ask)
    - Order refresh and cancellation
    - Position monitoring
    - P&L tracking
    - Paper mode fallback when geoblocked
    """

    def __init__(
        self,
        private_key: str = None,
        wallet_address: str = None,
        relayer_api_key: str = None,
        relayer_key_address: str = None,
        paper_mode: bool = True,
        position_size: int = 500,
        max_positions: int = 20,
        capital: float = 100000,
        state_dir: str = "data/live_trader",
        scan_interval: float = 1.5,
        order_refresh_interval: float = 10.0,
        min_spread_cents: float = 0.01,
        max_spread_cents: float = 0.15,
    ):
        self.private_key = private_key
        self.wallet_address = wallet_address
        self.relayer_api_key = relayer_api_key
        self.relayer_key_address = relayer_key_address
        self.paper_mode = paper_mode
        self.position_size = position_size
        self.max_positions = max_positions
        self.capital = capital
        self.scan_interval = scan_interval
        self.order_refresh_interval = order_refresh_interval
        self.min_spread_cents = min_spread_cents
        self.max_spread_cents = max_spread_cents

        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # State
        self.orders: dict[str, MarketMakingOrder] = {}  # token_id -> MMOrder
        self.rewards: dict[str, RewardsConfig] = {}  # condition_id -> rewards
        self.cash = capital
        self.realized_pnl = 0
        self.positions: dict[str, dict] = {}  # token_id -> position info
        self.started_at = 0
        self.fill_count = 0
        self.cycle_count = 0

        # SDK clients (initialized in async context)
        self.public_client = None
        self.secure_client = None

    async def initialize(self):
        """Initialize SDK clients."""
        self.public_client = AsyncPublicClient()
        
        if not self.paper_mode and self.private_key:
            try:
                # Create secure client with relayer API key
                api_key = RelayerApiKey(
                    key=self.relayer_api_key,
                    address=self.relayer_key_address,
                ) if self.relayer_api_key else None
                
                self.secure_client = await AsyncSecureClient.create(
                    private_key=self.private_key,
                    wallet=self.wallet_address,
                    api_key=api_key,
                )
                
                # Set up trading approvals
                await self.secure_client.setup_trading_approvals()
                logger.info("Secure client initialized — LIVE MODE")
                self.paper_mode = False
            except Exception as e:
                logger.error(f"Failed to init secure client: {e}")
                logger.info("Falling back to paper mode")
                self.paper_mode = True
        else:
            logger.info("Running in PAPER MODE (no real orders)")

    async def close(self):
        """Clean up clients."""
        if self.public_client:
            await self.public_client.close()
        if self.secure_client:
            await self.secure_client.close()

    async def fetch_rewards_programs(self) -> dict[str, RewardsConfig]:
        """Fetch all active rewards programs from Polymarket."""
        rewards = {}
        try:
            stream = self.public_client.list_current_rewards()
            page = await stream.first_page()
            
            for r in page.items:
                config = RewardsConfig(
                    condition_id=r.condition_id,
                    max_spread=float(r.rewards_max_spread) / 100.0,  # convert from basis points
                    min_size=int(r.rewards_min_size),
                    rate_per_day=float(r.total_daily_rate),
                )
                rewards[r.condition_id] = config
                
            logger.info(f"Fetched {len(rewards)} rewards programs")
            
            # Also fetch market rewards for specific markets we're interested in
            # This maps condition_ids to token_ids
        except Exception as e:
            logger.error(f"Error fetching rewards: {e}")
        
        self.rewards = rewards
        return rewards

    async def discover_markets(
        self,
        min_volume: float = 50000,
        max_markets: int = 200,
        prefer_rewarded: bool = True,
    ) -> list:
        """
        Discover active markets suitable for market making.
        Prioritizes markets with rewards programs.
        """
        markets = []
        
        paginator = self.public_client.list_markets(closed=False, page_size=50)
        
        async for page in paginator:
            for m in page.items:
                if not m.outcomes or not m.outcomes.yes or not m.outcomes.yes.token_id:
                    continue
                if not m.condition_id:
                    continue
                
                vol = float(m.metrics.volume) if m.metrics and m.metrics.volume else 0
                if vol < min_volume:
                    continue
                
                markets.append(m)
                if len(markets) >= max_markets:
                    break
            
            if len(markets) >= max_markets:
                break
        
        logger.info(f"Discovered {len(markets)} markets with volume >= ${min_volume}")
        
        # Sort: rewarded markets first, then by volume
        if prefer_rewarded and self.rewards:
            markets.sort(
                key=lambda m: (
                    m.condition_id in self.rewards,
                    float(m.metrics.volume) if m.metrics and m.metrics.volume else 0,
                ),
                reverse=True,
            )
        else:
            markets.sort(
                key=lambda m: float(m.metrics.volume) if m.metrics and m.metrics.volume else 0,
                reverse=True,
            )
        
        return markets

    async def get_market_book(self, token_id: str) -> dict:
        """Get current order book for a token."""
        try:
            book = await self.public_client.get_order_book(token_id=token_id)
            
            bids = []
            asks = []
            
            if book.bids:
                for b in book.bids:
                    bids.append({
                        "price": float(b.price),
                        "size": float(b.size),
                    })
            if book.asks:
                for a in book.asks:
                    asks.append({
                        "price": float(a.price),
                        "size": float(a.size),
                    })
            
            # Sort
            bids.sort(key=lambda x: x["price"], reverse=True)  # best bid first
            asks.sort(key=lambda x: x["price"])  # best ask first
            
            return {
                "bids": bids,
                "asks": asks,
                "best_bid": bids[0]["price"] if bids else 0,
                "best_ask": asks[0]["price"] if asks else 0,
                "bid_size": bids[0]["size"] if bids else 0,
                "ask_size": asks[0]["size"] if asks else 0,
                "spread": (asks[0]["price"] - bids[0]["price"]) if bids and asks else 0,
                "spread_pct": ((asks[0]["price"] - bids[0]["price"]) / asks[0]["price"] * 100) if bids and asks and asks[0]["price"] > 0 else 0,
            }
        except Exception as e:
            logger.error(f"Error getting book for {token_id[:15]}...: {e}")
            return {"bids": [], "asks": [], "best_bid": 0, "best_ask": 0, "spread": 0, "spread_pct": 0}

    async def select_markets_for_mm(
        self,
        markets: list = None,
        min_spread_pct: float = 0.5,
        max_spread_pct: float = 15.0,
        top_n: int = 20,
    ) -> list:
        """
        Select best markets for market making.
        Goes DIRECTLY from Data API trades → token_id → orderbook.
        No need for SDK market list — the Data API gives us token_ids directly.
        """
        import requests as req
        from collections import Counter
        
        # Step 1: Get recent trades from Data API
        logger.info("Fetching recent trade activity from Data API...")
        all_trades = []
        offset = 0
        for batch in range(5):
            try:
                r = req.get(
                    "https://data-api.polymarket.com/trades",
                    params={"limit": 500, "offset": offset, "sortOrder": "desc"},
                    timeout=15,
                )
                if r.status_code == 200:
                    batch_trades = r.json()
                    all_trades.extend(batch_trades)
                    if len(batch_trades) < 500:
                        break
                    offset += 500
                else:
                    break
            except:
                break
        
        # Count trades per asset (token_id)
        trade_counts_by_asset = Counter()
        trade_volumes_by_asset = {}
        trade_titles = {}
        trade_conditions = {}
        trade_sides_by_asset = {}
        trade_prices = {}  # latest trade price per asset
        
        for t in all_trades:
            asset = t.get("asset", "")
            condition = t.get("conditionId", "")
            title = t.get("title", "")
            size = t.get("size", 0)
            price = t.get("price", 0)
            side = t.get("side", "")
            
            trade_counts_by_asset[asset] += 1
            trade_volumes_by_asset[asset] = trade_volumes_by_asset.get(asset, 0) + size * price
            trade_titles[asset] = title
            trade_conditions[asset] = condition
            trade_prices[asset] = price
            if asset not in trade_sides_by_asset:
                trade_sides_by_asset[asset] = {"BUY": 0, "SELL": 0}
            trade_sides_by_asset[asset][side] = trade_sides_by_asset[asset].get(side, 0) + 1
        
        logger.info(f"Data API: {len(all_trades)} trades across {len(trade_counts_by_asset)} assets")
        
        # Also build a lookup from SDK markets for rewards matching
        market_by_condition = {}
        if markets:
            for m in markets:
                if m.condition_id:
                    market_by_condition[m.condition_id] = m
        
        # Step 2: Check orderbooks for the hottest assets
        candidates = []
        hot_assets = trade_counts_by_asset.most_common(100)
        
        logger.info(f"Checking orderbooks for top {min(len(hot_assets), 100)} active assets...")
        
        seen_condition_ids = set()
        
        for asset_id, count in hot_assets:
            if not asset_id:
                continue
            
            # Skip if we already have this market (YES + NO on same condition)
            condition = trade_conditions.get(asset_id, "")
            if condition in seen_condition_ids:
                continue
            
            # Get orderbook directly using token_id
            book = await self.get_market_book(asset_id)
            
            if book["spread"] <= 0:
                continue
            if book["spread_pct"] < min_spread_pct or book["spread_pct"] > max_spread_pct:
                continue
            
            seen_condition_ids.add(condition)
            
            # Check rewards
            reward = self.rewards.get(condition)
            reward_rate = reward.rate_per_day if reward else 0
            
            recent_vol = trade_volumes_by_asset.get(asset_id, 0)
            sides = trade_sides_by_asset.get(asset_id, {})
            buy_count = sides.get("BUY", 0)
            sell_count = sides.get("SELL", 0)
            title = trade_titles.get(asset_id, "Unknown")
            last_price = trade_prices.get(asset_id, 0)
            
            # Score: trade activity + spread + rewards + two-sided trading
            score = (
                count * 100  # Total trades
                + book["spread"] * 500  # Spread width
                + reward_rate * 2  # Rewards
                + min(buy_count, sell_count) * 50  # Balanced trading
                + min(recent_vol, 5000) / 10  # Volume
            )
            
            candidates.append({
                "token_id": asset_id,
                "question": title,
                "condition_id": condition,
                "book": book,
                "reward_rate": reward_rate,
                "recent_trades": count,
                "recent_volume": recent_vol,
                "buy_trades": buy_count,
                "sell_trades": sell_count,
                "last_price": last_price,
                "score": score,
            })
        
        # Sort by score
        candidates.sort(key=lambda x: x["score"], reverse=True)
        
        selected = candidates[:top_n]
        
        logger.info(f"Selected {len(selected)} markets for MM:")
        for c in selected[:15]:
            b = c["book"]
            r = c["reward_rate"]
            rt = c["recent_trades"]
            rv = c["recent_volume"]
            bt = c["buy_trades"]
            st = c["sell_trades"]
            q = c["question"][:45]
            print(f"  {q:45s} | sprd={b['spread']:.3f} | rct={rt:3d} (B{bt}/S{st}) | vol=${rv:>7.0f} | rwd=${r:.1f}/d | score={c['score']:.1f}")
        
        return selected

    async def place_mm_orders(self, token_id: str, question: str, condition_id: str,
                               buy_price: float, sell_price: float, size: int):
        """Place dual-sided limit orders (buy at bid, sell at ask)."""
        
        if self.paper_mode:
            # Paper mode: just record the order
            order = MarketMakingOrder(
                token_id=token_id,
                question=question,
                condition_id=condition_id,
                buy_price=buy_price,
                sell_price=sell_price,
                size=size,
                placed_at=time.time(),
                last_refresh=time.time(),
            )
            self.orders[token_id] = order
            logger.debug(f"PAPER: Placed BUY {size}@{buy_price} + SELL {size}@{sell_price} on {question[:30]}")
            return order
        
        # Live mode: place real orders via SDK
        try:
            # Place buy order
            buy_resp = await self.secure_client.place_limit_order(
                token_id=token_id,
                side="BUY",
                price=str(buy_price),
                size=str(size),
                expiration=int(time.time()) + 300,  # 5 min expiry
            )
            
            buy_id = None
            if buy_resp.ok:
                buy_id = buy_resp.order_id
            else:
                logger.error(f"Buy order failed: {buy_resp.code} - {buy_resp.message}")
            
            # Place sell order
            sell_resp = await self.secure_client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price=str(sell_price),
                size=str(size),
                expiration=int(time.time()) + 300,
            )
            
            sell_id = None
            if sell_resp.ok:
                sell_id = sell_resp.order_id
            else:
                logger.error(f"Sell order failed: {sell_resp.code} - {sell_resp.message}")
            
            order = MarketMakingOrder(
                token_id=token_id,
                question=question,
                condition_id=condition_id,
                buy_price=buy_price,
                sell_price=sell_price,
                size=size,
                buy_order_id=buy_id,
                sell_order_id=sell_id,
                placed_at=time.time(),
                last_refresh=time.time(),
            )
            self.orders[token_id] = order
            
            logger.info(f"LIVE: Placed BUY {size}@{buy_price} (id={buy_id}) + SELL {size}@{sell_price} (id={sell_id}) on {question[:30]}")
            return order
            
        except Exception as e:
            logger.error(f"Error placing orders on {token_id[:15]}...: {e}")
            return None

    async def cancel_order(self, order: MarketMakingOrder, side: str = "both"):
        """Cancel one or both sides of an MM order pair."""
        if self.paper_mode or not self.secure_client:
            return
        
        try:
            if side in ("both", "buy") and order.buy_order_id:
                await self.secure_client.cancel_order(order_id=order.buy_order_id)
                order.buy_order_id = None
            
            if side in ("both", "sell") and order.sell_order_id:
                await self.secure_client.cancel_order(order_id=order.sell_order_id)
                order.sell_order_id = None
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")

    async def refresh_orders(self):
        """Refresh all orders — cancel and re-place at current bid/ask."""
        now = time.time()
        
        for token_id, order in list(self.orders.items()):
            if now - order.last_refresh < self.order_refresh_interval:
                continue
            
            # Get current book
            book = await self.get_market_book(token_id)
            if book["spread"] <= 0:
                continue
            
            new_bid = book["best_bid"]
            new_ask = book["best_ask"]
            
            # Check if prices changed enough to warrant refresh
            if abs(new_bid - order.buy_price) < 0.001 and abs(new_ask - order.sell_price) < 0.001:
                order.last_refresh = now
                continue
            
            # Cancel old orders
            await self.cancel_order(order)
            
            # Place new orders
            await self.place_mm_orders(
                token_id=token_id,
                question=order.question,
                condition_id=order.condition_id,
                buy_price=new_bid,
                sell_price=new_ask,
                size=order.size,
            )

    async def check_fills(self):
        """Check if any orders have been filled (paper mode simulates fills)."""
        if self.paper_mode:
            # Paper fill simulation using Data API trade rates
            await self._check_fills_paper()
            return
        
        # Live mode: check actual order status
        for token_id, order in list(self.orders.items()):
            try:
                if order.buy_order_id:
                    buy_order = await self.secure_client.get_order(order_id=order.buy_order_id)
                    if buy_order and buy_order.size_matched and float(buy_order.size_matched) > order.buy_filled:
                        order.buy_filled = float(buy_order.size_matched)
                        logger.info(f"BUY FILL: {order.question[:30]} {order.buy_filled} shares @ {order.buy_price}")
                
                if order.sell_order_id:
                    sell_order = await self.secure_client.get_order(order_id=order.sell_order_id)
                    if sell_order and sell_order.size_matched and float(sell_order.size_matched) > order.sell_filled:
                        order.sell_filled = float(sell_order.size_matched)
                        logger.info(f"SELL FILL: {order.question[:30]} {order.sell_filled} shares @ {order.sell_price}")
                
                if order.buy_filled >= order.size and order.sell_filled >= order.size:
                    spread = (order.sell_price - order.buy_price) * order.size
                    order.spread_captured += spread
                    self.realized_pnl += spread
                    self.cycle_count += 1
                    
                    reward = self.rewards.get(order.condition_id)
                    if reward:
                        reward_share = reward.rate_per_day * (self.scan_interval / 86400)
                        order.reward_earned += reward_share
                        self.realized_pnl += reward_share
                    
                    logger.info(f"CYCLE: {order.question[:30]} spread=${spread:.2f} cycles={self.cycle_count}")
                    order.buy_filled = 0
                    order.sell_filled = 0
                    
            except Exception as e:
                logger.error(f"Error checking fills: {e}")
    
    async def _check_fills_paper(self):
        """Simulate fills in paper mode using real trade data from Data API."""
        import requests as req
        
        for token_id, order in list(self.orders.items()):
            try:
                # Fetch recent trades for this specific market
                r = req.get(
                    "https://data-api.polymarket.com/trades",
                    params={"asset": token_id, "limit": 10, "sortOrder": "desc"},
                    timeout=5,
                )
                if r.status_code != 200:
                    continue
                
                trades = r.json()
                if not trades:
                    continue
                
                # Check if any recent trades crossed our bid or ask
                for t in trades:
                    trade_price = t.get("price", 0)
                    trade_ts = t.get("timestamp", 0)
                    
                    # Only count trades since our last check
                    if trade_ts < order.last_refresh:
                        continue
                    
                    trade_side = t.get("side", "")
                    trade_size = t.get("size", 0)
                    
                    # If someone BOUGHT at or above our ask → our sell fills
                    if trade_side == "BUY" and trade_price >= order.sell_price:
                        fill_shares = min(trade_size * 0.15, order.size - order.sell_filled)  # 15% our share
                        if fill_shares > 0:
                            order.sell_filled += fill_shares
                            self.fill_count += 1
                            logger.debug(f"PAPER SELL FILL: {order.question[:30]} {fill_shares:.0f}sh @ {order.sell_price}")
                    
                    # If someone SOLD at or below our bid → our buy fills
                    if trade_side == "SELL" and trade_price <= order.buy_price:
                        fill_shares = min(trade_size * 0.15, order.size - order.buy_filled)
                        if fill_shares > 0:
                            order.buy_filled += fill_shares
                            self.fill_count += 1
                            logger.debug(f"PAPER BUY FILL: {order.question[:30]} {fill_shares:.0f}sh @ {order.buy_price}")
                    
                    # Check cycle completion
                    if order.buy_filled >= order.size and order.sell_filled >= order.size:
                        spread = (order.sell_price - order.buy_price) * order.size
                        order.spread_captured += spread
                        self.realized_pnl += spread
                        self.cycle_count += 1
                        
                        # Rewards
                        reward = self.rewards.get(order.condition_id)
                        if reward:
                            reward_share = reward.rate_per_day * (self.scan_interval / 86400)
                            order.reward_earned += reward_share
                            self.realized_pnl += reward_share
                        
                        logger.info(f"CYCLE: {order.question[:30]} spread=${spread:.2f} reward=${order.reward_earned:.4f} cycles={self.cycle_count}")
                        order.buy_filled = 0
                        order.sell_filled = 0
                        break
                        
            except Exception as e:
                pass  # Non-fatal

    async def get_portfolio_value(self) -> float:
        """Get current portfolio value."""
        if self.paper_mode or not self.secure_client:
            # Paper: estimate from positions
            total = self.cash + self.realized_pnl
            for token_id, pos in self.positions.items():
                total += pos.get("unrealized", 0)
            return total
        
        try:
            values = await self.secure_client.get_portfolio_values()
            total = sum(float(v.value) for v in values if v.value)
            return total
        except Exception as e:
            logger.error(f"Error getting portfolio: {e}")
            return self.capital + self.realized_pnl

    async def get_positions(self) -> list:
        """Get current positions."""
        if self.paper_mode or not self.secure_client:
            return list(self.positions.values())
        
        try:
            stream = self.secure_client.list_positions(page_size=50)
            page = await stream.first_page()
            return list(page.items)
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    def save_state(self):
        """Save state to disk."""
        state = {
            "started_at": self.started_at,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "fill_count": self.fill_count,
            "cycle_count": self.cycle_count,
            "paper_mode": self.paper_mode,
            "orders": {
                tid: {
                    "question": o.question,
                    "condition_id": o.condition_id,
                    "buy_price": o.buy_price,
                    "sell_price": o.sell_price,
                    "size": o.size,
                    "buy_order_id": o.buy_order_id,
                    "sell_order_id": o.sell_order_id,
                    "buy_filled": o.buy_filled,
                    "sell_filled": o.sell_filled,
                    "spread_captured": o.spread_captured,
                    "reward_earned": o.reward_earned,
                    "placed_at": o.placed_at,
                    "last_refresh": o.last_refresh,
                }
                for tid, o in self.orders.items()
            },
            "rewards_count": len(self.rewards),
            "timestamp": datetime.now().isoformat(),
        }
        
        with open(self.state_dir / "state.json", 'w') as f:
            json.dump(state, f, indent=2, default=str)

    async def run(self, duration: int = 3600, scan_interval: float = None):
        """Main trading loop."""
        if scan_interval:
            self.scan_interval = scan_interval
        
        self.started_at = time.time()
        logger.info(f"{'='*60}")
        logger.info(f"LIVE TRADER STARTING — {'PAPER' if self.paper_mode else 'LIVE'} MODE")
        logger.info(f"Capital: ${self.capital} | Markets: {self.max_positions} | Size: {self.position_size} shares")
        logger.info(f"Scan: {self.scan_interval}s | Refresh: {self.order_refresh_interval}s")
        logger.info(f"{'='*60}")
        
        # Initialize
        await self.initialize()
        
        # Fetch rewards
        await self.fetch_rewards_programs()
        
        # Discover and select markets
        markets = await self.discover_markets(min_volume=5000, max_markets=500)
        selected = await self.select_markets_for_mm(markets, top_n=self.max_positions)
        
        if not selected:
            logger.error("No suitable markets found!")
            return
        
        # Place initial orders
        logger.info(f"Placing initial orders on {len(selected)} markets...")
        for s in selected:
            book = s["book"]
            await self.place_mm_orders(
                token_id=s["token_id"],
                question=s["question"],
                condition_id=s["condition_id"],
                buy_price=book["best_bid"],
                sell_price=book["best_ask"],
                size=self.position_size,
            )
            await asyncio.sleep(0.1)  # Rate limit
        
        logger.info(f"Orders placed on {len(self.orders)} markets")
        
        # Main loop
        last_scan = 0
        last_refresh = 0
        last_report = 0
        last_save = 0
        
        try:
            while time.time() - self.started_at < duration:
                now = time.time()
                
                # Scan: check for new opportunities
                if now - last_scan > self.scan_interval:
                    await self.check_fills()
                    last_scan = now
                
                # Refresh orders
                if now - last_refresh > self.order_refresh_interval:
                    await self.refresh_orders()
                    last_refresh = now
                
                # Report
                if now - last_report > 60:
                    runtime = now - self.started_at
                    portfolio = await self.get_portfolio_value()
                    
                    logger.info(f"{'='*60}")
                    logger.info(f"STATUS — {runtime/60:.1f}min | {'PAPER' if self.paper_mode else 'LIVE'}")
                    logger.info(f"Portfolio: ${portfolio:,.2f} | P&L: ${self.realized_pnl:+,.2f}")
                    logger.info(f"Cycles: {self.cycle_count} | Orders: {len(self.orders)}")
                    
                    total_spread = sum(o.spread_captured for o in self.orders.values())
                    total_reward = sum(o.reward_earned for o in self.orders.values())
                    logger.info(f"Spread captured: ${total_spread:+.2f} | Rewards: ${total_reward:+.2f}")
                    
                    if runtime > 0:
                        rate_h = self.realized_pnl / (runtime / 3600)
                        logger.info(f"Rate: ${rate_h:+.2f}/h → ${rate_h*24:+.2f}/day")
                    
                    # Top positions
                    active = [(tid, o) for tid, o in self.orders.items() if o.spread_captured > 0]
                    if active:
                        logger.info(f"Top earners:")
                        for tid, o in sorted(active, key=lambda x: x[1].spread_captured, reverse=True)[:5]:
                            logger.info(f"  {o.question[:40]:40s} | ${o.spread_captured:+.2f} | ${o.reward_earned:+.2f}")
                    
                    last_report = now
                
                # Save state
                if now - last_save > 10:
                    self.save_state()
                    last_save = now
                
                await asyncio.sleep(0.5)
        
        except KeyboardInterrupt:
            logger.info("Stopping...")
        finally:
            # Cancel all orders on exit
            if not self.paper_mode:
                logger.info("Cancelling all orders...")
                for order in self.orders.values():
                    await self.cancel_order(order)
            
            self.save_state()
            await self.close()
            logger.info("Stopped. State saved.")


# Entry point
def run_paper_trader(
    capital=100000,
    duration=86400,
    position_size=500,
    max_positions=20,
    scan_interval=1.5,
    state_dir="data/live_trader",
):
    """Run the live trader in paper mode."""
    trader = LiveTrader(
        paper_mode=True,
        capital=capital,
        position_size=position_size,
        max_positions=max_positions,
        scan_interval=scan_interval,
        state_dir=state_dir,
    )
    asyncio.run(trader.run(duration=duration))


def run_live_trader(
    private_key: str,
    wallet_address: str,
    relayer_api_key: str,
    relayer_key_address: str,
    capital=1000,
    duration=86400,
    position_size=100,
    max_positions=10,
    scan_interval=1.5,
    state_dir="data/live_trader",
):
    """Run the live trader with real orders."""
    trader = LiveTrader(
        private_key=private_key,
        wallet_address=wallet_address,
        relayer_api_key=relayer_api_key,
        relayer_key_address=relayer_key_address,
        paper_mode=False,
        capital=capital,
        position_size=position_size,
        max_positions=max_positions,
        scan_interval=scan_interval,
        state_dir=state_dir,
    )
    asyncio.run(trader.run(duration=duration))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Polymarket Live Trader")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--position-size", type=int, default=500)
    parser.add_argument("--max-positions", type=int, default=20)
    parser.add_argument("--scan-interval", type=float, default=1.5)
    parser.add_argument("--reset", action="store_true", help="Reset state")
    
    # Live mode credentials
    parser.add_argument("--private-key", type=str, default=None)
    parser.add_argument("--wallet", type=str, default=None)
    parser.add_argument("--relayer-key", type=str, default=None)
    parser.add_argument("--relayer-addr", type=str, default=None)
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    
    if args.mode == "live":
        if not args.private_key or not args.wallet:
            print("ERROR: --private-key and --wallet required for live mode")
            sys.exit(1)
        run_live_trader(
            private_key=args.private_key,
            wallet_address=args.wallet,
            relayer_api_key=args.relayer_key,
            relayer_key_address=args.relayer_addr,
            capital=args.capital,
            duration=args.duration,
            position_size=args.position_size,
            max_positions=args.max_positions,
            scan_interval=args.scan_interval,
        )
    else:
        run_paper_trader(
            capital=args.capital,
            duration=args.duration,
            position_size=args.position_size,
            max_positions=args.max_positions,
            scan_interval=args.scan_interval,
        )
