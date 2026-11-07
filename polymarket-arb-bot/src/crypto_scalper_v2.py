#!/usr/bin/env python3
"""
Crypto Scalper V2 — replicates @nj23adsknml3 strategy.
100% crypto Up/Down 5-min and 15-min markets.
Strategy: Buy at 30-50¢ on both sides + directional, sell on spikes, hold for redeem.
Proven: +$37,813 in one day, 100% win rate across 116 markets.

Key differences from V1:
- Buy at 30-50¢ (NOT 89-99¢)
- Buy BOTH sides when prices are near 50¢ (guaranteed win)
- Sell on ANY price spike (bid > buy_price + 5¢)
- Hold remaining shares for resolution ($1.00 redeem)
- Trade BTC, ETH, XRP, BNB simultaneously
- Small trade sizes ($2-5), high volume
- 3-second scan interval for rapid cycling
"""
import os, sys, json, time, logging, argparse, threading
from datetime import datetime, timezone
from collections import defaultdict
import requests

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderPayload, PartialCreateOrderOptions, OrderType
from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.constants import POLYGON
import logging as _logging
_logging.getLogger("py_clob_client_v2.http_helpers.helpers").setLevel(_logging.CRITICAL)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("crypto_scalper_v2")

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
STATE_FILE = os.path.expanduser("~/polymarket-arb-bot/data/crypto_scalper/state.json")
STATE_LOCK = threading.Lock()
BINANCE_API = "https://api.binance.com/api/v3"
