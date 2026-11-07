"""
Polymarket Arbitrage Bot — Main Loop
=====================================

Continuous monitoring + execution engine.

Strategies:
1. Sum-of-probabilities arbitrage on negRisk events (aggressive + passive limit)
2. Limit order basket arbitrage (place passive orders summing to <100¢)
3. Real-time mispricing detection on liquid markets

The bot runs in a loop:
  SCAN → DETECT → EVALUATE RISK → EXECUTE → MONITOR → REPEAT
"""

import os
import sys
import time
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner import MarketScanner, Event, Market
from detector import ArbitrageDetector, ArbitrageOpportunity
from advanced_detector import AdvancedArbitrageDetector
from risk_manager import RiskManager, RiskConfig
from execution import ExecutionEngine, WalletConfig, OrderResult, setup_wallet_from_env

logger = logging.getLogger("polymarket-arb.bot")


@dataclass
class BotConfig:
    """Bot configuration."""
    # Scanning
    scan_interval: int = 30  # seconds between full scans
    min_market_volume: float = 5000  # only scan markets with >$5K volume
    min_event_markets: int = 3  # min markets per event for arb
    max_spread: float = 0.05  # max bid-ask spread to consider tradeable
    
    # Arbitrage detection
    min_mispricing: float = 0.005  # 0.5% minimum mispricing
    min_profit_per_share: float = 0.01  # 1¢ minimum profit per share
    
    # Risk management
    max_total_exposure: float = 100000.0  # $100K default
    max_per_trade: float = 5000.0
    min_profit_threshold: float = 5.0  # min $5 profit per trade
    max_concurrent_positions: int = 20
    min_depth: float = 100.0  # min 100 shares of depth
    
    # Execution
    use_limit_orders: bool = True  # passive limit orders (better fill prices)
    limit_order_edge: float = 0.3  # how far into spread to place (0=at bid, 1=at ask)
    order_timeout: int = 300  # cancel unfilled orders after 5 min
    
    # Alerts
    alert_on_opportunity: bool = True
    min_alert_profit: float = 10.0  # only alert for >$10 potential profit
    
    # Storage
    data_dir: str = os.path.expanduser("~/.polymarket-arb-bot")
    log_dir: str = os.path.expanduser("~/.polymarket-arb-bot/logs")


class PolymarketArbBot:
    """
    Main bot controller.
    
    Runs the scan-detect-execute loop, manages positions, logs everything.
    """

    def __init__(self, config: BotConfig, execution_engine: Optional[ExecutionEngine] = None):
        self.config = config
        self.scanner = MarketScanner(max_workers=30)
        self.detector: Optional[ArbitrageDetector] = None
        self.advanced_detector: Optional[AdvancedArbitrageDetector] = None
        self.execution = execution_engine
        
        # Risk manager
        risk_config = RiskConfig(
            max_total_exposure=config.max_total_exposure,
            max_per_trade=config.max_per_trade,
            min_profit_threshold=config.min_profit_threshold,
            max_concurrent_positions=config.max_concurrent_positions,
            min_depth=config.min_depth,
            max_spread=config.max_spread,
        )
        self.risk_manager = RiskManager(risk_config)
        
        # State
        self.running = False
        self.scan_count = 0
        self.opportunities_found = 0
        self.trades_executed = 0
        self.start_time = 0
        self.open_orders: dict = {}  # order_id -> order metadata
        
        # Setup directories
        Path(self.config.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.config.log_dir).mkdir(parents=True, exist_ok=True)
        
        # Stats file
        self.stats_file = os.path.join(self.config.data_dir, "stats.json")

    def setup_logging(self):
        """Configure file + console logging."""
        log_file = os.path.join(self.config.log_dir, f"bot_{datetime.now().strftime('%Y%m%d')}.log")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(),
            ],
        )

    def scan_and_detect(self) -> list:
        """Run one scan + detect cycle. Returns all opportunities found."""
        logger.info(f"=== Scan #{self.scan_count + 1} ===")
        
        # Scan markets
        summary = self.scanner.scan(
            fetch_books=True,
            min_volume=self.config.min_market_volume,
            min_markets=self.config.min_event_markets,
        )
        logger.info(f"Scan: {summary}")
        
        # Basic detection (sum-of-probabilities)
        self.detector = ArbitrageDetector(
            self.scanner,
            min_mispricing=self.config.min_mispricing,
            min_net_profit=self.config.min_profit_threshold,
        )
        basic_opps = self.detector.detect_all()
        
        # Advanced detection (limit orders + depth)
        self.advanced_detector = AdvancedArbitrageDetector(
            self.scanner,
            min_profit_per_share=self.config.min_profit_per_share,
            max_spread=self.config.max_spread,
            min_liquidity=self.config.min_market_volume,
        )
        limit_opps = self.advanced_detector.detect_limit_order_arb()
        depth_opps = self.advanced_detector.detect_depth_arb()
        
        all_opps = basic_opps + depth_opps
        
        # Log opportunities
        executable = [o for o in all_opps if o.executable]
        if executable:
            logger.info(f"🔥 {len(executable)} EXECUTABLE opportunities found!")
            for opp in executable[:5]:
                logger.info(f"  {opp.event_title[:40]} | "
                           f"profit: ${opp.net_profit_usd:.2f} | "
                           f"investment: ${opp.max_investment_usd:.2f}")
        
        if limit_opps:
            logger.info(f"📊 {len(limit_opps)} limit-order arb setups found")
            for opp in limit_opps[:3]:
                logger.info(f"  {opp.event_title[:40]} | "
                           f"sum: {opp.sum_target_prices*100:.1f}% | "
                           f"profit/share: {opp.expected_profit_per_share*100:.1f}¢")
        
        self.scan_count += 1
        self.opportunities_found += len(all_opps)
        
        return all_opps, limit_opps

    def execute_opportunity(self, opp: ArbitrageOpportunity) -> bool:
        """
        Execute an arbitrage opportunity.
        
        For sum_underpriced: BUY Yes on all outcomes (aggressive = hit asks)
        For sum_overpriced: SELL Yes on all outcomes (aggressive = hit bids)
        """
        if not self.execution:
            logger.warning("No execution engine configured — dry run only")
            return False
        
        logger.info(f"Executing arb: {opp.event_title[:40]} | "
                     f"type={opp.arb_type} | "
                     f"profit=${opp.net_profit_usd:.2f}")
        
        # Calculate position size
        available = self.risk_manager.config.max_total_exposure - self.risk_manager.total_exposure
        size = min(
            available / max(opp.max_investment_usd, 0.01) * opp.net_profit_usd,
            self.config.max_per_trade / opp.max_investment_usd * opp.net_profit_usd,
        ) if opp.max_investment_usd > 0 else 0
        
        # Use min depth from the opportunity
        min_depth = min(md.get("available_size", 0) for md in opp.markets)
        size = min(size, min_depth * 0.9)  # 90% of available depth
        
        if size < 5:  # min order size
            logger.warning(f"Size too small: {size:.2f}")
            return False
        
        # Risk check
        investment = opp.max_investment_usd * (size / min_depth) if min_depth > 0 else 0
        spread = max(abs(md.get("spread", 0)) for md in opp.markets) if opp.markets else 1
        
        allowed, reason = self.risk_manager.can_trade(
            investment_amount=investment,
            expected_profit=opp.net_profit_usd * (size / min_depth) if min_depth > 0 else 0,
            orderbook_depth=min_depth,
            spread=spread,
        )
        
        if not allowed:
            logger.warning(f"Risk check failed: {reason}")
            return False
        
        # Place orders on all outcomes
        results = []
        for md in opp.markets:
            token_id = md.get("token_id", "")
            if not token_id:
                continue
            
            price = md.get("exec_price", 0)
            side = md.get("side", "BUY")
            
            if side == "BUY":
                result = self.execution.place_buy_order(token_id, price, size)
            else:
                result = self.execution.place_sell_order(token_id, price, size)
            
            results.append(result)
            
            if result.success:
                logger.info(f"  ✅ {side} {size:.0f} @ {price:.4f} | order_id={result.order_id}")
                self.open_orders[result.order_id] = {
                    "event_title": opp.event_title,
                    "token_id": token_id,
                    "side": side,
                    "price": price,
                    "size": size,
                    "placed_at": time.time(),
                }
            else:
                logger.error(f"  ❌ {side} {size:.0f} @ {price:.4f} | error={result.error}")
        
        success_count = sum(1 for r in results if r.success)
        if success_count == len(opp.markets):
            logger.info(f"All {success_count} orders placed successfully")
            self.trades_executed += 1
            self.risk_manager.register_position({
                "id": opp.event_id,
                "event_title": opp.event_title,
                "investment": investment,
                "orders": [r.order_id for r in results if r.success],
            })
            return True
        elif success_count > 0:
            logger.warning(f"Partial fill: {success_count}/{len(opp.markets)} orders placed")
            # Cancel all orders since we need ALL outcomes for the arb to work
            for r in results:
                if r.success:
                    self.execution.cancel_order(r.order_id)
                    self.open_orders.pop(r.order_id, None)
            logger.info("Cancelled partial orders (need all outcomes for arb)")
            return False
        else:
            logger.error("All orders failed")
            return False

    def place_limit_order_basket(self, opp) -> bool:
        """
        Place passive limit buy orders on all outcomes of a negRisk event.
        If all fill, we've bought all outcomes for < $1.00 total = guaranteed profit.
        """
        if not self.execution:
            logger.warning("No execution engine — skipping limit order placement")
            return False
        
        logger.info(f"Placing limit order basket: {opp.event_title[:40]} | "
                     f"sum={opp.sum_target_prices*100:.1f}% | "
                     f"profit/share={opp.expected_profit_per_share*100:.1f}¢")
        
        # Calculate size per market
        available = self.risk_manager.config.max_total_exposure - self.risk_manager.total_exposure
        max_shares = available / opp.sum_target_prices if opp.sum_target_prices > 0 else 0
        max_shares = min(max_shares, self.config.max_per_trade / opp.sum_target_prices)
        
        # Limit by min depth across markets
        for md in opp.markets:
            depth = md.get("ask_depth", 0) or md.get("bid_depth", 0)
            max_shares = min(max_shares, depth * 0.5)  # Don't take more than half the depth
        
        size = max_shares * 0.9  # safety margin
        
        if size < 5:
            logger.warning(f"Size too small for limit basket: {size:.2f}")
            return False
        
        # Place buy limit orders at target prices
        results = []
        for i, md in enumerate(opp.markets):
            token_id = md.get("token_id", "")
            target_price = opp.target_buy_prices[i]
            
            result = self.execution.place_buy_order(token_id, target_price, size)
            results.append(result)
            
            if result.success:
                logger.info(f"  📈 BUY LIMIT {size:.0f} @ {target_price:.4f} | {md['question'][:30]}")
                self.open_orders[result.order_id] = {
                    "event_title": opp.event_title,
                    "token_id": token_id,
                    "side": "BUY",
                    "price": target_price,
                    "size": size,
                    "placed_at": time.time(),
                    "type": "limit_basket",
                }
            else:
                logger.error(f"  ❌ Failed: {result.error}")
        
        success_count = sum(1 for r in results if r.success)
        logger.info(f"Limit basket: {success_count}/{len(opp.markets)} orders placed")
        
        if success_count > 0:
            self.trades_executed += 1
            return True
        return False

    def check_open_orders(self):
        """Check status of open orders, remove filled/cancelled ones."""
        if not self.execution or not self.open_orders:
            return
        
        filled_orders = []
        for order_id, meta in list(self.open_orders.items()):
            try:
                status = self.execution.get_order_status(order_id)
                order_status = status.get("status", "UNKNOWN") if isinstance(status, dict) else "UNKNOWN"
                
                if order_status in ("MATCHED", "FILLED"):
                    filled_orders.append((order_id, meta))
                    logger.info(f"  ✅ FILLED: {order_id} | {meta['event_title'][:30]}")
                    del self.open_orders[order_id]
                elif order_status in ("CANCELLED", "EXPIRED"):
                    logger.info(f"  ⏰ Cancelled: {order_id}")
                    del self.open_orders[order_id]
                    
            except Exception as e:
                logger.debug(f"Order check failed for {order_id}: {e}")
        
        # Check for completed baskets (all orders for an event filled)
        if filled_orders:
            self._check_completed_baskets(filled_orders)

    def _check_completed_baskets(self, filled_orders: list):
        """Check if any limit order baskets have fully filled."""
        # Group by event
        by_event = {}
        for order_id, meta in filled_orders:
            event = meta.get("event_title", "")
            if event not in by_event:
                by_event[event] = []
            by_event[event].append((order_id, meta))
        
        # Check if any event has all outcomes filled
        # (This is simplified — real implementation would track expected count)
        for event, orders in by_event.items():
            logger.info(f"  🎯 Basket fill: {event[:30]} | {len(orders)} orders filled")

    def cancel_stale_orders(self):
        """Cancel orders that have been open too long."""
        if not self.execution or not self.open_orders:
            return
        
        now = time.time()
        stale = []
        for order_id, meta in list(self.open_orders.items()):
            age = now - meta.get("placed_at", now)
            if age > self.config.order_timeout:
                stale.append(order_id)
        
        for order_id in stale:
            logger.info(f"Cancelling stale order: {order_id}")
            self.execution.cancel_order(order_id)
            self.open_orders.pop(order_id, None)

    def save_stats(self):
        """Save bot statistics to file."""
        stats = {
            "scan_count": self.scan_count,
            "opportunities_found": self.opportunities_found,
            "trades_executed": self.trades_executed,
            "open_orders": len(self.open_orders),
            "risk_status": self.risk_manager.get_status(),
            "uptime_seconds": time.time() - self.start_time if self.start_time else 0,
            "timestamp": datetime.now().isoformat(),
        }
        with open(self.stats_file, "w") as f:
            json.dump(stats, f, indent=2)

    def print_status(self):
        """Print current bot status."""
        uptime = time.time() - self.start_time if self.start_time else 0
        risk = self.risk_manager.get_status()
        
        print(f"\n{'='*50}")
        print(f"  POLYMARKET ARB BOT — STATUS")
        print(f"{'='*50}")
        print(f"  Uptime: {uptime/3600:.1f}h")
        print(f"  Scans: {self.scan_count}")
        print(f"  Opportunities found: {self.opportunities_found}")
        print(f"  Trades executed: {self.trades_executed}")
        print(f"  Open orders: {len(self.open_orders)}")
        print(f"  Total exposure: ${risk['total_exposure']:.2f}")
        print(f"  Available capital: ${risk['available_capital']:.2f}")
        print(f"  Daily PnL: ${risk['daily_pnl']:.2f}")
        print(f"{'='*50}\n")

    def run(self, dry_run: bool = False):
        """
        Main bot loop.
        
        Args:
            dry_run: If True, scan and detect but don't execute trades
        """
        self.running = True
        self.start_time = time.time()
        self.setup_logging()
        
        logger.info("🤖 Polymarket Arbitrage Bot starting...")
        logger.info(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
        logger.info(f"  Max exposure: ${self.config.max_total_exposure}")
        logger.info(f"  Scan interval: {self.config.scan_interval}s")
        
        if not dry_run and not self.execution:
            logger.warning("No execution engine — falling back to dry run mode")
            dry_run = True
        
        try:
            while self.running:
                try:
                    # 1. Scan + detect
                    all_opps, limit_opps = self.scan_and_detect()
                    
                    # 2. Execute aggressive opportunities (depth arb)
                    executable = [o for o in all_opps if o.executable]
                    for opp in executable:
                        if not dry_run and self.execution:
                            self.execute_opportunity(opp)
                        else:
                            logger.info(f"[DRY] Would execute: {opp.event_title[:30]} | "
                                       f"profit=${opp.net_profit_usd:.2f}")
                    
                    # 3. Place passive limit order baskets
                    if not dry_run and self.execution:
                        for lopp in limit_opps[:3]:  # Top 3 setups
                            self.place_limit_order_basket(lopp)
                    else:
                        for lopp in limit_opps[:3]:
                            logger.info(f"[DRY] Would place limit basket: {lopp.event_title[:30]} | "
                                       f"profit/share={lopp.expected_profit_per_share*100:.1f}¢")
                    
                    # 4. Manage open orders
                    if not dry_run and self.execution:
                        self.check_open_orders()
                        self.cancel_stale_orders()
                    
                    # 5. Save stats
                    self.save_stats()
                    self.print_status()
                    
                    # 6. Wait for next cycle
                    logger.info(f"Next scan in {self.config.scan_interval}s...")
                    time.sleep(self.config.scan_interval)
                    
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(f"Error in main loop: {e}", exc_info=True)
                    time.sleep(10)  # Wait before retrying
                    
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        finally:
            self.running = False
            self.save_stats()
            logger.info(f"Bot stopped. Final stats: scans={self.scan_count}, "
                       f"trades={self.trades_executed}")
            
            # Cancel all open orders on shutdown
            if not dry_run and self.execution and self.open_orders:
                logger.info(f"Cancelling {len(self.open_orders)} open orders...")
                self.execution.cancel_all_orders()


def create_config_from_env() -> BotConfig:
    """Create bot config from environment variables."""
    return BotConfig(
        max_total_exposure=float(os.environ.get("BOT_MAX_EXPOSURE", "100000")),
        max_per_trade=float(os.environ.get("BOT_MAX_PER_TRADE", "5000")),
        scan_interval=int(os.environ.get("BOT_SCAN_INTERVAL", "30")),
        min_market_volume=float(os.environ.get("BOT_MIN_VOLUME", "5000")),
        min_profit_threshold=float(os.environ.get("BOT_MIN_PROFIT", "5.0")),
    )


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Bot")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, no execution")
    parser.add_argument("--live", action="store_true", help="Enable live trading (requires wallet)")
    parser.add_argument("--exposure", type=float, default=100000, help="Max total exposure in USDC")
    args = parser.parse_args()
    
    config = create_config_from_env()
    config.max_total_exposure = args.exposure
    
    execution = None
    if args.live and not args.dry_run:
        execution = setup_wallet_from_env()
        if not execution:
            print("❌ Failed to initialize wallet. Running in dry-run mode.")
            args.dry_run = True
    
    bot = PolymarketArbBot(config, execution_engine=execution)
    bot.run(dry_run=args.dry_run)
