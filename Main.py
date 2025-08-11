import os
import time
import krakenex
from pykrakenapi import KrakenAPI
from datetime import datetime, timezone
from stats_tracker import (
    load_stats, log_profit, log_dust,
    log_tradeable, log_reserve, maybe_write_daily_summary
)

# ====== CONFIG ======
PROFIT_TARGET = 0.04  # 4% profit target
LOOP_DELAY = 30       # seconds between checks
RESERVE_RATIO = 0.30  # 30% kept in reserve
MEME_COINS = ["SHIB/USD", "PEPE/USD", "RIZZ/USD", "RIZE/USD"]  # Example set

# ====== INIT KRAKEN ======
api = krakenex.API(
    key=os.getenv("KRAKEN_API_KEY"),
    secret=os.getenv("KRAKEN_API_SECRET")
)
k = KrakenAPI(api)

stats = load_stats()

# ====== FUNCTIONS ======
def get_usd_balance():
    balances = k.get_account_balance()
    return float(balances.get("ZUSD", 0))

def get_price(pair):
    ohlc, _ = k.get_ohlc_data(pair, interval=1)
    return float(ohlc["close"].iloc[-1])

def buy(pair, usd_amount):
    price = get_price(pair)
    volume = usd_amount / price
    try:
        resp = api.query_private("AddOrder", {
            "pair": pair.replace("/", ""),
            "type": "buy",
            "ordertype": "market",
            "volume": str(volume)
        })
        if resp.get("error"):
            print(f"[BUY FAILED] {pair}: {resp['error']}")
        else:
            print(f"[BUY] {pair} -> ${usd_amount:.2f}")
    except Exception as e:
        print(f"[BUY ERROR] {e}")

def sell(pair, volume, buy_price):
    sell_price = get_price(pair)
    profit_pct = (sell_price - buy_price) / buy_price
    if profit_pct >= PROFIT_TARGET:
        try:
            resp = api.query_private("AddOrder", {
                "pair": pair.replace("/", ""),
                "type": "sell",
                "ordertype": "market",
                "volume": str(volume)
            })
            if resp.get("error"):
                print(f"[SELL FAILED] {pair}: {resp['error']}")
            else:
                profit_usd = (sell_price - buy_price) * volume
                log_profit(stats, profit_usd)
                print(f"[SELL] {pair} -> Profit ${profit_usd:.2f}")
        except Exception as e:
            print(f"[SELL ERROR] {e}")

# ====== MAIN LOOP ======
portfolio = {}  # pair -> {"buy_price": float, "volume": float}

while True:
    try:
        usd_balance = get_usd_balance()
        tradeable = usd_balance * (1 - RESERVE_RATIO)
        reserve = usd_balance * RESERVE_RATIO

        log_tradeable(stats, tradeable)
        log_reserve(stats, reserve)

        print(f"[{datetime.now(timezone.utc)}] [POOL] Total USD ${usd_balance:.2f} | "
              f"Tradeable ${tradeable:.2f} | Reserve ${reserve:.2f}")

        # Buy logic
        for coin in MEME_COINS:
            if coin not in portfolio and tradeable > 5:  # minimum $5
                buy_amt = tradeable / len(MEME_COINS)
                buy(coin, buy_amt)
                portfolio[coin] = {
                    "buy_price": get_price(coin),
                    "volume": buy_amt / get_price(coin)
                }

        # Sell logic
        for coin, data in list(portfolio.items()):
            sell(coin, data["volume"], data["buy_price"])
            # Remove from portfolio if sold
            current_price = get_price(coin)
            if (current_price - data["buy_price"]) / data["buy_price"] >= PROFIT_TARGET:
                del portfolio[coin]

        # Dust cleanup (auto-sell tiny leftover balances)
        balances = k.get_account_balance()
        for asset, amount in balances.items():
            if asset != "ZUSD" and float(amount) > 0:
                try:
                    pair = asset + "USD"
                    vol = float(amount)
                    resp = api.query_private("AddOrder", {
                        "pair": pair,
                        "type": "sell",
                        "ordertype": "market",
                        "volume": str(vol)
                    })
                    if not resp.get("error"):
                        dust_value = vol * get_price(pair)
                        log_dust(stats, dust_value)
                        print(f"[DUST SOLD] {pair} -> ${dust_value:.2f}")
                except:
                    pass

        # Daily summary
        maybe_write_daily_summary(stats)

        time.sleep(LOOP_DELAY)

    except Exception as e:
        print(f"[ERROR] {e}")
        time.sleep(LOOP_DELAY)
