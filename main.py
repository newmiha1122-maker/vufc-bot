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
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- КОНФИГУРАЦИЯ И БАЗа ДАННЫХ (АБСОЛЮТНЫЙ ПУТЬ ВНУТРИ ПРОЕКТА) ---
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

# --- БЛОКИРОВКИ ДЛЯ ЗАЩИТЫ ОТ МУЛЬТИКЛИКОВ ---
user_locks = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
  if user_id not in user_locks:
    user_locks[user_id] = asyncio.Lock()
  return user_locks[user_id]


# --- ПАРАМЕТРЫ РУЛЕТОК ---
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

# --- ПАРАМЕТРЫ МИН ---
MINE_MODES = {
    "warmup": {"name": "🟢 Разминка", "mines": 3, "cap": 100000},
    "main_card": {"name": "🟡 Основной кард", "mines": 5, "cap": 150000},
    "title": {"name": "🔴 Титульный бой", "mines": 10, "cap": 200000},
}

# --- ПУЛ КВЕСТОВ ---
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
  balance: Mapped[int] = mapped_column(Integer, default=5000)
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
  game_type: Mapped[str] = mapped_column(
      String(32), default="roulette"
  )  # roulette или mines
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
  grid_json: Mapped[str] = mapped_column(Text)  # Координаты мин
  opened_json: Mapped[str] = mapped_column(
      Text, default="[]"
  )  # Открытые клетки
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
  waiting_for_bet = State()
  waiting_for_promo = State()
  waiting_for_name = State()
  waiting_for_mine_bet = State()


# --- МАТЕМАТИКА КЭФОВ КЛЕТКИ МИН ---
def nCr(n, r):
  if r < 0 or r > n:
    return 0
  return math.comb(n, r)


def get_mine_coef(k: int, mode_key: str) -> float:
  if k == 0:
    return 1.00
  mines_count = MINE_MODES[mode_key]["mines"]
  fair = nCr(25, k) / nCr(25 - mines_count, k)
  coef = max(1.00, round(0.90 * fair, 2))
  return coef


# --- УТИЛИТЫ ---
def sanitize_name(raw_name: str) -> str:
  if not raw_name:
    return ""
  if re.search(
      r"http://|https://|t\.me/|@\w+|www\.", raw_name, re.IGNORECASE
  ):
    return ""
  cleaned = re.sub(r"[^\w\s\-А-Яа-яЁё]", "", raw_name)
  cleaned = re.sub(r"\s+", " ", cleaned).strip()
  if not cleaned or len(cleaned) > 20:
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


# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# --- ОБРАБОТЧИК КВЕСТОВ (ТРИГГЕР) ---
async def update_quest_progress(
    session, user_id: int, trigger_type: str, stake: int, val_add: int = 1
):
  if stake < 150:
    return  # Антифрод

  today = datetime.utcnow().date()
  quests_res = await session.execute(
      select(UserQuest).where(
          UserQuest.user_id == user_id,
          UserQuest.quest_date == today,
          UserQuest.is_claimed == False,
      )
  )
  user_quests = quests_res.scalars().all()

  for q in user_quests:
    q_info = QUEST_POOL.get(q.quest_id)
    if not q_info:
      continue

    if q_info["type"] == trigger_type:
      q.progress = min(q.target, q.progress + val_add)
    elif q_info["type"] == "high_bet" and stake >= 500:
      q.progress = min(q.target, q.progress + 1)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
  await state.clear()
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = res.scalar_one_or_none()

    if not user:
      fighter_name = sanitize_name(message.from_user.first_name) or "Новый Боец"
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


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
  await state.clear()
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()
    text = (
        f"Главное меню:\n\n👤 Боец: <b>{html.escape(user.fighter_name)}</b>\n"
        f"💰 Твой баланс: <b>{user.balance:,} $</b>"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
  await callback.answer()


@dp.callback_query(F.data == "menu_balance")
async def show_balance(callback: types.CallbackQuery):
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()
    await callback.message.edit_text(
        f"💰 Твой баланс: <b>{user.balance:,} $</b>", reply_markup=back_kb()
    )
  await callback.answer()


@dp.callback_query(F.data == "menu_rules")
async def show_rules(callback: types.CallbackQuery):
  rules_text = (
      "ℹ️ <b>Правила VUFC CASINO:</b>\n\n"
      "• 🎰 <b>Рулетка:</b> Ставьте и выигрывайте кэфы до x15! Кулдаун: 30"
      " мин.\n"
      "• 💣 <b>Клетка Мин:</b> Открывайте клетки на поле 5x5, забирайте кэф"
      " вовремя! Кулдаун: 3 мин.\n"
      "• 🏦 <b>Налог на роскошь:</b> 10% в Банк Лиги с выигрышей > 25 000 $.\n"
      "• 🏆 <b>Розыгрыш Банка:</b> Каждое воскресенье в 20:00 МСК среди участников"
      " недели!\n"
      "• 📋 <b>Квесты & Бонусы:</b> Выполняйте 3 квеста ежедневно и получайте"
      " 150 $ каждые 24ч!"
  )
  await callback.message.edit_text(rules_text, reply_markup=back_kb())
  await callback.answer()


@dp.callback_query(F.data == "menu_bank")
async def show_bank(callback: types.CallbackQuery):
  async with async_session_maker() as session:
    res = await session.execute(
        select(Setting).where(Setting.key == "league_bank")
    )
    bank_val = int(res.scalar_one().value)
  text = (
      f"🏦 <b>Банк Лиги</b>\n\nТекущий призовой фонд: <b>{bank_val:,} $</b>\n"
      f"Розыгрыш в воскресенье в 20:00 МСК среди всех участников недели!"
  )
  await callback.message.edit_text(text, reply_markup=back_kb())
  await callback.answer()


@dp.callback_query(F.data == "menu_top")
async def show_top(callback: types.CallbackQuery):
  async with async_session_maker() as session:
    res = await session.execute(
        select(Spin, User)
        .join(User, Spin.user_id == User.id)
        .order_by(Spin.net_win.desc())
        .limit(5)
    )
    top_spins = res.all()

  medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
  text = "🏆 <b>ЗАЛ СЛАВЫ (Топ-5 выигрышей)</b>\n\n"
  if not top_spins:
    text += "История побед пока пуста."
  else:
    for idx, (spin, user) in enumerate(top_spins):
      game_label = (
          "💣 Клетка Мин"
          if spin.game_type == "mines"
          else ROULETTES.get(spin.roulette_type, {}).get("name", "Рулетка")
      )
      text += (
          f"{medals[idx]} <b>{html.escape(user.fighter_name)}</b> — "
          f"<b>{spin.net_win:,} $</b> ({game_label})\n"
      )

  await callback.message.edit_text(text, reply_markup=back_kb())
  await callback.answer()


@dp.callback_query(F.data == "menu_history")
async def show_history(callback: types.CallbackQuery):
  async with async_session_maker() as session:
    user_res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = user_res.scalar_one()

    spins_res = await session.execute(
        select(Spin)
        .where(Spin.user_id == user.id)
        .order_by(Spin.created_at.desc())
        .limit(10)
    )
    spins = spins_res.scalars().all()

  text = "📜 <b>Последние 10 игр:</b>\n\n"
  if not spins:
    text += "Вы еще не сделали ни одной игры."
  else:
    for s in spins:
      dt_str = (s.created_at + timedelta(hours=3)).strftime("%d.%m %H:%M")
      prefix = "🎟️ " if s.is_free_spin else ""
      g_name = (
          "💣 Клетка Мин"
          if s.game_type == "mines"
          else ROULETTES.get(s.roulette_type, {}).get("name", "Игра")
      )
      if s.net_win > 0:
        text += (
            f"{prefix}{dt_str} · {g_name} · {s.bet_amount:,} $ →"
            f" <b>+{s.net_win:,} $ (x{s.multiplier})</b>\n"
        )
      else:
        text += (
            f"{prefix}{dt_str} · {g_name} · {s.bet_amount:,} $ →"
            f" <i>-{s.bet_amount:,} $</i>\n"
        )

  await callback.message.edit_text(text, reply_markup=back_kb())
  await callback.answer()


# --- ЕЖЕДНЕВНЫЙ БОНУС ---
@dp.callback_query(F.data == "menu_bonus")
async def process_bonus(callback: types.CallbackQuery):
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()

    now = datetime.utcnow()
    is_bankrupt = user.balance < 100

    if user.last_bonus_at:
      diff = now - user.last_bonus_at
      if diff < timedelta(hours=24):
        rem = timedelta(hours=24) - diff
        hours, rem_sec = divmod(int(rem.total_seconds()), 3600)
        minutes = rem_sec // 60
        await callback.answer(
            f"Вы уже получали бонус. Возвращайтесь через {hours} ч. {minutes}"
            " мин.",
            show_alert=True,
        )
        return

    if not is_bankrupt:
      if not user.last_spin_at and not user.last_mine_at:
        await callback.answer(
            "Бонус доступен после первой игры.", show_alert=True
        )
        return

    user.balance += 150
    user.last_bonus_at = now
    await session.commit()

    await callback.message.edit_text(
        "🎁 <b>Ежедневный бонус получен!</b>\nВам начислено <b>150 $</b>.",
        reply_markup=back_kb(),
    )
  await callback.answer()


# --- СМЕНА ИМЕНИ ---
@dp.callback_query(F.data == "menu_change_name")
async def change_name_start(callback: types.CallbackQuery, state: FSMContext):
  await state.set_state(FSMForm.waiting_for_name)
  await callback.message.edit_text(
      "👤 Введите имя вашего бойца (до 20 символов, без мата и ссылок):",
      reply_markup=cancel_kb(),
  )
  await callback.answer()


@dp.message(FSMForm.waiting_for_name)
async def process_name_input(message: types.Message, state: FSMContext):
  cleaned = sanitize_name(message.text)
  if not cleaned:
    await message.answer(
        "❌ Некорректное имя (слишком длинное, содержит мат или ссылки)."
        " Попробуйте ещё:",
        reply_markup=cancel_kb(),
    )
    return

  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = res.scalar_one()
    user.fighter_name = cleaned
    await session.commit()

  await state.clear()
  await message.answer(
      f"✅ Имя бойца изменено на <b>{html.escape(cleaned)}</b>!",
      reply_markup=main_menu_kb(),
  )


# --- ПРОМОКОДЫ ---
@dp.callback_query(F.data == "menu_promo")
async def promo_start(callback: types.CallbackQuery, state: FSMContext):
  await state.set_state(FSMForm.waiting_for_promo)
  await callback.message.edit_text(
      "🎟️ Введите промокод:", reply_markup=cancel_kb()
  )
  await callback.answer()


@dp.message(FSMForm.waiting_for_promo)
async def process_promo_input(message: types.Message, state: FSMContext):
  code_text = message.text.strip()
  async with async_session_maker() as session:
    user_res = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = user_res.scalar_one()

    promo_res = await session.execute(
        select(Promocode).where(
            Promocode.code == code_text, Promocode.is_active == True
        )
    )
    promo = promo_res.scalar_one_or_none()

    if not promo or promo.remaining_activations <= 0:
      await message.answer(
          "❌ Промокод не найден или не активен.", reply_markup=cancel_kb()
      )
      return

    act_res = await session.execute(
        select(PromoActivation).where(
            PromoActivation.user_id == user.id,
            PromoActivation.promocode_id == promo.id,
        )
    )
    if act_res.scalar_one_or_none():
      await message.answer(
          "❌ Вы уже активировали этот промокод.", reply_markup=cancel_kb()
      )
      return

    promo.remaining_activations -= 1
    session.add(
        PromoActivation(user_id=user.id, promocode_id=promo.id)
    )

    roulette = ROULETTES.get(promo.roulette_type, ROULETTES["jobber"])
    is_win = secrets.randbelow(10000) + 1 <= roulette["chance_bp"]

    gross, tax, net = 0, 0, 0
    if is_win:
      gross = promo.amount * roulette["multiplier"]
      net = int(gross)
      if net > 25000:
        tax = int(net * 0.10)
        net -= tax
        bank_res = await session.execute(
            select(Setting).where(Setting.key == "league_bank")
        )
        bank_set = bank_res.scalar_one()
        bank_set.value = str(int(bank_set.value) + tax)

      user.balance += net

    session.add(
        Spin(
            user_id=user.id,
            game_type="roulette",
            roulette_type=promo.roulette_type,
            bet_amount=promo.amount,
            multiplier=roulette["multiplier"],
            gross_win=int(gross),
            net_win=net if is_win else 0,
            tax=tax,
            is_free_spin=True,
        )
    )
    await session.commit()

  await state.clear()
  if is_win:
    await message.answer(
        f"🎟️ <b>Фриспин по промокоду сыграл!</b>\nРулетка: {roulette['name']}\n🔥"
        f" Вы выиграли <b>{net:,} $</b>!",
        reply_markup=main_menu_kb(),
    )
  else:
    await message.answer(
        "🎟️ <b>Фриспин по промокоду:</b> к сожалению, проигрыш.",
        reply_markup=main_menu_kb(),
    )


# --- МЕХАНИКА РУЛЕТОК ---
@dp.callback_query(F.data == "menu_roulette")
async def roulette_menu(callback: types.CallbackQuery):
  kb = []
  for key, r in ROULETTES.items():
    kb.append([
        InlineKeyboardButton(
            text=(
                f"{r['name']} (Шанс: {r['chance_bp']/100}%, Кэф x{r['multiplier']})"
            ),
            callback_data=f"spin_{key}",
        )
    ])
  kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
  await callback.message.edit_text(
      "🎰 Выберите рулетку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
  )
  await callback.answer()


@dp.callback_query(F.data.startswith("spin_"))
async def choose_roulette(callback: types.CallbackQuery, state: FSMContext):
  r_key = callback.data.split("_")[1]
  r_data = ROULETTES.get(r_key)
  if not r_data:
    return

  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()

    if user.balance >= 100 and user.last_spin_at:
      diff = datetime.utcnow() - user.last_spin_at
      if diff < timedelta(minutes=30):
        rem_sec = int(1800 - diff.total_seconds())
        await callback.answer(
            f"⏳ Кулдаун! Следующий спин через {rem_sec // 60} мин."
            f" {rem_sec % 60} сек.",
            show_alert=True,
        )
        return

  await state.set_state(FSMForm.waiting_for_bet)
  await state.update_data(roulette_key=r_key)

  text = (
      f"🎰 Рулетка: <b>{r_data['name']}</b>\n"
      f"Лимиты ставки: от {r_data['min_bet']:,} $ до {r_data['max_bet']:,} $\n\n"
      f"Введите сумму ставки:"
  )
  await callback.message.edit_text(text, reply_markup=cancel_kb())
  await callback.answer()


@dp.message(FSMForm.waiting_for_bet)
async def process_bet(message: types.Message, state: FSMContext):
  try:
    bet = int(message.text)
  except ValueError:
    await message.answer(
        "❌ Введите целое число.", reply_markup=cancel_kb()
    )
    return

  data = await state.get_data()
  r_key = data.get("roulette_key")
  r_data = ROULETTES.get(r_key)

  if not r_data or not (r_data["min_bet"] <= bet <= r_data["max_bet"]):
    await message.answer(
        f"❌ Ставка должна быть от {r_data['min_bet']:,} до"
        f" {r_data['max_bet']:,} $.",
        reply_markup=cancel_kb(),
    )
    return

  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = res.scalar_one()

    if user.balance < bet:
      await message.answer("❌ Недостаточно средств.", reply_markup=cancel_kb())
      return

    user.balance -= bet
    user.last_spin_at = datetime.utcnow()
    user.is_in_weekly_draw = True

    # Обновление квестов
    await update_quest_progress(session, user.id, "roulette_spin", bet)

    is_win = secrets.randbelow(10000) + 1 <= r_data["chance_bp"]

    gross, tax, net = 0, 0, 0
    if is_win:
      gross = int(bet * r_data["multiplier"])
      net = gross
      if gross > 25000:
        tax = int(gross * 0.10)
        net = gross - tax
        bank_res = await session.execute(
            select(Setting).where(Setting.key == "league_bank")
        )
        bank_set = bank_res.scalar_one()
        bank_set.value = str(int(bank_set.value) + tax)

      user.balance += net

      if r_key == "jobber":
        await update_quest_progress(session, user.id, "jobber_win", bet)

    session.add(
        Spin(
            user_id=user.id,
            game_type="roulette",
            roulette_type=r_key,
            bet_amount=bet,
            multiplier=r_data["multiplier"],
            gross_win=gross,
            net_win=net if is_win else 0,
            tax=tax,
            is_free_spin=False,
        )
    )
    await session.commit()

  await state.clear()
  msg = await message.answer("🎰 Крутим...")
  await asyncio.sleep(2)

  if not is_win:
    await msg.edit_text(
        f"Вы проиграли <b>{bet:,} $</b>. Попробуете через 30 минут?",
        reply_markup=main_menu_kb(),
    )
  else:
    win_title = (
        "💀 <b>ДЖЕКПОТ!</b>" if r_key == "jackpot" else "🔥 <b>ПОБЕДА!</b>"
    )
    tax_info = (
        f"\nУдержан налог 10% ({tax:,} $) в Банк Лиги." if tax > 0 else ""
    )
    await msg.edit_text(
        f"{win_title} Выпал кэф x{r_data['multiplier']}.\nВы выиграли"
        f" <b>{net:,} $</b>!{tax_info}",
        reply_markup=main_menu_kb(),
    )


# --- ИГРА «КЛЕТКА МИН» (САПЕР) ---
@dp.callback_query(F.data == "menu_mines")
async def mines_start(callback: types.CallbackQuery, state: FSMContext):
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()

    if user.balance >= 100 and user.last_mine_at:
      diff = datetime.utcnow() - user.last_mine_at
      if diff < timedelta(minutes=3):
        rem_sec = int(180 - diff.total_seconds())
        await callback.answer(
            f"⏳ Кулдаун! Следующая игра через {rem_sec // 60} мин."
            f" {rem_sec % 60} сек.",
            show_alert=True,
        )
        return

  await state.set_state(FSMForm.waiting_for_mine_bet)
  text = (
      "💣 <b>Клетка Мин</b>\nЛимиты ставки: от 100 $ до 10 000 $\n\nВведите"
      " сумму ставки:"
  )
  await callback.message.edit_text(text, reply_markup=cancel_kb())
  await callback.answer()


@dp.message(FSMForm.waiting_for_mine_bet)
async def process_mine_bet(message: types.Message, state: FSMContext):
  try:
    bet = int(message.text)
  except ValueError:
    await message.answer(
        "❌ Введите целое число.", reply_markup=cancel_kb()
    )
    return

  if not (100 <= bet <= 10000):
    await message.answer(
        "❌ Ставка должна быть от 100 $ до 10 000 $.", reply_markup=cancel_kb()
    )
    return

  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = res.scalar_one()

    if user.balance < bet:
      await message.answer("❌ Недостаточно средств.", reply_markup=cancel_kb())
      return

  await state.update_data(mine_bet=bet)

  kb = [
      [
          InlineKeyboardButton(
              text="🟢 Разминка · 3 мины · x1.02",
              callback_data="mine_diff_warmup",
          )
      ],
      [
          InlineKeyboardButton(
              text="🟡 Основной кард · 5 мин · x1.13",
              callback_data="mine_diff_main_card",
          )
      ],
      [
          InlineKeyboardButton(
              text="🔴 Титульный бой · 10 мин · x1.50",
              callback_data="mine_diff_title",
          )
      ],
      [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")],
  ]
  await message.answer(
      f"💣 Ставка: <b>{bet:,} $</b>\nВыберите режим сложности:",
      reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
  )


def build_mines_keyboard(
    game_id: str,
    opened: list,
    mines: list,
    is_finished: bool,
    mode: str,
    stake: int,
):
  kb = []
  for r in range(5):
    row_btns = []
    for c in range(5):
      idx = r * 5 + c
      if idx in opened:
        if idx in mines:
          btn_text = "💥"
        else:
          btn_text = "💎"
      else:
        if is_finished and idx in mines:
          btn_text = "💣"
        else:
          btn_text = "❓"

      cb_data = (
          "ignore"
          if is_finished or idx in opened
          else f"mine_click:{game_id}:{r}:{c}"
      )
      row_btns.append(InlineKeyboardButton(text=btn_text, callback_data=cb_data))
    kb.append(row_btns)

  k = len(opened)
  coef = get_mine_coef(k, mode)
  gross = round(stake * coef, 2)
  fee = round(gross * 0.10, 2) if gross > 25000 else 0
  net = int(gross - fee)
  cap = MINE_MODES[mode]["cap"]
  if net > cap:
    net = cap

  if not is_finished:
    if k > 0:
      kb.append([
          InlineKeyboardButton(
              text=f"💰 Забрать: {net:,} $ (Кэф x{coef})",
              callback_data=f"mine_cashout:{game_id}",
          )
      ])
    else:
      kb.append([
          InlineKeyboardButton(
              text="💰 Забрать: 0 $ (Кэф x1.00)", callback_data="mine_disabled"
          )
      ])
  else:
    kb.append([
        InlineKeyboardButton(
            text="🔁 Играть снова", callback_data="menu_mines"
        ),
        InlineKeyboardButton(
            text="⬅️ В меню", callback_data="back_to_menu"
        ),
    ])

  return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.callback_query(F.data.startswith("mine_diff_"))
async def start_mine_game(callback: types.CallbackQuery, state: FSMContext):
  mode = callback.data.split("_")[2]
  data = await state.get_data()
  stake = data.get("mine_bet")
  if not stake:
    await callback.answer("Ошибка сессии.", show_alert=True)
    return

  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()

    if user.balance < stake:
      await callback.answer("Недостаточно средств.", show_alert=True)
      return

    user.balance -= stake
    user.last_mine_at = datetime.utcnow()
    user.is_in_weekly_draw = True

    # Создание поля
    mines_count = MINE_MODES[mode]["mines"]
    all_cells = list(range(25))
    mines_coords = random.sample(all_cells, mines_count)

    game_id = secrets.token_hex(8)
    mine_game = MineGame(
        user_id=user.id,
        game_id=game_id,
        mode=mode,
        stake=stake,
        grid_json=json.dumps(mines_coords),
        opened_json="[]",
    )
    session.add(mine_game)
    await session.commit()

  await state.clear()
  kb = build_mines_keyboard(
      game_id, [], mines_coords, False, mode, stake
  )
  m_text = (
      f"💣 <b>Клетка Мин</b> ({MINE_MODES[mode]['name']})\nСтавка:"
      f" <b>{stake:,} $</b>\n\nОткрывайте клетки и забирайте выигрыш!"
  )
  await callback.message.edit_text(m_text, reply_markup=kb)
  await callback.answer()


@dp.callback_query(F.data.startswith("mine_click:"))
async def mine_click(callback: types.CallbackQuery):
  lock = get_user_lock(callback.from_user.id)
  async with lock:
    _, game_id, r_str, c_str = callback.data.split(":")
    r, c = int(r_str), int(c_str)
    idx = r * 5 + c

    async with async_session_maker() as session:
      res = await session.execute(
          select(MineGame, User)
          .join(User, MineGame.user_id == User.id)
          .where(MineGame.game_id == game_id)
      )
      row = res.first()
      if not row:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

      game, user = row
      if game.is_finished:
        await callback.answer("⚠️ Эта игра уже завершена.", show_alert=True)
        return

      mines = json.loads(game.grid_json)
      opened = json.loads(game.opened_json)

      if idx in opened:
        await callback.answer()
        return

      opened.append(idx)
      game.opened_json = json.dumps(opened)

      # Обновление прогресса квеста «Сапер»
      await update_quest_progress(
          session, user.id, "mines_cell", game.stake, 1
      )

      # ВЗРЫВ!
      if idx in mines:
        game.is_finished = True
        game.is_win = False
        session.add(
            Spin(
                user_id=user.id,
                game_type="mines",
                roulette_type=game.mode,
                bet_amount=game.stake,
                multiplier=0.0,
                gross_win=0,
                net_win=0,
                tax=0,
            )
        )
        await session.commit()

        kb = build_mines_keyboard(
            game_id, opened, mines, True, game.mode, game.stake
        )
        await callback.message.edit_text(
            f"💥 <b>ВЗРЫВ!</b> Вы наступили на мину!\nСтавка <b>{game.stake:,}"
            " $</b> сгорела.",
            reply_markup=kb,
        )
        await callback.answer()
        return

      # УСПЕШНЫЙ КЛИК
      k = len(opened)
      safe_cells_needed = 25 - MINE_MODES[game.mode]["mines"]

      # АВТО-КЭШ (все безопасные клетки открыты)
      if k == safe_cells_needed:
        game.is_finished = True
        game.is_win = True
        coef = get_mine_coef(k, game.mode)
        gross = round(game.stake * coef, 2)
        fee = round(gross * 0.10, 2) if gross > 25000 else 0
        net = int(gross - fee)
        cap = MINE_MODES[game.mode]["cap"]
        if net > cap:
          net = cap

        game.gross = int(gross)
        game.fee = int(fee)
        game.net = net

        user.balance += net

        if fee > 0:
          bank_res = await session.execute(
              select(Setting).where(Setting.key == "league_bank")
          )
          bank_set = bank_res.scalar_one()
          bank_set.value = str(int(bank_set.value) + int(fee))

        session.add(
            Spin(
                user_id=user.id,
                game_type="mines",
                roulette_type=game.mode,
                bet_amount=game.stake,
                multiplier=coef,
                gross_win=int(gross),
                net_win=net,
                tax=int(fee),
            )
        )
        await session.commit()

        kb = build_mines_keyboard(
            game_id, opened, mines, True, game.mode, game.stake
        )
        await callback.message.edit_text(
            f"🏆 <b>ИДЕАЛЬНО!</b> Вы открыли все безопасные клетки!\nЗабрано:"
            f" <b>{net:,} $</b> (Кэф x{coef})",
            reply_markup=kb,
        )
        await callback.answer()
        return

      await session.commit()

      kb = build_mines_keyboard(
          game_id, opened, mines, False, game.mode, game.stake
      )
      coef = get_mine_coef(k, game.mode)
      await callback.message.edit_reply_markup(reply_markup=kb)
      await callback.answer(f"Открыто: {k} | Кэф: x{coef}")


@dp.callback_query(F.data.startswith("mine_cashout:"))
async def mine_cashout(callback: types.CallbackQuery):
  lock = get_user_lock(callback.from_user.id)
  async with lock:
    game_id = callback.data.split(":")[1]
    async with async_session_maker() as session:
      res = await session.execute(
          select(MineGame, User)
          .join(User, MineGame.user_id == User.id)
          .where(MineGame.game_id == game_id)
      )
      row = res.first()
      if not row:
        await callback.answer()
        return

      game, user = row
      if game.is_finished:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

      opened = json.loads(game.opened_json)
      mines = json.loads(game.grid_json)
      k = len(opened)
      if k == 0:
        await callback.answer("Сначала откройте хотя бы одну клетку!")
        return

      game.is_finished = True
      game.is_win = True
      coef = get_mine_coef(k, game.mode)
      gross = round(game.stake * coef, 2)
      fee = round(gross * 0.10, 2) if gross > 25000 else 0
      net = int(gross - fee)
      cap = MINE_MODES[game.mode]["cap"]
      if net > cap:
        net = cap

      game.gross = int(gross)
      game.fee = int(fee)
      game.net = net

      user.balance += net

      if fee > 0:
        bank_res = await session.execute(
            select(Setting).where(Setting.key == "league_bank")
        )
        bank_set = bank_res.scalar_one()
        bank_set.value = str(int(bank_set.value) + int(fee))

      session.add(
          Spin(
              user_id=user.id,
              game_type="mines",
              roulette_type=game.mode,
              bet_amount=game.stake,
              multiplier=coef,
              gross_win=int(gross),
              net_win=net,
              tax=int(fee),
          )
      )
      await session.commit()

    kb = build_mines_keyboard(
        game_id, opened, mines, True, game.mode, game.stake
    )
    tax_info = f"\nℹ️ Удержано {int(fee):,} $ в Банк Лиги." if fee > 0 else ""
    await callback.message.edit_text(
        f"✅ <b>Забрали!</b>\nВыигрыш: <b>{net:,} $</b> (Кэф x{coef}){tax_info}",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "mine_disabled")
async def mine_disabled_cb(callback: types.CallbackQuery):
  await callback.answer("Сначала откройте хотя бы одну клетку!", show_alert=True)


# --- ЕЖЕДНЕВНЫЕ КВЕСТЫ ---
@dp.callback_query(F.data == "menu_quests")
async def show_quests(callback: types.CallbackQuery):
  today = datetime.utcnow().date()
  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()

    # Генерация квестов если день сменился
    if user.last_quest_date != today:
      user.last_quest_date = today
      q_keys = random.sample(list(QUEST_POOL.keys()), 3)
      for qk in q_keys:
        q_data = QUEST_POOL[qk]
        session.add(
            UserQuest(
                user_id=user.id,
                quest_id=qk,
                progress=0,
                target=q_data["target"],
                is_claimed=False,
                quest_date=today,
            )
        )
      await session.commit()

    quests_res = await session.execute(
        select(UserQuest).where(
            UserQuest.user_id == user.id, UserQuest.quest_date == today
        )
    )
    user_quests = quests_res.scalars().all()

  text = "📋 <b>Твои задания на сегодня:</b>\n\n"
  claimable_total = 0

  for idx, q in enumerate(user_quests, 1):
    q_info = QUEST_POOL.get(q.quest_id)
    if not q_info:
      continue

    pct = min(1.0, q.progress / q.target)
    bars = int(pct * 5)
    progress_bar = "■" * bars + "□" * (5 - bars)

    if q.is_claimed:
      status = "✅ <i>Забрано</i>"
    elif q.progress >= q.target:
      status = f"🎉 <b>Готово! (+{q_info['reward']} $)</b>"
      claimable_total += q_info["reward"]
    else:
      status = f"Награда: {q_info['reward']} $"

    text += (
        f"<b>{idx}. {q_info['title']}</b>: {q_info['desc']}\n"
        f"   Прогресс: [{progress_bar}] {q.progress}/{q.target}. {status}\n\n"
    )

  kb = []
  if claimable_total > 0:
    kb.append([
        InlineKeyboardButton(
            text=f"🎁 Забрать награды: {claimable_total:,} $",
            callback_data="claim_quests",
        )
    ])
  kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])

  await callback.message.edit_text(
      text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
  )
  await callback.answer()


@dp.callback_query(F.data == "claim_quests")
async def claim_quests(callback: types.CallbackQuery):
  today = datetime.utcnow().date()
  total_reward = 0

  async with async_session_maker() as session:
    res = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = res.scalar_one()

    quests_res = await session.execute(
        select(UserQuest).where(
            UserQuest.user_id == user.id,
            UserQuest.quest_date == today,
            UserQuest.is_claimed == False,
        )
    )
    user_quests = quests_res.scalars().all()

    for q in user_quests:
      if q.progress >= q.target:
        q_info = QUEST_POOL.get(q.quest_id)
        if q_info:
          total_reward += q_info["reward"]
          q.is_claimed = True

    if total_reward > 0:
      user.balance += total_reward
      await session.commit()
      await callback.answer(
          f"🎉 Получено {total_reward:,} $ на баланс!", show_alert=True
      )
    else:
      await callback.answer("Нет готовых квестов.", show_alert=True)

  await show_quests(callback)


# --- АДМИНКА ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    return
  text = (
      "⚙️ <b>Админ-панель VUFC CASINO</b>\n\nСоздать промокод:\n/addpromo"
      " [код] [рулетка] [сумма] [активации]"
  )
  await message.answer(text)


@dp.message(Command("addpromo"))
async def cmd_addpromo(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    return
  args = message.text.split()
  if len(args) < 5:
    await message.answer(
        "Формат: /addpromo [код] [рулетка_key] [сумма] [активации]"
    )
    return

  _, code, r_type, amount, acts = args
  async with async_session_maker() as session:
    session.add(
        Promocode(
            code=code,
            roulette_type=r_type,
            amount=int(amount),
            remaining_activations=int(acts),
        )
    )
    await session.commit()
  await message.answer(f"✅ Промокод <code>{code}</code> создан!")


# --- ФОНОВЫЕ ЗАДАЧИ И ТАЙМАУТЫ ---
async def check_mine_timeouts():
  """Проверяет брошенные игры в Клетку Мин (>10 мин)."""
  now = datetime.utcnow()
  cutoff = now - timedelta(minutes=10)

  async with async_session_maker() as session:
    res = await session.execute(
        select(MineGame, User)
        .join(User, MineGame.user_id == User.id)
        .where(MineGame.is_finished == False, MineGame.created_at <= cutoff)
    )
    abandoned = res.all()

    for game, user in abandoned:
      game.is_finished = True
      opened = json.loads(game.opened_json)
      k = len(opened)

      if k > 0:
        coef = get_mine_coef(k, game.mode)
        gross = round(game.stake * coef, 2)
        fee = round(gross * 0.10, 2) if gross > 25000 else 0
        net = int(gross - fee)
        cap = MINE_MODES[game.mode]["cap"]
        if net > cap:
          net = cap

        game.is_win = True
        game.gross, game.fee, game.net = int(gross), int(fee), net
        user.balance += net
      else:
        user.balance += game.stake  # Возврат ставки

    await session.commit()


async def run_league_bank_draw(bot: Bot):
  async with async_session_maker() as session:
    bank_res = await session.execute(
        select(Setting).where(Setting.key == "league_bank")
    )
    bank_setting = bank_res.scalar_one_or_none()
    bank_amount = int(bank_setting.value) if bank_setting else 0
    if bank_amount <= 0:
      return

    users_res = await session.execute(
        select(User).where(
            User.is_in_weekly_draw == True, User.is_blocked == False
        )
    )
    participants = users_res.scalars().all()
    if not participants:
      return

    winner = random.choice(participants)
    winner.balance += bank_amount
    bank_setting.value = "0"
    await session.execute(update(User).values(is_in_weekly_draw=False))
    await session.commit()

    await bot.send_message(
        winner.telegram_id,
        f"🏆 <b>РОЗЫГРЫШ БАНКА ЛИГИ!</b>\nВы забираете <b>{bank_amount:,} $</b>!",
    )


# --- ВЕБ-СЕРВЕР ---
async def handle_ping(request):
  return web.Response(text="VUFC Casino Bot with Mines & Quests is alive!")


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
  scheduler.add_job(
      run_league_bank_draw,
      trigger="cron",
      day_of_week="sun",
      hour=20,
      minute=0,
      args=[bot],
  )
  scheduler.add_job(check_mine_timeouts, trigger="interval", seconds=30)
  scheduler.start()

  logging.info("VUFC CASINO Bot successfully started!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    logging.info("Bot stopped!")
