# main.py
# Rapid multi-coin scalper for Kraken with Redis persistence
# Requires env vars: KRAKEN_API_KEY, KRAKEN_API_SECRET, and REDIS_URL (Heroku Redis add-on will provide)

import os
import time
import math
import json
import ccxt
import redis
from collections import deque
from datetime import datetime, timedelta

# ----------------------------
# Configurable parameters
# ----------------------------
TARGET_TRADES_PER_HOUR = 10
CYCLE_SECONDS = 6
MIN_TRADE_USD = 1.0
TRADEABLE_USD_FRAC = 0.70
FEE_EST_RATE = 0.0026
EXTRA_PROFIT_USD = 0.05
MAX_CONCURRENT_POS = 6
MAX_BUYS_PER_CYCLE = 2
WATCHLIST = ['SHIB/USD', 'DOGE/USD', 'PEPE/USD']
TOP_MOVER_COUNT = 8

# ----------------------------
# Setup exchange (ccxt Kraken)
# ----------------------------
API_KEY = os.getenv('KRAKEN_API_KEY')
API_SECRET = os.getenv('KRAKEN_API_SECRET')
REDIS_URL = os.getenv('REDIS_URL')  # set by Heroku Redis add-on

if not API_KEY or not API_SECRET:
    raise SystemExit("Set KRAKEN_API_KEY and KRAKEN_API_SECRET in env vars")
if not REDIS_URL:
    raise SystemExit("Set REDIS_URL (Heroku Redis add-on must be provisioned)")

exchange = ccxt.kraken({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
})
exchange.load_markets()

# ----------------------------
# Setup Redis
# ----------------------------
r = redis.from_url(REDIS_URL, decode_responses=True)

# Redis keys we use
KEY_BUY_PRICES = "bot:buy_prices"      # hash: base -> price (str)
KEY_RESERVE = "bot:reserve_usd"        # string: float
KEY_TRADES = "bot:trade_timestamps"    # list: ISO timestamp strings (LPUSH newest)
KEY_VERSION = "bot:version"            # for future migrations

# ----------------------------
# Helpers: persistence and logging
# ----------------------------
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

def now_iso():
    return datetime.utcnow().isoformat()

def iso_to_dt(s):
    return datetime.fromisoformat(s)

# ----------------------------
# Persistent state helpers
# ----------------------------
def load_buy_prices():
    # returns dict base->float
    raw = r.hgetall(KEY_BUY_PRICES)
    return {k: float(v) for k, v in raw.items()} if raw else {}

def save_buy_price(base, price):
    r.hset(KEY_BUY_PRICES, base, repr(price))

def delete_buy_price(base):
    r.hdel(KEY_BUY_PRICES, base)

def load_reserve():
    v = r.get(KEY_RESERVE)
    return float(v) if v is not None else 0.0

def add_to_reserve(amount):
    # use Redis INCRBYFLOAT if available, fallback
    try:
        r.incrbyfloat(KEY_RESERVE, float(amount))
    except Exception:
        cur = load_reserve()
        r.set(KEY_RESERVE, repr(cur + float(amount)))

def push_trade_timestamp(ts_iso):
    # LPUSH newest on left; keep list trimmed to last 1000 entries
    r.lpush(KEY_TRADES, ts_iso)
    r.ltrim(KEY_TRADES, 0, 999)

def load_trade_timestamps():
    # returns deque of datetimes (oldest first)
    raw = r.lrange(KEY_TRADES, 0, -1) or []
    # raw is newest-first; reverse to oldest-first
    raw_rev = list(reversed(raw))
    return deque([iso_to_dt(x) for x in raw_rev])

def prune_trade_timestamps_in_redis():
    timestamps = load_trade_timestamps()
    cutoff = datetime.utcnow() - timedelta(hours=1)
    kept = [dt.isoformat() for dt in timestamps if dt >= cutoff]
    # overwrite Redis list with kept timestamps (newest-first)
    if kept:
        pipe = r.pipeline()
        pipe.delete(KEY_TRADES)
        for ts in reversed(kept):  # reversed so LPUSH produces newest-first
            pipe.lpush(KEY_TRADES, ts)
        pipe.execute()
    else:
        r.delete(KEY_TRADES)

# ----------------------------
# In-memory view of state (mirrors Redis)
# ----------------------------
buy_prices = load_buy_prices()   # base -> price
reserve_usd = load_reserve()
trade_timestamps = load_trade_timestamps()

# ----------------------------
# Utilities
# ----------------------------
def record_trade():
    t = now_iso()
    push_trade_timestamp(t)
    trade_timestamps.append(datetime.fromisoformat(t))

def prune_old_trades():
    cutoff = datetime.utcnow() - timedelta(hours=1)
    while trade_timestamps and trade_timestamps[0] < cutoff:
        trade_timestamps.popleft()
    # persist trimmed list
    prune_trade_timestamps_in_redis()

def trades_last_hour():
    prune_old_trades()
    return len(trade_timestamps)

def safe_fetch_balance():
    try:
        return exchange.fetch_balance()
    except Exception as e:
        log(f"Balance fetch error: {e}")
        return {'total': {}}

def get_total_usd():
    bal = safe_fetch_balance()
    total = bal.get('total', {})
    return float(total.get('USD', 0) or total.get('ZUSD', 0) or 0.0)

def get_coin_balance(base):
    bal = safe_fetch_balance()
    total = bal.get('total', {})
    return float(total.get(base, 0) or 0.0)

def market_min_amount(symbol):
    m = exchange.markets.get(symbol)
    if not m:
        return None
    return m.get('limits', {}).get('amount', {}).get('min')

def market_precision_amount(symbol):
    m = exchange.markets.get(symbol)
    if not m:
        return 8
    return m.get('precision', {}).get('amount') or 8

def floor_to_precision(amount, precision):
    if precision is None:
        precision = 8
    factor = 10 ** precision
    return math.floor(amount * factor) / factor

def required_profit_usd(purchase_usd):
    est_fees = purchase_usd * FEE_EST_RATE * 2
    return max(EXTRA_PROFIT_USD, est_fees)

# ----------------------------
# Market scanning
# ----------------------------
def get_top_movers(limit=TOP_MOVER_COUNT):
    movers = []
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        log(f"Failed to fetch tickers: {e}")
        return []
    for sym, t in tickers.items():
        if not sym.endswith('/USD'):
            continue
        pct = t.get('percentage') or t.get('change') or None
        vol = t.get('baseVolume') or t.get('quoteVolume') or 0
        if pct is not None:
            try:
                p = float(pct)
            except:
                continue
            if p > 0.5 and (vol is None or float(vol) > 200):
                movers.append((sym, p, vol))
    movers.sort(key=lambda x: x[1], reverse=True)
    return [m[0] for m in movers[:limit]]

# ----------------------------
# Trading functions (persistent)
# ----------------------------
def try_sell_if_profitable(symbol):
    base = symbol.split('/')[0]
    amt = get_coin_balance(base)
    if amt <= 0:
        return False
    if base not in buy_prices:
        log(f"Skipping sell {base}: no recorded buy price (amt={amt})")
        return False
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get('last') or ticker.get('info', {}).get('c', [None])[0])
    except Exception as e:
        log(f"Ticker fetch error for {symbol}: {e}")
        return False

    buy_price = float(buy_prices[base])
    purchase_usd = buy_price * amt
    profit_usd = (price - buy_price) * amt
    required = required_profit_usd(purchase_usd)

    if profit_usd >= required:
        prec = market_precision_amount(symbol)
        sell_amt = floor_to_precision(amt, prec)
        if sell_amt <= 0:
            log(f"Sell skip {symbol}: computed sell amount 0")
            return False
        try:
            order = exchange.create_market_sell_order(symbol, sell_amt)
            record_trade()
            # update reserve and clear buy price in both Redis and memory
            reserve_part = profit_usd * 0.30
            add_to_reserve(reserve_part)
            # reload reserve in memory
            global reserve_usd
            reserve_usd = load_reserve()
            delete_buy_price(base)
            buy_prices.pop(base, None)
            log(f"Sold {sell_amt:.8f} {base} at ${price:.8f} profit ${profit_usd:.4f} (reserve +${reserve_part:.4f}) order id {order.get('id') if isinstance(order, dict) else order}")
            return True
        except Exception as e:
            log(f"Sell failed for {symbol}: {e}")
            return False
    else:
        log(f"Holding {base}: profit ${profit_usd:.4f} < required ${required:.4f}")
        return False

def try_buy(symbol, usd_pool):
    base = symbol.split('/')[0]
    if get_coin_balance(base) > 0:
        return False
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get('last') or ticker.get('info', {}).get('c', [None])[0])
    except Exception as e:
        log(f"Price fetch failed for {symbol}: {e}")
        return False

    curr_positions = sum(1 for b in buy_prices.keys() if get_coin_balance(b) > 0)
    free_slots = max(1, MAX_CONCURRENT_POS - curr_positions)
    usd_for_this = max(MIN_TRADE_USD, usd_pool / free_slots)

    raw_amount = usd_for_this / price
    prec = market_precision_amount(symbol)
    amount = floor_to_precision(raw_amount, prec)

    min_amount = market_min_amount(symbol)
    if min_amount and amount < min_amount:
        log(f"Buy skip {symbol}: amount {amount} < min {min_amount}")
        return False
    if amount * price < MIN_TRADE_USD:
        log(f"Buy skip {symbol}: cost ${amount*price:.4f} < MIN_TRADE_USD")
        return False

    try:
        order = exchange.create_market_buy_order(symbol, amount)
        record_trade()
        buy_prices[base] = price
        save_buy_price(base, price)
        log(f"Bought {amount:.8f} {base} at ${price:.8f} allocated ${usd_for_this:.2f} order id {order.get('id') if isinstance(order, dict) else order}")
        return True
    except Exception as e:
        log(f"Buy failed for {symbol}: {e}")
        return False

# ----------------------------
# Main loop
# ----------------------------
def main_loop():
    global reserve_usd, buy_prices, trade_timestamps
    # reload persisted state in case Redis changed externally
    buy_prices = load_buy_prices()
    reserve_usd = load_reserve()
    trade_timestamps = load_trade_timestamps()

    log("Starting scalper with Redis persistence")
    while True:
        start = time.time()
        try:
            prune_old_trades()
            trades_done = trades_last_hour()
            trades_needed = max(0, TARGET_TRADES_PER_HOUR - trades_done)

            usd_total = get_total_usd()
            tradeable_usd = max(0.0, usd_total * TRADEABLE_USD_FRAC)
            log(f"USD total: ${usd_total:.2f} tradeable: ${tradeable_usd:.2f} trades_last_hour: {trades_done} reserve_usd_memory: ${reserve_usd:.4f}")

            # candidate list
            candidates = list(WATCHLIST)
            movers = get_top_movers(TOP_MOVER_COUNT)
            for m in movers:
                if m not in candidates:
                    candidates.append(m)

            # SELL pass
            for sym in list(candidates):
                try_sell_if_profitable(sym)

            # refresh tradeable pool
            usd_total = get_total_usd()
            tradeable_usd = max(0.0, usd_total * TRADEABLE_USD_FRAC)

            # BUY pass
            buys = 0
            allowed_buys = MAX_BUYS_PER_CYCLE + (1 if trades_needed > 0 else 0)
            for sym in candidates:
                if buys >= allowed_buys:
                    break
                if sum(1 for b in buy_prices.keys() if get_coin_balance(b) > 0) >= MAX_CONCURRENT_POS:
                    break
                if tradeable_usd < MIN_TRADE_USD:
                    break
                ok = try_buy(sym, tradeable_usd)
                if ok:
                    buys += 1
                    usd_total = get_total_usd()
                    tradeable_usd = max(0.0, usd_total * TRADEABLE_USD_FRAC)
                    time.sleep(1.1)

        except Exception as e:
            log(f"Main loop error: {e}")

        elapsed = time.time() - start
        to_sleep = max(1, CYCLE_SECONDS - elapsed)
        time.sleep(to_sleep)

if __name__ == '__main__':
    main_loop()
