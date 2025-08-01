import ccxt
import os
import time

# Setup Kraken with Heroku config vars
kraken = ccxt.kraken({
    'apiKey': os.environ['KRAKEN_API_KEY'],
    'secret': os.environ['KRAKEN_API_SECRET']
})

# Settings
USD_BALANCE = 25
MIN_PROFIT = 0.15  # USD
TRADE_INTERVAL = 60  # seconds between trade cycles
TRADING_SYMBOLS = ['SHIB/USD', 'DOGE/USD', 'PEPE/USD']

buy_prices = {}  # Stores buy price for each coin

def log(msg):
    print(f"[LOG] {msg}")

def get_balance(symbol):
    balance = kraken.fetch_balance()
    return balance['total'].get(symbol, 0)

def trade_cycle():
    global buy_prices

    for symbol in TRADING_SYMBOLS:
        try:
            ticker = kraken.fetch_ticker(symbol)
            last_price = ticker['last']
            base = symbol.split('/')[0]

            coin_balance = get_balance(base)

            # If you already own this coin
            if coin_balance > 0 and base in buy_prices:
                buy_price = buy_prices[base]
                profit = (last_price - buy_price) * coin_balance
                if profit >= MIN_PROFIT:
                    kraken.create_market_sell_order(symbol, coin_balance)
                    log(f"Sold {coin_balance:.6f} {base} at ${last_price:.8f} for ${profit:.2f} profit.")
                    del buy_prices[base]
            else:
                usd_balance = get_balance('USD')
                if usd_balance >= 1:
                    usd_per_trade = USD_BALANCE / len(TRADING_SYMBOLS)
                    amount_to_buy = usd_per_trade / last_price
                    kraken.create_market_buy_order(symbol, amount_to_buy)
                    buy_prices[base] = last_price
                    log(f"Bought {amount_to_buy:.6f} {base} at ${last_price:.8f}")
        except Exception as e:
            log(f"Error trading {symbol}: {e}")

# Run loop forever
while True:
    try:
        trade_cycle()
        time.sleep(TRADE_INTERVAL)
    except Exception as e:
        log(f"Fatal Error: {e}")
        time.sleep(60)
