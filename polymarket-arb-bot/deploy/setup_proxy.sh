#!/bin/bash
# ============================================================
# SOCKS5 Proxy Setup for Polymarket Split Architecture
# ============================================================
# Run this script ON YOUR EU VPS (Hetzner, AWS eu-west-1, etc.)
# It installs a lightweight SOCKS5 proxy that your us-east-1
# bot will route order POSTs through.
#
# Requirements: Fresh Ubuntu 22.04/24.04 VPS in an ALLOWED region
# (Ireland, Spain, Sweden, Netherlands, etc. — NOT US/UK/DE/FR)
# ============================================================

set -e

echo "========================================"
echo "  Polymarket SOCKS5 Proxy Setup"
echo "========================================"
echo

# Check if running as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo bash setup_proxy.sh"
  exit 1
fi

# Get the public IP
PUBLIC_IP=$(curl -s http://ip-api.com/json | python3 -c "import sys,json; print(json.load(sys.stdin)['query'])" 2>/dev/null || echo "unknown")
echo "This VPS public IP: $PUBLIC_IP"

# Check geoblock status
echo "Checking Polymarket geoblock status..."
GEOBLOCK=$(curl -s https://polymarket.com/api/geoblock)
echo "Geoblock response: $GEOBLOCK"
echo

BLOCKED=$(echo $GEOBLOCK | python3 -c "import sys,json; print(json.load(sys.stdin).get('blocked', 'unknown'))" 2>/dev/null || echo "unknown")
COUNTRY=$(echo $GEOBLOCK | python3 -c "import sys,json; print(json.load(sys.stdin).get('country', 'unknown'))" 2>/dev/null || echo "unknown")

if [ "$BLOCKED" = "True" ]; then
  echo "❌ This VPS is in a BLOCKED region ($COUNTRY)!"
  echo "   You need a VPS in: Ireland, Spain, Sweden, Netherlands, etc."
  echo "   Blocked regions: US, UK, France, Germany, Italy, Canada (most), etc."
  exit 1
fi

echo "✅ This VPS is in an ALLOWED region ($COUNTRY)!"
echo

# Install dante-server (SOCKS5 proxy)
echo "Installing SOCKS5 proxy (dante-server)..."
apt-get update -qq
apt-get install -y -qq dante-server

# Create proxy user
PROXY_USER="polyproxy"
PROXY_PASS=$(openssl rand -hex 12)
echo "Creating proxy user: $PROXY_USER"
useradd -r -s /bin/false $PROXY_USER 2>/dev/null || true
echo "$PROXY_USER:$PROXY_PASS" | chpasswd

# Detect main network interface
INTERFACE=$(ip route | grep default | awk '{print $5}' | head -1)
echo "Network interface: $INTERFACE"

# Configure dante-server
cat > /etc/danted.conf << EOF
logoutput: syslog
internal: 0.0.0.0 port = 1080
external: $INTERFACE
socksmethod: username
clientmethod: none
user.privileged: root
user.notprivileged: $PROXY_USER

client pass {
    from: 0.0.0.0/0 to: 0.0.0.0/0
    log: error
}

socks pass {
    from: 0.0.0.0/0 to: clob.polymarket.com
    log: connect error
}

socks pass {
    from: 0.0.0.0/0 to: polymarket.com
    log: connect error
}

socks pass {
    from: 0.0.0.0/0 to: .
    log: connect error
}
EOF

# Start and enable dante-server
systemctl restart danted
systemctl enable danted

# Open firewall port 1080
if command -v ufw &> /dev/null; then
  ufw allow 1080/tcp
  echo "Firewall: port 1080 opened (ufw)"
elif command -v iptables &> /dev/null; then
  iptables -A INPUT -p tcp --dport 1080 -j ACCEPT
  echo "Firewall: port 1080 opened (iptables)"
fi

echo
echo "========================================"
echo "  ✅ SOCKS5 Proxy Ready!"
echo "========================================"
echo
echo "Proxy details:"
echo "  Host: $PUBLIC_IP"
echo "  Port: 1080"
echo "  User: $PROXY_USER"
echo "  Pass: $PROXY_PASS"
echo "  Type: SOCKS5 (with auth)"
echo
echo "Test from your us-east-1 bot:"
echo "  python3 proxy_manager.py --proxy-host $PUBLIC_IP --proxy-port 1080 --proxy-user $PROXY_USER --proxy-pass $PROXY_PASS"
echo
echo "Bot config (add to yes_mm.py args):"
echo "  --proxy-host $PUBLIC_IP --proxy-port 1080 --proxy-user $PROXY_USER --proxy-pass $PROXY_PASS"
echo
echo "⚠️  Save these credentials! They won't be shown again."
echo
