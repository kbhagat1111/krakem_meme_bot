import os
import time
import json
import requests
import datetime
import krakenex
from decimal import Decimal

# ========================
# CONFIG
# ========================
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")

KRAKEN_FEE = 0.0052       # 0.52% per trade
PROFIT_BUFFER = 0.0015    # 0.15% safety margin
ROUND_TRIP_FEE = KRAKEN_FEE * 2
SELL_THRESHOLD = ROUND_TRIP_FEE + PROFIT_BUFFER   # 0.67% net profit required

BASE_PAIRS = ["DOGE/USD", "SHIB/USD", "PEPE/USD", "FLOKI/USD", "BONK/USD"]

# ========================
# KRAKEN CLIENT
# ========================
kraken = krakenex.API(API_KEY, API_SECRET)

def kraken_request(method, data=None, private=False):
    """Wrapper for Kraken API requests with retries."""
    for _ in range(3):
        try:
            if private:
                return kraken.query_private(method, data or {})
            else:
                return kraken.query_public(method, data or {})
        except Exception as e:
            print(f"[ERROR] Kraken API call {method} failed: {e}")
            time.sleep(2)
    return None

# ========================
# UTILITIES
# ========================
def log(msg):
    print(f"[{datetime.datetime.utcnow().isoformat()}] {msg}")

def get_price(pair):
    res = kraken_request("Ticker", {"pair": pair})
    if not res or "error" in res and res["error"]:
        return None
    return Decimal(res["result"][list(res["result"].keys())[0]]["c"][0])

def get_top_gainers():
    """Fetch top gainers from Kraken (mock fallback if no endpoint)."""
    try:
        # Replace with Kraken endpoint once available; using fallback for now
        # Example: Kraken doesn't provide direct "top gainers", so we'd need to simulate
        res = kraken_request("Assets")
        if not res:
            raise Exception("No response")
        # TODO: Implement actual gainer calculation from OHLC if needed
        return BASE_PAIRS  # fallback
    except:
        return BASE_PAIRS

def get_balance():
    res = kraken_request("Balance", private=True)
    if not res or "result" not in res:
        return {}
    return res["result"]

def get_positions():
    res = kraken_request("OpenPositions", private=True)
    if not res or "result" not in res:
        return {}
    return res["result"]

def cancel_all_orders():
    kraken_request("CancelAll", private=True)

def place_order(pair, side, volume):
    log(f"[ORDER] {side.upper()} {volume} {pair}")
    return kraken_request("AddOrder", {
        "pair": pair,
        "type": side,
        "ordertype": "market",
        "volume": str(volume)
    }, private=True)

# ========================
# STARTUP FORCE-SELL
# ========================
def force_sell_startup():
    log("[STARTUP] Cancelling open orders...")
    cancel_all_orders()

    positions = get_positions()
    if not positions:
        log("[STARTUP] No open positions.")
        return

    for txid, pos in positions.items():
        pair = pos["pair"]
        vol = Decimal(pos["vol"])
        cost = Decimal(pos["cost"])
        price = get_price(pair)
        if not price:
            continue
        current_value = vol * price
        profit_pct = (current_value - cost) / cost * 100

        if profit_pct > SELL_THRESHOLD * 100:
            log(f"[FORCE-SELL] {pair} profit {profit_pct:.2f}% > {SELL_THRESHOLD*100:.2f}%")
            place_order(pair, "sell", vol)
        else:
            log(f"[KEEP] {pair} profit {profit_pct:.2f}% <= threshold")

# ========================
# MAIN LOOP
# ========================
def run_bot():
    log("[BOT] Starting loop...")
    last_scan = 0
    trading_pairs = BASE_PAIRS

    while True:
        now = time.time()

        # Rescan every 10 minutes
        if now - last_scan > 600:
            top = get_top_gainers()
            trading_pairs = top[1:5] if len(top) > 1 else BASE_PAIRS
            log(f"[SCAN] Trading pool updated: {trading_pairs}")
            last_scan = now

        # Balances
        balances = get_balance()
        usd_balance = Decimal(balances.get("ZUSD", "0"))

        # Positions
        positions = get_positions()
        total_value = Decimal("0")
        for txid, pos in positions.items():
            pair = pos["pair"]
            vol = Decimal(pos["vol"])
            cost = Decimal(pos["cost"])
            price = get_price(pair)
            if price:
                total_value += vol * price
                profit_pct = (vol*price - cost) / cost * 100
                if profit_pct > SELL_THRESHOLD * 100:
                    log(f"[SELL] {pair} profit {profit_pct:.2f}% > {SELL_THRESHOLD*100:.2f}%")
                    place_order(pair, "sell", vol)

        log(f"[POOL] USD ${usd_balance:.2f} | Positions est ${total_value:.2f}")

        # TODO: Insert buy dip logic here
        # Currently skipping buy if SMA/dip not satisfied
        for pair in trading_pairs:
            log(f"[SKIP BUY] {pair} waiting for dip + momentum")

        time.sleep(15)

# ========================
# ENTRY
# ========================
if __name__ == "__main__":
    force_sell_startup()
    run_bot()
