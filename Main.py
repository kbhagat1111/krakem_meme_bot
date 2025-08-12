import os
import time
import ccxt
from datetime import datetime
import statistics

# ================== CONFIG ================== #
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")
TRADE_PAIRS = []  # leave empty to auto-fetch meme coins from Kraken
PROFIT_TARGET = 0.15  # $ profit per trade before selling
SPREAD_LIMIT = 1.0  # % max spread allowed
LOOP_DELAY = 15  # seconds between checks
RESERVE_RATIO = 0.30  # 30% of profits held aside
DIP_PERCENT = 3.0  # % dip from recent high to consider buying
MOMENTUM_BARS = 5  # number of recent candles for upward momentum
TIMEFRAME = '1m'  # small timeframe for scalping
# ============================================ #

exchange = ccxt.kraken({
    'apiKey': API_KEY,
    'secret': API_SECRET
})
exchange.load_markets()

# ---------- Utility Functions ---------- #
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

def get_tradeable_pairs():
    markets = exchange.load_markets()
    meme_pairs = []
    for m in markets:
        if "/USD" in m and m.isupper():
            if any(x in m.lower() for x in ['doge', 'shib', 'pepe', 'floki', 'inu', 'bonk']):
                meme_pairs.append(m)
    return meme_pairs

def spread_ok(pair):
    orderbook = exchange.fetch_order_book(pair)
    best_bid = orderbook['bids'][0][0] if orderbook['bids'] else 0
    best_ask = orderbook['asks'][0][0] if orderbook['asks'] else 0
    if best_bid <= 0 or best_ask <= 0:
        return False
    spread_percent = ((best_ask - best_bid) / best_bid) * 100
    return spread_percent <= SPREAD_LIMIT

def volume_ok(pair, amount):
    market = exchange.market(pair)
    min_vol = market.get('limits', {}).get('amount', {}).get('min', 0)
    return amount >= (min_vol or 0)

def get_balance(currency):
    balances = exchange.fetch_balance()
    return balances['total'].get(currency, 0)

def get_recent_candles(pair):
    try:
        candles = exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=MOMENTUM_BARS * 3)
        return candles
    except Exception as e:
        log(f"[CANDLE ERROR] {pair}: {e}")
        return []

def detect_buy_signal(pair):
    candles = get_recent_candles(pair)
    if len(candles) < MOMENTUM_BARS:
        return False

    closes = [c[4] for c in candles]  # closing prices
    recent_high = max(closes)
    recent_low = min(closes)
    current_price = closes[-1]

    dip_percent = ((recent_high - current_price) / recent_high) * 100
    if dip_percent < DIP_PERCENT:
        return False

    recent_avg = statistics.mean(closes[-MOMENTUM_BARS:])
    return current_price > recent_avg  # upward momentum

def buy(pair, usd_amount):
    ticker = exchange.fetch_ticker(pair)
    price = ticker['ask']
    qty = usd_amount / price
    if not volume_ok(pair, qty):
        log(f"[BUY SKIP] {pair}: qty {qty} below min trade size")
        return False
    try:
        order = exchange.create_market_buy_order(pair, qty)
        log(f"[BUY] {pair} {qty} @ {price}")
        return order
    except Exception as e:
        log(f"[BUY ERROR] {pair}: {e}")
        return False

def sell(pair):
    base_currency = pair.split('/')[0]
    qty = get_balance(base_currency)
    if not volume_ok(pair, qty):
        log(f"[SELL SKIP] {pair}: qty {qty} below min trade size (dust)")
        return False
    try:
        ticker = exchange.fetch_ticker(pair)
        price = ticker['bid']
        order = exchange.create_market_sell_order(pair, qty)
        log(f"[SELL] {pair} {qty} @ {price}")
        return order
    except Exception as e:
        log(f"[SELL ERROR] {pair}: {e}")
        return False

# ---------- Main Loop ---------- #
profit_pool = 0
reserve_pool = 0

if not TRADE_PAIRS:
    TRADE_PAIRS = get_tradeable_pairs()
    log(f"Auto-selected meme pairs: {TRADE_PAIRS}")

while True:
    try:
        usd_balance = get_balance('USD')
        log(f"[POOL] Total USD ${usd_balance + reserve_pool:.2f} | Tradeable ${usd_balance:.2f} | Reserve ${reserve_pool:.2f}")

        for pair in TRADE_PAIRS:
            if not spread_ok(pair):
                log(f"[SPREAD SKIP] {pair} spread too high")
                continue

            ticker = exchange.fetch_ticker(pair)
            price = ticker['last']

            # Check if we already hold this coin
            base_currency = pair.split('/')[0]
            holdings = get_balance(base_currency)
            if holdings > 0:
                buy_price = price * 0.85  # simulated entry price
                target_price = buy_price + PROFIT_TARGET
                if price >= target_price:
                    sell(pair)
                    profit_pool += PROFIT_TARGET
                    reinvest = profit_pool * (1 - RESERVE_RATIO)
                    reserve_add = profit_pool * RESERVE_RATIO
                    reserve_pool += reserve_add
                    profit_pool = 0
                    if usd_balance >= 5:
                        buy(pair, reinvest)
            else:
                # Buy only if dip + momentum detected
                if usd_balance >= 5 and detect_buy_signal(pair):
                    buy(pair, usd_balance / len(TRADE_PAIRS))

        time.sleep(LOOP_DELAY)

    except Exception as e:
        log(f"[ERROR] main loop exception: {e}")
        time.sleep(10)
