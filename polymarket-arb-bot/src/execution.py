"""
Polymarket Wallet & Execution Engine
=====================================

Handles wallet authentication, order signing (EIP-712), and order submission
to Polymarket's CLOB API using the official py-clob-client SDK.

Requirements:
- EVM wallet private key (Polygon network)
- USDC balance on Polygon
- Polymarket proxy/Safety Module registration (done via Polymarket UI)
- API credentials (derived from wallet signature)

The flow:
1. Create ClobClient with wallet credentials
2. Derive API credentials (one-time setup)
3. Create and sign orders using EIP-712
4. Submit orders to CLOB POST endpoints
5. Monitor order status and fills
"""

import logging
import time
import json
import os
from typing import Optional
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("polymarket-arb.execution")

# Polymarket contract addresses on Polygon
POLYGON_CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"
NEG_RISK_CLOB_HOST = "https://clob.polymarket.com"  # Same host, different exchange contract


@dataclass
class WalletConfig:
    """Wallet configuration for Polymarket trading."""
    private_key: str  # EVM private key (with or without 0x prefix)
    # Polymarket contract addresses (auto-filled from chain)
    conditional_address: str = ""  # ConditionalTokens contract
    exchange_address: str = ""  # Exchange contract
    neg_risk_exchange_address: str = ""  # NegRisk Exchange contract
    neg_risk_conditional_address: str = ""  # NegRisk ConditionalTokens
    chain_id: int = POLYGON_CHAIN_ID


@dataclass
class OrderResult:
    """Result of an order submission."""
    success: bool
    order_id: str = ""
    error: str = ""
    order_data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class ExecutionEngine:
    """
    Order execution engine using Polymarket's py-clob-client.
    
    Handles:
    - Wallet authentication
    - API credential derivation
    - Order creation and signing (EIP-712)
    - Order submission
    - Order cancellation
    - Balance checks
    """

    def __init__(self, wallet_config: WalletConfig):
        self.config = wallet_config
        self.client = None
        self.api_creds = None
        self._initialize_client()

    def _initialize_client(self):
        """Initialize the ClobClient with wallet credentials."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:
            logger.error(f"py-clob-client not installed: {e}")
            raise

        pk = self.config.private_key
        if not pk.startswith("0x"):
            pk = "0x" + pk

        # Create the client — it will auto-detect contract addresses
        # For negRisk markets, we need the neg-risk exchange
        self.client = ClobClient(
            host=CLOB_HOST,
            key=pk,
            chain_id=self.config.chain_id,
        )

        # Get contract addresses from the client
        try:
            self.config.conditional_address = self.client.get_conditional_address()
            self.config.exchange_address = self.client.get_exchange_address()
            logger.info(f"Conditional address: {self.config.conditional_address}")
            logger.info(f"Exchange address: {self.config.exchange_address}")
        except Exception as e:
            logger.warning(f"Could not auto-detect contract addresses: {e}")

        logger.info("ClobClient initialized successfully")

    def derive_api_credentials(self) -> bool:
        """
        Derive API credentials from the wallet signature.
        This is a one-time setup — save the credentials for reuse.
        """
        try:
            self.api_creds = self.client.create_or_derive_api_creds()
            logger.info("API credentials derived successfully")
            logger.info(f"  API key: {self.api_creds.api_secret[:10]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to derive API credentials: {e}")
            return False

    def set_api_creds(self, api_key: str, api_secret: str, api_passphrase: str):
        """Set existing API credentials."""
        from py_clob_client.clob_types import ApiCreds
        self.api_creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        self.client.set_api_creds(self.api_creds)
        logger.info("API credentials set")

    def save_credentials(self, path: str):
        """Save API credentials to a file for reuse."""
        if not self.api_creds:
            logger.error("No API credentials to save")
            return
        creds = {
            "api_key": self.api_creds.api_key,
            "api_secret": self.api_creds.api_secret,
            "api_passphrase": self.api_creds.api_passphrase,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(creds, f, indent=2)
        os.chmod(path, 0o600)
        logger.info(f"Credentials saved to {path}")

    def load_credentials(self, path: str) -> bool:
        """Load API credentials from a file."""
        try:
            with open(path) as f:
                creds = json.load(f)
            self.set_api_creds(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                api_passphrase=creds["api_passphrase"],
            )
            return True
        except FileNotFoundError:
            return False
        except Exception as e:
            logger.error(f"Failed to load credentials: {e}")
            return False

    def get_balance(self, asset_type: str = "COLLATERAL") -> dict:
        """Get USDC balance and allowance."""
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL if asset_type == "COLLATERAL" else AssetType.CONDITIONAL,
            )
            result = self.client.get_balance_allowance(params)
            return result
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return {}

    def place_buy_order(self, token_id: str, price: float, size: float, 
                        tick_size: str = "0.01") -> OrderResult:
        """
        Place a BUY limit order for a Yes token.
        
        Args:
            token_id: The CLOB token ID to buy
            price: Limit price (0.0 to 1.0), e.g., 0.65
            size: Number of shares to buy
            tick_size: Price increment ("0.01" or "0.001")
        
        Returns:
            OrderResult with success status and order ID
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.constants import BUY

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            )
            
            signed_order = self.client.create_order(order_args)
            
            if signed_order is None:
                return OrderResult(success=False, error="Failed to create signed order")

            response = self.client.post_order(signed_order, OrderType.GTC)
            
            if response and isinstance(response, dict):
                order_id = response.get("orderID", response.get("id", ""))
                success = response.get("status") in ("LIVE", "PLACED", "MATCHED", "")
                return OrderResult(
                    success=success,
                    order_id=str(order_id),
                    order_data=response,
                    error="" if success else str(response),
                )
            else:
                return OrderResult(
                    success=False,
                    error=f"Unexpected response: {response}",
                )

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return OrderResult(success=False, error=str(e))

    def place_sell_order(self, token_id: str, price: float, size: float,
                         tick_size: str = "0.01") -> OrderResult:
        """Place a SELL limit order for a Yes token."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.constants import SELL

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=SELL,
            )
            
            signed_order = self.client.create_order(order_args)
            
            if signed_order is None:
                return OrderResult(success=False, error="Failed to create signed order")

            response = self.client.post_order(signed_order, OrderType.GTC)
            
            if response and isinstance(response, dict):
                order_id = response.get("orderID", response.get("id", ""))
                success = response.get("status") in ("LIVE", "PLACED", "MATCHED", "")
                return OrderResult(
                    success=success,
                    order_id=str(order_id),
                    order_data=response,
                    error="" if success else str(response),
                )
            else:
                return OrderResult(
                    success=False,
                    error=f"Unexpected response: {response}",
                )

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return OrderResult(success=False, error=str(e))

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            self.client.cancel(order_id)
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            logger.error(f"Cancel all failed: {e}")
            return False

    def get_open_orders(self) -> list:
        """Get all open orders."""
        try:
            return self.client.get_orders()
        except Exception as e:
            logger.error(f"Get orders failed: {e}")
            return []

    def get_order_status(self, order_id: str) -> dict:
        """Get status of a specific order."""
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            logger.error(f"Get order status failed: {e}")
            return {}


def setup_wallet_from_env() -> Optional[ExecutionEngine]:
    """
    Initialize execution engine from environment variables.
    
    Required env vars:
    - POLY_PRIVATE_KEY: EVM private key for Polymarket wallet
    
    Optional:
    - POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE: Pre-derived API creds
    """
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    if not private_key:
        logger.error("POLY_PRIVATE_KEY environment variable not set")
        logger.error("Set it with: export POLY_PRIVATE_KEY='your_private_key_here'")
        return None

    config = WalletConfig(private_key=private_key)
    
    try:
        engine = ExecutionEngine(config)
        
        # Try to load existing API credentials, or derive new ones
        creds_path = os.environ.get("POLY_CREDS_PATH", 
                                     os.path.expanduser("~/.polymarket/creds.json"))
        
        if os.path.exists(creds_path):
            if engine.load_credentials(creds_path):
                logger.info("Loaded existing API credentials")
            else:
                if engine.derive_api_credentials():
                    engine.save_credentials(creds_path)
        else:
            if engine.derive_api_credentials():
                engine.save_credentials(creds_path)
            else:
                logger.error("Failed to derive API credentials")
                return None
        
        return engine
        
    except Exception as e:
        logger.error(f"Wallet initialization failed: {e}")
        return None


if __name__ == "__main__":
    # Demo: check if wallet is configured
    import sys
    
    if not os.environ.get("POLY_PRIVATE_KEY"):
        print("⚠️  No wallet configured!")
        print()
        print("To set up your Polymarket wallet:")
        print("1. Export your EVM private key:")
        print("   export POLY_PRIVATE_KEY='your_private_key_here'")
        print()
        print("2. Make sure you have USDC on Polygon in this wallet")
        print("3. Make sure you've registered on Polymarket (proxy/Safety Module)")
        print("   Visit https://polymarket.com and complete account setup")
        print()
        print("4. Re-run this script to verify connection")
        sys.exit(1)
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    engine = setup_wallet_from_env()
    
    if engine:
        print("✅ Wallet connected successfully!")
        balance = engine.get_balance()
        print(f"   Balance: {balance}")
    else:
        print("❌ Wallet setup failed")
        sys.exit(1)
