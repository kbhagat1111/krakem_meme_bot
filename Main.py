import ccxt
import os
import time

# Connect to Kraken API using Heroku Config Vars
kraken = ccxt.kraken({
    'apiKey': os.environ['KRAKEN_API_KEY'],
    'secret': os.environ['KRAKEN_API_SECRET']
})

# Config
USD_BALANCE = 25
MIN_PROFIT = 0.15
TRADE_INTERVAL = 60  # seconds
TRADING_SYMBOLS = ['SHIB/USD', 'DOGE/USD', 'PEPE/USD']

# Store buys
buy_prices = {}

def get_balance(symbol):
    balance = kraken.fetch_balance()
    return balance['total'].get(symbol, 0)

def log(msg):
    print(f"[LOG] {msg}")

def trade_cycle():
    global buy_prices
    for symbol in TRADING_SYMBOLS:
        ticker = kraken.fetch_ticker(symbol)
        last_price = ticker['last']

        base = symbol.split('/')[0]

        # If you already hold this coin
        coin_balance = get_balance(base)
        if coin_balance > 0 and base in buy_prices:
            buy_price = buy_prices[base]
            if (last_price - buy_price) * coin_balance >= MIN_PROFIT:
              kraken.create_market_sell_order(symbol, coin_balance)
                log(f"Sold {coin_balance:.5f} {base} at {last_price} for profit.")
                del buy_prices[base]
        elif get_balance('USD') >= 1:
            usd_to_spend = USD_BALANCE / len(TRADING_SYMBOLS)
            amount_to_buy = usd_to_spend / last_price
            kraken.create_market_buy_order(symbol, amount_to_buy)
            buy_prices[base] = last_price
            log(f"Bought {amount_to_buy:.5f} {base} at {last_price}")

while True:
    try:
        trade_cycle()
        time.sleep(TRADE_INTERVAL)
    except Exception as e:
        log(f"Error: {e}")
        time.sleep(60)
