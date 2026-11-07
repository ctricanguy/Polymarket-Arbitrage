
class CryptoScalper:
    def __init__(self, private_key, funder, chain_id=137,
                 crypto_budget_pct=0.25, max_positions=15,
                 min_trade_size=2.0, max_trade_size=6.0,
                 scan_interval=3, daily_loss_limit=15.0,
                 builder_code=""):
        self.funder = funder
        self.crypto_budget_pct = crypto_budget_pct
        self.max_positions = max_positions
        self.min_trade_size = min_trade_size
        self.max_trade_size = max_trade_size
        self.scan_interval = scan_interval
        self.daily_loss_limit = daily_loss_limit
        self.builder_code = builder_code
        
        host = "https://clob.polymarket.com"
        self.clob = ClobClient(
            host, key=private_key, chain_id=chain_id,
            signature_type=SignatureTypeV2.POLY_1271,
            funder=funder,
        )
        creds = self.clob.create_or_derive_api_key()
        self.clob.set_api_creds(creds)
        logger.info(f"CLOB client initialized. Funder: {funder}")
        
        self.price_feed = PriceFeed()
        self.positions = {}
        self.daily_pnl = 0.0
        self.daily_pnl_reset = time.time()
        self.stopped_today = False
        self.total_trades = 0
        self.total_profit = 0.0
        self.scan_count = 0
        
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
        try:
            r = requests.get(f"{DATA}/positions", params={
                "user": self.funder.lower(), "assetType": "COLLATERAL", "limit": 1
            }, timeout=10)
            positions = r.json()
            if positions:
                return float(positions[0].get("size", 0) or 0)
        except:
            pass
        try:
            r2 = requests.get(f"{DATA}/value", params={"user": self.funder.lower()}, timeout=10)
            data = r2.json()
            return float(data.get("cash", 0) or 0)
        except:
            return 0.0
    
    def get_crypto_budget(self):
        cash = self.get_cash()
        budget = cash * self.crypto_budget_pct
        deployed = sum(p.get("cost", 0) for p in self.positions.values()
                       if p.get("status") in ("buying", "holding"))
        return max(0, budget - deployed)
