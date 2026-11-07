"""
Real-time Paper Trading Dashboard
===================================

A web dashboard that reads the paper trader's saved state and displays
it in a live-updating UI. Shows positions, P&L, trade history, and
real-time price movements.

Usage:
    python3 dashboard.py
    # Then open http://localhost:8484 in your browser

The dashboard auto-refreshes every 3 seconds by reading the state.json
file that the paper trader writes.
"""

import json
import os
import time
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("polymarket-arb.dashboard")

STATE_DIR = Path(__file__).parent.parent / "data" / "yes_mm"
STATE_FILE = STATE_DIR / "state.json"
TRADE_EVENTS_FILE = STATE_DIR / "trade_events.json"
LOG_DIR = Path(__file__).parent.parent / "logs"
TRADE_LOG = LOG_DIR / "yes_mm.log"

PORT = 8484

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Paper Trading Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #0d1117;
    color: #c9d1d9;
    font-family: 'SF Mono', 'Monaco', 'Cascadia Code', 'Consolas', monospace;
    font-size: 14px;
    padding: 20px;
    max-width: 1400px;
    margin: 0 auto;
}
.header {
    text-align: center;
    padding: 20px 0;
    border-bottom: 1px solid #30363d;
    margin-bottom: 20px;
}
.header h1 {
    color: #58a6ff;
    font-size: 24px;
    margin-bottom: 5px;
}
.header .subtitle {
    color: #8b949e;
    font-size: 12px;
}
.summary-grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 15px;
    margin-bottom: 20px;
}
.stat-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 15px;
    text-align: center;
}
.stat-label {
    color: #8b949e;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 5px;
}
.stat-value {
    font-size: 22px;
    font-weight: bold;
}
.stat-value.green { color: #3fb950; }
.stat-value.red { color: #f85149; }
.stat-value.blue { color: #58a6ff; }
.stat-value.yellow { color: #d29922; }
.section {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    margin-bottom: 20px;
    overflow: hidden;
}
.section-header {
    background: #21262d;
    padding: 12px 15px;
    border-bottom: 1px solid #30363d;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.section-header h2 {
    font-size: 14px;
    color: #58a6ff;
}
.section-header .badge {
    background: #238636;
    color: white;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 11px;
}
table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}
th {
    background: #21262d;
    color: #8b949e;
    padding: 10px 12px;
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky;
    top: 0;
}
td {
    padding: 10px 12px;
    border-top: 1px solid #21262d;
}
tr:hover { background: #1c2128; }
.profit { color: #3fb950; }
.loss { color: #f85149; }
.neutral { color: #8b949e; }
.status-badge {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: bold;
}
.status-holding { background: #1f6feb33; color: #58a6ff; }
.status-pending_buy { background: #d2992233; color: #d29922; }
.status-pending_sell { background: #f8514933; color: #f85149; }
.status-sold { background: #23863633; color: #3fb950; }
.status-stopped { background: #da363333; color: #f85149; }
.trade-log {
    max-height: 300px;
    overflow-y: auto;
    padding: 10px 15px;
    font-size: 12px;
    line-height: 1.6;
}
.log-entry {
    padding: 3px 0;
    border-bottom: 1px solid #21262d;
}
.log-time { color: #8b949e; }
.log-info { color: #58a6ff; }
.log-buy { color: #3fb950; }
.log-sell { color: #f85149; }
.log-profit { color: #3fb950; font-weight: bold; }
.log-loss { color: #f85149; }
.refresh-indicator {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #3fb950;
    margin-right: 5px;
    animation: pulse 2s infinite;
}
@keyframes pulse {
    0% { opacity: 1; }
    50% { opacity: 0.3; }
    100% { opacity: 1; }
}
.empty {
    text-align: center;
    color: #8b949e;
    padding: 40px;
}
.progress-bar {
    background: #21262d;
    border-radius: 4px;
    height: 6px;
    margin-top: 5px;
    overflow: hidden;
}
.progress-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.5s;
}
.tp-bar { background: #3fb950; }
.sl-bar { background: #f85149; }
</style>
</head>
<body>

<div class="header">
    <h1>📊 Polymarket Paper Trading Dashboard</h1>
    <div class="subtitle">
        <span class="refresh-indicator"></span>
        Live simulation against real market data — Auto-refresh every 3s —
        <span id="last-update">--</span>
    </div>
</div>

<div class="summary-grid" id="summary">
    <div class="stat-card">
        <div class="stat-label">Portfolio Value</div>
        <div class="stat-value blue" id="total-value">$0</div>
    </div>
    <div class="stat-card">
        <div class="stat-label">Spread Captured</div>
        <div class="stat-value green" id="realized-pnl">$0</div>
    </div>
    <div class="stat-card">
        <div class="stat-label">Rate / Day</div>
        <div class="stat-value" id="rate-day">$0</div>
    </div>
    <div class="stat-card">
        <div class="stat-label">Fill Cycles</div>
        <div class="stat-value yellow" id="fill-cycles">0</div>
    </div>
    <div class="stat-card">
        <div class="stat-label">Total Fills</div>
        <div class="stat-value blue" id="total-fills">0</div>
    </div>
</div>

<div class="section">
    <div class="section-header">
        <h2>Open Positions</h2>
        <span class="badge" id="holding-count">0 holding</span>
    </div>
    <div id="positions-table">
        <div class="empty">Loading positions...</div>
    </div>
</div>

<div class="section">
    <div class="section-header">
        <h2>Trade History</h2>
        <span class="badge" id="trades-count">0 trades</span>
    </div>
    <div id="trades-table">
        <div class="empty">No trades yet</div>
    </div>
</div>

<div class="section">
    <div class="section-header">
        <h2>Live Trade Log</h2>
        <span class="badge" id="log-count">0 entries</span>
    </div>
    <div class="trade-log" id="trade-log">
        <div class="empty">No log entries</div>
    </div>
</div>

<script>
async function fetchState() {
    try {
        const resp = await fetch('/api/state');
        const data = await resp.json();
        updateDashboard(data);
    } catch (e) {
        console.error('Fetch error:', e);
        document.getElementById('last-update').textContent = 'ERROR';
    }
}

function fmtMoney(v) {
    const sign = v >= 0 ? '+' : '';
    return sign + '$' + Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function fmtPct(v) {
    const sign = v >= 0 ? '+' : '';
    return sign + v.toFixed(2) + '%';
}

function fmtPrice(v) {
    return (v * 100).toFixed(2) + '¢';
}

function updateDashboard(data) {
    // Summary
    const totalVal = data.total_value || 0;
    const realizedPnl = data.total_pnl || data.total_profit || data.realized_pnl || 0;
    const rateDay = data.daily_pnl || data.rate_per_day || 0;
    const fillCycles = data.round_trips || data.fill_cycles || 0;
    const buyFills = data.buy_fills || 0;
    const sellFills = data.sell_fills || 0;
    const totalFills = buyFills + sellFills;
    const positions = data.positions || [];

    document.getElementById('total-value').textContent = '$' + totalVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    
    const realEl = document.getElementById('realized-pnl');
    realEl.textContent = fmtMoney(realizedPnl);
    realEl.className = 'stat-value ' + (realizedPnl >= 0 ? 'green' : 'red');
    
    const rateEl = document.getElementById('rate-day');
    rateEl.textContent = fmtMoney(rateDay);
    rateEl.className = 'stat-value ' + (rateDay >= 0 ? 'green' : 'red');
    
    document.getElementById('fill-cycles').textContent = fillCycles;
    document.getElementById('total-fills').textContent = totalFills;

    // Holding count
    document.getElementById('holding-count').textContent = positions.length + ' markets';

    // Positions table
    if (positions.length === 0) {
        document.getElementById('positions-table').innerHTML = '<div class="empty">No active markets — bot is initializing...</div>';
    } else {
        let html = '<table><thead><tr>' +
            '<th>Market</th><th>Category</th><th>Shares</th>' +
            '<th>Buy/Sell Fills</th><th>Spread</th><th>Position Value</th>' +
            '</tr></thead><tbody>';
        
        for (const p of positions) {
            const posValue = (p.buy_fills || 0) * (p.entry_price || 0);
            const spreadCaptured = p.status === 'selling' ? posValue : 0;
            const spreadClass = spreadCaptured >= 0 ? 'profit' : 'loss';
            
            html += '<tr>' +
                '<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + p.question + '</td>' +
                '<td>' + (p.category || 'other') + '</td>' +
                '<td>' + (p.entry_shares || 0).toLocaleString('en-US', {maximumFractionDigits: 0}) + '</td>' +
                '<td>B:' + (p.buy_fills || 0).toLocaleString('en-US', {maximumFractionDigits: 0}) + ' / S:' + (p.sell_fills || 0).toLocaleString('en-US', {maximumFractionDigits: 0}) + '</td>' +
                '<td>' + fmtPrice(p.spread || 0) + '</td>' +
                '<td class="' + spreadClass + '">$' + posValue.toFixed(2) + '</td>' +
                '</tr>';
        }
        html += '</tbody></table>';
        document.getElementById('positions-table').innerHTML = html;
    }

    // Trade history — completed round trips
    const filledTrades = (data.trades || data.completed_rounds || []).slice().reverse().slice(0, 20);
    document.getElementById('trades-count').textContent = filledTrades.length + ' trades';
    
    if (filledTrades.length === 0) {
        document.getElementById('trades-table').innerHTML = '<div class="empty">No trades filled yet</div>';
    } else {
        let html = '<table><thead><tr>' +
            '<th>Market</th><th>Spread</th><th>Shares</th><th>Buy@</th><th>Sell@</th><th>Profit</th><th>Duration</th>' +
            '</tr></thead><tbody>';
        for (const t of filledTrades) {
            const profitClass = (t.profit || 0) >= 0 ? 'profit' : 'loss';
            const durMin = ((t.duration_sec || 0) / 60).toFixed(0);
            html += '<tr>' +
                '<td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + t.question + '</td>' +
                '<td>' + fmtPrice(t.spread || 0) + '</td>' +
                '<td>' + (t.shares || 0).toLocaleString('en-US', {maximumFractionDigits: 0}) + '</td>' +
                '<td>' + fmtPrice(t.buy_price || 0) + '</td>' +
                '<td>' + fmtPrice(t.sell_price || 0) + '</td>' +
                '<td class="' + profitClass + '">' + fmtMoney(t.profit || 0) + '</td>' +
                '<td>' + durMin + 'min</td>' +
                '</tr>';
        }
        html += '</tbody></table>';
        document.getElementById('trades-table').innerHTML = html;
    }

    // Trade log
    const logEntries = (data.log_entries || []).slice().reverse().slice(0, 50);
    document.getElementById('log-count').textContent = logEntries.length + ' entries';
    
    if (logEntries.length === 0) {
        document.getElementById('trade-log').innerHTML = '<div class="empty">No log entries</div>';
    } else {
        let html = '';
        for (const entry of logEntries) {
            const tTime = new Date(entry.timestamp * 1000).toLocaleTimeString();
            let cls = 'log-info';
            if (entry.message.includes('BUY FILLED')) cls = 'log-buy';
            else if (entry.message.includes('SELL FILLED') || entry.message.includes('STOP_LOSS')) cls = 'log-sell';
            else if (entry.message.includes('ROUND TRIP')) cls = 'log-profit';
            else if (entry.message.includes('OPEN') || entry.message.includes('Scan')) cls = 'log-info';
            
            html += '<div class="log-entry"><span class="log-time">[' + tTime + ']</span> <span class="' + cls + '">' + entry.message + '</span></div>';
        }
        document.getElementById('trade-log').innerHTML = html;
    }

    // Last update time
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
}

// Auto-refresh
fetchState();
setInterval(fetchState, 3000);
</script>
</body>
</html>"""


def load_state() -> dict:
    """Load bot state — supports YES MM, NO accumulator, and MM trader formats."""
    if not STATE_FILE.exists():
        return {"error": "No state file found. Start the bot first."}
    
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception as e:
        return {"error": f"Failed to load state: {e}"}
    
    # Detect format by checking for YES MM fields
    raw_positions = state.get("positions", {})
    
    # YES MM format: has total_profit, total_round_trips, positions with bid_price/ask_price
    if "total_round_trips" in state:
        # YES Market Maker format
        cash = state.get("cash", 0)
        total_profit = state.get("total_profit", 0)
        round_trips = state.get("total_round_trips", 0)
        completed = state.get("completed_rounds", [])
        
        active_positions = []
        if isinstance(raw_positions, dict):
            pos_list = [v for v in raw_positions.values() if isinstance(v, dict)]
        else:
            pos_list = [p for p in raw_positions if isinstance(p, dict)]
        
        unrealized = 0
        for p in pos_list:
            pnl = p.get("unrealized_pnl", 0)
            unrealized += pnl
            buy_fills = p.get("shares_filled_buy", 0)
            sell_fills = p.get("shares_filled_sell", 0)
            active_positions.append({
                "question": p.get("question", "?"),
                "side": p.get("side", "YES"),
                "entry_price": p.get("bid_price", 0),
                "current_price": p.get("current_bid", 0),
                "entry_shares": p.get("shares_target", 0),
                "unrealized_pnl": pnl,
                "volume24hr": p.get("volume24hr", 0),
                "one_hour_change": p.get("spread", 0),
                "status": p.get("status", "?"),
                "spread": p.get("spread", 0),
                "expected_profit": p.get("expected_profit", 0),
                "roi_pct": p.get("roi_pct", 0),
                "buy_fills": buy_fills,
                "sell_fills": sell_fills,
                "cost": p.get("cost", 0),
                "token_id": p.get("token_id", ""),
            })
        
        total_value = cash + sum(p["entry_price"] * p["entry_shares"] for p in active_positions if p["status"] == "selling") + unrealized
        starting = state.get("starting_capital", 100000)
        
        state["total_value"] = total_value
        state["total_pnl"] = total_profit + unrealized
        state["total_return_pct"] = ((total_value - starting) / starting * 100) if starting > 0 else 0
        state["daily_pnl"] = total_profit
        state["positions"] = active_positions
        state["cash"] = cash
        state["round_trips"] = round_trips
        state["completed_rounds"] = completed[-50:]
        state["trades"] = [{
            "question": r.get("question", "?"),
            "spread": r.get("spread", 0),
            "profit": r.get("profit", 0),
            "shares": r.get("shares", 0),
            "buy_price": r.get("buy_price", 0),
            "sell_price": r.get("sell_price", 0),
            "duration_sec": r.get("duration_sec", 0),
            "timestamp": r.get("exit_time", r.get("entry_time", "")),
        } for r in completed[-50:]]
        state["bot_type"] = "yes_mm"
        
        # Load recent log entries from journalctl (systemd) or log file
        log_entries = []
        import subprocess
        try:
            result = subprocess.run(['journalctl', '-u', 'poly-bot', '--no-pager', '-n', '50', '-o', 'short-unix'], 
                                  capture_output=True, text=True, timeout=5)
            for line in result.stdout.strip().split('\n'):
                if ' INFO: ' in line:
                    parts = line.split(' INFO: ', 1)
                    ts_str = parts[0].strip()
                    try:
                        ts = float(ts_str.split()[0])
                    except:
                        ts = time.time()
                    msg = parts[1] if len(parts) > 1 else line
                    log_entries.append({"timestamp": ts, "message": msg})
        except:
            pass
        state["log_entries"] = log_entries[-50:]
        return state
    
    # NO Accumulator format: dict positions, no total_round_trips
    elif isinstance(raw_positions, dict):
        pos_list = [v for v in raw_positions.values() if isinstance(v, dict)]
        cash = state.get("cash", 0)
        positions_value = 0
        unrealized_pnl = 0
        open_positions = []
        
        for p in pos_list:
            if p.get("status") == "open":
                shares = p.get("shares", 0)
                cur_price = p.get("current_price", 0)
                entry_price = p.get("entry_price", 0)
                positions_value += shares * cur_price
                pnl = (cur_price - entry_price) * shares
                unrealized_pnl += pnl
                open_positions.append({
                    "question": p.get("question", "?"),
                    "side": p.get("side", "NO"),
                    "entry_price": entry_price,
                    "current_price": cur_price,
                    "entry_shares": shares,
                    "unrealized_pnl": pnl,
                    "volume24hr": p.get("volume24hr", 0),
                    "one_hour_change": p.get("one_hour_change", 0),
                    "status": "open",
                })
        
        total_value = cash + state.get("total_invested", 0) + unrealized_pnl
        starting = state.get("starting_capital", 100000)
        total_pnl = unrealized_pnl
        total_return_pct = ((total_value - starting) / starting * 100) if starting > 0 else 0
        
        state["total_value"] = total_value
        state["total_pnl"] = total_pnl
        state["total_return_pct"] = total_return_pct
        state["daily_pnl"] = unrealized_pnl
        state["positions"] = open_positions
        state["cash"] = cash
        state["invested"] = state.get("total_invested", 0)
        state["bot_type"] = "no_accumulator"
        
        log_entries = []
        if TRADE_LOG.exists():
            try:
                with open(TRADE_LOG) as f:
                    lines = f.readlines()[-100:]
                for line in lines:
                    parts = line.strip().split(" INFO: ", 1)
                    if len(parts) == 2:
                        ts_str = parts[0].split(" ")[0] + " " + parts[0].split(" ")[1]
                        try:
                            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").timestamp()
                        except:
                            ts = time.time()
                        log_entries.append({"timestamp": ts, "message": parts[1]})
            except:
                pass
        state["log_entries"] = log_entries[-50:]
        return state
    
    else:
        # MM Paper Trader format (legacy)
        cash = state.get("available_cash", 0)
        positions_value = 0
        unrealized_pnl = 0
        
        for p in state.get("positions", []):
            if p.get("status") in ("holding", "pending_sell"):
                positions_value += p.get("current_price", 0) * p.get("entry_shares", 0)
                unrealized_pnl += p.get("unrealized_pnl", 0)
        
        total_value = cash + positions_value
        starting = state.get("starting_capital", 1000)
        total_pnl = state.get("realized_pnl", 0) + unrealized_pnl
        total_return_pct = ((total_value - starting) / starting * 100) if starting > 0 else 0
        
        state["total_value"] = total_value
        state["total_pnl"] = total_pnl
        state["total_return_pct"] = total_return_pct
        state["daily_pnl"] = state.get("realized_pnl", 0)
        state["bot_type"] = "mm_paper_trader"
        
        trade_events = []
        if TRADE_EVENTS_FILE.exists():
            try:
                with open(TRADE_EVENTS_FILE) as f:
                    trade_events = json.load(f)
            except:
                pass
        state["trade_events"] = trade_events[-50:]
        
        log_entries = []
        if TRADE_LOG.exists():
            try:
                with open(TRADE_LOG) as f:
                    lines = f.readlines()[-100:]
                for line in lines:
                    parts = line.strip().split(" INFO: ", 1)
                    if len(parts) == 2:
                        ts_str = parts[0].split(" ")[0] + " " + parts[0].split(" ")[1]
                        try:
                            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").timestamp()
                        except:
                            ts = time.time()
                        log_entries.append({"timestamp": ts, "message": parts[1]})
            except:
                pass
        state["log_entries"] = log_entries[-50:]
        return state

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/state':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            state = load_state()
            self.wfile.write(json.dumps(state, default=str).encode())
        elif self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Suppress default logging


def run_dashboard(port: int = PORT):
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    logger.info(f"Dashboard running on http://localhost:{port}")
    print(f"\n{'='*60}")
    print(f"  📊 POLYMARKET DASHBOARD — YES Market Maker")
    print(f"  Open: http://localhost:{port}")
    print(f"  Auto-refresh: every 3 seconds")
    print(f"  Data source: {STATE_FILE}")
    print(f"  Log: {TRADE_LOG}")
    print(f"{'='*60}\n")
    server.serve_forever()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    run_dashboard(args.port)
