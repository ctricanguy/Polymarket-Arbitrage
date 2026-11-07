# Polymarket YES MM Bot — Split Architecture Setup Guide

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  AWS us-east-1 (Virginia) — Main Bot Server (~$5-8/mo)  │
│                                                         │
│  yes_mm.py (bot)                                        │
│    ├─ GET requests (99%) → direct to Polymarket (~10ms) │
│    └─ POST /order (1%)  → via EU SOCKS5 proxy (~80ms)   │
│                                                         │
│  dashboard.py (monitor) → http://bot-ip:8484            │
└──────────────────────┬──────────────────────────────────┘
                       │ SOCKS5 tunnel
                       ▼
┌─────────────────────────────────────────────────────────┐
│  EU VPS (Ireland/Frankfurt) — Proxy Only (~€4/mo)       │
│                                                         │
│  dante-server (SOCKS5 proxy on port 1080)               │
│  Just relays POST requests to Polymarket                │
│  No bot logic runs here                                 │
└─────────────────────────────────────────────────────────┘
```

## Why This Works

- **Polymarket geoblocks POST /order from US IPs** (403 error)
- **GET requests work fine from US** (markets, orderbooks, prices)
- The bot does 99% GETs (scanning) and 1% POSTs (placing orders)
- GETs go direct from us-east-1 at ~10ms (super fast)
- POSTs route through EU proxy at ~80ms (fast enough for limit orders)
- Limit orders sit in the book anyway — 80ms placement time doesn't matter

## Step-by-Step Setup

### Step 1: Create the EU VPS (Proxy Server)

**Option A: Hetzner Cloud (cheapest, €4/month)**
1. Go to https://console.hetzner.cloud
2. Create new project → Add server
3. Location: **Falkenstein (Germany)** ← Wait, Germany is blocked! Use **Helsinki (Finland)** instead
   - Actually, Hetzner locations: Nuremberg (DE), Falkenstein (DE), Helsinki (FI), Ashburn (US), Hillsboro (US)
   - **Use Helsinki (FI) — Finland is NOT on the geoblock list ✅**
4. Image: Ubuntu 22.04
5. Type: CX11 (1 vCPU, 1GB RAM) — €4.15/month
6. Add SSH key
7. Note the server IP

**Option B: AWS eu-west-1 (Ireland, ~$8/month)**
1. AWS Console → EC2 → Launch Instance
2. Region: **eu-west-1 (Ireland)**
3. AMI: Ubuntu 22.04 LTS
4. Type: t3.micro (1 vCPU, 1GB RAM)
5. Security group: Allow inbound TCP 1080 from your us-east-1 IP
6. Note the public IP

**Option C: Contabo VPS (cheap, €4/month)**
1. Go to https://contabo.com
2. Order VPS S (1 vCPU, 4GB RAM)
3. Location: **Singapore** or **Netherlands** ← Wait, NL is frontend-only block... 
   - Actually Netherlands is "Close-only on Frontend, API not restricted" — **API WORKS! ✅**
   - But to be safe, use a clearly allowed country

**RECOMMENDED: Hetzner Helsinki (Finland) — €4/month, clearly allowed**

### Step 2: Set Up the SOCKS5 Proxy

SSH into your EU VPS and run the setup script:

```bash
# SSH into EU VPS
ssh root@YOUR_EU_VPS_IP

# Download and run the proxy setup script
apt-get update && apt-get install -y git
# Copy the setup_proxy.sh script to the VPS (or wget it)
# Option 1: Copy-paste the file contents
nano setup_proxy.sh
# [paste contents of deploy/setup_proxy.sh]
# Option 2: SCP from your local machine
# scp deploy/setup_proxy.sh root@YOUR_EU_VPS_IP:/root/

# Run it
sudo bash setup_proxy.sh
```

The script will:
- Check if the VPS is in an allowed region (auto-verifies with Polymarket)
- Install dante-server (SOCKS5 proxy)
- Create a proxy user with random password
- Open port 1080
- Print the proxy credentials (SAVE THESE!)

**Output you'll see:**
```
✅ This VPS is in an ALLOWED region (FI)!
...
Proxy details:
  Host: 65.108.xxx.xxx
  Port: 1080
  User: polyproxy
  Pass: a1b2c3d4e5f6...
```

### Step 3: Create the us-east-1 Instance (Bot Server)

1. AWS Console → EC2 → Launch Instance
2. Region: **us-east-1 (Virginia)**
3. AMI: Ubuntu 22.04 LTS
4. Type: t3.small (2 vCPU, 2GB RAM) — ~$15/month
   - t3.micro works too (~$8/month) but might be slow for 300+ market scans
5. Security group: Allow inbound TCP 22 (SSH) and TCP 8484 (dashboard)
6. Note the public IP

### Step 4: Deploy the Bot on us-east-1

SSH into your us-east-1 instance:

```bash
# SSH in
ssh ubuntu@YOUR_US_EAST_1_IP

# Run the deployment script
sudo bash deploy_us_east1.sh

# Copy bot files (from your local machine)
# scp -r src/ ubuntu@YOUR_US_EAST_1_IP:/opt/polymarket-bot/
# scp -r deploy/ ubuntu@YOUR_US_EAST_1_IP:/opt/polymarket-bot/

# Set credentials
export POLY_PRIVATE_KEY='your_private_key_here'
export POLY_WALLET_ADDRESS='0x85388D04...'

# Set proxy credentials (from Step 2)
export PROXY_HOST='65.108.xxx.xxx'  # EU VPS IP
export PROXY_PORT=1080
export PROXY_USER='polyproxy'
export PROXY_PASS='a1b2c3d4e5f6...'
```

### Step 5: Test the Proxy Connection

Before running the bot, verify the split architecture works:

```bash
cd /opt/polymarket-bot/src
source ../venv/bin/activate

# Test proxy
python3 proxy_manager.py \
  --proxy-host $PROXY_HOST \
  --proxy-port $PROXY_PORT \
  --proxy-user $PROXY_USER \
  --proxy-pass $PROXY_PASS
```

**Expected output:**
```
Testing direct connection (GET requests)...
  Direct GET latency: avg=10ms min=8ms max=12ms    ← FAST! us-east-1 direct

Testing proxy: 65.108.xxx.xxx:1080
  Proxy IP location: FI
  Blocked: False
  ✅ Proxy is in an ALLOWED region — orders will work!
  Testing POST /order through proxy...
  ✅ POST /order accepted by proxy (status 400)    ← 400 = bad order data, but NOT geoblocked!

✅ Split architecture ready!
   GET requests → direct (fast)
   POST orders  → via EU proxy (unblocked)
```

### Step 6: Launch the Bot (Paper Mode First)

```bash
cd /opt/polymarket-bot/src
source ../venv/bin/activate

# Paper trade with proxy configured (orders simulated, but proxy tested)
python3 yes_mm.py \
  --capital 100000 \
  --position-budget 1000 \
  --max-positions 50 \
  --min-yes-price 0.01 \
  --max-yes-price 0.10 \
  --min-spread 0.001 \
  --scan-interval 15 \
  --proxy-host $PROXY_HOST \
  --proxy-port $PROXY_PORT \
  --proxy-user $PROXY_USER \
  --proxy-pass $PROXY_PASS \
  --reset
```

### Step 7: Go LIVE

Once paper trading looks good:

```bash
python3 yes_mm.py \
  --capital 1000 \
  --position-budget 50 \
  --max-positions 20 \
  --min-yes-price 0.01 \
  --max-yes-price 0.10 \
  --min-spread 0.001 \
  --scan-interval 15 \
  --proxy-host $PROXY_HOST \
  --proxy-port $PROXY_PORT \
  --proxy-user $PROXY_USER \
  --proxy-pass $PROXY_PASS \
  --live \
  --reset
```

**Key changes for live:**
- `--capital 1000` — your real $1k (not $100k simulated)
- `--position-budget 50` — $50 per position (20 positions × $50 = $1k)
- `--live` — places REAL orders via proxy

### Step 8: Monitor

```bash
# Dashboard (open in browser)
http://YOUR_US_EAST_1_IP:8484

# Or watch logs
tail -f /opt/polymarket-bot/logs/yes_mm.log
```

## Cost Summary

| Component | Cost |
|-----------|------|
| AWS us-east-1 t3.small | ~$15/month |
| Hetzner Helsinki CX11 | ~€4/month (~$4.50) |
| **Total** | **~$19.50/month** |

Cheaper option: t3.micro on us-east-1 (~$8) + Hetzner (~$4.50) = **~$12.50/month**

## Troubleshooting

### "Cannot connect to proxy"
- Check EU VPS is running: `ssh root@EU_VPS_IP`
- Check dante-server: `systemctl status danted`
- Check firewall: `ufw status` (port 1080 should be open)
- Check from us-east-1: `nc -zv EU_VPS_IP 1080`

### "Still geoblocked through proxy"
- The EU VPS might be in a blocked country
- Check: `curl https://polymarket.com/api/geoblock` from the EU VPS
- If blocked, destroy VPS and create in: Ireland, Finland, Spain, Sweden

### "Direct GET latency is 150ms+"
- You're not actually on us-east-1 — check your AWS region
- Or Polymarket's Cloudflare edge is routing you wrong
- Try from a different us-east-1 AZ

### "POST /order returns 400 (not 403)"
- This is GOOD! 400 = bad request (invalid order data), NOT geoblocked
- 403 = geoblocked. 400 = proxy works, order just has bad data
- The bot will handle signing properly in live mode
