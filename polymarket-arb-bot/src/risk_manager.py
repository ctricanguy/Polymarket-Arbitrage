"""
Risk Manager
============
Controls position sizing, exposure limits, and risk controls for the bot.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("polymarket-arb.risk")


@dataclass
class RiskConfig:
    """Risk management configuration."""
    # Maximum total capital to deploy (USDC)
    max_total_exposure: float = 1000.0
    # Maximum per-trade investment
    max_per_trade: float = 200.0
    # Minimum profit threshold to execute (USDC)
    min_profit_threshold: float = 0.50
    # Maximum number of concurrent open positions
    max_concurrent_positions: int = 10
    # Maximum slippage tolerance (as decimal, 0.02 = 2%)
    max_slippage: float = 0.02
    # Stop loss: exit if unrealized loss exceeds this (USDC)
    stop_loss_usd: float = 10.0
    # Maximum time to hold a position (seconds) — 0 = no limit
    max_hold_time: int = 0
    # Minimum orderbook depth required (shares)
    min_depth: float = 50.0
    # Maximum spread to trade (as decimal)
    max_spread: float = 0.05


class RiskManager:
    """Evaluates and controls risk for all trades."""

    def __init__(self, config: RiskConfig):
        self.config = config
        self.open_positions: list = []  # Track open positions
        self.total_exposure: float = 0.0
        self.daily_pnl: float = 0.0
        self.trades_today: int = 0

    def can_trade(self, investment_amount: float, expected_profit: float, 
                  orderbook_depth: float, spread: float) -> tuple[bool, str]:
        """
        Check if a trade passes all risk controls.
        
        Returns (allowed, reason).
        """
        # Check total exposure
        if self.total_exposure + investment_amount > self.config.max_total_exposure:
            return False, f"Would exceed max total exposure ({self.config.max_total_exposure} USDC)"

        # Check per-trade limit
        if investment_amount > self.config.max_per_trade:
            return False, f"Exceeds max per-trade ({self.config.max_per_trade} USDC)"

        # Check profit threshold
        if expected_profit < self.config.min_profit_threshold:
            return False, f"Profit below threshold ({self.config.min_profit_threshold} USDC)"

        # Check concurrent positions
        if len(self.open_positions) >= self.config.max_concurrent_positions:
            return False, f"Max concurrent positions reached ({self.config.max_concurrent_positions})"

        # Check orderbook depth
        if orderbook_depth < self.config.min_depth:
            return False, f"Insufficient depth ({orderbook_depth:.0f} < {self.config.min_depth})"

        # Check spread
        if spread > self.config.max_spread:
            return False, f"Spread too wide ({spread:.4f} > {self.config.max_spread})"

        return True, "OK"

    def calculate_position_size(self, available_capital: float, 
                                max_depth: float, price: float) -> float:
        """
        Calculate optimal position size.
        
        Size is limited by:
        - Available capital
        - Max per-trade limit
        - Orderbook depth (don't exceed what's available)
        """
        # Can't invest more than available capital
        max_by_capital = min(
            available_capital,
            self.config.max_per_trade,
        ) / price  # Convert USDC to shares

        # Can't exceed orderbook depth
        max_by_depth = max_depth

        # Use the smaller of the two, with a 90% safety margin
        size = min(max_by_capital, max_by_depth) * 0.9

        return max(size, 0)

    def register_position(self, position: dict):
        """Register a new open position."""
        self.open_positions.append(position)
        self.total_exposure += position.get("investment", 0)
        self.trades_today += 1
        logger.info(f"Position opened: {position.get('event_title', '?')[:30]} | "
                     f"Investment: ${position.get('investment', 0):.2f}")

    def close_position(self, position_id: str, realized_pnl: float):
        """Close a position and update PnL."""
        self.open_positions = [p for p in self.open_positions if p.get("id") != position_id]
        self.total_exposure -= sum(
            p.get("investment", 0) for p in self.open_positions
        )
        self.daily_pnl += realized_pnl
        logger.info(f"Position closed: {position_id} | PnL: ${realized_pnl:.2f} | "
                     f"Daily PnL: ${self.daily_pnl:.2f}")

    def get_status(self) -> dict:
        """Get current risk status."""
        return {
            "open_positions": len(self.open_positions),
            "total_exposure": self.total_exposure,
            "available_capital": self.config.max_total_exposure - self.total_exposure,
            "daily_pnl": self.daily_pnl,
            "trades_today": self.trades_today,
            "max_exposure": self.config.max_total_exposure,
        }
