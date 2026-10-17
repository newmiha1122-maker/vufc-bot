import asyncio
import html
import logging
import os
import re
import secrets
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, select, update
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
TIMEZONE = "Europe/Moscow"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///vufc_casino.db")

logging.basicConfig(level=logging.INFO)

# --- БАЗА ДАННЫХ ---
engine = create_async_engine(DATABASE_URL, echo=False)
async_session_maker = async_session_maker(engine, expire_on_commit=False)


class Base(DeclarativeBase, AsyncAttrs):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fighter_name: Mapped[str] = mapped_column(String(20), default="Новый Боец", index=True)
    balance: Mapped[int] = mapped_column(Integer, default=5000)
    last_spin_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_bonus_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_in_weekly_draw: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --- КНОПКИ И МЕНЮ ---
def main_menu_kb():
    buttons = [
        [InlineKeyboardButton(text="🎰 Крутить рулетку", callback_data="menu_roulette")],
        [
            InlineKeyboardButton(text="🎁 Ежедневный бонус", callback_data="menu_bonus"),
            InlineKeyboardButton(text="🎟️ Ввести промокод", callback_data="menu_promo"),
        ],
        [
            InlineKeyboardButton(text="🏦 Банк Лиги", callback_data="menu_bank"),
            InlineKeyboardButton(text="💰 Мой баланс", callback_data="menu_balance"),
        ],
        [
            InlineKeyboardButton(text="📜 История игр", callback_data="menu_history"),
            InlineKeyboardButton(text="🏆 Зал славы", callback_data="menu_top"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Правила", callback_data="menu_rules"),
            InlineKeyboardButton(text="👤 Сменить имя бойца", callback_data="menu_change_name"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- ИНИЦИАЛИЗА БОТА ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    async with async_session_maker() as session:
        res = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = res.scalar_one_or_none()

        if not user:
            fighter_name = message.from_user.first_name or "Новый Боец"
            fighter_name = re.sub(r"[^\w\s\-А-Яа-яЁё]", "", fighter_name).strip()[:20]
            if not fighter_name:
                fighter_name = "Новый Боец"

            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                fighter_name=fighter_name,
                balance=5000,
            )
            session.add(user)
            await session.commit()

        text = (
            f"Добро пожаловать в <b>VUFC CASINO</b>! 🥊🎰\n\n"
            f"👤 Боец: <b>{html.escape(user.fighter_name)}</b>\n"
            f"💰 Твой баланс: <b>{user.balance:,} $</b>\n\n"
            f"Выбери действие:"
        )
        await message.answer(text, reply_markup=main_menu_kb())


# --- ОБХОД ЗАСЫПАНИЯ НА RENDER ---
async def handle_ping(request):
    return web.Response(text="VUFC Casino Bot is alive!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")


# --- ЗАПУСК ---
async def main():
    await init_db()
    asyncio.create_task(start_web_server())
    logging.info("VUFC CASINO Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped!")
