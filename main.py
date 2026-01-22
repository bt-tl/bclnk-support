import os
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode

import psycopg
from psycopg_pool import AsyncConnectionPool

logging.basicConfig(level=logging.INFO)

# =========================
# ENV CONFIG (Railway)
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "8342328997:AAE3mlEIL3Aw9Bdo24yRS-Q_WXtYc74P0p8").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "postgresql://postgres:FsGCNVcYUsxUodNDsCgbMNXigMBJkpMR@switchback.proxy.rlwy.net:18615/railway").strip()

ADMIN_WEB_ID = int(os.getenv("ADMIN_WEB_ID", "960048629"))
ADMIN_ADS_ID = int(os.getenv("ADMIN_ADS_ID", "5513998345"))
ADMIN_REPORT_ID = int(os.getenv("ADMIN_REPORT_ID", "5577603728"))

# Optional: biar header rapi
SUPPORT_BRAND = os.getenv("SUPPORT_BRAND", "Bicolink Support").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum di-set di Environment Variables.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL belum di-set (Railway Postgres).")

if not (ADMIN_WEB_ID and ADMIN_ADS_ID and ADMIN_REPORT_ID):
    raise RuntimeError("ADMIN_WEB_ID / ADMIN_ADS_ID / ADMIN_REPORT_ID wajib diisi.")

# =========================
# CATEGORY ROUTING
# =========================
CATEGORY_WEB = "websupport"
CATEGORY_ADS = "advertise"
CATEGORY_REPORT = "reportlink"

CATEGORY_TO_ADMIN = {
    CATEGORY_WEB: ADMIN_WEB_ID,
    CATEGORY_ADS: ADMIN_ADS_ID,
    CATEGORY_REPORT: ADMIN_REPORT_ID,
}

CATEGORY_LABEL = {
    CATEGORY_WEB: "🌐 Web Support",
    CATEGORY_ADS: "📣 Advertiser Specialist",
    CATEGORY_REPORT: "🚨 Report Link/Content",
}

# =========================
# DB POOL
# =========================
pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False)

# =========================
# DB SETUP
# =========================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tg_users (
    user_id     BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- user memilih jalur support apa (aktif)
CREATE TABLE IF NOT EXISTS user_sessions (
    user_id     BIGINT PRIMARY KEY REFERENCES tg_users(user_id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- semua chat dicatat
CREATE TABLE IF NOT EXISTS chat_messages (
    id              BIGSERIAL PRIMARY KEY,
    direction       TEXT NOT NULL,  -- 'user_to_admin' atau 'admin_to_user'
    category        TEXT NOT NULL,
    user_id         BIGINT NOT NULL,
    admin_id        BIGINT NOT NULL,
    tg_message_id   BIGINT,
    tg_reply_to_id  BIGINT,
    text            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- mapping: pesan yang dikirim ke admin (message_id admin) -> user mana
CREATE TABLE IF NOT EXISTS admin_inbox_map (
    id                 BIGSERIAL PRIMARY KEY,
    admin_id           BIGINT NOT NULL,
    admin_message_id   BIGINT NOT NULL,
    user_id            BIGINT NOT NULL,
    category           TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(admin_id, admin_message_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_admin ON chat_messages(admin_id);
"""

async def init_db():
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
        await conn.commit()

# =========================
# DB HELPERS
# =========================
def now_utc():
    return datetime.now(timezone.utc)

async def upsert_user(m: Message):
    u = m.from_user
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tg_users(user_id, username, first_name, last_name, last_seen)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET username=EXCLUDED.username,
                    first_name=EXCLUDED.first_name,
                    last_name=EXCLUDED.last_name,
                    last_seen=EXCLUDED.last_seen
                """,
                (u.id, u.username, u.first_name, u.last_name, now_utc()),
            )
        await conn.commit()

async def set_user_category(user_id: int, category: str):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_sessions(user_id, category, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET category=EXCLUDED.category,
                    updated_at=EXCLUDED.updated_at
                """,
                (user_id, category, now_utc()),
            )
        await conn.commit()

async def get_user_category(user_id: int) -> str | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT category FROM user_sessions WHERE user_id=%s", (user_id,))
            row = await cur.fetchone()
            return row[0] if row else None

async def log_message(direction: str, category: str, user_id: int, admin_id: int,
                      text: str | None, tg_message_id: int | None = None,
                      tg_reply_to_id: int | None = None):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO chat_messages(direction, category, user_id, admin_id, text, tg_message_id, tg_reply_to_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (direction, category, user_id, admin_id, text, tg_message_id, tg_reply_to_id),
            )
        await conn.commit()

async def map_admin_message(admin_id: int, admin_message_id: int, user_id: int, category: str):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO admin_inbox_map(admin_id, admin_message_id, user_id, category)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (admin_id, admin_message_id) DO NOTHING
                """,
                (admin_id, admin_message_id, user_id, category),
            )
        await conn.commit()

async def resolve_reply_target(admin_id: int, replied_message_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user_id, category
                FROM admin_inbox_map
                WHERE admin_id=%s AND admin_message_id=%s
                """,
                (admin_id, replied_message_id),
            )
            row = await cur.fetchone()
            return (row[0], row[1]) if row else (None, None)

# =========================
# BOT LOGIC
# =========================
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

def build_user_identity(m: Message) -> str:
    u = m.from_user
    name = (u.full_name or "").strip()
    uname = f"@{u.username}" if u.username else "-"
    return f"{name} ({uname}) | <code>{u.id}</code>"

@dp.message(CommandStart())
async def cmd_start(m: Message):
    await upsert_user(m)

    text = (
        f"👋 Halo! Ini <b>{SUPPORT_BRAND}</b> via Telegram.\n\n"
        "Sebelum chat masuk ke admin, pilih dulu tujuan kamu:\n"
        "• /websupport — kendala website Bicolink\n"
        "• /advertise — mau beriklan di jaringan Bicolink\n"
        "• /reportlink — lapor link/konten\n\n"
        "Setelah memilih, kirim pesan kamu seperti biasa."
    )
    await m.answer(text)

@dp.message(Command("websupport"))
async def cmd_websupport(m: Message):
    await upsert_user(m)
    await set_user_category(m.from_user.id, CATEGORY_WEB)
    await m.answer("✅ Oke! Kamu masuk ke <b>Web Support</b>. Silakan tulis kendalanya ya.")

@dp.message(Command("advertise"))
async def cmd_advertise(m: Message):
    await upsert_user(m)
    await set_user_category(m.from_user.id, CATEGORY_ADS)
    await m.answer("✅ Oke! Kamu masuk ke <b>Advertiser Specialist</b>. Silakan jelaskan kebutuhan iklannya.")

@dp.message(Command("reportlink"))
async def cmd_reportlink(m: Message):
    await upsert_user(m)
    await set_user_category(m.from_user.id, CATEGORY_REPORT)
    await m.answer("✅ Oke! Kamu masuk ke <b>Report Link/Content</b>. Kirim detail link/konten yang mau dilaporkan.")

# ====== Handle USER -> ADMIN (semua pesan user selain command)
@dp.message(F.from_user.id != ADMIN_WEB_ID, F.from_user.id != ADMIN_ADS_ID, F.from_user.id != ADMIN_REPORT_ID)
async def user_message_router(m: Message):
    await upsert_user(m)

    # Abaikan kalau command lain
    if m.text and m.text.startswith("/"):
        return

    category = await get_user_category(m.from_user.id)
    if not category:
        await m.answer("⚠️ Pilih dulu tujuan chat: /websupport atau /advertise atau /reportlink")
        return

    admin_id = CATEGORY_TO_ADMIN[category]
    label = CATEGORY_LABEL.get(category, category)

    identity = build_user_identity(m)
    header = (
        f"📩 <b>INCOMING</b>\n"
        f"👤 {identity}\n"
        f"🏷️ <b>Type:</b> {label}\n"
        f"— — —\n"
    )

    # Kirim ke admin: kalau user kirim text
    if m.text:
        sent = await bot.send_message(admin_id, header + m.text)
        await map_admin_message(admin_id, sent.message_id, m.from_user.id, category)
        await log_message(
            direction="user_to_admin",
            category=category,
            user_id=m.from_user.id,
            admin_id=admin_id,
            text=m.text,
            tg_message_id=m.message_id,
        )
        return

    # Kalau user kirim media / non-text, forward + kirim header terpisah biar admin paham konteks
    # (forward supaya file/media ikut)
    sent_header = await bot.send_message(admin_id, header + "<i>[media/message forwarded below]</i>")
    await map_admin_message(admin_id, sent_header.message_id, m.from_user.id, category)

    fwd = await bot.forward_message(chat_id=admin_id, from_chat_id=m.chat.id, message_id=m.message_id)
    # map juga message forward-nya, supaya admin bisa reply ke media forward
    await map_admin_message(admin_id, fwd.message_id, m.from_user.id, category)

    await log_message(
        direction="user_to_admin",
        category=category,
        user_id=m.from_user.id,
        admin_id=admin_id,
        text=m.caption or "[non-text message]",
        tg_message_id=m.message_id,
    )

# ====== Handle ADMIN -> USER (admin reply ke pesan yang masuk)
@dp.message(F.from_user.id.in_({ADMIN_WEB_ID, ADMIN_ADS_ID, ADMIN_REPORT_ID}))
async def admin_reply_handler(m: Message):
    # admin harus reply ke pesan yang masuk (yang bot kirim/forward)
    if not m.reply_to_message:
        # optional: kasih hint
        if m.text and m.text.startswith("/"):
            return
        await m.answer("⚠️ Balas (reply) pesan user yang mau kamu jawab, biar bot tau target user-nya.")
        return

    admin_id = m.from_user.id
    replied_id = m.reply_to_message.message_id

    user_id, category = await resolve_reply_target(admin_id, replied_id)
    if not user_id:
        await m.answer("⚠️ Target user tidak ditemukan untuk reply ini. Pastikan kamu reply ke pesan yang bot kirim/forward.")
        return

    # Tentukan admin tujuan dari category (harus match)
    expected_admin = CATEGORY_TO_ADMIN.get(category)
    if expected_admin != admin_id:
        await m.answer("⚠️ Kamu bukan admin untuk kategori chat ini.")
        return

    # Kirim ke user
    prefix = f"💬 <b>{CATEGORY_LABEL.get(category, category)}</b>\n"
    if m.text:
        await bot.send_message(user_id, prefix + m.text)
        await log_message(
            direction="admin_to_user",
            category=category,
            user_id=user_id,
            admin_id=admin_id,
            text=m.text,
            tg_message_id=m.message_id,
            tg_reply_to_id=replied_id,
        )
        return

    # Kalau admin kirim media, bot copy message biar user dapat media
    await bot.copy_message(chat_id=user_id, from_chat_id=m.chat.id, message_id=m.message_id)
    await log_message(
        direction="admin_to_user",
        category=category,
        user_id=user_id,
        admin_id=admin_id,
        text=m.caption or "[non-text message]",
        tg_message_id=m.message_id,
        tg_reply_to_id=replied_id,
    )

# =========================
# STARTUP
# =========================
async def main():
    await pool.open()
    await init_db()
    logging.info("DB ready. Bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
