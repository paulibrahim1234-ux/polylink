#!/usr/bin/env python3
"""
app.py — Polymarket BTC 5m Live Link Server ("soonest end" version)

Selects the active BTC 5-minute market whose end time is the
soonest in the future, using logic analogous to PolymarketAPI
from polymarket_bot.py.
"""

import time
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from flask import Flask, redirect, jsonify

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API        = "https://gamma-api.polymarket.com"
CRYPTO_TAG_ID    = 100381          # same as PolymarketAPI.CRYPTO_TAG_ID
BTC_5MIN_SLUG    = "-5m-"          # matches cfg.btc_5min_slug_pattern
REFRESH_EVERY    = 15              # seconds
KEEP_ALIVE_EVERY = 600             # seconds
FALLBACK_URL     = "https://polymarket.com/crypto/5M"

# ── Regex-like helpers from polymarket_bot.py (simplified) ───────────────────
import re
DUR_RE = re.compile(r"(\d{1,2}):?(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):?(\d{2})\s*(AM|PM)", re.IGNORECASE)
FIVEMIN_KEYWORDS = re.compile(r"5-?min(ute)?s?|5m", re.IGNORECASE)


def _parse_duration(question: str) -> int:
    m = DUR_RE.search(question or "")
    if not m:
        return 15

    def to_min(h: str, mn: str, ap: str) -> int:
        hi, mni = int(h), int(mn)
        ap = ap.upper()
        if ap == "PM" and hi != 12:
            hi += 12
        elif ap == "AM" and hi == 12:
            hi = 0
        return hi * 60 + mni

    start = to_min(m.group(1), m.group(2), m.group(3))
    end = to_min(m.group(4), m.group(5), m.group(6))
    diff = end - start
    if diff <= 0:
        diff += 1440
    return diff if 1 <= diff <= 60 else 15


def _slug_duration(slug: str) -> Optional[int]:
    s = (slug or "").lower()
    if "-5m-" in s:
        return 5
    if "-15m-" in s:
        return 15
    if "-1h-" in s:
        return 60
    if "up-or-down" in s and s.endswith("-et"):
        return 60
    return None


# ── State ─────────────────────────────────────────────────────────────────────
current_url  = FALLBACK_URL
last_updated = "never"
lock         = threading.Lock()
app          = Flask(__name__)


# ── Core discovery: find soonest-ending BTC 5m market ────────────────────────

def fetch_btc_5m_markets() -> List[Dict]:
    """Fetch active crypto markets and filter to BTC 5m.

    Mirrors PolymarketAPI._fetch_markets_for_5min_discovery + _discover_5min.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "PolyLinkServer/2.0", "Accept": "application/json"})

    params = {"active": "true", "closed": "false", "limit": 200, "tag_id": CRYPTO_TAG_ID}
    try:
        r = session.get(f"{GAMMA_API}/markets", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            data = data.get("data", [])
    except Exception as e:
        print(f"[MARKETS] primary tag query failed: {e}")
        # broad fallback without tag
        try:
            r = session.get(f"{GAMMA_API}/markets", params={"active": "true", "closed": "false", "limit": 200}, timeout=10)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                data = data.get("data", [])
        except Exception as e2:
            print(f"[MARKETS] broad query failed: {e2}")
            return []

    btc_5m: List[Dict] = []
    for mkt in data:
        q = (mkt.get("question") or "").lower()
        slug = (mkt.get("slug") or "").lower()
        if not ("bitcoin" in q or "btc" in q):
            continue

        dur = _slug_duration(slug)
        if dur is None:
            dur = _parse_duration(mkt.get("question", ""))
        keyword_match = bool(FIVEMIN_KEYWORDS.search(mkt.get("question", "")))
        slug_match = "-5m-" in slug
        if dur != 5 and not keyword_match and not slug_match:
            continue

        # Enrich with end_ms similar to _enrich()
        end_str = mkt.get("endDate") or mkt.get("end_date")
        if not end_str:
            continue
        try:
            end_ms = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            continue

        m = dict(mkt)
        m["duration_min"] = 5
        m["end_ms"] = end_ms
        btc_5m.append(m)

    return btc_5m


def pick_soonest_btc_5m_url() -> str:
    markets = fetch_btc_5m_markets()
    if not markets:
        print("[PICK] no BTC 5m markets found, using fallback")
        return FALLBACK_URL

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    # keep only markets that end in the future
    future = [m for m in markets if m["end_ms"] > now_ms]
    if not future:
        print("[PICK] no future BTC 5m markets, using fallback")
        return FALLBACK_URL

    future.sort(key=lambda m: m["end_ms"])  # soonest end first
    best = future[0]
    slug = best.get("slug")
    if not slug:
        print("[PICK] best market missing slug, using fallback")
        return FALLBACK_URL

    url = f"https://polymarket.com/event/{slug}"
    end_dt = datetime.fromtimestamp(best["end_ms"] / 1000, tz=timezone.utc).astimezone()
    print(f"[PICK] chose {slug} ending at {end_dt.isoformat()}")
    return url


# ── Background refresh + keep-alive ──────────────────────────────────────────

def refresh_loop():
    global current_url, last_updated
    while True:
        try:
            url = pick_soonest_btc_5m_url()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            with lock:
                current_url = url
                last_updated = now
        except Exception as e:
            print(f"[REFRESH ERROR] {e}")
        time.sleep(REFRESH_EVERY)


def keep_alive():
    time.sleep(30)
    while True:
        time.sleep(KEEP_ALIVE_EVERY)
        try:
            requests.get("http://localhost:5000/status", timeout=5)
            print("[KEEP-ALIVE] ping")
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
            "current_url": current_url,
            "last_updated": last_updated,
            "refresh_every_sec": REFRESH_EVERY,
            "fallback_url": FALLBACK_URL,
        })


@app.route("/")
def index():
    return jsonify({
        "service": "Polymarket BTC 5m Live Link Server (soonest end)",
        "routes": {"/live": "Redirects to soonest-ending BTC 5m market", "/status": "Current URL & metadata"},
    })


# ── Startup ───────────────────────────────────────────────────────────────────

def startup():
    global current_url, last_updated
    current_url = pick_soonest_btc_5m_url()
    last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[INIT] starting at {current_url}")
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()


startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
