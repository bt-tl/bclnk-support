"""
broadcast.py — Broadcast helper bot (Aiogram v3)

Fungsi:
- Admin kirim: /broadcast (dengan cara reply ke pesan apa pun)
- Bot akan mengirim COPY dari pesan yang direply ke semua user_id di tabel tg_users (kecuali admin).
- Aman untuk text / foto / video / dokumen / dll karena pakai copy_message.

ENV wajib (Railway):
- BOT_TOKEN
- DATABASE_URL
- BROADCAST_ADMIN_ID   (satu admin yang boleh broadcast)

Opsional:
- BROADCAST_EXCLUDE_BANNED=true/false  (default false; kalau true, skip user yang sedang ban di tabel user_bans)
"""

import os
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

from psycopg_pool import AsyncConnectionPool

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "8342328997:AAE3mlEIL3Aw9Bdo24yRS-Q_WXtYc74P0p8").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "postgresql://postgres:FsGCNVcYUsxUodNDsCgbMNXigMBJkpMR@switchback.proxy.rlwy.net:18615/railway").strip()
BROADCAST_ADMIN_ID = int(os.getenv("BROADCAST_ADMIN_ID", "960048629"))
EXCLUDE_BANNED = (os.getenv("BROADCAST_EXCLUDE_BANNED") or "false").strip().lower() in ("1", "true", "yes", "on")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum di-set.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL belum di-set.")
if not BROADCAST_ADMIN_ID:
    raise RuntimeError("BROADCAST_ADMIN_ID wajib di-set.")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_all_user_ids() -> list[int]:
    """
    Ambil semua user_id dari tabel tg_users.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM tg_users ORDER BY created_at ASC")
            rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def is_user_banned(user_id: int) -> bool:
    """
    True kalau user sedang ban aktif (mengacu ke tabel user_bans yang kamu pakai di main bot).
    Kalau tabel tidak ada, fungsi ini akan dianggap tidak ban.
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT banned_until FROM user_bans WHERE user_id=%s",
                    (user_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return False
                banned_until = row[0]
                return banned_until and banned_until > now_utc()
    except Exception:
        # kalau tabel user_bans tidak ada / error, jangan block broadcast
        return False


@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if m.from_user.id != BROADCAST_ADMIN_ID:
        return

    if not m.reply_to_message:
        await m.reply(
            "⚠️ Pakai /broadcast dengan cara <b>reply</b> ke pesan yang mau dibroadcast.\n\n"
            "Contoh:\n"
            "1) Kirim pesan/foto/video\n"
            "2) Reply pesan itu -> ketik <code>/broadcast</code>"
        )
        return

    # pesan target yang akan di-copy ke semua user
    src_chat_id = m.reply_to_message.chat.id
    src_message_id = m.reply_to_message.message_id

    await m.reply("📣 Mulai broadcast... ambil daftar user dari database.")

    user_ids = await fetch_all_user_ids()
    # exclude admin (dan bisa exclude user lain kalau mau)
    user_ids = [uid for uid in user_ids if uid != BROADCAST_ADMIN_ID]

    total = len(user_ids)
    if total == 0:
        await m.reply("⚠️ Tidak ada user di database.")
        return

    ok = 0
    fail = 0
    skipped = 0

    status_msg = await m.reply(f"📦 Target: <b>{total}</b> user\n⏳ Progress: 0/{total}")

    for i, uid in enumerate(user_ids, start=1):
        # optional: skip banned user
        if EXCLUDE_BANNED:
            try:
                if await is_user_banned(uid):
                    skipped += 1
                    continue
            except Exception:
                pass

        try:
            # copy_message => aman untuk berbagai jenis pesan (text/media)
            await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
            ok += 1

        except TelegramRetryAfter as e:
            # kena rate limit, tidur sesuai saran Telegram
            wait_s = int(getattr(e, "retry_after", 2))
            await asyncio.sleep(max(wait_s, 1))
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
                ok += 1
            except Exception:
                fail += 1

        except (TelegramForbiddenError, TelegramBadRequest):
            # user block bot / chat invalid
            fail += 1

        except Exception:
            fail += 1

        # update progress tiap 50 user biar tidak spam edit
        if i % 50 == 0 or i == total:
            try:
                await status_msg.edit_text(
                    f"📦 Target: <b>{total}</b> user\n"
                    f"✅ Sukses: <b>{ok}</b>\n"
                    f"⏭️ Skip: <b>{skipped}</b>\n"
                    f"❌ Gagal: <b>{fail}</b>\n"
                    f"⏳ Progress: {i}/{total}"
                )
            except Exception:
                pass

        # sedikit delay halus biar lebih aman
        await asyncio.sleep(0.03)

    await m.reply(
        "✅ Broadcast selesai.\n\n"
        f"📦 Target: <b>{total}</b>\n"
        f"✅ Sukses: <b>{ok}</b>\n"
        f"⏭️ Skip: <b>{skipped}</b>\n"
        f"❌ Gagal: <b>{fail}</b>"
    )


@dp.message(Command("start"))
async def cmd_start(m: Message):
    if m.from_user.id != BROADCAST_ADMIN_ID:
        await m.reply("Bot ini khusus admin untuk broadcast.")
        return
    await m.reply(
        "✅ Broadcast bot siap.\n\n"
        "Cara pakai:\n"
        "1) Kirim pesan/foto/video yang mau dibroadcast\n"
        "2) Reply pesan itu\n"
        "3) Ketik <code>/broadcast</code>\n\n"
        f"EXCLUDE_BANNED = <b>{EXCLUDE_BANNED}</b>"
    )


async def main():
    await pool.open()
    logging.info("Broadcast bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
