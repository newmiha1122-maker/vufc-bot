import asyncio
import html
import logging
import os
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

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]
TIMEZONE = "Europe/Moscow"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///vufc_casino.db")

logging.basicConfig(level=logging.INFO)

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
  last_bonus_at: Mapped[datetime | None] = mapped_column(
      DateTime, nullable=True
  )
  is_in_weekly_draw: Mapped[bool] = mapped_column(Boolean, default=False)
  is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class Spin(Base):
  __tablename__ = "spins"
  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  user_id: Mapped[int] = mapped_column(
      Integer, ForeignKey("users.id"), index=True
  )
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
                  text="📜 История игр", callback_data="menu_history"
              ),
              InlineKeyboardButton(
                  text="🏆 Зал славы", callback_data="menu_top"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="ℹ️ Правила", callback_data="menu_rules"
              ),
              InlineKeyboardButton(
                  text="👤 Сменить имя бойца",
                  callback_data="menu_change_name",
              ),
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
    else:
      if user.is_blocked:
        user.is_blocked = False
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


# --- ГЛАВНОЕ МЕНЮ И КНОПКИ ---
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
      "• Ставьте в рулетках, выигрывайте кэфы до x15!\n"
      "• Глобальный кулдаун на платные спины — 30 минут.\n"
      "• Налог на роскошь: 10% в Банк Лиги с выигрышей > 25 000 $.\n"
      "• Розыгрыш накопленного Банка Лиги — каждое воскресенье в 20:00 МСК!\n"
      "• Ежедневный бонус — 150 $ раз в 24 часа."
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
      text += (
          f"{medals[idx]} <b>{html.escape(user.fighter_name)}</b> — "
          f"<b>{spin.net_win:,} $</b> (Кэф x{spin.multiplier})\n"
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
    text += "Вы еще не сделали ни одного спина."
  else:
    for s in spins:
      dt_str = (
          s.created_at + timedelta(hours=3)
      ).strftime("%d.%m %H:%M")  # МСК
      prefix = "🎟️ " if s.is_free_spin else ""
      roulette_name = ROULETTES.get(s.roulette_type, {}).get("name", "Игра")
      if s.net_win > 0:
        text += (
            f"{prefix}{dt_str} · {roulette_name} · {s.bet_amount} $ →"
            f" <b>+{s.net_win:,} $ (x{s.multiplier})</b>\n"
        )
      else:
        text += (
            f"{prefix}{dt_str} · {roulette_name} · {s.bet_amount} $ →"
            f" <i>-{s.bet_amount} $</i>\n"
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
      if not user.last_spin_at:
        await callback.answer(
            "Бонус доступен после первой игры.", show_alert=True
        )
        return
      if now - user.last_spin_at > timedelta(hours=48):
        await callback.answer(
            "Бонус доступен после первой игры (нужен спин за последние 48"
            " часов).",
            show_alert=True,
        )
        return

    user.balance += 150
    user.last_bonus_at = now
    if is_bankrupt:
      user.last_spin_at = None
    await session.commit()

    await callback.message.edit_text(
        "🎁 <b>Ежедневный бонус получен!</b>\nВам начислено <b>150 $</b>.",
        reply_markup=back_kb(),
    )
  await callback.answer()


# --- СМЕНА ИМЕНИ БОЙЦА ---
@dp.callback_query(F.data == "menu_change_name")
async def change_name_start(callback: types.CallbackQuery, state: FSMContext):
  await state.set_state(FSMForm.waiting_for_name)
  text = (
      "👤 Введите имя вашего бойца (до 20 символов, без мата и ссылок):"
  )
  await callback.message.edit_text(text, reply_markup=cancel_kb())
  await callback.answer()


@dp.message(FSMForm.waiting_for_name)
async def process_name_input(message: types.Message, state: FSMContext):
  if message.text == "❌ Отмена" or not message.text:
    return
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
          "❌ Промокод не найден или больше не активен.", reply_markup=cancel_kb()
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
        f"🎟️ <b>Фриспин по промокоду:</b> к сожалению, проигрыш.",
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
      f"Лимиты ставки: от {r_data['min_bet']} $ до {r_data['max_bet']} $\n\n"
      f"Введите сумму ставки:"
  )
  await callback.message.edit_text(text, reply_markup=cancel_kb())
  await callback.answer()


@dp.message(FSMForm.waiting_for_bet)
async def process_bet(message: types.Message, state: FSMContext):
  if message.text == "❌ Отмена":
    await state.clear()
    return

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
        f"❌ Ставка должна быть от {r_data['min_bet']} до {r_data['max_bet']} $.",
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

    session.add(
        Spin(
            user_id=user.id,
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
  await asyncio.sleep(3)

  if not is_win:
    await msg.edit_text(
        f"Вы проиграли <b>{bet} $</b>. Попробуете через 30 минут?",
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


# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    return
  text = (
      "⚙️ <b>Админ-панель VUFC CASINO</b>\n\nКоманды:\n/addpromo [код]"
      " [рулетка] [сумма] [активации]"
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
  if r_type not in ROULETTES:
    await message.answer(f"Неверная рулетка. Доступны: {list(ROULETTES.keys())}")
    return

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
  await message.answer(f"✅ Промокод <code>{code}</code> успешно создан!")


# --- ПЛАНИРОВЩИК РОЗЫГРЫША БАНКА ---
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
        f"🏆 <b>РОЗЫГРЫШ БАНКА ЛИГИ!</b>\nВы стали победителем и забираете"
        f" <b>{bank_amount:,} $</b>!",
    )


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
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

  scheduler = AsyncIOScheduler(timezone=TIMEZONE)
  scheduler.add_job(
      run_league_bank_draw,
      trigger="cron",
      day_of_week="sun",
      hour=20,
      minute=0,
      args=[bot],
      id="bank_draw",
      replace_existing=True,
  )
  scheduler.start()

  logging.info("VUFC CASINO Bot fully started with all features!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    logging.info("Bot stopped!")
