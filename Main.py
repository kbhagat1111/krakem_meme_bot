import os
import time
import ccxt
from datetime import datetime, timezone

API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")

# Configure Kraken
exchange = ccxt.kraken({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True
})

# Track last seen prices
last_highs = {}
buy_orders = {}
reserve_usd = 0.0

# Config
BUY_DROP_PCT = 0.5 / 100     # 0.5% drop to trigger buy
SELL_RISE_PCT = 0.8 / 100    # 0.8% rise to trigger sell
TRADE_USD = 10               # USD per trade
LOOP_DELAY = 15              # seconds between checks

def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)

def get_balance_usd():
    balance = exchange.fetch_balance()
    usd_bal = balance['total'].get('USD', 0.0)
    return usd_bal

def get_price(pair):
    ticker = exchange.fetch_ticker(pair)
    return ticker['last']

def place_buy(pair, usd_amount):
    price = get_price(pair)
    amount = usd_amount / price
    order = exchange.create_market_buy_order(pair, amount)
    log(f"[BUY] {pair} at ${price:.6f} for ${usd_amount:.2f}")
    return price

def place_sell(pair, amount):
    price = get_price(pair)
    order = exchange.create_market_sell_order(pair, amount)
    log(f"[SELL] {pair} at ${price:.6f}")
    return price

def main():
    global reserve_usd
    meme_pairs = ['BONK/USD', 'FLOKI/USD', 'PEPE/USD', 'SHIB/USD', 'DOGE/USD']

    log(f"Tracking pairs: {meme_pairs}")

    while True:
        usd_balance = get_balance_usd()
        tradeable_usd = usd_balance
        log(f"[POOL] Total USD ${usd_balance:.2f} | Tradeable ${tradeable_usd:.2f} | Reserve ${reserve_usd:.2f}")

        for pair in meme_pairs:
            try:
                price = get_price(pair)

                # Track highest price seen for drop calculation
                if pair not in last_highs or price > last_highs[pair]:
                    last_highs[pair] = price

                # If not holding this coin, check buy condition
                if pair not in buy_orders:
                    drop_pct = ((last_highs[pair] - price) / last_highs[pair]) * 100
                    if drop_pct >= BUY_DROP_PCT * 100 and tradeable_usd >= TRADE_USD:
                        buy_price = place_buy(pair, TRADE_USD)
                        amount_bought = TRADE_USD / buy_price
                        buy_orders[pair] = {"buy_price": buy_price, "amount": amount_bought}
                        log(f"[BUY TRIGGER] Drop {drop_pct:.2f}% met for {pair}")
                    else:
                        log(f"[SKIP BUY] {pair} drop {drop_pct:.2f}% < {BUY_DROP_PCT*100:.2f}%")
                
                # If holding this coin, check sell condition
                else:
                    buy_price = buy_orders[pair]["buy_price"]
                    amount_held = buy_orders[pair]["amount"]
                    rise_pct = ((price - buy_price) / buy_price) * 100
                    if rise_pct >= SELL_RISE_PCT * 100:
                        sell_price = place_sell(pair, amount_held)
                        profit = (sell_price - buy_price) * amount_held
                        reserve_cut = profit * 0.30
                        reserve_usd += reserve_cut
                        log(f"[SELL TRIGGER] Rise {rise_pct:.2f}% met for {pair} | Profit ${profit:.4f} | Reserve +${reserve_cut:.4f}")
                        del buy_orders[pair]
                    else:
                        log(f"[SKIP SELL] {pair} rise {rise_pct:.2f}% < {SELL_RISE_PCT*100:.2f}%")

            except Exception as e:
                log(f"[ERROR] {pair}: {str(e)}")

        time.sleep(LOOP_DELAY)

if __name__ == "__main__":
    main()
