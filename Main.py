import os
import time
import krakenex

# API Keys from Heroku Config Vars
api_key = os.getenv("KRAKEN_API_KEY")
api_secret = os.getenv("KRAKEN_API_SECRET")
if not api_key or not api_secret:
    raise ValueError("Missing Kraken API keys in Heroku Config Vars")

api = krakenex.API(api_key, api_secret)

# Constants
FEE_RATE = 0.0026       # Kraken fee ~0.26%
TARGET_PROFIT = 0.15    # USD profit target after fees
STOP_LOSS = 0.05        # Sell if price drops more than 5% from buy price
SIDEWAYS_LIMIT = 600    # 10 minutes in seconds
SIDEWAYS_THRESHOLD = 0.01  # ±1% price range counts as "sideways"
SLEEP_INTERVAL = 5
MEME_COINS = ["SHIBUSD", "DOGEUSD", "PEPEUSD", "BONKUSD", "FLOKIUSD"]

# --- Helpers ---
def get_balances():
    res = api.query_private('Balance')
    if res.get("error"):
        print("Balance Error:", res["error"])
        return {}
    return {asset: float(amount) for asset, amount in res['result'].items() if float(amount) > 0}

def sell_all_except_usd():
    balances = get_balances()
    for asset, amount in balances.items():
        if asset != "ZUSD":
            if asset.startswith("X") or asset.startswith("Z"):
                pair = asset[1:] + "USD"
            else:
                pair = asset + "USD"
            pair = pair.replace("XBT", "BTC")
            try:
                print(f"Selling {amount} {asset}...")
                api.query_private('AddOrder', {
                    'pair': pair,
                    'type': 'sell',
                    'ordertype': 'market',
                    'volume': amount
                })
            except Exception as e:
                print(f"Error selling {asset}: {e}")

def get_price(pair):
    res = api.query_public('Ticker', {'pair': pair})
    if res.get("error"):
        print("Price Error:", res["error"])
        return None
    return float(list(res['result'].values())[0]['c'][0])

def buy_coin(pair, usd_amount):
    price = get_price(pair)
    if not price:
        return None
    volume = usd_amount / price
    api.query_private('AddOrder', {
        'pair': pair,
        'type': 'buy',
        'ordertype': 'market',
        'volume': volume
    })
    print(f"Bought {pair} at {price} with ${usd_amount}")
    return price

def trade_coin(pair, usd_amount):
    buy_price = buy_coin(pair, usd_amount)
    if not buy_price:
        return
    target_price = buy_price * (1 + TARGET_PROFIT)
    stop_price = buy_price * (1 - STOP_LOSS)
    sideways_low = buy_price * (1 - SIDEWAYS_THRESHOLD)
    sideways_high = buy_price * (1 + SIDEWAYS_THRESHOLD)

    start_time = time.time()

    while True:
        price = get_price(pair)
        if not price:
            time.sleep(SLEEP_INTERVAL)
            continue

        # Take profit
        if price >= target_price:
            balances = get_balances()
            asset_symbol = pair.replace("USD", "")
            vol = balances.get(asset_symbol, 0)
            if vol > 0:
                api.query_private('AddOrder', {
                    'pair': pair,
                    'type': 'sell',
                    'ordertype': 'market',
                    'volume': vol
                })
                print(f"✅ Sold {pair} at {price} for profit.")
            break

        # Stop-loss
        if price <= stop_price:
            balances = get_balances()
            asset_symbol = pair.replace("USD", "")
            vol = balances.get(asset_symbol, 0)
            if vol > 0:
                api.query_private('AddOrder', {
                    'pair': pair,
                    'type': 'sell',
                    'ordertype': 'market',
                    'volume': vol
                })
                print(f"⚠️ Stop-loss triggered for {pair}, sold at {price}")
            break

        # Sideways trading timeout
        if sideways_low <= price <= sideways_high and (time.time() - start_time) >= SIDEWAYS_LIMIT:
            balances = get_balances()
            asset_symbol = pair.replace("USD", "")
            vol = balances.get(asset_symbol, 0)
            if vol > 0:
                api.query_private('AddOrder', {
                    'pair': pair,
                    'type': 'sell',
                    'ordertype': 'market',
                    'volume': vol
                })
                print(f"⏳ Sideways timeout: Sold {pair} at {price} after {SIDEWAYS_LIMIT} seconds of no movement.")
            break

        time.sleep(SLEEP_INTERVAL)

# --- Main loop ---
if __name__ == "__main__":
    print("Starting perpetual trading bot with 70-30 split, stop-loss, and sideways timeout...")

    while True:
        print("Selling all current coins to free funds...")
        sell_all_except_usd()
        time.sleep(5)

        balances = get_balances()
        usd_balance = balances.get("ZUSD", 0)
        if usd_balance <= 0:
            print("No USD available for trading. Retrying...")
            time.sleep(30)
            continue

        invest_amount = usd_balance * 0.70
        reserve_amount = usd_balance * 0.30

        print(f"Total USD: ${usd_balance:.2f} | Invest: ${invest_amount:.2f} | Reserve: ${reserve_amount:.2f}")

        per_coin_investment = invest_amount / len(MEME_COINS)

        for coin in MEME_COINS:
            trade_coin(coin, per_coin_investment)

        print("Cycle complete. Restarting cycle...")
        time.sleep(5)
