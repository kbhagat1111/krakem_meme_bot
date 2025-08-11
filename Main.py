# main.py
# Kraken scalper (ccxt) — buys on dips + momentum, sells on net profit or stop-loss,
# 70/30 reserve, trailing stop, cooldown, logs reasons.
#
# WARNING: This script places real orders. Test with small balances first.

import os
import time
import math
import ccxt
from datetime import datetime, timedelta
from collections import defaultdict, deque

# ---------------- Config (can be overridden with Heroku Config Vars) ----------------
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "30"))    # how often the bot loops
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "1.0")) # minimum USD allocation per buy
TRADEABLE_FRAC = float(os.getenv("TRADEABLE_FRAC", "0.70"))
RESERVE_FRAC = float(os.getenv("RESERVE_FRAC", "0.30"))
FEE_EST = float(os.getenv("FEE_EST", "0.0026"))          # approx taker fee 0.26
DIP_PCT = float(os.getenv("DIP_PCT", "0.02"))            # 2% dip required
TAKE_PROFIT_NET_PCT = float(os.getenv("TAKE_PROFIT_NET_PCT", "0.04"))  # 4% net after fees
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-0.04"))            # -4% stop loss
SIDEWAYS_THRESHOLD = float(os.getenv("SIDEWAYS_THRESHOLD", "0.01"))  # 1%
SIDEWAYS_SECONDS = int(os.getenv("SIDEWAYS_SECONDS", "600"))         # 10 minutes
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "15"))         # cooldown after sell
SHORT_MA_MIN = int(os.getenv("SHORT_MA_MIN", "3"))   # minutes for short MA
LONG_MA_MIN = int(os.getenv("LONG_MA_MIN", "15"))   # minutes for long MA
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "100"))  # min volume filter
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.03")) # max allowed spread
MAX_CONCURRENT_POS = int(os.getenv("MAX_CONCURRENT_POS", "6"))
MAX_BUYS_PER_LOOP = int(os.getenv("MAX_BUYS_PER_LOOP", "2"))

# Sell-all on start? (True will liquidate non-USD balances at startup)
SELL_ALL_ON_START = os.getenv("SELL_ALL_ON_START", "true").lower() in ("1", "true", "yes")

# ---------------- Exchange setup ----------------
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")
if not API_KEY or not API_SECRET:
    raise SystemExit("Missing KRAKEN_API_KEY or KRAKEN_API_SECRET in environment variables")

exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
exchange.load_markets()

# ---------------- In-memory state ----------------
buy_price = {}       # base -> buy price USD (what the bot bought at)
buy_amount = {}      # base -> amount purchased
buy_timestamp = {}   # base -> datetime of buy
trailing_stop = {}   # base -> trailing stop price
cooldown_until = {}  # base -> datetime until which we won't buy it
reserve_usd = 0.0    # 30% tracked in memory (not transferred)
restricted_pairs = set()
trade_log = deque(maxlen=500)

# ---------------- Logging helpers ----------------
def now():
    return datetime.utcnow()

def log(msg):
    print(f"[{now().isoformat()}] {msg}", flush=True)

# ---------------- Exchange helpers ----------------
def safe_fetch_balance():
    try:
        return exchange.fetch_balance()
    except Exception as e:
        log(f"Balance fetch error: {e}")
        return {"total": {}}

def get_total_usd():
    bal = safe_fetch_balance().get("total", {})
    return float(bal.get("USD") or bal.get("ZUSD") or 0.0)

def get_base_balance(base):
    bal = safe_fetch_balance().get("total", {})
    return float(bal.get(base) or 0.0)

def mark_restricted(pair, err):
    s = str(err).lower()
    if "restricted" in s or "invalid permissions" in s or "not available" in s:
        restricted_pairs.add(pair)
        log(f"[RESTRICTED] Marked {pair} as restricted due to error: {err}")
        return True
    return False

def precision_for_amount(pair):
    m = exchange.markets.get(pair)
    if not m:
        return 8
    return m.get("precision", {}).get("amount", 8)

def market_min_amount(pair):
    m = exchange.markets.get(pair)
    if not m:
        return None
    return (m.get("limits", {}) or {}).get("amount", {}).get("min")

def quantize(a, prec):
    if prec is None:
        prec = 8
    f = 10 ** prec
    return math.floor(a * f) / f

# ---------------- Market data helpers ----------------
def fetch_price(pair):
    try:
        t = exchange.fetch_ticker(pair)
        return float(t.get("last") or t.get("info", {}).get("c", [None])[0])
    except Exception as e:
        mark_restricted(pair, e)
        return None

def fetch_ohlcv(pair, timeframe='1m', limit=60):
    try:
        return exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
    except Exception as e:
        mark_restricted(pair, e)
        return []

def recent_peak(pair, minutes=15):
    o = fetch_ohlcv(pair, '1m', limit=max(5, minutes))
    if not o:
        return None
    highs = [c[2] for c in o[-minutes:]] if len(o) >= minutes else [c[2] for c in o]
    return max(highs) if highs else None

def simple_ma(values):
    return sum(values) / len(values) if values else None

def momentum_ok(pair, short=3, long=15):
    o = fetch_ohlcv(pair, '1m', limit=max(long, short)+5)
    if not o or len(o) < long:
        return False
    closes = [c[4] for c in o]
    short_ma = sum(closes[-short:]) / short
    long_ma = sum(closes[-long:]) / long
    return short_ma > long_ma

def volume_ok(pair, min_vol=MIN_VOLUME_24H):
    try:
        t = exchange.fetch_ticker(pair)
        vol = t.get('baseVolume') or t.get('quoteVolume') or 0
        if vol is None:
            return True
        return float(vol) >= min_vol
    except Exception:
        return True

def spread_ok(pair, max_spread_pct=MAX_SPREAD_PCT):
    try:
        ob = exchange.fetch_order_book(pair, 5)
        bids = ob.get('bids') or []
        asks = ob.get('asks') or []
        if not bids or not asks:
            return False
        bid = bids[0][0]
        ask = asks[0][0]
        mid = (bid + ask) / 2
        return (ask - bid) / mid <= max_spread_pct
    except Exception:
        return True

# ---------------- Trading primitives ----------------
def market_sell(pair, amt):
    try:
        res = exchange.create_market_sell_order(pair, amt)
        log(f"[SELL] {pair} amt={amt} -> order={res.get('id') if isinstance(res, dict) else res}")
        return True
    except Exception as e:
        mark_restricted(pair, e)
        log(f"Sell failed {pair}: {e}")
        return False

def market_buy(pair, usd_amount):
    price = fetch_price(pair)
    if not price:
        return None
    raw_amt = usd_amount / price
    prec = precision_for_amount(pair)
    amt = quantize(raw_amt, prec)
    if amt <= 0:
        log(f"Buy skipped {pair}: amt 0 after precision")
        return None
    m = market_min_amount(pair)
    if m and amt < m:
        log(f"Buy skipped {pair}: amt {amt} < market min {m}")
        return None
    try:
        res = exchange.create_market_buy_order(pair, amt)
        log(f"[BUY] {pair} amt={amt} usd_alloc={usd_amount} -> order={res.get('id') if isinstance(res, dict) else res}")
        return price, amt
    except Exception as e:
        mark_restricted(pair, e)
        log(f"Buy failed {pair}: {e}")
        return None

# ---------------- Decision helpers ----------------
def net_pct_after_fees(buy_p, sell_p):
    gross = (sell_p / buy_p) - 1.0
    fee_cost = 2 * FEE_EST
    return gross - fee_cost

def pct_change(a, b):
    return (b / a) - 1.0

# ---------------- High level helpers ----------------
def sell_all_positions_on_start():
    bal = safe_fetch_balance().get('total', {})
    sold_any = False
    for asset, amt in list(bal.items()):
        if asset in ("USD", "ZUSD") or float(amt) <= 0:
            continue
        base = asset
        candidates = []
        if base.startswith('X') or base.startswith('Z'):
            candidates.append(base[1:] + '/USD')
        candidates.append(base + '/USD')
        for pair in candidates:
            if pair in exchange.markets:
                prec = precision_for_amount(pair)
                sell_amt = quantize(float(amt), prec)
                if sell_amt <= 0:
                    log(f"Start-sell skip {pair}: qty after precision 0")
                    continue
                ok = market_sell(pair, sell_amt)
                sold_any = sold_any or ok
                time.sleep(0.3)
                break
    return sold_any

def current_positions():
    bal = safe_fetch_balance().get('total', {})
    pos = []
    for pair in exchange.markets:
        if not pair.endswith('/USD'):
            continue
        base = pair.split('/')[0]
        amt = float(bal.get(base) or 0.0)
        if amt > 0:
            pos.append((pair, amt))
    return pos

def ensure_tradeable(target_usd):
    total_usd = get_total_usd()
    tradeable = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
    log(f"[ENSURE] tradeable ${tradeable:.2f} target ${target_usd:.2f}")
    if tradeable >= target_usd:
        return True
    # sell worst performing positions first
    bal = safe_fetch_balance().get('total', {})
    held = []
    for pair in exchange.markets:
        if not pair.endswith('/USD'):
            continue
        base = pair.split('/')[0]
        amt = float(bal.get(base) or 0.0)
        if amt <= 0:
            continue
        price = fetch_price(pair)
        bought = buy_price.get(base)
        if price is None:
            continue
        unreal = pct_change(bought or price, price)
        held.append((unreal, pair, base, amt, price, bought))
    held.sort(key=lambda x: x[0])  # worst (most negative) first
    for unreal, pair, base, amt, price, bought in held:
        if tradeable >= target_usd:
            break
        prec = precision_for_amount(pair)
        sell_qty = quantize(amt, prec)
        if sell_qty <= 0:
            continue
        log(f"[ENSURE] Selling {base} (unreal={unreal*100:.2f}%) to free funds")
        if market_sell(pair, sell_qty):
            total_usd = get_total_usd()
            tradeable = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
        time.sleep(0.3)
    total_usd = get_total_usd()
    tradeable = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
    return tradeable >= target_usd

# ---------------- Core loop ----------------
def main_loop():
    global reserve_usd
    log("Bot starting — configuration:")
    log(f"CYCLE_SECONDS={CYCLE_SECONDS} DIP_PCT={DIP_PCT*100:.2f}% TAKE_PROFIT_NET_PCT={TAKE_PROFIT_NET_PCT*100:.2f}% STOP_LOSS_PCT={STOP_LOSS_PCT*100:.2f}%")

    if SELL_ALL_ON_START:
        log("SELL_ALL_ON_START enabled — attempting to liquidate non-USD holdings...")
        sell_all_positions_on_start()
        time.sleep(2)

    while True:
        loop_start = time.time()
        try:
            # expire cooldowns
            for b in list(cooldown_until.keys()):
                if cooldown_until[b] <= now():
                    del cooldown_until[b]

            total_usd = get_total_usd()
            tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
            log(f"[POOL] Total USD ${total_usd:.2f} | Tradeable ${tradeable_pool:.2f} | Reserve(mem) ${reserve_usd:.2f}")

            # Build candidate list: all /USD markets filtered
            candidates = []
            for pair, m in exchange.markets.items():
                if not pair.endswith('/USD'):
                    continue
                if pair in restricted_pairs:
                    continue
                candidates.append(pair)

            # SELL PHASE: check holdings for TP, stoploss, sideways
            pos = current_positions()
            for pair, amt in pos:
                base = pair.split('/')[0]
                price = fetch_price(pair)
                if price is None:
                    continue
                bought = buy_price.get(base)
                held_since = buy_timestamp.get(base)
                net_pct = None
                net_usd = None
                if bought:
                    net_pct = net_pct_after_fees(bought, price)
                    # approximate profit usd = (price - bought) * amt - fees
                    gross_profit = (price - bought) * amt
                    fees = (price * amt + bought * amt) * FEE_EST
                    net_usd = gross_profit - fees
                # stop-loss immediate
                if bought and pct_change(bought, price) <= STOP_LOSS_PCT:
                    log(f"[SELL-STOP] {base} bought={bought:.8f} now={price:.8f} pct={pct_change(bought,price)*100:.2f}% -> SELL")
                    prec = precision_for_amount(pair)
                    sell_qty = quantize(amt, prec)
                    if sell_qty > 0 and market_sell(pair, sell_qty):
                        if net_usd and net_usd > 0:
                            add = net_usd * RESERVE_FRAC
                            reserve_usd += add
                            log(f"[RESERVE] +${add:.4f}")
                        cooldown_until[base] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                    continue
                # trailing / take profit
                if bought and net_pct is not None and net_pct >= TAKE_PROFIT_NET_PCT:
                    log(f"[SELL-TP] {base} bought={bought:.8f} now={price:.8f} net%={net_pct*100:.2f}% -> SELL")
                    prec = precision_for_amount(pair)
                    sell_qty = quantize(amt, prec)
                    if sell_qty > 0 and market_sell(pair, sell_qty):
                        if net_usd and net_usd > 0:
                            add = net_usd * RESERVE_FRAC
                            reserve_usd += add
                            log(f"[RESERVE] +${add:.4f}")
                        cooldown_until[base] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                    continue
                # sideways recycle: only if held long and no momentum and sideways
                if bought and held_since and (now() - held_since).total_seconds() >= SIDEWAYS_SECONDS:
                    if abs(pct_change(bought, price)) <= SIDEWAYS_THRESHOLD:
                        # only sell if momentum is not positive
                        if not momentum_ok(pair, short=SHORT_MA_MIN, long=LONG_MA_MIN):
                            log(f"[SELL-SIDEWAYS] {base} held long & sideways -> SELL")
                            prec = precision_for_amount(pair)
                            sell_qty = quantize(amt, prec)
                            if sell_qty > 0 and market_sell(pair, sell_qty):
                                cooldown_until[base] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                            continue

            # refresh tradeable pool
            total_usd = get_total_usd()
            tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
            log(f"[AFTER SELL] Total USD ${total_usd:.2f} | Tradeable ${tradeable_pool:.2f} | Reserve(mem) ${reserve_usd:.2f}")

            # If insufficient tradeable funds, attempt to free some
            if tradeable_pool < MIN_TRADE_USD:
                ok = ensure_tradeable(MIN_TRADE_USD * 2)
                total_usd = get_total_usd()
                tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
                if not ok:
                    log("[WAIT] Could not free additional funds; skipping buys this loop")

            # BUY PHASE
            buys = 0
            current_pos_count = sum(1 for b in buy_price.keys() if get_base_balance(b) > 0)
            free_slots = max(0, MAX_CONCURRENT_POS - current_pos_count)
            if free_slots <= 0:
                log("[BUY] Max concurrent positions reached; skipping buys")
            else:
                per_buy = max(MIN_TRADE_USD, tradeable_pool / max(1, free_slots))
                # iterate candidate list, apply dip+momentum+liquidity+spread+cooldown filters
                for pair in candidates:
                    if buys >= MAX_BUYS_PER_LOOP:
                        break
                    price = fetch_price(pair)
                    if price is None:
                        continue
                    base = pair.split('/')[0]
                    # skip if currently held or in cooldown or restricted
                    if get_base_balance(base) > 0:
                        continue
                    if cooldown_until.get(base):
                        continue
                    # liquidity / spread checks
                    if not volume_ok(pair):
                        continue
                    if not spread_ok(pair):
                        continue
                    # dip check: current price must be at least DIP_PCT below recent peak
                    peak = recent_peak(pair, minutes=15)
                    if peak is None:
                        continue
                    dip = (peak - price) / peak
                    if dip < DIP_PCT:
                        # skip if not sufficient dip
                        continue
                    # momentum check: short MA > long MA
                    if not momentum_ok(pair, short=SHORT_MA_MIN, long=LONG_MA_MIN):
                        continue
                    # ready to buy: compute allocation
                    usd_alloc = min(per_buy, tradeable_pool)
                    if usd_alloc < MIN_TRADE_USD:
                        break
                    res = market_buy(pair, usd_alloc)
                    if res:
                        p_buy, qty = res
                        buy_price[base] = p_buy
                        buy_amount[base] = qty
                        buy_timestamp[base] = now()
                        # initialize trailing stop: set to break-even minus small margin or at STOP_LOSS
                        trailing_stop[base] = p_buy * (1 + 0.005)  # small initial buffer; will trail with gains
                        trade_log.append((now().isoformat(), 'BUY', base, p_buy, qty, usd_alloc))
                        buys += 1
                        tradeable_pool -= usd_alloc
                        time.sleep(0.5)

            # housekeeping and sleep
        except Exception as e:
            log(f"[ERROR] Main loop exception: {e}")

        elapsed = time.time() - loop_start
        to_sleep = max(1, CYCLE_SECONDS - elapsed)
        time.sleep(to_sleep)

if __name__ == "__main__":
    main_loop()
