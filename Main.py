# main.py  (stateless scalper, sells only when net profit after fees >= $0.05)
# WARNING: This will place real orders on Kraken. Test with a small balance first.

import os
import time
import math
import ccxt
from datetime import datetime, timedelta
from collections import deque

# ---------------------------
# CONFIG (adjust by env or edit here)
# ---------------------------
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")

# Behavior flags
SELL_ALL_ON_START = os.getenv("SELL_ALL_ON_START", "false").lower() in ("1","true","yes")
SELL_STRATEGY = os.getenv("SELL_STRATEGY", "liquidate")  # "liquidate" or "profit_only"

# Trading parameters
TARGET_TRADES_PER_HOUR = int(os.getenv("TARGET_TRADES_PER_HOUR", "10"))
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "10"))   # 10s loop
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "1.0"))
TRADEABLE_USD_FRAC = float(os.getenv("TRADEABLE_USD_FRAC", "0.70"))  # default 70% usable
FEE_EST_RATE = float(os.getenv("FEE_EST_RATE", "0.0026"))  # Kraken taker fee estimate (0.26%)
MIN_NET_PROFIT_USD = float(os.getenv("MIN_NET_PROFIT_USD", "0.05"))  # required net profit after fees
MAX_CONCURRENT_POS = int(os.getenv("MAX_CONCURRENT_POS", "6"))
MAX_BUYS_PER_CYCLE = int(os.getenv("MAX_BUYS_PER_CYCLE", "2"))

# watchlist & scanning
WATCHLIST = ['SHIB/USD', 'DOGE/USD', 'PEPE/USD']
TOP_MOVER_COUNT = int(os.getenv("TOP_MOVER_COUNT", "6"))

# ---------------------------
# sanity check for keys
# ---------------------------
if not API_KEY or not API_SECRET:
    raise SystemExit("Set KRAKEN_API_KEY and KRAKEN_API_SECRET in Heroku Config Vars")

# ---------------------------
# exchange setup (ccxt)
# ---------------------------
exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
exchange.load_markets()

# ---------------------------
# in-memory state (stateless across restarts)
# ---------------------------
buy_prices = {}      # base -> buy_price_usd (set when bot buys)
reserve_usd = 0.0    # tracks 30% of profits that should not be used (in-memory only)
trade_timestamps = deque()

# ---------------------------
# logging / helpers
# ---------------------------
def now():
    return datetime.utcnow()

def log(msg):
    print(f"[{now().isoformat()}] {msg}", flush=True)

def record_trade():
    trade_timestamps.append(now())

def prune_trades():
    cutoff = now() - timedelta(hours=1)
    while trade_timestamps and trade_timestamps[0] < cutoff:
        trade_timestamps.popleft()

def trades_last_hour():
    prune_trades()
    return len(trade_timestamps)

def safe_fetch_balance():
    try:
        return exchange.fetch_balance()
    except Exception as e:
        log(f"Balance fetch error: {e}")
        return {"total": {}}

def get_total_usd():
    bal = safe_fetch_balance().get("total", {})
    return float(bal.get("USD", 0) or bal.get("ZUSD", 0) or 0.0)

def get_coin_balance(base):
    bal = safe_fetch_balance().get("total", {})
    return float(bal.get(base, 0) or 0.0)

def market_precision_amount(symbol):
    m = exchange.markets.get(symbol)
    if not m:
        return 8
    return m.get("precision", {}).get("amount") or 8

def market_min_amount(symbol):
    m = exchange.markets.get(symbol)
    if not m:
        return None
    return m.get("limits", {}).get("amount", {}).get("min")

def floor_to_precision(amount, precision):
    precision = precision if precision is not None else 8
    factor = 10 ** precision
    return math.floor(amount * factor) / factor

# ---------------------------
# profit calculation
# ---------------------------
def required_net_profit(purchase_usd, current_usd):
    # fees on buy and sell, approximate
    buy_fee = purchase_usd * FEE_EST_RATE
    sell_fee = current_usd * FEE_EST_RATE
    # required net profit must cover both fees and meet MIN_NET_PROFIT_USD
    return max(MIN_NET_PROFIT_USD, buy_fee + sell_fee)

# ---------------------------
# market scanning
# ---------------------------
def get_top_movers(limit=TOP_MOVER_COUNT):
    movers = []
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        log(f"fetch_tickers failed: {e}")
        return []
    for sym, t in tickers.items():
        if not sym.endswith("/USD"):
            continue
        pct = t.get("percentage") or t.get("change") or None
        vol = t.get("baseVolume") or t.get("quoteVolume") or 0
        if pct is None:
            continue
        try:
            p = float(pct)
        except Exception:
            continue
        if p > 0.5 and (vol is None or float(vol) > 200):
            movers.append((sym, p, vol))
    movers.sort(key=lambda x: x[1], reverse=True)
    return [m[0] for m in movers[:limit]]

# ---------------------------
# trade functions
# ---------------------------
def try_sell_if_profitable(symbol, force=False):
    base = symbol.split("/")[0]
    amount = get_coin_balance(base)
    if amount <= 0:
        return False
    # get current price
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("info", {}).get("c", [0])[0])
    except Exception as e:
        log(f"Ticker fetch error for {symbol}: {e}")
        return False

    buy_price = buy_prices.get(base)  # may be None if bot didn't buy it
    purchase_usd = (buy_price or price) * amount
    current_usd = price * amount
    net_profit = (current_usd - purchase_usd) - (purchase_usd * FEE_EST_RATE + current_usd * FEE_EST_RATE)

    required = required_net_profit(purchase_usd, current_usd)

    if force or (buy_price is not None and net_profit >= required):
        # prepare sell amount with correct precision
        prec = market_precision_amount(symbol)
        sell_amt = floor_to_precision(amount, prec)
        if sell_amt <= 0:
            log(f"Sell skip {symbol}: sell_amt 0 after precision")
            return False
        # check market min
        min_a = market_min_amount(symbol)
        if min_a and sell_amt < min_a:
            log(f"Sell failed {symbol}: amount {sell_amt} less than market min {min_a}")
            return False
        try:
            order = exchange.create_market_sell_order(symbol, sell_amt)
            record_trade()
            realized = (price - (buy_price or price)) * sell_amt
            reserve_part = realized * 0.30 if realized > 0 else 0.0
            global reserve_usd
            reserve_usd += reserve_part
            if base in buy_prices:
                buy_prices.pop(base, None)
            log(f"Sold {sell_amt:.8f} {base} at ${price:.8f}. net_profit_est ${net_profit:.4f}, reserve +${reserve_part:.4f}, order id {order.get('id') if isinstance(order, dict) else order}")
            return True
        except Exception as e:
            log(f"Sell order failed for {symbol}: {e}")
            return False
    else:
        log(f"Holding {base}: net_profit ${net_profit:.4f} < required ${required:.4f}")
        return False

def try_buy(symbol, usd_pool):
    base = symbol.split("/")[0]
    if get_coin_balance(base) > 0:
        return False
    # get price
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("info", {}).get("c", [0])[0])
    except Exception as e:
        log(f"Price fetch error for {symbol}: {e}")
        return False

    curr_positions = sum(1 for b in buy_prices.keys() if get_coin_balance(b) > 0)
    free_slots = max(1, MAX_CONCURRENT_POS - curr_positions)
    usd_for_this = max(MIN_TRADE_USD, usd_pool / free_slots)

    raw_amount = usd_for_this / price
    prec = market_precision_amount(symbol)
    amount = floor_to_precision(raw_amount, prec)
    min_a = market_min_amount(symbol)
    if min_a and amount < min_a:
        log(f"Buy skip {symbol}: amount {amount} < market min {min_a}")
        return False
    if amount * price < MIN_TRADE_USD:
        log(f"Buy skip {symbol}: cost ${amount*price:.4f} < MIN_TRADE_USD")
        return False

    try:
        order = exchange.create_market_buy_order(symbol, amount)
        record_trade()
        buy_prices[base] = price
        log(f"Bought {amount:.8f} {base} at ${price:.8f} (allocated ${usd_for_this:.2f}). order id {order.get('id') if isinstance(order, dict) else order}")
        return True
    except Exception as e:
        log(f"Buy order failed for {symbol}: {e}")
        return False

# ---------------------------
# ensure enough tradeable USD by selling positions
# ---------------------------
def ensure_tradeable_usd(target_usd, candidates):
    usd_total = get_total_usd()
    tradeable = usd_total * TRADEABLE_USD_FRAC - reserve_usd
    log(f"ensure_tradeable_usd: tradeable ${tradeable:.2f}, target ${target_usd:.2f}")
    if tradeable >= target_usd:
        return True
    # build positions list
    positions = []
    for sym in candidates:
        base = sym.split("/")[0]
        amt = get_coin_balance(base)
        if amt <= 0:
            continue
        try:
            ticker = exchange.fetch_ticker(sym)
            price = float(ticker.get("last") or ticker.get("info", {}).get("c", [0])[0])
        except:
            price = None
        buy = buy_prices.get(base)
        unreal = None
        if price is not None and buy is not None:
            unreal = (price - buy) * amt
        elif price is not None:
            unreal = 0.0
        positions.append((sym, base, amt, buy, price, unreal))
    # sell best unrealized profits first
    positions.sort(key=lambda x: (x[5] if x[5] is not None else 0.0), reverse=True)
    for sym, base, amt, buy, price, unreal in positions:
        if tradeable >= target_usd:
            break
        if SELL_STRATEGY == "profit_only":
            if buy is None or unreal is None:
                continue
            purch = buy * amt
            if unreal < required_net_profit(purch, price * amt):
                log(f"Skipping {base} under profit_only: unreal ${unreal:.4f} < required")
                continue
        log(f"Attempting to sell {base} to free funds (amt={amt}, price={price}, unreal={unreal})")
        sold = try_sell_if_profitable(sym, force=(SELL_STRATEGY != "profit_only"))
        if sold:
            usd_total = get_total_usd()
            tradeable = usd_total * TRADEABLE_USD_FRAC - reserve_usd
            log(f"After sell, tradeable ${tradeable:.2f}")
        else:
            log(f"Failed selling {base} while freeing funds")
    usd_total = get_total_usd()
    tradeable = usd_total * TRADEABLE_USD_FRAC - reserve_usd
    return tradeable >= target_usd

# ---------------------------
# MAIN LOOP
# ---------------------------
def main_loop():
    global reserve_usd, buy_prices
    log("Stateless scalper starting. SELL_ALL_ON_START = " + str(SELL_ALL_ON_START))
    # optional sell all at start (user requested OFF; default behavior is false)
    if SELL_ALL_ON_START:
        log("SELL_ALL_ON_START true: forcing sells of non-USD positions now.")
        candidates = set(WATCHLIST + get_top_movers(TOP_MOVER_COUNT))
        for s in candidates:
            try_sell_if_profitable(s, force=True)
        time.sleep(2)

    while True:
        start = time.time()
        try:
            prune_trades()
            trades_done = trades_last_hour()
            trades_needed = max(0, TARGET_TRADES_PER_HOUR - trades_done)
            usd_total = get_total_usd()
            tradeable_usd = max(0.0, usd_total * TRADEABLE_USD_FRAC - reserve_usd)
            log(f"USD total: ${usd_total:.2f} tradeable: ${tradeable_usd:.2f} trades_last_hour: {trades_done} reserve_in_memory: ${reserve_usd:.4f}")

            # build candidate list
            candidates = list(WATCHLIST)
            movers = get_top_movers(TOP_MOVER_COUNT)
            for m in movers:
                if m not in candidates:
                    candidates.append(m)

            # If tradeable USD is below MIN_TRADE_USD, try to free funds
            desired_pool = MIN_TRADE_USD * 2
            if tradeable_usd < MIN_TRADE_USD:
                log("Tradeable USD too low; attempting to free funds by selling positions")
                ok = ensure_tradeable_usd(desired_pool, candidates)
                if not ok:
                    log("Unable to free sufficient USD this cycle.")

            # SELL pass: sell positions meeting net profit requirement
            for sym in candidates:
                try_sell_if_profitable(sym)

            # refresh tradeable USD after sells
            usd_total = get_total_usd()
            tradeable_usd = max(0.0, usd_total * TRADEABLE_USD_FRAC - reserve_usd)

            # BUY pass
            buys = 0
            allowed_buys = MAX_BUYS_PER_CYCLE + (1 if trades_needed > 0 else 0)
            for sym in candidates:
                if buys >= allowed_buys:
                    break
                # don't exceed concurrent positions
                held = sum(1 for b in buy_prices.keys() if get_coin_balance(b) > 0)
                if held >= MAX_CONCURRENT_POS:
                    log("Max concurrent positions reached.")
                    break
                if tradeable_usd < MIN_TRADE_USD:
                    log("Not enough tradeable USD for more buys.")
                    break
                ok = try_buy(sym, tradeable_usd)
                if ok:
                    buys += 1
                    usd_total = get_total_usd()
                    tradeable_usd = max(0.0, usd_total * TRADEABLE_USD_FRAC - reserve_usd)
                    time.sleep(1.15)

        except Exception as e:
            log(f"Main loop error: {e}")

        elapsed = time.time() - start
        to_sleep = max(1, CYCLE_SECONDS - elapsed)
        time.sleep(to_sleep)

if __name__ == "__main__":
    main_loop()
