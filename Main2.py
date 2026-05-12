import asyncio
import os
import json
import logging
from datetime import datetime, timedelta
import uvicorn
import pandas as pd
import pandas_ta_classic as ta
import ccxt
import cryptobot

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from cryptobot import WebhookListener
from fastapi import FastAPI, Request
# ===================== CONFIG =====================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTOBOT_API_TOKEN = os.getenv("CRYPTOBOT_API_TOKEN")

if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN missing")
if not CRYPTOBOT_API_TOKEN:
    logger.warning("⚠️ CRYPTOBOT_API_TOKEN missing! Платежи не будут работать.")

CHECK_INTERVAL = 5  # minutes
COOLDOWN_MINUTES = 30
DATA_FILE = "data.json"

try:
    from cryptobot import CryptoBotClient, Asset
    cryptobot_client = CryptoBotClient(api_token=CRYPTOBOT_API_TOKEN)
    logger.info("✅ CryptoBot client initialized")
except ImportError:
    logger.error("❌ Установи библиотеку: pip install cryptobot-python")
    cryptobot_client = None

# Тарифы подписки
SUBSCRIPTION_PLANS = {
    "2_weeks": {"name": "2 недели", "price": "5$", "days": 14},
    "1_month": {"name": "1 месяц", "price": "10$", "days": 30},
    "3_months": {"name": "3 месяца", "price": "25$", "days": 90}
}

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ===================== BOT =====================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

subscribers = {}  # {chat_id: {"expiry_date": datetime, "status": "active"}}
last_signals = {}

# ===================== STORAGE =====================

def load_data():
    global subscribers
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                # Преобразуем строки дат обратно в datetime
                subscribers = {
                    int(k): {
                "expiry_date": datetime.fromisoformat(v["expiry_date"]),
                "status": v["status"]
            }
            for k, v in data.get("subscribers", {}).items()
        }
            # СТРОКА ЛОГИРОВАНИЯ ТЕПЕРЬ НА СВОЁМ МЕСТЕ — внутри try, после загрузки данных
            logger.info(f"Loaded {len(subscribers)} subscribers")
        except Exception as e:
            logger.error(f"Failed to load data file: {e}")
    else:
        logger.info("No data file found, starting fresh")
def save_data():
    try:
        # Преобразуем datetime в строки для JSON
        serializable_subcribers = {
            k: {
                "expiry_date": v["expiry_date"].isoformat(),
                "status": v["status"]
            }
            for k, v in subscribers.items()
        }

        with open(DATA_FILE, "w") as f:
            json.dump({"subcribers": serializable_subcribers}, f)
    except Exception as e:
        logger.error(f"Failed to save data file: {e}")

# ===================== EXCHANGE =====================
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

# ===================== COMMANDS =====================
@dp.message(Command("start"))
async def start(message: Message):
    chat_id = message.chat.id
    current_date = datetime.now()

    # Проверяем статус подписки
    if chat_id in subscribers:
        sub_data = subscribers[chat_id]
        if sub_data["status"] == "active" and sub_data["expiry_date"] > current_date:
            await message.answer("✅ Вы уже подписаны! Ожидайте сигналов.")
            return

    welcome_text = (
        "Привет я Флафи, твой милый помощник в этом мрачном мире, "
        "я буду давать тебе сигналы на шорт. "
        "Для включения подписки оплатите тариф — /pay"
    )
    await message.answer(welcome_text)

@dp.message(Command("pay"))
async def pay(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{plan['name']} — {plan['price']}",
                callback_data=plan_key
            )
            for plan_key, plan in SUBSCRIPTION_PLANS.items()
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_start")]
    ])

    tariff_text = "💳 Выберите тариф подписки:\n\n"
    for plan_key, plan in SUBSCRIPTION_PLANS.items():
        tariff_text += f"• <b>{plan['name']}</b> — {plan['price']}\n"

    tariff_text += "\nПосле оплаты вы получите доступ к сигналам на шорт."

    await message.answer(tariff_text, parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(F.data.in_(SUBSCRIPTION_PLANS.keys()))
async def handle_tariff_selection(callback):
    if not cryptobot_client:
        await callback.message.answer("❌ Платежная система временно недоступна.")
        await callback.answer()
        return

    plan_key = callback.data
    plan = SUBSCRIPTION_PLANS[plan_key]
    chat_id = callback.from_user.id

    try:
        # Создаём invoice в USDT
        invoice = cryptobot_client.create_invoice(
            asset=Asset.USDT,           # Можно TON, BTC и т.д.
            amount=plan["price"].replace("$", ""),  # "5", "10", "25"
            description=f"Подписка Fluffy Signals — {plan['name']}",
            payload=f"{chat_id}:{plan_key}",   # Важно! Чтобы знать кто и что оплатил
            # expired_in=3600,                 # можно добавить
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="💳 Оплатить USDT",
                url=invoice.bot_invoice_url
            )
        ]])

        await callback.message.edit_text(
            f"💎 Оплата подписки <b>{plan['name']}</b> — {plan['price']}\n\n"
            f"Перейди по ссылке ниже и оплати:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"CryptoBot invoice error: {e}")
        await callback.message.answer("❌ Ошибка создания счёта. Попробуй позже.")

    await callback.answer()

@dp.message(Command("stop"))
async def stop(message: Message):
    chat_id = message.chat.id

    if chat_id in subscribers:
        del subscribers[chat_id]
        save_data()
        await message.answer("❌ Подписка отключена. Если передумаете — /start")
    else:
        await message.answer("У вас нет активной подписки.")


# Обработчик для всех остальных сообщений
@dp.message()
async def handle_all(message: Message):
    chat_id = message.chat.id
    current_date = datetime.now()

    # Проверяем подписку перед ответом
    if chat_id not in subscribers or subscribers[chat_id]["expiry_date"] < current_date:
        await message.answer("Для использования бота нужна активная подписка — /pay")
        return

    text = message.text
    if text == "/start":
        await start(message)
    elif text == "/stop":
        await stop(message)
    elif text == "/pay":
        await pay(message)
    else:
        await message.answer("Я работаю 👍")

# ===================== CORE =====================

def calculate_indicators(df):
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["ema50"] = ta.ema(df["close"], length=50)
    return df

def get_signal(df, funding_rate, open_interest):
    price = df["close"].iloc[-1]
    ema = df["ema50"].iloc[-1]
    rsi = df["rsi"].iloc[-1]

    price_change = (df["close"].iloc[-1] / df["close"].iloc[-4] - 1) * 100

    avg_vol = df["volume"].rolling(20).mean().iloc[-2]
    cur_vol = df["volume"].iloc[-1]
    volume_spike = cur_vol > avg_vol * 1.8 if avg_vol else False

    far_from_ema = price > ema * 1.04 if ema else False
    last_red = df["close"].iloc[-1] < df["open"].iloc[-1]

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
        "volume_ratio": (cur_vol / avg_vol) if avg_vol else 0,
        "ema_distance": ((price / ema - 1) * 100) if ema else 0,
        "funding": funding_rate,
        "oi": open_interest
    }

async def fetch_ohlcv_async(symbol):
    try:
        return await asyncio.to_thread(
            exchange.fetch_ohlcv,
            symbol,
            timeframe="5m",
            limit=50,
            params={"category": "linear"}
        )
    except Exception as e:
        logger.error(f"fetch_ohlcv_async error for {symbol}: {e}")
        return []

async def fetch_funding(symbol):
    try:
        data = await asyncio.to_thread(exchange.fetch_funding_rate, symbol)
        return data.get("fundingRate", 0)
    except Exception as e:
        logger.error(f"fetch_funding error for {symbol}: {e}")
        return 0

async def fetch_oi(symbol):
    try:
        data = await asyncio.to_thread(exchange.fetch_open_interest, symbol, params={"category": "linear"})
        return float(data.get("openInterest", 0))
    except Exception as e:
        logger.error(f"fetch_oi error for {symbol}: {e}")
        return 0

async def process_symbol(symbol):
    try:
        now = datetime.now()
        if symbol in last_signals and now - last_signals[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
            return None

        ohlcv = await fetch_ohlcv_async(symbol)
        if not ohlcv:
            return None

        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        df["close"] = pd.to_numeric(df["close"], errors='coerce')
        df["volume"] = pd.to_numeric(df["volume"], errors='coerce')

        df = calculate_indicators(df)

        funding = await fetch_funding(symbol)
        oi = await fetch_oi(symbol)

        score, data = get_signal(df, funding, oi)

        if score >= 7:
            last_signals[symbol] = now
            return symbol, score, data

    except Exception as e:
        logger.error(f"process_symbol error for {symbol}: {e}")

    return None

async def scan_market():
    logger.info("🔍 Scan start")
    try:
        current_date = datetime.now()
        # Фильтруем активных подписчиков
        active_subcribers = [
            user_id for user_id, data in subscribers.items()
            if data["status"] == "active" and data["expiry_date"] > current_date
        ]

        if not active_subcribers:
            logger.info("No active subscribers, skipping scan")
            return

        markets = await asyncio.to_thread(exchange.load_markets)
        symbols = [
            s for s, i in markets.items()
            if i.get("linear") and i.get("quote") == "USDT" and i.get("active", True)
        ]

        # Ограничение на скорость
        tasks = [process_symbol(s) for s in symbols[:100]]
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

            for user in active_subcribers:
                try:
                    await bot.send_message(user, text, parse_mode="HTML", disable_web_page_preview=True)
                except Exception as e:
                    logger.warning(f"Failed to send message to {user}: {e}")

        logger.info(f"✅ Signals sent: {len(signals)} to {len(active_subcribers)} users")

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

    # Запуск сканирования сразу с небольшой задержкой
    async def delayed_scan():
        await asyncio.sleep(2)
        await scan_market()

    asyncio.create_task(delayed_scan())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())