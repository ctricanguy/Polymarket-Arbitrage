"""
Paper Trading Bot
==================

Simulates the full stat-arb + active-trading strategy against REAL market data
without risking any real money. 

- Uses live WebSocket feed for real-time prices
- Simulates limit order fills (fills when price crosses our limit)
- Tracks virtual portfolio, P&L, trade history
- Saves state to disk so it survives restarts
- Generates daily reports

NO AWS needed. NO wallet needed. NO real orders.
Just connect to Polymarket's public WebSocket and trade virtually.

Usage:
    python3 paper_trader.py --capital 1000
    python3 paper_trader.py --capital 1000 --duration 3600  # run for 1 hour
    python3 paper_trader.py --report  # show current portfolio status
"""

import json
import os
import time
import logging
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

from hybrid_scanner import HybridScanner
from stat_detector import StatisticalArbDetector
from active_trader import ActiveTrader, ActivePosition, PositionStatus

logger = logging.getLogger("polymarket-arb.paper")

STATE_DIR = Path(__file__).parent.parent / "data" / "paper_trader"
STATE_FILE = STATE_DIR / "state.json"
TRADE_EVENTS_FILE = STATE_DIR / "trade_events.json"
LOG_DIR = Path(__file__).parent.parent / "logs"


@dataclass
class PaperOrder:
    """A simulated order."""
    order_id: str
    condition_id: str
    token_id: str
    question: str
    side: str  # "BUY" or "SELL"
    price: float  # limit price
    shares: float
    status: str = "OPEN"  # OPEN, FILLED, CANCELLED
    created_at: float = 0.0
    filled_at: float = 0.0
    fill_price: float = 0.0


class PaperTrader:
    """
    Full paper trading simulation.
    
    Flow:
    1. Scan markets via REST + WebSocket (hybrid_scanner)
    2. Detect underpriced positions (stat_detector)
    3. Open virtual positions (active_trader)
    4. Monitor price updates via WebSocket
    5. Simulate fills when market price crosses our limit
    6. Track P&L and generate reports
    """
    
    def __init__(self, starting_capital: float = 1000,
                 take_profit_pct: float = 0.15,
                 stop_loss_pct: float = 0.15,
                 max_positions: int = 5,
                 max_per_category: int = 3):
        self.starting_capital = starting_capital
        self.available_cash = starting_capital
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_positions = max_positions
        self.max_per_category = max_per_category
        
        # Positions and orders
        self.positions: dict[str, ActivePosition] = {}  # condition_id -> position
        self.open_orders: list[PaperOrder] = []
        self.filled_orders: list[PaperOrder] = []
        
        # Price cache from WebSocket
        self.prices: dict[str, dict] = {}  # token_id -> {bid, ask, mid, ts}
        
        # Stats
        self.realized_pnl: float = 0.0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.started_at: float = time.time()
        
        # Scanner and detector
        self.scanner: Optional[HybridScanner] = None
        self.detector: Optional[StatisticalArbDetector] = None
        self.trader: Optional[ActiveTrader] = None
        
        # Market metadata
        self.market_meta: dict[str, dict] = {}  # condition_id -> {question, category, token_id}
        
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    def _log_trade_event(self, event_type: str, data: dict):
        """Append a trade event to the events file for dashboard display."""
        event = {
            "timestamp": time.time(),
            "type": event_type,
            "data": data,
        }
        events = []
        if TRADE_EVENTS_FILE.exists():
            try:
                with open(TRADE_EVENTS_FILE) as f:
                    events = json.load(f)
            except:
                pass
        events.append(event)
        # Keep last 500 events
        events = events[-500:]
        with open(TRADE_EVENTS_FILE, 'w') as f:
            json.dump(events, f, default=str)
    
    def initialize(self):
        """Connect to Polymarket and scan for opportunities."""
        logger.info("Initializing paper trader...")
        
        # Phase 1: Scan markets
        self.scanner = HybridScanner()
        self.scanner.discover_markets()
        
        # Phase 2: Subscribe to WebSocket for top markets
        self.scanner.subscribe_top_markets(min_volume=5000)
        
        # Phase 3: Build detector — need to inject WS book data into Market objects
        hybrid = self.scanner
        
        class ScannerAdapter:
            """Adapter that exposes hybrid scanner data with WS books as dict format."""
            def __init__(self):
                self.markets = {}  # condition_id -> Market (with book dict injected)
                self.events = hybrid.all_events  # event_id -> Event
                self._hybrid = hybrid
                self._refresh_markets()
            
            def _refresh_markets(self):
                """Rebuild markets dict with WS book data injected."""
                # Iterate over WS books we actually have, not all markets
                for token_id, ws_book in self._hybrid.ws_client.books.items():
                    if not ws_book or not ws_book.bids or not ws_book.asks:
                        continue
                    
                    # Find the market for this token
                    mkt = self._hybrid.token_to_market.get(token_id)
                    if not mkt:
                        continue
                    
                    # Only use Yes tokens (first token)
                    yes_token = mkt.tokens[0].get("token_id", "") if isinstance(mkt.tokens[0], dict) else ""
                    if token_id != yes_token:
                        continue  # Skip No tokens
                    
                    bid_price, bid_size = ws_book.get_best_bid()
                    ask_price, ask_size = ws_book.get_best_ask()
                    mkt.book = {
                        "bids": [{"price": str(bid_price), "size": str(bid_size)}],
                        "asks": [{"price": str(ask_price), "size": str(ask_size)}],
                    }
                    self.markets[mkt.condition_id] = mkt
            
            def get_ws_book(self, token_id):
                return self._hybrid.get_market_book(token_id)
        
        adapter = ScannerAdapter()
        
        self.detector = StatisticalArbDetector(
            adapter,
            target_position_size=self.starting_capital / self.max_positions,
            max_positions=self.max_positions,
            min_volume=5000,
            max_spread=0.10,
            min_ask_depth=100,
            max_per_category=self.max_per_category,
        )
        
        # Phase 4: Build active trader
        self.trader = ActiveTrader(
            take_profit_pct=self.take_profit_pct,
            stop_loss_pct=self.stop_loss_pct,
            max_hold_days=7,
            max_positions=self.max_positions,
            capital_per_position=self.starting_capital / self.max_positions,
        )
        
        logger.info(f"Paper trader initialized: ${self.starting_capital} capital, "
                    f"{self.max_positions} max positions")
    
    def find_and_open_positions(self):
        """Scan for opportunities and open virtual positions."""
        # Wait for WebSocket books to arrive
        if self.scanner.ws_books_received == 0:
            logger.info("Waiting for WebSocket books to arrive...")
            self.scanner.wait_for_books(timeout=20)
        
        # Refresh adapter with latest WS book data
        if hasattr(self.detector.scanner, '_refresh_markets'):
            self.detector.scanner._refresh_markets()
            logger.info(f"Adapter refreshed: {len(self.detector.scanner.markets)} markets with WS books")
        
        # Find opportunities
        opportunities = self.detector.find_opportunities()
        
        if not opportunities:
            logger.info("No opportunities found")
            return
        
        logger.info(f"Found {len(opportunities)} opportunities")
        
        # Build portfolio
        portfolio = self.detector.build_portfolio(
            available_capital=self.available_cash,
        )
        
        logger.info(f"Building portfolio with {len(portfolio)} new positions")
        
        for opp in portfolio:
            if len(self.positions) >= self.max_positions:
                break
            if opp.condition_id in self.positions:
                continue
            
            # Store market metadata
            self.market_meta[opp.condition_id] = {
                "question": opp.question,
                "category": opp.category,
                "token_id": opp.token_id,
            }
            
            # Open position in active trader
            pos = self.trader.open_position(
                condition_id=opp.condition_id,
                token_id=opp.token_id,
                question=opp.question,
                category=opp.category,
                entry_price=opp.best_ask,  # We'll try to buy at ask
                bid=opp.best_bid,
                ask=opp.best_ask,
                shares=opp.suggested_shares,
            )
            
            # Place simulated limit buy order (at bid+0.001 for passive entry)
            buy_price = opp.best_bid + 0.001  # Just above best bid
            order = PaperOrder(
                order_id=f"BUY-{opp.condition_id[:8]}-{int(time.time())}",
                condition_id=opp.condition_id,
                token_id=opp.token_id,
                question=opp.question,
                side="BUY",
                price=buy_price,
                shares=opp.suggested_shares,
                created_at=time.time(),
            )
            self.open_orders.append(order)
            self.positions[opp.condition_id] = pos
            
            logger.info(f"PLACED BUY: {opp.question[:40]} @ {buy_price:.4f} "
                        f"({opp.suggested_shares:.0f} shares, ${opp.suggested_investment:.2f})")
    
    def on_price_update(self, token_id: str, bid: float, ask: float):
        """Handle WebSocket price update — check fills and exit signals."""
        mid = (bid + ask) / 2
        self.prices[token_id] = {"bid": bid, "ask": ask, "mid": mid, "ts": time.time()}
        
        # Check open orders for fills
        for order in self.open_orders[:]:
            if order.token_id != token_id:
                continue
            
            if order.side == "BUY":
                # Buy limit fills when ask drops to our price
                if ask <= order.price:
                    self._fill_buy_order(order, fill_price=order.price)
            
            elif order.side == "SELL":
                # Sell limit fills when bid rises to our price
                if bid >= order.price:
                    self._fill_sell_order(order, fill_price=order.price)
        
        # Update positions with new prices
        for pos in self.positions.values():
            if pos.token_id == token_id:
                pos.update_price(bid, ask)
                
                # Check exit conditions
                exit_reason = pos.check_exit_conditions()
                if exit_reason and pos.status == PositionStatus.HOLDING:
                    self._place_sell_order(pos, reason=exit_reason)
                
                # Check re-entry
                if pos.should_reenter() and pos.status == PositionStatus.SOLD:
                    self._place_reentry_order(pos)
    
    def _fill_buy_order(self, order: PaperOrder, fill_price: float):
        """Simulate a buy order fill."""
        order.status = "FILLED"
        order.filled_at = time.time()
        order.fill_price = fill_price
        self.open_orders.remove(order)
        self.filled_orders.append(order)
        
        # Update position
        pos = self.positions.get(order.condition_id)
        if pos:
            self.trader.on_fill(order.condition_id, fill_price, order.shares)
            # Deduct cash
            cost = fill_price * order.shares
            self.available_cash -= cost
            logger.info(f"FILLED BUY: {order.question[:40]} @ {fill_price:.4f} "
                        f"({order.shares:.0f} shares, ${cost:.2f}) — "
                        f"Cash: ${self.available_cash:.2f}")
            self._log_trade_event("FILLED_BUY", {
                "question": order.question, "price": fill_price,
                "shares": order.shares, "cost": cost,
                "cash_remaining": self.available_cash,
            })
    
    def _place_sell_order(self, pos: ActivePosition, reason: str):
        """Place a simulated sell order."""
        # Sell at bid (market sell) or at take_profit price (limit sell)
        if "TAKE_PROFIT" in reason:
            sell_price = pos.take_profit_price
        elif "STOP_LOSS" in reason:
            sell_price = pos.current_bid  # Market sell at bid for stop loss
        elif "EXPIRED" in reason:
            sell_price = pos.current_bid
        else:
            sell_price = pos.current_bid
        
        order = PaperOrder(
            order_id=f"SELL-{pos.condition_id[:8]}-{int(time.time())}",
            condition_id=pos.condition_id,
            token_id=pos.token_id,
            question=pos.question,
            side="SELL",
            price=sell_price,
            shares=pos.entry_shares,
            created_at=time.time(),
        )
        self.open_orders.append(order)
        pos.status = PositionStatus.PENDING_SELL
        
        logger.info(f"PLACED SELL: {pos.question[:40]} @ {sell_price:.4f} "
                    f"— {reason}")
    
    def _fill_sell_order(self, order: PaperOrder, fill_price: float):
        """Simulate a sell order fill."""
        order.status = "FILLED"
        order.filled_at = time.time()
        order.fill_price = fill_price
        self.open_orders.remove(order)
        self.filled_orders.append(order)
        
        pos = self.positions.get(order.condition_id)
        if pos:
            pnl = (fill_price - pos.entry_price) * order.shares
            self.trader.close_position(order.condition_id, fill_price, order.shares)
            self.available_cash += fill_price * order.shares
            self.realized_pnl += pnl
            self.total_trades += 1
            
            if pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1
            
            logger.info(f"FILLED SELL: {order.question[:40]} @ {fill_price:.4f} "
                        f"P&L: ${pnl:+.2f} — Cash: ${self.available_cash:.2f}")
            proceeds = fill_price * order.shares
            self._log_trade_event("FILLED_SELL", {
                "question": order.question, "price": fill_price,
                "shares": order.shares, "revenue": proceeds,
                "pnl": pnl,
                "cash_remaining": self.available_cash,
            })
            
            # If it was a stop loss, remove the position
            if fill_price <= pos.stop_loss_price:
                self.trader.remove_position(order.condition_id)
                self.positions.pop(order.condition_id, None)
                logger.info(f"Position removed (stop loss): {order.question[:40]}")
    
    def _place_reentry_order(self, pos: ActivePosition):
        """Re-enter a position after selling."""
        buy_price = pos.current_bid + 0.001
        shares = self.trader.capital_per_position / buy_price
        
        if self.available_cash < shares * buy_price:
            logger.info(f"Insufficient cash for re-entry: {pos.question[:40]}")
            return
        
        order = PaperOrder(
            order_id=f"REBUY-{pos.condition_id[:8]}-{int(time.time())}",
            condition_id=pos.condition_id,
            token_id=pos.token_id,
            question=pos.question,
            side="BUY",
            price=buy_price,
            shares=shares,
            created_at=time.time(),
        )
        self.open_orders.append(order)
        pos.status = PositionStatus.PENDING_BUY
        
        logger.info(f"RE-ENTRY BUY: {pos.question[:40]} @ {buy_price:.4f}")
    
    def run(self, duration: int = 0, scan_interval: int = 300):
        """
        Run the paper trading loop.
        
        Args:
            duration: How long to run (seconds). 0 = run forever.
            scan_interval: How often to re-scan for new opportunities (seconds).
        """
        logger.info("=" * 60)
        logger.info("PAPER TRADING BOT STARTING")
        logger.info(f"Capital: ${self.starting_capital} | Positions: {self.max_positions}")
        logger.info(f"Take Profit: {self.take_profit_pct*100}% | Stop Loss: {self.stop_loss_pct*100}%")
        logger.info("=" * 60)
        
        # Initial scan and position opening
        self.find_and_open_positions()
        
        # Save initial state
        self.save_state()
        
        start_time = time.time()
        last_scan = time.time()
        last_report = time.time()
        last_price_check = time.time()
        
        try:
            while True:
                now = time.time()
                
                # Check duration
                if duration > 0 and (now - start_time) > duration:
                    logger.info(f"Duration reached ({duration}s). Stopping.")
                    break
                
                # Periodic re-scan for new opportunities
                if (now - last_scan) > scan_interval:
                    logger.info("Re-scanning for opportunities...")
                    self.find_and_open_positions()
                    last_scan = now
                
                # Process WebSocket messages
                if self.scanner and self.scanner.ws_client:
                    # Get latest books from ws_client
                    books = self.scanner.ws_client.get_all_books()
                    for token_id, book in books.items():
                        if not book or not hasattr(book, 'get_best_bid'):
                            continue
                        bid_price, bid_size = book.get_best_bid()
                        ask_price, ask_size = book.get_best_ask()
                        if bid_price > 0 and ask_price < 1.0:
                            self.on_price_update(token_id, bid_price, ask_price)
                
                # Periodic report (every 60s)
                if (now - last_report) > 60:
                    self.print_status()
                    self.save_state()
                    last_report = now
                
                # Check for stale orders (cancel if open > 5 min)
                for order in self.open_orders[:]:
                    if (now - order.created_at) > 300:
                        order.status = "CANCELLED"
                        self.open_orders.remove(order)
                        self.filled_orders.append(order)
                        pos = self.positions.get(order.condition_id)
                        if pos and pos.status == PositionStatus.PENDING_BUY:
                            pos.status = PositionStatus.HOLDING  # Will retry on next scan
                        logger.info(f"CANCELLED stale order: {order.question[:40]}")
                
                time.sleep(1)
        
        except KeyboardInterrupt:
            logger.info("Stopping paper trader (Ctrl+C)...")
        
        finally:
            self.save_state()
            self.print_final_report()
    
    def get_portfolio_value(self) -> dict:
        """Calculate current portfolio value."""
        holding_value = 0.0
        unrealized_pnl = 0.0
        
        for pos in self.positions.values():
            if pos.status in (PositionStatus.HOLDING, PositionStatus.PENDING_SELL):
                holding_value += pos.current_price * pos.entry_shares
                unrealized_pnl += pos.unrealized_pnl
        
        total_value = self.available_cash + holding_value
        
        return {
            "cash": self.available_cash,
            "positions_value": holding_value,
            "total_value": total_value,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "total_pnl": self.realized_pnl + unrealized_pnl,
            "total_return_pct": ((total_value - self.starting_capital) / self.starting_capital) * 100,
        }
    
    def print_status(self):
        """Print current portfolio status."""
        val = self.get_portfolio_value()
        runtime = time.time() - self.started_at
        hours = runtime / 3600
        
        print(f"\n{'='*60}")
        print(f"PAPER TRADING STATUS — {datetime.now().strftime('%H:%M:%S')}")
        print(f"Runtime: {hours:.1f}h | Trades: {self.total_trades} "
              f"(W:{self.winning_trades} L:{self.losing_trades})")
        print(f"{'─'*60}")
        print(f"  Cash:          ${val['cash']:,.2f}")
        print(f"  Positions:     ${val['positions_value']:,.2f}")
        print(f"  TOTAL VALUE:   ${val['total_value']:,.2f}")
        print(f"  Realized P&L:  ${val['realized_pnl']:+,.2f}")
        print(f"  Unrealized:    ${val['unrealized_pnl']:+,.2f}")
        print(f"  TOTAL P&L:     ${val['total_pnl']:+,.2f} ({val['total_return_pct']:+.2f}%)")
        print(f"{'─'*60}")
        
        # Show positions
        holding = [p for p in self.positions.values() 
                   if p.status in (PositionStatus.HOLDING, PositionStatus.PENDING_SELL, PositionStatus.PENDING_BUY)]
        if holding:
            print(f"  OPEN POSITIONS ({len(holding)}):")
            for pos in holding:
                pnl_pct = ((pos.current_price / pos.entry_price - 1) * 100) if pos.entry_price > 0 else 0
                print(f"    {pos.question[:45]:45s} | {pos.entry_price:.3f}¢ → {pos.current_price:.3f}¢ "
                      f"| {pos.entry_shares:>8.0f} sh | ${pos.entry_price*pos.entry_shares:>7.2f} | "
                      f"{pnl_pct:+.1f}% | {pos.status.value}")
        else:
            print("  No open positions")
        
        if self.open_orders:
            print(f"\n  OPEN ORDERS ({len(self.open_orders)}):")
            for order in self.open_orders[:5]:
                age = time.time() - order.created_at
                print(f"    {order.side:4s} {order.question[:40]:40s} @ {order.price:.4f} "
                      f"| {order.shares:.0f} sh | {age:.0f}s ago")
        
        print(f"{'='*60}\n")
    
    def print_final_report(self):
        """Print final summary report."""
        val = self.get_portfolio_value()
        runtime = time.time() - self.started_at
        
        print(f"\n{'#'*60}")
        print(f"# FINAL PAPER TRADING REPORT")
        print(f"{'#'*60}")
        print(f"  Starting capital:  ${self.starting_capital:,.2f}")
        print(f"  Final value:       ${val['total_value']:,.2f}")
        print(f"  Total P&L:         ${val['total_pnl']:+,.2f} ({val['total_return_pct']:+.2f}%)")
        print(f"  Realized P&L:      ${val['realized_pnl']:+,.2f}")
        print(f"  Unrealized P&L:    ${val['unrealized_pnl']:+,.2f}")
        print(f"  Total trades:      {self.total_trades}")
        print(f"  Win rate:          {self.winning_trades}/{self.total_trades} "
              f"({self.winning_trades/max(self.total_trades,1)*100:.1f}%)")
        print(f"  Runtime:           {runtime/3600:.1f} hours")
        if runtime > 0:
            daily_pnl = val['total_pnl'] / (runtime / 86400)
            print(f"  Projected daily:   ${daily_pnl:+,.2f}")
        print(f"{'#'*60}\n")
    
    def save_state(self):
        """Save state to disk for persistence across restarts."""
        state = {
            "starting_capital": self.starting_capital,
            "available_cash": self.available_cash,
            "realized_pnl": self.realized_pnl,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "started_at": self.started_at,
            "saved_at": time.time(),
            "positions": [],
            "open_orders": [],
            "filled_orders": [],
        }
        
        for pos in self.positions.values():
            state["positions"].append({
                "condition_id": pos.condition_id,
                "token_id": pos.token_id,
                "question": pos.question,
                "category": pos.category,
                "entry_price": pos.entry_price,
                "entry_shares": pos.entry_shares,
                "entry_time": pos.entry_time,
                "status": pos.status.value,
                "current_price": pos.current_price,
                "take_profit_price": pos.take_profit_price,
                "stop_loss_price": pos.stop_loss_price,
                "realized_pnl": pos.realized_pnl,
                "unrealized_pnl": pos.unrealized_pnl,
                "trades": pos.trades,
            })
        
        for order in self.open_orders:
            state["open_orders"].append(asdict(order))
        
        for order in self.filled_orders[-100:]:  # Keep last 100
            state["filled_orders"].append(asdict(order))
        
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
        
        logger.debug(f"State saved to {STATE_FILE}")
    
    def load_state(self) -> bool:
        """Load state from disk."""
        if not STATE_FILE.exists():
            return False
        
        with open(STATE_FILE) as f:
            state = json.load(f)
        
        self.starting_capital = state.get("starting_capital", self.starting_capital)
        self.available_cash = state.get("available_cash", self.starting_capital)
        self.realized_pnl = state.get("realized_pnl", 0)
        self.total_trades = state.get("total_trades", 0)
        self.winning_trades = state.get("winning_trades", 0)
        self.losing_trades = state.get("losing_trades", 0)
        self.started_at = state.get("started_at", time.time())
        
        # Restore positions
        for p in state.get("positions", []):
            pos = ActivePosition(
                condition_id=p["condition_id"],
                token_id=p["token_id"],
                question=p["question"],
                category=p.get("category", ""),
                entry_price=p["entry_price"],
                entry_shares=p["entry_shares"],
                entry_time=p["entry_time"],
                status=PositionStatus(p.get("status", "holding")),
                current_price=p.get("current_price", p["entry_price"]),
                take_profit_price=p.get("take_profit_price", 0),
                stop_loss_price=p.get("stop_loss_price", 0),
                realized_pnl=p.get("realized_pnl", 0),
                unrealized_pnl=p.get("unrealized_pnl", 0),
                trades=p.get("trades", []),
            )
            self.positions[pos.condition_id] = pos
        
        logger.info(f"State loaded: ${self.available_cash:.2f} cash, "
                    f"{len(self.positions)} positions, "
                    f"${self.realized_pnl:+.2f} realized P&L")
        return True
    
    def print_report_from_state(self):
        """Print report from saved state (no live data needed)."""
        if not self.load_state():
            print("No saved state found. Run the paper trader first.")
            return
        
        self.print_final_report()
        self.print_status()


def main():
    parser = argparse.ArgumentParser(description="Polymarket Paper Trading Bot")
    parser.add_argument("--capital", type=float, default=1000,
                        help="Starting capital in USD (default: $1000)")
    parser.add_argument("--duration", type=int, default=0,
                        help="How long to run in seconds (0 = forever)")
    parser.add_argument("--positions", type=int, default=5,
                        help="Max simultaneous positions (default: 5)")
    parser.add_argument("--tp", type=float, default=0.15,
                        help="Take profit percentage (default: 0.15 = 15%)")
    parser.add_argument("--sl", type=float, default=0.15,
                        help="Stop loss percentage (default: 0.15 = 15%)")
    parser.add_argument("--report", action="store_true",
                        help="Show report from saved state")
    parser.add_argument("--reset", action="store_true",
                        help="Reset saved state and start fresh")
    parser.add_argument("--scan-interval", type=int, default=300,
                        help="Re-scan interval in seconds (default: 300)")
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / "paper_trader.log"),
        ],
    )
    # Quiet down noisy loggers
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    
    if args.report:
        trader = PaperTrader()
        trader.print_report_from_state()
        return
    
    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("State reset.")
    
    trader = PaperTrader(
        starting_capital=args.capital,
        take_profit_pct=args.tp,
        stop_loss_pct=args.sl,
        max_positions=args.positions,
        max_per_category=max(2, args.positions // 3),
    )
    
    # Try to load existing state
    if not args.reset:
        trader.load_state()
    
    # Initialize and run
    trader.initialize()
    trader.run(duration=args.duration, scan_interval=args.scan_interval)


if __name__ == "__main__":
    main()
