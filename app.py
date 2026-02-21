#!/usr/bin/env python3
"""
app.py — Polymarket BTC 5m Live Link Server v3.0

Strategy: compute the current 5-minute window slug DIRECTLY from the
system clock (UTC rounded down to nearest 5 min), matching Polymarket's
slug naming convention: btc-updown-5m-{unix_ts_of_window_start_utc}

Falls back to Gamma API discovery (mirrors polymarket_bot.py) if the
computed slug doesn't exist on Polymarket.
"""

import re
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

import requests
from flask import Flask, redirect, jsonify

# ── Config ─────────────────────────────────────────────────────────────────
GAMMA_API        = "https://gamma-api.polymarket.com"
BTC_5MIN_SERIES  = "10684"          # btc_5min_series_id in BotConfig
CRYPTO_TAG_ID    = 100381           # PolymarketAPI.CRYPTO_TAG_ID
FALLBACK_URL     = "https://polymarket.com/crypto/5M"
REFRESH_EVERY    = 10               # seconds — recalculates every 10s
KEEP_ALIVE_EVERY = 600              # seconds
POLYMARKET_BASE  = "https://polymarket.com/event"

# mirrors polymarket_bot.py helpers
DUR_RE = re.compile(
    r"(\d{1,2}):?(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):?(\d{2})\s*(AM|PM)",
    re.IGNORECASE
)
FIVEMIN_KEYWORDS = re.compile(r"5-?min(ute)?s?|5m", re.IGNORECASE)

# ── State ──────────────────────────────────────────────────────────────────
current_url  = FALLBACK_URL
last_updated = "never"
last_method  = "none"
lock         = threading.Lock()
app          = Flask(__name__)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PolyLinkServer/3.0", "Accept": "application/json"})


# ── Timestamp-based slug (primary method) ─────────────────────────────────

def compute_slug_for_window(dt_utc: datetime) -> str:
    """Round dt_utc DOWN to nearest 5-min boundary -> slug timestamp."""
    minutes = (dt_utc.minute // 5) * 5
    window_start = dt_utc.replace(minute=minutes, second=0, microsecond=0)
    ts = int(window_start.timestamp())
    return f"btc-updown-5m-{ts}"


def slug_exists(slug: str) -> bool:
    """Verify slug exists via Gamma events API."""
    try:
        r = SESSION.get(f"{GAMMA_API}/events/{slug}", timeout=8)
        return r.status_code == 200
    except Exception:
        return False


def get_url_by_timestamp() -> Optional[str]:
    """
    Build slug from current UTC time. Try current window first,
    then next window (in case current just resolved), then previous.
    """
    now_utc = datetime.now(timezone.utc)
    candidates = [
        now_utc,                            # current window
        now_utc + timedelta(minutes=5),     # next window (if current just closed)
        now_utc - timedelta(minutes=5),     # previous window (buffer)
    ]
    for candidate in candidates:
        slug = compute_slug_for_window(candidate)
        if slug_exists(slug):
            return f"{POLYMARKET_BASE}/{slug}"
    return None


# ── API-based discovery (fallback — mirrors polymarket_bot.py) ────────────

def _slug_duration(slug: str) -> Optional[int]:
    s = (slug or "").lower()
    if "-5m-" in s: return 5
    if "-15m-" in s: return 15
    if "-1h-" in s: return 60
    return None


def _parse_duration(question: str) -> int:
    m = DUR_RE.search(question or "")
    if not m:
        return 15
    def to_min(h, mn, ap):
        hi, mni = int(h), int(mn)
        ap = ap.upper()
        if ap == "PM" and hi != 12: hi += 12
        elif ap == "AM" and hi == 12: hi = 0
        return hi * 60 + mni
    start = to_min(m.group(1), m.group(2), m.group(3))
    end   = to_min(m.group(4), m.group(5), m.group(6))
    diff  = end - start
    if diff <= 0: diff += 1440
    return diff if 1 <= diff <= 60 else 15


def get_url_via_api() -> Optional[str]:
    """
    Mirrors PolymarketAPI._fetch_series + _discover_5min + _enrich.
    Queries series_id=10684, then broad scan. Returns soonest-ending
    BTC 5m market whose endDate is in the future.
    """
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    candidates: List[Dict] = []

    # Path A: series_id (mirrors _fetch_series)
    for param_key in ["series_id", "seriesId"]:
        try:
            r = SESSION.get(
                f"{GAMMA_API}/events",
                params={param_key: BTC_5MIN_SERIES, "active": "true", "closed": "false"},
                timeout=10
            )
            r.raise_for_status()
            events = r.json()
            if not isinstance(events, list): events = [events]
            for ev in events:
                slug = (ev.get("slug") or "").lower()
                if "-5m-" in slug and ("btc" in slug or "bitcoin" in slug):
                    end_str = ev.get("endDate") or ev.get("end_date")
                    if end_str:
                        try:
                            end_ms = datetime.fromisoformat(
                                end_str.replace("Z", "+00:00")
                            ).timestamp() * 1000
                            if end_ms > now_ms:
                                candidates.append({"slug": ev["slug"], "end_ms": end_ms})
                        except Exception:
                            pass
        except Exception as e:
            print(f"[API-A:{param_key}] {e}")

    # Path B: /markets tag scan (mirrors _discover_5min)
    if not candidates:
        for tag_id in [CRYPTO_TAG_ID, None]:
            params = {"active": "true", "closed": "false", "limit": 200}
            if tag_id: params["tag_id"] = tag_id
            try:
                r = SESSION.get(f"{GAMMA_API}/markets", params=params, timeout=10)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list): data = data.get("data", [])
                for mkt in data:
                    q    = (mkt.get("question") or "").lower()
                    slug = (mkt.get("slug") or "").lower()
                    if not ("bitcoin" in q or "btc" in q): continue
                    dur = _slug_duration(slug)
                    if dur is None: dur = _parse_duration(mkt.get("question", ""))
                    kw  = bool(FIVEMIN_KEYWORDS.search(mkt.get("question", "")))
                    if dur != 5 and not kw and "-5m-" not in slug: continue
                    end_str = mkt.get("endDate") or mkt.get("end_date")
                    if not end_str: continue
                    try:
                        end_ms = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00")
                        ).timestamp() * 1000
                        if end_ms > now_ms:
                            candidates.append({"slug": mkt["slug"], "end_ms": end_ms})
                    except Exception:
                        pass
                if candidates: break
            except Exception as e:
                print(f"[API-B] {e}")

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["end_ms"])
    best = candidates[0]
    print(f"[API] chose slug={best['slug']}")
    return f"{POLYMARKET_BASE}/{best['slug']}"


# ── Master fetch: timestamp first, API fallback ────────────────────────────

def fetch_live_url() -> tuple:
    """Returns (url, method_used)."""
    url = get_url_by_timestamp()
    if url:
        return url, "timestamp"
    print("[FETCH] timestamp method failed, trying API fallback")
    url = get_url_via_api()
    if url:
        return url, "api"
    print("[FETCH] both methods failed, using fallback")
    return FALLBACK_URL, "fallback"


# ── Background threads ────────────────────────────────────────────────────

def refresh_loop():
    global current_url, last_updated, last_method
    while True:
        try:
            url, method = fetch_live_url()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            with lock:
                current_url  = url
                last_updated = now
                last_method  = method
        except Exception as e:
            print(f"[REFRESH ERROR] {e}")
        time.sleep(REFRESH_EVERY)


def keep_alive():
    time.sleep(30)
    while True:
        time.sleep(KEEP_ALIVE_EVERY)
        try:
            SESSION.get("http://localhost:5000/status", timeout=5)
            print("[KEEP-ALIVE] ping")
        except Exception:
            pass


# ── Routes ────────────────────────────────────────────────────────────────

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
            "last_updated":      last_updated,
            "last_method":       last_method,
            "refresh_every_sec": REFRESH_EVERY,
        })


@app.route("/")
def index():
    return jsonify({
        "service": "Polymarket BTC 5m Link Server v3.0",
        "routes": {
            "/live":   "Redirects to current BTC 5m market",
            "/status": "JSON: current URL, method used, last update time"
        }
    })


# ── Startup ───────────────────────────────────────────────────────────────

def startup():
    global current_url, last_updated, last_method
    url, method = fetch_live_url()
    current_url  = url
    last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    last_method  = method
    print(f"[INIT] method={method} url={url}")
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=keep_alive,   daemon=True).start()


startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
