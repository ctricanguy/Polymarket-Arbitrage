#!/usr/bin/env python3
# Part 4: market fetching + orderbook (methods of CryptoScalper class)
# This file is a fragment, not standalone

    def fetch_active_crypto_markets(self):
        """Find all active 5-min and 15-min crypto up/down markets for BTC, ETH, XRP, BNB."""
        now_ts = int(time.time())
        markets = []
        
        for crypto, prefix in [("BTC", "btc"), ("ETH", "eth"), ("XRP", "xrp"), ("BNB", "bnb"), ("SOL", "sol")]:
            minute = datetime.fromtimestamp(now_ts, tz=timezone.utc).minute
            second = datetime.fromtimestamp(now_ts, tz=timezone.utc).second
            
            seconds_to_next_5 = (5 - (minute % 5)) * 60 - second
            next_5_end = now_ts + seconds_to_next_5
            
            for offset_min in [0, -5]:
                end_ts = next_5_end + (offset_min * 60)
                if end_ts < now_ts + 30:
                    continue
                slug = f"{prefix}-updown-5m-{end_ts}"
                m = self._fetch_market_by_slug(slug, crypto, "5m", end_ts)
                if m:
                    markets.append(m)
            
            seconds_to_next_15 = (15 - (minute % 15)) * 60 - second
            next_15_end = now_ts + seconds_to_next_15
            slug = f"{prefix}-updown-15m-{next_15_end}"
            m = self._fetch_market_by_slug(slug, crypto, "15m", next_15_end)
            if m:
                markets.append(m)
        
        return markets
    
    def _fetch_market_by_slug(self, slug, crypto, window, end_ts):
        try:
            r = requests.get(f"{GAMMA}/events", params={"limit": 1, "slug": slug}, timeout=8)
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
        except:
            return None
    
    def get_orderbook(self, token_id):
        try:
            book = self.clob.get_order_book(token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1
            return best_bid, best_ask, bids, asks
        except:
            return 0, 1, [], []
