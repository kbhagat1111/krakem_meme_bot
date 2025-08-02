import os
import time
import krakenex
from pykrakenapi import KrakenAPI

# Setup Kraken API using Heroku environment variables
api = krakenex.API()
api.key = os.environ['KRAKEN_API_KEY']
api.secret = os.environ['KRAKEN_API_SECRET']
k = KrakenAPI(api)

# Settings
USD_BALANCE = 25
MIN_PROFIT = 0.15  # USD profit target
TRADE_INTERVAL = 60  # in seconds
TRADING_SYMBOLS = ['SHIB/USD', 'DOGE/USD', 'PEPE/USD']
buy_prices = {}

def log(msg):
    print(f"[LOG] {msg}")

def get_balance(currency):
    try:
        balance = k.get_account_balance()
        return float(balance.loc[currency]['vol'])
    except Exception as e:
        log(f"Balance error for {currency}: {e}")
        return 0

def trade_cycle():
    global buy_prices

    for symbol in TRADING_SYMBOLS:
        try:
            base = symbol.split('/')[0]
            quote = symbol.split('/')[1]
            ticker = k.get_ticker_information(symbol)
            last_price = float(ticker['c'][0][0])

            balance = get_balance(base)

            # SELL if we own it and hit target profit
            if balance > 0 and base in buy_prices:
                buy_price = buy_prices[base]
                profit = (last_price - buy_price) * balance
                if profit >= MIN_PROFIT:
                    k.api.query_private('AddOrder', {
                        'pair': symbol.replace("/", ""),
                        'type': 'sell',
                        'ordertype': 'market',
                        'volume': str(balance)
                    })
                    log(f"Sold {balance:.6f} {base} at ${last_price} for ${profit:.2f} profit.")
                    del buy_prices[base]

            # BUY if we don't own it
            else:
                usd_available = get_balance('ZUSD')
                if usd_available > 1:
                    usd_per_trade = USD_BALANCE / len(TRADING_SYMBOLS)
                    volume = usd_per_trade / last_price
                    k.api.query_private('AddOrder', {
                        'pair': symbol.replace("/", ""),
                        'type': 'buy',
                        'ordertype': 'market',
                        'volume': str(volume)
                    })
                    buy_prices[base] = last_price
                    log(f"Bought {volume:.6f} {base} at ${last_price}")
        except Exception as e:
            log(f"Error trading {symbol}: {e}")

# Main loop
while True:
    try:
        trade_cycle()
        time.sleep(TRADE_INTERVAL)
    except Exception as e:
        log(f"Fatal Error: {e}")
        time.sleep(60)
