"""
user.py — Simple admin helper to check total users in Railway Postgres

Fitur:
- /users  -> menampilkan total user di tabel tg_users
- /users active7d -> (opsional) jumlah user yang last_seen dalam 7 hari terakhir
- /users today -> (opsional) jumlah user yang created_at hari ini (UTC)

ENV wajib (Railway):
- BOT_TOKEN
- DATABASE_URL
- USERS_ADMIN_ID   (Telegram user id kamu)

Catatan:
- Script ini hanya baca database (read-only).
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from psycopg_pool import AsyncConnectionPool

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "8342328997:AAE3mlEIL3Aw9Bdo24yRS-Q_WXtYc74P0p8").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "postgresql://postgres:FsGCNVcYUsxUodNDsCgbMNXigMBJkpMR@switchback.proxy.rlwy.net:18615/railway").strip()
USERS_ADMIN_ID = int(os.getenv("USERS_ADMIN_ID", "960048629"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum di-set.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL belum di-set.")
if not USERS_ADMIN_ID:
    raise RuntimeError("USERS_ADMIN_ID wajib di-set.")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def count_all_users() -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM tg_users")
            row = await cur.fetchone()
            return int(row[0] if row else 0)


async def count_active_users(days: int) -> int:
    since = now_utc() - timedelta(days=days)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM tg_users WHERE last_seen >= %s", (since,))
            row = await cur.fetchone()
            return int(row[0] if row else 0)


async def count_created_today_utc() -> int:
    # range: start of today UTC -> start of tomorrow UTC
    now = now_utc()
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM tg_users WHERE created_at >= %s AND created_at < %s",
                (start, end),
            )
            row = await cur.fetchone()
            return int(row[0] if row else 0)


@dp.message(Command("start"))
async def cmd_start(m: Message):
    if m.from_user.id != USERS_ADMIN_ID:
        await m.reply("Bot ini khusus admin.")
        return
    await m.reply(
        "✅ User counter bot siap.\n\n"
        "Perintah:\n"
        "• <code>/users</code> — total user\n"
        "• <code>/users active7d</code> — aktif 7 hari terakhir\n"
        "• <code>/users today</code> — user daftar hari ini (UTC)"
    )


@dp.message(Command("users"))
async def cmd_users(m: Message):
    if m.from_user.id != USERS_ADMIN_ID:
        return

    arg = (m.text or "").split(maxsplit=1)
    mode = arg[1].strip().lower() if len(arg) > 1 else ""

    try:
        if mode == "active7d":
            n = await count_active_users(7)
            await m.reply(f"👤 User aktif 7 hari terakhir: <b>{n}</b>")
            return

        if mode == "today":
            n = await count_created_today_utc()
            await m.reply(f"🆕 User daftar hari ini (UTC): <b>{n}</b>")
            return

        # default total
        total = await count_all_users()
        await m.reply(f"👥 Total user di database: <b>{total}</b>")

    except Exception as e:
        await m.reply(f"❌ Error: <code>{type(e).__name__}</code>")


async def main():
    await pool.open()
    logging.info("User counter bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
