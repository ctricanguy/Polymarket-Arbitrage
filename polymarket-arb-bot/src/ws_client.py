"""
Polymarket Real-Time WebSocket Client v2
==========================================

Stable WebSocket client for Polymarket CLOB market data.

Key fixes from v1:
- Proper auto-reconnect with re-subscription
- Watchdog thread detects silent disconnects (no messages for 60s)
- Subscription message matches Polymarket's expected format
- Channel selection: book, price_change, last_trade_price
- Thread-safe book updates with proper locking
- Stats for monitoring connection health

WebSocket endpoints:
- wss://ws-subscriptions-clob.polymarket.com/ws/market — market data
- wss://ws-subscriptions-clob.polymarket.com/ws/user — user data

Message types from the market WS:
- book: Full orderbook snapshot
- price_change: Delta update to orderbook
- last_trade_price: Latest trade
- tick_size_change: Tick size update
"""

import json
import logging
import time
import threading
from collections import defaultdict
from typing import Callable, Optional
import websocket

logger = logging.getLogger("polymarket-arb.ws")

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class OrderBook:
    """Real-time orderbook maintained from WebSocket updates."""
    
    def __init__(self, asset_id: str):
        self.asset_id = asset_id
        self.bids: dict[float, float] = {}  # price -> size
        self.asks: dict[float, float] = {}  # price -> size
        self.last_update: float = 0
        self.last_trade_price: float = 0
        self.last_trade_time: float = 0
        self.hash: str = ""
        self.update_count: int = 0
    
    def update_from_snapshot(self, data: dict):
        """Update from a full book snapshot."""
        self.bids.clear()
        self.asks.clear()
        for b in data.get("bids", []):
            price = float(b["price"])
            size = float(b["size"])
            if size > 0:
                self.bids[price] = size
        for a in data.get("asks", []):
            price = float(a["price"])
            size = float(a["size"])
            if size > 0:
                self.asks[price] = size
        self.hash = data.get("hash", "")
        self.last_update = time.time()
        self.update_count += 1
    
    def apply_price_change(self, data: dict):
        """Apply a delta price change."""
        changes = data.get("changes", [])
        for change in changes:
            price = float(change.get("price", 0))
            side = change.get("side", "")
            size = float(change.get("size", 0))
            
            if side == "BUY":
                if size > 0:
                    self.bids[price] = size
                else:
                    self.bids.pop(price, None)
            elif side == "SELL":
                if size > 0:
                    self.asks[price] = size
                else:
                    self.asks.pop(price, None)
        
        self.last_update = time.time()
        self.update_count += 1
    
    def get_best_bid(self) -> tuple[float, float]:
        """Returns (price, size) of best bid."""
        if not self.bids:
            return 0.0, 0.0
        best_price = max(self.bids.keys())
        return best_price, self.bids[best_price]
    
    def get_best_ask(self) -> tuple[float, float]:
        """Returns (price, size) of best ask."""
        if not self.asks:
            return 1.0, 0.0
        best_price = min(self.asks.keys())
        return best_price, self.asks[best_price]
    
    def get_spread(self) -> float:
        """Current bid-ask spread."""
        bid, _ = self.get_best_bid()
        ask, _ = self.get_best_ask()
        return ask - bid
    
    def get_midpoint(self) -> float:
        """Current midpoint price."""
        bid, _ = self.get_best_bid()
        ask, _ = self.get_best_ask()
        return (bid + ask) / 2
    
    def get_depth(self, levels: int = 5) -> dict:
        """Get top N levels of the book."""
        sorted_bids = sorted(self.bids.items(), key=lambda x: -x[0])[:levels]
        sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])[:levels]
        return {
            "bids": sorted_bids,
            "asks": sorted_asks,
            "best_bid": sorted_bids[0] if sorted_bids else None,
            "best_ask": sorted_asks[0] if sorted_asks else None,
            "spread": self.get_spread(),
            "midpoint": self.get_midpoint(),
        }
    
    def is_stale(self, max_age: float = 120) -> bool:
        """Check if this book hasn't been updated recently."""
        return (time.time() - self.last_update) > max_age
    
    def __repr__(self):
        bid, bid_size = self.get_best_bid()
        ask, ask_size = self.get_best_ask()
        return f"OrderBook(asset={self.asset_id[:15]}..., bid={bid:.4f}x{bid_size:.0f}, ask={ask:.4f}x{ask_size:.0f})"


class MarketWebSocketClient:
    """
    Real-time WebSocket client for Polymarket market data (v2).
    
    Fixes:
    - Auto-reconnect with re-subscription (subscriptions are restored after reconnect)
    - Watchdog thread detects silent disconnects and forces reconnect
    - Proper ping/pong handling
    - Thread-safe everything
    """

    def __init__(self, on_book_update: Optional[Callable] = None,
                 on_price_change: Optional[Callable] = None,
                 on_trade: Optional[Callable] = None):
        self.ws: Optional[websocket.WebSocketApp] = None
        self.books: dict[str, OrderBook] = {}  # asset_id -> OrderBook
        self.subscribed_assets: set = set()
        self.on_book_update = on_book_update
        self.on_price_change = on_price_change
        self.on_trade = on_trade
        self.connected = False
        self._should_reconnect = True
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        
        # Trade tracking for realistic fill calibration
        self.trade_history: dict[str, list[dict]] = {}  # asset_id -> [{price, time, side, size}]
        self._trade_lock = threading.Lock()
        self._lock = threading.RLock()
        
        # Stats
        self.messages_received = 0
        self.last_message_time = 0
        self.connect_count = 0
        self._stop = False

    def _on_message(self, ws, message):
        """Handle incoming WebSocket message."""
        self.messages_received += 1
        self.last_message_time = time.time()
        
        try:
            data = json.loads(message)
            
            # Messages can be arrays (batch) or single objects
            if isinstance(data, list):
                for item in data:
                    self._process_message(item)
            else:
                self._process_message(data)
                
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON message: {message[:100]}")
        except Exception as e:
            logger.error(f"WS message processing error: {e}")

    def _process_message(self, data: dict):
        """Process a single message."""
        msg_type = data.get("event_type", "")
        
        # Standard event types
        if msg_type == "book":
            # Full orderbook snapshot
            asset_id = data.get("asset_id", "")
            if asset_id:
                with self._lock:
                    if asset_id not in self.books:
                        self.books[asset_id] = OrderBook(asset_id)
                    self.books[asset_id].update_from_snapshot(data)
                
                if self.on_book_update:
                    self.on_book_update(self.books[asset_id])
        
        elif msg_type == "price_change":
            # Delta update (standard format)
            asset_id = data.get("asset_id", "")
            if asset_id and asset_id in self.books:
                with self._lock:
                    self.books[asset_id].apply_price_change(data)
                
                if self.on_price_change:
                    self.on_price_change(self.books[asset_id])
        
        elif msg_type == "last_trade_price":
            asset_id = data.get("asset_id", "")
            if asset_id:
                price = float(data.get("price", 0))
                size = float(data.get("size", 0)) if data.get("size") else 0
                side = data.get("side", "unknown")
                timestamp = data.get("timestamp", time.time())
                
                with self._lock:
                    if asset_id not in self.books:
                        self.books[asset_id] = OrderBook(asset_id)
                    self.books[asset_id].last_trade_price = price
                    self.books[asset_id].last_trade_time = time.time()
                
                # Record trade for fill calibration
                with self._trade_lock:
                    if asset_id not in self.trade_history:
                        self.trade_history[asset_id] = []
                    self.trade_history[asset_id].append({
                        "price": price,
                        "time": time.time(),
                        "side": side,
                        "size": size,
                        "timestamp": timestamp,
                    })
                    if len(self.trade_history[asset_id]) > 1000:
                        self.trade_history[asset_id] = self.trade_history[asset_id][-1000:]
                
                if self.on_trade:
                    self.on_trade(asset_id, price, side, size)
        
        elif msg_type == "tick_size_change":
            logger.debug(f"Tick size change: {data}")
        
        else:
            # Non-standard formats — Polymarket also sends:
            # {"market": "0x...", "price_changes": [...]}  — price change deltas
            # {"asset_id": "...", "changes": [...]}        — another price change format
            # {"market": "0x...", "timestamp": ...}         — tick/timing info
            
            if "price_changes" in data:
                # Price change format: {"market": "0x...", "price_changes": [{asset_id, changes}]}
                market_id = data.get("market", "")
                price_changes = data.get("price_changes", [])
                for pc in price_changes:
                    asset_id = pc.get("asset_id", pc.get("market", ""))
                    changes = pc.get("changes", [])
                    if asset_id:
                        with self._lock:
                            if asset_id not in self.books:
                                self.books[asset_id] = OrderBook(asset_id)
                            # Apply changes
                            for change in changes:
                                price = float(change.get("price", 0))
                                side = change.get("side", "")
                                size = float(change.get("size", 0))
                                if side == "BUY":
                                    if size > 0:
                                        self.books[asset_id].bids[price] = size
                                    else:
                                        self.books[asset_id].bids.pop(price, None)
                                elif side == "SELL":
                                    if size > 0:
                                        self.books[asset_id].asks[price] = size
                                    else:
                                        self.books[asset_id].asks.pop(price, None)
                            self.books[asset_id].last_update = time.time()
                            self.books[asset_id].update_count += 1
                        
                        if self.on_price_change:
                            self.on_price_change(self.books[asset_id])
            
            elif "changes" in data and "asset_id" in data:
                # Direct price change with asset_id
                asset_id = data.get("asset_id", "")
                if asset_id:
                    with self._lock:
                        if asset_id not in self.books:
                            self.books[asset_id] = OrderBook(asset_id)
                        self.books[asset_id].apply_price_change(data)
                    if self.on_price_change:
                        self.on_price_change(self.books[asset_id])
            
            elif "trades" in data:
                # Trade batch format
                trades = data.get("trades", [])
                for t in trades:
                    asset_id = t.get("asset_id", t.get("market", ""))
                    price = float(t.get("price", 0))
                    size = float(t.get("size", 0)) if t.get("size") else 0
                    side = t.get("side", "unknown")
                    if asset_id:
                        with self._lock:
                            if asset_id not in self.books:
                                self.books[asset_id] = OrderBook(asset_id)
                            self.books[asset_id].last_trade_price = price
                            self.books[asset_id].last_trade_time = time.time()
                        with self._trade_lock:
                            if asset_id not in self.trade_history:
                                self.trade_history[asset_id] = []
                            self.trade_history[asset_id].append({
                                "price": price,
                                "time": time.time(),
                                "side": side,
                                "size": size,
                            })
                            if len(self.trade_history[asset_id]) > 1000:
                                self.trade_history[asset_id] = self.trade_history[asset_id][-1000:]
                        if self.on_trade:
                            self.on_trade(asset_id, price, side, size)

    def _on_error(self, ws, error):
        logger.error(f"WS error: {error}")

    def _on_close(self, ws, close_status_code=None, close_msg=None):
        self.connected = False
        logger.info(f"WS closed (code={close_status_code})")
        
        if self._should_reconnect and not self._stop:
            logger.info("Reconnecting in 3 seconds...")
            time.sleep(3)
            if self._should_reconnect and not self._stop:
                self._do_connect()

    def _on_open(self, ws):
        self.connected = True
        self.connect_count += 1
        logger.info(f"WS connected! (connection #{self.connect_count})")
        
        # Subscribe to all registered assets
        if self.subscribed_assets:
            self._send_subscription()

    def _send_subscription(self):
        """Send subscription message for all registered assets."""
        if not self.ws or not self.subscribed_assets:
            return
        
        # Polymarket WS subscription format
        # Type "market" with assets_ids and channels
        assets = list(self.subscribed_assets)
        
        # Send in chunks of 100 to avoid message size limits
        chunk_size = 100
        for i in range(0, len(assets), chunk_size):
            chunk = assets[i:i + chunk_size]
            sub_msg = {
                "type": "market",
                "assets_ids": chunk,
                "channels": ["book", "price_change", "last_trade_price"]
            }
            try:
                self.ws.send(json.dumps(sub_msg))
            except Exception as e:
                logger.error(f"Failed to send subscription: {e}")
                return
        
        logger.info(f"Subscribed to {len(assets)} assets ({(len(assets)-1)//chunk_size + 1} messages)")

    def _do_connect(self):
        """Internal connect — creates WS and starts thread."""
        self.ws = websocket.WebSocketApp(
            WS_MARKET_URL,
            on_message=self._on_message,
            on_error=self._on_error,
            on_open=self._on_open,
            on_close=self._on_close,
        )
        
        self._thread = threading.Thread(
            target=self.ws.run_forever,
            kwargs={
                'ping_interval': 20,   # Send ping every 20s
                'ping_timeout': 10,    # Wait 10s for pong
                'reconnect': 5,        # websocket-client internal reconnect delay
            },
            daemon=True
        )
        self._thread.start()

    def _watchdog(self):
        """Watchdog thread — detects actual disconnects and forces reconnect.
        
        NOTE: Polymarket WS sends book snapshots once, then price_change updates
        only when the book changes. A market with no trades = no updates = silence.
        Silence does NOT mean disconnected. We only reconnect if the WebSocket
        thread itself dies or the connection drops.
        """
        last_reconnect_attempt = 0
        reconnect_in_progress = False
        while not self._stop:
            time.sleep(30)
            if self._stop:
                break
            
            # Only reconnect if the WS thread died (actual disconnect)
            if self._thread and not self._thread.is_alive() and not self._stop:
                if (time.time() - last_reconnect_attempt) > 60:
                    logger.warning("WS thread died — restarting")
                    last_reconnect_attempt = time.time()
                    reconnect_in_progress = True
                    self._do_connect()
                    time.sleep(5)  # Give it time to connect
                    reconnect_in_progress = False
            
            # If connected but no messages in 5 minutes, try a fresh connection
            # (the old one might be half-open)
            if self.connected and self.last_message_time > 0:
                silence = time.time() - self.last_message_time
                if silence > 300 and (time.time() - last_reconnect_attempt) > 120 and not reconnect_in_progress:
                    logger.warning(f"WS silent for {silence:.0f}s — forcing fresh reconnect")
                    last_reconnect_attempt = time.time()
                    reconnect_in_progress = True
                    self.connected = False  # Mark as disconnected so we don't double-reconnect
                    try:
                        if self.ws:
                            self.ws.close()
                    except:
                        pass
                    # _on_close will call _do_connect which will reconnect and re-subscribe
                    # But also try directly in case _on_close doesn't fire
                    time.sleep(5)
                    if not self.connected:
                        logger.warning("Reconnect via _on_close failed — direct reconnect")
                        self._do_connect()
                    reconnect_in_progress = False

    def subscribe(self, asset_ids: list[str]):
        """Subscribe to orderbook updates for given asset IDs."""
        with self._lock:
            for aid in asset_ids:
                self.subscribed_assets.add(aid)
                if aid not in self.books:
                    self.books[aid] = OrderBook(aid)
        
        if self.connected and self.ws:
            self._send_subscription()
        
        logger.info(f"Registered {len(asset_ids)} assets for subscription (total: {len(self.subscribed_assets)})")

    def unsubscribe(self, asset_ids: list[str]):
        """Unsubscribe from assets."""
        with self._lock:
            for aid in asset_ids:
                self.subscribed_assets.discard(aid)
                # Don't remove the book — keep historical data
        
        # Polymarket doesn't have an unsubscribe message, so we just stop tracking
        logger.info(f"Unsubscribed from {len(asset_ids)} assets (total: {len(self.subscribed_assets)})")

    def connect(self):
        """Connect to the WebSocket and start listening."""
        self._stop = False
        self._should_reconnect = True
        self._do_connect()
        
        # Start watchdog thread
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._watchdog_thread.start()
        
        # Wait for connection
        for _ in range(10):
            if self.connected:
                break
            time.sleep(0.5)

    def disconnect(self):
        """Disconnect from WebSocket."""
        self._stop = True
        self._should_reconnect = False
        if self.ws:
            self.ws.close()
        logger.info("WS disconnected")

    def get_book(self, asset_id: str) -> Optional[OrderBook]:
        """Get the current orderbook for an asset."""
        with self._lock:
            return self.books.get(asset_id)

    def get_all_books(self) -> dict[str, OrderBook]:
        """Get all current orderbooks."""
        with self._lock:
            return dict(self.books)

    def get_active_books(self, max_age: float = 120) -> dict[str, OrderBook]:
        """Get only books that have been updated recently."""
        with self._lock:
            return {aid: book for aid, book in self.books.items() 
                    if not book.is_stale(max_age)}

    def get_trade_stats(self, asset_id: str, window_seconds: int = 3600) -> dict:
        """Get trade statistics for an asset over a time window."""
        with self._trade_lock:
            trades = self.trade_history.get(asset_id, [])
            if not trades:
                return {"count": 0, "rate_per_hour": 0, "avg_size": 0, "avg_price": 0}
            
            now = time.time()
            recent = [t for t in trades if (now - t["time"]) < window_seconds]
            
            if not recent:
                return {"count": 0, "rate_per_hour": 0, "avg_size": 0, "avg_price": 0}
            
            sizes = [t["size"] for t in recent if t["size"] > 0]
            prices = [t["price"] for t in recent if t["price"] > 0]
            
            return {
                "count": len(recent),
                "rate_per_hour": len(recent) / (window_seconds / 3600),
                "avg_size": sum(sizes) / len(sizes) if sizes else 0,
                "avg_price": sum(prices) / len(prices) if prices else 0,
                "total_volume": sum(t["size"] * t["price"] for t in recent),
            }

    def get_all_trade_stats(self, window_seconds: int = 3600) -> dict[str, dict]:
        """Get trade stats for all assets."""
        with self._trade_lock:
            return {aid: self.get_trade_stats(aid, window_seconds) 
                    for aid in self.trade_history if self.trade_history[aid]}

    def get_stats(self) -> dict:
        """Get WebSocket client stats."""
        return {
            "connected": self.connected,
            "subscribed_assets": len(self.subscribed_assets),
            "active_books": len(self.books),
            "fresh_books": len(self.get_active_books()),
            "messages_received": self.messages_received,
            "last_message_ago": time.time() - self.last_message_time if self.last_message_time else -1,
            "connect_count": self.connect_count,
        }


if __name__ == "__main__":
    # Test: subscribe to top markets and verify stable connection
    import requests
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    # Get top markets by volume
    r = requests.get("https://gamma-api.polymarket.com/markets", params={
        "limit": 10, "active": "true", "closed": "false", "order": "volume", "ascending": "false"
    })
    markets = r.json()
    
    asset_ids = []
    for m in markets:
        tokens_raw = m.get("clobTokenIds", "[]")
        if isinstance(tokens_raw, str):
            tokens = json.loads(tokens_raw)
        else:
            tokens = tokens_raw
        if tokens:
            asset_ids.extend(tokens)
            print(f"Market: {m['question'][:60]}")
            print(f"  Tokens: {tokens[0][:25]}..., {tokens[1][:25]}...")
    
    print(f"\nSubscribing to {len(asset_ids)} assets...\n")
    
    update_count = 0
    
    def on_update(book: OrderBook):
        global update_count
        update_count += 1
        if update_count % 50 == 0:
            bid, bid_size = book.get_best_bid()
            ask, ask_size = book.get_best_ask()
            spread = book.get_spread()
            print(f"  [{update_count}] {book.asset_id[:20]}... | "
                  f"bid={bid:.4f}x{bid_size:.0f} ask={ask:.4f}x{ask_size:.0f} "
                  f"spread={spread:.4f}")
    
    client = MarketWebSocketClient(on_book_update=on_update, on_price_change=on_update)
    client.subscribe(asset_ids[:10])
    client.connect()
    
    # Run for 120 seconds to test stability
    print("\nWatching real-time updates for 120 seconds...\n")
    for i in range(12):
        time.sleep(10)
        stats = client.get_stats()
        print(f"  {(i+1)*10}s — connected={stats['connected']} books={stats['active_books']} "
              f"fresh={stats['fresh_books']} msgs={stats['messages_received']} "
              f"last_msg={stats['last_message_ago']:.0f}s ago")
    
    # Final stats
    stats = client.get_stats()
    print(f"\nFinal stats: {stats}")
    print(f"Total updates: {update_count}")
    
    # Show tight spread markets
    books = client.get_all_books()
    tight = []
    for aid, book in books.items():
        if book.bids and book.asks:
            bid, _ = book.get_best_bid()
            ask, _ = book.get_best_ask()
            if bid > 0.02 and ask < 0.90:
                spread_pct = (ask - bid) / ask * 100
                if spread_pct < 15:
                    tight.append((aid, bid, ask, spread_pct))
    
    tight.sort(key=lambda x: x[3])
    print(f"\nTight spread markets ({len(tight)}):")
    for aid, bid, ask, sp in tight[:10]:
        print(f"  bid={bid:.3f} ask={ask:.3f} spread={sp:.1f}% | {aid[:30]}...")
    
    client.disconnect()
