# Main.py
import os
import time
from datetime import datetime, timezone
import statistics
import math

import ccxt

# ===================== CONFIG ===================== #
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# Leave empty to auto-pick common meme pairs; or set manually e.g. ["DOGE/USD","SHIB/USD"]
TRADE_PAIRS = []

# Risk & trade parameters
TIMEFRAME = "1m"        # small TF for scalping
LOOP_DELAY = 10         # seconds between cycles
CANDLE_LOOKBACK = 30    # how many candles to fetch each time
MOMENTUM_BARS = 5       # last-N closes must show momentum
DIP_PERCENT = 2.0       # % drop from recent high to consider BUY (e.g., 2.0 = 2%)
SPREAD_LIMIT = 1.0      # max % bid/ask spread allowed

# Profit logic (fee-aware)
# Kraken taker fee typical default ~0.26% (adjust if your tier is different)
KRAKEN_FEE_RATE = float(os.getenv("KRAKEN_FEE_RATE", "0.0026"))  # per side
EXTRA_MARGIN = float(os.getenv("EXTRA_MARGIN", "0.0010"))        # 0.10% cushion over fees
MIN_ABS_PROFIT = float(os.getenv("MIN_ABS_PROFIT", "0.05"))      # at least $0.05 net after fees

# Position sizing & money management
MAX_OPEN_POSITIONS = 4                # cap concurrent positions
PER_BUY_USD = 20.0                    # nominal USD to deploy per new entry
RESERVE_RATIO = 0.30                  # 30% of realized profit goes to reserve (not traded)
SELL_ALL_ON_START = os.getenv("SELL_ALL_ON_START", "false").lower() == "true"
# ================================================== #

def now():
    return datetime.now(timezone.utc).isoformat()

def log(msg):
    print(f"[{now()}] {msg}", flush=True)

# -------- Exchange bootstrap -------- #
exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
exchange.load_markets()


# -------- Helpers for markets / balances -------- #
def usd_balance():
    try:
        bal = exchange.fetch_balance()
        # Kraken returns 'USD' balance key
        return float(bal.get("total", {}).get("USD", 0.0))
    except Exception as e:
        log(f"[BALANCE ERROR] {e}")
        return 0.0

def base_balance(base):
    try:
        bal = exchange.fetch_balance()
        return float(bal.get("total", {}).get(base, 0.0))
    except Exception as e:
        log(f"[BALANCE ERROR] {base}: {e}")
        return 0.0

def get_pairs():
    if TRADE_PAIRS:
        return TRADE_PAIRS[:]
    memes = []
    for symbol, m in exchange.markets.items():
        if not symbol.endswith("/USD"):
            continue
        low = symbol.lower()
        if any(x in low for x in ["doge", "shib", "pepe", "floki", "bonk"]):
            if m.get("active", True):
                memes.append(symbol)
    # Deduplicate & stable order
    uniq = sorted(set(memes))
    # Reasonable default focus set if too many
    focus = [s for s in uniq if any(k in s for k in ["DOGE/", "SHIB/", "PEPE/", "FLOKI/", "BONK/"])]
    return focus[:5] if focus else uniq[:5]

def fetch_candles(pair, limit=CANDLE_LOOKBACK):
    try:
        return exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=limit)
    except Exception as e:
        log(f"[OHLCV ERROR] {pair}: {e}")
        return []

def spread_ok(pair):
    try:
        ob = exchange.fetch_order_book(pair)
        bid = ob["bids"][0][0] if ob["bids"] else 0.0
        ask = ob["asks"][0][0] if ob["asks"] else 0.0
        if bid <= 0 or ask <= 0:
            return False
        spread_pct = (ask - bid) / bid * 100.0
        return spread_pct <= SPREAD_LIMIT
    except Exception as e:
        log(f"[SPREAD ERROR] {pair}: {e}")
        return False

def min_amount(pair):
    m = exchange.market(pair)
    return float(((m.get("limits") or {}).get("amount") or {}).get("min") or 0.0)

def min_cost(pair):
    m = exchange.market(pair)
    return float(((m.get("limits") or {}).get("cost") or {}).get("min") or 0.0)

def tick_size(pair):
    m = exchange.market(pair)
    prec = (m.get("precision") or {}).get("amount", 8)
    return max(1e-8, 10 ** (-prec))

def quote_price(pair):
    t = exchange.fetch_ticker(pair)
    # Use mid price to estimate break-evens
    bid = t.get("bid") or t.get("last")
    ask = t.get("ask") or t.get("last")
    if not bid or not ask:
        return float(t.get("last", 0.0)), float(t.get("last", 0.0)), float(t.get("last", 0.0))
    mid = (bid + ask) / 2.0
    return float(bid), float(ask), float(mid)


# -------- Fee-aware math -------- #
def net_profit_after_fees_usd(qty, buy_price, sell_price):
    """
    Profit after taker fees on both sides:
    Buy cash outflow = qty * buy_price * (1 + fee)
    Sell cash inflow = qty * sell_price * (1 - fee)
    Net = inflow - outflow
    """
    outflow = qty * buy_price * (1.0 + KRAKEN_FEE_RATE)
    inflow = qty * sell_price * (1.0 - KRAKEN_FEE_RATE)
    return inflow - outflow

def required_sell_price_for_min_profit(buy_price, qty):
    """
    Solve for sell_price such that net_profit >= MIN_ABS_PROFIT and margin >= (2*fee + extra)
    We enforce BOTH:
      1) percentage margin >= (2*fee + EXTRA_MARGIN)
      2) absolute profit >= MIN_ABS_PROFIT
    Take the max of the two requirements.
    """
    # Percent condition:
    # sell >= buy * (1 + 2*fee + extra) / (1 - fee_adjust_for_sell_side)
    # Derive using net profit margin approximation:
    pct_target = buy_price * (1.0 + (2.0 * KRAKEN_FEE_RATE + EXTRA_MARGIN)) / (1.0 - 0.0)
    # Absolute condition: solve inflow - outflow >= MIN_ABS_PROFIT
    # qty*sell*(1-fee) - qty*buy*(1+fee) >= MIN_ABS_PROFIT
    # sell >= [MIN_ABS_PROFIT/qty + buy*(1+fee)] / (1-fee)
    if qty <= 0:
        return float("inf")
    abs_target = (MIN_ABS_PROFIT / qty + buy_price * (1.0 + KRAKEN_FEE_RATE)) / (1.0 - KRAKEN_FEE_RATE)
    return max(pct_target, abs_target)


# -------- Signals -------- #
def buy_signal(pair):
    candles = fetch_candles(pair, CANDLE_LOOKBACK)
    if len(candles) < max(MOMENTUM_BARS + 1, 10):
        return False, 0.0
    closes = [c[4] for c in candles]
    current = closes[-1]
    recent_high = max(closes[-10:])                # short window high
    dip_pct = (recent_high - current) / recent_high * 100.0 if recent_high > 0 else 0.0
    # upward momentum: current above mean of last MOMENTUM_BARS
    mom_ok = current > statistics.mean(closes[-MOMENTUM_BARS:])
    return (dip_pct >= DIP_PERCENT and mom_ok), current

def sell_signal(buy_px, qty, current_px):
    target_px = required_sell_price_for_min_profit(buy_px, qty)
    return current_px >= target_px, target_px


# -------- Order wrappers -------- #
def clamp_amount(pair, qty):
    mmin = min_amount(pair)
    if qty < mmin:
        return 0.0
    step = tick_size(pair)
    # floor to step
    steps = math.floor(qty / step)
    return round(steps * step, 8)

def can_afford_cost(pair, usd):
    cmin = min_cost(pair)
    return usd >= (cmin or 0.0)

def place_market_buy(pair, usd_to_spend):
    bid, ask, mid = quote_price(pair)
    price = float(ask or mid or bid)
    if price <= 0:
        raise Exception("No valid ask price")
    qty_raw = usd_to_spend / price
    qty = clamp_amount(pair, qty_raw)
    if qty <= 0:
        raise Exception(f"Qty {qty_raw} too small for market min {min_amount(pair)}")
    if not can_afford_cost(pair, usd_to_spend):
        raise Exception(f"Cost ${usd_to_spend:.2f} below min cost {min_cost(pair)}")
    order = exchange.create_market_buy_order(pair, qty)
    return order, qty, price

def place_market_sell(pair, qty):
    qty = clamp_amount(pair, qty)
    if qty <= 0:
        raise Exception(f"Sell qty too small for market min {min_amount(pair)}")
    bid, ask, mid = quote_price(pair)
    price = float(bid or mid or ask)
    if price <= 0:
        raise Exception("No valid bid price")
    order = exchange.create_market_sell_order(pair, qty)
    return order, qty, price


# -------- State -------- #
# positions: pair -> {"base": str, "qty": float, "avg_price": float}
positions = {}
reserve_usd = 0.0
realized_profit_usd = 0.0


# -------- Start: optionally liquidate -------- #
def try_sell_all_small_holdings(pairs):
    log("SELL_ALL_ON_START enabled — attempting to liquidate non-USD holdings (where size >= min).")
    for pair in pairs:
        base = pair.split("/")[0]
        qty = base_balance(base)
        if qty <= 0:
            continue
        amt = clamp_amount(pair, qty)
        if amt <= 0:
            log(f"[START SKIP] {pair}: qty after precision {amt}")
            continue
        try:
            order, s_qty, s_px = place_market_sell(pair, amt)
            net = net_profit_after_fees_usd(s_qty, s_px, s_px)  # zero-ish; just liquidation info
            log(f"[START SELL] {pair} qty={s_qty} px≈{s_px} order={order.get('id','?')} net≈${net:.4f}")
        except Exception as e:
            log(f"[START SELL FAILED] {pair}: {e}")


# -------- Main -------- #
def main():
    pairs = get_pairs()
    log(f"Tracking pairs: {pairs}")

    if SELL_ALL_ON_START:
        try_sell_all_small_holdings(pairs)

    global reserve_usd, realized_profit_usd

    while True:
        try:
            # Print pool snapshot
            cash = usd_balance()
            log(f"[POOL] USD total ${cash + reserve_usd:.2f} | Tradeable ${cash:.2f} | Reserve ${reserve_usd:.2f}")

            # Refresh positions from wallet for tracked pairs
            for pair in pairs:
                base = pair.split("/")[0]
                qty = base_balance(base)
                if qty > 0 and pair not in positions:
                    # unknown cost basis if pre-held; set avg at current mid
                    _, _, mid = quote_price(pair)
                    positions[pair] = {"base": base, "qty": qty, "avg_price": mid}
                    log(f"[SYNC] {pair} detected holdings qty={qty:.8f} avg≈{mid:.10f}")
                if qty <= 0 and pair in positions:
                    del positions[pair]
                    log(f"[SYNC] {pair} position closed (no balance)")

            # Attempt sells first (profit protection)
            for pair, pos in list(positions.items()):
                if not spread_ok(pair):
                    continue
                _, _, mid = quote_price(pair)
                ok, tgt = sell_signal(pos["avg_price"], pos["qty"], mid)
                if ok:
                    try:
                        order, s_qty, s_px = place_market_sell(pair, pos["qty"])
                        pnl = net_profit_after_fees_usd(s_qty, pos["avg_price"], s_px)
                        realized_profit_usd += max(0.0, pnl)
                        # 70/30 split on positive realized profit
                        reserve_add = max(0.0, pnl) * RESERVE_RATIO
                        reserve_usd += reserve_add
                        log(f"[SELL] {pair} qty={s_qty:.8f} px≈{s_px:.10f} "
                            f"avg={pos['avg_price']:.10f} net_pnl=${pnl:.4f} -> reserve+${reserve_add:.4f}")
                        del positions[pair]
                    except Exception as e:
                        log(f"[SELL ERROR] {pair}: {e}")

            # Buys (respect max positions & per-buy budget)
            cash = usd_balance()
            open_slots = max(0, MAX_OPEN_POSITIONS - len(positions))
            if cash >= min( PER_BUY_USD, cash ) and open_slots > 0:
                for pair in pairs:
                    if pair in positions:
                        continue
                    if open_slots <= 0:
                        break
                    if not spread_ok(pair):
                        continue

                    signal, cur_px = buy_signal(pair)
                    if not signal:
                        log(f"[SKIP BUY] {pair} no dip/momentum")
                        continue

                    # Ensure we meet min_cost and min_amount
                    budget = min(PER_BUY_USD, cash)  # use defined per-trade or what's available
                    if not can_afford_cost(pair, budget):
                        # try to bump to min cost if cash allows
                        need = max(budget, min_cost(pair) or budget)
                        if cash >= need:
                            budget = need
                        else:
                            log(f"[BUY SKIP] {pair} cash ${cash:.2f} < min_cost ${min_cost(pair):.2f}")
                            continue

                    try:
                        order, b_qty, b_px = place_market_buy(pair, budget)
                        # average in if already somehow present (rare during same loop)
                        if pair in positions:
                            old = positions[pair]
                            new_qty = old["qty"] + b_qty
                            new_avg = (old["avg_price"] * old["qty"] + b_px * b_qty) / new_qty
                            positions[pair]["qty"] = new_qty
                            positions[pair]["avg_price"] = new_avg
                        else:
                            positions[pair] = {"base": pair.split("/")[0], "qty": b_qty, "avg_price": b_px}
                        open_slots -= 1
                        log(f"[BUY] {pair} spent≈${budget:.2f} qty={b_qty:.8f} px≈{b_px:.10f}")
                        cash -= budget  # reflect local budget (exchange balance will reflect real)
                    except Exception as e:
                        log(f"[BUY ERROR] {pair}: {e}")

            time.sleep(LOOP_DELAY)

        except Exception as e:
            log(f"[ERROR] loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
