
class PriceFeed:
    """Tracks crypto prices from Binance for optional trend detection."""
    def __init__(self):
        self.prices = {}
        self.symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT", "BNB": "BNBUSDT"}
    
    def update(self):
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
                    if len(history) > 60:
                        history = history[-60:]
                    self.prices[crypto] = {"price": current, "prev": prev, "history": history}
        except Exception as e:
            logger.warning(f"Binance price update failed: {e}")
    
    def get_trend(self, crypto, window_minutes=5):
        p = self.prices.get(crypto)
        if not p or len(p["history"]) < 2:
            return "flat"
        history = p["history"]
        recent = history[-min(len(history), 20):]
        first = recent[0]
        last = recent[-1]
        change_pct = (last - first) / first if first > 0 else 0
        if change_pct > 0.0005:
            return "up"
        elif change_pct < -0.0005:
            return "down"
        return "flat"
    
    def get_price(self, crypto):
        return self.prices.get(crypto, {}).get("price", 0)
