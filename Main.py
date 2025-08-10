import os
import time
from datetime import datetime
from krakenex import API
from decimal import Decimal, ROUND_DOWN

# ===============================
# CONFIG
# ===============================
TARGET_PROFIT = Decimal("0.05")  # Minimum profit after fees
STOP_LOSS_PERCENT = Decimal("-3.0")  # % loss before selling
STOP_LOSS_HOURS = 2  # Minimum hold before stop-loss can trigger
TRADE_AMOUNT_USD = Decimal("5.00")  # Amount per trade
SCAN_INTERVAL = 10  # Seconds between scans

# Kraken API
kraken = API(
    key=os.getenv("KRAKEN_API_KEY"),
    secret=os.getenv("KRAKEN_API_SECRET")
)

portfolio = {}
restricted_coins = set()

def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ===============================
# CHECK FOR RESTRICTED COINS
# ===============================
def is_coin_restricted(pair):
    if pair in restricted_coins:
        return True
    try:
        order_test = kraken.query_private('AddOrder', {
            'pair': pair,
            'type': 'buy',
            'ordertype': 'market',
            'volume': '0.00001'
        })
        if 'error' in order_test and any("restricted" in e.lower() for e in order_test['error']):
            restricted_coins.add(pair)
            log(f"Restricted detected: {pair} — skipping")
            return True
    except Exception as e:
        if "restricted" in str(e).lower():
            restricted_coins.add(pair)
            log(f"Restricted detected: {pair} — skipping")
            return True
    return False

# ===============================
# GET MARKET PRICE
# ===============================
def get_price(pair):
    try:
        data = kraken.query_public('Ticker', {'pair': pair})
        price = Decimal(data['result'][list(data['result'].keys())[0]]['c'][0])
        return price
    except Exception as e:
        log(f"Price fetch failed for {pair}: {e}")
        return None

# ===============================
# PLACE BUY ORDER
# ===============================
def execute_buy(pair, usd_amount):
    price = get_price(pair)
    if not price:
        return False
    volume = (usd_amount / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    try:
        order = kraken.query_private('AddOrder', {
            'pair': pair,
            'type': 'buy',
            'ordertype': 'market',
            'volume': str(volume)
        })
        if 'error' in order and order['error']:
            log(f"Buy failed for {pair}: {order['error']}")
            return False
        portfolio[pair] = {
            'buy_price': price,
            'buy_time': datetime.utcnow(),
            'amount': volume
        }
        log(f"Bought {volume} {pair} @ ${price}")
        return True
    except Exception as e:
        log(f"Buy order error for {pair}: {e}")
        return False

# ===============================
# PLACE SELL ORDER
# ===============================
def execute_sell(pair, amount):
    try:
        order = kraken.query_private('AddOrder', {
            'pair': pair,
            'type': 'sell',
            'ordertype': 'market',
            'volume': str(amount)
        })
        if 'error' in order and order['error']:
            log(f"Sell failed for {pair}: {order['error']}")
            return False
        log(f"Sold {amount} {pair}")
        portfolio.pop(pair, None)
        return True
    except Exception as e:
        log(f"Sell order error for {pair}: {e}")
        return False

# ===============================
# SELL DECISION
# ===============================
def should_sell(pair, buy_price, current_price, buy_time):
    net_profit = (current_price - buy_price) * portfolio[pair]['amount']
    hold_time_hours = (datetime.utcnow() - buy_time).total_seconds() / 3600

    if net_profit >= TARGET_PROFIT:
        return True, f"Target profit reached: ${net_profit:.4f}"
    if hold_time_hours >= STOP_LOSS_HOURS:
        pct_change = ((current_price - buy_price) / buy_price) * 100
        if pct_change <= STOP_LOSS_PERCENT:
            return True, f"Stop-loss triggered: {pct_change:.2f}%"
    return False, f"Holding — Profit ${net_profit:.4f}"

# ===============================
# GET MEME COINS LIST
# ===============================
def get_meme_coins():
    return ["SHIB/USD", "DOGE/USD", "PEPE/USD", "BONK/USD", "FLOKI/USD"]

# ===============================
# MAIN LOOP
# ===============================
while True:
    try:
        for coin in get_meme_coins():
            if is_coin_restricted(coin):
                continue

            price = get_price(coin)
            if not price:
                continue

            if coin in portfolio:
                buy_price = portfolio[coin]['buy_price']
                buy_time = portfolio[coin]['buy_time']
                sell, reason = should_sell(coin, buy_price, price, buy_time)
                if sell:
                    execute_sell(coin, portfolio[coin]['amount'])
                else:
                    log(f"{coin}: {reason}")
            else:
                execute_buy(coin, TRADE_AMOUNT_USD)

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        log(f"Main loop error: {e}")
        time.sleep(5)
