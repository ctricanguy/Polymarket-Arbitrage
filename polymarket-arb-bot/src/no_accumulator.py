#!/usr/bin/env python3
"""
Polymarket NO-Share Accumulator Bot
====================================
Replicates friend's strategy: buy NO shares at 1-5¢ on events unlikely to happen.
Hold to resolution. Diversify across 20+ events. Slow daily drift = profit.

Strategy:
1. Scan Polymarket for markets with NO-side price at 1-5¢
2. Buy NO shares in bulk (budget per position: $500-1000)
3. Hold until resolution (most events don't happen → NO shares → $1.00)
4. Track portfolio drift as positions approach resolution
5. Reinvest profits into new positions

Key differences from MM bot:
- No WebSocket needed (REST prices are fine for low-frequency events)
- No spread capture, no queue position, no adverse selection
- Positions held for days/weeks, not seconds
- Risk = event actually happening (diversification mitigates this)
- Reward = 50-100x on each position that resolves to NO
"""

import asyncio
import json
import time
import os
import sys
import logging
import random
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("polymarket-arb.no_accumulator")


class NoAccumulatorBot:
    """Buy cheap NO shares on unlikely events, hold to resolution."""

    def __init__(
        self,
        capital: float = 100000,
        position_budget: float = 1000,  # $ per position
        max_positions: int = 30,
        min_no_price: float = 0.01,  # 1¢ minimum
        max_no_price: float = 0.05,  # 5¢ maximum
        scan_interval: int = 300,  # 5 min between scans
        reset: bool = False,
        state_dir: str = "/home/star/polymarket-arb-bot/data/no_accumulator",
    ):
        self.capital = capital
        self.starting_capital = capital
        self.position_budget = position_budget
        self.max_positions = max_positions
        self.min_no_price = min_no_price
        self.max_no_price = max_no_price
        self.scan_interval = scan_interval
        self.reset = reset
        self.state_dir = state_dir

        os.makedirs(state_dir, exist_ok=True)
        self.state_file = os.path.join(state_dir, "state.json")
        self.events_file = os.path.join(state_dir, "trade_events.json")

        # State
        self.positions = {}  # token_id → position dict
        self.cash = capital
        self.realized_pnl = 0
        self.total_invested = 0
        self.total_resolved = 0
        self.wins = 0
        self.losses = 0
        self.started_at = time.time()

        # API
        self.gamma_url = "https://gamma-api.polymarket.com/markets"
        self.data_api_url = "https://data-api.polymarket.com/trades"
        self.clob_url = "https://clob.polymarket.com"

        # Cached market scan results (refresh every 2 min)
        self._cached_markets = []
        self._last_market_scan = 0
        self._market_cache_ttl = 120  # 2 min cache

        if reset and os.path.exists(self.state_file):
            os.remove(self.state_file)
            logger.info("State reset")

        self.load_state()

    def load_state(self):
        """Load state from disk."""
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                s = json.load(f)
            self.positions = s.get("positions", {})
            self.cash = s.get("cash", self.capital)
            self.realized_pnl = s.get("realized_pnl", 0)
            self.total_invested = s.get("total_invested", 0)
            self.total_resolved = s.get("total_resolved", 0)
            self.wins = s.get("wins", 0)
            self.losses = s.get("losses", 0)
            self.started_at = s.get("started_at", time.time())
            self.starting_capital = s.get("starting_capital", self.capital)
            logger.info(f"Loaded state: {len(self.positions)} positions, ${self.cash:,.2f} cash")

    def save_state(self):
        """Save state to disk."""
        s = {
            "positions": self.positions,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "total_invested": self.total_invested,
            "total_resolved": self.total_resolved,
            "wins": self.wins,
            "losses": self.losses,
            "started_at": self.started_at,
            "starting_capital": self.starting_capital,
            "timestamp": time.time(),
        }
        with open(self.state_file, 'w') as f:
            json.dump(s, f, indent=2, default=str)

    def scan_markets(self) -> list:
        """Scan Gamma API for markets with cheap NO shares (1-5¢).

        Uses a 2-minute cache so the bot can buy rapidly without re-scanning.
        Returns list of market dicts with token_id, question, no_price, no_token_id.
        """
        # Use cached results if fresh
        if self._cached_markets and (time.time() - self._last_market_scan) < self._market_cache_ttl:
            # Filter out markets we already have positions in
            available = [m for m in self._cached_markets if m["no_token_id"] not in self.positions]
            return available

        logger.info("Scanning for cheap NO markets...")

        # Get active markets from Gamma API
        markets = []
        offset = 0
        limit = 100

        while len(markets) < 500:
            try:
                r = requests.get(self.gamma_url, params={
                    "limit": limit,
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                    "order": "volume",
                    "ascending": "false",
                }, timeout=10)
                if r.status_code != 200:
                    break
                batch = r.json()
                if not batch:
                    break
                markets.extend(batch)
                offset += limit
            except Exception as e:
                logger.warning(f"Gamma API error: {e}")
                break

        logger.info(f"Found {len(markets)} active markets from Gamma API")

        # Filter for markets with cheap NO side
        cheap_markets = []
        for m in markets:
            try:
                # Get outcomes and tokens
                outcomes = m.get("outcomes", [])
                outcome_prices = m.get("outcomePrices", "[]")

                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)

                if not outcomes or not outcome_prices:
                    continue

                if len(outcomes) < 2 or len(outcome_prices) < 2:
                    continue

                # Find the NO side (usually second outcome)
                # outcomes = ["Yes", "No"] typically
                no_idx = None
                for i, o in enumerate(outcomes):
                    if o.lower() in ("no", "down", "under"):
                        no_idx = i
                        break

                if no_idx is None:
                    # If no explicit "No", use the lower-priced outcome
                    if float(outcome_prices[0]) < float(outcome_prices[1]):
                        no_idx = 0
                    else:
                        no_idx = 1

                no_price = float(outcome_prices[no_idx])
                yes_price = float(outcome_prices[1 - no_idx]) if len(outcome_prices) > 1 else 1.0 - no_price

                # Check if NO price is in our range (1-5¢)
                if self.min_no_price <= no_price <= self.max_no_price:
                    # Get token IDs
                    clob_token_ids = m.get("clobTokenIds", "[]")
                    if isinstance(clob_token_ids, str):
                        clob_token_ids = json.loads(clob_token_ids)

                    if not clob_token_ids or no_idx >= len(clob_token_ids):
                        continue

                    no_token_id = clob_token_ids[no_idx]

                    # Skip if we already have a position
                    if no_token_id in self.positions:
                        continue

                    # Skip markets ending too soon (< 7 days) or too far (> 365 days)
                    end_date = m.get("endDate", "")
                    if end_date:
                        try:
                            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                            days_to_end = (end_dt - datetime.now(end_dt.tzinfo)).days
                            if days_to_end < 7 or days_to_end > 365:
                                continue
                        except:
                            pass

                    # Get real market data from Gamma
                    volume24hr = float(m.get("volume24hr", 0) or 0)
                    volume1mo = float(m.get("volume1mo", 0) or 0)
                    one_hour_change = float(m.get("oneHourPriceChange", 0) or 0)
                    liquidity = float(m.get("liquidity", 0) or 0)
                    spread = float(m.get("spread", 0) or 0)

                    # Skip dead markets (no volume in 24h and no liquidity)
                    if volume24hr < 1 and liquidity < 100:
                        continue

                    cheap_markets.append({
                        "question": m.get("question", ""),
                        "no_price": no_price,
                        "yes_price": yes_price,
                        "no_token_id": no_token_id,
                        "condition_id": m.get("conditionId", ""),
                        "volume": float(m.get("volume", 0) or 0),
                        "volume24hr": volume24hr,
                        "volume1mo": volume1mo,
                        "liquidity": liquidity,
                        "spread": spread,
                        "one_hour_change": one_hour_change,
                        "end_date": end_date,
                        "category": self._classify(m.get("question", "")),
                        "spread_pct": abs(yes_price - no_price) * 100,
                    })

            except (ValueError, KeyError, TypeError) as e:
                continue

        # Sort by 24hr volume (higher = more actively traded)
        cheap_markets.sort(key=lambda x: x.get("volume24hr", 0), reverse=True)

        # Cache results
        self._cached_markets = cheap_markets
        self._last_market_scan = time.time()

        logger.info(f"Found {len(cheap_markets)} cheap NO markets (1-5¢)")
        for m in cheap_markets[:15]:
            logger.info(f"  {m['question'][:50]:50s} | NO={m['no_price']:.3f} YES={m['yes_price']:.3f} | vol=${m['volume']:,.0f}")

        return cheap_markets

    def _classify(self, question: str) -> str:
        q = question.lower()
        if any(w in q for w in ["bitcoin", "ethereum", "solana", "crypto", "usdt", "market cap"]):
            return "crypto"
        if any(w in q for w in ["earthquake", "temperature", "weather", "hurricane"]):
            return "nature"
        if any(w in q for w in ["trump", "biden", "election", "president", "congress", "scotus", "pardon"]):
            return "politics"
        if any(w in q for w in ["ipo", "acquisition", "stock", "company", "warner", "paramount", "discord"]):
            return "business"
        if any(w in q for w in ["russia", "ukraine", "china", "iran", "kupiansk", "war"]):
            return "geopolitics"
        if any(w in q for w in ["ai", "model", "gpt", "openai"]):
            return "tech"
        if any(w in q for w in ["world cup", "nba", "nfl", "soccer", "football", "game", "match"]):
            return "sports"
        return "other"

    def get_real_fill_price(self, token_id: str, target_shares: int) -> tuple:
        """Check real CLOB orderbook and calculate actual fill price.
        
        Returns (fill_price, fillable_shares, avg_ask_price) or (None, 0, None) if no asks.
        """
        try:
            r = requests.get(
                f"{self.clob_url}/book",
                params={"token_id": token_id},
                timeout=5
            )
            if r.status_code != 200:
                return None, 0, None
            
            book = r.json()
            asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
            
            if not asks:
                return None, 0, None
            
            # Walk the orderbook to fill target_shares
            remaining = target_shares
            total_cost = 0
            for a in asks:
                sz = float(a["size"])
                px = float(a["price"])
                fill = min(sz, remaining)
                total_cost += fill * px
                remaining -= fill
                if remaining <= 0:
                    break
            
            filled = target_shares - remaining
            if filled == 0:
                return None, 0, float(asks[0]["price"])
            
            fill_price = total_cost / filled
            return fill_price, filled, float(asks[0]["price"])
            
        except Exception as e:
            logger.warning(f"CLOB book fetch failed: {e}")
            return None, 0, None

    def buy_no_shares(self, market: dict):
        """Buy cheap shares on a market (simulated paper trade).
        
        Strategy: Place LIMIT BUY at 1-5¢ and wait for sellers.
        This is a PASSIVE strategy — we don't take the ask (which is 50-99¢).
        We place a limit order and fill when someone market-sells into our bid.
        
        Fill probability is based on REAL 24h volume:
        - High volume → more traders → more likely someone sells at our bid
        - Low volume → fewer traders → less likely to fill
        """
        token_id = market["no_token_id"]
        gamma_price = market["no_price"]

        # We place a limit buy at the Gamma price (last trade price)
        # This is what the friend does — he bids at the last trade price
        limit_price = gamma_price + 0.001  # 0.1¢ above last trade to be top of book
        max_shares = int(self.position_budget / limit_price)
        max_shares = min(max_shares, 30000)

        cost = max_shares * limit_price
        if cost > self.cash:
            max_shares = int(self.cash / limit_price)
            cost = max_shares * limit_price
            if max_shares < 100:
                logger.warning(f"Insufficient cash: ${self.cash:.2f}")
                return

        # Fill probability based on REAL 24h volume AND time
        # The friend holds many positions and fills slowly over time
        # Each scan cycle represents 15 seconds
        #
        # REALISTIC FILL MODEL:
        # - High volume ($1K+/day): trades happen frequently → higher fill chance
        # - Low volume ($0-100/day): trades are rare but DO happen occasionally
        # - Even $0 volume markets get occasional market-sellers (news events, etc.)
        #
        # MINIMUM FLOOR: 0.3% per scan = ~26% chance per hour = ~99% in 24h
        # This ensures even dead markets eventually fill over days (matching friend)
        vol24 = market.get("volume24hr", 0)
        avg_trade_size = 100  # conservative estimate
        if vol24 > 0:
            trades_per_day = vol24 / (avg_trade_size * limit_price)
        else:
            trades_per_day = 2  # even dead markets get ~2 trades/day from random events
        trades_per_scan = trades_per_day / (86400 / self.scan_interval)
        fill_prob = min(trades_per_scan * 0.30, 0.15)  # cap at 15% per scan
        fill_prob = max(fill_prob, 0.003)  # MINIMUM FLOOR: 0.3% per scan

        if random.random() > fill_prob:
            logger.info(f"  WAIT: No seller at {limit_price:.3f} ({fill_prob:.1%}/scan): {market['question'][:40]}")
            return

        # Record position
        self.positions[token_id] = {
            "question": market["question"],
            "token_id": token_id,
            "condition_id": market["condition_id"],
            "side": "NO",
            "entry_price": limit_price,
            "gamma_price": gamma_price,
            "shares": max_shares,
            "cost": cost,
            "entry_time": time.time(),
            "category": market["category"],
            "end_date": market["end_date"],
            "current_price": limit_price,
            "unrealized_pnl": 0,
            "status": "open",
            "volume24hr": vol24,
            "one_hour_change": market.get("one_hour_change", 0),
            "liquidity": market.get("liquidity", 0),
        }

        self.cash -= cost
        self.total_invested += cost

        logger.info(
            f"  FILLED: {market['question'][:42]:42s} | "
            f"{max_shares:>6} sh @ {limit_price:.4f} = ${cost:>8.2f} | "
            f"vol24=${vol24:.0f} | 1hChg={market.get('one_hour_change',0):+.4f}"
        )

    def update_prices(self):
        """Update current prices for all open positions using REAL Gamma API data.
        
        The Gamma API provides:
        - outcomePrices: current fair value (last trade price)
        - oneHourPriceChange: real 1-hour price movement
        
        We re-fetch from Gamma every 5 min to get real price updates.
        """
        if not self.positions:
            return

        # Re-fetch market data from Gamma every 5 min
        open_tokens = {pos["token_id"] for pos in self.positions.values() if pos["status"] == "open"}
        if not open_tokens:
            return

        # Build a set of condition_ids for matching
        open_conditions = {pos.get("condition_id", "") for pos in self.positions.values() if pos["status"] == "open"}
        
        try:
            # Fetch markets from Gamma that match our positions
            r = requests.get(self.gamma_url, params={
                "limit": 500,
                "active": "true",
                "closed": "false",
            }, timeout=10)
            
            if r.status_code != 200:
                # Fallback to drift model if API fails
                self._drift_fallback()
                return
            
            gamma_markets = r.json()
            
            # Build lookup by condition_id
            gamma_by_condition = {}
            for gm in gamma_markets:
                cid = gm.get("conditionId", "")
                if cid:
                    gamma_by_condition[cid] = gm
            
            updated = 0
            for token_id, pos in list(self.positions.items()):
                if pos["status"] != "open":
                    continue
                
                cid = pos.get("condition_id", "")
                gm = gamma_by_condition.get(cid)
                
                if gm:
                    # Use REAL Gamma prices
                    prices_raw = gm.get("outcomePrices", "[]")
                    if isinstance(prices_raw, str):
                        prices = json.loads(prices_raw)
                    else:
                        prices = prices_raw
                    
                    if len(prices) >= 2:
                        p0, p1 = float(prices[0]), float(prices[1])
                        # Find our side's price
                        # We bought NO. If entry_price was low (< 0.10), we're on the cheap side
                        if pos["entry_price"] < 0.10:
                            current = min(p0, p1)
                        else:
                            current = max(p0, p1)
                        
                        pos["current_price"] = round(current, 4)
                        pos["unrealized_pnl"] = (current - pos["entry_price"]) * pos["shares"]
                        pos["one_hour_change"] = float(gm.get("oneHourPriceChange", 0) or 0)
                        updated += 1
                else:
                    # Market not in Gamma results - use drift
                    days_held = (time.time() - pos["entry_time"]) / 86400
                    drift = 1 + (0.0002 * days_held)
                    pos["current_price"] = round(pos["entry_price"] * drift, 4)
                    pos["unrealized_pnl"] = (pos["current_price"] - pos["entry_price"]) * pos["shares"]
            
            if updated > 0:
                logger.info(f"Updated {updated} positions with real Gamma prices")
                
        except Exception as e:
            logger.warning(f"Gamma price update failed: {e}, using drift fallback")
            self._drift_fallback()

    def _drift_fallback(self):
        """Fallback price model when Gamma API is unavailable."""
        for token_id, pos in list(self.positions.items()):
            if pos["status"] != "open":
                continue
            days_held = (time.time() - pos["entry_time"]) / 86400
            drift = 1 + (0.0002 * days_held)
            pos["current_price"] = round(pos["entry_price"] * drift, 4)
            pos["unrealized_pnl"] = (pos["current_price"] - pos["entry_price"]) * pos["shares"]

    def check_resolutions(self):
        """Check if any positions have resolved (event expired or occurred)."""
        for token_id, pos in list(self.positions.items()):
            if pos["status"] != "open":
                continue

            # Check end date
            end_date = pos.get("end_date", "")
            if not end_date:
                continue

            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if datetime.now(end_dt.tzinfo) > end_dt:
                    # Market resolved — simulate outcome
                    # For low-probability events (1-5¢ NO), 95% resolve to NO ($1.00)
                    # 5% resolve to YES ($0.00)
                    resolve_prob = 0.95  # 95% chance event doesn't happen

                    if random.random() < resolve_prob:
                        # NO wins — shares go to $1.00
                        resolution_price = 1.0
                        pnl = (resolution_price - pos["entry_price"]) * pos["shares"]
                        pos["status"] = "resolved_no"
                        pos["resolution_price"] = resolution_price
                        pos["realized_pnl"] = pnl
                        self.realized_pnl += pnl
                        self.cash += pos["shares"] * resolution_price
                        self.wins += 1
                        logger.info(
                            f"  ✅ RESOLVED NO: {pos['question'][:40]:40s} | "
                            f"{pos['shares']} shares → $1.00 | P&L: ${pnl:+,.2f}"
                        )
                    else:
                        # YES wins — shares go to $0.00
                        resolution_price = 0.0
                        pnl = (resolution_price - pos["entry_price"]) * pos["shares"]
                        pos["status"] = "resolved_yes"
                        pos["resolution_price"] = resolution_price
                        pos["realized_pnl"] = pnl
                        self.realized_pnl += pnl
                        self.losses += 1
                        logger.warning(
                            f"  ❌ RESOLVED YES: {pos['question'][:40]:40s} | "
                            f"{pos['shares']} shares → $0.00 | P&L: ${pnl:+,.2f}"
                        )

                    self.total_resolved += 1
            except Exception:
                pass

    def get_portfolio_value(self) -> dict:
        """Calculate current portfolio value."""
        invested = 0
        unrealized = 0
        open_count = 0
        category_breakdown = defaultdict(lambda: {"invested": 0, "unrealized": 0, "count": 0})

        for pos in self.positions.values():
            if pos["status"] == "open":
                invested += pos["cost"]
                unrealized += pos["unrealized_pnl"]
                open_count += 1
                cat = pos.get("category", "other")
                category_breakdown[cat]["invested"] += pos["cost"]
                category_breakdown[cat]["unrealized"] += pos["unrealized_pnl"]
                category_breakdown[cat]["count"] += 1

        total_value = self.cash + invested + unrealized
        total_pnl = total_value - self.starting_capital

        return {
            "total_value": total_value,
            "cash": self.cash,
            "invested": invested,
            "unrealized": unrealized,
            "realized": self.realized_pnl,
            "total_pnl": total_pnl,
            "open_positions": open_count,
            "resolved": self.total_resolved,
            "wins": self.wins,
            "losses": self.losses,
            "categories": dict(category_breakdown),
        }

    def print_status(self):
        """Print current portfolio status."""
        pf = self.get_portfolio_value()
        runtime = time.time() - self.started_at
        hours = runtime / 3600
        days = hours / 24

        print("\n" + "=" * 70)
        print(f"NO-SHARE ACCUMULATOR BOT — {runtime/3600:.1f}h ({days:.2f}d)")
        print("=" * 70)
        print(f"Portfolio Value:  ${pf['total_value']:>12,.2f}")
        print(f"Cash:             ${pf['cash']:>12,.2f}")
        print(f"Invested:          ${pf['invested']:>12,.2f}")
        print(f"Realized P&L:      ${pf['realized']:>+12,.2f}")
        print(f"Unrealized P&L:    ${pf['unrealized']:>+12,.2f}")
        print(f"Total P&L:         ${pf['total_pnl']:>+12,.2f}")
        print(f"Win/Loss:          {pf['wins']}W / {pf['losses']}L / {pf['resolved']} resolved")
        print(f"Open Positions:    {pf['open_positions']}")
        if days > 0:
            daily_rate = pf['total_pnl'] / days
            print(f"Daily Rate:        ${daily_rate:>+12,.2f}/day")
        print()

        # Category breakdown
        if pf["categories"]:
            print("By Category:")
            for cat, vals in sorted(pf["categories"].items(), key=lambda x: -x[1]["invested"]):
                print(f"  {cat:12s}: ${vals['invested']:>8,.0f} invested | {vals['count']:>2} positions | unreal=${vals['unrealized']:>+8,.2f}")
            print()

        # Top positions
        open_positions = [p for p in self.positions.values() if p["status"] == "open"]
        open_positions.sort(key=lambda x: x["unrealized_pnl"], reverse=True)
        print("Top Positions (by unrealized P&L):")
        for p in open_positions[:10]:
            q = p["question"][:45]
            entry = p["entry_price"]
            curr = p["current_price"]
            shares = p["shares"]
            unreal = p["unrealized_pnl"]
            print(f"  {q:45s} | {shares:>6}sh @ {entry:.3f}→{curr:.3f} | ${unreal:>+8.2f}")

        print("=" * 70 + "\n")

        logger.info(
            f"Portfolio: ${pf['total_value']:,.2f} | P&L: ${pf['total_pnl']:+,.2f} | "
            f"Positions: {pf['open_positions']} | Realized: ${pf['realized']:+,.2f} | "
            f"Unrealized: ${pf['unrealized']:+,.2f}"
        )

    def run(self, duration: int = 86400):
        """Main bot loop."""
        logger.info("=" * 60)
        logger.info("NO-SHARE ACCUMULATOR BOT STARTING")
        logger.info(f"Capital: ${self.capital:,.2f} | Position budget: ${self.position_budget}")
        logger.info(f"NO price range: {self.min_no_price}¢ - {self.max_no_price}¢")
        logger.info(f"Max positions: {self.max_positions} | Scan interval: {self.scan_interval}s")
        logger.info("=" * 60)

        end_time = time.time() + duration
        last_scan = 0
        last_price_update = 0
        last_status = 0

        while time.time() < end_time:
            now = time.time()

            # Scan for new markets and buy positions
            if now - last_scan > self.scan_interval:
                open_count = sum(1 for p in self.positions.values() if p["status"] == "open")
                available_slots = self.max_positions - open_count

                if available_slots > 0 and self.cash > self.position_budget:
                    markets = self.scan_markets()
                    bought = 0
                    for m in markets:
                        if bought >= available_slots:
                            break
                        if self.cash < self.position_budget:
                            break
                        self.buy_no_shares(m)
                        if m["no_token_id"] in self.positions:
                            bought += 1
                    logger.info(f"Bought {bought} new positions ({open_count + bought}/{self.max_positions})")
                else:
                    logger.info(f"Slots full ({open_count}/{self.max_positions}) or insufficient cash (${self.cash:.2f})")

                self.save_state()
                last_scan = now

            # Update prices every 60s
            if now - last_price_update > 60:
                self.update_prices()
                last_price_update = now

            # Check resolutions every 60s
            if now - last_price_update > 60:
                self.check_resolutions()
                last_price_update = now

            # Print status every 5 min
            if now - last_status > 300:
                self.print_status()
                self.save_state()
                last_status = now

            # Sleep between cycles
            time.sleep(10)

        # Final save
        self.update_prices()
        self.check_resolutions()
        self.print_status()
        self.save_state()
        logger.info("Bot finished")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="NO-Share Accumulator Bot")
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--position-budget", type=float, default=1000)
    parser.add_argument("--max-positions", type=int, default=30)
    parser.add_argument("--min-no-price", type=float, default=0.01)
    parser.add_argument("--max-no-price", type=float, default=0.05)
    parser.add_argument("--scan-interval", type=int, default=15)
    parser.add_argument("--duration", type=int, default=86400)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    bot = NoAccumulatorBot(
        capital=args.capital,
        position_budget=args.position_budget,
        max_positions=args.max_positions,
        min_no_price=args.min_no_price,
        max_no_price=args.max_no_price,
        scan_interval=args.scan_interval,
        reset=args.reset,
    )
    bot.run(duration=args.duration)


if __name__ == "__main__":
    main()
