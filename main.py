import asyncio
import html
import json
import logging
import math
import os
import pathlib
import random
import re
import secrets
from datetime import datetime, timedelta

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
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- КОНФИГУРАЦИЯ И БАЗА ДАННЫХ ---
BASE_DIR = pathlib.Path(__file__).resolve().parent
DATABASE_URL = os.getenv(
    "DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR}/vufc_casino.db"
)

TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]
TIMEZONE = "Europe/Moscow"

logging.basicConfig(level=logging.INFO)

user_locks = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
  if user_id not in user_locks:
    user_locks[user_id] = asyncio.Lock()
  return user_locks[user_id]


# --- ПАРАМЕТРЫ РУЛЕТОК И ИГР ---
ROULETTES = {
    "jobber": {
        "name": "🟢 Джоббер",
        "chance_bp": 6000,
        "multiplier": 1.5,
        "min_bet": 100,
        "max_bet": 10000,
    },
    "challenger": {
        "name": "🔵 Претендент",
        "chance_bp": 4500,
        "multiplier": 2.0,
        "min_bet": 100,
        "max_bet": 10000,
    },
    "title": {
        "name": "🟡 Титульник",
        "chance_bp": 3300,
        "multiplier": 2.7,
        "min_bet": 100,
        "max_bet": 5000,
    },
    "main_card": {
        "name": "🟠 Мэйн-кард",
        "chance_bp": 2500,
        "multiplier": 3.5,
        "min_bet": 100,
        "max_bet": 5000,
    },
    "legend": {
        "name": "🔴 Легенда",
        "chance_bp": 1500,
        "multiplier": 5.5,
        "min_bet": 100,
        "max_bet": 3000,
    },
    "jackpot": {
        "name": "💀 Джекпот",
        "chance_bp": 500,
        "multiplier": 15.0,
        "min_bet": 100,
        "max_bet": 2000,
    },
}

MINE_MODES = {
    "warmup": {"name": "🟢 Разминка", "mines": 3, "cap": 100000},
    "main_card": {"name": "🟡 Основной кард", "mines": 5, "cap": 150000},
    "title": {"name": "🔴 Титульный бой", "mines": 10, "cap": 200000},
}

QUEST_POOL = {
    "play_roulette_5": {
        "title": "Разминка в рулетке",
        "desc": "Сделай 5 ставок в рулетке (от 150$)",
        "target": 5,
        "reward": 200,
        "type": "roulette_spin",
    },
    "open_mines_10": {
        "title": "Сапер",
        "desc": "Открой 10 безопасных клеток в Клетке Мин (от 150$)",
        "target": 10,
        "reward": 500,
        "type": "mines_cell",
    },
    "high_roller_1": {
        "title": "Хайроллер",
        "desc": "Сделай 1 ставку от 500$ в любой игре",
        "target": 1,
        "reward": 700,
        "type": "high_bet",
    },
    "win_jobber_3": {
        "title": "Лакер",
        "desc": "Выиграй 3 раза в рулетке «Джоббер» (от 150$)",
        "target": 3,
        "reward": 800,
        "type": "jobber_win",
    },
    "play_roulette_3": {
        "title": "Фанат",
        "desc": "Сделай 3 ставки в рулетке (от 150$)",
        "target": 3,
        "reward": 300,
        "type": "roulette_spin",
    },
}

# --- БАЗА ДАННЫХ ---
engine = create_async_engine(DATABASE_URL, echo=False)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase, AsyncAttrs):
  pass


class User(Base):
  __tablename__ = "users"
  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
  username: Mapped[str | None] = mapped_column(String(64), nullable=True)
  fighter_name: Mapped[str] = mapped_column(
      String(20), default="Новый Боец", index=True
  )
  balance: Mapped[int] = mapped_column(Integer, default=0)
  is_registered: Mapped[bool] = mapped_column(Boolean, default=False)
  last_spin_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
  last_mine_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
  last_bonus_at: Mapped[datetime | None] = mapped_column(
      DateTime, nullable=True
  )
  is_in_weekly_draw: Mapped[bool] = mapped_column(Boolean, default=False)
  is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
  last_quest_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)


class Spin(Base):
  __tablename__ = "spins"
  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  user_id: Mapped[int] = mapped_column(
      Integer, ForeignKey("users.id"), index=True
  )
  game_type: Mapped[str] = mapped_column(String(32), default="roulette")
  roulette_type: Mapped[str] = mapped_column(String(32))
  bet_amount: Mapped[int] = mapped_column(Integer)
  multiplier: Mapped[float] = mapped_column(Float)
  gross_win: Mapped[int] = mapped_column(Integer)
  net_win: Mapped[int] = mapped_column(Integer)
  tax: Mapped[int] = mapped_column(Integer, default=0)
  is_free_spin: Mapped[bool] = mapped_column(Boolean, default=False)
  created_at: Mapped[datetime] = mapped_column(
      DateTime, default=datetime.utcnow
  )


class Setting(Base):
  __tablename__ = "settings"
  key: Mapped[str] = mapped_column(String(64), primary_key=True)
  value: Mapped[str] = mapped_column(Text)


class Promocode(Base):
  __tablename__ = "promocodes"
  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
  roulette_type: Mapped[str] = mapped_column(String(32))
  amount: Mapped[int] = mapped_column(Integer)
  remaining_activations: Mapped[int] = mapped_column(Integer)
  is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromoActivation(Base):
  __tablename__ = "promo_activations"
  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
  promocode_id: Mapped[int] = mapped_column(Integer, ForeignKey("promocodes.id"))


class MineGame(Base):
  __tablename__ = "mine_games"
  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  user_id: Mapped[int] = mapped_column(
      Integer, ForeignKey("users.id"), index=True
  )
  game_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
  mode: Mapped[str] = mapped_column(String(32))
  stake: Mapped[int] = mapped_column(Integer)
  grid_json: Mapped[str] = mapped_column(Text)
  opened_json: Mapped[str] = mapped_column(Text, default="[]")
  is_finished: Mapped[bool] = mapped_column(Boolean, default=False)
  is_win: Mapped[bool] = mapped_column(Boolean, default=False)
  gross: Mapped[int] = mapped_column(Integer, default=0)
  fee: Mapped[int] = mapped_column(Integer, default=0)
  net: Mapped[int] = mapped_column(Integer, default=0)
  created_at: Mapped[datetime] = mapped_column(
      DateTime, default=datetime.utcnow
  )


class UserQuest(Base):
  __tablename__ = "user_quests"
  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  user_id: Mapped[int] = mapped_column(
      Integer, ForeignKey("users.id"), index=True
  )
  quest_id: Mapped[str] = mapped_column(String(64))
  progress: Mapped[int] = mapped_column(Integer, default=0)
  target: Mapped[int] = mapped_column(Integer)
  is_claimed: Mapped[bool] = mapped_column(Boolean, default=False)
  quest_date: Mapped[datetime] = mapped_column(Date)


async def init_db():
  async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
  async with async_session_maker() as session:
    res = await session.execute(
        select(Setting).where(Setting.key == "league_bank")
    )
    if not res.scalar_one_or_none():
      session.add(Setting(key="league_bank", value="0"))
      await session.commit()


# --- FSM СОСТОЯНИЯ ---
class FSMForm(StatesGroup):
  registration_name = State()
  waiting_for_bet = State()
  waiting_for_promo = State()
  waiting_for_name = State()
  waiting_for_mine_bet = State()

  # Админка
  admin_grant_target = State()
  admin_grant_amount = State()
  admin_rename_target = State()
  admin_rename_new_name = State()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_rank_title(balance: int) -> str:
  if balance < 2000:
    return "🥊 Джоббер"
  elif balance < 10000:
    return "⚔️ Претендент"
  elif balance < 50000:
    return "🏆 Чемпион Лиги"
  else:
    return "👑 Легенда P4P"


def sanitize_name(raw_name: str) -> str:
  if not raw_name:
    return ""
  if re.search(
      r"http://|https://|t\.me/|@\w+|www\.", raw_name, re.IGNORECASE
  ):
    return ""
  cleaned = re.sub(r"[^\w\s\-А-Яа-яЁё]", "", raw_name)
  cleaned = re.sub(r"\s+", " ", cleaned).strip()
  if not cleaned or len(cleaned) > 20 or cleaned.lower() == "новый боец":
    return ""
  profanity = ["хуй", "пизд", "еб", "blyat", "kurwa", "сук"]
  norm = (
      cleaned.lower()
      .replace("0", "o")
      .replace("1", "i")
      .replace("3", "e")
      .replace("@", "a")
  )
  for word in profanity:
    if word in norm:
      return ""
  return cleaned


def main_menu_kb():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🎰 Крутить рулетку", callback_data="menu_roulette"
              )
          ],
          [
              InlineKeyboardButton(
                  text="💣 Клетка Мин", callback_data="menu_mines"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎁 Ежедневный бонус", callback_data="menu_bonus"
              ),
              InlineKeyboardButton(
                  text="🎟️ Ввести промокод", callback_data="menu_promo"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🏦 Банк Лиги", callback_data="menu_bank"
              ),
              InlineKeyboardButton(
                  text="💰 Мой баланс", callback_data="menu_balance"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="📊 Таблица балансов", callback_data="menu_balances_top"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📋 Квесты", callback_data="menu_quests"
              ),
              InlineKeyboardButton(
                  text="📜 История игр", callback_data="menu_history"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🏆 Зал славы", callback_data="menu_top"
              ),
              InlineKeyboardButton(
                  text="ℹ️ Правила", callback_data="menu_rules"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="👤 Сменить имя бойца",
                  callback_data="menu_change_name",
              )
          ],
      ]
  )


def back_kb():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
      ]
  )


def cancel_kb():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")]
      ]
  )


def cancel_admin_kb():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]
      ]
  )


# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# --- РЕГИСТРАЦИЯ И СТАРТ (ТЗ 1) ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
  await state.clear()
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = res.scalar_one_or_none()

    if not user:
      user = User(
          telegram_id=message.from_user.id,
          username=message.from_user.username,
          fighter_name="Новый Боец",
          balance=0,
          is_registered=False,
      )
      session.add(user)
      await session.commit()
    else:
      user.username = message.from_user.username
      await session.commit()

    if not user.is_registered or user.fighter_name == "Новый Боец":
      await state.set_state(FSMForm.registration_name)
      await message.answer(
          "Привет! Для начала игры введи имя своего бойца из лиги (например:"
          " <b>Conor McGregor</b>).\n\n"
          "⚠️ <i>Внимание: от этого зависит начисление твоих гонораров!</i>"
      )
      return

    text = (
        f"Добро пожаловать в <b>VUFC CASINO</b>! 🥊🎰\n\n"
        f"👤 Боец: <b>{html.escape(user.fighter_name)}</b> ({get_rank_title(user.balance)})\n"
        f"💰 Твой баланс: <b>{user.balance:,} $</b>\n\n"
        f"Выбери действие:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@dp.message(FSMForm.registration_name)
async def process_registration_name(message: types.Message, state: FSMContext):
  cleaned = sanitize_name(message.text)
  if not cleaned:
    await message.answer(
        "❌ Некорректное имя (слишком длинное, содержит мат, ссылки или"
        " системные слова).\nПопробуй вписать другое имя:"
    )
    return

  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = res.scalar_one()

    user.fighter_name = cleaned
    if not user.is_registered:
      user.balance = 5000  # Начисление 5000$ при первой регистрации
      user.is_registered = True
    await session.commit()

  await state.clear()
  await message.answer(
      f"🎉 Боец <b>{html.escape(cleaned)}</b> успешно зарегистрирован!\nВам"
      " начислен стартовый гонорар <b>5 000 $</b>.",
      reply_markup=main_menu_kb(),
  )


# --- ТАБЛИЦА БАЛАНСОВ (ТЗ 2) ---
@dp.callback_query(F.data == "menu_balances_top")
async def show_balances_top(callback: types.CallbackQuery):
  async with async_session_maker() as session:
    res = await session.execute(
        select(User)
        .where(User.is_registered == True)
        .order_by(User.balance.desc())
        .limit(20)
    )
    users = res.scalars().all()

    total_res = await session.execute(
        select(func.sum(User.balance)).where(User.is_registered == True)
    )
    total_money = total_res.scalar() or 0

  text = "📊 <b>ТАБЛИЦА БАЛАНСОВ БОЙЦОВ</b>\n\n"
  if not users:
    text += "В лиге пока нет зарегистрированных бойцов."
  else:
    for idx, u in enumerate(users, 1):
      username_str = f"@{u.username}" if u.username else "без_тега"
      text += (
          f"<b>{idx}.</b> {username_str} (<b>{html.escape(u.fighter_name)}</b>)"
          f" — <b>{u.balance:,} $</b>\n"
      )

  text += (
      f"\n💵 <b>Общая сумма денег в системе:</b> <b>{total_money:,} $</b>"
  )
  await callback.message.edit_text(text, reply_markup=back_kb())
  await callback.answer()


# --- АДМИН-ПАНЕЛЬ (ТЗ 3) ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    return
  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="💰 Начислить гонорар", callback_data="admin_grant"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👤 Изменить имя бойца", callback_data="admin_rename"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎟️ Создать промокод", callback_data="admin_promo"
              )
          ],
      ]
  )
  await message.answer("⚙️ <b>Панель Администратора VUFC CASINO</b>", reply_markup=kb)


@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(callback: types.CallbackQuery, state: FSMContext):
  await state.clear()
  await callback.message.edit_text("❌ Действие отменено.")
  await callback.answer()


# 1. Начисление гонорара
@dp.callback_query(F.data == "admin_grant")
async def admin_grant_start(callback: types.CallbackQuery, state: FSMContext):
  if callback.from_user.id not in ADMIN_IDS:
    return
  await state.set_state(FSMForm.admin_grant_target)
  await callback.message.edit_text(
      "💰 <b>Начисление гонорара</b>\n\nВведите `@username`, ID пользователя"
      " или имя бойца:",
      reply_markup=cancel_admin_kb(),
  )
  await callback.answer()


@dp.message(FSMForm.admin_grant_target)
async def admin_grant_target_input(message: types.Message, state: FSMContext):
  target_str = message.text.strip().replace("@", "")
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(
            or_(
                User.username.ilike(target_str),
                User.fighter_name.ilike(target_str),
                User.telegram_id == (int(target_str) if target_str.isdigit() else 0),
            )
        )
    )
    target_user = res.scalar_one_or_none()

    if not target_user:
      await message.answer(
          "❌ Боец не найден. Попробуйте ввести еще раз:",
          reply_markup=cancel_admin_kb(),
      )
      return

    await state.update_data(target_user_id=target_user.id)
    await state.set_state(FSMForm.admin_grant_amount)
    await message.answer(
        f"Найден боец: <b>{html.escape(target_user.fighter_name)}</b>"
        f" (@{target_user.username})\nТекущий баланс:"
        f" <b>{target_user.balance:,} $</b>\n\nВведите сумму гонорара для"
        " начисления:",
        reply_markup=cancel_admin_kb(),
    )


@dp.message(FSMForm.admin_grant_amount)
async def admin_grant_amount_input(message: types.Message, state: FSMContext):
  try:
    amount = int(message.text.strip())
    if amount <= 0:
      raise ValueError()
  except ValueError:
    await message.answer(
        "❌ Введите положительное целое число!",
        reply_markup=cancel_admin_kb(),
    )
    return

  data = await state.get_data()
  user_id = data.get("target_user_id")

  async with async_session_maker() as session:
    res = await session.execute(select(User).where(User.id == user_id))
    user = res.scalar_one()

    user.balance += amount
    new_bal = user.balance
    await session.commit()

    # Уведомление игроку в ЛС
    try:
      await bot.send_message(
          user.telegram_id,
          f"🏆 <b>Вам начислен гонорар: {amount:,} $!</b>\nВаш новый баланс:"
          f" <b>{new_bal:,} $</b>",
      )
    except Exception:
      pass

  await state.clear()
  await message.answer(
      f"✅ Успешно начислено <b>{amount:,} $</b> бойцу"
      f" <b>{html.escape(user.fighter_name)}</b>.\nНовый баланс:"
      f" <b>{new_bal:,} $</b>"
  )


# 2. Изменение имени бойца
@dp.callback_query(F.data == "admin_rename")
async def admin_rename_start(callback: types.CallbackQuery, state: FSMContext):
  if callback.from_user.id not in ADMIN_IDS:
    return
  await state.set_state(FSMForm.admin_rename_target)
  await callback.message.edit_text(
      "👤 <b>Изменение имени бойца</b>\n\nВведите `@username`, ID пользователя"
      " или текущее имя бойца:",
      reply_markup=cancel_admin_kb(),
  )
  await callback.answer()


@dp.message(FSMForm.admin_rename_target)
async def admin_rename_target_input(
    message: types.Message, state: FSMContext
):
  target_str = message.text.strip().replace("@", "")
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(
            or_(
                User.username.ilike(target_str),
                User.fighter_name.ilike(target_str),
                User.telegram_id == (int(target_str) if target_str.isdigit() else 0),
            )
        )
    )
    target_user = res.scalar_one_or_none()

    if not target_user:
      await message.answer(
          "❌ Боец не найден. Попробуйте еще раз:",
          reply_markup=cancel_admin_kb(),
      )
      return

    await state.update_data(target_user_id=target_user.id)
    await state.set_state(FSMForm.admin_rename_new_name)
    await message.answer(
        f"Боец: <b>{html.escape(target_user.fighter_name)}</b>"
        f" (@{target_user.username})\n\nВведите НОВОЕ имя для этого бойца (до 20"
        " символов):",
        reply_markup=cancel_admin_kb(),
    )


@dp.message(FSMForm.admin_rename_new_name)
async def admin_rename_new_name_input(
    message: types.Message, state: FSMContext
):
  cleaned = sanitize_name(message.text)
  if not cleaned:
    await message.answer(
        "❌ Некорректное имя (слишком длинное или содержит мат). Попробуйте"
        " еще:",
        reply_markup=cancel_admin_kb(),
    )
    return

  data = await state.get_data()
  user_id = data.get("target_user_id")

  async with async_session_maker() as session:
    res = await session.execute(select(User).where(User.id == user_id))
    user = res.scalar_one()

    old_name = user.fighter_name
    user.fighter_name = cleaned
    await session.commit()

    try:
      await bot.send_message(
          user.telegram_id,
          f"👑 <b>Президент лиги изменил имя вашего бойца на"
          f" [{html.escape(cleaned)}]</b>",
      )
    except Exception:
      pass

  await state.clear()
  await message.answer(
      f"✅ Имя бойца <b>{html.escape(old_name)}</b> изменено на"
      f" <b>{html.escape(cleaned)}</b>!"
  )


# --- ФИЧА ОТ МЕНЯ: ПЕРЕВОД ДЕНЕГ МЕЖДУ БОЙЦАМИ (/pay) ---
@dp.message(Command("pay"))
async def cmd_pay(message: types.Message):
  args = message.text.split()
  if len(args) < 3:
    await message.answer("Формат: `/pay [@username / ID] [сумма]`")
    return

  target_str = args[1].replace("@", "")
  try:
    amount = int(args[2])
    if amount <= 0:
      raise ValueError()
  except ValueError:
    await message.answer("❌ Введите корректную сумму.")
    return

  async with async_session_maker() as session:
    res_sender = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    sender = res_sender.scalar_one()

    if sender.balance < amount:
      await message.answer("❌ Недостаточно средств.")
      return

    res_target = await session.execute(
        select(User).where(
            or_(
                User.username.ilike(target_str),
                User.telegram_id == (int(target_str) if target_str.isdigit() else 0),
            )
        )
    )
    target = res_target.scalar_one_or_none()

    if not target or target.id == sender.id:
      await message.answer("❌ Получатель не найден.")
      return

    fee = int(amount * 0.05)  # 5% комиссия лиги
    net_transfer = amount - fee

    sender.balance -= amount
    target.balance += net_transfer
    await session.commit()

    try:
      await bot.send_message(
          target.telegram_id,
          f"💸 Боец <b>{html.escape(sender.fighter_name)}</b> перевел вам"
          f" <b>{net_transfer:,} $</b> (комиссия 5%).",
      )
    except Exception:
      pass

  await message.answer(
      f"✅ Успешно переведено <b>{net_transfer:,} $</b> бойцу"
      f" <b>{html.escape(target.fighter_name)}</b>!"
  )


# --- НАВИГАЦИЯ И ОСТАЛЬНЫЕ КНОПКИ ---
@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
  await state.clear()
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()

    if not user.is_registered:
      await state.set_state(FSMForm.registration_name)
      await callback.message.edit_text(
          "Привет! Для начала игры введи имя своего бойца из лиги (например:"
          " Conor McGregor):"
      )
      return

    text = (
        f"Главное меню:\n\n👤 Боец: <b>{html.escape(user.fighter_name)}</b>"
        f" ({get_rank_title(user.balance)})\n💰 Твой баланс:"
        f" <b>{user.balance:,} $</b>"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
  await callback.answer()


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def handle_ping(request):
  return web.Response(text="VUFC Casino Bot v1.2 is alive!")


async def start_web_server():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 10000))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()


# --- ЗАПУСК ---
async def main():
  await init_db()
  asyncio.create_task(start_web_server())

  scheduler = AsyncIOScheduler(timezone=TIMEZONE)
  scheduler.start()

  logging.info("VUFC CASINO Bot v1.2 successfully started!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    logging.info("Bot stopped!")
