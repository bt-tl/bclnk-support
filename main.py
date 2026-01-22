import os
import asyncio
import logging
from datetime import datetime, timezone
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BufferedInputFile
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

ARCHIVE_CHANNEL_ID = int(os.getenv("ARCHIVE_CHANNEL_ID", "-1003614003005"))  # -100xxxxxxx
SUPPORT_BRAND = (os.getenv("SUPPORT_BRAND") or "Bicolink Support").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum di-set di Environment Variables.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL belum di-set (Railway Postgres).")
if not (ADMIN_WEB_ID and ADMIN_ADS_ID and ADMIN_REPORT_ID):
    raise RuntimeError("ADMIN_WEB_ID / ADMIN_ADS_ID / ADMIN_REPORT_ID wajib diisi.")
if not ARCHIVE_CHANNEL_ID:
    raise RuntimeError("ARCHIVE_CHANNEL_ID wajib diisi (channel private, bot jadi admin).")

ADMINS = {ADMIN_WEB_ID, ADMIN_ADS_ID, ADMIN_REPORT_ID}

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


def now_utc():
    return datetime.now(timezone.utc)


# =========================
# DB SCHEMA
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

CREATE TABLE IF NOT EXISTS user_sessions (
    user_id     BIGINT PRIMARY KEY REFERENCES tg_users(user_id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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

CREATE TABLE IF NOT EXISTS admin_inbox_map (
    id                 BIGSERIAL PRIMARY KEY,
    admin_id           BIGINT NOT NULL,
    admin_message_id   BIGINT NOT NULL,
    user_id            BIGINT NOT NULL,
    category           TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(admin_id, admin_message_id)
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tg_message_index (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    role TEXT NOT NULL, -- 'admin' atau 'user'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_admin ON chat_messages(admin_id);
CREATE INDEX IF NOT EXISTS idx_msg_index_user ON tg_message_index(user_id);
"""


async def init_db():
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
        await conn.commit()


# =========================
# DB HELPERS
# =========================
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


async def clear_user_category(user_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM user_sessions WHERE user_id=%s", (user_id,))
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


async def set_setting(key: str, value: str):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO bot_settings(key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE
                SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at
                """,
                (key, value, now_utc()),
            )
        await conn.commit()


async def get_setting(key: str) -> str | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM bot_settings WHERE key=%s", (key,))
            row = await cur.fetchone()
            return row[0] if row else None


async def index_bot_message(user_id: int, category: str, chat_id: int, message_id: int, role: str):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tg_message_index(user_id, category, chat_id, message_id, role)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, category, chat_id, message_id, role),
            )
        await conn.commit()


async def get_transcript_text(user_id: int) -> str:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT created_at, direction, category, COALESCE(text,'')
                FROM chat_messages
                WHERE user_id=%s
                ORDER BY created_at ASC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()

    lines = []
    for created_at, direction, category, text in rows:
        ts = created_at.strftime("%Y-%m-%d %H:%M:%S")
        who = "USER→ADMIN" if direction == "user_to_admin" else "ADMIN→USER"
        cat = CATEGORY_LABEL.get(category, category)
        lines.append(f"[{ts}] [{cat}] {who}: {text}")
    return "\n".join(lines) if lines else "(no messages)"


async def cleanup_user_data(user_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM user_sessions WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM admin_inbox_map WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM chat_messages WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM tg_message_index WHERE user_id=%s", (user_id,))
        await conn.commit()


# =========================
# BOT INIT
# =========================
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()


def build_user_identity(m: Message) -> str:
    u = m.from_user
    name = (u.full_name or "").strip()
    uname = f"@{u.username}" if u.username else "-"
    return f"{name} ({uname}) | <code>{u.id}</code>"


# =========================
# COMMANDS
# =========================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await upsert_user(m)

    if m.from_user.id in ADMINS:
        await m.answer(
            "✅ <b>Admin Panel</b>\n"
            "Cara pakai:\n"
            "• User pilih /websupport /advertise /reportlink\n"
            "• Kamu tinggal <b>reply</b> pesan INCOMING untuk balas user\n"
            "• Akhiri chat: <b>/endchat</b> (reply ke pesan user)\n"
            "• Set foto balasan: kirim foto + /setreplyphoto"
        )
        return

    text = (
        f"👋 Halo! Ini <b>{SUPPORT_BRAND}</b> via Telegram.\n\n"
        "Sebelum chat masuk ke admin, pilih dulu tujuan kamu:\n"
        "• /websupport — kendala website Bicolink\n"
        "• /advertise — mau beriklan di jaringan website Bicolink\n"
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


@dp.message(Command("setreplyphoto"))
async def set_reply_photo(m: Message):
    if m.from_user.id not in ADMINS:
        return

    if not m.photo:
        await m.answer("⚠️ Kirim <b>foto landscape 320×180</b> lalu ketik /setreplyphoto sebagai caption.")
        return

    file_id = m.photo[-1].file_id
    await set_setting("reply_photo_file_id", file_id)
    await m.answer("✅ Foto balasan berhasil diset. Semua reply admin ke user akan pakai foto ini.")


@dp.message(Command("endchat"))
async def end_chat(m: Message):
    target_user_id = None
    category = None

    if m.from_user.id in ADMINS:
        if not m.reply_to_message:
            await m.answer("⚠️ Admin: pakai <b>/endchat</b> dengan cara <b>reply</b> ke pesan INCOMING user.")
            return
        target_user_id, category = await resolve_reply_target(m.from_user.id, m.reply_to_message.message_id)
        if not target_user_id:
            await m.answer("⚠️ Tidak bisa menentukan user. Pastikan reply ke pesan yang bot kirim/forward.")
            return
    else:
        target_user_id = m.from_user.id
        category = await get_user_category(target_user_id) or CATEGORY_WEB  # fallback label

    transcript = await get_transcript_text(target_user_id)
    transcript_bytes = transcript.encode("utf-8", errors="ignore")
    bio = BytesIO(transcript_bytes)
    bio.seek(0)

    filename = f"bicolink_chat_{target_user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    doc = BufferedInputFile(bio.read(), filename=filename)

    await bot.send_document(
        chat_id=ARCHIVE_CHANNEL_ID,
        document=doc,
        caption=f"📦 <b>Chat Archive</b>\nUser ID: <code>{target_user_id}</code>",
        parse_mode=ParseMode.HTML
    )

    # delete messages bot can (best effort)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT chat_id, message_id FROM tg_message_index WHERE user_id=%s", (target_user_id,))
            rows = await cur.fetchall()

    deleted = 0
    for chat_id, message_id in rows:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except Exception:
            pass

    await cleanup_user_data(target_user_id)

    if m.from_user.id in ADMINS:
        await m.answer(
            f"✅ Chat user <code>{target_user_id}</code> diakhiri.\n"
            f"Arsip masuk channel.\n"
            f"Deleted (best effort): {deleted}"
        )
        try:
            await bot.send_message(target_user_id, "✅ Chat kamu sudah diakhiri. Terima kasih! Kalau perlu bantuan lagi, /start ya.")
        except Exception:
            pass
    else:
        await m.answer("✅ Chat kamu sudah diakhiri. Terima kasih! Kalau perlu bantuan lagi, /start ya.")


# =========================
# ROUTERS
# =========================

# USER -> ADMIN
@dp.message(F.from_user.id.not_in(ADMINS))
async def user_message_router(m: Message):
    await upsert_user(m)

    # ignore bot commands (handled elsewhere)
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

    if m.text:
        sent = await bot.send_message(admin_id, header + m.text)
        await map_admin_message(admin_id, sent.message_id, m.from_user.id, category)
        await index_bot_message(m.from_user.id, category, admin_id, sent.message_id, "admin")

        await log_message(
            direction="user_to_admin",
            category=category,
            user_id=m.from_user.id,
            admin_id=admin_id,
            text=m.text,
            tg_message_id=m.message_id,
        )
        return

    # media / non-text
    sent_header = await bot.send_message(admin_id, header + "<i>[media/message forwarded below]</i>")
    await map_admin_message(admin_id, sent_header.message_id, m.from_user.id, category)
    await index_bot_message(m.from_user.id, category, admin_id, sent_header.message_id, "admin")

    fwd = await bot.forward_message(chat_id=admin_id, from_chat_id=m.chat.id, message_id=m.message_id)
    await map_admin_message(admin_id, fwd.message_id, m.from_user.id, category)
    await index_bot_message(m.from_user.id, category, admin_id, fwd.message_id, "admin")

    await log_message(
        direction="user_to_admin",
        category=category,
        user_id=m.from_user.id,
        admin_id=admin_id,
        text=m.caption or "[non-text message]",
        tg_message_id=m.message_id,
    )


# ADMIN -> USER (must reply)
@dp.message(F.from_user.id.in_(ADMINS))
async def admin_reply_handler(m: Message):
    # allow /setreplyphoto and /endchat commands handled already
    if m.text and m.text.startswith("/"):
        return

    if not m.reply_to_message:
        await m.answer("⚠️ Balas (reply) pesan user yang mau kamu jawab, biar bot tau target user-nya.")
        return

    admin_id = m.from_user.id
    replied_id = m.reply_to_message.message_id

    user_id, category = await resolve_reply_target(admin_id, replied_id)
    if not user_id:
        await m.answer("⚠️ Target user tidak ditemukan. Pastikan reply ke pesan yang bot kirim/forward.")
        return

    expected_admin = CATEGORY_TO_ADMIN.get(category)
    if expected_admin != admin_id:
        await m.answer("⚠️ Kamu bukan admin untuk kategori chat ini.")
        return

    reply_photo_file_id = await get_setting("reply_photo_file_id")
    prefix = f"💬 <b>{CATEGORY_LABEL.get(category, category)}</b>\n"

    if m.text:
        if reply_photo_file_id:
            sent_u = await bot.send_photo(
                chat_id=user_id,
                photo=reply_photo_file_id,
                caption=prefix + m.text,
                parse_mode=ParseMode.HTML
            )
        else:
            sent_u = await bot.send_message(user_id, prefix + m.text)

        # index message in user chat so it can be deleted later (best effort)
        await index_bot_message(user_id, category, user_id, sent_u.message_id, "user")

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

    # Non-text from admin -> user
    copied = await bot.copy_message(chat_id=user_id, from_chat_id=m.chat.id, message_id=m.message_id)
    # copied is Message in aiogram 3
    try:
        await index_bot_message(user_id, category, user_id, copied.message_id, "user")
    except Exception:
        pass

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
