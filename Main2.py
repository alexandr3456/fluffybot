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

CHECK_INTERVAL = 5          # minutes
COOLDOWN_MINUTES = 30
DATA_FILE = "data.json"
BYBIT_DELAY = 0.25
MAX_CONCURRENT = 20         # ограничение параллельных запросов

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ===================== CRYPTOBOT =====================
cryptobot_client = None
pending_invoices = {}  # {invoice_id: {"chat_id": int, "plan_key": str}}

if CRYPTOBOT_API_TOKEN:
    try:
        from cryptobot import CryptoBotClient, Asset
        cryptobot_client = CryptoBotClient(api_token=CRYPTOBOT_API_TOKEN)
        logger.info("✅ CryptoBot клиент успешно подключён")
    except ImportError:
        logger.error("❌ Установи библиотеку: pip install cryptobot-python")
    except Exception as e:
        logger.error(f"❌ CryptoBot init error: {e}")
else:
    logger.warning("⚠️ CRYPTOBOT_API_TOKEN не найден в .env")

# Тарифы
SUBSCRIPTION_PLANS = {
    "2_weeks": {"name": "2 недели", "price": 5, "days": 14},
    "1_month": {"name": "1 месяц", "price": 10, "days": 30},
    "3_months": {"name": "3 месяца", "price": 25, "days": 90}
}

# ===================== BOT =====================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

subscribers = {}      # {chat_id: {"expiry_date": datetime, "status": "active"}}
last_signals = {}

# ===================== STORAGE =====================
def load_data():
    global subscribers
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                subscribers = {
                    int(k): {
                        "expiry_date": datetime.fromisoformat(v["expiry_date"]),
                        "status": v["status"]
                    }
                    for k, v in data.get("subscribers", {}).items()
                }
            logger.info(f"✅ Загружено {len(subscribers)} подписчиков")
        except Exception as e:
            logger.error(f"Ошибка загрузки data.json: {e}")
    else:
        logger.info("📁 data.json не найден, начинаем с нуля")

def save_data():
    try:
        serializable = {
            str(k): {
                "expiry_date": v["expiry_date"].isoformat(),
                "status": v["status"]
            }
            for k, v in subscribers.items()
        }
        with open(DATA_FILE, "w") as f:
            json.dump({"subscribers": serializable}, f, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения data.json: {e}")

# ===================== EXCHANGE =====================
connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {
        "defaultType": "future",
        "http": {"aiohttp_session": aiohttp.ClientSession(connector=connector)}
    }
})

sem = asyncio.Semaphore(MAX_CONCURRENT)

# ===================== COMMANDS =====================
@dp.message(Command("start"))
async def start(message: Message):
    chat_id = message.chat.id
    if is_subscribed(chat_id):
        await message.answer("✅ У вас активная подписка! Ожидайте сигналов.")
        return

    await message.answer(
        "Привет! Я Флафи — бот с сигналами на шорт.\n\n"
        "Чтобы получать сигналы, нужно оформить подписку — /pay"
    )

@dp.message(Command("pay"))
async def pay(message: Message):
    if not cryptobot_client:
        return await message.answer("❌ Платежная система временно недоступна.")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{p['name']} — ${p['price']}", callback_data=key)]
        for key, p in SUBSCRIPTION_PLANS.items()
    ])

    text = "💳 Выберите тариф подписки:\n\n" + \
           "\n".join([f"• <b>{p['name']}</b> — ${p['price']}" for p in SUBSCRIPTION_PLANS.values()])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(F.data.in_(SUBSCRIPTION_PLANS.keys()))
async def handle_payment(callback):
    plan_key = callback.data
    plan = SUBSCRIPTION_PLANS[plan_key]
    chat_id = callback.from_user.id

    try:
        invoice = cryptobot_client.create_invoice(
            asset="USDT",
            amount=plan["price"],
            description=f"Fluffy Signals — {plan['name']}",
            payload=f"{chat_id}:{plan_key}"
        )

        pending_invoices[invoice.invoice_id] = {
            "chat_id": chat_id,
            "plan_key": plan_key
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💰 Оплатить USDT", url=invoice.bot_invoice_url)
        ]])

        await callback.message.edit_text(
            f"✅ Счёт на {plan['price']}$ создан!\n\n"
            f"Тариф: <b>{plan['name']}</b>\n"
            f"Оплатите по кнопке ниже 👇",
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Invoice error: {e}")
        await callback.message.answer("❌ Ошибка создания счёта.")

    await callback.answer()

@dp.message(Command("stop"))
async def stop_cmd(message: Message):
    chat_id = message.chat.id
    if chat_id in subscribers:
        del subscribers[chat_id]
        save_data()
        await message.answer("❌ Подписка отключена.")
    else:
        await message.answer("У вас нет активной подписки.")

def is_subscribed(chat_id: int) -> bool:
    if chat_id not in subscribers:
        return False
    sub = subscribers[chat_id]
    return sub["status"] == "active" and sub["expiry_date"] > datetime.now()

# ===================== CORE FUNCTIONS =====================
# ... (calculate_indicators, get_signal, fetch_ohlcv_async, fetch_funding, fetch_oi — оставил без изменений)

async def process_symbol(symbol):
    async with sem:
        await asyncio.sleep(BYBIT_DELAY)
        # ... (твой оригинальный код process_symbol без изменений)
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
            logger.error(f"process_symbol error {symbol}: {e}")
        return None


async def scan_market():
    logger.info("🔍 Scan start")
    try:
        active_users = [uid for uid, data in subscribers.items() 
                       if data["status"] == "active" and data["expiry_date"] > datetime.now()]

        if not active_users:
            logger.info("Нет активных подписчиков")
            return

        markets = await asyncio.to_thread(exchange.load_markets)
        symbols = [s for s, info in markets.items() 
                  if info.get("linear") and info.get("quote") == "USDT" and info.get("active")]

        tasks = [process_symbol(s) for s in symbols[:100]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = [r for r in results if isinstance(r, tuple)]

        for symbol, score, d in signals:
            token = symbol.replace("USDT", "")
            text = f"""🚨 <b>SHORT SIGNAL</b> — ${token}
🔥 Score: <b>{score}/10</b>
📈 Рост: {d['price_change']:.2f}%
📉 RSI: {d['rsi']:.1f}
📊 Volume: x{d['volume_ratio']:.1f}
📐 EMA dist: {d['ema_distance']:.1f}%
💰 Funding: {d['funding']:.4f}
📊 OI: {d['oi']:.0f}
🕒 {datetime.now().strftime('%H:%M:%S')}
🔗 https://www.bybit.com/trade/perpetual/{symbol}"""

            for user_id in active_users:
                try:
                    await bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True)
                except Exception as e:
                    logger.warning(f"Не удалось отправить сообщение {user_id}: {e}")

        logger.info(f"✅ Отправлено {len(signals)} сигналов | Активных пользователей: {len(active_users)}")

    except Exception as e:
        logger.error(f"Scan error: {e}")


async def check_pending_payments():
    if not cryptobot_client or not pending_invoices:
        return

    try:
        invoices = cryptobot_client.get_invoices(count=100)
        for inv in invoices:
            if inv.invoice_id in pending_invoices and inv.status == "paid":
                data = pending_invoices.pop(inv.invoice_id)
                plan = SUBSCRIPTION_PLANS[data["plan_key"]]
                expiry = datetime.now() + timedelta(days=plan["days"])

                subscribers[data["chat_id"]] = {"expiry_date": expiry, "status": "active"}
                save_data()

                await bot.send_message(
                    data["chat_id"],
                    f"✅ Оплата прошла!\n\n"
                    f"Подписка <b>{plan['name']}</b> активна до {expiry.strftime('%d.%m.%Y')}",
                    parse_mode="HTML"
                )
                logger.info(f"Подписка активирована для {data['chat_id']}")
    except Exception as e:
        logger.error(f"Check payments error: {e}")


# ===================== MAIN =====================
async def main():
    load_data()
    logger.info("🚀 Бот запущен")

    await bot.delete_webhook(drop_pending_updates=True)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_market, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(check_pending_payments, "interval", seconds=45)   # проверка платежей
    scheduler.start()

    asyncio.create_task(asyncio.sleep(3) or scan_market())  # первый запуск

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
