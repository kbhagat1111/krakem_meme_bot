# main.py — stateless multi-coin scalper for Kraken (ccxt)
# WARNING: This will place real market orders. Test with small balances.

import os
import time
import math
import ccxt
from datetime import datetime, timedelta
from collections import deque

# ---------------- Config (tweak as needed via env or here)
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")
SELL_ALL_ON_START = os.getenv("SELL_ALL_ON_START", "false").lower() in ("1","true","yes")
SELL_STRATEGY = os.getenv("SELL_STRATEGY", "liquidate")  # "liquidate" or "profit_only"
TARGET_TRADES_PER_HOUR = int(os.getenv("TARGET_TRADES_PER_HOUR", "10"))

TRADE_CYCLE_SECONDS = 6                     # how often the bot cycles
MIN_TRADE_USD = 1.0                         # minimum order cost USD
TRADEABLE_USD_FRAC = 0.70                   # 70% usable for trading; 30% reserved
FEE_EST_RATE = 0.0026                       # estimated taker fee (0.26%)
EXTRA_PROFIT_USD = 0.05                     # extra absolute profit beyond fees
MAX_CONCURRENT_POS = 6
MAX_BUYS_PER_CYCLE = 2
WATCHLIST = ['SHIB/USD', 'DOGE/USD', 'PEPE/USD']  # base watchlist; scanner will add movers
TOP_MOVER_COUNT = 6

if not API_KEY or not API_SECRET:
    raise SystemExit("Set KRAKEN_API_KEY and KRAKEN_API_SECRET in env vars on Heroku")

# ---------------- Exchange setup
exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
exchange.load_markets()

# ---------------- State (in-memory)
buy_prices = {}     # base -> buy_price (USD) — this is in-memory only
reserve_usd = 0.0   # 30% tracked in-memory (not moved off-exchange)
trade_timestamps = deque()

# ---------------- Helpers
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
    b = safe_fetch_balance().get("total", {})
    return float(b.get("USD", 0) or b.get("ZUSD", 0) or 0.0)

def get_coin_balance(base):
    b = safe_fetch_balance().get("total", {})
    return float(b.get(base, 0) or 0.0)

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
    if precision is None:
        precision = 8
    factor = 10 ** precision
    return math.floor(amount * factor) / factor

def required_profit_usd(purchase_usd):
    est_fees = purchase_usd * FEE_EST_RATE * 2
    return max(EXTRA_PROFIT_USD, est_fees)

# ---------------- Market scanning helpers
def get_top_movers(limit=TOP_MOVER_COUNT):
    movers = []
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        log(f"Failed to fetch tickers: {e}")
        return []
    for sym, t in tickers.items():
        if not sym.endswith("/USD"):
            continue
        pct = t.get("percentage") or t.get("change") or t.get("info", {}).get("p")
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

# ---------------- Trading core
def try_sell_if_profitable(symbol, force=False):
    """Sell a position in symbol if profit >= required OR force True"""
    base = symbol.split("/")[0]
    amt = get_coin_balance(base)
    if amt <= 0:
        return False
    # if force, we sell regardless of profit (liquidation)
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("info", {}).get("c", [None])[0])
    except Exception as e:
        log(f"Ticker fetch error for {symbol}: {e}")
        return False

    buy_price = buy_prices.get(base)  # may be None
    purchase_usd = (buy_price or price) * amt
    profit_usd = (price - (buy_price or price)) * amt  # if buy_price None => 0
    required = required_profit_usd(purchase_usd)

    if force or (buy_price is not None and profit_usd >= required):
        prec = market_precision_amount(symbol)
        sell_amt = floor_to_precision(amt, prec)
        if sell_amt <= 0:
            log(f"Sell skip {symbol}: sell_amt 0 after precision")
            return False
        try:
            order = exchange.create_market_sell_order(symbol, sell_amt)
            record_trade()
            # approximate realized profit = (price - buy_price) * sell_amt
            realized = (price - (buy_price or price)) * sell_amt
            reserve_part = realized * 0.30 if realized > 0 else 0.0
            global reserve_usd
            reserve_usd += reserve_part
            if base in buy_prices:
                buy_prices.pop(base, None)
            log(f"Sold {sell_amt:.8f} {base} at ${price:.8f} profit est ${realized:.4f} reserve +${reserve_part:.4f} order id {order.get('id') if isinstance(order, dict) else order}")
            return True
        except Exception as e:
            log(f"Sell order failed for {symbol}: {e}")
            return False
    else:
        log(f"Holding {base}: profit ${profit_usd:.4f} < required ${required:.4f}")
        return False

def try_buy(symbol, usd_pool):
    base = symbol.split("/")[0]
    if get_coin_balance(base) > 0:
        return False
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("info", {}).get("c", [None])[0])
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
        log(f"Bought {amount:.8f} {base} at ${price:.8f} (allocated ${usd_for_this:.2f}) order id {order.get('id') if isinstance(order, dict) else order}")
        return True
    except Exception as e:
        log(f"Buy order failed for {symbol}: {e}")
        return False

# ---------------- Helper: decide which positions to sell to free USD
def ensure_tradeable_usd(target_usd, candidates):
    """
    Ensure we have at least target_usd available for trading.
    If not, sell positions (by profit desc) until satisfied.
    candidates is list of symbols to consider (WATCHLIST + movers).
    Returns True if achieved, False otherwise.
    """
    usd_total = get_total_usd()
    tradeable = usd_total * TRADEABLE_USD_FRAC
    log(f"ensure_tradeable_usd: currently tradeable ${tradeable:.2f}, target ${target_usd:.2f}")
    if tradeable >= target_usd:
        return True

    # build list of positions with estimated unrealized profit
    pos = []
    for sym in candidates:
        base = sym.split("/")[0]
        amt = get_coin_balance(base)
        if amt <= 0:
            continue
        try:
            ticker = exchange.fetch_ticker(sym)
            price = float(ticker.get("last") or ticker.get("info", {}).get("c", [None])[0])
        except Exception:
            price = None
        buy = buy_prices.get(base)
        unreal = None
        if price is not None and buy is not None:
            unreal = (price - buy) * amt
        elif price is not None:
            unreal = 0.0
        pos.append((sym, base, amt, buy, price, unreal))

    # sort: prefer selling positions with highest unrealized profit first (liquidate profits)
    pos.sort(key=lambda x: (x[5] if x[5] is not None else 0.0), reverse=True)

    # If strategy is profit_only, only sell those with profit >= required; otherwise liquidate as needed
    for sym, base, amt, buy, price, unreal in pos:
        if tradeable >= target_usd:
            break
        if SELL_STRATEGY == "profit_only":
            if buy is None:
                continue
            purch_usd = buy * amt
            if unreal is None:
                continue
            if unreal < required_profit_usd(purch_usd):
                log(f"Skipping {base} (no sufficient profit) under profit_only strategy")
                continue
        # sell this position to free funds
        log(f"Attempting to sell {base} to free funds (amt={amt}, price={price}, unreal=${unreal})")
        sold = try_sell_if_profitable(sym, force=(SELL_STRATEGY!="profit_only"))
        if sold:
            usd_total = get_total_usd()
            tradeable = usd_total * TRADEABLE_USD_FRAC
            log(f"After sell, tradeable ${tradeable:.2f}")
        else:
            log(f"Failed to sell {base} while trying to free funds")
    usd_total = get_total_usd()
    tradeable = usd_total * TRADEABLE_USD_FRAC
    return tradeable >= target_usd

# ---------------- Main loop
def main_loop():
    global reserve_usd, buy_prices
    log("Stateless scalper starting (will sell to create USD if needed).")
    # optional initial full-sell
    if SELL_ALL_ON_START:
        log("SELL_ALL_ON_START is true — attempting to liquidate all non-USD positions now.")
        # list all markets from watchlist + top movers
        candidates = set(WATCHLIST + get_top_movers(TOP_MOVER_COUNT))
        # sell all positions forced
        for sym in candidates:
            try_sell_if_profitable(sym, force=True)
        time.sleep(2)

    while True:
        loop_start = time.time()
        try:
            prune_trades()
            trades_done = trades_last_hour()
            trades_needed = max(0, TARGET_TRADES_PER_HOUR - trades_done)
            usd_total = get_total_usd()
            tradeable_usd = usd_total * TRADEABLE_USD_FRAC
            log(f"USD total: ${usd_total:.2f} tradeable: ${tradeable_usd:.2f} trades_last_hour: {trades_done}")

            # build candidate list
            candidates = list(WATCHLIST)
            movers = get_top_movers(TOP_MOVER_COUNT)
            for m in movers:
                if m not in candidates:
                    candidates.append(m)

            # If there is virtually no USD, ensure we free at least MIN_TRADE_USD * 2 (to allow 2 buys)
            desired_pool = MIN_TRADE_USD * 2
            if tradeable_usd < MIN_TRADE_USD:
                log("Tradeable USD below minimum — attempting to free funds by selling positions")
                ok = ensure_tradeable_usd(desired_pool, candidates)
                if not ok:
                    log("Unable to free sufficient USD this loop (no sellers or sells failed). Will retry next cycle.")

            # SELL pass: try to sell positions that meet profit targets
            for sym in candidates:
                try_sell_if_profitable(sym)

            # refresh tradeable USD after sells
            usd_total = get_total_usd()
            tradeable_usd = usd_total * TRADEABLE_USD_FRAC

            # BUY pass: attempt up to allowed buys
            buys = 0
            allowed_buys = MAX_BUYS_PER_CYCLE + (1 if trades_needed > 0 else 0)
            for sym in candidates:
                if buys >= allowed_buys:
                    break
                if sum(1 for b in buy_prices.keys() if get_coin_balance(b) > 0) >= MAX_CONCURRENT_POS:
                    log("Max concurrent positions held; skipping further buys")
                    break
                if tradeable_usd < MIN_TRADE_USD:
                    log("Not enough tradeable USD to buy; skipping buys this cycle")
                    break
                ok = try_buy(sym, tradeable_usd)
                if ok:
                    buys += 1
                    usd_total = get_total_usd()
                    tradeable_usd = usd_total * TRADEABLE_USD_FRAC
                    time.sleep(1.2)

        except Exception as e:
            log(f"Main loop error: {e}")

        elapsed = time.time() - loop_start
        sleep_for = max(1, TRADE_CYCLE_SECONDS - elapsed)
        time.sleep(sleep_for)

if __name__ == "__main__":
    main_loop()
