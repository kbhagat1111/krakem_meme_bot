# Main.py
# Kraken scalper (ccxt)
# - Ensures buys are sized so post-buy qty >= market minimum (so dust doesn't block sells)
# - Persists in-memory buys + stats to JSON files under /app/data
# - Combines dust into future buys by taking current base balance into account
# - 70% reinvest / 30% reserve split
# - Forever loop
#
# WARNING: This places real market orders. Test with tiny balances first.

import os, time, json, math, traceback
import ccxt
from datetime import datetime, timedelta, timezone
from collections import deque

# ---------- Config ----------
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "30"))
DIP_PCT = float(os.getenv("DIP_PCT", "0.04"))               # buy when price >=4% below recent peak
SHORT_MA_MIN = int(os.getenv("SHORT_MA_MIN", "3"))
LONG_MA_MIN = int(os.getenv("LONG_MA_MIN", "15"))
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "1.0"))
TRADEABLE_FRAC = float(os.getenv("TRADEABLE_FRAC", "0.70"))  # 70% usable
RESERVE_FRAC = float(os.getenv("RESERVE_FRAC", "0.30"))      # 30% reserve from profits
FEE_EST = float(os.getenv("FEE_EST", "0.0026"))              # estimate taker fee (0.26%)
TAKE_PROFIT_NET_PCT = float(os.getenv("TAKE_PROFIT_NET_PCT", "0.04"))  # 4% net
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-0.04"))   # -4% stop loss
REVERSAL_DROP_PCT = float(os.getenv("REVERSAL_DROP_PCT", "0.015"))
SIDEWAYS_SECONDS = int(os.getenv("SIDEWAYS_SECONDS", "600"))
SIDEWAYS_THRESHOLD = float(os.getenv("SIDEWAYS_THRESHOLD", "0.01"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "15"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "100"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.03"))
MAX_BUYS_PER_LOOP = int(os.getenv("MAX_BUYS_PER_LOOP", "2"))
MAX_CONCURRENT_POS = int(os.getenv("MAX_CONCURRENT_POS", "6"))
SELL_ALL_ON_START = os.getenv("SELL_ALL_ON_START", "true").lower() in ("1","true","yes")

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
BUYS_FILE = os.path.join(DATA_DIR, "buys.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
DAILY_LOG = os.path.join(DATA_DIR, "daily_log.txt")

# ---------- Exchange ----------
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")
if not API_KEY or not API_SECRET:
    raise SystemExit("Missing KRAKEN_API_KEY or KRAKEN_API_SECRET in env vars")

exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
exchange.load_markets()

# ---------- Persistence helpers ----------
def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        print(f"[WARN] load {path} failed, using default", flush=True)
    return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)

# persistent structures
# buys: base -> { pair, price, qty, time_iso, cooldown_until_iso }
buys = load_json(BUYS_FILE, {})
# stats
stats = load_json(STATS_FILE, {
    "lifetime_take_profit_usd": 0.0,
    "lifetime_dust_recovered_usd": 0.0,
    "reserve_usd": 0.0,
    "last_daily_summary": None
})

trade_log = deque(maxlen=1000)

def now(): return datetime.utcnow().replace(tzinfo=timezone.utc)
def log(s): print(f"[{now().isoformat()}] {s}", flush=True)

# ---------- Market helpers ----------
def safe_fetch_balance():
    try:
        return exchange.fetch_balance()
    except Exception as e:
        log(f"[ERROR] fetch_balance: {e}")
        return {"total": {}}

def get_total_usd():
    b = safe_fetch_balance().get("total", {})
    return float(b.get("USD") or b.get("ZUSD") or 0.0)

def get_base_balance(base):
    b = safe_fetch_balance().get("total", {})
    return float(b.get(base) or 0.0)

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

def quantize_amount(amount, prec):
    if prec is None:
        prec = 8
    f = 10 ** prec
    return math.floor(amount * f) / f

def fetch_ticker(pair):
    try:
        return exchange.fetch_ticker(pair)
    except Exception as e:
        # minimal logging
        return None

def fetch_price(pair):
    t = fetch_ticker(pair)
    if not t:
        return None
    return float(t.get("last") or t.get("close"))

def fetch_ohlcv(pair, timeframe='1m', limit=60):
    try:
        return exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
    except Exception:
        return []

def recent_peak(pair, minutes=15):
    o = fetch_ohlcv(pair, '1m', limit=max(5, minutes))
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
    t = fetch_ticker(pair)
    if not t:
        return True
    vol = t.get('baseVolume') or t.get('quoteVolume') or 0
    try:
        return float(vol) >= min_vol
    except Exception:
        return True

# ---------- Trading primitives ----------
def market_sell(pair, qty):
    try:
        res = exchange.create_market_sell_order(pair, qty)
        log(f"[SELL EXECUTED] {pair} qty={qty}")
        return {"ok": True, "res": res}
    except Exception as e:
        s = str(e).lower()
        if "volume" in s and "minimum" in s or "minimum" in s:
            log(f"[SELL FAILED-MIN] {pair}: {e}")
            return {"ok": False, "error": "min_volume", "raw": str(e)}
        log(f"[SELL FAILED] {pair}: {e}")
        return {"ok": False, "error": str(e)}

def market_buy(pair, usd_amount):
    price = fetch_price(pair)
    if not price:
        return None
    raw_qty = usd_amount / price
    prec = precision_amount(pair)
    qty = quantize_amount(raw_qty, prec)
    if qty <= 0:
        log(f"[BUY SKIP] {pair}: qty 0 after precision")
        return None
    m = market_min_amount(pair)
    if m and qty < m:
        required_usd = m * price
        log(f"[BUY SKIP] {pair}: qty {qty} < min {m} -> need ${required_usd:.4f}")
        return None
    try:
        res = exchange.create_market_buy_order(pair, qty)
        log(f"[BUY EXECUTED] {pair} qty={qty} usd_alloc={usd_amount:.2f}")
        return {"price": price, "qty": qty, "res": res}
    except Exception as e:
        log(f"[BUY FAILED] {pair}: {e}")
        return None

# ---------- Stats helpers ----------
def record_take_profit(net_usd):
    stats["lifetime_take_profit_usd"] = stats.get("lifetime_take_profit_usd", 0.0) + net_usd
    reinvest = net_usd * 0.70
    reserve_add = net_usd * 0.30
    stats["reserve_usd"] = stats.get("reserve_usd", 0.0) + reserve_add
    save_json(STATS_FILE, stats)
    return reinvest, reserve_add

def record_dust(amount_usd):
    stats["lifetime_dust_recovered_usd"] = stats.get("lifetime_dust_recovered_usd", 0.0) + amount_usd
    save_json(STATS_FILE, stats)

def maybe_write_daily_summary():
    today = now().astimezone(timezone.utc).strftime("%Y-%m-%d")
    if stats.get("last_daily_summary") == today:
        return
    summary = (
        f"[{today} UTC] DAILY SUMMARY\n"
        f"Take-profit total: ${stats.get('lifetime_take_profit_usd',0.0):.2f}\n"
        f"Dust recovered: ${stats.get('lifetime_dust_recovered_usd',0.0):.2f}\n"
        f"Reserve (mem): ${stats.get('reserve_usd',0.0):.2f}\n"
        "----\n"
    )
    with open(DAILY_LOG, "a") as f:
        f.write(summary)
    stats["last_daily_summary"] = today
    save_json(STATS_FILE, stats)
    log(f"[DAILY SUMMARY WRITTEN] {today}")

# ---------- Ensure funds (sell worst) ----------
def ensure_tradeable(target_usd):
    total = get_total_usd()
    tradeable = max(0.0, total * TRADEABLE_FRAC - stats.get("reserve_usd", 0.0))
    log(f"[ENSURE] tradeable ${tradeable:.2f} target ${target_usd:.2f}")
    if tradeable >= target_usd:
        return True
    balances = safe_fetch_balance().get("total", {})
    held = []
    for pair in exchange.markets:
        if not pair.endswith('/USD'):
            continue
        base = pair.split('/')[0]
        amt = float(balances.get(base) or 0.0)
        if amt <= 0:
            continue
        price = fetch_price(pair)
        if not price:
            continue
        held.append((amt * price, pair, base, amt, price))
    # sell largest USD value first to free funds
    held.sort(reverse=True)
    for _, pair, base, amt, price in held:
        if tradeable >= target_usd:
            break
        prec = precision_amount(pair)
        sell_qty = quantize_amount(amt, prec)
        if sell_qty <= 0:
            continue
        res = market_sell(pair, sell_qty)
        if res.get("error") == "min_volume":
            log(f"[ENSURE] {pair} unsellable (min vol). skipping")
            continue
        time.sleep(0.3)
        total = get_total_usd()
        tradeable = max(0.0, total * TRADEABLE_FRAC - stats.get("reserve_usd", 0.0))
    total = get_total_usd()
    tradeable = max(0.0, total * TRADEABLE_FRAC - stats.get("reserve_usd", 0.0))
    return tradeable >= target_usd

# ---------- Startup sell (optional) ----------
if SELL_ALL_ON_START:
    log("SELL_ALL_ON_START enabled - attempting to liquidate non-USD holdings...")
    balances = safe_fetch_balance().get("total", {})
    for asset, amt in list(balances.items()):
        if asset in ("USD", "ZUSD") or float(amt) <= 0:
            continue
        base = asset
        candidates = []
        if base.startswith('X') or base.startswith('Z'):
            candidates.append(base[1:] + '/USD')
        candidates.append(base + '/USD')
        for pair in candidates:
            if pair in exchange.markets:
                prec = precision_amount(pair)
                sell_qty = quantize_amount(float(amt), prec)
                if sell_qty <= 0:
                    log(f"[START SKIP] {pair}: qty after precision 0")
                    continue
                res = market_sell(pair, sell_qty)
                if res.get("error") == "min_volume":
                    log(f"[START] {pair} min-volume unsellable; left as dust")
                time.sleep(0.3)
                break

# ---------- Main loop ----------
last_summary_time = time.time()
while True:
    loop_start = time.time()
    try:
        total_usd = get_total_usd()
        tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - stats.get("reserve_usd", 0.0))
        log(f"[POOL] Total USD ${total_usd:.2f} | Tradeable ${tradeable_pool:.2f} | Reserve(mem) ${stats.get('reserve_usd',0.0):.2f}")

        # candidate list
        candidates = []
        for pair, meta in exchange.markets.items():
            if not pair.endswith('/USD'):
                continue
            base = pair.split('/')[0].upper()
            if base in ("USDT","USDC","USD","ZUSD"):
                continue
            candidates.append(pair)

        # SELL PHASE: check holdings
        balances = safe_fetch_balance().get("total", {})
        held_pairs = []
        for pair in candidates:
            base = pair.split('/')[0]
            amt = float(balances.get(base) or 0.0)
            if amt > 0:
                held_pairs.append((pair, amt))
        for pair, amt in held_pairs:
            base = pair.split('/')[0]
            price = fetch_price(pair)
            if price is None:
                continue
            rec = buys.get(base)
            buy_price = rec.get("price") if rec else None
            net_usd = None
            if buy_price:
                gross = (price - buy_price) * amt
                fees = (price * amt + buy_price * amt) * FEE_EST
                net_usd = gross - fees
                net_pct = (price / buy_price) - 1.0 - 2*FEE_EST
            else:
                net_pct = None

            # stop-loss
            if buy_price and (price / buy_price - 1.0) <= STOP_LOSS_PCT:
                log(f"[SELL-STOPLOSS] {base} buy={buy_price:.8f} now={price:.8f} -> SELL")
                prec = precision_amount(pair)
                sell_qty = quantize_amount(amt, prec)
                if sell_qty > 0:
                    res = market_sell(pair, sell_qty)
                    if res.get("error") == "min_volume":
                        log(f"[DUST] {pair} unsellable at min volume")
                    else:
                        if net_usd and net_usd > 0:
                            reinvest, reserve_add = record_take_profit(net_usd)
                            log(f"[RESERVE] +${reserve_add:.4f} reinvest ${reinvest:.4f}")
                        if base in buys:
                            del buys[base]
                            save_json(BUYS_FILE, buys)
                        time.sleep(0.3)
                continue

            # take-profit net
            if buy_price and net_pct is not None and net_pct >= TAKE_PROFIT_NET_PCT:
                log(f"[SELL-TP] {base} buy={buy_price:.8f} now={price:.8f} net%={(net_pct)*100:.2f}% -> SELL")
                prec = precision_amount(pair)
                sell_qty = quantize_amount(amt, prec)
                if sell_qty > 0:
                    res = market_sell(pair, sell_qty)
                    if res.get("error") == "min_volume":
                        log(f"[DUST] {pair} unsellable at min volume after TP")
                    else:
                        if net_usd and net_usd > 0:
                            reinvest, reserve_add = record_take_profit(net_usd)
                            log(f"[RESERVE] +${reserve_add:.4f} reinvest ${reinvest:.4f}")
                        if base in buys:
                            del buys[base]
                            save_json(BUYS_FILE, buys)
                        time.sleep(0.3)
                continue

            # reversal sell
            closes = last_n_closes(pair, 3)
            if len(closes) >= 3 and (closes[-1] < closes[-2] < closes[-3]):
                peak = recent_peak(pair, minutes=30)
                if peak and (peak - price) / peak >= REVERSAL_DROP_PCT:
                    log(f"[SELL-REV] {base} reversal -> SELL")
                    prec = precision_amount(pair)
                    sell_qty = quantize_amount(amt, prec)
                    if sell_qty > 0:
                        res = market_sell(pair, sell_qty)
                        if res.get("error") == "min_volume":
                            log(f"[DUST] {pair} unsellable on reversal")
                        else:
                            if base in buys:
                                del buys[base]; save_json(BUYS_FILE, buys)
                            time.sleep(0.3)
                    continue

            # attempt selling dust if amt >= market min
            m = market_min_amount(pair)
            if m and amt >= m:
                prec = precision_amount(pair)
                sell_qty = quantize_amount(amt, prec)
                if sell_qty > 0:
                    res = market_sell(pair, sell_qty)
                    if not res.get("error"):
                        dust_usd = sell_qty * price
                        record_dust(dust_usd)
                        log(f"[DUST-SOLD] {pair} recovered ${dust_usd:.4f}")
                        time.sleep(0.3)

        # refresh pools
        total_usd = get_total_usd()
        tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - stats.get("reserve_usd", 0.0))
        log(f"[AFTER SELL] Total USD ${total_usd:.2f} | Tradeable ${tradeable_pool:.2f} | Reserve(mem) ${stats.get('reserve_usd',0.0):.2f}")

        if tradeable_pool < MIN_TRADE_USD:
            ensure_tradeable(MIN_TRADE_USD * 2)
            total_usd = get_total_usd()
            tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - stats.get("reserve_usd", 0.0))

        # BUY PHASE
        buys_this_loop = 0
        current_positions = sum(1 for b in buys.keys() if get_base_balance(b) > 0)
        free_slots = max(0, MAX_CONCURRENT_POS - current_positions)
        if free_slots <= 0:
            log("[BUY] max concurrent positions reached")
        else:
            per_buy = max(MIN_TRADE_USD, tradeable_pool / max(1, free_slots))
            for pair in candidates:
                if buys_this_loop >= MAX_BUYS_PER_LOOP:
                    break
                if tradeable_pool < MIN_TRADE_USD:
                    break
                price = fetch_price(pair)
                if price is None:
                    continue
                base = pair.split('/')[0]
                # skip if already holding
                if get_base_balance(base) > 0:
                    continue
                # liquidity/spread
                if not volume_ok(pair) or not spread_ok(pair):
                    continue
                peak = recent_peak(pair, minutes=15)
                if not peak:
                    continue
                dip = (peak - price) / peak
                if dip < DIP_PCT:
                    continue
                closes = last_n_closes(pair, 3)
                if len(closes) < 3 or not (closes[-1] > closes[-2] > closes[-3]):
                    continue
                if not short_long_momentum(pair, short=SHORT_MA_MIN, long=LONG_MA_MIN):
                    continue

                # --- key logic to combine dust: ensure post-buy qty >= market min ---
                m = market_min_amount(pair)
                usd_alloc = per_buy
                if m:
                    current_base = get_base_balance(base)
                    need_qty = max(0.0, m - current_base)
                    usd_needed_for_min = need_qty * price
                    if usd_alloc < usd_needed_for_min:
                        # try to increase allocation up to tradeable_pool
                        if tradeable_pool >= usd_needed_for_min:
                            usd_alloc = usd_needed_for_min
                        else:
                            log(f"[BUY SKIP] {pair}: per_buy ${per_buy:.2f} insufficient to reach min. need ${usd_needed_for_min:.2f}")
                            continue

                # finally buy
                res = market_buy(pair, usd_alloc)
                if res:
                    p_buy = res["price"]; qty = res["qty"]
                    buys[base] = {"pair": pair, "price": p_buy, "qty": qty, "time": now().isoformat(),
                                  "cooldown_until": (now() + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()}
                    save_json(BUYS_FILE, buys)
                    trade_log.append(("BUY", base, p_buy, qty, usd_alloc, now().isoformat()))
                    buys_this_loop += 1
                    tradeable_pool -= usd_alloc
                    time.sleep(0.5)

        # periodic summary every 10 minutes
        if time.time() - last_summary_time >= 600:
            log(f"[SUMMARY] TP=${stats.get('lifetime_take_profit_usd',0.0):.2f} | Dust=${stats.get('lifetime_dust_recovered_usd',0.0):.2f} | Reserve=${stats.get('reserve_usd',0.0):.2f}")
            maybe_write_daily_summary()
            last_summary_time = time.time()

    except Exception as e:
        log(f"[ERROR] main loop exception: {e}")
        traceback.print_exc()
    elapsed = time.time() - loop_start
    time.sleep(max(1, CYCLE_SECONDS - elapsed))
