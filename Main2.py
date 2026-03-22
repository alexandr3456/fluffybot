import asyncio
import os
import json
import logging
from datetime import datetime, timedelta

import pandas as pd
import pandas_ta_classic as ta
import ccxt

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# ===================== CONFIG =====================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN missing")

CHECK_INTERVAL = 5  # minutes
COOLDOWN_MINUTES = 30
DATA_FILE = "data.json"

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ===================== BOT =====================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

subscribers = set()
last_signals = {}

# ===================== STORAGE =====================
def load_data():
    global subscribers
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            subscribers = set(data.get("subscribers", []))


def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump({"subscribers": list(subscribers)}, f)


# ===================== EXCHANGE =====================
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})


# ===================== COMMANDS =====================
@dp.message()
async def handle_all(message: Message):
    if message.text == "/start":
        subscribers.add(message.chat.id)
        save_data()
        await message.answer("✅ Подписка включена")

    elif message.text == "/stop":
        subscribers.discard(message.chat.id)
        save_data()
        await message.answer("❌ Подписка отключена")

    elif message.text == "/status":
        await message.answer(f"👥 Подписчиков: {len(subscribers)}")

    else:
        await message.answer("Я работаю 👍")

@dp.message(Command("stop"))
async def stop(message: Message):
    subscribers.discard(message.chat.id)
    save_data()
    await message.answer("❌ Подписка отключена")


@dp.message(Command("status"))
async def status(message: Message):
    await message.answer(f"👥 Подписчиков: {len(subscribers)}\n⚙️ Интервал: {CHECK_INTERVAL} мин")


# ===================== CORE =====================

def calculate_indicators(df):
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["ema50"] = ta.ema(df["close"], length=50)
    return df


def get_signal(df, funding_rate, open_interest):
    price = df["close"].iloc[-1]
    ema = df["ema50"].iloc[-1]
    rsi = df["rsi"].iloc[-1]

    # рост за 15 минут
    price_change = (df["close"].iloc[-1] / df["close"].iloc[-4] - 1) * 100

    # объем
    avg_vol = df["volume"].rolling(20).mean().iloc[-2]
    cur_vol = df["volume"].iloc[-1]
    volume_spike = cur_vol > avg_vol * 1.8

    # перегрев
    far_from_ema = price > ema * 1.04

    # слабость
    last_red = df["close"].iloc[-1] < df["open"].iloc[-1]

    # ================= SCORE =================
    score = 0

    if price_change > 3:
        score += 2

    if rsi > 75:
        score += 2

    if volume_spike:
        score += 2

    if far_from_ema:
        score += 1

    if last_red:
        score += 1

    if funding_rate > 0.01:
        score += 2

    if open_interest > 0:
        score += 1

    return score, {
        "price_change": price_change,
        "rsi": rsi,
        "volume_ratio": cur_vol / avg_vol if avg_vol else 0,
        "ema_distance": (price / ema - 1) * 100 if ema else 0,
        "funding": funding_rate,
        "oi": open_interest
    }


async def fetch_funding(symbol):
    try:
        data = exchange.fetch_funding_rate(symbol)
        return data["fundingRate"]
    except:
        return 0


async def fetch_oi(symbol):
    try:
        data = exchange.fetch_open_interest(symbol, params={"category": "linear"})
        return float(data["openInterest"])
    except:
        return 0


async def process_symbol(symbol):
    try:
        # cooldown
        now = datetime.now()
        if symbol in last_signals:
            if now - last_signals[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                return None

        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=50, params={"category": "linear"})
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])

        df["close"] = pd.to_numeric(df["close"])
        df["volume"] = pd.to_numeric(df["volume"])

        df = calculate_indicators(df)

        funding = await fetch_funding(symbol)
        oi = await fetch_oi(symbol)

        score, data = get_signal(df, funding, oi)

        if score >= 7:
            last_signals[symbol] = now
            return symbol, score, data

    except Exception as e:
        logger.warning(f"{symbol}: {e}")

    return None


async def scan_market():
    logger.info("🔍 Scan start")

    try:
        markets = exchange.load_markets()
        symbols = [
            s for s, i in markets.items()
            if i.get("linear") and i.get("quote") == "USDT" and i.get("active", True)
        ]

        tasks = [process_symbol(s) for s in symbols[:100]]  # ограничение для скорости
        results = await asyncio.gather(*tasks)

        signals = [r for r in results if r]

        for symbol, score, d in signals:
            token = symbol.replace("USDT", "")

            text = f"""
🚨 <b>SHORT SIGNAL</b> — ${token}

🔥 Score: <b>{score}/10</b>

📈 Рост: {d['price_change']:.2f}%
📉 RSI: {d['rsi']:.1f}
📊 Volume: x{d['volume_ratio']:.1f}
📐 EMA dist: {d['ema_distance']:.1f}%

💰 Funding: {d['funding']:.4f}
📊 OI: {d['oi']:.0f}

🕒 {datetime.now().strftime('%H:%M:%S')}

🔗 https://www.bybit.com/trade/perpetual/{symbol}
"""

            for user in subscribers:
                try:
                    await bot.send_message(user, text, parse_mode="HTML", disable_web_page_preview=True)
                except:
                    pass

        logger.info(f"✅ Signals: {len(signals)}")

    except Exception as e:
        logger.error(f"Scan error: {e}")


# ===================== MAIN =====================
async def main():
    load_data()
    logger.info("🚀 Bot started")

    await bot.delete_webhook(drop_pending_updates=True)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_market, "interval", minutes=CHECK_INTERVAL)
    scheduler.start()

   # asyncio.create_task(scan_market())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())