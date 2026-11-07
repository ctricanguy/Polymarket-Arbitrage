#!/usr/bin/env python3
"""
Split Architecture Proxy Manager
=================================
Routes GET requests directly (fast, ~10ms from us-east-1)
Routes POST /order requests through EU SOCKS5 proxy (unblock, ~80ms)

Usage in yes_mm.py:
    from proxy_manager import ProxyManager
    pm = ProxyManager(proxy_host='EU_VPS_IP', proxy_port=1080)
    
    # GET requests — direct, fast
    markets = pm.get('https://gamma-api.polymarket.com/markets')
    
    # POST orders — via EU proxy, unblocked
    result = pm.post_order(order_payload)
"""

import requests
import logging
import time
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PROXY] %(message)s')
logger = logging.getLogger(__name__)


class ProxyManager:
    """
    Manages split routing: GETs go direct, POSTs go through EU SOCKS5 proxy.
    
    The CLOB API geoblocks POST /order from US IPs.
    GET requests (markets, books, prices) work fine from anywhere.
    
    This class routes:
      - GET → direct connection (us-east-1 → Polymarket ~10ms)
      - POST /order → SOCKS5 proxy in EU (us-east-1 → EU VPS → Polymarket ~80ms)
    """
    
    def __init__(self, proxy_host=None, proxy_port=1080, proxy_user=None, proxy_pass=None):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.proxy_user = proxy_user
        self.proxy_pass = proxy_pass
        self.enabled = proxy_host is not None
        
        if self.enabled:
            # Build SOCKS5 proxy URL
            auth = ""
            if proxy_user and proxy_pass:
                auth = f"{proxy_user}:{proxy_pass}@"
            self.proxy_url = f"socks5://{auth}{proxy_host}:{proxy_port}"
            self.proxies = {
                'http': self.proxy_url,
                'https': self.proxy_url,
            }
            logger.info(f"Proxy enabled: {proxy_host}:{proxy_port} (SOCKS5)")
            logger.info(f"GET requests → direct (fast)")
            logger.info(f"POST orders  → via EU proxy (unblocked)")
        else:
            self.proxies = None
            logger.info("No proxy configured — all requests direct (will be geoblocked for orders from US)")
        
        # Stats
        self.stats = {
            'get_direct': 0,
            'get_direct_ms': 0,
            'post_proxied': 0,
            'post_proxied_ms': 0,
            'post_errors': 0,
        }
    
    def get(self, url, params=None, timeout=10):
        """GET request — goes DIRECT (no proxy), fast path."""
        start = time.time()
        try:
            r = requests.get(url, params=params, timeout=timeout)
            elapsed = (time.time() - start) * 1000
            self.stats['get_direct'] += 1
            self.stats['get_direct_ms'] += elapsed
            return r
        except Exception as e:
            logger.error(f"GET {url[:60]}... failed: {e}")
            raise
    
    def post_order(self, url, json_data, timeout=15):
        """POST /order — goes through EU SOCKS5 proxy to bypass geoblock."""
        start = time.time()
        
        if not self.enabled:
            logger.warning("POST /order without proxy — will be geoblocked from US!")
            try:
                r = requests.post(url, json=json_data, timeout=timeout)
                elapsed = (time.time() - start) * 1000
                self.stats['post_proxied'] += 1
                self.stats['post_proxied_ms'] += elapsed
                
                if r.status_code == 403:
                    logger.error(f"GEOBLOCKED (403): {r.text[:100]}")
                    self.stats['post_errors'] += 1
                
                return r
            except Exception as e:
                logger.error(f"POST order failed: {e}")
                self.stats['post_errors'] += 1
                raise
        
        try:
            r = requests.post(url, json=json_data, proxies=self.proxies, timeout=timeout)
            elapsed = (time.time() - start) * 1000
            self.stats['post_proxied'] += 1
            self.stats['post_proxied_ms'] += elapsed
            
            if r.status_code == 403:
                logger.error(f"Still geoblocked through proxy! Check proxy location.")
                logger.error(f"Response: {r.text[:200]}")
                self.stats['post_errors'] += 1
            elif r.status_code == 200:
                logger.info(f"Order POST OK via proxy ({elapsed:.0f}ms)")
            else:
                logger.warning(f"Order POST returned {r.status_code} ({elapsed:.0f}ms): {r.text[:100]}")
            
            return r
        except requests.exceptions.ConnectTimeout:
            logger.error(f"Proxy connection timeout — is EU VPS running? {self.proxy_host}:{self.proxy_port}")
            self.stats['post_errors'] += 1
            raise
        except requests.exceptions.ProxyError as e:
            logger.error(f"Proxy error: {e}")
            self.stats['post_errors'] += 1
            raise
        except Exception as e:
            logger.error(f"POST order failed: {e}")
            self.stats['post_errors'] += 1
            raise
    
    def test_proxy(self):
        """Test that the proxy works and exits in an allowed (non-geoblocked) region."""
        if not self.enabled:
            print("No proxy configured")
            return False
        
        print(f"Testing proxy: {self.proxy_host}:{self.proxy_port}")
        
        # Test 1: Can we connect through the proxy?
        try:
            r = requests.get(
                'https://polymarket.com/api/geoblock',
                proxies=self.proxies,
                timeout=15
            )
            geo = r.json()
            print(f"  Proxy IP location: {geo.get('country', '?')} {geo.get('region', '?')}")
            print(f"  Blocked: {geo.get('blocked', '?')}")
            
            if geo.get('blocked'):
                print(f"  ❌ Proxy is in a BLOCKED region ({geo.get('country')})!")
                print(f"  Need a VPS in: Ireland, Spain, Sweden, Netherlands, etc.")
                return False
            else:
                print(f"  ✅ Proxy is in an ALLOWED region — orders will work!")
        except Exception as e:
            print(f"  ❌ Cannot connect to proxy: {e}")
            return False
        
        # Test 2: Can we POST /order through the proxy?
        print(f"  Testing POST /order through proxy...")
        dummy_order = {
            'order': {
                'salt': '1', 'maker': '0x0', 'signer': '0x0',
                'taker': '0x0', 'tokenId': '123',
                'makerAmount': '1', 'takerAmount': '1',
                'expiration': '0', 'nonce': '0', 'feeRateBps': '0',
                'side': 'BUY', 'signatureType': 1, 'signature': '0x',
            },
            'orderType': 'GTC', 'chainId': 137,
        }
        try:
            r = requests.post(
                'https://clob.polymarket.com/order',
                json=dummy_order,
                proxies=self.proxies,
                timeout=15
            )
            if r.status_code == 403 and 'geoblock' in r.text.lower():
                print(f"  ❌ Still geoblocked through proxy!")
                return False
            else:
                # Any non-403-geoblock response means the proxy works
                # (we'll get 400 "invalid order" since it's a dummy)
                print(f"  ✅ POST /order accepted by proxy (status {r.status_code})")
                print(f"  Response: {r.text[:100]}")
                return True
        except Exception as e:
            print(f"  ❌ POST test failed: {e}")
            return False
    
    def test_direct(self):
        """Test direct connection latency (for GET requests)."""
        print("Testing direct connection (GET requests)...")
        times = []
        for _ in range(5):
            try:
                start = time.time()
                r = requests.get('https://clob.polymarket.com/time', timeout=5)
                t = (time.time() - start) * 1000
                times.append(t)
            except:
                pass
        if times:
            avg = sum(times) / len(times)
            print(f"  Direct GET latency: avg={avg:.0f}ms min={min(times):.0f}ms max={max(times):.0f}ms")
            return True
        else:
            print(f"  ❌ Direct connection failed")
            return False
    
    def get_stats(self):
        """Return routing statistics."""
        get_avg = (self.stats['get_direct_ms'] / self.stats['get_direct']
                   if self.stats['get_direct'] > 0 else 0)
        post_avg = (self.stats['post_proxied_ms'] / self.stats['post_proxied']
                    if self.stats['post_proxied'] > 0 else 0)
        return {
            'get_requests': self.stats['get_direct'],
            'get_avg_ms': round(get_avg, 1),
            'post_requests': self.stats['post_proxied'],
            'post_avg_ms': round(post_avg, 1),
            'post_errors': self.stats['post_errors'],
            'proxy_enabled': self.enabled,
            'proxy_host': self.proxy_host or 'none',
        }
    
    def print_stats(self):
        """Print routing statistics."""
        s = self.get_stats()
        print(f"Proxy Stats:")
        print(f"  GET (direct):    {s['get_requests']} requests, avg {s['get_avg_ms']}ms")
        print(f"  POST (proxied):  {s['post_requests']} requests, avg {s['post_avg_ms']}ms")
        print(f"  POST errors:     {s['post_errors']}")
        print(f"  Proxy: {s['proxy_host']}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Test split architecture proxy')
    parser.add_argument('--proxy-host', help='EU VPS IP address')
    parser.add_argument('--proxy-port', type=int, default=1080)
    parser.add_argument('--proxy-user', help='SOCKS5 username (optional)')
    parser.add_argument('--proxy-pass', help='SOCKS5 password (optional)')
    args = parser.parse_args()
    
    pm = ProxyManager(
        proxy_host=args.proxy_host,
        proxy_port=args.proxy_port,
        proxy_user=args.proxy_user,
        proxy_pass=args.proxy_pass,
    )
    
    print()
    pm.test_direct()
    print()
    if pm.enabled:
        success = pm.test_proxy()
        if success:
            print()
            print("✅ Split architecture ready!")
            print("   GET requests → direct (fast)")
            print("   POST orders  → via EU proxy (unblocked)")
        else:
            print()
            print("❌ Proxy not working yet — check EU VPS setup")
    else:
        print()
        print("No proxy configured — run with --proxy-host <EU_VPS_IP>")
