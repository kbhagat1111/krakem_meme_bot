# main.py
# Stateles scalper for Kraken (ccxt)
# - Sells all holdings on start
# - 70% invest / 30% reserve
# - Buy only on 2% dip + momentum
# - Take profit: +2% NET after fees
# - Stop loss: -4% from buy price
# - Sideways timeout: 10 minutes within ±1%
# - Cooldown: 5 loops before rebuying same coin
#
# WARNING: This places real orders. Test small first.

import os
import time
import math
import ccxt
from datetime import datetime, timedelta
from collections import deque, defaultdict

# ---------------- CONFIG ----------------
CYCLE_SECONDS = 30                 # loop interval (you asked 30s)
MIN_TRADE_USD = 1.0                # minimum USD per order
TRADEABLE_FRAC = 0.70              # 70% invested
RESERVE_FRAC = 0.30                # 30% saved
FEE_EST = 0.0026                   # estimated taker fee (0.26%)
NET_TAKE_PROFIT_PCT = 0.02         # 2% net profit after fees
STOP_LOSS_PCT = -0.04              # -4% stop-loss
DIP_PCT = 0.02                     # 2% dip detection from recent peak
SIDEWAYS_THRESHOLD = 0.01          # ±1% = sideways
SIDEWAYS_SECONDS = 600             # 10 minutes
COOLDOWN_LOOPS = 5                 # loops to wait before rebuying same coin
MAX_CONCURRENT_POS = 6
MAX_BUYS_PER_LOOP = 2
WATCHLIST = ['SHIB/USD', 'DOGE/USD', 'PEPE/USD', 'BONK/USD', 'FLOKI/USD']  # base watchlist

# ---------------- SETUP ----------------
API_KEY = os.getenv('KRAKEN_API_KEY')
API_SECRET = os.getenv('KRAKEN_API_SECRET')
if not API_KEY or not API_SECRET:
    raise SystemExit("Set KRAKEN_API_KEY and KRAKEN_API_SECRET in env vars")

exchange = ccxt.kraken({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
})
exchange.load_markets()

# In-memory state (stateless across dyno restarts)
buy_price_map = {}         # base -> buy_price (last buy the bot did)
buy_time_map = {}          # base -> datetime of buy
reserve_usd = 0.0          # 30% reserve tracked in memory (not moved)
cooldowns = defaultdict(int)  # base -> loops remaining to block rebuy
restricted_pairs = set()      # pairs we discovered are restricted/blocked

# small rolling log of trades
trade_history = deque(maxlen=200)

# ---------------- utilities ----------------
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

def now(): return datetime.utcnow()

def sleep(secs):
    try:
        time.sleep(secs)
    except KeyboardInterrupt:
        raise

def safe_fetch_balance():
    try:
        return exchange.fetch_balance()
    except Exception as e:
        log(f"Balance fetch error: {e}")
        return {'total': {}}

def get_total_usd():
    bal = safe_fetch_balance().get('total', {})
    return float(bal.get('USD') or bal.get('ZUSD') or 0.0)

def get_base_balance(base):
    bal = safe_fetch_balance().get('total', {})
    return float(bal.get(base) or 0.0)

def market_for_pair(pair):
    # ccxt uses 'SHIB/USD' etc; ensure exact mapping
    if pair in exchange.markets:
        return exchange.markets[pair]
    # try alternative formatting
    alt = pair.replace('/', '')
    for m in exchange.markets:
        if m.replace('/', '') == alt:
            return exchange.markets[m]
    return None

def precision_amount(sym):
    m = market_for_pair(sym)
    if not m:
        return 8
    return m.get('precision', {}).get('amount', 8)

def min_amount(sym):
    m = market_for_pair(sym)
    if not m:
        return None
    return (m.get('limits', {}).get('amount') or {}).get('min')

def quantize_amount(amount, prec):
    if prec is None:
        prec = 8
    factor = 10 ** prec
    return math.floor(amount * factor) / factor

def try_mark_restricted(pair, err):
    s = str(err).lower()
    if 'restricted' in s or 'invalid permissions' in s or 'not available' in s:
        restricted_pairs.add(pair)
        log(f"[RESTRICTED] Marking {pair} restricted due to error: {err}")
        return True
    return False

# ---------------- market helpers ----------------
def fetch_price(pair):
    try:
        t = exchange.fetch_ticker(pair)
        return float(t.get('last') or t.get('close') or t.get('info', {}).get('c', [None])[0])
    except Exception as e:
        # if pair not tradable or restricted, mark it
        try_mark_restricted(pair, e)
        return None

def fetch_ohlcv(pair, timeframe='1m', limit=30):
    try:
        return exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
    except Exception as e:
        try_mark_restricted(pair, e)
        return []

def recent_peak(pair, minutes=15):
    # use 1m candles, last `minutes` values
    o = fetch_ohlcv(pair, '1m', limit=max(5, minutes))
    if not o:
        return None
    highs = [candle[2] for candle in o[-minutes:]] if len(o) >= minutes else [c[2] for c in o]
    return max(highs) if highs else None

def short_long_momentum_ok(pair, short_m=5, long_m=15):
    # short MA of closing price should be above long MA (momentum)
    o = fetch_ohlcv(pair, '1m', limit=long_m + 5)
    if not o or len(o) < long_m:
        return False
    closes = [c[4] for c in o]
    short_ma = sum(closes[-short_m:]) / short_m
    long_ma = sum(closes[-long_m:]) / long_m
    return short_ma > long_ma

def volume_ok(pair, min_vol=200):
    # check baseVolume if available in ticker
    try:
        t = exchange.fetch_ticker(pair)
        vol = t.get('baseVolume') or t.get('quoteVolume') or t.get('info', {}).get('v', [None])[-1]
        if vol is None:
            return True
        return float(vol) >= min_vol
    except Exception:
        return True

def spread_ok(pair, max_spread_pct=0.02):
    # require (ask-bid)/mid < max_spread_pct
    try:
        orderbook = exchange.fetch_order_book(pair, 5)
        bid = orderbook['bids'][0][0] if orderbook['bids'] else None
        ask = orderbook['asks'][0][0] if orderbook['asks'] else None
        if not bid or not ask:
            return False
        mid = (bid + ask) / 2
        return (ask - bid) / mid <= max_spread_pct
    except Exception:
        return True

# ---------------- trading primitives ----------------
def market_sell(pair, amount):
    try:
        res = exchange.create_market_sell_order(pair, amount)
        log(f"[SELL EXECUTED] {pair} amount={amount} -> order {res.get('id') if isinstance(res, dict) else res}")
        return True
    except Exception as e:
        if try_mark_restricted(pair, e):
            return False
        log(f"Sell failed for {pair}: {e}")
        return False

def market_buy(pair, amount_usd):
    price = fetch_price(pair)
    if not price:
        return None
    raw_amount = amount_usd / price
    prec = precision_amount(pair)
    buy_amt = quantize_amount(raw_amount, prec)
    if buy_amt <= 0:
        log(f"Buy skipped {pair}: computed amount 0 after precision")
        return None
    m = min_amount(pair)
    if m and buy_amt < m:
        log(f"Buy skipped {pair}: amount {buy_amt} < market min {m}")
        return None
    try:
        res = exchange.create_market_buy_order(pair, buy_amt)
        log(f"[BUY EXECUTED] {pair} amount={buy_amt} usd_alloc={amount_usd} order {res.get('id') if isinstance(res, dict) else res}")
        return price, buy_amt
    except Exception as e:
        if try_mark_restricted(pair, e):
            return None
        log(f"Buy failed for {pair}: {e}")
        return None

# ---------------- decision helpers ----------------
def net_pct_after_fees(buy_price, sell_price):
    gross_pct = (sell_price / buy_price) - 1.0
    fee_cost = 2 * FEE_EST  # approximate both sides
    return gross_pct - fee_cost

def percent_change(a, b):
    return (b / a) - 1.0

# ---------------- high-level helpers ----------------
def sell_all_positions():
    bal = safe_fetch_balance().get('total', {})
    sold_any = False
    for asset, amt in list(bal.items()):
        if asset in ('USD', 'ZUSD') or float(amt) <= 0:
            continue
        # build pair string: Kraken assets sometimes have X/Z prefix; try a few forms
        base = asset
        # canonical pair candidates
        candidates = []
        if base.startswith('X') or base.startswith('Z'):
            candidates.append(base[1:] + '/USD')
        candidates.append(base + '/USD')
        for pair in candidates:
            if pair in exchange.markets:
                if float(amt) <= 0:
                    continue
                prec = precision_amount(pair)
                sell_amt = quantize_amount(float(amt), prec)
                if sell_amt <= 0:
                    log(f"Sell skipped {pair}: amount after precision 0")
                    continue
                ok = market_sell(pair, sell_amt)
                sold_any = sold_any or ok
                time.sleep(0.3)
                break
    return sold_any

def positions_list():
    bal = safe_fetch_balance().get('total', {})
    positions = []
    for pair in exchange.markets:
        base = pair.split('/')[0]
        amt = float(bal.get(base) or 0.0)
        if amt > 0 and pair.endswith('/USD'):
            positions.append((pair, amt))
    return positions

def ensure_tradeable(target_usd, candidates):
    # if tradeable < target_usd, sell worst performers first until enough
    total_usd = get_total_usd()
    tradeable = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
    log(f"[ENSURE] tradeable ${tradeable:.2f} target ${target_usd:.2f}")
    if tradeable >= target_usd:
        return True
    # build list of held positions with unrealized percent (low->high)
    held = []
    bal = safe_fetch_balance().get('total', {})
    for pair, _ in candidates:
        base = pair.split('/')[0]
        amt = float(bal.get(base) or 0.0)
        if amt <= 0:
            continue
        price = fetch_price(pair)
        buy = buy_price_map.get(base)
        if price is None:
            continue
        unreal_pct = percent_change(buy or price, price)
        held.append((unreal_pct, pair, base, amt, price, buy))
    held.sort(key=lambda x: x[0])  # sell worst (most negative) first
    for unreal_pct, pair, base, amt, price, buy in held:
        if tradeable >= target_usd:
            break
        log(f"[ENSURE SELL] selling {base} unreal_pct={unreal_pct:.4f} to free funds")
        prec = precision_amount(pair)
        sell_amt = quantize_amount(amt, prec)
        if sell_amt <= 0:
            continue
        ok = market_sell(pair, sell_amt)
        if ok:
            # update balances and tradeable
            total_usd = get_total_usd()
            tradeable = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
        time.sleep(0.5)
    total_usd = get_total_usd()
    tradeable = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
    return tradeable >= target_usd

# ---------------- main loop ----------------
def main_loop():
    global reserve_usd
    log("Starting bot — will sell all holdings at start to free funds.")
    sell_all_positions()
    time.sleep(2)

    while True:
        loop_start = time.time()
        try:
            # decrement cooldowns
            for k in list(cooldowns.keys()):
                if cooldowns[k] > 0:
                    cooldowns[k] -= 1
                if cooldowns[k] <= 0:
                    del cooldowns[k]

            total_usd = get_total_usd()
            tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
            log(f"Total USD: ${total_usd:.2f} | Tradeable pool: ${tradeable_pool:.2f} | Reserve (mem): ${reserve_usd:.2f}")

            # build candidates: watchlist + top movers
            candidates = []
            # include watchlist pairs that exist on Kraken
            for p in WATCHLIST:
                if p in exchange.markets and p not in restricted_pairs:
                    candidates.append((p,))  # store as tuple for uniformity
            # add top movers from ticker scan (simple: pct field > 0.5)
            try:
                tickers = exchange.fetch_tickers()
                movers = []
                for sym, t in tickers.items():
                    if not sym.endswith('/USD'):
                        continue
                    pct = t.get('percentage') or t.get('change') or 0
                    vol = t.get('baseVolume') or 0
                    if pct is None:
                        continue
                    try:
                        pchange = float(pct)
                    except:
                        continue
                    if pchange > 0.5 and (vol is None or float(vol) > 100):
                        if sym not in restricted_pairs:
                            movers.append((sym, pchange))
                movers.sort(key=lambda x: x[1], reverse=True)
                for m in movers[:8]:
                    if (m[0],) not in candidates:
                        candidates.append((m[0],))
            except Exception as e:
                log(f"Ticker scan error: {e}")

            # SELL phase: evaluate holdings for profit/stoploss/sideways
            bal = safe_fetch_balance().get('total', {})
            held_pairs = []
            for pair in exchange.markets:
                if not pair.endswith('/USD'):
                    continue
                base = pair.split('/')[0]
                amt = float(bal.get(base) or 0.0)
                if amt <= 0:
                    continue
                held_pairs.append((pair, amt))
            # Evaluate each held position
            for pair, amt in held_pairs:
                base = pair.split('/')[0]
                price = fetch_price(pair)
                buy = buy_price_map.get(base)
                if price is None:
                    continue
                # compute net pct after fees if we had buy price; otherwise skip profit sell
                if buy:
                    net_pct = net_pct_after_fees(buy, price)
                    net_usd = (price - buy) * amt - ( (buy*amt + price*amt) * FEE_EST )
                else:
                    net_pct = None
                    net_usd = None
                # stop-loss check
                if buy and percent_change(buy, price) <= STOP_LOSS_PCT:
                    log(f"[SELL-STOPLOSS] {base} buy={buy:.8f} now={price:.8f} pct={percent_change(buy,price)*100:.2f}% -> SELL")
                    prec = precision_amount(pair)
                    sell_amt = quantize_amount(amt, prec)
                    if sell_amt > 0:
                        if market_sell(pair, sell_amt):
                            # track reserve if profit positive
                            if net_usd and net_usd > 0:
                                reserve_add = net_usd * RESERVE_FRAC
                                reserve_usd += reserve_add
                                log(f"[RESERVE] +${reserve_add:.4f}")
                            cooldowns[base] = COOLDOWN_LOOPS
                    continue
                # take-profit check (net 2% after fees)
                if buy and net_pct is not None and net_pct >= NET_TAKE_PROFIT_PCT:
                    log(f"[SELL-TP] {base} buy={buy:.8f} now={price:.8f} net_pct={net_pct*100:.2f}% -> SELL")
                    prec = precision_amount(pair)
                    sell_amt = quantize_amount(amt, prec)
                    if sell_amt > 0:
                        if market_sell(pair, sell_amt):
                            # compute profit in USD approx
                            profit_usd = (price - buy) * sell_amt - ((buy*sell_amt + price*sell_amt) * FEE_EST)
                            if profit_usd > 0:
                                reserve_add = profit_usd * RESERVE_FRAC
                                reserve_usd += reserve_add
                                log(f"[RESERVE] +${reserve_add:.4f}")
                            cooldowns[base] = COOLDOWN_LOOPS
                    continue
                # sideways timeout: if held > SIDEWAYS_SECONDS and price within ±1% of buy, sell to recycle
                if buy and (now() - buy_time_map.get(base, now())) > timedelta(seconds=SIDEWAYS_SECONDS):
                    if abs(percent_change(buy, price)) <= SIDEWAYS_THRESHOLD:
                        log(f"[SELL-SIDEWAYS] {base} held long and sideways -> SELL")
                        prec = precision_amount(pair)
                        sell_amt = quantize_amount(amt, prec)
                        if sell_amt > 0:
                            if market_sell(pair, sell_amt):
                                cooldowns[base] = COOLDOWN_LOOPS
                        continue

            # refresh tradeable pool after sells
            total_usd = get_total_usd()
            tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)
            log(f"[AFTER SELL] Total USD: ${total_usd:.2f} tradeable_pool: ${tradeable_pool:.2f} reserve(mem) ${reserve_usd:.2f}")

            # BUY phase: decide how many buys to allow this loop
            buys = 0
            allowed_buys = MAX_BUYS_PER_LOOP
            if trade_history:
                # if behind target trades/hour, allow 1 extra
                pass

            # If low tradeable USD, try to free funds by selling worst performers
            if tradeable_pool < MIN_TRADE_USD:
                # ensure at least MIN_TRADE_USD * 2
                ensure_tradeable(MIN_TRADE_USD * 2, [(p[0],) for p in candidates])

                total_usd = get_total_usd()
                tradeable_pool = max(0.0, total_usd * TRADEABLE_FRAC - reserve_usd)

            # allocate per-buy amount: split tradeable_pool among up to (MAX_CONCURRENT_POS - current_positions)
            current_positions = sum(1 for b in buy_price_map.keys() if get_base_balance(b) > 0)
            free_slots = max(1, MAX_CONCURRENT_POS - current_positions)
            if free_slots <= 0:
                log("Max positions held; skipping buys this loop.")
            else:
                per_buy = max(MIN_TRADE_USD, tradeable_pool / free_slots)
                # iterate candidates and attempt buys using dip + momentum + liquidity + spread + cooldown filters
                for (pair_tuple) in candidates:
                    if buys >= allowed_buys:
                        break
                    pair = pair_tuple[0]
                    if pair in restricted_pairs:
                        continue
                    base = pair.split('/')[0]
                    # skip if holding or in cooldown
                    if get_base_balance(base) > 0:
                        continue
                    if cooldowns.get(base, 0) > 0:
                        continue
                    # get price and checks
                    price = fetch_price(pair)
                    if price is None:
                        continue
                    # dip check: current price must be at least DIP_PCT below recent peak
                    peak = recent_peak(pair, minutes=15)
                    if peak is None:
                        continue
                    dip = (peak - price) / peak
                    if dip < DIP_PCT:
                        log(f"Skip buy {pair}: dip {dip*100:.2f}% < {DIP_PCT*100:.1f}%")
                        continue
                    # momentum check
                    if not short_long_momentum_ok(pair, short_m=5, long_m=15):
                        log(f"Skip buy {pair}: momentum check failed")
                        continue
                    # liquidity/spread check
                    if not volume_ok(pair, min_vol=100):
                        log(f"Skip buy {pair}: low volume")
                        continue
                    if not spread_ok(pair, max_spread_pct=0.03):
                        log(f"Skip buy {pair}: spread too wide")
                        continue
                    # finally buy
                    # cap per_buy by available tradeable_pool
                    usd_to_use = min(per_buy, tradeable_pool)
                    if usd_to_use < MIN_TRADE_USD:
                        log("Not enough tradeable USD for another buy")
                        break
                    res = market_buy(pair, usd_to_use)
                    if res:
                        buy_price, amount_bought = res
                        buy_price_map[base] = buy_price
                        buy_time_map[base] = now()
                        trade_history.append(('BUY', base, buy_price, amount_bought, now().isoformat()))
                        buys += 1
                        # reduce tradeable pool locally
                        tradeable_pool -= usd_to_use
                        time.sleep(0.5)

            # loop housekeeping
        except Exception as e:
            log(f"Main loop exception: {e}")

        elapsed = time.time() - loop_start
        to_sleep = max(1, CYCLE_SECONDS - elapsed)
        sleep(to_sleep)

if __name__ == '__main__':
    main_loop()
