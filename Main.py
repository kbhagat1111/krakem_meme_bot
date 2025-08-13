import os
import time
from datetime import datetime, timezone
import statistics
import math
import ccxt

# ========================= CONFIG (env overrides) ========================= #
API_KEY            = os.getenv("KRAKEN_API_KEY", "")
API_SECRET         = os.getenv("KRAKEN_API_SECRET", "")
TRADE_PAIRS_ENV    = os.getenv("TRADE_PAIRS", "")
PROFIT_TARGET_USD  = float(os.getenv("PROFIT_TARGET_USD", "0.15"))
DIP_PERCENT        = float(os.getenv("DIP_PERCENT", "0.50"))
MOMENTUM_BARS      = int(os.getenv("MOMENTUM_BARS", "5"))
TIMEFRAME          = os.getenv("TIMEFRAME", "1m")
SPREAD_LIMIT       = float(os.getenv("SPREAD_LIMIT", "1.0"))
LOOP_DELAY         = int(os.getenv("LOOP_DELAY", "15"))
RESERVE_RATIO      = float(os.getenv("RESERVE_RATIO", "0.30"))

# Old Kraken fee setting kept for normal loop
KRAKEN_FEE_PCT     = float(os.getenv("KRAKEN_FEE_PCT", "0.26"))

# New massive fee settings for startup force-sell
KRAKEN_FEE = 0.375        # 37.5% per trade
ROUND_TRIP_FEE = 0.75     # 75% total
PROFIT_BUFFER = 0.05      # 5% above fees

MAX_CONCURRENT_POS = int(os.getenv("MAX_CONCURRENT_POS", "5"))
MIN_USD_PER_BUY    = float(os.getenv("MIN_USD_PER_BUY", "10"))
# ======================================================================== #

def now():
    return datetime.now(timezone.utc).isoformat()

def log(msg):
    print(f"[{now()}] {msg}", flush=True)

def as_fee_fraction():
    return KRAKEN_FEE_PCT / 100.0

exchange = ccxt.kraken({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
})
exchange.load_markets()

def get_auto_meme_pairs():
    memes = []
    for sym, m in exchange.markets.items():
        if not sym.endswith("/USD"):
            continue
        base_lower = m['base'].lower()
        name_lower = sym.lower()
        if any(tag in base_lower or tag in name_lower for tag in ['doge', 'shib', 'pepe', 'floki', 'inu', 'bonk']):
            memes.append(sym)
    preferred = ['DOGE/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD', 'BONK/USD']
    picked = [p for p in preferred if p in memes]
    for m in memes:
        if len(picked) >= 8:
            break
        if m not in picked:
            picked.append(m)
    return picked or ['DOGE/USD', 'SHIB/USD', 'PEPE/USD']

TRADE_PAIRS = []
if TRADE_PAIRS_ENV.strip():
    TRADE_PAIRS = [p.strip() for p in TRADE_PAIRS_ENV.split(",") if p.strip()]
else:
    TRADE_PAIRS = get_auto_meme_pairs()
log(f"Tracking pairs: {TRADE_PAIRS}")

def fetch_bid_ask(pair):
    ob = exchange.fetch_order_book(pair)
    bid = ob['bids'][0][0] if ob['bids'] else None
    ask = ob['asks'][0][0] if ob['asks'] else None
    return bid, ask

def spread_ok(pair):
    bid, ask = fetch_bid_ask(pair)
    if not bid or not ask or bid <= 0 or ask <= 0:
        return False
    spread_pct = ((ask - bid) / bid) * 100.0
    return spread_pct <= SPREAD_LIMIT

def get_balance(symbol):
    bal = exchange.fetch_balance()
    return float(bal['total'].get(symbol, 0.0))

def get_usd_balance():
    return get_balance('USD')

def market_min_amount(pair):
    m = exchange.market(pair)
    return m.get('limits', {}).get('amount', {}).get('min') or 0.0

def amount_precision(pair):
    m = exchange.market(pair)
    return m.get('precision', {}).get('amount')

def price_precision(pair):
    m = exchange.market(pair)
    return m.get('precision', {}).get('price')

def round_amount(pair, qty):
    prec = amount_precision(pair)
    if prec is None:
        return float(qty)
    step = 10 ** (-prec)
    return math.floor(qty / step) * step

def round_price(pair, px):
    prec = price_precision(pair)
    if prec is None:
        return float(px)
    step = 10 ** (-prec)
    return math.floor(px / step) * step

def recent_closes(pair, limit):
    try:
        candles = exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=limit)
        return [c[4] for c in candles]
    except Exception as e:
        log(f"[CANDLE ERROR] {pair}: {e}")
        return []

def buy_signal(pair):
    closes = recent_closes(pair, max(15, MOMENTUM_BARS * 3))
    if len(closes) < MOMENTUM_BARS + 3:
        return False
    current = closes[-1]
    recent_high = max(closes)
    if recent_high <= 0:
        return False
    drop_pct = ((recent_high - current) / recent_high) * 100.0
    if drop_pct < DIP_PERCENT:
        return False
    sma = statistics.mean(closes[-MOMENTUM_BARS:])
    return current > sma

book = {}
realized_profit_total = 0.0
reserve_pool = 0.0

def mark_buy(pair, qty, fill_price):
    global book
    fee = as_fee_fraction()
    cost_usd = qty * fill_price * (1.0 + fee)
    if pair in book and book[pair]['qty'] > 0:
        old_qty = book[pair]['qty']
        old_cost = book[pair]['cost_usd']
        new_qty = old_qty + qty
        new_cost = old_cost + cost_usd
        book[pair] = {'qty': new_qty, 'cost_usd': new_cost}
    else:
        book[pair] = {'qty': qty, 'cost_usd': cost_usd}

def mark_sell(pair, qty_sold, fill_price):
    global book
    fee = as_fee_fraction()
    if pair not in book or book[pair]['qty'] <= 0:
        return 0.0
    pos = book[pair]
    pos_qty = pos['qty']
    pos_cost = pos['cost_usd']
    if qty_sold > pos_qty:
        qty_sold = pos_qty
    cost_slice = pos_cost * (qty_sold / pos_qty)
    proceeds = qty_sold * fill_price * (1.0 - fee)
    realized = proceeds - cost_slice
    remain_qty = pos_qty - qty_sold
    remain_cost = pos_cost - cost_slice
    if remain_qty > 0:
        book[pair] = {'qty': remain_qty, 'cost_usd': remain_cost}
    else:
        book.pop(pair, None)
    return realized

def current_cost_basis_px(pair):
    if pair not in book or book[pair]['qty'] <= 0:
        return None
    pos = book[pair]
    if pos['qty'] <= 0:
        return None
    return pos['cost_usd'] / pos['qty']

def can_trade_more_positions():
    count = sum(1 for v in book.values() if v['qty'] > 0)
    return count < MAX_CONCURRENT_POS

def create_market_buy(pair, usd_amount):
    if usd_amount < MIN_USD_PER_BUY:
        log(f"[BUY SKIP] {pair}: usd {usd_amount:.2f} < MIN_USD_PER_BUY {MIN_USD_PER_BUY}")
        return None
    bid, ask = fetch_bid_ask(pair)
    if not ask:
        log(f"[BUY SKIP] {pair}: no ask")
        return None
    qty = usd_amount / ask
    qty = round_amount(pair, qty)
    min_amt = market_min_amount(pair)
    if qty <= 0 or qty < min_amt:
        log(f"[BUY SKIP] {pair}: qty {qty} < min {min_amt}")
        return None
    try:
        o = exchange.create_market_buy_order(pair, qty)
        fill_price = ask
        log(f"[BUY] {pair} qty={qty} @~{fill_price}")
        mark_buy(pair, qty, fill_price)
        return o
    except Exception as e:
        log(f"[BUY ERROR] {pair}: {e}")
        return None

def create_market_sell(pair, qty):
    bid, ask = fetch_bid_ask(pair)
    if not bid:
        log(f"[SELL SKIP] {pair}: no bid")
        return None
    qty = round_amount(pair, qty)
    min_amt = market_min_amount(pair)
    if qty <= 0 or qty < min_amt:
        log(f"[SELL SKIP] {pair}: qty {qty} < min {min_amt}")
        return None
    try:
        o = exchange.create_market_sell_order(pair, qty)
        fill_price = bid
        realized = mark_sell(pair, qty, fill_price)
        log(f"[SELL] {pair} qty={qty} @~{fill_price} | realized ${realized:.4f}")
        return o, realized
    except Exception as e:
        log(f"[SELL ERROR] {pair}: {e}")
        return None

def required_take_profit_price(entry_px):
    f = as_fee_fraction()
    return entry_px * (1 + f) / (1 - f) + (PROFIT_TARGET_USD / (1 - f))

# ========== STARTUP FORCE-SELL IF PROFITABLE ==========
log("[STARTUP] Checking existing positions for force-sell...")
balances = exchange.fetch_balance()
for pair in TRADE_PAIRS:
    base = pair.split("/")[0]
    qty = float(balances['total'].get(base, 0.0))
    if qty <= 0:
        continue
    bid, ask = fetch_bid_ask(pair)
    if not bid:
        continue
    # Estimate cost basis if unknown
    entry_px = current_cost_basis_px(pair) or bid
    profit_pct = ((bid - entry_px) / entry_px)
    if profit_pct > (ROUND_TRIP_FEE + PROFIT_BUFFER):
        log(f"[FORCE SELL] {pair} profit {profit_pct*100:.2f}% > {(ROUND_TRIP_FEE + PROFIT_BUFFER)*100:.2f}% threshold")
        create_market_sell(pair, qty)
    else:
        log(f"[KEEP] {pair} profit {profit_pct*100:.2f}% <= threshold")

# ========== MAIN LOOP ==========
while True:
    try:
        usd = get_usd_balance()
        total_positions_value = 0.0
        for pair, pos in list(book.items()):
            if pos['qty'] <= 0:
                continue
            bid, ask = fetch_bid_ask(pair)
            mkt = (bid or ask or 0.0)
            total_positions_value += pos['qty'] * mkt

        log(f"[POOL] USD ${usd:.2f} | Positions est ${total_positions_value:.2f} | Reserve ${reserve_pool:.2f}")

        # SELL logic
        for pair in TRADE_PAIRS:
            if pair not in book or book[pair]['qty'] <= 0:
                continue
            if not spread_ok(pair):
                continue
            entry_px = current_cost_basis_px(pair)
            if entry_px is None:
                continue
            bid, ask = fetch_bid_ask(pair)
            px = bid or 0.0
            if px <= 0:
                continue
            min_take = required_take_profit_price(entry_px)
            if px >= min_take:
                qty_to_sell = book[pair]['qty']
                res = create_market_sell(pair, qty_to_sell)
                if res:
                    _, realized = res
                    if realized > 0:
                        reserve_add = realized * RESERVE_RATIO
                        reserve_pool += reserve_add
                        reinvest_usd = realized * (1.0 - RESERVE_RATIO)
                        log(f"[PROFIT] {pair} realized ${realized:.4f} -> reserve +${reserve_add:.4f}, reinvest ${reinvest_usd:.4f}")

        usd = get_usd_balance()

        # BUY logic
        open_positions = sum(1 for v in book.values() if v['qty'] > 0)
        slots_left = max(0, MAX_CONCURRENT_POS - open_positions)

        if slots_left > 0 and usd >= MIN_USD_PER_BUY:
            per_trade_usd = max(MIN_USD_PER_BUY, usd / (slots_left + open_positions + 1))
            for pair in TRADE_PAIRS:
                if slots_left <= 0:
                    break
                if pair in book and book[pair]['qty'] > 0:
                    continue
                if not spread_ok(pair):
                    continue
                if not buy_signal(pair):
                    log(f"[SKIP BUY] {pair} drop/momentum not satisfied (dip>={DIP_PERCENT}%, SMA breakout)")
                    continue
                order = create_market_buy(pair, per_trade_usd)
                if order:
                    slots_left -= 1

        time.sleep(LOOP_DELAY)

    except Exception as e:
        log(f"[ERROR] main loop: {e}")
        time.sleep(10)
