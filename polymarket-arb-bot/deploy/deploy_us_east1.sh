#!/bin/bash
# ============================================================
# Polymarket Bot — us-east-1 Deployment Script
# ============================================================
# Run this ON YOUR us-east-1 AWS INSTANCE (after SSH-ing in)
# Installs all dependencies and deploys the YES MM bot
# ============================================================

set -e

echo "========================================"
echo "  Polymarket YES MM Bot — Deployment"
echo "  Target: AWS us-east-1 (Virginia)"
echo "========================================"
echo

# Check if running as root or sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo bash deploy_us_east1.sh"
  exit 1
fi

# Update system
echo "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# Install Python 3.13 and dependencies
echo "Installing Python and dependencies..."
apt-get install -y -qq python3 python3-pip python3-venv git curl jq

# Create bot directory
BOT_DIR="/opt/polymarket-bot"
mkdir -p $BOT_DIR
cd $BOT_DIR

# Create virtual environment
echo "Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python packages
echo "Installing Python packages..."
pip install --quiet py-clob-client requests websockets pysocks 'requests[socks]'

# Create directory structure
mkdir -p src data/yes_mm logs

echo
echo "========================================"
echo "  ✅ Base System Ready!"
echo "========================================"
echo
echo "Next steps:"
echo "  1. Copy bot files to $BOT_DIR/src/"
echo "     (yes_mm.py, proxy_manager.py, dashboard.py)"
echo "  2. Set your credentials:"
echo "     export POLY_PRIVATE_KEY='your_private_key'"
echo "     export POLY_WALLET_ADDRESS='0x85388D04...'"
echo "  3. Set your EU proxy details:"
echo "     export PROXY_HOST='EU_VPS_IP'"
echo "     export PROXY_PORT=1080"
echo "     export PROXY_USER='polyproxy'"
echo "     export PROXY_PASS='your_proxy_password'"
echo "  4. Test the proxy:"
echo "     cd $BOT_DIR/src && python3 proxy_manager.py --proxy-host \$PROXY_HOST --proxy-port \$PROXY_PORT --proxy-user \$PROXY_USER --proxy-pass \$PROXY_PASS"
echo "  5. Launch the bot:"
echo "     cd $BOT_DIR/src && python3 yes_mm.py --capital 100000 --position-budget 1000 --max-positions 50 --min-yes-price 0.01 --max-yes-price 0.10 --min-spread 0.001 --scan-interval 15 --proxy-host \$PROXY_HOST --proxy-port \$PROXY_PORT --proxy-user \$PROXY_USER --proxy-pass \$PROXY_PASS --live"
echo
