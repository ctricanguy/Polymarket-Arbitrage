#!/usr/bin/env python3
"""
Polymarket Fill Rate Monitor
=============================
Watches real CLOB orderbooks and detects actual trades.

How it works:
  1. Poll /book for each market every 3 seconds
  2. Track bid_size and ask_size over time
  3. When sizes drop, a trade happened at that level
  4. Count trades per market per hour
  5. Output real fill rates the bot can use

This replaces the bot's guessed fill probabilities (5%, 2%, etc.)
with measured reality.
"""

import argparse
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("polymarket-arb.fill_monitor")

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"


class FillMonitor:
    def __init__(
        self,
        max_markets: int = 30,
        poll_interval: int = 3,
        duration: int = 3600,
        output_file: str = "/home/star/polymarket-arb-bot/data/fill_rates.json",
    ):
        self.max_markets = max_markets
        self.poll_interval = poll_interval
        self.duration = duration
        self.output_file = Path(output_file)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)

        # token_id → market info
        self.markets = {}

        # token_id → list of detected trades
        # Each trade: {timestamp, side, price, size_change, prev_size, new_size}
        self.trades = defaultdict(list)

        # token_id → previous orderbook state
        self.prev_book = {}

        # token_id → poll count
        self.poll_count = defaultdict(int)

        self.start_time = time.time()

    def discover_markets(self):
        """Find low-YES markets with good spreads to monitor."""
        all_markets = []
        for offset in range(0, 10000, 100):
            try:
                r = requests.get(GAMMA_API, params={
                    "limit": 100, "offset": offset,
                    "active": "true", "closed": "false",
                }, timeout=15)
                data = r.json()
                if not data or not isinstance(data, list):
                    break
                all_markets.extend([m for m in data if isinstance(m, dict)])
                if len(data) < 100:
                    break
            except:
                break

        candidates = []
        for m in all_markets:
            try:
                prices = m.get("outcomePrices", "[]")
                clob_ids = m.get("clobTokenIds", "[]")
                if isinstance(prices, str): prices = json.loads(prices)
                if isinstance(clob_ids, str): clob_ids = json.loads(clob_ids)
                if not isinstance(prices, list) or not isinstance(clob_ids, list):
                    continue
                if len(prices) < 1 or len(clob_ids) < 1:
                    continue
                yes_price = float(prices[0])
                if not (0.01 <= yes_price <= 0.10):
                    continue
                vol = float(m.get("volume24hr", 0) or 0)
                if vol < 10:
                    continue
                candidates.append({
                    "question": m.get("question", "?"),
                    "token_id": clob_ids[0],
                    "yes_price": yes_price,
                    "volume24hr": vol,
                })
            except:
                continue

        # Sort by volume — most active markets first
        candidates.sort(key=lambda x: x["volume24hr"], reverse=True)

        # Take top N
        selected = candidates[:self.max_markets]
        for m in selected:
            self.markets[m["token_id"]] = m

        logger.info(f"Monitoring {len(self.markets)} markets (top {self.max_markets} by 24h volume)")
        for i, (tid, m) in enumerate(self.markets.items()):
            logger.info(f"  {i+1:2d}. {m['question'][:50]:50s} | YES={m['yes_price']*100:.1f}¢ | vol=${m['volume24hr']:,.0f}")

        return len(self.markets)

    def get_orderbook(self, token_id: str) -> dict:
        """Get orderbook snapshot."""
        try:
            r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
            if r.status_code != 200:
                return None
            book = r.json()
            bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
            asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))

            return {
                "best_bid": float(bids[0]["price"]) if bids else None,
                "best_ask": float(asks[0]["price"]) if asks else None,
                "bid_size": float(bids[0]["size"]) if bids else 0,
                "ask_size": float(asks[0]["size"]) if asks else 0,
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "bids": bids[:3],  # Top 3 levels
                "asks": asks[:3],
            }
        except:
            return None

    def detect_trades(self, token_id: str, book: dict):
        """Compare current book to previous and detect trades."""
        if not book or not book["best_bid"]:
            return

        prev = self.prev_book.get(token_id)
        self.prev_book[token_id] = book

        if not prev:
            return  # First snapshot, nothing to compare

        # Detect trades at the best bid (someone sold into the bid)
        if prev["best_bid"] and book["best_bid"]:
            if prev["best_bid"] == book["best_bid"]:
                # Same price level — check if size decreased
                size_change = prev["bid_size"] - book["bid_size"]
                if size_change > 0:
                    # Someone sold INTO the bid (market sell hit our buy order)
                    self.trades[token_id].append({
                        "timestamp": time.time(),
                        "side": "sell",  # Taker sold
                        "price": book["best_bid"],
                        "size": size_change,
                        "prev_size": prev["bid_size"],
                        "new_size": book["bid_size"],
                        "type": "bid_hit",
                    })
                    logger.info(
                        f"  TRADE DETECTED: {self.markets[token_id]['question'][:35]:35s} | "
                        f"BID HIT {book['best_bid']*100:.1f}¢ | "
                        f"{size_change:>8.0f} shares | "
                        f"size: {prev['bid_size']:.0f} → {book['bid_size']:.0f}"
                    )
            elif book["best_bid"] < prev["best_bid"]:
                # Best bid dropped — could be a large sell that ate through levels
                self.trades[token_id].append({
                    "timestamp": time.time(),
                    "side": "sell",
                    "price": prev["best_bid"],
                    "size": prev["bid_size"],  # Entire bid level was consumed
                    "prev_size": prev["bid_size"],
                    "new_size": 0,
                    "type": "bid_consumed",
                })
                logger.info(
                    f"  TRADE DETECTED: {self.markets[token_id]['question'][:35]:35s} | "
                    f"BID CONSUMED {prev['best_bid']*100:.1f}¢ | "
                    f"{prev['bid_size']:>8.0f} shares | level wiped"
                )

        # Detect trades at the best ask (someone bought from the ask)
        if prev["best_ask"] and book["best_ask"]:
            if prev["best_ask"] == book["best_ask"]:
                size_change = prev["ask_size"] - book["ask_size"]
                if size_change > 0:
                    # Someone bought FROM the ask (market buy hit our sell order)
                    self.trades[token_id].append({
                        "timestamp": time.time(),
                        "side": "buy",  # Taker bought
                        "price": book["best_ask"],
                        "size": size_change,
                        "prev_size": prev["ask_size"],
                        "new_size": book["ask_size"],
                        "type": "ask_hit",
                    })
                    logger.info(
                        f"  TRADE DETECTED: {self.markets[token_id]['question'][:35]:35s} | "
                        f"ASK HIT {book['best_ask']*100:.1f}¢ | "
                        f"{size_change:>8.0f} shares | "
                        f"size: {prev['ask_size']:.0f} → {book['ask_size']:.0f}"
                    )
            elif book["best_ask"] > prev["best_ask"]:
                # Best ask rose — could be a large buy that ate through levels
                self.trades[token_id].append({
                    "timestamp": time.time(),
                    "side": "buy",
                    "price": prev["best_ask"],
                    "size": prev["ask_size"],
                    "prev_size": prev["ask_size"],
                    "new_size": 0,
                    "type": "ask_consumed",
                })
                logger.info(
                    f"  TRADE DETECTED: {self.markets[token_id]['question'][:35]:35s} | "
                    f"ASK CONSUMED {prev['best_ask']*100:.1f}¢ | "
                    f"{prev['ask_size']:>8.0f} shares | level wiped"
                )

    def poll_cycle(self):
        """Poll all markets once and detect trades."""
        cycle_start = time.time()
        polled = 0
        for tid in self.markets:
            book = self.get_orderbook(tid)
            self.poll_count[tid] += 1
            if book:
                self.detect_trades(tid, book)
                polled += 1
            time.sleep(0.05)  # 50ms between requests = rate limit friendly

        elapsed = time.time() - cycle_start
        return polled, elapsed

    def compute_fill_rates(self) -> dict:
        """Compute real fill rates from observed trades."""
        now = time.time()
        elapsed_hours = (now - self.start_time) / 3600

        rates = {}
        for tid, m in self.markets.items():
            trades = self.trades[tid]
            polls = self.poll_count[tid]

            # Count trades by type
            bid_hits = [t for t in trades if t["type"] in ("bid_hit", "bid_consumed")]
            ask_hits = [t for t in trades if t["type"] in ("ask_hit", "ask_consumed")]

            # Trades per hour
            total_trades = len(trades)
            tph = total_trades / elapsed_hours if elapsed_hours > 0 else 0
            bid_tph = len(bid_hits) / elapsed_hours if elapsed_hours > 0 else 0
            ask_tph = len(ask_hits) / elapsed_hours if elapsed_hours > 0 else 0

            # Trades per scan (fill probability per 15s scan)
            # If we poll every 3s and scan every 15s, that's 5 polls per scan
            polls_per_scan = 15 / self.poll_interval
            trades_per_poll = total_trades / polls if polls > 0 else 0
            trades_per_scan = trades_per_poll * polls_per_scan

            # This is the REAL fill probability — chance per 15s scan that
            # someone hits our order
            bid_fill_prob = (len(bid_hits) / polls) * polls_per_scan if polls > 0 else 0
            ask_fill_prob = (len(ask_hits) / polls) * polls_per_scan if polls > 0 else 0

            # Average trade size
            avg_trade_size = sum(t["size"] for t in trades) / total_trades if total_trades > 0 else 0

            # Volume estimate (sum of trade sizes × price)
            est_volume = sum(t["size"] * t["price"] for t in trades)

            rates[tid] = {
                "question": m["question"],
                "yes_price": m["yes_price"],
                "volume24hr": m["volume24hr"],
                "polls": polls,
                "total_trades": total_trades,
                "bid_trades": len(bid_hits),
                "ask_trades": len(ask_hits),
                "trades_per_hour": round(tph, 2),
                "bid_trades_per_hour": round(bid_tph, 2),
                "ask_trades_per_hour": round(ask_tph, 2),
                "trades_per_scan_15s": round(trades_per_scan, 4),
                "bid_fill_prob_per_scan": round(bid_fill_prob, 4),
                "ask_fill_prob_per_scan": round(ask_fill_prob, 4),
                "avg_trade_size": round(avg_trade_size, 0),
                "est_volume_observed": round(est_volume, 2),
                "elapsed_hours": round(elapsed_hours, 2),
            }

        return rates

    def save_results(self):
        """Save fill rates to JSON for the bot to read."""
        rates = self.compute_fill_rates()

        # Sort by trade count
        sorted_rates = dict(sorted(rates.items(), key=lambda x: x[1]["total_trades"], reverse=True))

        output = {
            "start_time": self.start_time,
            "elapsed_hours": (time.time() - self.start_time) / 3600,
            "poll_interval_sec": self.poll_interval,
            "markets_monitored": len(self.markets),
            "total_polls": sum(self.poll_count.values()),
            "total_trades_detected": sum(len(t) for t in self.trades.values()),
            "fill_rates": sorted_rates,
        }

        with open(self.output_file, "w") as f:
            json.dump(output, f, indent=2, default=str)

        return output

    def print_summary(self):
        """Print a summary of detected trades and fill rates."""
        rates = self.compute_fill_rates()
        elapsed = (time.time() - self.start_time) / 60
        total_trades = sum(r["total_trades"] for r in rates.values())

        logger.info("=" * 80)
        logger.info(f"FILL RATE MONITOR — {elapsed:.1f} min elapsed | {total_trades} trades detected")
        logger.info("=" * 80)
        logger.info(f"{'Market':<45} {'Polls':>5} {'Trades':>6} {'BidHit':>6} {'AskHit':>6} {'T/hr':>5} {'FillPct':>7} {'AvgSz':>7}")
        logger.info("-" * 80)

        for tid, r in sorted(rates.items(), key=lambda x: x[1]["total_trades"], reverse=True):
            if r["total_trades"] > 0 or r["polls"] > 50:
                logger.info(
                    f"{r['question'][:43]:43s} | "
                    f"{r['polls']:>5} | "
                    f"{r['total_trades']:>6} | "
                    f"{r['bid_trades']:>6} | "
                    f"{r['ask_trades']:>6} | "
                    f"{r['trades_per_hour']:>5.1f} | "
                    f"{r['bid_fill_prob_per_scan']*100:>5.1f}% | "
                    f"{r['avg_trade_size']:>7.0f}"
                )

        logger.info("=" * 80)

    def run(self):
        logger.info("=" * 70)
        logger.info(f"FILL RATE MONITOR")
        logger.info(f"Markets: {self.max_markets} | Poll: every {self.poll_interval}s | Duration: {self.duration}s")
        logger.info(f"Output: {self.output_file}")
        logger.info("=" * 70)

        # Discover markets
        count = self.discover_markets()
        if count == 0:
            logger.error("No markets found to monitor")
            return

        end_time = time.time() + self.duration
        last_summary = time.time()
        save_interval = 60  # Save results every 60 seconds

        while time.time() < end_time:
            try:
                polled, elapsed = self.poll_cycle()
            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)
                time.sleep(5)
                continue

            # Save results periodically
            if time.time() - last_summary > save_interval:
                self.save_results()
                self.print_summary()
                last_summary = time.time()

            # Sleep until next poll cycle
            sleep_time = max(0.1, self.poll_interval - elapsed)
            time.sleep(sleep_time)

        # Final save and summary
        output = self.save_results()
        self.print_summary()
        logger.info(f"Monitor stopped. {output['total_trades_detected']} trades detected over {output['elapsed_hours']:.1f} hours")
        logger.info(f"Results saved to {self.output_file}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Fill Rate Monitor")
    parser.add_argument("--max-markets", type=int, default=30, help="Number of markets to monitor")
    parser.add_argument("--poll-interval", type=int, default=3, help="Seconds between polls")
    parser.add_argument("--duration", type=int, default=3600, help="Duration in seconds (default 1 hour)")
    parser.add_argument("--output", type=str, default="/home/star/polymarket-arb-bot/data/fill_rates.json")
    args = parser.parse_args()

    monitor = FillMonitor(
        max_markets=args.max_markets,
        poll_interval=args.poll_interval,
        duration=args.duration,
        output_file=args.output,
    )
    monitor.run()


if __name__ == "__main__":
    main()
