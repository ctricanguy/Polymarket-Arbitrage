"""
Active Trading Strategy — Position Rotation
=============================================

This is the key to matching the friend's $700-1000/day returns.

Instead of buy-and-hold, this strategy:
1. Buys underpriced cheap Yes positions (1-15¢)
2. Monitors price movements in real-time via WebSocket
3. Sells when price spikes (take profit at 10-30% gain)
4. Re-buys when price dips back to entry level
5. Repeats throughout the day — same capital, multiple trades

The math:
- 20 positions × 3-5 trades/day × $5-15 profit per trade = $300-1500/day
- On $100K capital = 0.3-1.5% daily returns
- Matches the friend's $733/day (+0.65% daily)

Position management:
- Entry: Buy at bid+0.001 (passive limit order, just above best bid)
- Take profit: Sell at ask when price rises 15%+ from entry
- Stop loss: Sell if price drops 25% from entry (cheap positions can go to 0)
- Re-entry: If sold and price drops back to within 5% of original entry, re-buy
- Max hold time: 7 days (if no movement, exit and redeploy capital)
"""

import logging
import time
import math
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = logging.getLogger("polymarket-arb.active_trader")


class PositionStatus(Enum):
    PENDING_BUY = "pending_buy"     # Limit buy order placed, waiting for fill
    HOLDING = "holding"             # Filled, monitoring for profit/loss
    PENDING_SELL = "pending_sell"   # Limit sell order placed, waiting for fill
    SOLD = "sold"                   # Sold, watching for re-entry
    STOPPED = "stopped"             # Stop-loss triggered
    EXPIRED = "expired"             # Max hold time reached


@dataclass
class ActivePosition:
    """An actively managed position."""
    # Market info
    condition_id: str
    token_id: str
    question: str
    category: str = ""
    
    # Entry
    entry_price: float = 0.0        # Price we bought at
    entry_shares: float = 0.0       # Shares we hold
    entry_time: float = 0.0         # Timestamp of entry
    entry_order_id: str = ""        # CLOB order ID
    
    # Current state
    status: PositionStatus = PositionStatus.PENDING_BUY
    current_price: float = 0.0      # Latest price from WebSocket
    current_bid: float = 0.0
    current_ask: float = 0.0
    
    # Targets
    take_profit_price: float = 0.0  # Sell target (entry + profit %)
    stop_loss_price: float = 0.0    # Sell if drops below this
    reentry_price: float = 0.0      # Re-buy target after selling
    
    # Trade history (for this position)
    trades: list[dict] = field(default_factory=list)
    
    # P&L
    realized_pnl: float = 0.0       # Profit from completed trades
    unrealized_pnl: float = 0.0     # Current position value - entry cost
    
    # Config
    take_profit_pct: float = 0.15   # 15% profit target
    stop_loss_pct: float = 0.25     # 25% stop loss
    max_hold_seconds: float = 7 * 24 * 3600  # 7 days
    
    def update_price(self, bid: float, ask: float):
        """Update current price from WebSocket."""
        self.current_bid = bid
        self.current_ask = ask
        self.current_price = (bid + ask) / 2
        if self.status == PositionStatus.HOLDING:
            self.unrealized_pnl = (self.current_price - self.entry_price) * self.entry_shares
    
    def check_exit_conditions(self) -> Optional[str]:
        """Check if position should be exited. Returns reason or None."""
        if self.status != PositionStatus.HOLDING:
            return None
        
        # Take profit: price rose by take_profit_pct
        if self.current_price >= self.take_profit_price:
            return f"TAKE_PROFIT: {self.current_price:.4f} >= {self.take_profit_price:.4f} (+{(self.current_price/self.entry_price - 1)*100:.1f}%)"
        
        # Stop loss: price dropped by stop_loss_pct
        if self.current_price <= self.stop_loss_price:
            return f"STOP_LOSS: {self.current_price:.4f} <= {self.stop_loss_price:.4f} ({(self.current_price/self.entry_price - 1)*100:.1f}%)"
        
        # Max hold time
        hold_time = time.time() - self.entry_time
        if hold_time > self.max_hold_seconds:
            return f"EXPIRED: held for {hold_time/3600:.1f}h"
        
        return None
    
    def should_reenter(self) -> bool:
        """Check if we should re-enter after selling."""
        if self.status != PositionStatus.SOLD:
            return False
        # Re-enter if price dropped back near original entry
        if self.current_price <= self.reentry_price:
            return True
        return False
    
    def record_trade(self, action: str, price: float, shares: float, pnl: float = 0):
        """Record a completed trade."""
        self.trades.append({
            "action": action,
            "price": price,
            "shares": shares,
            "pnl": pnl,
            "timestamp": time.time(),
        })
        if action == "SELL":
            self.realized_pnl += pnl


class ActiveTrader:
    """
    Active trading strategy that rotates capital through positions.
    
    Lifecycle per position:
    1. Identify underpriced market (from stat_detector)
    2. Place limit buy at bid+0.001
    3. Wait for fill
    4. Monitor price via WebSocket
    5. When price rises 15% → place sell order at ask
    6. When filled → watch for re-entry opportunity
    7. If price drops back → re-buy and repeat
    8. If price drops 25% → stop loss, exit permanently
    """
    
    def __init__(self, 
                 take_profit_pct: float = 0.15,
                 stop_loss_pct: float = 0.25,
                 max_hold_days: int = 7,
                 max_positions: int = 20,
                 capital_per_position: float = 5000,
                 reentry_threshold_pct: float = 0.05,
                 max_trades_per_day: int = 100):
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold_seconds = max_hold_days * 24 * 3600
        self.max_positions = max_positions
        self.capital_per_position = capital_per_position
        self.reentry_threshold_pct = reentry_threshold_pct
        self.max_trades_per_day = max_trades_per_day
        
        # Active positions
        self.positions: dict[str, ActivePosition] = {}  # condition_id -> position
        
        # Daily stats
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.daily_resets_at: float = time.time()
        
        # All-time stats
        self.total_pnl: float = 0.0
        self.total_trades: int = 0
        
        logger.info(f"ActiveTrader initialized: "
                    f"take_profit={take_profit_pct*100}%, "
                    f"stop_loss={stop_loss_pct*100}%, "
                    f"max_hold={max_hold_days}d, "
                    f"max_positions={max_positions}")
    
    def reset_daily_stats(self):
        """Reset daily counters."""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_resets_at = time.time()
    
    def open_position(self, condition_id: str, token_id: str, question: str,
                      category: str, entry_price: float, bid: float, ask: float,
                      shares: float) -> ActivePosition:
        """Open a new position."""
        pos = ActivePosition(
            condition_id=condition_id,
            token_id=token_id,
            question=question,
            category=category,
            entry_price=entry_price,
            entry_shares=shares,
            entry_time=time.time(),
            status=PositionStatus.PENDING_BUY,
            current_bid=bid,
            current_ask=ask,
            current_price=(bid + ask) / 2,
            take_profit_price=entry_price * (1 + self.take_profit_pct),
            stop_loss_price=entry_price * (1 - self.stop_loss_pct),
            reentry_price=entry_price * (1 + self.reentry_threshold_pct),
            take_profit_pct=self.take_profit_pct,
            stop_loss_pct=self.stop_loss_pct,
            max_hold_seconds=self.max_hold_seconds,
        )
        self.positions[condition_id] = pos
        logger.info(f"OPENED position: {question[:40]} @ {entry_price:.4f} "
                    f"({shares:.0f} shares, ${shares*entry_price:.2f}) "
                    f"TP={pos.take_profit_price:.4f} SL={pos.stop_loss_price:.4f}")
        return pos
    
    def on_fill(self, condition_id: str, fill_price: float, fill_shares: float):
        """Called when a buy order is filled."""
        pos = self.positions.get(condition_id)
        if pos and pos.status == PositionStatus.PENDING_BUY:
            pos.entry_price = fill_price
            pos.entry_shares = fill_shares
            pos.status = PositionStatus.HOLDING
            pos.take_profit_price = fill_price * (1 + self.take_profit_pct)
            pos.stop_loss_price = fill_price * (1 - self.stop_loss_pct)
            pos.reentry_price = fill_price * (1 + self.reentry_threshold_pct)
            logger.info(f"FILLED: {pos.question[:40]} @ {fill_price:.4f} "
                        f"({fill_shares:.0f} shares)")
    
    def on_price_update(self, condition_id: str, bid: float, ask: float):
        """Called on WebSocket price update. Main monitoring loop."""
        pos = self.positions.get(condition_id)
        if not pos:
            return
        
        pos.update_price(bid, ask)
        
        # Check exit conditions
        exit_reason = pos.check_exit_conditions()
        if exit_reason:
            logger.info(f"EXIT SIGNAL: {pos.question[:40]} — {exit_reason}")
            return "SELL", pos
        
        # Check re-entry
        if pos.should_reenter():
            logger.info(f"RE-ENTRY: {pos.question[:40]} — "
                        f"price {pos.current_price:.4f} <= {pos.reentry_price:.4f}")
            return "BUY", pos
        
        return None, pos
    
    def close_position(self, condition_id: str, sell_price: float, sell_shares: float):
        """Close a position (sell filled)."""
        pos = self.positions.get(condition_id)
        if not pos:
            return
        
        pnl = (sell_price - pos.entry_price) * sell_shares
        pos.record_trade("SELL", sell_price, sell_shares, pnl)
        pos.status = PositionStatus.SOLD
        pos.unrealized_pnl = 0.0
        
        self.daily_pnl += pnl
        self.total_pnl += pnl
        self.daily_trades += 1
        self.total_trades += 1
        
        logger.info(f"SOLD: {pos.question[:40]} @ {sell_price:.4f} "
                    f"P&L: ${pnl:+.2f} (total: {len(pos.trades)} trades, "
                    f"realized: ${pos.realized_pnl:+.2f})")
    
    def remove_position(self, condition_id: str):
        """Remove a position entirely (after stop loss or expiry)."""
        pos = self.positions.pop(condition_id, None)
        if pos:
            logger.info(f"REMOVED: {pos.question[:40]} — "
                        f"final P&L: ${pos.realized_pnl:+.2f} "
                        f"({len(pos.trades)} trades)")
    
    def get_portfolio_summary(self) -> dict:
        """Get current portfolio summary."""
        holding = [p for p in self.positions.values() if p.status == PositionStatus.HOLDING]
        pending = [p for p in self.positions.values() if p.status == PositionStatus.PENDING_BUY]
        sold = [p for p in self.positions.values() if p.status == PositionStatus.SOLD]
        
        total_unrealized = sum(p.unrealized_pnl for p in holding)
        total_realized = sum(p.realized_pnl for p in self.positions.values())
        total_deployed = sum(p.entry_price * p.entry_shares for p in holding)
        
        return {
            "total_positions": len(self.positions),
            "holding": len(holding),
            "pending_buy": len(pending),
            "sold_watching": len(sold),
            "capital_deployed": total_deployed,
            "unrealized_pnl": total_unrealized,
            "realized_pnl": total_realized,
            "daily_pnl": self.daily_pnl,
            "daily_trades": self.daily_trades,
            "total_pnl": self.total_pnl,
            "total_trades": self.total_trades,
            "avg_pnl_per_trade": total_realized / max(self.total_trades, 1),
        }
    
    def estimate_daily_returns(self, capital: float, positions: int = 20,
                                trades_per_day_per_position: int = 3) -> dict:
        """
        Estimate daily returns based on active trading.
        
        REALISTIC MATH (calibrated to friend's actual returns):
        
        Friend's data: $100K, 15 positions, ~$733/day
        = ~$49/day per position
        = ~$6,667 per position
        = 0.73% daily per position
        = ~2-3 trades/day at ~$20-25 net profit each
        
        Profit per trade SCALES with position size:
        - $6,667 position → ~$20/trade
        - $333 position → ~$1/trade (proportional)
        
        With $1K / 3 positions = $333 each:
        - Best case: 3 trades × $1 = $3/day
        - But cheap positions have higher % moves (more volatile)
        - Realistic: $5-10/day with good execution
        """
        capital_per_pos = capital / positions
        avg_entry_price = 0.02  # 2¢ average
        shares_per_pos = capital_per_pos / avg_entry_price
        
        # Profit per trade scales with position size
        # Calibrated: $6667 position = $20/trade → ratio = 0.003
        avg_net_profit_per_trade = max(1.0, capital_per_pos * 0.003)
        
        # Small positions get slight volatility bonus (cheap positions swing more)
        if capital_per_pos < 1000:
            avg_net_profit_per_trade *= 1.5  # 50% bonus for high volatility
        
        total_trades_per_day = positions * trades_per_day_per_position
        net_daily_profit = avg_net_profit_per_trade * total_trades_per_day
        daily_return_pct = net_daily_profit / capital * 100
        
        return {
            "capital": capital,
            "positions": positions,
            "capital_per_position": capital_per_pos,
            "avg_entry_price": avg_entry_price,
            "shares_per_position": shares_per_pos,
            "trades_per_day_per_position": trades_per_day_per_position,
            "total_trades_per_day": total_trades_per_day,
            "avg_net_profit_per_trade": avg_net_profit_per_trade,
            "net_daily_profit": net_daily_profit,
            "daily_return_pct": daily_return_pct,
            "monthly_projection": net_daily_profit * 30,
            "yearly_projection": net_daily_profit * 365,
        }


if __name__ == "__main__":
    trader = ActiveTrader(
        take_profit_pct=0.15,    # Sell when up 15%
        stop_loss_pct=0.15,      # Tighter stop for small bankroll (15% vs 25%)
        max_hold_days=7,
        max_positions=20,
        capital_per_position=5000,
    )
    
    # Show projected returns at different capital levels
    print("=" * 60)
    print("ACTIVE TRADING PROJECTIONS")
    print("=" * 60)
    
    for capital, positions, tpd, label in [
        (1000, 3, 3, "$1K START (your situation)"),
        (1000, 3, 5, "$1K AGGRESSIVE"),
        (5000, 10, 3, "$5K GROWTH PHASE"),
        (10000, 15, 3, "$10K FULL STRATEGY (friend's start)"),
        (100000, 20, 3, "$100K (friend's current)"),
    ]:
        est = trader.estimate_daily_returns(capital, positions=positions,
                                            trades_per_day_per_position=tpd)
        print(f"\n{'─'*60}")
        print(f"  {label}")
        print(f"  ${capital:,} | {est['positions']} positions | {tpd} trades/day/pos")
        print(f"  ${est['capital_per_position']:,.0f} per position | {est['shares_per_position']:,.0f} shares @ 2¢")
        print(f"  {est['total_trades_per_day']} trades/day | ${est['avg_net_profit_per_trade']}/trade avg")
        print(f"  NET daily: ${est['net_daily_profit']:.0f} ({est['daily_return_pct']:.2f}%)")
        print(f"  Monthly: ${est['monthly_projection']:,.0f} | Yearly: ${est['yearly_projection']:,.0f}")
    
    print(f"\n{'='*60}")
    print("GROWTH PATH: $1K → $10K")
    print("="*60)
    
    # Simulate compounding growth from $1K
    balance = 1000
    days = 0
    milestones = {5000: None, 10000: None}
    
    while balance < 10000 and days < 365:
        # Adjust strategy as capital grows
        if balance < 2000:
            pos, tpd = 3, 3
        elif balance < 5000:
            pos, tpd = 5, 3
        elif balance < 10000:
            pos, tpd = 10, 3
        
        est = trader.estimate_daily_returns(balance, positions=pos,
                                            trades_per_day_per_position=tpd)
        daily = est['net_daily_profit']
        balance += daily
        days += 1
        
        for milestone in list(milestones.keys()):
            if balance >= milestone and milestones[milestone] is None:
                milestones[milestone] = days
                print(f"  Day {days}: ${balance:,.0f} — reached ${milestone:,} milestone!")
    
    print(f"\n  Final: ${balance:,.0f} in {days} days ({days/30:.1f} months)")
    print(f"  $1K → $5K: {milestones[5000]} days")
    print(f"  $1K → $10K: {milestones[10000]} days")
    print(f"\n  Once at $10K, switch to full 15-position strategy")
    print(f"  matching the friend's approach ($300+/day projected)")
