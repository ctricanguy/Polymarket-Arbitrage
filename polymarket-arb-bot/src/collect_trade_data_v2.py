"""
Real Trade Data Collector V2
=============================

Uses the PUBLIC Data API (data-api.polymarket.com/trades) to pull ACTUAL trade data.
No authentication required!

Also uses the new polymarket-client Python SDK for order book data.

Output:
  - data/real_trade_rates.json (full data)
  - data/trade_calibration.json (simplified for bot calibration)
"""
import json, time, logging, sys, asyncio
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter, defaultdict
import requests

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logging.getLogger("websocket").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

logger = logging.getLogger("collector")

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
OUTPUT_FILE = Path(__file__).parent.parent / "data" / "real_trade_rates.json"
SUMMARY_FILE = Path(__file__).parent.parent / "data" / "trade_calibration.json"


def fetch_trades(limit=500, offset=0, taker_only=False):
    """Fetch trades from the public Data API."""
    params = {
        "limit": limit,
        "offset": offset,
        "sortBy": "timestamp",
        "sortOrder": "desc",
    }
    if taker_only:
        params["type"] = "TAKER"
    
    r = requests.get(f"{DATA_API}/trades", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_trades_for_market(token_id, limit=500):
    """Fetch trades for a specific market by asset token ID."""
    params = {
        "asset": token_id,
        "limit": limit,
        "sortBy": "timestamp",
        "sortOrder": "desc",
    }
    r = requests.get(f"{DATA_API}/trades", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_book(token_id):
    """Fetch order book from CLOB API (public)."""
    r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
    if r.status_code == 200:
        return r.json()
    return None


def fetch_midpoint(token_id):
    """Fetch midpoint from CLOB API (public)."""
    r = requests.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=10)
    if r.status_code == 200:
        return float(r.json().get("mid", 0))
    return None


def fetch_spread(token_id):
    """Fetch spread from CLOB API (public)."""
    r = requests.get(f"{CLOB_API}/spread", params={"token_id": token_id}, timeout=10)
    if r.status_code == 200:
        return r.json()
    return None


def collect_via_data_api(duration_seconds=3600, poll_interval=30):
    """
    Collect real trade data by polling the Data API every poll_interval seconds.
    Much more reliable than WebSocket for data collection.
    """
    print(f"\n{'='*70}")
    print(f"REAL TRADE DATA COLLECTOR V2 — Data API (PUBLIC, no auth)")
    print(f"Duration: {duration_seconds/60:.0f} minutes | Poll every {poll_interval}s")
    print(f"{'='*70}\n")
    
    start_time = time.time()
    all_trades = []
    seen_tx_hashes = set()
    market_stats = defaultdict(lambda: {
        "trade_count": 0,
        "total_volume": 0,
        "buy_count": 0,
        "sell_count": 0,
        "prices": [],
        "sizes": [],
        "first_seen": None,
        "last_seen": None,
    })
    
    poll_count = 0
    last_report = 0
    
    while time.time() - start_time < duration_seconds:
        poll_count += 1
        now = time.time()
        elapsed = now - start_time
        
        try:
            # Fetch recent trades
            trades = fetch_trades(limit=500)
            new_trades = 0
            
            for t in trades:
                tx = t.get("transactionHash", "")
                if tx and tx not in seen_tx_hashes:
                    seen_tx_hashes.add(tx)
                    all_trades.append(t)
                    new_trades += 1
                    
                    title = t.get("title", "Unknown")
                    stats = market_stats[title]
                    stats["trade_count"] += 1
                    stats["total_volume"] += t.get("size", 0) * t.get("price", 0)
                    stats["prices"].append(t.get("price", 0))
                    stats["sizes"].append(t.get("size", 0))
                    
                    if t.get("side") == "BUY":
                        stats["buy_count"] += 1
                    else:
                        stats["sell_count"] += 1
                    
                    ts = t.get("timestamp", 0)
                    if stats["first_seen"] is None or ts < stats["first_seen"]:
                        stats["first_seen"] = ts
                    if stats["last_seen"] is None or ts > stats["last_seen"]:
                        stats["last_seen"] = ts
            
            if now - last_report > 60 or poll_count == 1:
                elapsed_min = elapsed / 60
                total_unique = len(all_trades)
                active_markets = len(market_stats)
                
                print(f"\n--- Poll #{poll_count} | {datetime.now().strftime('%H:%M:%S')} | {elapsed_min:.1f}min ---")
                print(f"New trades this batch: {new_trades}")
                print(f"Total unique trades: {total_unique}")
                print(f"Markets seen: {active_markets}")
                if elapsed_min > 0:
                    print(f"Rate: {total_unique/elapsed_min:.1f} trades/min = {total_unique/elapsed_min*60:.0f}/hour")
                
                # Top markets
                top = sorted(market_stats.items(), key=lambda x: x[1]["trade_count"], reverse=True)[:15]
                print(f"\nTop 15 markets by trade count:")
                print(f"{'Market':<50s} {'Trades':>6s} {'Volume':>10s} {'B/S':>8s} {'AvgSz':>7s}")
                print("-" * 85)
                for title, s in top:
                    bs = f"{s['buy_count']}/{s['sell_count']}"
                    avg_sz = sum(s["sizes"])/len(s["sizes"]) if s["sizes"] else 0
                    print(f"{title[:50]:<50s} {s['trade_count']:>6d} ${s['total_volume']:>9.2f} {bs:>8s} {avg_sz:>7.1f}")
                
                last_report = now
            
        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
        
        time.sleep(poll_interval)
    
    # Final analysis
    elapsed = time.time() - start_time
    elapsed_min = elapsed / 60
    
    print(f"\n{'='*70}")
    print(f"FINAL REPORT — {elapsed_min:.1f} minutes")
    print(f"{'='*70}")
    print(f"Total unique trades: {len(all_trades)}")
    print(f"Unique markets: {len(market_stats)}")
    print(f"Rate: {len(all_trades)/elapsed_min:.1f}/min = {len(all_trades)/elapsed_min*60:.0f}/hour")
    
    # Build calibration data
    calibration = {
        "collection_time": datetime.now().isoformat(),
        "duration_minutes": elapsed_min,
        "total_trades": len(all_trades),
        "trades_per_minute": len(all_trades) / elapsed_min,
        "trades_per_hour": len(all_trades) / elapsed_min * 60,
        "unique_markets": len(market_stats),
        "per_market": {},
    }
    
    # Sort markets by trade count
    sorted_markets = sorted(market_stats.items(), key=lambda x: x[1]["trade_count"], reverse=True)
    
    print(f"\nTop 30 markets for market making:")
    print(f"{'Market':<50s} {'Trades':>6s} {'/Hour':>7s} {'Volume':>10s} {'AvgPrc':>7s} {'B/S':>8s}")
    print("-" * 92)
    
    for title, s in sorted_markets[:30]:
        tph = s["trade_count"] / elapsed_min * 60
        avg_price = sum(s["prices"])/len(s["prices"]) if s["prices"] else 0
        bs = f"{s['buy_count']}/{s['sell_count']}"
        print(f"{title[:50]:<50s} {s['trade_count']:>6d} {tph:>7.1f} ${s['total_volume']:>9.2f} {avg_price:>7.4f} {bs:>8s}")
        
        calibration["per_market"][title] = {
            "trade_count": s["trade_count"],
            "trades_per_hour": tph,
            "total_volume": s["total_volume"],
            "avg_price": avg_price,
            "avg_size": sum(s["sizes"])/len(s["sizes"]) if s["sizes"] else 0,
            "buy_count": s["buy_count"],
            "sell_count": s["sell_count"],
            "buy_sell_ratio": s["buy_count"] / s["trade_count"] if s["trade_count"] > 0 else 0,
        }
    
    # Revenue projection with REAL data
    print(f"\n{'='*70}")
    print(f"REALISTIC REVENUE PROJECTION (from real trade data)")
    print(f"{'='*70}")
    
    # For top markets, fetch current spread and project
    total_projected_conservative = 0
    total_projected_moderate = 0
    
    for title, s in sorted_markets[:20]:
        trades_per_hour = s["trade_count"] / elapsed_min * 60
        avg_price = sum(s["prices"])/len(s["prices"]) if s["prices"] else 0
        avg_size = sum(s["sizes"])/len(s["sizes"]) if s["sizes"] else 0
        
        if trades_per_hour > 0 and avg_price > 0:
            # Conservative: 1/10th of trades are ours (10 MMs ahead)
            # Moderate: 1/5th of trades (5 MMs ahead)
            our_trades_conservative = trades_per_hour / 10
            our_trades_moderate = trades_per_hour / 5
            
            # Revenue per trade: ~1% spread × position size
            spread_estimate = 0.01  # 1¢ spread on average
            revenue_per_trade = spread_estimate * min(avg_size, 500)  # Cap at 500 shares
            
            rev_conservative = our_trades_conservative * revenue_per_trade * 24
            rev_moderate = our_trades_moderate * revenue_per_trade * 24
            
            total_projected_conservative += rev_conservative
            total_projected_moderate += rev_moderate
    
    print(f"  Conservative (1/10 share, 1¢ spread): ${total_projected_conservative:.2f}/day")
    print(f"  Moderate (1/5 share, 1¢ spread):     ${total_projected_moderate:.2f}/day")
    print(f"  Friend's actual:                      $734/day")
    
    calibration["projected_daily_conservative"] = total_projected_conservative
    calibration["projected_daily_moderate"] = total_projected_moderate
    
    # Save
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(calibration, f, indent=2, default=str)
    
    # Simplified summary
    summary = {
        "collection_time": calibration["collection_time"],
        "duration_minutes": calibration["duration_minutes"],
        "total_trades_per_hour": calibration["trades_per_hour"],
        "top_markets": [
            {
                "title": title,
                "trades_per_hour": calibration["per_market"][title]["trades_per_hour"],
                "avg_price": calibration["per_market"][title]["avg_price"],
                "avg_size": calibration["per_market"][title]["avg_size"],
                "volume": calibration["per_market"][title]["total_volume"],
            }
            for title in [t for t, _ in sorted_markets[:30]]
        ],
        "projected_daily_conservative": total_projected_conservative,
        "projected_daily_moderate": total_projected_moderate,
    }
    with open(SUMMARY_FILE, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    
    print(f"\nSaved: {OUTPUT_FILE}")
    print(f"Summary: {SUMMARY_FILE}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=int, default=3600, help='Collection duration in seconds')
    parser.add_argument('--poll', type=int, default=30, help='Poll interval in seconds')
    args = parser.parse_args()
    collect_via_data_api(duration_seconds=args.duration, poll_interval=args.poll)
