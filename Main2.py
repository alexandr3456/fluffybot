import asyncio
import os
import json
import logging
from datetime import datetime, timedelta

import pandas as pd
import pandas_ta_classic as ta
import ccxt
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# ===================== CONFIG =====================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTOBOT_API_TOKEN = os.getenv("CRYPTOBOT_API_TOKEN")

CHECK_INTERVAL = 5
COOLDOWN_MINUTES = 30
DATA_FILE = "data.json"
BYBIT_DELAY = 0.25
MAX_CONCURRENT = 20

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ===================== CRYPTOBOT =====================
cryptobot_client = None
pending_invoices = {}

if CRYPTOBOT_API_TOKEN:
    try:
        from cryptobot import CryptoBotClient, Asset
        cryptobot_client = CryptoBotClient(api_token=CRYPTOBOT_API_TOKEN)
        logger.info("✅ CryptoBot клиент подключён")
    except ImportError:
        logger.error("❌ pip install cryptobot-python")
    except Exception as e:
        logger.error(f"❌ CryptoBot error: {e}")
else:
    logger.warning("⚠️ CRYPTOBOT_API_TOKEN не найден")

SUBSCRIPTION_PLANS = {
    "2_weeks": {"name": "2 недели", "price": 5, "days": 14},
    "1_month": {"name": "1 месяц", "price": 10, "days": 30},
    "3_months": {"name": "3 месяца", "price": 25, "days": 90}
}

# ===================== BOT =====================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

subscribers = {}
last_signals = {}

# ===================== STORAGE =====================
def load_data():
    global subscribers
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
                subscribers = {
                    int(k): {
                        "expiry_date": datetime.fromisoformat(v["expiry_date"]),
                        "status": v["status"]
                    }
                    for k, v in data.get("subscribers", {}).items()
                }
            logger.info(f"Загружено {len(subscribers)} подписчиков")
        except Exception as e:
            logger.error(f"Ошибка загрузки data.json: {e}")

def save_data():
    try:
        data = {
            str(k): {
                "expiry_date": v["expiry_date"].isoformat(),
                "status": v["status"]
            }
            for k, v in subscribers.items()
        }
        with open(DATA_FILE, "w") as f:
            json.dump({"subscribers": data}, f, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

# ===================== EXCHANGE (исправлено) =====================
exchange = None
sem = None

def init_exchange():
    global exchange, sem
    connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
    session = aiohttp.ClientSession(connector=connector)
    
    exchange = ccxt.bybit({
        "enableRateLimit": True,
        "options": {
            "defaultType": "future",
            "http": {"aiohttp_session": session}
        }
    })
    sem = asyncio.Semaphore(MAX_CONCURRENT)

# ===================== HELPER =====================
def is_subscribed(chat_id: int) -> bool:
    if chat_id not in subscribers:
        return False
    sub = subscribers[chat_id]
    return sub["status"] == "active" and sub["expiry_date"] > datetime.now()

# ===================== COMMANDS =====================
# ... (твои хендлеры start, pay, stop и т.д. — оставь как были)

# ===================== CORE =====================
async def process_symbol(symbol):
    async with sem:
        await asyncio.sleep(BYBIT_DELAY)
        # твой код process_symbol...
        try:
            now = datetime.now()
            if symbol in last_signals and now - last_signals[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                return None

            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, timeframe="5m", limit=50, params={"category": "linear"})
            if not ohlcv:
                return None

            df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
            df["close"] = pd.to_numeric(df["close"], errors='coerce')
            df["volume"] = pd.to_numeric(df["volume"], errors='coerce')
            df["rsi"] = ta.rsi(df["close"], length=14)
            df["ema50"] = ta.ema(df["close"], length=50)

            # funding и oi тоже можно оставить
            funding = 0
            oi = 0
            # ... остальной код get_signal и возврат сигнала
        except Exception as e:
            logger.error(f"process_symbol {symbol}: {e}")
        return None


async def scan_market():
    # ... твой код scan_market
    pass


async def check_pending_payments():
    # ... твой код проверки платежей
    pass


# ===================== MAIN =====================
async def main():
    load_data()
    init_exchange()                    # ← Важно! Инициализируем здесь
    logger.info("🚀 Бот запущен")

    await bot.delete_webhook(drop_pending_updates=True)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_market, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(check_pending_payments, "interval", seconds=40)
    scheduler.start()

    asyncio.create_task(asyncio.sleep(3) or scan_market())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
