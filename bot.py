"""
pump.fun Callout Watcher -> Discord

Polls pump.fun's callouts feed on an interval, figures out which callouts
are new (not seen before), and posts each new one to a Discord webhook.

Setup instructions are in README.md. Short version:
  1. Open https://pump.fun/callouts in your browser while logged in.
  2. Open DevTools -> Network tab, filter for "callout".
  3. Find the request that returns the callout list. Copy:
       - the full request URL          -> CALLOUTS_URL
       - the "authorization" header    -> PUMP_AUTH  (include "Bearer ..." if present)
       - the "cookie" header           -> PUMP_COOKIE (optional, some endpoints need it)
  4. Put those in your .env (local) or Railway service variables (prod),
     along with DISCORD_WEBHOOK_URL.
  5. Run `python bot.py` once locally to confirm it prints callouts to the
     console correctly before deploying.

If pump.fun changes the shape of the response, adjust `parse_callouts()`
below -- the raw JSON is logged on parse failure so you can see what
changed.
"""

import json
import os
import time
import logging
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("callout-bot")

# ---------------------------------------------------------------------------
# Config (all from environment variables so nothing secret lives in code)
# ---------------------------------------------------------------------------

CALLOUTS_URL = os.environ.get(
    "CALLOUTS_URL",
    "https://frontend-api-v3.pump.fun/callouts",  # placeholder -- confirm in DevTools, see README
)
PUMP_AUTH = os.environ.get("PUMP_AUTH", "")        # e.g. "Bearer eyJhbGciOi..."
PUMP_COOKIE = os.environ.get("PUMP_COOKIE", "")    # optional
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))

# Where "already notified" callout IDs are persisted. On Railway, mount a
# volume at /data so this survives restarts/redeploys; locally it just
# writes next to the script.
STATE_PATH = Path(os.environ.get("STATE_PATH", "/data/seen_callouts.json"))
if not STATE_PATH.parent.exists():
    STATE_PATH = Path(__file__).parent / "seen_callouts.json"

MAX_SEEN_IDS = 5000  # cap memory/disk growth, oldest are dropped first

if not DISCORD_WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL is not set. Add it as an env var and restart.")

# ---------------------------------------------------------------------------
# State (dedup) handling
# ---------------------------------------------------------------------------

def load_seen() -> list:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read state file, starting fresh.")
    return []

def save_seen(seen_ids: list) -> None:
    trimmed = seen_ids[-MAX_SEEN_IDS:]
    try:
        STATE_PATH.write_text(json.dumps(trimmed))
    except OSError as e:
        log.error("Failed to persist state: %s", e)

# ---------------------------------------------------------------------------
# Fetching + parsing pump.fun's callouts feed
# ---------------------------------------------------------------------------

def fetch_callouts() -> list:
    headers = {
        "Accept": "application/json",
        "Origin": "https://pump.fun",
        "Referer": "https://pump.fun/callouts",
        "User-Agent": "Mozilla/5.0",
    }
    if PUMP_AUTH:
        headers["Authorization"] = PUMP_AUTH
    if PUMP_COOKIE:
        headers["Cookie"] = PUMP_COOKIE

    resp = requests.get(CALLOUTS_URL, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def _first(d: dict, *keys, default=None):
    """Return the first present key's value from a dict of uncertain shape."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

def parse_callouts(raw) -> list:
    """
    Normalize whatever pump.fun returns into a flat list of dicts with
    consistent keys. The exact field names for this endpoint are not
    officially documented, so this is defensive: it tries several likely
    key names and falls back gracefully.
    """
    items = raw
    if isinstance(raw, dict):
        # Some pump.fun endpoints wrap arrays in {"data": [...]} or similar
        items = _first(raw, "data", "callouts", "items", "results", default=[])

    if not isinstance(items, list):
        log.warning("Unexpected callouts response shape, raw response: %s", raw)
        return []

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        callout_id = _first(item, "id", "calloutId", "_id", "uuid")
        username = _first(item, "username", "displayName", "handle", "name", default="unknown")
        wallet = _first(item, "wallet", "walletAddress", "user", "creator", default="")
        message = _first(item, "content", "message", "text", "body", default="")
        coin_symbol = _first(item, "symbol", "ticker", default="")
        coin_name = _first(item, "coinName", "tokenName", default="")
        mint = _first(item, "mint", "mintAddress", "tokenAddress", default="")
        position = _first(item, "position", "positionUsd", default=None)
        pnl = _first(item, "netPnl", "pnl", "profitLoss", default=None)
        mc_called_at = _first(item, "marketCapAtCallout", "calledAtMc", "startMc", default=None)
        mc_now = _first(item, "currentMarketCap", "marketCapNow", "mc", default=None)
        timestamp = _first(item, "createdAt", "timestamp", "created_at", default=None)

        if not callout_id:
            # fall back to a stable-ish synthetic id so we don't silently drop it
            callout_id = f"{wallet}:{mint}:{timestamp}:{message[:40]}"

        normalized.append({
            "id": str(callout_id),
            "username": username,
            "wallet": wallet,
            "message": message,
            "coin_symbol": coin_symbol,
            "coin_name": coin_name,
            "mint": mint,
            "position": position,
            "pnl": pnl,
            "mc_called_at": mc_called_at,
            "mc_now": mc_now,
            "timestamp": timestamp,
        })
    return normalized

# ---------------------------------------------------------------------------
# Discord posting
# ---------------------------------------------------------------------------

def post_to_discord(callout: dict) -> None:
    coin_label = callout["coin_symbol"] or callout["coin_name"] or "Unknown coin"
    title = f"📣 New callout: {coin_label}"

    fields = []
    if callout["message"]:
        fields.append({"name": "Message", "value": callout["message"][:1000], "inline": False})
    if callout["position"] is not None:
        fields.append({"name": "Position", "value": f"${callout['position']}", "inline": True})
    if callout["pnl"] is not None:
        fields.append({"name": "Net PNL", "value": f"${callout['pnl']}", "inline": True})
    if callout["mc_called_at"] is not None or callout["mc_now"] is not None:
        fields.append({
            "name": "Called at -> Now",
            "value": f"{callout['mc_called_at']} -> {callout['mc_now']}",
            "inline": True,
        })
    if callout["mint"]:
        fields.append({
            "name": "Coin",
            "value": f"[View on pump.fun](https://pump.fun/coin/{callout['mint']})",
            "inline": False,
        })

    embed = {
        "title": title,
        "description": f"by **{callout['username']}**" + (f" (`{callout['wallet']}`)" if callout["wallet"] else ""),
        "fields": fields,
        "color": 0x00FF88,
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    if resp.status_code >= 300:
        log.error("Discord webhook failed (%s): %s", resp.status_code, resp.text[:300])

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    seen = load_seen()
    seen_set = set(seen)
    log.info("Starting up. %d callouts already seen. Polling every %ss.", len(seen), POLL_INTERVAL_SECONDS)
    log.info("Watching: %s", CALLOUTS_URL)

    first_pass = True

    while True:
        try:
            raw = fetch_callouts()
            callouts = parse_callouts(raw)

            new_ones = [c for c in callouts if c["id"] not in seen_set]

            if first_pass:
                # Don't blast Discord with the entire feed history on first boot --
                # just record what's already there and start notifying from here.
                log.info("First pass: recording %d existing callouts as seen, no notifications sent.", len(new_ones))
                for c in new_ones:
                    seen_set.add(c["id"])
                    seen.append(c["id"])
                first_pass = False
            else:
                for c in reversed(new_ones):  # oldest-first so Discord order makes sense
                    log.info("New callout: %s by %s", c["coin_symbol"] or c["mint"], c["username"])
                    post_to_discord(c)
                    seen_set.add(c["id"])
                    seen.append(c["id"])

            if new_ones:
                save_seen(seen)

        except requests.RequestException as e:
            log.error("Fetch failed: %s", e)
        except Exception as e:
            log.exception("Unexpected error: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    run()
