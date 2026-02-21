#!/usr/bin/env python3
"""
app.py — Polymarket BTC 5m Live Link Server
Hosts a static redirect URL that always points to the current live
BTC 5-minute Polymarket market. Deploy on Render (free tier).

Routes:
  /live   → 302 redirect to current live BTC 5m market
  /status → JSON showing current URL and last update time
"""

import time
import threading
from datetime import datetime
import requests
from flask import Flask, redirect, jsonify

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API        = "https://gamma-api.polymarket.com"
BTC_5MIN_SERIES  = "10684"          # series_id from polymarket_bot.py BotConfig
BTC_5MIN_SLUG    = "-5m-"           # btc_5min_slug_pattern from BotConfig
FALLBACK_URL     = "https://polymarket.com/crypto/5M"
REFRESH_EVERY    = 15               # seconds
KEEP_ALIVE_EVERY = 600              # 10 min — prevents Render free-tier sleep

# ── State ─────────────────────────────────────────────────────────────────────
current_url  = FALLBACK_URL
last_slug    = ""
last_updated = "never"
lock         = threading.Lock()
app          = Flask(__name__)

# ── Discovery (mirrors polymarket_bot.py PolymarketAPI logic) ─────────────────
def fetch_live_btc5m_url() -> str:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "PolyLinkServer/1.0",
        "Accept":     "application/json"
    })

    # Path A: series_id query — mirrors _fetch_series() in PolymarketAPI
    for param_key in ["series_id", "seriesId"]:
        try:
            r = session.get(
                f"{GAMMA_API}/events",
                params={param_key: BTC_5MIN_SERIES, "active": "true", "closed": "false"},
                timeout=10
            )
            r.raise_for_status()
            events = r.json()
            if not isinstance(events, list):
                events = [events]
            for ev in events:
                slug = ev.get("slug", "")
                if BTC_5MIN_SLUG in slug and ("btc" in slug.lower() or "bitcoin" in slug.lower()):
                    return f"https://polymarket.com/event/{slug}"
        except Exception as e:
            print(f"[PATH-A:{param_key}] {e}")

    # Path B: broad events scan — mirrors _discover_5min_via_events() in PolymarketAPI
    try:
        r = session.get(
            f"{GAMMA_API}/events",
            params={"active": "true", "closed": "false", "limit": 50},
            timeout=10
        )
        r.raise_for_status()
        events = r.json()
        if not isinstance(events, list):
            events = [events]
        for ev in events:
            slug = ev.get("slug", "")
            if BTC_5MIN_SLUG in slug and ("btc" in slug.lower() or "bitcoin" in slug.lower()):
                return f"https://polymarket.com/event/{slug}"
    except Exception as e:
        print(f"[PATH-B] {e}")

    # Path C: markets endpoint — mirrors _discover_5min() tag_id scan in PolymarketAPI
    try:
        r = session.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "tag_id": 100381, "limit": 200},
            timeout=10
        )
        r.raise_for_status()
        markets = r.json()
        if not isinstance(markets, list):
            markets = markets.get("data", [])
        for mkt in markets:
            slug = (mkt.get("slug") or "").lower()
            q    = (mkt.get("question") or "").lower()
            if BTC_5MIN_SLUG in slug and ("btc" in slug or "bitcoin" in slug or "btc" in q):
                return f"https://polymarket.com/event/{slug}"
    except Exception as e:
        print(f"[PATH-C] {e}")

    return FALLBACK_URL


# ── Background refresh loop ───────────────────────────────────────────────────
def refresh_loop():
    global current_url, last_slug, last_updated
    while True:
        try:
            url      = fetch_live_btc5m_url()
            new_slug = url.split("/")[-1]
            now      = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            with lock:
                if new_slug != last_slug:
                    print(f"[REFRESH] New market → {url}")
                    current_url  = url
                    last_slug    = new_slug
                    last_updated = now
                # else: same market, silent

        except Exception as e:
            print(f"[REFRESH ERROR] {e}")

        time.sleep(REFRESH_EVERY)


# ── Keep-alive (prevents Render free-tier spin-down) ─────────────────────────
def keep_alive():
    time.sleep(30)  # wait for server to fully start first
    while True:
        time.sleep(KEEP_ALIVE_EVERY)
        try:
            requests.get("http://localhost:5000/status", timeout=5)
            print("[KEEP-ALIVE] pinged")
        except Exception as e:
            print(f"[KEEP-ALIVE ERROR] {e}")


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/live")
def live():
    with lock:
        target = current_url
    return redirect(target, code=302)


@app.route("/status")
def status():
    with lock:
        return jsonify({
            "current_url":       current_url,
            "last_slug":         last_slug,
            "last_updated":      last_updated,
            "refresh_every_sec": REFRESH_EVERY,
            "fallback_url":      FALLBACK_URL
        })


@app.route("/")
def index():
    return jsonify({
        "service": "Polymarket BTC 5m Live Link Server",
        "routes": {
            "/live":   "Redirects to current live BTC 5m market",
            "/status": "Returns current market URL and metadata"
        }
    })


# ── Startup ───────────────────────────────────────────────────────────────────
def startup():
    global current_url, last_slug, last_updated
    url = fetch_live_btc5m_url()
    current_url  = url
    last_slug    = url.split("/")[-1]
    last_updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[INIT] Live market → {url}")

    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=keep_alive,   daemon=True).start()


startup()
@app.route("/wake")
def wake():
    """Force immediate refresh for cold starts"""
    global current_url, last_slug, last_updated
    url = fetch_live_btc5m_url()
    new_slug = url.split("/")[-1]
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    with lock:
        current_url  = url
        last_slug    = new_slug
        last_updated = now
    return f"Woke! Now pointing to: {url}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
