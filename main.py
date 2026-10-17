import asyncio
import html
import logging
import os
import re
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///vufc_casino.db")

logging.basicConfig(level=logging.INFO)

# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
  fighter_name = message.from_user.first_name or "Новый Боец"
  fighter_name = re.sub(r"[^\w\s\-А-Яа-яЁё]", "", fighter_name).strip()[:20]
  if not fighter_name:
    fighter_name = "Новый Боец"

  text = (
      f"Добро пожаловать в <b>VUFC CASINO</b>! 🥊🎰\n\n"
      f"👤 Боец: <b>{html.escape(fighter_name)}</b>\n"
      f"💰 Твой баланс: <b>5,000 $</b>\n\n"
      f"Выбери действие:"
  )
  await message.answer(text)


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (ЧТОБЫ НЕ БЫЛО ОШИБКИ ПОРТОВ) ---
async def handle_ping(request):
  return web.Response(text="VUFC Casino Bot is alive and working!")


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
  asyncio.create_task(start_web_server())
  logging.info("VUFC CASINO Bot started!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    logging.info("Bot stopped!")
