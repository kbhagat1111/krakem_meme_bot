import krakenex
import time
from datetime import datetime, timezone

# === CONFIGURATION ===
PAIR_LIST = ["SHIB/USD", "DOGE/USD", "PEPE/USD", "BONK/USD", "FLOKI/USD"]  # Meme coins
SELL_PROFIT_TARGET = 0.15  # USD profit target after fees
TRADE_AMOUNT_USD = 5  # Amount per trade
LOOP_DELAY = 6  # seconds between checks

# === KRAKEN API ===
api = krakenex.API()
api.load_key('kraken.key')

# === LOGGING ===
def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)

# === MARKET PRICE ===
def get_price(pair):
    try:
        resp = api.query_public('Ticker', {'pair': pair})
        return float(list(resp['result'].values())[0]['c'][0])
    except Exception as e:
        log(f"Error getting price for {pair}: {e}")
        return None

# === BALANCES ===
def get_balance():
    try:
        resp = api.query_private('Balance')
        return resp['result']
    except Exception as e:
        log(f"Error fetching balance: {e}")
        return {}

# === SELL COIN ===
def sell_coin(pair, volume):
    try:
        resp = api.query_private('AddOrder', {
            'pair': pair,
            'type': 'sell',
            'ordertype': 'market',
            'volume': volume
        })
        if 'error' in resp and resp['error']:
            log(f"Sell failed for {pair}: {resp['error']}")
        else:
            log(f"Sold {volume} {pair}")
    except Exception as e:
        log(f"Error selling {pair}: {e}")

# === BUY COIN ===
def buy_coin(pair, usd_amount):
    price = get_price(pair)
    if price is None:
        return
    volume = round(usd_amount / price, 0 if "USD" in pair else 8)
    try:
        resp = api.query_private('AddOrder', {
            'pair': pair,
            'type': 'buy',
            'ordertype': 'market',
            'volume': volume
        })
        if 'error' in resp and resp['error']:
            log(f"Buy failed for {pair}: {resp['error']}")
        else:
            log(f"Bought {volume} of {pair}")
    except Exception as e:
        log(f"Error buying {pair}: {e}")

# === CHECK AND SELL CURRENT HOLDINGS ON STARTUP ===
def clear_positions():
    balances = get_balance()
    for pair in PAIR_LIST:
        base = pair.split('/')[0]  # e.g., "SHIB"
        if base in balances and float(balances[base]) > 0:
            sell_coin(pair, balances[base])
            time.sleep(2)  # avoid API rate limit

# === MAIN LOOP ===
def main():
    log("Starting bot - Clearing existing positions...")
    clear_positions()
    log("Positions cleared. Starting trading...")

    bought_prices = {}

    while True:
        balances = get_balance()
        usd_balance = float(balances.get("ZUSD", 0))

        # SELL if target profit reached
        for pair in list(bought_prices.keys()):
            base = pair.split('/')[0]
            if base in balances and float(balances[base]) > 0:
                current_price = get_price(pair)
                buy_price = bought_prices[pair]
                profit = (current_price - buy_price) * float(balances[base])
                if profit >= SELL_PROFIT_TARGET:
                    sell_coin(pair, balances[base])
                    del bought_prices[pair]
                    time.sleep(2)

        # BUY new coins if we have USD
        if usd_balance >= TRADE_AMOUNT_USD:
            for pair in PAIR_LIST:
                if pair not in bought_prices:
                    buy_coin(pair, TRADE_AMOUNT_USD)
                    bought_prices[pair] = get_price(pair)
                    time.sleep(2)

        time.sleep(LOOP_DELAY)

if __name__ == "__main__":
    main()
