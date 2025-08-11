# main.py
# Kraken trading bot (ccxt)
# - Ensures minimum order sizes are respected
# - Sizes buys so sold amounts meet Kraken's minimums
# - Attempts to sell "dust" only when it's >= market minimum
# - 70% invest / 30% reserve split
# - Buy on dips + momentum, sell on net take-profit or stop-loss or reversal
#
# WARNING: This places real orders. Test with tiny balances first.

import os
import time
import math
import ccxt
from datetime import datetime, timedelta
from collections import deque, defaultdict

# ---------------- CONFIG (override by Heroku env vars) ----------------
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "30"))      # loop interval
DIP_PCT = float(os.getenv("DIP_PCT", "0.04"))              # 4% dip required
SHORT_MA_MIN = int(os.getenv("SHORT_MA_MIN", "3"))
LONG_MA_MIN = int(os.getenv("LONG_MA_MIN", "15"))
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "1.0"))
TRADEABLE_FRAC = float(os.getenv("TRADEABLE_FRAC", "0.70"))  # 70% invest
RESERVE_FRAC = float(os.getenv("RESERVE_FRAC", "0.30"))      # 30% reserved
FEE_EST = float(os.getenv("FEE_EST", "0.0026"))              # estimated taker fee
TAKE_PROFIT_NET_PCT = float(os.getenv("TAKE_PROFIT_NET_PCT", "0.04"))  # 4% net
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-0.04"))   # -4% stop loss
REVERSAL_DROP_PCT = float(os.getenv("REVERSAL_DROP_PCT", "0.015")) # 1.5% drop for reversal
SIDEWAYS_SECONDS = int(os.getenv("SIDEWAYS_SECONDS", "600")) # 10 minutes
SIDEWAYS_THRESHOLD = float(os.getenv("SIDEWAYS_THRESHOLD", "0.01")) # ±1%
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "15"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "100"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.03"))
MAX_BUYS_PER_LOOP = int(os.getenv("MAX_BUYS_PER_LOOP", "2"))
MAX_CONCURRENT_POS = int(os.getenv("MAX_CONCURRENT_POS", "6"))
SELL_ALL_ON_START = os.getenv("SELL_ALL_ON_START", "true").lower() in ("1","true","yes")

# ---------------- Exchange init ----------------
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")
if not API_KEY or not API_SECRET:
    raise SystemExit("Set KRAKEN_API_KEY and KRAKEN_API_SECRET in environment variables")

exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
exchange.load_markets()

# ---------------- In-memory state ----------------
buy_price = {}          # base -> price paid (USD)
buy_qty = {}            # base -> qty bought
buy_time = {}           # base -> datetime of buy
reserve_usd = 0.0       # reserved portion (30%) tracked in memory
cooldown_until = {}     # base -> datetime until which we won't buy it
restricted = set()      # pairs flagged restricted by Kraken
trade_log = deque(maxlen=1000)

# ---------------- Helpers ----------------
def now(): return datetime.utcnow()
def log(msg): print(f"[{now().isoformat()}] {msg}", flush=True)

def safe_fetch_balance():
    try:
        return exchange.fetch_balance()
    except Exception as e:
        log(f"[ERROR] fetch_balance: {e}")
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
        restricted.add(pair)
        log(f"[RESTRICTED] {pair} flagged: {err}")
        return True
    return False

def precision_amount(pair):
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
    factor = 10 ** prec
    return math.floor(a * factor) / factor

# ---------------- Market data ----------------
def fetch_price(pair):
    try:
        t = exchange.fetch_ticker(pair)
        return float(t.get("last") or t.get("close") or (t.get("info", {}).get("c") or [None])[0])
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
    o = fetch_ohlcv(pair, '1m', limit=max(minutes,5))
    if not o:
        return None
    highs = [c[2] for c in o[-minutes:]] if len(o) >= minutes else [c[2] for c in o]
    return max(highs) if highs else None

def last_n_closes(pair, n=3):
    o = fetch_ohlcv(pair, '1m', limit=n+2)
    if not o or len(o) < n:
        return []
    return [c[4] for c in o[-n:]]

def short_long_momentum(pair, short=3, long=15):
    o = fetch_ohlcv(pair, '1m', limit=max(short,long)+5)
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

def spread_ok(pair, max_spread=MAX_SPREAD_PCT):
    try:
        ob = exchange.fetch_order_book(pair, 5)
        bids = ob.get('bids') or []
        asks = ob.get('asks') or []
        if not bids or not asks:
            return False
        bid = bids[0][0]
        ask = asks[0][0]
        mid = (bid + ask) / 2.0
        return (ask - bid) / mid <= max_spread
    except Exception:
        return True

# ---------------- Trading primitives ----------------
def market_sell(pair, qty):
    try:
        res = exchange.create_market_sell_order(pair, qty)
        log(f"[SELL EXECUTED] {pair} qty={qty} -> {res.get('id') if isinstance(res, dict) else res}")
        return True
    except Exception as e:
        mark_restricted(pair, e)
        log(f"[SELL FAILED] {pair}: {e}")
        return False

def market_buy(pair, usd_amount):
    price = fetch_price(pair)
    if not price:
        return None
    raw_qty = usd_amount / price
    prec = precision_amount(pair)
    qty = quantize(raw_qty, prec)
    if qty <= 0:
        log(f"[BUY SKIP] {pair}: qty 0 after precision")
        return None
    m = market_min_amount(pair)
    if m and qty < m:
        # qty below min — compute minimal USD needed to reach min and skip buy (caller can consider larger allocation)
        required_usd = m * price
        log(f"[BUY SKIP] {pair}: qty {qty} < min {m} (need ${required_usd:.4f} to meet min)")
        return None
    try:
        res = exchange.create_market_buy_order(pair, qty)
        log(f"[BUY EXECUTED] {pair} qty={qty} usd_alloc={usd_amount} -> {res.get('id') if isinstance(res, dict) else res}")
        return price, qty
    except Exception as e:
        mark_restricted(pair, e)
        log(f"[BUY FAILED] {pair}: {e}")
        return None

# ---------------- Utilities for ensuring tradable amounts ----------------
def sell_all_positions_on_start():
    bal = safe_fetch_balance().get("total", {})
    sold_any = False
    for asset, amt in list(bal.items()):
        if asset in ("USD", "ZUSD") or float(amt) <= 0:
            continue
        base = asset
        candidates = []
        if base.startswith("X") or base.startswith("Z"):
            candidates.append(base[1:] + "/USD")
        candidates.append(base + "/USD")
        for pair in candidates:
            if pair in exchange.markets:
                prec = precision_amount(pair)
                sell_qty = quantize(float(amt), prec)
                if sell_qty <= 0:
                    log(f"[START SKIP] {pair}: qty after precision 0")
                    continue
                ok = market_sell(pair, sell_qty)
                sold_any = sold_any or ok
                time.sleep(0.25)
                break
    return sold_any

def ensure_buy_will_meet_min(pair, usd_alloc):
    """
    Ensure that buying with usd_alloc will produce qty >= market min.
    If not, return the minimum USD required to meet the min (so caller may increase allocation).
    """
    price = fetch_price(pair)
    if price is None:
        return None
    m = market_min_amount(pair)
    if not m:
        return usd_alloc  # no min declared
    required_usd = m * price
    if usd_alloc >= required_usd:
        return usd_alloc
    else:
        return required_usd

def attempt_sell_dust(pair, base, qty):
    """
    Attempt to sell very small balances (dust) if they meet market min.
    Otherwise, log and leave them.
    """
    m = market_min_amount(pair)
    if m and qty < m:
        log(f"[DUST] {base} qty {qty} < market min {m} — will not sell automatically")
        return False
    # try to sell
    prec = precision_amount(pair)
    sell_qty = quantize(qty, prec)
    if sell_qty <= 0:
        log(f"[DUST] {pair} after quantize qty 0")
        return False
    return market_sell(pair, sell_qty)

# ---------------- Decision helpers ----------------
def net_pct_after_fees(buy_p, sell_p):
    gross = (sell_p / buy_p) - 1.0
    return gross - (2 * FEE_EST)

def pct_change(a, b):
    return (b / a) - 1.0

# ---------------- High-level helpers ----------------
def ensure_tradeable(target_usd):
    """
    If tradeable pool < target_usd, sell worst performers until we have enough.
    This respects market minima.
    """
    total = get_total_usd()
    tradeable = max(0.0, total * TRADEABLE_FRAC - reserve_usd)
    log(f"[ENSURE] tradeable ${tradeable:.2f} target ${target_usd:.2f}")
    if tradeable >= target_usd:
        return True
    # build list of held positions (pair, amt, unreal_pct)
    bal = safe_fetch_balance().get("total", {})
    held = []
    for pair in exchange.markets:
        if not pair.endswith("/USD"):
            continue
        base = pair.split("/")[0]
        amt = float(bal.get(base) or 0.0)
        if amt <= 0:
            continue
        price = fetch_price(pair)
        bprice = buy_price.get(base)
        if price is None or bprice is None:
            continue
        unreal = pct_change(bprice, price)
        held.append((unreal, pair, base, amt, price, bprice))
    # sell worst performers first
    held.sort(key=lambda x: x[0])
    for unreal, pair, base, amt, price, bprice in held:
        if tradeable >= target_usd:
            break
        prec = precision_amount(pair)
        sell_qty = quantize(amt, prec)
        if sell_qty <= 0:
            continue
        log(f"[ENSURE-SELL] Selling {base} (unreal={unreal*100:.2f}%) to free funds")
        if market_sell(pair, sell_qty):
            total = get_total_usd()
            tradeable = max(0.0, total * TRADEABLE_FRAC - reserve_usd)
        time.sleep(0.3)
    total = get_total_usd()
    tradeable = max(0.0, total * TRADEABLE_FRAC - reserve_usd)
    return tradeable >= target_usd

# ---------------- Core loop ----------------
def main_loop():
    global reserve_usd
    log("Bot starting (will auto-sell on start if configured).")
    if SELL_ALL_ON_START:
        log("SELL_ALL_ON_START enabled — liquidating non-USD holdings...")
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

            # Build candidate list: all /USD markets excluding stablecoins
            candidates = []
            for pair, m in exchange.markets.items():
                if not pair.endswith("/USD"):
                    continue
                base = pair.split("/")[0].upper()
                # skip obvious stables / fiat
                if base in ("USDT","USDC","USD","ZUSD"):
                    continue
                if pair in restricted:
                    continue
                candidates.append(pair)

            # SELL PHASE: evaluate holdings for TP, stoploss, reversal, sideways
            bal = safe_fetch_balance().get("total", {})
            held_pairs = []
            for pair in candidates:
                base = pair.split("/")[0]
                amt = float(bal.get(base) or 0.0)
                if amt > 0:
                    held_pairs.append((pair, amt))
            for pair, amt in held_pairs:
                base = pair.split("/")[0]
                price = fetch_price(pair)
                if price is None:
                    continue
                bprice = buy_price.get(base)
                net_usd = None
                if bprice:
                    gross = (price - bprice) * amt
                    fees = (price * amt + bprice * amt) * FEE_EST
                    net_usd = gross - fees
                    net_pct = net_pct_after = net_pct_after_fees(bprice, price)
                else:
                    net_pct_after = None

                # immediate stop loss
                if bprice and pct_change(bprice, price) <= STOP_LOSS_PCT:
                    log(f"[SELL-STOPLOSS] {base} buy={bprice:.8f} now={price:.8f} pct={pct_change(bprice,price)*100:.2f}% -> SELL")
                    prec = precision_amount(pair)
                    sell_qty = quantize(amt, prec)
                    if sell_qty > 0 and market_sell(pair, sell_qty):
                        if net_usd and net_usd > 0:
                            add = net_usd * RESERVE_FRAC
                            reserve_usd += add
                            log(f"[RESERVE] +${add:.4f}")
                        cooldown_until[base] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                    continue

                # take profit check (net >= target)
                if bprice and net_pct_after is not None and net_pct_after >= TAKE_PROFIT_NET_PCT:
                    log(f"[SELL-TP] {base} buy={bprice:.8f} now={price:.8f} net_pct={net_pct_after*100:.2f}% -> SELL")
                    prec = precision_amount(pair)
                    sell_qty = quantize(amt, prec)
                    if sell_qty > 0 and market_sell(pair, sell_qty):
                        if net_usd and net_usd > 0:
                            add = net_usd * RESERVE_FRAC
                            reserve_usd += add
                            log(f"[RESERVE] +${add:.4f}")
                        cooldown_until[base] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                    continue

                # reversal-based sell: last 2 candles red + drop from recent peak >= REVERSAL_DROP_PCT
                closes = last_n_closes(pair, 3)
                if len(closes) >= 3 and (closes[-1] < closes[-2] < closes[-3]):
                    peak = recent_peak(pair, minutes=30)
                    if peak and (peak - price) / peak >= REVERSAL_DROP_PCT:
                        log(f"[SELL-REV] {base} reversal detected -> SELL (peak={peak:.8f} now={price:.8f})")
                        prec = precision_amount(pair)
                        sell_qty = quantize(amt, prec)
                        if sell_qty > 0 and market_sell(pair, sell_qty):
                            cooldown_until[base] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                        continue

                # sideways recycle
                if bprice and buy_time.get(base) and (now() - buy_time[base]).total_seconds() >= SIDEWAYS_SECONDS:
                    if abs(pct_change(bprice, price)) <= SIDEWAYS_THRESHOLD and not short_long_momentum(pair, short=SHORT_MA_MIN, long=LONG_MA_MIN):
                        log(f"[SELL-SIDEWAYS] {base} held long and sideways -> SELL")
                        prec = precision_amount(pair)
                        sell_qty = quantize(amt, prec)
                        if sell_qty > 0 and market_sell(pair, sell_qty):
                            cooldown_until[base] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                        continue

                # if qty < market minimum, attempt to sell dust if meets min, else log
                m = market_min_amount(pair)
                if m and amt < m:
                    log(f"[DUST] {base} qty {amt} < market min {m} -> attempting dust sell if possible")
                    attempt_sell_dust(pair, base, amt)
                    # continue (we won't try to sell for TP/SL since amount was tiny)

            # refresh pool after sells
            total_usd = get_total_usd()
            tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
            log(f"[AFTER SELL] Total USD ${total_usd:.2f} | Tradeable ${tradeable_pool:.2f} | Reserve(mem) ${reserve_usd:.2f}")

            # ensure tradeable funds
            if tradeable_pool < MIN_TRADE_USD:
                ok = ensure_tradeable(MIN_TRADE_USD * 2)
                total_usd = get_total_usd()
                tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
                if not ok:
                    log("[WAIT] cannot free sufficient funds; skipping buys this loop")

            # BUY PHASE
            buys = 0
            current_positions = sum(1 for b in buy_price.keys() if get_base_balance(b) > 0)
            free_slots = max(0, MAX_CONCURRENT_POS - current_positions)
            if free_slots <= 0:
                log("[BUY] max concurrent positions reached; skipping buys")
            else:
                per_buy = max(MIN_TRADE_USD, tradeable_pool / max(1, free_slots))
                # iterate candidates
                for pair in candidates:
                    if buys >= MAX_BUYS_PER_LOOP:
                        break
                    if tradeable_pool < MIN_TRADE_USD:
                        break
                    price = fetch_price(pair)
                    if price is None:
                        continue
                    base = pair.split("/")[0]
                    # skip if already held or in cooldown
                    if get_base_balance(base) > 0:
                        continue
                    if cooldown_until.get(base) and cooldown_until[base] > now():
                        continue
                    # liquidity/spread checks
                    if not volume_ok(pair):
                        continue
                    if not spread_ok(pair):
                        continue
                    # dip check
                    peak = recent_peak(pair, minutes=15)
                    if not peak:
                        continue
                    dip = (peak - price) / peak
                    if dip < DIP_PCT:
                        continue
                    # momentum: last 2 candles green + short > long MA
                    closes = last_n_closes(pair, 3)
                    if len(closes) < 3:
                        continue
                    if not (closes[-1] > closes[-2] and closes[-2] > closes[-3]):
                        continue
                    if not short_long_momentum(pair, short=SHORT_MA_MIN, long=LONG_MA_MIN):
                        continue
                    # ensure allocation will meet market min
                    m = market_min_amount(pair)
                    usd_needed = per_buy
                    if m:
                        needed = m * price
                        if usd_needed < needed:
                            # raise allocation if tradeable_pool allows
                            if tradeable_pool >= needed:
                                usd_needed = needed
                            else:
                                log(f"[BUY SKIP] {pair}: per_buy ${per_buy:.2f} < required ${needed:.2f} to meet min. pool ${tradeable_pool:.2f}")
                                continue
                    # do buy
                    res = market_buy(pair, usd_needed)
                    if res:
                        p_buy, qty = res
                        buy_price[base] = p_buy
                        buy_qty[base] = qty
                        buy_time[base] = now()
                        trade_log.append((now().isoformat(), 'BUY', base, p_buy, qty, usd_needed))
                        buys += 1
                        tradeable_pool -= usd_needed
                        time.sleep(0.5)

        except Exception as e:
            log(f"[ERROR] main loop exception: {e}")

        elapsed = time.time() - loop_start
        to_sleep = max(1, CYCLE_SECONDS - elapsed)
        time.sleep(to_sleep)

if __name__ == "__main__":
    main_loop()
