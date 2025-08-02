import time
import os
import krakenex
from dotenv import load_dotenv
from pykrakenapi import KrakenAPI

load_dotenv()

# Kraken API setup
api = krakenex.API()
api.load_key('kraken.key')  # or use env variables below

# Alternatively, you can use environment variables:
# api_key = os.getenv("KRAKEN_API_KEY")
# api_sec = os.getenv("KRAKEN_API_SECRET")
# api = krakenex.API(api_key, api_sec)

k = KrakenAPI(api)

# Settings
meme_coins = ["SHIB/USD", "PEPE/USD", "DOGE/USD"]
usd_per_trade = 5  # Trade $5 per coin
profit_target = 0.15  # Sell when $0.15 profit is reached
check_interval = 60  # in seconds

# Buy prices to track profits
buy_prices = {}

def log(msg):
    print(f"[LOG] {msg}")

def get_balance():
    try:
        return k.get_account_balance()
    except Exception as e:
        log(f"Balance fetch error: {e}")
        return {}

def get_price(symbol):
    try:
        ticker = api.query_public('Ticker', {'pair': symbol.replace("/", "")})
        pair_data = list(ticker['result'].values())[0]
        return float(pair_data['c'][0])  # last trade price
    except Exception as e:
        log(f"Price fetch error for {symbol}: {e}")
        return None

def create_market_buy_order(symbol, usd_amount):
    try:
        price = get_price(symbol)
        base = symbol.split('/')[0]
        if price:
            volume = round(usd_amount / price, 0)  # rounded for meme coins
            if volume <= 0:
                log(f"Buy volume too small for {symbol}")
                return
            api.query_private('AddOrder', {
                'pair': symbol.replace("/", ""),
                'type': 'buy',
                'ordertype': 'market',
                'volume': str(volume)
            })
            buy_prices[base] = price
            log(f"Bought {volume} {base} at ${price}")
    except Exception as e:
        log(f"Error buying {symbol}: {e}")

def create_market_sell_order(symbol, volume):
    try:
        api.query_private('AddOrder', {
            'pair': symbol.replace("/", ""),
            'type': 'sell',
            'ordertype': 'market',
            'volume': str(volume)
        })
    except Exception as e:
        log(f"Error selling {symbol}: {e}")

while True:
    balances = get_balance()

    for symbol in meme_coins:
        base = symbol.split("/")[0]
        price = get_price(symbol)
        if not price:
            continue

        # Attempt to sell if already owned
        if base in balances:
            amount = float(balances[base])
            if amount > 0:
                if base in buy_prices:
                    profit = (price - buy_prices[base]) * amount
                    if profit >= profit_target:
                        create_market_sell_order(symbol, amount)
                        log(f"Sold {amount} {base} at ${price:.8f} for ${profit:.2f} profit.")
                        del buy_prices[base]
                    else:
                        log(f"Holding {base}. Profit: ${profit:.2f}, waiting for ${profit_target}")
                else:
                    log(f"Skipping sell for {base}. No recorded buy price.")

        # Attempt to buy
        usd_balance = float(balances.get("ZUSD", 0))
        if usd_balance >= usd_per_trade:
            create_market_buy_order(symbol, usd_per_trade)
        else:
            log("Not enough USD to trade.")

    time.sleep(check_interval)
