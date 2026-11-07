#!/usr/bin/env python3
"""
Polymarket YES-Side Market Maker Bot — LIVE + PAPER
====================================================
Strategy: Capture bid-ask spreads on low-priced YES tokens.

For each market:
  1. Buy YES at bid price (limit order)
  2. Immediately list sell at ask price (limit order)
  3. When both fill, pocket the spread
  4. Repeat

V2 SDK with POLY_1271 deposit wallet support.
"""

import argparse
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Suppress httpx request logging — cuts log volume by 90%
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("polymarket-arb.yes_mm")

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"


class LiveClobManager:
    """Manages the py-clob-client-v2 connection for live trading with POLY_1271 deposit wallet."""

    def __init__(self, private_key, deposit_wallet, builder_code, chain_id=137):
        from py_clob_client_v2 import ClobClient, ApiCreds, BalanceAllowanceParams, AssetType, SignatureTypeV2
        self.private_key = private_key
        self.deposit_wallet = deposit_wallet
        self.builder_code = builder_code
        self.chain_id = chain_id
        self.host = CLOB_API
        self._client = None
        self._creds = None

    def get_client(self):
        """Get or create the CLOB client with credentials set."""
        if self._client and self._creds:
            return self._client, self._creds

        from py_clob_client_v2 import ClobClient, SignatureTypeV2
        import logging as _logging
        # Suppress the harmless "Could not create api key" error from py_clob_client_v2
        _logging.getLogger("py_clob_client_v2.http_helpers.helpers").setLevel(_logging.CRITICAL)
        client = ClobClient(
            self.host,
            key=self.private_key,
            chain_id=self.chain_id,
            signature_type=SignatureTypeV2.POLY_1271,
            funder=self.deposit_wallet,
        )
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        self._client = client
        self._creds = creds
        logger.info(f"CLOB client ready: deposit_wallet={self.deposit_wallet}, API key={creds.api_key[:8]}...")
        return client, creds

    def get_balance(self):
        """Get current USDC balance."""
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        client, _ = self.get_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        return int(bal.get("balance", 0)) / 1e6

    def place_order(self, token_id, price, size, side, tick_size="0.01", neg_risk=False):
        """Place a live order. Returns (success, order_id, response)."""
        from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, OrderType, Side
        client, _ = self.get_client()

        side_enum = Side.BUY if side.upper() == "BUY" else Side.SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side_enum,
        )

        # Try with provided tick_size, then fallback to the other one
        # Also try flipping neg_risk if signature errors occur
        tick_options = [tick_size, "0.01" if tick_size == "0.001" else "0.001"]
        neg_risk_options = [neg_risk, not neg_risk]
        for tick in tick_options:
            for nr in neg_risk_options:
                options = PartialCreateOrderOptions(tick_size=tick, neg_risk=nr)
                try:
                    result = client.create_and_post_order(order_args, options, OrderType.GTC)
                    if isinstance(result, dict):
                        success = result.get("success", False)
                        order_id = result.get("orderID", "")
                        return success, order_id, result
                    return False, "", result
                except Exception as e:
                    err = str(e)
                    if "tick" in err.lower() and tick == tick_options[0]:
                        break  # Try next tick size
                    if "signature" in err.lower() and nr == neg_risk:
                        continue  # Try flipping neg_risk
                    logger.error(f"Order placement error: {e}")
                    return False, "", {"error": str(e)}
        return False, "", {"error": "All tick/neg_risk combos failed"}

    def cancel_order(self, order_id):
        """Cancel a live order."""
        client, _ = self.get_client()
        try:
            from py_clob_client_v2.clob_types import OrderPayload
            payload = OrderPayload(orderID=order_id)
            result = client.cancel_order(payload)
            return True, result
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            return False, str(e)

    def cancel_all(self):
        """Cancel all open orders."""
        client, _ = self.get_client()
        try:
            result = client.cancel_all()
            return True, result
        except Exception as e:
            logger.error(f"Cancel all error: {e}")
            return False, str(e)

    def get_open_orders(self):
        """Get all open orders."""
        client, _ = self.get_client()
        try:
            return client.get_open_orders()
        except Exception as e:
            logger.error(f"Get open orders error: {e}")
            return []

    def get_order_status(self, order_id):
        """Get order status from the CLOB."""
        client, _ = self.get_client()
        try:
            order = client.get_order(order_id)
            return order
        except Exception as e:
            # get_order may not exist in V2 — try get_orders
            try:
                orders = client.get_open_orders()
                for o in orders:
                    if o.get("id") == order_id or o.get("order_id") == order_id:
                        return o
            except:
                pass
            # Last resort: check trades for this order
            try:
                import requests as _req
                r = _req.get(f"https://clob.polymarket.com/trades", 
                           params={"maker_order_id": order_id}, timeout=5)
                if r.status_code == 200 and r.json():
                    trades = r.json()
                    total_matched = sum(float(t.get("size", 0)) for t in trades)
                    return {"status": "matched", "size_matched": total_matched}
            except:
                pass
            return None


class YesMarketMaker:
    def __init__(
        self,
        capital: float = 100_000,
        position_budget: float = 1_000,
        max_positions: int = 50,
        min_yes_price: float = 0.01,
        max_yes_price: float = 0.20,
        min_spread: float = 0.001,
        scan_interval: int = 15,
        duration: int = 86400,
        state_dir: str = None,
        reset: bool = False,
        live: bool = False,
        private_key: str = None,
        deposit_wallet: str = None,
        builder_code: str = None,
        no_drift: bool = False,
        max_no_positions: int = 10,
        no_min_price: float = 0.82,
        no_max_price: float = 0.96,
        no_drift_target: float = 0.06,
    ):
        self.capital = capital
        self.starting_capital = capital
        self.position_budget = position_budget
        self.max_positions = max_positions
        self.min_yes_price = min_yes_price
        self.max_yes_price = max_yes_price
        self.min_spread = min_spread
        self.scan_interval = scan_interval
        self.duration = duration
        # NO-side drift strategy params (Wallet A strategy)
        self.no_drift = no_drift
        self.max_no_positions = max_no_positions
        self.no_min_price = no_min_price
        self.no_max_price = no_max_price
        self.no_drift_target = no_drift_target  # target 6¢ drift before selling
        if state_dir is None:
            state_dir = str(Path(__file__).parent.parent / "data" / "yes_mm")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"

        # Live trading setup
        self.live = live
        self.clob = None
        if live:
            if not private_key or not deposit_wallet:
                raise ValueError("Live mode requires --private-key and --deposit-wallet")
            self.clob = LiveClobManager(private_key, deposit_wallet, builder_code or "")
            # Get real balance
            real_balance = self.clob.get_balance()
            logger.info(f"🔴 LIVE MODE — Real balance: ${real_balance:.2f}")
            self.capital = real_balance
            self.starting_capital = real_balance
            self.position_budget = min(position_budget, real_balance / max_positions * 0.9)  # Spread capital evenly, leave 10% buffer
            logger.info(f"Position budget: ${self.position_budget:.2f}")
        else:
            logger.info(f"📊 PAPER MODE — ${capital:,.2f} simulated capital")

        # Runtime state
        self.cash = self.capital
        self.positions = {}
        self.completed_rounds = []
        self.total_profit = 0
        self.total_round_trips = 0
        self.start_time = time.time()
        self.scans = 0
        self.markets_cache = []
        self.markets_cache_time = 0

        # Re-entry cooldown
        self.reentry_cooldowns = {}
        self.reentry_cooldown_sec = 30

        if not reset and self.state_file.exists():
            self.load_state()
        else:
            logger.info(f"Fresh start — ${self.capital:,.2f} capital, {max_positions} max positions")

    # ─── State Management ───────────────────────────────────────────

    def load_state(self):
        try:
            with open(self.state_file) as f:
                s = json.load(f)
            # Always load positions (both live and paper)
            self.positions = s.get("positions", {})
            self.completed_rounds = s.get("completed_rounds", [])
            self.total_profit = s.get("total_profit", 0)
            self.total_round_trips = s.get("total_round_trips", 0)
            self.start_time = s.get("start_time", time.time())
            self.scans = s.get("scans", 0)
            if self.live:
                # In live mode, use real balance for capital but keep positions
                logger.info(f"Live mode — loaded {len(self.positions)} positions from state, using real balance")
            else:
                self.cash = s.get("cash", self.capital)
                self.starting_capital = s.get("starting_capital", self.capital)
            logger.info(f"Loaded state: {len(self.positions)} active positions, "
                        f"${self.cash:,.2f} cash, ${self.total_profit:+.2f} profit, "
                        f"{self.total_round_trips} round trips")
        except Exception as e:
            logger.warning(f"Failed to load state: {e} — starting fresh")

    def sync_real_positions(self):
        """Sync with data API — adopt any positions we own but aren't tracking."""
        if not self.live:
            return
        try:
            import requests as _req
            r = _req.get("https://data-api.polymarket.com/positions",
                        params={"user": self.clob.deposit_wallet}, timeout=10)
            if r.status_code != 200:
                return
            real_positions = r.json()
            adopted = 0
            for p in real_positions:
                if not isinstance(p, dict):
                    continue
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                # Skip dust — positions below 5 shares can't be sold on Polymarket
                # and just waste slots. They'll resolve on their own.
                if size < 5:
                    continue
                # Skip NO-side positions from other bots/manual trading
                # (NO drift positions are tracked by the bot itself)
                outcome = p.get("outcome", "")
                if outcome and outcome.upper() == "NO":
                    continue  # Don't auto-adopt NO positions
                asset = p.get("asset", "")
                if not asset:
                    continue
                # Check if we're already tracking this position
                if asset in self.positions:
                    continue
                # Check if we have an open order for this asset
                try:
                    orders = self.clob.get_open_orders()
                    if any(o.get("asset_id") == asset for o in orders):
                        continue  # We have an open order, bot will track it
                except:
                    pass
                # ORPHAN DETECTED — we own shares but bot doesn't track them
                title = p.get("title", p.get("market", "Unknown"))[:60]
                avg_price = float(p.get("avgPrice", 0))
                cur_price = float(p.get("curPrice", 0))
                # Get current orderbook to find sell price
                book = self.get_orderbook(asset)
                sell_price = book.get("best_ask", cur_price) if book else cur_price
                if not sell_price or sell_price <= 0:
                    sell_price = cur_price
                # Adopt this position — set to selling status
                self.positions[asset] = {
                    "question": title,
                    "token_id": asset,
                    "bid_price": avg_price,
                    "ask_price": sell_price,
                    "shares_target": int(size),
                    "shares": int(size),  # ← set shares field
                    "shares_filled_buy": int(size),
                    "cost": size * avg_price,
                    "status": "selling",
                    "entry_time": time.time(),
                    "live_order_id": "",
                    "sell_order_id": "",
                    "tick_size": "0.01",
                    "neg_risk": False,
                    "volume24hr": 0,
                    "current_bid": book.get("best_bid", cur_price) if book else cur_price,
                    "current_ask": sell_price,
                    "orphan_adopted": True,
                }
                # Place a sell order immediately — try tick sizes and neg_risk combos
                placed = False
                for ts in ["0.01", "0.001"]:
                    if placed:
                        break
                    for nr in [False, True]:
                        if placed:
                            break
                        try:
                            success, order_id, response = self.clob.place_order(
                                token_id=asset,
                                price=sell_price,
                                size=int(size),
                                side="SELL",
                                tick_size=ts,
                                neg_risk=nr,
                            )
                            if success:
                                self.positions[asset]["sell_order_id"] = order_id
                                self.positions[asset]["tick_size"] = ts
                                self.positions[asset]["neg_risk"] = nr
                                logger.info(f"  🔄 ADOPTED ORPHAN: {title[:45]:45s} | "
                                           f"{int(size)} sh @ {avg_price*100:.1f}c -> sell@{sell_price*100:.1f}c (tick={ts}, nr={nr})")
                                adopted += 1
                                placed = True
                        except Exception as e:
                            continue  # Try next combo
                if not placed:
                    logger.warning(f"  ⚠️ Adopted orphan but ALL sell attempts failed: {title[:40]}")
            if adopted > 0:
                logger.info(f"  ✅ Adopted {adopted} orphaned positions from data API")
                self.save_state()
            # Also clean up ghost selling positions (no real shares, no sell order)
            real_assets = {str(p.get("asset", "")) for p in real_positions if float(p.get("size", 0)) > 0}
            ghost_removed = 0
            for tid in list(self.positions.keys()):
                pos = self.positions[tid]
                if pos.get("status") == "selling" and tid not in real_assets:
                    # No real shares — check if sell order is still live
                    sell_id = pos.get("sell_order_id", "")
                    try:
                        orders = self.clob.get_open_orders()
                        if sell_id and any(o.get("id") == sell_id for o in orders):
                            continue  # Sell order still live, let it fill
                    except:
                        pass
                    # Ghost — remove it
                    q = pos.get("question", "?")[:45]
                    logger.info(f"  🗑️ GHOST SELLING REMOVED: {q:45s} | no shares in data API, no live sell")
                    del self.positions[tid]
                    ghost_removed += 1
            if ghost_removed > 0:
                logger.info(f"  ✅ Removed {ghost_removed} ghost selling positions")
                self.save_state()
        except Exception as e:
            logger.warning(f"Position sync failed: {e}")

    def save_state(self):
        s = {
            "cash": self.cash,
            "positions": self.positions,
            "completed_rounds": self.completed_rounds[-200:],
            "total_profit": self.total_profit,
            "total_round_trips": self.total_round_trips,
            "start_time": self.start_time,
            "scans": self.scans,
            "starting_capital": self.starting_capital,
            "last_save": time.time(),
            "live": self.live,
        }
        with open(self.state_file, "w") as f:
            json.dump(s, f, indent=2, default=str)

    # ─── Market Discovery ───────────────────────────────────────────

    def fetch_markets(self) -> list:
        now = time.time()
        if now - self.markets_cache_time < 120 and self.markets_cache:
            return self.markets_cache

        all_markets = []
        for offset in range(0, 10000, 500):
            try:
                r = requests.get(GAMMA_API, params={
                    "limit": 500,
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                }, timeout=15)
                data = r.json()
                if not data or not isinstance(data, list):
                    break
                all_markets.extend([m for m in data if isinstance(m, dict)])
                if len(data) < 500:
                    break
            except Exception as e:
                logger.warning(f"Gamma fetch error at offset {offset}: {e}")
                break

        good = []
        for m in all_markets:
            try:
                prices = m.get("outcomePrices", "[]")
                clob_ids = m.get("clobTokenIds", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                if not isinstance(prices, list) or not isinstance(clob_ids, list):
                    continue
                if len(prices) < 1 or len(clob_ids) < 1:
                    continue
                yes_price = float(prices[0])
                if not (self.min_yes_price <= yes_price <= self.max_yes_price):
                    continue
                vol = float(m.get("volume24hr", 0) or 0)
                if vol < 10000:
                    continue

                # SAME-DAY FILTER: Skip markets that resolve today
                # Live games (MLB, NBA, etc) swing too fast for market making
                # EXCEPTION: Exact score markets — allow down to 2.5h before end
                # (pre-game window). Soccer matches are ~2h, so 2.5h = just before kickoff.
                # This gives us 30+ min to buy and sell pre-game with high volume.
                end_date_str = m.get("endDate", "")
                if end_date_str:
                    try:
                        from datetime import datetime, timezone
                        # Parse endDate (ISO format: 2026-07-06T22:00:00Z)
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        now_dt = datetime.now(timezone.utc)
                        hours_until_end = (end_dt - now_dt).total_seconds() / 3600
                        q_lower_check = m.get("question", "").lower()
                        is_exact_score = "exact score" in q_lower_check
                        # Exact score: allow down to 2.5h. Everything else: 6h.
                        min_hours = 2.5 if is_exact_score else 6.0
                        if hours_until_end < min_hours:
                            continue  # Skip markets ending too soon (live games)
                        # SAME-DAY LOW-PRICE FILTER: If YES price < 3c AND market ends
                        # within 24h, the outcome is almost certainly NO. Skip it.
                        # Prevents buying into dying markets like "BTC above $66K today"
                        # when BTC is already at $63K — spread looks good but it'll resolve $0.
                        # EXCEPTION: Exact score markets are exempt — low YES price is normal
                        # (most exact scores are unlikely) and the pre-game volume makes them fill.
                        if hours_until_end < 24 and not is_exact_score:
                            try:
                                mp = m.get("outcomePrices", "[]")
                                if isinstance(mp, str): mp = json.loads(mp)
                                yes_p = float(mp[0]) if mp else 0
                                if yes_p < 0.03:
                                    logger.info(f"  SKIP (same-day low-price): {m.get('question','')[:42]:42s} | YES={yes_p*100:.1f}c, ends in {hours_until_end:.1f}h")
                                    continue
                            except:
                                pass
                    except:
                        pass  # If we can't parse the date, don't skip

                # TOURNAMENT FUTURE FILTER: Block tournament winner futures
                # "Will X win the World Cup?" etc — these crash instantly when
                # a team gets eliminated in a live game. Not market-making, just
                # directional bets. The spread looks good but the underlying is binary.
                q_lower = m.get("question", "").lower()
                is_tournament_future = any(kw in q_lower for kw in [
                    "win the 2026 fifa world cup",
                    "win the 2026 world cup",
                    "win the 2027 nfl",
                    "win the 2027 super bowl",
                    "win the 2027 superbowl",
                    "win the 2026 nba",
                    "win the 2026 mlb",
                    "win the 2026 nhl",
                    "win the 2026 premier league",
                    "win the 2026 uefa",
                    "win the 2026 euros",
                    "win the 2026 copa",
                    "win the championship",
                ])
                if is_tournament_future:
                    continue

                # PRICE CRASH FILTER: Skip markets where price is crashing
                # oneHourPriceChange shows % change in last hour
                # If price dropped >15% in 1hr, it's likely a live game crash — avoid
                one_hr_change = float(m.get("oneHourPriceChange", 0) or 0)
                one_day_change = float(m.get("oneDayPriceChange", 0) or 0)
                if one_hr_change < -0.15:
                    continue  # Skip crashing markets (>15% drop in 1hr = live game crash)

                good.append({
                    "question": m.get("question", "?"),
                    "condition_id": m.get("conditionId", ""),
                    "yes_price": yes_price,
                    "no_price": float(prices[1]) if len(prices) > 1 else 1 - yes_price,
                    "yes_token_id": clob_ids[0],
                    "no_token_id": clob_ids[1] if len(clob_ids) > 1 else None,
                    "volume24hr": vol,
                    "volume1mo": float(m.get("volume1mo", 0) or 0),
                    "liquidity": float(m.get("liquidity", 0) or 0),
                    "end_date": m.get("endDate", ""),
                    "category": self._categorize(m.get("question", "")),
                    "neg_risk": m.get("negRisk", False),
                    "tick_size": str(m.get("orderPriceMinTickSize", "0.01")),
                        "one_hr_change": one_hr_change,
                        "one_day_change": one_day_change,
                        "condition_id": m.get("conditionId", ""),
                })
            except:
                continue

        good.sort(key=lambda x: x["volume24hr"], reverse=True)
        self.markets_cache = good
        self.markets_cache_time = now
        logger.info(f"Fetched {len(all_markets)} markets, {len(good)} qualify (YES {self.min_yes_price*100:.0f}-{self.max_yes_price*100:.0f}¢, vol>$10)")
        return good

    # ─── NO-Side Drift Strategy (Wallet A replica) ──────────────────────
    # Buy NO on "will X unlikely event happen?" markets at 82-96¢.
    # These events almost never happen, so NO drifts toward 100¢.
    # Target: 6¢ drift per position. Hold days/weeks. Large position sizes.
    # Wallet A made +$5,313 with this strategy across 55 positions.

    def fetch_no_drift_markets(self) -> list:
        """Find markets where NO is priced 82-96¢ — unlikely events drifting to 100¢."""
        all_markets = []
        for offset in range(0, 5000, 500):
            try:
                r = requests.get(GAMMA_API, params={
                    "limit": 500,
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                }, timeout=15)
                data = r.json()
                if not data or not isinstance(data, list):
                    break
                all_markets.extend([m for m in data if isinstance(m, dict)])
                if len(data) < 500:
                    break
            except Exception as e:
                logger.warning(f"Gamma fetch (NO drift) error at offset {offset}: {e}")
                break

        candidates = []
        for m in all_markets:
            try:
                prices = m.get("outcomePrices", "[]")
                clob_ids = m.get("clobTokenIds", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                if not isinstance(prices, list) or not isinstance(clob_ids, list):
                    continue
                if len(prices) < 2 or len(clob_ids) < 2:
                    continue

                yes_price = float(prices[0])
                no_price = float(prices[1]) if len(prices) > 1 else 1 - yes_price

                # NO must be in our target range (82-96¢)
                if not (self.no_min_price <= no_price <= self.no_max_price):
                    continue

                vol = float(m.get("volume24hr", 0) or 0)
                if vol < 50000:  # Higher volume threshold for NO drift (need liquid markets)
                    continue

                # END DATE FILTER: Need at least 7 days until resolution for drift to happen
                end_date_str = m.get("endDate", "")
                days_left = 999
                if end_date_str:
                    try:
                        from datetime import datetime, timezone
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        now_dt = datetime.now(timezone.utc)
                        days_left = (end_dt - now_dt).total_seconds() / 86400
                        if days_left < 7:
                            continue  # Need time for drift
                    except:
                        pass

                # SKIP live-game markets (sports, exact score) — NO drift is for
                # long-term "will X happen" markets, not games
                q_lower = m.get("question", "").lower()
                if any(kw in q_lower for kw in [
                    "exact score", "vs", "o/u", "over/under",
                    "wimbledon", "atp", "wta",
                ]):
                    continue

                # SKIP tournament winner futures — these can swing wildly during games
                if any(kw in q_lower for kw in [
                    "win the 2026 fifa world cup",
                    "win the 2026 world cup",
                    "win the 2027 nfl",
                    "win the 2027 super bowl",
                    "win the 2026 nba",
                    "win the 2026 mlb",
                    "win the 2026 nhl",
                    "win the championship",
                ]):
                    continue

                # SKIP price crash markets
                one_hr_change = float(m.get("oneHourPriceChange", 0) or 0)
                if one_hr_change < -0.10:  # Stricter for NO (10% crash = danger)
                    continue

                candidates.append({
                    "question": m.get("question", "?"),
                    "condition_id": m.get("conditionId", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "yes_token_id": clob_ids[0],
                    "no_token_id": clob_ids[1],
                    "volume24hr": vol,
                    "volume1mo": float(m.get("volume1mo", 0) or 0),
                    "liquidity": float(m.get("liquidity", 0) or 0),
                    "end_date": m.get("endDate", ""),
                    "category": self._categorize(m.get("question", "")),
                    "neg_risk": m.get("negRisk", False),
                    "tick_size": str(m.get("orderPriceMinTickSize", "0.01")),
                    "one_hr_change": one_hr_change,
                    "one_day_change": float(m.get("oneDayPriceChange", 0) or 0),
                    "days_left": days_left,
                    "last_trade_price": float(m.get("lastTradePrice", no_price) or no_price),
                })
            except:
                continue

        # Sort by volume (highest volume = most liquid = best drift candidates)
        candidates.sort(key=lambda x: x["volume24hr"], reverse=True)
        logger.info(f"Fetched {len(all_markets)} markets, {len(candidates)} NO-drift candidates (NO {self.no_min_price*100:.0f}-{self.no_max_price*100:.0f}¢, vol>$50K, >7d left)")
        return candidates

    def open_no_positions(self, markets: list):
        """Open NO-side drift positions — buy NO at bid, place sell at bid + drift_target."""
        # Count existing NO positions
        no_count = sum(1 for p in self.positions.values() if p.get("side") == "NO")
        slots = self.max_no_positions - no_count
        if slots <= 0:
            return

        # NO drift uses a separate budget: 40% of total cash allocated to NO, 60% to YES
        # But only if we have enough cash to make meaningful NO positions (min $50)
        no_budget = min(self.cash * 0.40, self.cash / max(1, slots))
        if no_budget < 50:
            return  # Not enough capital for NO drift yet

        # Pre-filter
        existing_no_tokens = set()
        for pos in self.positions.values():
            if pos.get("side") == "NO":
                existing_no_tokens.add(pos.get("token_id", ""))

        candidate_markets = []
        for m in markets[:100]:
            no_token = m.get("no_token_id")
            if not no_token or no_token in self.positions or no_token in existing_no_tokens:
                continue
            if no_token in self.reentry_cooldowns:
                continue
            candidate_markets.append(m)

        if not candidate_markets:
            return

        # Fetch orderbooks for NO tokens
        books = {}
        batch_size = min(len(candidate_markets), 30)
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_market = {}
            for m in candidate_markets[:30]:
                tid = m["no_token_id"]
                future = executor.submit(self.get_orderbook, tid)
                future_to_market[future] = (tid, m)
            for future in as_completed(future_to_market):
                tid, m = future_to_market[future]
                try:
                    books[tid] = future.result()
                except:
                    books[tid] = {"best_bid": None, "best_ask": None, "spread": 0,
                                  "bid_size": 0, "ask_size": 0}

        scored = []
        for m in candidate_markets[:30]:
            no_token = m["no_token_id"]
            book = books.get(no_token)
            if not book or not book["best_bid"] or not book["best_ask"]:
                continue

            bid = book["best_bid"]
            ask = book["best_ask"]
            bid_size = book["bid_size"]
            ask_size = book["ask_size"]

            if bid_size < 50 or ask_size < 50:
                continue

            # Buy NO at bid, sell at bid + drift_target (e.g. buy@90¢ sell@96¢)
            buy_price = bid
            sell_price = min(bid + self.no_drift_target, 0.99)  # Never sell above 99¢
            profit_per_share = sell_price - buy_price
            if profit_per_share < 0.02:  # Need at least 2¢ profit per share
                continue

            shares = int(no_budget / buy_price)
            if shares < 10:
                continue

            # Cap shares
            shares = min(shares, 5000)
            cost = shares * buy_price
            if cost > self.cash * 0.40:  # Never spend more than 40% of cash on one NO position
                shares = int(self.cash * 0.40 / buy_price)
                cost = shares * buy_price
                if shares < 10:
                    continue

            profit_per_rt = shares * profit_per_share
            vol = m["volume24hr"]

            # Score: profit per day (assuming drift takes ~7 days on average)
            est_days = 7.0
            profit_per_day = profit_per_rt / est_days
            vol_bonus = min(vol / 100000, 0.5)
            total_score = profit_per_day * (1 + vol_bonus)

            scored.append({
                "market": m,
                "book": book,
                "score": total_score,
                "profit_per_rt": profit_per_rt,
                "profit_per_day": profit_per_day,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "shares": shares,
                "cost": cost,
                "no_token": no_token,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)

        opened = 0
        for sm in scored:
            if opened >= slots:
                break
            if self.cash < sm["cost"]:
                continue

            m = sm["market"]
            no_token = sm["no_token"]
            buy_price = sm["buy_price"]
            sell_price = sm["sell_price"]
            shares = sm["shares"]
            cost = sm["cost"]
            tick_size = m.get("tick_size", "0.01")
            neg_risk = m.get("neg_risk", False)

            # Place LIVE buy order
            live_order_id = ""
            live_placed = False
            if self.live:
                success, order_id, response = self.clob.place_order(
                    token_id=no_token,
                    price=buy_price,
                    size=shares,
                    side="BUY",
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                )
                if success:
                    live_order_id = order_id
                    live_placed = True
                    logger.info(f"  🟢 LIVE NO BUY placed: {order_id[:16]}...")
                else:
                    logger.warning(f"  ⚠️ LIVE NO BUY failed: {response}")
                    continue

            # Create position
            self.positions[no_token] = {
                "question": m["question"],
                "token_id": no_token,
                "condition_id": m["condition_id"],
                "side": "NO",
                "strategy": "no_drift",
                "bid_price": buy_price,
                "ask_price": sell_price,
                "spread": sell_price - buy_price,
                "shares_target": shares,
                "shares_filled_buy": 0,
                "shares_filled_sell": 0,
                "cost": cost,
                "expected_profit": sm["profit_per_rt"],
                "roi_pct": (sell_price - buy_price) / buy_price * 100,
                "entry_time": time.time(),
                "status": "buying",
                "category": m["category"],
                "end_date": m["end_date"],
                "volume24hr": m["volume24hr"],
                "current_bid": buy_price,
                "current_ask": sell_price,
                "unrealized_pnl": 0,
                "score": sm["score"],
                "tick_size": tick_size,
                "neg_risk": neg_risk,
                "live_order_id": live_order_id,
                "live_placed": live_placed,
                "no_drift_target_price": sell_price,  # Target sell price
            }

            self.cash -= cost
            opened += 1

            mode_tag = "LIVE" if self.live else "PAPER"
            logger.info(
                f"  [{mode_tag}] NO-DRIFT OPEN: {m['question'][:42]:42s} | "
                f"buy NO@{buy_price*100:.1f}¢ sell@{sell_price*100:.1f}¢ | "
                f"{shares:>6} sh | ${cost:>7.2f} | profit=${sm['profit_per_rt']:.2f} | "
                f"vol=${m['volume24hr']:,.0f} | {m.get('days_left', 0):.0f}d left"
            )

        if opened > 0:
            logger.info(f"Opened {opened} NO-drift positions ({sum(1 for p in self.positions.values() if p.get('side') == 'NO')}/{self.max_no_positions})")

    def _categorize(self, q: str) -> str:
        q = q.lower()
        if any(w in q for w in ["bitcoin", "ethereum", "solana", "crypto", "xrp", "token"]):
            return "crypto"
        if any(w in q for w in ["nfl", "nba", "mlb", "nhl", "fifa", "world cup", "super bowl", "playoff"]):
            return "sports"
        if any(w in q for w in ["trump", "biden", "president", "senate", "congress", "election"]):
            return "politics"
        if any(w in q for w in ["fed", "rate", "gdp", "inflation", "recession", "cpi"]):
            return "economics"
        if any(w in q for w in ["spacex", "moon", "nasa", "ai", "gpt", "gemini", "claude", "openai"]):
            return "tech"
        return "other"

    # ─── Orderbook ──────────────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> dict:
        try:
            r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
            if r.status_code != 200:
                return {"bids": [], "asks": [], "best_bid": None, "best_ask": None, "spread": 0}

            book = r.json()
            bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
            asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]), reverse=True)  # HIGH→LOW for queue math

            best_bid = float(bids[0]["price"]) if bids else None
            best_ask_low = float(asks[-1]["price"]) if asks else None  # Lowest ask = best ask
            spread = (best_ask_low - best_bid) if (best_bid and best_ask_low) else 0

            # Total shares queued at the best ask price (our competition when selling)
            best_ask_total = 0
            if asks:
                best_ask_price = best_ask_low
                for a in asks:
                    if float(a["price"]) == best_ask_price:
                        best_ask_total += float(a["size"])

            # Total ask queue at or below our sell price (everyone ahead of us)
            # If we sell at best_ask, we're behind everyone at that price
            ask_queue_at_best = best_ask_total

            return {
                "bids": bids,
                "asks": asks,
                "best_bid": best_bid,
                "best_ask": best_ask_low,
                "spread": spread,
                "bid_size": float(bids[0]["size"]) if bids else 0,
                "ask_size": float(asks[-1]["size"]) if asks else 0,
                "ask_queue": ask_queue_at_best,  # Total shares ahead of us at best ask
            }
        except:
            return {"bids": [], "asks": [], "best_bid": None, "best_ask": None, "spread": 0,
                    "bid_size": 0, "ask_size": 0, "ask_queue": 0}

    # ─── Market Making Logic ────────────────────────────────────────

    def check_balance_changes(self):
        """Auto-detect deposit/withdrawal by comparing real balance vs tracked capital.
        If balance increased (deposit), adjust capital and position budget.
        If balance decreased significantly (withdrawal), adjust accordingly.
        """
        try:
            real_balance = self.clob.get_balance()
            # Total value = cash + cost of BUYING positions only
            # (selling positions already converted to shares, cost was spent)
            total_tracked = self.cash + sum(
                p.get("cost", 0) for p in self.positions.values()
                if p.get("status") == "buying"
            )
            
            if real_balance > self.capital + 0.50:  # Balance went up by more than $0.50
                old_capital = self.capital
                self.capital = real_balance
                self.starting_capital = real_balance
                # Don't override position_budget or max_positions — they're set by CLI args
                # Just add the deposit to our cash
                deposit_amount = real_balance - total_tracked
                if deposit_amount > 0:
                    self.cash += deposit_amount
                logger.info(
                    f"  💰 BALANCE INCREASED: ${old_capital:.2f} → ${real_balance:.2f} "
                    f"(+${real_balance - old_capital:.2f}) | "
                    f"Budget: ${self.position_budget:.2f} | "
                    f"Max positions: {self.max_positions}"
                )
            elif real_balance < total_tracked - 1.00:  # Balance dropped by more than $1
                # Withdrawal or fill happened that we didn't track
                logger.warning(
                    f"  ⚠️ BALANCE DECREASED: tracked=${total_tracked:.2f} actual=${real_balance:.2f} | "
                    f"Syncing cash to real balance"
                )
                self.capital = real_balance
                # Only subtract buying positions (selling positions already spent)
                buying_cost = sum(
                    p.get("cost", 0) for p in self.positions.values()
                    if p.get("status") == "buying"
                )
                self.cash = max(real_balance - buying_cost, 0)
        except Exception as e:
            logger.warning(f"Balance check error: {e}")

    def scan_and_trade(self):
        self.scans += 1
        
        # DYNAMIC POSITION SIZING: Recalculate budget every scan based on actual cash
        # Formula: cash / remaining_slots, capped at 6.6% of total cash (diversification guard)
        # This auto-scales: $300→$20/pos, $1K→$66/pos, $5K→$333/pos
        if self.live:
            remaining_slots = max(1, self.max_positions - len(self.positions))
            dynamic_budget = self.cash / remaining_slots
            # Cap at 6.6% of total capital to prevent over-concentration
            cap = self.capital * 0.066
            self.position_budget = min(dynamic_budget, cap)
            # Never go below $5 (minimum useful position)
            self.position_budget = max(self.position_budget, 5.0)
        
        # Phase 0: Auto-detect balance changes (deposits/withdrawals)
        if self.live and self.scans % 20 == 0:  # Check every ~3 minutes
            self.check_balance_changes()
        
        # CACHED: Only fetch markets every 2 minutes (scan 12), use cache between
        if self.scans % 12 == 1 or not hasattr(self, '_cached_markets'):
            markets = self.fetch_markets()
            self._cached_markets = markets
            self._markets_cache_time = time.time()
        else:
            markets = self._cached_markets

        # Phase 1: Check existing positions for fills
        self.check_existing_positions()

        # Phase 1b: Check re-entry cooldowns
        self.check_reentry()

        # Phase 2: Open new YES positions (only if we have YES slots AND cash)
        # NO-drift positions don't count against YES max_positions
        yes_count = sum(1 for p in self.positions.values() if p.get("side") != "NO")
        if yes_count < self.max_positions and self.cash >= self.position_budget:
            self.open_new_positions(markets)

        # Phase 2b: Open NO-drift positions (separate slot pool, separate budget)
        if self.no_drift:
            # Cache NO markets separately (refresh every 5 minutes)
            if self.scans % 30 == 1 or not hasattr(self, '_cached_no_markets'):
                no_markets = self.fetch_no_drift_markets()
                self._cached_no_markets = no_markets
            else:
                no_markets = self._cached_no_markets
            self.open_no_positions(no_markets)

        # Phase 3: Update prices for dashboard (only every 4 scans to save API calls)
        if self.scans % 4 == 0:
            self.update_prices()

        # Save state (only every 3 scans to reduce disk I/O)
        if self.scans % 3 == 0:
            self.save_state()

        active = len([p for p in self.positions.values() if p.get("status") in ("buying", "holding", "selling", "holding_no")])
        cooldowns = len([k for k in self.reentry_cooldowns.keys() if not k.endswith("_info")])
        mode = "LIVE" if self.live else "PAPER"
        logger.info(f"[{mode}] Scan #{self.scans}: {active} active, {cooldowns} cooling, "
                    f"{self.total_round_trips} round trips, "
                    f"${self.total_profit:+.2f} profit, ${self.cash:,.2f} cash")

    def open_new_positions(self, markets: list):
        # Only count YES positions for YES slot limit (NO-drift has separate pool)
        yes_count = sum(1 for p in self.positions.values() if p.get("side") != "NO")
        slots = self.max_positions - yes_count
        if slots <= 0:
            return

        # PRE-FILTER: Skip markets we already have or are cooling down (no API calls)
        # Also build a set of token_ids from our live orders to prevent duplicates
        live_order_tokens = set()
        if self.live:
            for pos in self.positions.values():
                live_order_tokens.add(pos.get("token_id", ""))
        
        candidate_markets = []
        for m in markets[:300]:
            token_id = m["yes_token_id"]
            if token_id in self.positions:
                continue
            if token_id in self.reentry_cooldowns:
                continue
            candidate_markets.append(m)

        if not candidate_markets:
            return

        # PARALLEL: Fetch orderbooks for all candidates at once (up to 20 concurrent)
        books = {}
        batch_size = min(len(candidate_markets), 80)  # Top 80 candidates by volume (already sorted)
        with ThreadPoolExecutor(max_workers=15) as executor:
            future_to_market = {}
            for m in candidate_markets[:80]:
                tid = m["yes_token_id"]
                future = executor.submit(self.get_orderbook, tid)
                future_to_market[future] = (tid, m)
            for future in as_completed(future_to_market):
                tid, m = future_to_market[future]
                try:
                    books[tid] = future.result()
                except:
                    books[tid] = {"best_bid": None, "best_ask": None, "spread": 0,
                                  "bid_size": 0, "ask_size": 0}

        scored_markets = []
        for m in candidate_markets[:80]:
            if self.cash < self.position_budget:
                break

            token_id = m["yes_token_id"]
            book = books.get(token_id)
            if not book or not book["best_bid"] or not book["best_ask"]:
                continue

            spread = book["spread"]
            if spread < self.min_spread:
                continue

            # RECENT TRADES CHECK: Skip markets with no trades in the last 60 minutes
            # If nobody has traded in the last hour, our sell order will sit forever.
            # This prevents stuck positions like Alito, Hormuz, Fed decrease.
            cid = m.get("condition_id", "")
            if cid:
                try:
                    from datetime import datetime, timezone
                    rt_resp = requests.get(f"https://data-api.polymarket.com/trades?limit=1&market={cid}", timeout=3)
                    trades = rt_resp.json()
                    if trades and isinstance(trades, list) and len(trades) > 0:
                        last_trade_ts = int(trades[0].get("timestamp", 0))
                        if last_trade_ts > 0:
                            now_ts = int(datetime.now(timezone.utc).timestamp())
                            mins_since_trade = (now_ts - last_trade_ts) / 60
                            if mins_since_trade > 60:
                                logger.info(f"  SKIP (no recent trades): {m['question'][:40]:40s} | last trade {mins_since_trade:.0f}min ago")
                                continue
                    else:
                        # No trades at all — definitely skip
                        logger.info(f"  SKIP (no trades ever): {m['question'][:40]:40s}")
                        continue
                except Exception as e:
                    pass  # If API fails, don't skip — let other filters handle it

            bid = book["best_bid"]
            ask = book["best_ask"]
            bid_size = book["bid_size"]
            ask_size = book["ask_size"]
            ask_queue = book.get("ask_queue", 0)
            vol = m["volume24hr"]

            if bid_size < 100 or ask_size < 100:
                continue

            # SELL LIQUIDITY FILTER: Estimate how long our sell order would wait
            # behind the existing ask queue. If wait > 30 min, skip this market.
            # volume24hr / 1440 = volume per minute (1440 min in a day)
            vol_per_min = vol / 1440 if vol > 0 else 0
            if vol_per_min > 0 and ask_queue > 0:
                est_sell_wait_min = ask_queue / vol_per_min
                if est_sell_wait_min > 60:
                    logger.info(f"  SKIP (sell queue): {m['question'][:40]:40s} | "
                               f"queue={int(ask_queue)} sh, wait={est_sell_wait_min:.0f}min")
                    continue

            # PRICE CRASH FILTER (re-check at scoring time)
            # If market dropped >15% in last hour, skip — live game crash
            one_hr = m.get("one_hr_change", 0)
            if one_hr < -0.15:
                logger.info(f"  SKIP (price crash): {m['question'][:40]:40s} | "
                           f"1hr change={one_hr*100:.0f}%")
                continue

            shares_1k = int(self.position_budget / bid)
            if shares_1k < 10:  # Lower threshold for live (smaller budget)
                continue
            profit_per_rt = shares_1k * spread

            # FAST-FILL SCORING: Rank by estimated profit per HOUR, not per trade.
            # A market that fills in 2 min and pays $0.20 is better than one that
            # fills in 30 min and pays $0.30 — the fast one makes $6/hr vs $0.60/hr.
            #
            # Key insight: the ask queue tells us how long our SELL will wait.
            # vol_per_min tells us the fill rate. est_sell_wait_min = how long
            # until our sell clears. Total round-trip time ≈ buy_fill + sell_wait.
            # Buy fills are usually fast (we bid at market bid), so sell_wait dominates.

            vol_per_min = vol / 1440 if vol > 0 else 0
            if vol_per_min > 0 and ask_queue > 0:
                est_sell_wait_min = max(ask_queue / vol_per_min, 1.0)
            else:
                est_sell_wait_min = 5.0  # default assumption if no data

            # Also factor in bid_size — small bid means our buy might take longer too
            est_buy_wait_min = max(shares_1k / vol_per_min, 0.5) if vol_per_min > 0 else 2.0
            est_total_min = est_buy_wait_min + est_sell_wait_min

            # Profit per hour = profit_per_rt * (60 / est_total_min)
            # e.g. $0.20 profit in 5 min = $2.40/hr; $0.30 in 30 min = $0.60/hr
            profit_per_hour = profit_per_rt * (60.0 / est_total_min)

            # Score = profit_per_hour, with a small volume bonus for reliability
            # (high-volume markets have more consistent fill rates)
            vol_bonus = min(vol / 50000, 0.3)  # up to 30% bonus for high volume
            total_score = profit_per_hour * (1 + vol_bonus)

            scored_markets.append({
                "market": m,
                "book": book,
                "score": total_score,
                "profit_per_rt": profit_per_rt,
                "profit_per_hour": profit_per_hour,
                "est_wait_min": est_total_min,
            })

        scored_markets.sort(key=lambda x: x["score"], reverse=True)

        opened = 0
        for sm in scored_markets:
            if opened >= slots:
                break
            if self.cash < self.position_budget:
                break

            m = sm["market"]
            book = sm["book"]
            token_id = m["yes_token_id"]

            bid = book["best_bid"]
            ask = book["best_ask"]
            spread = book["spread"]

            # MARKET MAKING: Buy at bid, sell at ask — capture the spread
            # We are the maker, providing liquidity. Never cross the spread.
            buy_price = bid
            sell_price = ask
            shares = int(self.position_budget / bid)
            profit_per_rt = shares * spread
            actual_spread = spread

            if shares < 10:
                continue
            # For live: cap at position_budget (already split evenly across positions)
            if self.live:
                shares = min(shares, int(self.position_budget / buy_price * 1.1))  # Small buffer for fees
            shares = min(shares, 30000)

            cost = shares * buy_price
            if cost > self.cash:
                shares = int(self.cash / buy_price)
                cost = shares * buy_price
                if shares < 10:
                    continue

            # DUST PREVENTION: Polymarket minimum sell is 5 shares.
            # If position budget can't buy at least 10 shares, skip — partial fills
            # would leave us with unsellable dust positions blocking slots.
            if shares < 10:
                continue

            expected_profit = profit_per_rt
            roi = (actual_spread / buy_price) * 100 if self.live else (spread / bid) * 100

            vol = m["volume24hr"]
            tick_size = m.get("tick_size", "0.01")
            neg_risk = m.get("neg_risk", False)

            # Place LIVE order if in live mode
            live_order_id = ""
            live_placed = False
            if self.live:
                success, order_id, response = self.clob.place_order(
                    token_id=token_id,
                    price=buy_price,
                    size=shares,
                    side="BUY",
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                )
                if success:
                    live_order_id = order_id
                    live_placed = True
                    logger.info(f"  🟢 LIVE BUY placed: {order_id[:16]}...")
                else:
                    logger.warning(f"  ⚠️ LIVE BUY failed: {response}")
                    continue  # Skip this market if order failed

            # Create position
            self.positions[token_id] = {
                "question": m["question"],
                "token_id": token_id,
                "condition_id": m["condition_id"],
                "side": "YES",
                "bid_price": buy_price,  # Actual buy price (may be bid+tick in live)
                "ask_price": sell_price,  # Actual sell price (may be ask+tick in live)
                "spread": sell_price - buy_price,
                "shares_target": shares,
                "shares_filled_buy": 0,
                "shares_filled_sell": 0,
                "cost": cost,
                "expected_profit": expected_profit,
                "roi_pct": roi,
                "entry_time": time.time(),
                "status": "buying",
                "category": m["category"],
                "end_date": m["end_date"],
                "volume24hr": vol,
                "current_bid": bid,
                "current_ask": ask,
                "unrealized_pnl": 0,
                "score": sm["score"],
                "tick_size": tick_size,
                "neg_risk": neg_risk,
                # Live trading fields
                "live_order_id": live_order_id,
                "live_placed": live_placed,
            }

            self.cash -= cost
            opened += 1

            mode_tag = "LIVE" if self.live else "PAPER"
            actual_spread = sell_price - buy_price
            logger.info(
                f"  [{mode_tag}] OPEN: {m['question'][:42]:42s} | "
                f"buy@{buy_price*100:.1f}¢ sell@{sell_price*100:.1f}¢ spread={actual_spread*100:.1f}¢ | "
                f"{shares:>6} sh | ${cost:>7.2f} | profit=${expected_profit:.2f} ({roi:.0f}%) | "
                f"vol=${vol:,.0f} score={sm['score']:.2f} ${sm.get('profit_per_hour', 0):.2f}/hr wait={sm.get('est_wait_min', 0):.0f}min"
            )

        if opened > 0:
            logger.info(f"Opened {opened} new positions ({len(self.positions)}/{self.max_positions})")

    def check_existing_positions(self):
        STALE_TIMEOUT = 7200  # 2 hours — maker orders need patience

        # BATCH: Get all open orders in ONE API call instead of N individual calls
        live_order_map = {}  # order_id → order dict
        if self.live and self.positions:
            try:
                all_orders = self.clob.get_open_orders()
                if isinstance(all_orders, list):
                    for o in all_orders:
                        oid = o.get("id", "")
                        if oid:
                            live_order_map[oid] = o
            except Exception as e:
                logger.warning(f"Batch order fetch failed: {e}")

        # PARALLEL: Fetch orderbooks for all active positions at once
        tids_to_check = [tid for tid, pos in self.positions.items()
                         if pos.get("status") in ("buying", "selling", "holding_no")]
        books = {}
        if tids_to_check:
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_tid = {executor.submit(self.get_orderbook, tid): tid
                                 for tid in tids_to_check}
                for future in as_completed(future_to_tid):
                    tid = future_to_tid[future]
                    try:
                        books[tid] = future.result()
                    except:
                        books[tid] = {"best_bid": None, "best_ask": None, "spread": 0,
                                      "bid_size": 0, "ask_size": 0}

        for tid, pos in list(self.positions.items()):
            status = pos.get("status")
            age = time.time() - pos.get("entry_time", time.time())

            # EXACT SCORE FORCED EXIT: If position is an exact score market and
            # we're within 2 hours of market end, cancel sell and dump at BID.
            # This ensures we NEVER hold during a live game — the price crashes
            # instantly when the score changes. Better to take a small loss than
            # lose everything.
            q_lower = pos.get("question", "").lower()
            if "exact score" in q_lower and status in ("buying", "selling"):
                pos_end = pos.get("end_date", "")
                if pos_end:
                    try:
                        from datetime import datetime, timezone
                        end_dt = datetime.fromisoformat(pos_end.replace("Z", "+00:00"))
                        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                        if hours_left < 2.0:
                            book = books.get(tid, self.get_orderbook(tid))
                            bid = book.get("best_bid") if book else None
                            logger.warning(
                                f"  ⚡ EXACT SCORE FORCE-EXIT: {pos['question'][:38]:38s} | "
                                f"{hours_left:.1f}h left — dumping at bid"
                            )
                            if self.live:
                                # Cancel existing order
                                if status == "selling" and pos.get("sell_order_id"):
                                    try: self.clob.cancel_order(pos["sell_order_id"])
                                    except: pass
                                if status == "buying" and pos.get("live_order_id"):
                                    try: self.clob.cancel_order(pos["live_order_id"])
                                    except: pass
                                # If we have shares, sell at bid immediately
                                shares_held = pos.get("shares_filled_buy", 0)
                                if shares_held and shares_held >= 5 and bid and bid > 0:
                                    success, order_id, response = self.clob.place_order(
                                        token_id=tid,
                                        price=bid,
                                        size=shares_held,
                                        side="SELL",
                                        tick_size=pos.get("tick_size", "0.01"),
                                        neg_risk=pos.get("neg_risk", False),
                                    )
                                    if success:
                                        pos["sell_order_id"] = order_id
                                        pos["status"] = "selling"
                                        pos["ask_price"] = bid
                                        pos["force_exited"] = True
                                        logger.info(f"     → Sell placed at bid {bid*100:.1f}¢ for {shares_held} shares")
                                elif shares_held and shares_held < 5:
                                    pos["status"] = "holding"
                                    logger.info(f"     → Below 5 shares, marking holding (dust)")
                            self.save_state()
                            continue  # Skip other checks for this position
                    except:
                        pass

            # NO-DRIFT MONITOR: Check if NO price has reached our target.
            # If best_bid >= target_price, place a sell order at target.
            # Also update unrealized PnL for dashboard.
            if status == "holding_no" and pos.get("strategy") == "no_drift":
                book = books.get(tid) or self.get_orderbook(tid)
                if book and book.get("best_bid"):
                    current_bid = book["best_bid"]
                    target = pos.get("no_drift_target_price", pos.get("ask_price", 0))
                    buy_price = pos.get("bid_price", 0)
                    shares = pos.get("shares_filled_buy", pos.get("shares_target", 0))
                    
                    # Update unrealized PnL
                    pos["current_bid"] = current_bid
                    pos["current_ask"] = book.get("best_ask", current_bid)
                    pos["unrealized_pnl"] = (current_bid - buy_price) * shares

                    # Check if we've reached the target
                    if current_bid >= target and shares >= 5:
                        logger.info(
                            f"  🎯 NO-DRIFT TARGET HIT: {pos['question'][:40]:40s} | "
                            f"buy@{buy_price*100:.1f}¢ now_bid={current_bid*100:.1f}¢ target={target*100:.1f}¢ | "
                            f"placing sell"
                        )
                        if self.live:
                            success, order_id, response = self.clob.place_order(
                                token_id=tid,
                                price=target,
                                size=shares,
                                side="SELL",
                                tick_size=pos.get("tick_size", "0.01"),
                                neg_risk=pos.get("neg_risk", False),
                            )
                            if success:
                                pos["sell_order_id"] = order_id
                                pos["status"] = "selling"
                                pos["ask_price"] = target
                                pos["entry_time"] = time.time()  # Reset for sell tracking
                                logger.info(f"  🟢 NO-DRIFT SELL placed: {order_id[:16]}...")
                            else:
                                logger.warning(f"  ⚠️ NO-DRIFT sell failed: {response}")
                        else:
                            pos["status"] = "selling"
                            pos["ask_price"] = target
                            pos["entry_time"] = time.time()
                    elif age > 86400 * 30:  # 30 days — stale, sell at current bid
                        logger.info(f"  ⏰ NO-DRIFT STALE (30d): {pos['question'][:40]:40s} | selling at current bid")
                        if self.live and shares >= 5:
                            success, order_id, response = self.clob.place_order(
                                token_id=tid,
                                price=current_bid,
                                size=shares,
                                side="SELL",
                                tick_size=pos.get("tick_size", "0.01"),
                                neg_risk=pos.get("neg_risk", False),
                            )
                            if success:
                                pos["sell_order_id"] = order_id
                                pos["status"] = "selling"
                                pos["ask_price"] = current_bid
                                pos["entry_time"] = time.time()
                continue  # Skip other checks for NO drift positions

            # SMART REPRICING: If our sell hasn't filled in 30+ minutes and the
            # market ask has dropped below our sell price, re-list at the current ask.
            # This catches situations where the market moved but our order didn't.
            # Rules:
            #   - After 30 min: reprice to current ask IF still >= buy price (keep profit)
            #   - After 2 hours: allow reprice below buy price (small loss to free capital)
            #   - Never reprice below best bid (don't fire-sale)
            REPRICE_WARN_MIN = 30
            REPRICE_LOSS_MIN = 120  # Allow taking a loss after 2h to free capital
            if status == "selling" and age > REPRICE_WARN_MIN:
                book = books.get(tid)
                if book and book.get("best_ask") and book.get("best_bid"):
                    current_ask = book["best_ask"]
                    current_bid = book["best_bid"]
                    our_sell = pos.get("ask_price", 0)
                    buy_price = pos.get("bid_price", 0)
                    shares_held = pos.get("shares_filled_buy", pos.get("shares_target", 0))
                    
                    if shares_held and shares_held >= 5 and our_sell > current_ask:
                        # Our sell is above market — it won't fill
                        can_profit = current_ask >= buy_price
                        can_loss = age > REPRICE_LOSS_MIN and current_ask >= current_bid
                        
                        if can_profit or can_loss:
                            tag = "profit" if can_profit else "loss-cap"
                            logger.info(
                                f"  📉 REPRICE {tag}: {pos['question'][:38]:38s} | "
                                f"sell {our_sell*100:.1f}¢ → {current_ask*100:.1f}¢ | "
                                f"buy@{buy_price*100:.1f}¢ | age={age/60:.0f}min"
                            )
                            if self.live:
                                # Cancel old sell
                                if pos.get("sell_order_id"):
                                    try: self.clob.cancel_order(pos["sell_order_id"])
                                    except: pass
                                # Place new sell at current ask
                                success, order_id, response = self.clob.place_order(
                                    token_id=tid,
                                    price=current_ask,
                                    size=shares_held,
                                    side="SELL",
                                    tick_size=pos.get("tick_size", "0.01"),
                                    neg_risk=pos.get("neg_risk", False),
                                )
                                if success:
                                    pos["sell_order_id"] = order_id
                                    pos["ask_price"] = current_ask
                                    pos["entry_time"] = time.time()  # Reset timer
                                    logger.info(f"     → Re-listed at {current_ask*100:.1f}¢")
                                else:
                                    logger.warning(f"  ⚠️ Reprice sell failed: {response}")
                            continue

            # AUTO-EXIT stale positions
            if status in ("buying", "selling") and age > STALE_TIMEOUT:
                book = books.get(tid, self.get_orderbook(tid))
                still_healthy = (book["best_bid"] and book["best_ask"] and
                               book["bid_size"] > 10 and book["ask_size"] > 10)

                if not still_healthy:
                    logger.info(f"  EXIT STALE: {pos['question'][:42]:42s} | age={age/60:.0f}min")
                    if self.live and pos.get("live_order_id"):
                        self.clob.cancel_order(pos["live_order_id"])
                    if status == "buying":
                        self.cash += pos["cost"]
                    elif status == "selling":
                        # Don't lose shares! Cancel stale sell, re-place at current ask
                        if self.live and pos.get("sell_order_id"):
                            self.clob.cancel_order(pos["sell_order_id"])
                        # Check if any shares actually sold before stale
                        sold_shares = pos.get("shares_filled_sell", 0)
                        if sold_shares > 0:
                            # Partial sell completed — record profit for sold portion
                            self._on_sell_filled(pos, tid, sold_shares)
                        else:
                            # No sells happened — keep position, re-place sell at current ask
                            if book["best_ask"]:
                                pos["ask_price"] = book["best_ask"]
                                pos["entry_time"] = time.time()  # Reset timer
                                logger.info(f"  🔄 RE-LISTING: {pos['question'][:40]:40s} | sell@{book['best_ask']*100:.1f}¢ (stale reset)")
                                if self.live:
                                    success, order_id, response = self.clob.place_order(
                                        token_id=tid,
                                        price=book["best_ask"],
                                        size=pos.get("shares_filled_buy", pos["shares_target"]),
                                        side="SELL",
                                        tick_size=pos.get("tick_size", "0.01"),
                                        neg_risk=pos.get("neg_risk", False),
                                    )
                                    if success:
                                        pos["sell_order_id"] = order_id
                                    else:
                                        logger.warning(f"  ⚠️ Re-list sell failed: {response}")
                                continue
                    del self.positions[tid]
                    continue
                else:
                    pos["entry_time"] = time.time()

            # Update current prices from batch-fetched orderbooks
            book = books.get(tid)
            if book and book["best_bid"]:
                pos["current_bid"] = book["best_bid"]
                pos["current_ask"] = book["best_ask"] or pos["ask_price"]

            if status == "buying":
                if self.live and pos.get("live_order_id"):
                    # Use batch-fetched order data instead of individual API call
                    order_data = live_order_map.get(pos["live_order_id"])
                    filled = False
                    partial_shares = 0
                    if order_data and isinstance(order_data, dict):
                        size_matched = float(order_data.get("size_matched", 0))
                        original_size = float(order_data.get("original_size", pos["shares_target"]))
                        order_state = order_data.get("status", "").lower()
                        if size_matched >= original_size and original_size > 0:
                            filled = True
                            partial_shares = int(size_matched)
                        elif order_state in ("matched", "filled", "closed"):
                            filled = True
                            partial_shares = int(size_matched) if size_matched > 0 else int(original_size)
                        elif size_matched > 0:
                            # PARTIAL FILL: Sell what we have, cancel the rest
                            partial_shares = int(size_matched)
                            pos["shares_filled_buy"] = partial_shares
                            logger.info(f"  📝 PARTIAL FILL: {pos['question'][:40]:40s} | "
                                       f"{partial_shares}/{int(original_size)} shares filled -> selling partial")
                            # Treat as filled with partial shares
                            filled = True
                    else:
                        # Order NOT in open orders — it may have filled or been cancelled
                        # Check individual order status via API
                        order_status = self.clob.get_order_status(pos["live_order_id"])
                        if order_status:
                            size_matched = float(order_status.get("size_matched", 0))
                            order_state = order_status.get("status", "").lower()
                            if order_state in ("matched", "filled", "closed") or size_matched > 0:
                                filled = True
                                partial_shares = int(size_matched) if size_matched > 0 else int(pos["shares_target"])
                                logger.info(f"  📝 FILL DETECTED (individual): {pos['question'][:40]:40s} | "
                                           f"{partial_shares} shares filled")
                            elif order_state in ("cancelled", "canceled", "expired"):
                                # Buy order was cancelled/expired — reclaim cash, remove ghost
                                logger.info(f"  🗑️ BUY CANCELLED: {pos['question'][:40]:40s} | reclaiming ${pos.get('cost', 0):.2f}")
                                self.cash += pos.get("cost", 0)
                                self.reentry_cooldowns[tid] = time.time() + self.reentry_cooldown_sec
                                self.reentry_cooldowns[tid + "_info"] = {
                                    "question": pos.get("question", "?"),
                                    "token_id": tid,
                                    "condition_id": pos.get("condition_id", ""),
                                    "category": pos.get("category", ""),
                                    "end_date": pos.get("end_date", ""),
                                    "volume24hr": pos.get("volume24hr", 0),
                                    "tick_size": pos.get("tick_size", "0.01"),
                                    "neg_risk": pos.get("neg_risk", False),
                                }
                                del self.positions[tid]
                                continue
                            elif order_state == "invalid":
                                # Order marked INVALID — check data API for actual shares
                                # (Polymarket sometimes marks orders INVALID but shares were delivered)
                                try:
                                    r = requests.get("https://data-api.polymarket.com/positions",
                                                   params={"user": self.clob.deposit_wallet}, timeout=10)
                                    real_positions = {str(p.get("asset", "")): p for p in r.json() if float(p.get("size", 0)) > 0}
                                    real_pos = real_positions.get(tid)
                                    if real_pos and float(real_pos.get("size", 0)) > 0:
                                        filled = True
                                        partial_shares = int(float(real_pos.get("size", 0)))
                                        logger.info(f"  📝 FILL DETECTED (data API fallback): {pos['question'][:40]:40s} | "
                                                   f"{partial_shares} shares (order was INVALID but shares exist)")
                                    else:
                                        # No shares — treat as cancelled
                                        logger.info(f"  🗑️ BUY INVALID: {pos['question'][:40]:40s} | no shares, reclaiming ${pos.get('cost', 0):.2f}")
                                        self.cash += pos.get("cost", 0)
                                        self.reentry_cooldowns[tid] = time.time() + self.reentry_cooldown_sec
                                        self.reentry_cooldowns[tid + "_info"] = {
                                            "question": pos.get("question", "?"),
                                            "token_id": tid,
                                            "condition_id": pos.get("condition_id", ""),
                                            "category": pos.get("category", ""),
                                            "end_date": pos.get("end_date", ""),
                                            "volume24hr": pos.get("volume24hr", 0),
                                            "tick_size": pos.get("tick_size", "0.01"),
                                            "neg_risk": pos.get("neg_risk", False),
                                        }
                                        del self.positions[tid]
                                        continue
                                except Exception as e:
                                    logger.warning(f"  ⚠️ Data API fallback failed: {e} — treating as cancelled")
                                    self.cash += pos.get("cost", 0)
                                    del self.positions[tid]
                                    continue
                        else:
                            # Order completely gone and no status — treat as cancelled
                            logger.info(f"  🗑️ BUY GONE: {pos['question'][:40]:40s} | no order found, reclaiming ${pos.get('cost', 0):.2f}")
                            self.cash += pos.get("cost", 0)
                            self.reentry_cooldowns[tid] = time.time() + self.reentry_cooldown_sec
                            self.reentry_cooldowns[tid + "_info"] = {
                                "question": pos.get("question", "?"),
                                "token_id": tid,
                                "condition_id": pos.get("condition_id", ""),
                                "category": pos.get("category", ""),
                                "end_date": pos.get("end_date", ""),
                                "volume24hr": pos.get("volume24hr", 0),
                                "tick_size": pos.get("tick_size", "0.01"),
                                "neg_risk": pos.get("neg_risk", False),
                            }
                            del self.positions[tid]
                            continue
                    if filled:
                        self._on_buy_filled(pos, tid, partial_shares)
                else:
                    # Paper mode — simulate fill
                    fill_prob = self._get_fill_prob(pos.get("volume24hr", 0))
                    if random.random() < fill_prob:
                        self._on_buy_filled(pos, tid, pos["shares_target"])

            elif status == "selling":
                if self.live and pos.get("sell_order_id"):
                    # Use batch-fetched order data
                    order_data = live_order_map.get(pos["sell_order_id"])
                    filled = False
                    sold = 0
                    if order_data and isinstance(order_data, dict):
                        size_matched = float(order_data.get("size_matched", 0))
                        original_size = float(order_data.get("original_size", pos["shares_target"]))
                        order_state = order_data.get("status", "").lower()
                        if size_matched >= original_size and original_size > 0:
                            filled = True
                            sold = int(size_matched)
                        elif order_state in ("matched", "filled", "closed"):
                            filled = True
                            sold = int(size_matched) if size_matched > 0 else int(original_size)
                        elif size_matched > 0:
                            pos["shares_filled_sell"] = int(size_matched)
                            logger.info(f"  📝 PARTIAL SELL FILL: {pos['question'][:40]:40s} | "
                                       f"{int(size_matched)}/{int(original_size)} shares sold")
                            # Any sell fill = complete the round trip for sold shares
                            filled = True
                            sold = int(size_matched)
                    else:
                        # Sell order NOT in open orders — check individual status
                        order_status = self.clob.get_order_status(pos["sell_order_id"])
                        if order_status:
                            size_matched = float(order_status.get("size_matched", 0))
                            order_state = order_status.get("status", "").lower()
                            if order_state in ("matched", "filled", "closed") or size_matched > 0:
                                filled = True
                                sold = int(size_matched) if size_matched > 0 else int(pos.get("shares_filled_buy", pos["shares_target"]))
                                logger.info(f"  📝 SELL FILL DETECTED (individual): {pos['question'][:40]:40s} | "
                                           f"{sold} shares sold")
                            elif order_state in ("cancelled", "canceled", "expired"):
                                # Sell order cancelled/expired — re-place at current ask
                                # But skip if market ends within 6 hours (let it resolve)
                                end_date = pos.get("end_date", "")
                                skip_relist = False
                                if end_date:
                                    try:
                                        from datetime import datetime, timezone
                                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                                        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                                        if hours_left < 6:
                                            skip_relist = True
                                    except:
                                        pass
                                if skip_relist:
                                    logger.info(f"  ⏸️ SELL CANCELLED, not re-listing: {pos['question'][:40]:40s} | ends soon, letting it resolve")
                                    continue
                                book = books.get(tid)
                                if book and book.get("best_ask"):
                                    current_ask = book["best_ask"]
                                    shares = pos.get("shares", 0) or pos.get("shares_filled_buy", 0) or pos.get("shares_target", 0)
                                    if not shares or shares <= 0:
                                        logger.warning(f"  ⚠️ SELL CANCELLED but no shares: {pos['question'][:40]:40s} | removing")
                                        del self.positions[tid]
                                        continue
                                    # BALANCE GUARD: verify real shares before re-listing
                                    try:
                                        api_resp = requests.get(f"https://data-api.polymarket.com/positions?user={self.clob.deposit_wallet}&sizeThreshold=0.1", timeout=5)
                                        real_positions = {p.get("asset", ""): float(p.get("size", 0)) for p in api_resp.json()}
                                        real_shares = real_positions.get(tid, 0)
                                        if real_shares < 5:
                                            logger.info(f"  ⏸️ SELL CANCELLED, marking holding: {pos['question'][:40]:40s} | real={real_shares:.2f} shares (below min)")
                                            pos["status"] = "holding"
                                            pos["shares"] = real_shares
                                            pos["shares_filled_buy"] = real_shares
                                            pos["sell_order_id"] = ""
                                            continue
                                        if abs(real_shares - shares) > 0.5:
                                            shares = real_shares
                                            pos["shares"] = real_shares
                                            pos["shares_filled_buy"] = real_shares
                                    except Exception as e:
                                        logger.debug(f"  Balance check failed: {e}")
                                    logger.warning(f"  🔄 SELL CANCELLED: {pos['question'][:40]:40s} | re-listing @ {current_ask*100:.1f}c")
                                    success, order_id, response = self.clob.place_order(
                                        token_id=tid,
                                        price=current_ask,
                                        size=shares,
                                        side="SELL",
                                        tick_size=pos.get("tick_size", "0.01"),
                                        neg_risk=pos.get("neg_risk", False),
                                    )
                                    if success:
                                        pos["sell_order_id"] = order_id
                                        pos["ask_price"] = current_ask
                                        logger.info(f"  🟢 RE-PLACED SELL: {order_id[:16]}... | {pos['question'][:40]:40s}")
                                    else:
                                        logger.warning(f"  ⚠️ RE-PLACE sell failed: {response}")
                        else:
                            # Order completely gone — re-place at current ask
                            # But skip if market ends within 6 hours (let it resolve)
                            end_date = pos.get("end_date", "")
                            skip_relist = False
                            if end_date:
                                try:
                                    from datetime import datetime, timezone
                                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                                    hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                                    if hours_left < 6:
                                        skip_relist = True
                                except:
                                    pass
                            if skip_relist:
                                logger.info(f"  ⏸️ SELL GONE, not re-listing: {pos['question'][:40]:40s} | ends soon, letting it resolve")
                                continue
                            book = books.get(tid)
                            if book and book.get("best_ask"):
                                current_ask = book["best_ask"]
                                shares = pos.get("shares", 0) or pos.get("shares_filled_buy", 0) or pos.get("shares_target", 0)
                                if not shares or shares <= 0:
                                    logger.warning(f"  ⚠️ SELL GONE but no shares: {pos['question'][:40]:40s} | removing")
                                    del self.positions[tid]
                                    continue
                                # BALANCE GUARD: verify real shares before re-listing
                                try:
                                    api_resp = requests.get(f"https://data-api.polymarket.com/positions?user={self.clob.deposit_wallet}&sizeThreshold=0.1", timeout=5)
                                    real_positions = {p.get("asset", ""): float(p.get("size", 0)) for p in api_resp.json()}
                                    real_shares = real_positions.get(tid, 0)
                                    if real_shares < 5:
                                        logger.info(f"  ⏸️ SELL GONE, marking holding: {pos['question'][:40]:40s} | real={real_shares:.2f} shares (below min)")
                                        pos["status"] = "holding"
                                        pos["shares"] = real_shares
                                        pos["shares_filled_buy"] = real_shares
                                        pos["sell_order_id"] = ""
                                        continue
                                    if abs(real_shares - shares) > 0.5:
                                        shares = real_shares
                                        pos["shares"] = real_shares
                                        pos["shares_filled_buy"] = real_shares
                                except Exception as e:
                                    logger.debug(f"  Balance check failed: {e}")
                                logger.warning(f"  🔄 SELL GONE: {pos['question'][:40]:40s} | re-listing @ {current_ask*100:.1f}c")
                                success, order_id, response = self.clob.place_order(
                                    token_id=tid,
                                    price=current_ask,
                                    size=shares,
                                    side="SELL",
                                    tick_size=pos.get("tick_size", "0.01"),
                                    neg_risk=pos.get("neg_risk", False),
                                )
                                if success:
                                    pos["sell_order_id"] = order_id
                                    pos["ask_price"] = current_ask
                                    logger.info(f"  🟢 RE-PLACED SELL: {order_id[:16]}... | {pos['question'][:40]:40s}")
                                else:
                                    logger.warning(f"  ⚠️ RE-PLACE sell failed: {response}")

                    if filled:
                        self._on_sell_filled(pos, tid, sold)
                elif self.live and not pos.get("sell_order_id"):
                    # SELL RECOVERY: status=selling but no sell order placed — re-place now
                    # Backoff: only try every 5 scans to avoid spamming API
                    last_attempt = pos.get("sell_recover_attempt_time", 0)
                    if time.time() - last_attempt < 50:  # 5 scans * 10s
                        continue
                    # Skip recovery if market ends within 6 hours (let it resolve naturally)
                    end_date = pos.get("end_date", "")
                    if end_date:
                        try:
                            from datetime import datetime, timezone
                            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                            if hours_left < 6:
                                logger.info(f"  ⏸️ SELL RECOVERY skipped: {pos['question'][:40]:40s} | ends in {hours_left:.1f}h, letting it resolve")
                                continue
                        except:
                            pass
                    pos["sell_recover_attempt_time"] = time.time()
                    # ── BALANCE GUARD: verify real shares before selling ──
                    # Prevents infinite "not enough balance" errors when bot state is wrong
                    shares = pos.get("shares", 0) or pos.get("shares_filled_buy", 0) or pos.get("shares_target", 0)
                    if not shares or shares <= 0:
                        continue
                    if shares < 5:
                        logger.info(f"  ⏸️ SELL RECOVERY skipped: {pos['question'][:40]:40s} | only {shares} shares, below min order size (5)")
                        continue
                    # Check real balance from data API (throttled: only on recovery attempts)
                    try:
                        api_url = f"https://data-api.polymarket.com/positions?user={self.clob.deposit_wallet}&sizeThreshold=0.1"
                        resp = requests.get(api_url, timeout=5)
                        real_positions = {p.get("asset", ""): float(p.get("size", 0)) for p in resp.json()}
                        real_shares = real_positions.get(tid, 0)
                        if real_shares < 5:
                            logger.info(f"  ⏸️ SELL RECOVERY skipped: {pos['question'][:40]:40s} | real balance only {real_shares:.2f} shares (bot thought {shares:.0f}) — marking holding")
                            pos["status"] = "holding"
                            pos["shares"] = real_shares
                            pos["shares_filled_buy"] = real_shares
                            pos["sell_order_id"] = ""
                            continue
                        if abs(real_shares - shares) > 0.5:
                            logger.info(f"  🔄 SELL RECOVERY sync: {pos['question'][:40]:40s} | bot={shares:.2f} → real={real_shares:.2f}")
                            shares = real_shares
                            pos["shares"] = real_shares
                            pos["shares_filled_buy"] = real_shares
                    except Exception as e:
                        logger.debug(f"  Balance check failed (will proceed with bot state): {e}")
                    book = books.get(tid)
                    if book and book.get("best_ask"):
                        sell_price = book["best_ask"]
                        if shares and shares > 0:
                            logger.warning(f"  🔄 SELL RECOVERY: {pos['question'][:40]:40s} | no sell order, placing @ {sell_price*100:.1f}c")
                            success, order_id, response = self.clob.place_order(
                                token_id=tid,
                                price=sell_price,
                                size=shares,
                                side="SELL",
                                tick_size=pos.get("tick_size", "0.01"),
                                neg_risk=pos.get("neg_risk", False),
                            )
                            if success:
                                pos["sell_order_id"] = order_id
                                pos["ask_price"] = sell_price
                                logger.info(f"  🟢 RECOVERY SELL placed: {order_id[:16]}... | {pos['question'][:40]:40s} | {shares} sh @ {sell_price*100:.1f}c")
                            else:
                                # If balance error, sync shares to real and mark holding if too low
                                err_str = str(response)
                                if "not enough balance" in err_str:
                                    logger.warning(f"  ⚠️ RECOVERY sell failed (balance): {pos['question'][:40]:40s} — will sync real balance next attempt")
                                else:
                                    logger.warning(f"  ⚠️ RECOVERY sell failed: {response} — will retry in 50s")
                else:
                    # Paper mode — simulate fill
                    fill_prob = self._get_fill_prob(pos.get("volume24hr", 0))
                    if random.random() < fill_prob:
                        self._on_sell_filled(pos, tid)

    def _get_fill_prob(self, vol):
        """Fill probability based on volume."""
        if vol > 50000:
            return 0.08
        elif vol > 10000:
            return 0.05
        elif vol > 1000:
            return 0.02
        elif vol > 500:
            return 0.008
        else:
            return 0.003

    def _on_buy_filled(self, pos, tid, filled_shares=None):
        """Handle buy order fill — transition to selling."""
        if filled_shares is None or filled_shares <= 0:
            filled_shares = pos["shares_target"]
        
        # Cancel remaining buy order if partial fill
        if self.live and filled_shares < pos["shares_target"] and pos.get("live_order_id"):
            try:
                self.clob.cancel_order(pos["live_order_id"])
                logger.info(f"  🧹 Cancelled remaining buy for {pos['question'][:30]} (partial fill)")
                time.sleep(2)  # Wait for cancel to process before selling
            except:
                pass
        
        pos["shares"] = filled_shares  # ← THE FIX: set shares field
        pos["shares_filled_buy"] = filled_shares
        pos["shares_target"] = filled_shares  # Update target to actual filled amount
        
        # NO DRIFT positions: Don't sell immediately — hold for drift toward target
        # The check_existing_positions loop will monitor the price and sell when
        # the NO price reaches our target (bid_price + drift_target).
        if pos.get("strategy") == "no_drift":
            pos["status"] = "holding_no"  # Special status: holding for drift
            pos["buy_fill_time"] = time.time()
            logger.info(
                f"  📈 NO-DRIFT HOLDING: {pos['question'][:42]:42s} | "
                f"{filled_shares:>6} sh @ {pos['bid_price']*100:.1f}¢ | "
                f"target sell @ {pos.get('no_drift_target_price', 0)*100:.1f}¢ | "
                f"waiting for drift"
            )
            return
        
        pos["status"] = "selling"
        pos["buy_fill_time"] = time.time()

        # Place LIVE SELL order for the shares we actually own
        if self.live:
            # Retry sell up to 3 times (balance may need time to settle after cancel)
            for attempt in range(3):
                success, order_id, response = self.clob.place_order(
                    token_id=tid,
                    price=pos["ask_price"],
                    size=filled_shares,
                    side="SELL",
                    tick_size=pos.get("tick_size", "0.01"),
                    neg_risk=pos.get("neg_risk", False),
                )
                if success:
                    pos["sell_order_id"] = order_id
                    logger.info(
                        f"  🟢 LIVE SELL placed: {order_id[:16]}... | "
                        f"{pos['question'][:40]:40s} | "
                        f"{filled_shares:>6} sh @ {pos['ask_price']*100:.1f}¢"
                    )
                    break
                else:
                    if attempt < 2:
                        logger.warning(f"  ⚠️ LIVE SELL attempt {attempt+1} failed, retrying in 3s...")
                        time.sleep(3)
                    else:
                        logger.warning(f"  ⚠️ LIVE SELL failed after 3 attempts: {response} — will retry next scan")
                        pos["sell_order_id"] = ""
        else:
            logger.info(
                f"  BUY FILLED: {pos['question'][:42]:42s} | "
                f"{filled_shares:>6} sh @ {pos['bid_price']*100:.1f}¢ | "
                f"Now selling @ {pos['ask_price']*100:.1f}¢"
            )

    def _on_sell_filled(self, pos, tid, filled_shares=None):
        """Handle sell order fill — round trip complete (or partial)."""
        if filled_shares is None or filled_shares <= 0:
            filled_shares = pos.get("shares_filled_buy", pos["shares_target"])
        
        shares = filled_shares
        total_shares_owned = pos.get("shares_filled_buy", pos["shares_target"])
        remaining_shares = total_shares_owned - shares
        
        buy_cost = shares * pos["bid_price"]
        sell_revenue = shares * pos["ask_price"]
        profit = sell_revenue - buy_cost
        roi = (profit / buy_cost) * 100 if buy_cost > 0 else 0

        self.total_profit += profit
        self.total_round_trips += 1
        self.cash += sell_revenue

        self.completed_rounds.append({
            "question": pos["question"],
            "shares": shares,
            "buy_price": pos["bid_price"],
            "sell_price": pos["ask_price"],
            "spread": pos["spread"],
            "profit": profit,
            "roi_pct": roi,
            "duration_sec": time.time() - pos.get("entry_time", time.time()),
            "timestamp": time.time(),
            "live": self.live,
        })

        mode_tag = "LIVE" if self.live else "PAPER"
        logger.info(
            f"  ✅ [{mode_tag}] ROUND TRIP: {pos['question'][:40]:40s} | "
            f"{shares:>6} sh buy@{pos['bid_price']*100:.1f}¢ sell@{pos['ask_price']*100:.1f}¢ | "
            f"PROFIT ${profit:+.2f} ({roi:.0f}%) | "
            f"#{self.total_round_trips} total"
        )

        if remaining_shares > 0:
            # PARTIAL SELL FILL — keep position alive for remaining shares
            logger.info(f"  📝 PARTIAL SELL COMPLETE: {remaining_shares} shares remaining — re-listing sell")
            pos["shares"] = remaining_shares  # ← update shares field
            pos["shares_filled_buy"] = remaining_shares
            pos["shares_target"] = remaining_shares
            pos["sell_order_id"] = ""
            pos["status"] = "selling"
            pos["entry_time"] = time.time()
            # Place new sell for remaining shares
            if self.live:
                book = self.get_orderbook(tid)
                if book and book.get("best_ask"):
                    sell_price = book["best_ask"]
                    success, order_id, response = self.clob.place_order(
                        token_id=tid,
                        price=sell_price,
                        size=remaining_shares,
                        side="SELL",
                        tick_size=pos.get("tick_size", "0.01"),
                        neg_risk=pos.get("neg_risk", False),
                    )
                    if success:
                        pos["sell_order_id"] = order_id
                        pos["ask_price"] = sell_price
                        logger.info(f"  🟢 RE-LIST remaining: {order_id[:16]}... | {remaining_shares} sh @ {sell_price*100:.1f}c")
                    else:
                        logger.warning(f"  ⚠️ RE-LIST remaining sell failed: {response} — will retry next scan")
            return  # Don't delete position — keep selling remaining shares

        # All shares sold — store market info for re-entry
        market_info = {
            "question": pos["question"],
            "token_id": tid,
            "condition_id": pos.get("condition_id", ""),
            "category": pos.get("category", ""),
            "end_date": pos.get("end_date", ""),
            "volume24hr": pos.get("volume24hr", 0),
            "tick_size": pos.get("tick_size", "0.01"),
            "neg_risk": pos.get("neg_risk", False),
            "last_sell_price": pos.get("ask_price", 0),  # for price-trend re-entry check
        }

        del self.positions[tid]

        self.reentry_cooldowns[tid] = time.time() + self.reentry_cooldown_sec
        self.reentry_cooldowns[tid + "_info"] = market_info

    def check_reentry(self):
        now = time.time()
        reentered = 0

        for tid in list(self.reentry_cooldowns.keys()):
            if tid.endswith("_info"):
                continue

            cooldown_until = self.reentry_cooldowns[tid]
            if now < cooldown_until:
                continue

            if len(self.positions) >= self.max_positions:
                break
            if self.cash < self.position_budget:
                break

            book = self.get_orderbook(tid)
            if not book["best_bid"] or not book["best_ask"]:
                del self.reentry_cooldowns[tid]
                tid_info = tid + "_info"
                if tid_info in self.reentry_cooldowns:
                    del self.reentry_cooldowns[tid_info]
                continue

            spread = book["spread"]
            if spread < self.min_spread:
                del self.reentry_cooldowns[tid]
                tid_info = tid + "_info"
                if tid_info in self.reentry_cooldowns:
                    del self.reentry_cooldowns[tid_info]
                continue

            bid = book["best_bid"]
            ask = book["best_ask"]

            # PRICE TREND CHECK: Don't re-enter if price is falling
            # Compare current bid to the price we last sold at.
            # If bid < last sell price, the market is dropping — skip re-entry.
            tid_info = tid + "_info"
            market_info = self.reentry_cooldowns.get(tid_info, {})
            last_sell = market_info.get("last_sell_price", 0)
            if last_sell > 0 and bid < last_sell:
                logger.info(f"  SKIP (price falling): {market_info.get('question', '?')[:40]:40s} | "
                           f"last sell={last_sell*100:.1f}c, now bid={bid*100:.1f}c")
                del self.reentry_cooldowns[tid]
                if tid_info in self.reentry_cooldowns:
                    del self.reentry_cooldowns[tid_info]
                continue

            shares = int(self.position_budget / bid)
            if shares < 10:
                del self.reentry_cooldowns[tid]
                tid_info = tid + "_info"
                if tid_info in self.reentry_cooldowns:
                    del self.reentry_cooldowns[tid_info]
                continue
            if self.live:
                shares = min(shares, int(self.position_budget / bid * 1.1))
            shares = min(shares, 30000)

            cost = shares * bid
            if cost > self.cash:
                shares = int(self.cash / bid)
                cost = shares * bid
                if shares < 10:
                    continue

            expected_profit = shares * spread
            roi = (spread / bid) * 100

            tick_size = market_info.get("tick_size", "0.01")
            neg_risk = market_info.get("neg_risk", False)

            # Place LIVE order if in live mode
            live_order_id = ""
            if self.live:
                success, order_id, response = self.clob.place_order(
                    token_id=tid,
                    price=bid,
                    size=shares,
                    side="BUY",
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                )
                if success:
                    live_order_id = order_id
                else:
                    logger.warning(f"  ⚠️ RE-ENTRY BUY failed: {response}")
                    del self.reentry_cooldowns[tid]
                    if tid_info in self.reentry_cooldowns:
                        del self.reentry_cooldowns[tid_info]
                    continue

            self.positions[tid] = {
                "question": market_info.get("question", "?"),
                "token_id": tid,
                "condition_id": market_info.get("condition_id", ""),
                "side": "YES",
                "bid_price": bid,
                "ask_price": ask,
                "spread": spread,
                "shares_target": shares,
                "shares_filled_buy": 0,
                "shares_filled_sell": 0,
                "cost": cost,
                "expected_profit": expected_profit,
                "roi_pct": roi,
                "entry_time": time.time(),
                "status": "buying",
                "category": market_info.get("category", ""),
                "end_date": market_info.get("end_date", ""),
                "volume24hr": market_info.get("volume24hr", 0),
                "current_bid": bid,
                "current_ask": ask,
                "unrealized_pnl": 0,
                "reentry": True,
                "tick_size": tick_size,
                "neg_risk": neg_risk,
                "live_order_id": live_order_id,
                "live_placed": bool(live_order_id),
            }

            self.cash -= cost
            reentered += 1

            mode_tag = "LIVE" if self.live else "PAPER"
            logger.info(
                f"  [{mode_tag}] 🔄 RE-ENTER: {market_info.get('question', '?')[:42]:42s} | "
                f"buy@{bid*100:.1f}¢ sell@{ask*100:.1f}¢ spread={spread*100:.1f}¢ | "
                f"{shares:>6} sh | ${cost:>7.2f} | profit=${expected_profit:.2f} ({roi:.0f}%)"
            )

            del self.reentry_cooldowns[tid]
            if tid_info in self.reentry_cooldowns:
                del self.reentry_cooldowns[tid_info]

        if reentered > 0:
            logger.info(f"Re-entered {reentered} markets after cooldown")

    def update_prices(self):
        for tid, pos in self.positions.items():
            if pos.get("status") in ("buying", "selling"):
                if pos.get("status") == "selling" and pos.get("shares_filled_buy") not in (None, 0, "0", ""):
                    shares = pos["shares_filled_buy"]
                    current_bid = pos.get("current_bid") or pos.get("bid_price", 0)
                    current_value = shares * current_bid
                    cost = shares * pos.get("bid_price", 0)
                    pos["unrealized_pnl"] = current_value - cost

    # ─── Main Loop ──────────────────────────────────────────────────

    def run(self):
        mode = "LIVE 🔴" if self.live else "PAPER 📊"
        logger.info("=" * 70)
        logger.info(f"YES-SIDE MARKET MAKER BOT — {mode}")
        logger.info(f"Capital: ${self.starting_capital:,.2f} | Budget: ${self.position_budget:.2f}/position")
        logger.info(f"YES range: {self.min_yes_price*100:.0f}¢-{self.max_yes_price*100:.0f}¢ | Min spread: {self.min_spread*100:.1f}¢")
        logger.info(f"Max positions: {self.max_positions} | Scan: every {self.scan_interval}s")
        logger.info(f"State: {self.state_file}")
        logger.info("=" * 70)

        # LIVE: Cancel any leftover BUY orders from previous run (keep SELL orders!)
        if self.live:
            try:
                orders = self.clob.get_open_orders()
                buy_count = 0
                for o in orders:
                    side = o.get("side", "").upper()
                    if side == "BUY":
                        self.clob.cancel_order(o.get("id", ""))
                        buy_count += 1
                if buy_count:
                    logger.info(f"🧹 Cancelled {buy_count} leftover BUY orders (kept SELL orders)")
                    time.sleep(1)
                else:
                    logger.info(f"No leftover BUY orders to cancel ({len(orders)} SELL orders kept)")
            except Exception as e:
                logger.warning(f"Could not clean up orders: {e}")

            # SYNC: Adopt any orphaned positions from data API
            logger.info("🔄 Syncing with data API to find orphaned positions...")
            self.sync_real_positions()

        end_time = time.time() + self.duration
        while time.time() < end_time:
            try:
                self.scan_and_trade()
            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
                time.sleep(5)

            remaining = self.scan_interval - (time.time() % self.scan_interval)
            if remaining < 1:
                remaining = self.scan_interval
            time.sleep(remaining)

        self.save_state()
        logger.info(f"Bot stopped. {self.total_round_trips} round trips, ${self.total_profit:+.2f} profit")


def main():
    parser = argparse.ArgumentParser(description="Polymarket YES-Side Market Maker")
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--position-budget", type=float, default=1_000)
    parser.add_argument("--max-positions", type=int, default=50)
    parser.add_argument("--min-yes-price", type=float, default=0.01)
    parser.add_argument("--max-yes-price", type=float, default=0.20)
    parser.add_argument("--min-spread", type=float, default=0.001)
    parser.add_argument("--scan-interval", type=int, default=15)
    parser.add_argument("--duration", type=int, default=86400)
    parser.add_argument("--reset", action="store_true")
    # Live trading args
    parser.add_argument("--live", action="store_true", help="LIVE mode — place real orders")
    parser.add_argument("--private-key", default=os.environ.get("POLY_PRIVATE_KEY", ""), help="Signer private key")
    parser.add_argument("--deposit-wallet", default=os.environ.get("POLY_DEPOSIT_WALLET", ""), help="Deposit wallet address (funder)")
    parser.add_argument("--builder-code", default=os.environ.get("POLY_BUILDER_CODE", ""), help="Builder code for order attribution")
    # NO-side drift strategy args
    parser.add_argument("--no-drift", action="store_true", help="Enable NO-side drift strategy (Wallet A replica)")
    parser.add_argument("--max-no-positions", type=int, default=10, help="Max NO-drift positions")
    parser.add_argument("--no-min-price", type=float, default=0.82, help="Min NO price to buy (82¢)")
    parser.add_argument("--no-max-price", type=float, default=0.96, help="Max NO price to buy (96¢)")
    parser.add_argument("--no-drift-target", type=float, default=0.06, help="Target drift profit per share (6¢)")

    args = parser.parse_args()

    bot = YesMarketMaker(
        capital=args.capital,
        position_budget=args.position_budget,
        max_positions=args.max_positions,
        min_yes_price=args.min_yes_price,
        max_yes_price=args.max_yes_price,
        min_spread=args.min_spread,
        scan_interval=args.scan_interval,
        duration=args.duration,
        reset=args.reset,
        live=args.live,
        private_key=args.private_key,
        deposit_wallet=args.deposit_wallet,
        builder_code=args.builder_code,
        no_drift=args.no_drift,
        max_no_positions=args.max_no_positions,
        no_min_price=args.no_min_price,
        no_max_price=args.no_max_price,
        no_drift_target=args.no_drift_target,
    )
    bot.run()


if __name__ == "__main__":
    main()
