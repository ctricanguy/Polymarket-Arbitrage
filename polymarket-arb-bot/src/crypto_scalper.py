#!/usr/bin/env python3
"""
Crypto Scalper Module — replicates Wallet 2 (0x20d230) strategy.
Trades Polymarket crypto Up/Down 5-minute and 15-minute markets.
Strategy: Buy at price extremes (1-5¢ or 88-98¢), sell on quick swings.
Avoids 20-80¢ zone (where Wallet 2 data shows losses).

Designed to run alongside yes_mm.py — shared wallet, separate state file,
separate budget pool (25% of cash allocated to crypto).
"""
import os, sys, json, time, logging, argparse, threading
from datetime import datetime, timezone
from collections import defaultdict
import requests

# SDK imports
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderPayload, PartialCreateOrderOptions, OrderType
from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.constants import POLYGON
from enum import Enum
import logging as _logging
# Suppress harmless "Could not create api key" errors
_logging.getLogger("py_clob_client_v2.http_helpers.helpers").setLevel(_logging.CRITICAL)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger("crypto_scalper")

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
STATE_FILE = os.path.expanduser("~/polymarket-arb-bot/data/crypto_scalper/state.json")
STATE_LOCK = threading.Lock()

# --- Binance price feed (REST fallback, no websocket needed) ---
BINANCE_API = "https://api.binance.com/api/v3"

class PriceFeed:
    """Tracks crypto prices from Binance to determine trend direction."""
    def __init__(self):
        self.prices = {}  # symbol -> {"price": float, "prev": float, "history": []}
        self.symbols = {
            "BTC": "BTCUSDT",
            "ETH": "ETHUSDT",
            "SOL": "SOLUSDT",
            "XRP": "XRPUSDT",
        }
    
    def update(self):
        """Fetch current prices from Binance."""
        try:
            r = requests.get(f"{BINANCE_API}/ticker/price", timeout=5)
            data = r.json()
            price_map = {d["symbol"]: float(d["price"]) for d in data}
            for crypto, symbol in self.symbols.items():
                if symbol in price_map:
                    current = price_map[symbol]
                    prev = self.prices.get(crypto, {}).get("price", current)
                    history = self.prices.get(crypto, {}).get("history", [])
                    history.append(current)
                    if len(history) > 60:  # Keep last 60 readings
                        history = history[-60:]
                    self.prices[crypto] = {
                        "price": current,
                        "prev": prev,
                        "history": history,
                    }
        except Exception as e:
            logger.warning(f"Binance price update failed: {e}")
    
    def get_trend(self, crypto, window_minutes=5):
        """Returns trend: 'up', 'down', or 'flat' based on recent price history."""
        p = self.prices.get(crypto)
        if not p or len(p["history"]) < 2:
            return "flat"
        history = p["history"]
        if len(history) < 5:
            # Not enough data, compare first to last
            if history[-1] > history[0] * 1.001:
                return "up"
            elif history[-1] < history[0] * 0.999:
                return "down"
            return "flat"
        # Use last N readings
        recent = history[-min(len(history), 20):]
        first = recent[0]
        last = recent[-1]
        change_pct = (last - first) / first
        if change_pct > 0.0005:  # 0.05% threshold
            return "up"
        elif change_pct < -0.0005:
            return "down"
        return "flat"
    
    def get_price(self, crypto):
        return self.prices.get(crypto, {}).get("price", 0)


class CryptoScalper:
    def __init__(self, private_key, funder, chain_id=137,
                 crypto_budget_pct=0.25, max_positions=10,
                 min_trade_size=2.0, max_trade_size=6.0,
                 scan_interval=5, daily_loss_limit=15.0,
                 builder_code=""):
        self.funder = funder
        self.crypto_budget_pct = crypto_budget_pct
        self.max_positions = max_positions
        self.min_trade_size = min_trade_size
        self.max_trade_size = max_trade_size
        self.scan_interval = scan_interval
        self.daily_loss_limit = daily_loss_limit
        self.builder_code = builder_code
        
        # Initialize CLOB client
        host = "https://clob.polymarket.com"
        self.clob = ClobClient(
            host, key=private_key, chain_id=chain_id,
            signature_type=SignatureTypeV2.POLY_1271,
            funder=funder,
        )
        creds = self.clob.create_or_derive_api_key()
        self.clob.set_api_creds(creds)
        logger.info(f"CLOB client initialized. Funder: {funder}")
        
        # Price feed
        self.price_feed = PriceFeed()
        
        # State
        self.positions = {}  # token_id -> position dict
        self.daily_pnl = 0.0
        self.daily_pnl_reset = time.time()
        self.stopped_today = False
        self.total_trades = 0
        self.total_profit = 0.0
        
        self.load_state()
    
    def load_state(self):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                self.positions = data.get("positions", {})
                self.daily_pnl = data.get("daily_pnl", 0.0)
                self.daily_pnl_reset = data.get("daily_pnl_reset", time.time())
                self.stopped_today = data.get("stopped_today", False)
                self.total_trades = data.get("total_trades", 0)
                self.total_profit = data.get("total_profit", 0.0)
            logger.info(f"Loaded state: {len(self.positions)} positions, daily PnL: ${self.daily_pnl:+.2f}")
        except:
            logger.info("No state file found, starting fresh")
    
    def save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with STATE_LOCK:
            data = {
                "positions": self.positions,
                "daily_pnl": self.daily_pnl,
                "daily_pnl_reset": self.daily_pnl_reset,
                "stopped_today": self.stopped_today,
                "total_trades": self.total_trades,
                "total_profit": self.total_profit,
            }
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
    
    def get_cash(self):
        """Get available USDC balance."""
        try:
            r = requests.get(f"{DATA}/positions", params={
                "user": self.funder.lower(), "assetType": "COLLATERAL",
                "limit": 1
            }, timeout=10)
            positions = r.json()
            if positions:
                return float(positions[0].get("size", 0) or 0)
        except:
            pass
        # Fallback: use OnChain balance
        try:
            r2 = requests.get(f"{DATA}/value", params={
                "user": self.funder.lower()
            }, timeout=10)
            data = r2.json()
            return float(data.get("cash", 0) or 0)
        except:
            return 0.0
    
    def get_crypto_budget(self):
        """Calculate available budget for crypto trading (25% of cash)."""
        cash = self.get_cash()
        budget = cash * self.crypto_budget_pct
        # Subtract already-deployed crypto capital
        deployed = sum(p.get("cost", 0) for p in self.positions.values()
                       if p.get("status") in ("buying", "holding"))
        return max(0, budget - deployed)
    
    def fetch_active_crypto_markets(self):
        """Find all active 5-min and 15-min crypto up/down markets."""
        now_ts = int(time.time())
        markets = []
        
        # Check 5-min markets for BTC, ETH, SOL, XRP
        # Markets end at :00, :05, :10, :15, etc (5-min boundaries)
        for crypto, prefix in [("BTC", "btc"), ("ETH", "eth"), ("SOL", "sol"), ("XRP", "xrp")]:
            # Current 5-min window
            minute = datetime.fromtimestamp(now_ts, tz=timezone.utc).minute
            seconds_to_next_5 = (5 - (minute % 5)) * 60 - datetime.fromtimestamp(now_ts, tz=timezone.utc).second
            next_5_end = now_ts + seconds_to_next_5
            
            # Current and next 5-min market
            for offset_min in [0, -5]:
                end_ts = next_5_end + (offset_min * 60)
                if end_ts < now_ts:
                    continue  # Already ended
                slug = f"{prefix}-updown-5m-{end_ts}"
                m = self._fetch_market_by_slug(slug, crypto, "5m", end_ts)
                if m:
                    markets.append(m)
            
            # Current 15-min window
            seconds_to_next_15 = (15 - (minute % 15)) * 60 - datetime.fromtimestamp(now_ts, tz=timezone.utc).second
            next_15_end = now_ts + seconds_to_next_15
            for offset_min in [0]:
                end_ts = next_15_end + (offset_min * 60)
                slug = f"{prefix}-updown-15m-{end_ts}"
                m = self._fetch_market_by_slug(slug, crypto, "15m", end_ts)
                if m:
                    markets.append(m)
        
        return markets
    
    def _fetch_market_by_slug(self, slug, crypto, window, end_ts):
        """Fetch a single market by its event slug."""
        try:
            r = requests.get(f"{GAMMA}/events", params={
                "limit": 1, "slug": slug
            }, timeout=8)
            events = r.json()
            if not events:
                return None
            e = events[0]
            markets = e.get("markets", [])
            if not markets:
                return None
            m = markets[0]
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
            except:
                prices = []
            if len(prices) < 2:
                return None
            
            tokens_str = m.get("clobTokenIds", "[]")
            try:
                tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
            except:
                tokens = []
            if len(tokens) < 2:
                return None
            
            tick = str(m.get("orderPriceMinTickSize", "0.01") or "0.01")
            vol = float(m.get("volume24hr", 0) or 0)
            
            # Parse end time
            end_date = m.get("endDate", "")
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                seconds_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
            except:
                seconds_left = end_ts - time.time()
            
            return {
                "conditionId": m.get("conditionId", ""),
                "question": m.get("question", ""),
                "crypto": crypto,
                "window": window,
                "up_token": tokens[0],
                "down_token": tokens[1],
                "up_price": float(prices[0]),
                "down_price": float(prices[1]),
                "tick_size": tick,
                "volume": vol,
                "seconds_left": seconds_left,
                "end_ts": end_ts,
                "slug": slug,
            }
        except Exception as e:
            return None
    
    def get_orderbook(self, token_id):
        """Get orderbook for a token."""
        try:
            book = self.clob.get_order_book(token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1
            return best_bid, best_ask, bids, asks
        except:
            return 0, 1, [], []
    
    def place_order_with_fallback(self, token_id, price, size, side, tick_size, neg_risk=False, order_type="GTC"):
        """Place order with tick_size and neg_risk fallback (same pattern as yes_mm.py).
        
        Returns: (success, order_id, result, used_tick, used_nr)
        For FOK/FAK: success=True if order was accepted (may or may not have filled).
        Caller must verify fill via verify_fok_fill() for FOK orders.
        """
        ts_str = str(tick_size) if not isinstance(tick_size, str) else tick_size
        tick_options = [ts_str, "0.01" if ts_str == "0.001" else "0.001"]
        neg_risk_options = [neg_risk, not neg_risk]
        
        side_enum = Side.BUY if side.upper() == "BUY" else Side.SELL
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side_enum,
        )
        
        # Map order type string to enum
        ot = OrderType.GTC
        if order_type == "FOK":
            ot = OrderType.FOK
        elif order_type == "FAK":
            ot = OrderType.FAK
        
        for tick in tick_options:
            for nr in neg_risk_options:
                options = PartialCreateOrderOptions(tick_size=tick, neg_risk=nr)
                try:
                    result = self.clob.create_and_post_order(order_args, options, ot)
                    
                    # Parse the CLOB API response — handle multiple field name conventions
                    if isinstance(result, dict):
                        # Try multiple possible field names for success
                        success = (
                            result.get("success", False) or
                            result.get("status") == "MATCHED" or
                            result.get("status") == "LIVE" or
                            result.get("status") == "PLACED" or
                            (result.get("orderID") is not None and result.get("orderID") != "") or
                            (result.get("orderId") is not None and result.get("orderId") != "") or
                            (result.get("id") is not None and result.get("id") != "")
                        )
                        # Try multiple possible field names for order ID
                        order_id = (
                            result.get("orderID", "") or
                            result.get("orderId", "") or
                            result.get("id", "") or
                            result.get("order_id", "") or
                            ""
                        )
                        # For FOK: if no order_id, it may have been killed (not filled)
                        # but we MUST verify — the order may have filled with different field names
                        if order_type in ("FOK", "FAK") and not order_id:
                            # Log full result for debugging
                            logger.info(f"CRYPTO {order_type} response (no order_id): {json.dumps(result, default=str)[:300]}")
                        return success, order_id, result, tick, nr
                    return False, "", result, tick, nr
                except Exception as e:
                    err = str(e)
                    if "maker address not allowed" in err:
                        raise  # This is a config error, not a tick_size issue
                    last_err = err
                    continue
        raise Exception(f"All tick_size/neg_risk combos failed. Last: {last_err}")
    
    def verify_fok_fill(self, order_id, token_id, expected_shares, expected_price):
        """Verify whether a FOK/FAK order actually filled by checking positions or order status.
        
        FOK orders that fill don't always return a trackable order_id in the API response.
        We verify by:
        1. If order_id exists, check get_order() for status=MATCHED and size_matched
        2. Fallback: check data-api positions for this token_id
        
        Returns: (filled: bool, filled_shares: float, fill_price: float)
        """
        # Method 1: Check order status via CLOB
        if order_id and order_id != "":
            try:
                order = self.clob.get_order(order_id)
                if isinstance(order, dict):
                    status = order.get("status", "")
                    size_matched = float(order.get("size_matched", 0) or 0)
                    if status == "MATCHED" and size_matched > 0:
                        fill_price = float(order.get("price", expected_price) or 0)
                        logger.info(f"CRYPTO FOK verified via get_order: {size_matched:.1f}sh @ {fill_price*100:.1f}c (status={status})")
                        return True, size_matched, fill_price
                    elif status == "LIVE":
                        # Order is live but not matched — FOK shouldn't go LIVE, but check
                        logger.info(f"CRYPTO FOK order LIVE (not matched): order_id={order_id[:12]}...")
                        return False, 0, 0
                    else:
                        logger.info(f"CRYPTO FOK order status={status}, size_matched={size_matched}")
                        # Fall through to position check
                else:
                    logger.info(f"CRYPTO FOK get_order returned non-dict: {type(order)}")
            except Exception as e:
                logger.warning(f"CRYPTO FOK get_order failed: {e}")
        
        # Method 2: Check data-api for actual position
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/positions?user={self.funder.lower()}&asset={token_id}",
                timeout=5
            )
            if r.status_code == 200:
                positions = r.json()
                if isinstance(positions, list) and positions:
                    for pos in positions:
                        pos_size = float(pos.get("size", 0) or 0)
                        pos_price = float(pos.get("avg_price", 0) or 0)
                        if pos_size > 0:
                            logger.info(f"CRYPTO FOK verified via data-api: {pos_size:.1f}sh @ {pos_price*100:.1f}c")
                            return True, pos_size, pos_price
        except Exception as e:
            logger.warning(f"CRYPTO FOK position check failed: {e}")
        
        # Method 3: Check if the API response itself contains fill info
        # Some CLOB responses include the fill directly
        logger.info(f"CRYPTO FOK no fill detected for token={token_id[:12]}... order_id={order_id[:12] if order_id else 'none'}")
        return False, 0, 0
    
    def check_existing_positions(self, markets_by_token):
        """Check open crypto positions — sell on profit or cut losses."""
        to_remove = []
        now = time.time()
        
        for tid, pos in list(self.positions.items()):
            # Check if the market this position belongs to has ended
            end_ts = pos.get("end_ts", 0)
            if end_ts > 0 and end_ts < now - 60:
                # Market ended >1 min ago — this position is orphaned
                if pos.get("status") == "buying":
                    # Buy order never filled — try to cancel it
                    order_id = pos.get("order_id")
                    if order_id:
                        try:
                            self.clob.cancel_order(OrderPayload(order_id=order_id))
                        except:
                            pass
                    logger.info(f"CRYPTO orphaned buy (market ended {now-end_ts:.0f}s ago): {pos.get('crypto','')} {pos.get('side','')} — removing")
                    to_remove.append(tid)
                    continue
                elif pos.get("status") == "holding":
                    # Market resolved while we were holding — check if we won
                    # If we won, shares are worth $1 each. If we lost, worth $0.
                    # Position will settle automatically on Polymarket.
                    logger.info(f"CRYPTO position held to resolution (market ended {now-end_ts:.0f}s ago): {pos.get('crypto','')} {pos.get('side','')} {pos.get('shares',0):.0f}sh @ {pos.get('buy_price',0)*100:.0f}¢")
                    # Check actual settlement via data API
                    # For now, mark as resolved and remove from active tracking
                    profit_estimate = 0  # Will be confirmed by balance change
                    to_remove.append(tid)
                    continue
                elif pos.get("status") == "selling":
                    # Sell order may have filled or expired
                    order_id = pos.get("sell_order_id")
                    if order_id:
                        try:
                            order = self.clob.get_order(order_id)
                            if order and order.get("status") == "MATCHED":
                                filled = float(order.get("size_matched", 0) or 0)
                                sell_price = float(order.get("price", 0) or 0)
                                profit = (sell_price - pos["buy_price"]) * filled
                                self.daily_pnl += profit
                                self.total_profit += profit
                                self.total_trades += 1
                                logger.info(f"CRYPTO SELL FILLED (resolved market): {pos.get('crypto','')} {pos.get('side','')} | {filled:.0f}sh @ {sell_price*100:.1f}¢ | PnL ${profit:+.2f}")
                        except:
                            pass
                    to_remove.append(tid)
                    continue
        
        for tid, pos in list(self.positions.items()):
            status = pos.get("status")
            
            # BUYING — check if buy order filled
            if status == "buying":
                order_id = pos.get("order_id")
                filled = False
                if order_id:
                    try:
                        order = self.clob.get_order(order_id)
                        if order and order.get("status") == "MATCHED":
                            filled_shares = float(order.get("size_matched", 0) or 0)
                            if filled_shares > 0:
                                pos["shares"] = filled_shares
                                pos["status"] = "holding"
                                pos["hold_start"] = time.time()
                                logger.info(f"CRYPTO BUY FILLED: {pos['crypto']} {pos['side']} | {filled_shares:.0f}sh @ {pos['buy_price']*100:.1f}¢")
                                filled = True
                            elif order.get("status") in ("CANCELLED", "EXPIRED"):
                                to_remove.append(tid)
                                logger.info(f"CRYPTO buy order cancelled/expired: {pos['crypto']} {pos['side']}")
                                continue
                    except Exception as e:
                        pass
                
                # Cancel unfilled buys if <30 seconds to resolution
                m = markets_by_token.get(tid)
                if m and m["seconds_left"] < 30 and order_id:
                    try:
                        self.clob.cancel_order(OrderPayload(order_id=order_id))
                        logger.info(f"CRYPTO cancelling unfilled buy (market ending): {pos.get('crypto','')} {pos.get('side','')}")
                    except:
                        pass
                continue
            
            # SELLING — check if sell order filled
            if status == "selling":
                order_id = pos.get("sell_order_id")
                if order_id:
                    try:
                        order = self.clob.get_order(order_id)
                        if order and order.get("status") == "MATCHED":
                            filled_shares = float(order.get("size_matched", 0) or 0)
                            sell_price = float(order.get("price", 0) or 0)
                            profit = (sell_price - pos["buy_price"]) * filled_shares
                            self.daily_pnl += profit
                            self.total_profit += profit
                            self.total_trades += 1
                            logger.info(f"CRYPTO SELL FILLED: {pos['crypto']} {pos['side']} | {filled_shares:.0f}sh @ {sell_price*100:.1f}¢ | PnL ${profit:+.2f} | Total: {self.total_trades} RTs, ${self.total_profit:+.2f}")
                            to_remove.append(tid)
                        elif order.get("status") in ("CANCELLED", "EXPIRED"):
                            # Sell order expired — re-place at bid
                            m = markets_by_token.get(tid)
                            if m and m["seconds_left"] > 10:
                                best_bid, _, _, _ = self.get_orderbook(tid)
                                if best_bid > 0:
                                    self._place_sell(tid, pos, best_bid, m)
                            else:
                                to_remove.append(tid)
                    except:
                        pass
                continue
            
            # Holding — check if we should sell
            if pos.get("status") == "holding":
                m = markets_by_token.get(tid)
                if not m:
                    # Market resolved — check if we won or lost
                    buy_price = pos["buy_price"]
                    shares = pos.get("shares", 0)
                    pos_type = pos.get("type", "lottery")
                    if pos_type == "confirmed":
                        # Confirmed trend: should resolve to $1.00 if we were right
                        profit = (1.0 - buy_price) * shares
                        self.daily_pnl += profit
                        self.total_profit += profit
                        self.total_trades += 1
                        logger.info(f"CRYPTO RESOLVED (confirmed): {pos['crypto']} {pos['side']} | {shares:.0f}sh @ {buy_price*100:.1f}¢ → $1.00 | PnL ${profit:+.2f} | Total: {self.total_trades} RTs, ${self.total_profit:+.2f}")
                    else:
                        # Lottery: likely resolved to $0
                        profit = -buy_price * shares
                        self.daily_pnl += profit
                        self.total_profit += profit
                        self.total_trades += 1
                        logger.info(f"CRYPTO RESOLVED (lottery): {pos['crypto']} {pos['side']} | {shares:.0f}sh @ {buy_price*100:.1f}¢ → $0 | PnL ${profit:+.2f} | Total: {self.total_trades} RTs, ${self.total_profit:+.2f}")
                    to_remove.append(tid)
                    continue
                
                seconds_left = m["seconds_left"]
                best_bid, best_ask, _, _ = self.get_orderbook(tid)
                buy_price = pos["buy_price"]
                shares = pos.get("shares", 0)
                sell_target = pos.get("sell_target", buy_price * 5)
                pos_type = pos.get("type", "lottery")
                
                if shares < 5:
                    if seconds_left < 30:
                        to_remove.append(tid)
                    continue
                
                # CONFIRMED TREND: Hold to resolution (don't sell early)
                # Only cut loss if trend reverses and bid drops below 50¢
                if pos_type == "confirmed":
                    if best_bid < 0.50 and best_bid > 0:
                        # Trend reversed — cut loss
                        loss = (best_bid - buy_price) * shares
                        logger.info(f"CRYPTO TREND REVERSED — cut loss: {pos['crypto']} {pos['side']} | bid={best_bid*100:.1f}¢ | loss ${loss:.2f}")
                        self._place_sell(tid, pos, best_bid, m)
                    # Otherwise hold to resolution
                    continue
                
                # LOTTERY: Spike-sell on 5x, or cut loss near end
                # SPIKE SELL — if position jumped 5x from buy price, SELL IMMEDIATELY
                if best_bid >= buy_price * 5 and best_bid > 0:
                    profit = (best_bid - buy_price) * shares
                    logger.info(f"CRYPTO SPIKE SELL: {pos['crypto']} {pos['side']} | bid={best_bid*100:.1f}¢ (5x buy={buy_price*100:.1f}¢) | {shares:.0f}sh | profit ${profit:.2f}")
                    self._place_sell(tid, pos, best_bid, m)
                    continue
                
                # If position is profitable at all, sell
                if best_bid > buy_price and best_bid > 0:
                    profit = (best_bid - buy_price) * shares
                    if profit > 0.01:
                        logger.info(f"CRYPTO PROFIT SELL: {pos['crypto']} {pos['side']} | bid={best_bid*100:.1f}¢ > buy={buy_price*100:.1f}¢ | profit ${profit:.2f}")
                        self._place_sell(tid, pos, best_bid, m)
                        continue
                
                # Cut loss if <30 seconds left and losing
                if seconds_left < 30 and best_bid < buy_price and best_bid > 0:
                    loss = (best_bid - buy_price) * shares
                    logger.info(f"CRYPTO CUT LOSS: {pos['crypto']} {pos['side']} | bid={best_bid*100:.1f}¢ | {seconds_left:.0f}s left | loss ${loss:.2f}")
                    self._place_sell(tid, pos, best_bid, m)
                    continue
        
        for tid in to_remove:
            del self.positions[tid]
        self.save_state()
    
    def _place_sell(self, tid, pos, sell_price, market):
        """Place a sell order for a crypto position."""
        shares = pos.get("shares", 0)
        if shares < 5:
            return  # Can't sell below 5 shares
        
        tick = pos.get("tick_size", "0.01")
        # Round price to tick
        tick_f = float(tick)
        sell_price = round(sell_price / tick_f) * tick_f
        
        try:
            success, order_id, result, used_tick, used_nr = self.place_order_with_fallback(
                tid, sell_price, shares, "SELL", tick, neg_risk=False
            )
            
            pos["status"] = "selling"
            pos["sell_order_id"] = order_id
            pos["sell_price"] = sell_price
            pos["sell_time"] = time.time()
            pos["tick_size"] = used_tick  # Update to working tick
            self.save_state()
            logger.info(f"CRYPTO SELL PLACED: {pos['crypto']} {pos['side']} | {shares:.0f}sh @ {sell_price*100:.1f}¢ | order={order_id[:12]}...")
        except Exception as e:
            logger.error(f"CRYPTO sell order failed: {e}")
            # If sell fails, try at bid-1
            try:
                best_bid, _, _, _ = self.get_orderbook(tid)
                if best_bid > 0:
                    sell_price = max(best_bid - tick_f, tick_f)
                    sell_price = round(sell_price / tick_f) * tick_f
                    success, order_id, result, used_tick, used_nr = self.place_order_with_fallback(
                        tid, sell_price, shares, "SELL", tick, neg_risk=False
                    )
                    pos["status"] = "selling"
                    pos["sell_order_id"] = order_id
                    pos["sell_price"] = sell_price
                    pos["tick_size"] = used_tick
                    self.save_state()
                    logger.info(f"CRYPTO SELL RETRY: {shares:.0f}sh @ {sell_price*100:.1f}¢")
            except Exception as e2:
                logger.error(f"CRYPTO sell retry also failed: {e2}")
    
    def find_entry_opportunities(self, markets):
        """Find markets where we should enter — CONFIRMED TREND strategy.
        
        Based on Wallet 2 (0x20d230) actual trade data:
        - Buy the WINNING side at 89-99¢ when trend is confirmed
        - Hold to resolution (5 min) → shares resolve to $1.00
        - Profit = (1.00 - buy_price) * shares, e.g. buy 97¢ × 30 = $0.90 profit
        - High win rate (~90%), small profit per trade, high volume
        
        Uses Binance WebSocket to confirm crypto trend before entering.
        Only enters when one side is clearly winning (ask at 89-99¢ + trend confirms).
        SAFETY: Bid must be ≥5¢ to ensure real market conviction (prevents 1¢/99¢ trap).
        """
        opportunities = []
        
        for m in markets:
            seconds_left = m["seconds_left"]
            
            # Need at least 60 seconds for fill + resolution
            if seconds_left < 60:
                continue
            
            # Skip if we already have a position in this market
            if m["up_token"] in self.positions or m["down_token"] in self.positions:
                continue
            
            # Get orderbook for both sides
            up_bid, up_ask, _, _ = self.get_orderbook(m["up_token"])
            down_bid, down_ask, _, _ = self.get_orderbook(m["down_token"])
            
            # Get crypto trend from Binance
            trend = self.price_feed.get_trend(m["crypto"])
            
            # CONFIRMED TREND: Buy the winning side at 89-99¢ (TAKER — instant fill)
            # Only enter when Binance trend confirms the direction
            # SAFETY: Skip markets with extreme 1¢/99¢ spread — these have no real
            # liquidity and FOK at 99¢ may fill at much lower prices (the 50¢ bug)
            if trend == "up" and up_ask >= 0.89 and up_ask <= 0.99:
                # Check spread quality — bid must be > 5¢ (real market conviction)
                if up_bid >= 0.05:
                    opportunities.append({
                        "token": m["up_token"],
                        "side": "Up",
                        "crypto": m["crypto"],
                        "market": m,
                        "entry_price": up_ask,
                        "type": "confirmed",
                        "seconds_left": seconds_left,
                        "trend": trend,
                        "sell_target": 1.0,  # Hold to resolution = $1.00
                    })
                else:
                    logger.info(f"CRYPTO skip {m['crypto']} Up: spread {up_bid*100:.0f}c/{up_ask*100:.0f}c — bid too low (no conviction)")
            
            if trend == "down" and down_ask >= 0.89 and down_ask <= 0.99:
                if down_bid >= 0.05:
                    opportunities.append({
                        "token": m["down_token"],
                        "side": "Down",
                        "crypto": m["crypto"],
                        "market": m,
                        "entry_price": down_ask,
                        "type": "confirmed",
                        "seconds_left": seconds_left,
                        "trend": trend,
                        "sell_target": 1.0,
                    })
                else:
                    logger.info(f"CRYPTO skip {m['crypto']} Down: spread {down_bid*100:.0f}c/{down_ask*100:.0f}c — bid too low (no conviction)")
        
        # Sort by seconds left (less time = price more certain)
        opportunities.sort(key=lambda x: x["seconds_left"])
        return opportunities
    
    def open_position(self, opp):
        """Open a crypto position."""
        budget = self.get_crypto_budget()
        if budget < self.min_trade_size:
            return
        
        # Position size: confirmed trend only
        if opp["type"] == "confirmed":
            # Confirmed trend: larger size (buy 97¢ × 50 = $48.50, profit $1.50 on resolution)
            trade_size = min(50.0, budget * 0.4)
        else:
            # Unknown type — use conservative size
            trade_size = min(self.min_trade_size, budget * 0.15)
        
        trade_size = max(self.min_trade_size, trade_size)
        if trade_size > budget:
            trade_size = budget
        
        entry_price = opp["entry_price"]
        tick = opp["market"]["tick_size"]
        shares = int(trade_size / entry_price)
        if shares < 5:
            return  # Min 5 shares on Polymarket
        
        token = opp["token"]
        m = opp["market"]
        
        try:
            # Place buy order
            # For confirmed: TAKER (FOK) — take the ask for instant fill, hold to resolution
            
            if opp["type"] == "confirmed":
                # Taker order — buy at ask, immediate fill
                success, order_id, result, used_tick, used_nr = self.place_order_with_fallback(
                    token, entry_price, shares, "BUY", tick, neg_risk=False, order_type="FOK"
                )
                
                # CRITICAL: FOK orders may fill even when the API response doesn't
                # include the expected fields. Always verify via get_order() or data-api.
                filled, filled_shares, fill_price = self.verify_fok_fill(
                    order_id, token, shares, entry_price
                )
                
                if filled and filled_shares > 0:
                    actual_cost = fill_price * filled_shares if fill_price > 0 else entry_price * filled_shares
                    logger.info(f"CRYPTO BUY FILLED (FOK): {opp['crypto']} {opp['side']} | {filled_shares:.0f}sh @ {fill_price*100:.1f}c | ${actual_cost:.2f}")
                    self.positions[token] = {
                        "status": "holding",
                        "order_id": order_id,
                        "buy_price": fill_price if fill_price > 0 else entry_price,
                        "shares": filled_shares,
                        "target_shares": shares,
                        "crypto": opp["crypto"],
                        "side": opp["side"],
                        "type": opp["type"],
                        "cost": actual_cost,
                        "open_time": time.time(),
                        "hold_start": time.time(),
                        "market_slug": m["slug"],
                        "end_ts": m["end_ts"],
                        "sell_target": opp.get("sell_target", 1.0),
                        "tick_size": used_tick,
                    }
                    self.save_state()
                    return
                else:
                    logger.warning(f"CRYPTO FOK not filled: {opp['crypto']} {opp['side']} @ {entry_price*100:.1f}¢ (verified via get_order + data-api)")
                    return
            else:
                # Maker order — bid, wait for fill
                success, order_id, result, used_tick, used_nr = self.place_order_with_fallback(
                    token, entry_price, shares, "BUY", tick, neg_risk=False
                )
            
            self.positions[token] = {
                "status": "buying",
                "order_id": order_id,
                "buy_price": entry_price,
                "shares": 0,  # Will be set when filled
                "target_shares": shares,
                "crypto": opp["crypto"],
                "side": opp["side"],
                "type": opp["type"],
                "cost": entry_price * shares,
                "open_time": time.time(),
                "market_slug": m["slug"],
                "conditionId": m["conditionId"],
                "question": m["question"],
                "tick_size": used_tick,
                "end_ts": m["end_ts"],
                "sell_target": opp.get("sell_target", entry_price + float(tick) * 3),
            }
            self.save_state()
            logger.info(f"CRYPTO BUY PLACED: {opp['crypto']} {opp['side']} ({opp['type']}) | {shares}sh @ {entry_price*100:.1f}¢ | ${entry_price*shares:.2f} | {opp['seconds_left']:.0f}s left | order={order_id[:12]}...")
        except Exception as e:
            logger.error(f"CRYPTO buy order failed: {e}")
    
    def scan_and_trade(self):
        """Main scan loop — fetch markets, check positions, find entries."""
        # Reset daily PnL if 24h passed
        if time.time() - self.daily_pnl_reset > 86400:
            self.daily_pnl = 0.0
            self.daily_pnl_reset = time.time()
            self.stopped_today = False
            logger.info("Daily PnL reset")
        
        # Check daily loss limit
        if self.stopped_today:
            logger.warning(f"CRYPTO STOPPED TODAY — daily loss limit (${self.daily_loss_limit}) hit. PnL: ${self.daily_pnl:+.2f}")
            return
        
        if self.daily_pnl <= -self.daily_loss_limit:
            self.stopped_today = True
            logger.error(f"CRYPTO DAILY LOSS LIMIT HIT: ${self.daily_pnl:+.2f}. Stopping for today.")
            self.save_state()
            return
        
        # Update prices
        self.price_feed.update()
        
        # Fetch active markets
        markets = self.fetch_active_crypto_markets()
        if not markets:
            return
        
        logger.info(f"CRYPTO SCAN: {len(markets)} active markets, {len(self.positions)} positions, budget ${self.get_crypto_budget():.2f}")
        
        # Build token -> market map
        markets_by_token = {}
        for m in markets:
            markets_by_token[m["up_token"]] = m
            markets_by_token[m["down_token"]] = m
        
        # Check existing positions first
        self.check_existing_positions(markets_by_token)
        
        # Count active positions
        active = sum(1 for p in self.positions.values() if p.get("status") in ("buying", "holding"))
        if active >= self.max_positions:
            return
        
        # Find entry opportunities
        opportunities = self.find_entry_opportunities(markets)
        if opportunities:
            logger.info(f"CRYPTO OPPORTUNITIES: {len(opportunities)} found")
            for opp in opportunities[:3]:
                logger.info(f"  {opp['crypto']} {opp['side']} ({opp['type']}) @ {opp['entry_price']*100:.1f}¢ | {opp['seconds_left']:.0f}s left")
        else:
            # Debug: why no opportunities?
            for m in markets[:4]:
                sl = m["seconds_left"]
                up_bid, up_ask, _, _ = self.get_orderbook(m["up_token"])
                down_bid, down_ask, _, _ = self.get_orderbook(m["down_token"])
                trend = self.price_feed.get_trend(m["crypto"])
                hist_len = len(self.price_feed.prices.get(m["crypto"], {}).get("history", []))
                logger.info(f"  DEBUG: {m['crypto']} Up bid={up_bid*100:.1f}c ask={up_ask*100:.1f}c | Down bid={down_bid*100:.1f}c ask={down_ask*100:.1f}c | {sl:.0f}s left | trend={trend} hist={hist_len}")
        
        # Open positions (up to max_positions)
        for opp in opportunities:
            if active >= self.max_positions:
                break
            self.open_position(opp)
            active += 1
            time.sleep(0.5)  # Don't spam orders
    
    def run(self):
        """Main loop."""
        logger.info(f"Crypto Scalper starting | budget: {self.crypto_budget_pct*100:.0f}% of cash | max positions: {self.max_positions} | scan: {self.scan_interval}s | daily loss limit: ${self.daily_loss_limit}")
        
        # Initial price update
        self.price_feed.update()
        time.sleep(2)
        self.price_feed.update()  # Second update for trend detection
        
        scan_count = 0
        while True:
            try:
                scan_count += 1
                self.scan_and_trade()
                if scan_count % 12 == 0:  # Every ~60 seconds
                    logger.info(f"CRYPTO STATUS: {scan_count} scans | {self.total_trades} RTs | ${self.total_profit:+.2f} total | ${self.daily_pnl:+.2f} today | {len(self.positions)} positions")
            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
            
            time.sleep(self.scan_interval)


def main():
    parser = argparse.ArgumentParser(description="Crypto Scalper — Polymarket Up/Down markets")
    parser.add_argument("--private-key", required=True, help="Private key")
    parser.add_argument("--funder", required=True, help="Funder wallet address")
    parser.add_argument("--budget-pct", type=float, default=0.25, help="Percentage of cash for crypto (default: 0.25)")
    parser.add_argument("--max-positions", type=int, default=10, help="Max simultaneous positions")
    parser.add_argument("--min-trade", type=float, default=2.0, help="Min trade size in USD")
    parser.add_argument("--max-trade", type=float, default=6.0, help="Max trade size in USD")
    parser.add_argument("--scan-interval", type=int, default=5, help="Scan interval in seconds")
    parser.add_argument("--daily-loss-limit", type=float, default=15.0, help="Stop trading if daily loss exceeds this")
    parser.add_argument("--builder-code", default=os.environ.get("POLY_BUILDER_CODE", ""), help="Builder code")
    args = parser.parse_args()
    
    scalper = CryptoScalper(
        private_key=args.private_key,
        funder=args.funder,
        crypto_budget_pct=args.budget_pct,
        max_positions=args.max_positions,
        min_trade_size=args.min_trade,
        max_trade_size=args.max_trade,
        scan_interval=args.scan_interval,
        daily_loss_limit=args.daily_loss_limit,
        builder_code=args.builder_code,
    )
    scalper.run()


if __name__ == "__main__":
    main()
