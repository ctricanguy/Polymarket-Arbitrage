"""
Real Trade Data Collector
==========================

Connects to Polymarket WebSocket and collects ACTUAL trade data for 1 hour.
This gives us real trade rates per market to calibrate the fill model.

Output: /home/star/polymarket-arb-bot/data/real_trade_rates.json
"""
import json, time, logging, sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))
from hybrid_scanner import HybridScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logging.getLogger("websocket").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "real_trade_rates.json"
SUMMARY_FILE = Path(__file__).parent.parent / "data" / "trade_calibration.json"


def collect(duration_seconds: int = 3600):
    """Collect real trade data for the given duration."""
    print(f"\n{'='*60}")
    print(f"REAL TRADE DATA COLLECTOR")
    print(f"Duration: {duration_seconds/60:.0f} minutes")
    print(f"{'='*60}\n")

    scanner = HybridScanner()
    scanner.discover_markets()
    scanner.subscribe_top_markets(min_volume=10000)
    scanner.wait_for_books(timeout=20)

    books = scanner.ws_client.get_all_books()
    print(f"Connected to {len(books)} markets\n")

    start_time = time.time()
    last_report = time.time()
    
    # Track trades per market
    trade_counts = defaultdict(int)
    trade_details = defaultdict(list)
    
    # Also track price movements
    price_snapshots = {}
    for token_id, book in books.items():
        if book.bids and book.asks:
            bid, _ = book.get_best_bid()
            ask, _ = book.get_best_ask()
            price_snapshots[token_id] = {
                "start_bid": bid, "start_ask": ask,
                "start_mid": (bid + ask) / 2,
                "bid_history": [(time.time(), bid)],
                "ask_history": [(time.time(), ask)],
            }

    # Set up trade callback
    def on_trade(asset_id, price):
        trade_counts[asset_id] += 1
        trade_details[asset_id].append({
            "time": time.time() - start_time,
            "price": price,
        })
    
    scanner.ws_client.on_trade = on_trade

    print(f"Collecting trades for {duration_seconds/60:.0f} minutes...")
    print(f"Reports every 60 seconds\n")

    while True:
        now = time.time()
        elapsed = now - start_time
        
        if elapsed > duration_seconds:
            break

        # Record price snapshots
        for token_id, book in books.items():
            if book.bids and book.asks:
                bid, _ = book.get_best_bid()
                ask, _ = book.get_best_ask()
                if token_id in price_snapshots:
                    ps = price_snapshots[token_id]
                    ps["bid_history"].append((now, bid))
                    ps["ask_history"].append((now, ask))

        # Report every 60s
        if now - last_report > 60:
            total_trades = sum(trade_counts.values())
            active_markets = sum(1 for c in trade_counts.values() if c > 0)
            elapsed_min = elapsed / 60
            
            print(f"\n--- {datetime.now().strftime('%H:%M:%S')} | {elapsed_min:.0f}min elapsed ---")
            print(f"Total trades observed: {total_trades}")
            print(f"Markets with trades: {active_markets} / {len(books)}")
            print(f"Trade rate: {total_trades/elapsed_min:.1f} trades/min = {total_trades/elapsed_min*60:.0f}/hour")
            
            # Top markets by trade count
            top = sorted(trade_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            print(f"\nTop 10 markets by trade count:")
            for token_id, count in top:
                market = scanner.token_to_market.get(token_id)
                q = market.question[:45] if market else "Unknown"
                book = books.get(token_id)
                if book and book.bids and book.asks:
                    bid, _ = book.get_best_bid()
                    ask, _ = book.get_best_ask()
                    spread = (ask - bid) / ask * 100
                    print(f"  {count:4d} trades | {q:45s} | spread={spread:.1f}% | {count/elapsed_min:.1f}/min")
                else:
                    print(f"  {count:4d} trades | {q:45s}")
            
            last_report = now

        time.sleep(1)

    # Final report
    total_trades = sum(trade_counts.values())
    elapsed_min = (time.time() - start_time) / 60

    print(f"\n{'='*60}")
    print(f"FINAL REPORT — {elapsed_min:.1f} minutes")
    print(f"{'='*60}")
    print(f"Total trades: {total_trades}")
    print(f"Trade rate: {total_trades/elapsed_min:.1f}/min = {total_trades/elapsed_min*60:.0f}/hour")

    # Build calibration data
    calibration = {
        "collection_start": start_time,
        "collection_duration_seconds": time.time() - start_time,
        "total_trades": total_trades,
        "trades_per_minute": total_trades / elapsed_min,
        "trades_per_hour": total_trades / elapsed_min * 60,
        "markets_monitored": len(books),
        "markets_with_trades": sum(1 for c in trade_counts.values() if c > 0),
        "per_market": {},
    }

    for token_id, count in trade_counts.items():
        market = scanner.token_to_market.get(token_id)
        book = books.get(token_id)
        
        entry = {
            "question": market.question if market else "Unknown",
            "condition_id": market.condition_id if market else "",
            "volume": market.volume if market else 0,
            "trade_count": count,
            "trades_per_hour": count / elapsed_min * 60,
            "trades": trade_details[token_id],
        }
        
        if book and book.bids and book.asks:
            bid, bid_size = book.get_best_bid()
            ask, ask_size = book.get_best_ask()
            entry["final_bid"] = bid
            entry["final_ask"] = ask
            entry["spread"] = ask - bid
            entry["spread_pct"] = (ask - bid) / ask * 100
            entry["bid_size"] = bid_size
            entry["ask_size"] = ask_size
        
        # Price movement
        if token_id in price_snapshots:
            ps = price_snapshots[token_id]
            bid_changes = [b for _, b in ps["bid_history"]]
            ask_changes = [a for _, a in ps["ask_history"]]
            if bid_changes and ask_changes:
                entry["bid_movement"] = max(bid_changes) - min(bid_changes)
                entry["ask_movement"] = max(ask_changes) - min(ask_changes)
                entry["price_volatility"] = (max(bid_changes) - min(bid_changes)) / min(bid_changes) * 100 if min(bid_changes) > 0 else 0
        
        calibration["per_market"][token_id] = entry

    # Sort by trade count
    sorted_markets = sorted(calibration["per_market"].items(), key=lambda x: x[1]["trade_count"], reverse=True)
    
    print(f"\nTop 20 markets by trade activity:")
    print(f"{'Market':<45s} {'Trades':>6s} {'/Hour':>6s} {'Spread':>7s} {'Volat%':>7s}")
    print("-" * 75)
    for token_id, e in sorted_markets[:20]:
        spread = e.get("spread_pct", 0)
        volat = e.get("price_volatility", 0)
        print(f"{e['question'][:45]:<45s} {e['trade_count']:>6d} {e['trades_per_hour']:>6.1f} {spread:>6.1f}% {volat:>6.1f}%")

    # Calculate realistic revenue projection
    print(f"\n{'='*60}")
    print(f"REALISTIC REVENUE PROJECTION")
    print(f"{'='*60}")
    
    total_projected = 0
    for token_id, e in sorted_markets[:30]:
        trades_per_hour = e.get("trades_per_hour", 0)
        spread = e.get("spread", 0)
        if trades_per_hour > 0 and spread > 0:
            # Our share: 1 of 6 MMs
            our_trades = trades_per_hour / 6
            # Revenue per trade: spread × 500 shares
            revenue_per_trade = spread * 500
            revenue_per_hour = our_trades * revenue_per_trade
            revenue_per_day = revenue_per_hour * 24
            total_projected += revenue_per_day
            
            print(f"  {e['question'][:40]:<40s} | {trades_per_hour:>5.1f} t/h | spread={spread:.3f} | ${revenue_per_day:.2f}/day")
    
    print(f"\n  TOTAL PROJECTED (30 markets, 1/6 share): ${total_projected:.2f}/day")
    print(f"  With 3 MMs ahead:  ${total_projected*3/6:.2f}/day (conservative)")
    print(f"  With 10 MMs ahead: ${total_projected/11:.2f}/day (pessimistic)")

    # Save
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(calibration, f, indent=2)
    
    # Save simplified calibration for the bot
    simplified = {
        "collection_time": datetime.now().isoformat(),
        "total_trades_per_hour": calibration["trades_per_hour"],
        "per_market_rates": {
            token_id: {
                "question": e["question"],
                "trades_per_hour": e["trades_per_hour"],
                "spread": e.get("spread", 0),
                "spread_pct": e.get("spread_pct", 0),
            }
            for token_id, e in sorted_markets[:50]
        },
        "projected_daily_revenue_1_of_6": total_projected,
        "projected_daily_revenue_1_of_3": total_projected * 3 / 6,
        "projected_daily_revenue_1_of_11": total_projected / 11,
    }
    with open(SUMMARY_FILE, 'w') as f:
        json.dump(simplified, f, indent=2)
    
    print(f"\nSaved to: {OUTPUT_FILE}")
    print(f"Calibration: {SUMMARY_FILE}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=int, default=3600, help='Collection duration in seconds')
    args = parser.parse_args()
    collect(duration_seconds=args.duration)
