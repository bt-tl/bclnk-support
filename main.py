import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from psycopg_pool import AsyncConnectionPool

logging.basicConfig(level=logging.INFO)

# =========================
# ENV CONFIG (Railway)
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

ADMIN_WEB_ID = int(os.getenv("ADMIN_WEB_ID", "0"))
ADMIN_ADS_ID = int(os.getenv("ADMIN_ADS_ID", "0"))
ADMIN_REPORT_ID = int(os.getenv("ADMIN_REPORT_ID", "0"))

ARCHIVE_CHANNEL_ID = int(os.getenv("ARCHIVE_CHANNEL_ID", "0"))  # -100xxxxxxx (bot must be admin)
SUPPORT_BRAND = (os.getenv("SUPPORT_BRAND") or "Bicolink Support").strip()

# Social links (set these in Railway Environment Variables)
SOCIAL_TG_CHANNEL = (os.getenv("SOCIAL_TG_CHANNEL") or "").strip()  # https://t.me/xxxx
SOCIAL_TG_GROUP = (os.getenv("SOCIAL_TG_GROUP") or "").strip()      # https://t.me/xxxx
SOCIAL_YOUTUBE = (os.getenv("SOCIAL_YOUTUBE") or "").strip()        # https://youtube.com/...
SOCIAL_TIKTOK = (os.getenv("SOCIAL_TIKTOK") or "").strip()          # https://tiktok.com/@...
SOCIAL_FACEBOOK = (os.getenv("SOCIAL_FACEBOOK") or "").strip()      # https://facebook.com/...
SOCIAL_TWITTER = (os.getenv("SOCIAL_TWITTER") or "").strip()        # https://x.com/... or https://twitter.com/...

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum di-set.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL belum di-set.")
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
    CATEGORY_ADS: "📣 Advertise Specialist",
    CATEGORY_REPORT: "🚨 Report Link/Content",
}

# =========================
# DB POOL
# =========================
pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB")


def human_remaining(until: datetime) -> str:
    now = now_utc()
    if until <= now:
        return "0 menit"
    delta = until - now
    total_seconds = int(delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours <= 0:
        return f"{minutes} menit"
    return f"{hours} jam {minutes} menit"


def is_admin(uid: int) -> bool:
    return uid in ADMINS


def setting_key_reply_photo(category: str) -> str:
    # per kategori: reply_photo_file_id:websupport / advertise / reportlink
    return f"reply_photo_file_id:{category}"


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
    direction       TEXT NOT NULL,  -- 'user_to_admin' / 'admin_to_user' / 'system'
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
    role TEXT NOT NULL, -- 'admin_bot' / 'user_bot' / 'admin_user'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_bans (
    user_id      BIGINT PRIMARY KEY,
    banned_until TIMESTAMPTZ NOT NULL,
    reason       TEXT NOT NULL,
    banned_by    BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
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


async def clear_user_session(user_id: int):
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


async def resolve_target_from_admin_message(admin_id: int, admin_message_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user_id, category
                FROM admin_inbox_map
                WHERE admin_id=%s AND admin_message_id=%s
                """,
                (admin_id, admin_message_id),
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


async def index_message(user_id: int, category: str, chat_id: int, message_id: int, role: str):
    """
    role:
      - 'admin_bot'  : message yang BOT kirim ke admin
      - 'user_bot'   : message yang BOT kirim ke user
      - 'admin_user' : message admin (human) di chat admin-bot (untuk dibersihkan saat endchat)
    """
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
                SELECT created_at, direction, category, admin_id, COALESCE(text,'')
                FROM chat_messages
                WHERE user_id=%s
                ORDER BY created_at ASC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()

    lines = []
    for created_at, direction, category, admin_id, text in rows:
        ts = created_at.strftime("%Y-%m-%d %H:%M:%S")
        cat = CATEGORY_LABEL.get(category, category)
        if direction == "user_to_admin":
            who = "USER→ADMIN"
        elif direction == "admin_to_user":
            who = "ADMIN→USER"
        else:
            who = f"SYSTEM (admin:{admin_id})"
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


async def get_indexed_messages(user_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT chat_id, message_id FROM tg_message_index WHERE user_id=%s", (user_id,))
            return await cur.fetchall()


# ===== Ban helpers =====
async def get_active_ban(user_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT banned_until, reason, banned_by FROM user_bans WHERE user_id=%s",
                (user_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            banned_until, reason, banned_by = row
            if banned_until <= now_utc():
                await cur.execute("DELETE FROM user_bans WHERE user_id=%s", (user_id,))
                await conn.commit()
                return None
            return {"until": banned_until, "reason": reason, "by": banned_by}


async def set_ban_1day(user_id: int, banned_by: int, reason: str):
    until = now_utc() + timedelta(days=1)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_bans(user_id, banned_until, reason, banned_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET banned_until=EXCLUDED.banned_until,
                    reason=EXCLUDED.reason,
                    banned_by=EXCLUDED.banned_by,
                    created_at=NOW()
                """,
                (user_id, until, reason, banned_by),
            )
        await conn.commit()
    return until


# =========================
# UI (BUTTONS)
# =========================
def kb_user_menu():
    b = InlineKeyboardBuilder()
    b.button(text="🌐 Web Support", callback_data="pick:websupport")
    b.button(text="📣 Advertise", callback_data="pick:advertise")
    b.button(text="🚨 Report Link", callback_data="pick:reportlink")
    b.adjust(1)
    return b.as_markup()


def kb_user_endchat():
    b = InlineKeyboardBuilder()
    b.button(text="✅ End Chat", callback_data="user:endchat")
    return b.as_markup()


def kb_admin_actions():
    b = InlineKeyboardBuilder()
    b.button(text="✅ End Chat", callback_data="admin:endchat")
    b.button(text="⛔ Block 1 Hari", callback_data="admin:block1d")
    b.adjust(2)
    return b.as_markup()


def kb_admin_cancel():
    b = InlineKeyboardBuilder()
    b.button(text="✖️ Batalkan", callback_data="admin:cancel")
    return b.as_markup()


def kb_admin_panel():
    b = InlineKeyboardBuilder()
    b.button(text="🖼 Set Photo WebSupport", callback_data="admin:setphoto:websupport")
    b.button(text="🖼 Set Photo Advertise", callback_data="admin:setphoto:advertise")
    b.button(text="🖼 Set Photo ReportLink", callback_data="admin:setphoto:reportlink")
    b.adjust(1)
    return b.as_markup()


def kb_after_endchat_social():
    b = InlineKeyboardBuilder()

    # URL buttons (only if set)
    if SOCIAL_TG_CHANNEL:
        b.button(text="🔔 Subscribe Channel", url=SOCIAL_TG_CHANNEL)
    if SOCIAL_TG_GROUP:
        b.button(text="💬 Join Grup", url=SOCIAL_TG_GROUP)
    if SOCIAL_YOUTUBE:
        b.button(text="📺 YouTube Tutorial", url=SOCIAL_YOUTUBE)
    if SOCIAL_TIKTOK:
        b.button(text="🎵 TikTok", url=SOCIAL_TIKTOK)
    if SOCIAL_FACEBOOK:
        b.button(text="📘 Facebook", url=SOCIAL_FACEBOOK)
    if SOCIAL_TWITTER:
        b.button(text="🐦 Twitter/X", url=SOCIAL_TWITTER)

    # always add "chat again"
    b.button(text="🧑‍💻 Chat Lagi dengan Support", callback_data="user:chatagain")
    # layout
    b.adjust(2)
    return b.as_markup()


# =========================
# BOT INIT
# =========================
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# pending state: admin sedang diminta isi alasan block
PENDING_BLOCK_REASON: dict[int, dict] = {}
# pending state: admin sedang diminta kirim foto kategori tertentu
PENDING_SET_PHOTO: dict[int, dict] = {}


def build_user_identity(m: Message) -> str:
    u = m.from_user
    name = (u.full_name or "").strip()
    uname = f"@{u.username}" if u.username else "-"
    return f"{name} ({uname}) | <code>{u.id}</code>"


async def send_user_menu(user_id: int, note: str | None = None):
    text = (
        f"👋 Halo! Ini <b>{SUPPORT_BRAND}</b> via Telegram.\n\n"
        "Sebelum chat masuk ke admin, pilih dulu tujuan kamu (hanya boleh 1):"
    )
    if note:
        text = note + "\n\n" + text
    msg = await bot.send_message(user_id, text, reply_markup=kb_user_menu())
    await index_message(user_id, "system", user_id, msg.message_id, "user_bot")


async def send_user_session_locked(user_id: int, category: str):
    text = (
        f"✅ Kamu sedang berada di sesi <b>{CATEGORY_LABEL.get(category, category)}</b>.\n"
        f"Kalau mau pindah kategori lain, kamu harus <b>End Chat</b> dulu."
    )
    msg = await bot.send_message(user_id, text, reply_markup=kb_user_endchat())
    await index_message(user_id, "system", user_id, msg.message_id, "user_bot")


async def send_user_ban_notice(user_id: int, ban: dict):
    until = ban["until"]
    reason = ban["reason"]
    msg = await bot.send_message(
        user_id,
        "⛔ <b>Kamu sedang diblok selama 1 hari</b>\n"
        f"Alasan: <i>{reason}</i>\n"
        f"Selesai: <b>{fmt_dt(until)}</b>\n"
        f"Sisa waktu: <b>{human_remaining(until)}</b>",
    )
    await index_message(user_id, "system", user_id, msg.message_id, "user_bot")


# =========================
# START
# =========================
@dp.message(F.text == "/start")
async def start_cmd(m: Message):
    await upsert_user(m)

    if is_admin(m.from_user.id):
        await m.answer(
            "✅ <b>Admin Panel</b>\n"
            "• Pesan user masuk sesuai kategori\n"
            "• Balas user dengan <b>Reply</b> pesan INCOMING\n"
            "• Gunakan tombol pada INCOMING: <b>End Chat</b> / <b>Block 1 Hari</b>\n\n"
            "Set banner balasan per kategori (pakai tombol):",
            reply_markup=kb_admin_panel()
        )
        return

    # non-admin: jika sudah punya sesi aktif, jangan tampilkan menu awal lagi
    category = await get_user_category(m.from_user.id)
    if category:
        await send_user_session_locked(m.from_user.id, category)
        return

    await send_user_menu(m.from_user.id)


# =========================
# ADMIN: SET PHOTO PER CATEGORY (BUTTON)
# =========================
@dp.callback_query(F.data.startswith("admin:setphoto:"))
async def admin_setphoto_btn(q: CallbackQuery):
    admin_id = q.from_user.id
    if not is_admin(admin_id):
        await q.answer("Unauthorized", show_alert=True)
        return

    category = q.data.split(":", 2)[2].strip()
    if category not in CATEGORY_TO_ADMIN:
        await q.answer("Kategori tidak valid.", show_alert=True)
        return

    PENDING_SET_PHOTO[admin_id] = {"category": category}
    await q.answer()
    await q.message.answer(
        f"🖼 Kirim <b>foto landscape 320×180</b> untuk kategori <b>{CATEGORY_LABEL.get(category, category)}</b>.\n"
        "Cukup kirim foto sekarang (tanpa command).",
        reply_markup=kb_admin_cancel()
    )


# =========================
# USER PICK CATEGORY (BUTTON)
# =========================
@dp.callback_query(F.data.startswith("pick:"))
async def pick_category(q: CallbackQuery):
    await q.answer()
    user_id = q.from_user.id

    if is_admin(user_id):
        await q.answer("Admin tidak perlu memilih kategori.", show_alert=True)
        return

    # cek session lock
    active = await get_user_category(user_id)
    if active:
        await q.answer("Kamu masih punya sesi aktif. End Chat dulu.", show_alert=True)
        await send_user_session_locked(user_id, active)
        return

    # cek ban
    ban = await get_active_ban(user_id)
    if ban:
        await q.answer("Kamu sedang diblok sementara.", show_alert=True)
        await send_user_ban_notice(user_id, ban)
        return

    category = q.data.split(":", 1)[1].strip()
    if category not in CATEGORY_TO_ADMIN:
        await q.answer("Kategori tidak valid.", show_alert=True)
        return

    await set_user_category(user_id, category)

    msg = await bot.send_message(
        user_id,
        f"✅ Oke! Kamu masuk ke <b>{CATEGORY_LABEL.get(category, category)}</b>.\n"
        "Silakan tulis pesan kamu. Jika sudah selesai, tekan <b>End Chat</b>.",
        reply_markup=kb_user_endchat()
    )
    await index_message(user_id, category, user_id, msg.message_id, "user_bot")


# =========================
# USER ENDCHAT (BUTTON)
# =========================
@dp.callback_query(F.data == "user:endchat")
async def user_endchat(q: CallbackQuery):
    await q.answer()
    user_id = q.from_user.id
    if is_admin(user_id):
        await q.answer("Admin tidak pakai tombol ini.", show_alert=True)
        return
    await do_endchat(target_user_id=user_id, ended_by=user_id)


@dp.callback_query(F.data == "user:chatagain")
async def user_chat_again(q: CallbackQuery):
    await q.answer()
    user_id = q.from_user.id
    if is_admin(user_id):
        return
    category = await get_user_category(user_id)
    if category:
        await send_user_session_locked(user_id, category)
        return
    await send_user_menu(user_id)


# =========================
# ADMIN ACTIONS (BUTTON)
# =========================
@dp.callback_query(F.data == "admin:endchat")
async def admin_endchat_btn(q: CallbackQuery):
    admin_id = q.from_user.id
    if not is_admin(admin_id):
        await q.answer("Unauthorized", show_alert=True)
        return

    admin_msg_id = q.message.message_id
    user_id, category = await resolve_target_from_admin_message(admin_id, admin_msg_id)
    if not user_id:
        await q.answer("Target user tidak ditemukan.", show_alert=True)
        return

    await q.answer("Ending chat...")
    await do_endchat(target_user_id=user_id, ended_by=admin_id, category_hint=category)
    try:
        await q.message.answer(f"✅ Chat user <code>{user_id}</code> diakhiri.")
    except Exception:
        pass


@dp.callback_query(F.data == "admin:block1d")
async def admin_block_btn(q: CallbackQuery):
    admin_id = q.from_user.id
    if not is_admin(admin_id):
        await q.answer("Unauthorized", show_alert=True)
        return

    admin_msg_id = q.message.message_id
    user_id, category = await resolve_target_from_admin_message(admin_id, admin_msg_id)
    if not user_id:
        await q.answer("Target user tidak ditemukan.", show_alert=True)
        return

    PENDING_BLOCK_REASON[admin_id] = {
        "user_id": user_id,
        "category": category,
        "admin_msg_id": admin_msg_id,
    }

    await q.answer()
    await q.message.answer(
        f"⛔ <b>Block 1 hari</b> untuk user <code>{user_id}</code>\n"
        "Kirim <b>alasan</b> (teks) sekarang.\n"
        "Atau tekan Batalkan.",
        reply_markup=kb_admin_cancel()
    )


@dp.callback_query(F.data == "admin:cancel")
async def admin_cancel(q: CallbackQuery):
    admin_id = q.from_user.id
    PENDING_BLOCK_REASON.pop(admin_id, None)
    PENDING_SET_PHOTO.pop(admin_id, None)
    await q.answer("Dibatalkan.")
    try:
        await q.message.answer("✅ Dibatalkan.")
    except Exception:
        pass


# =========================
# ADMIN MESSAGE HANDLER
# - menerima:
#   1) foto untuk set banner per kategori (pending)
#   2) teks alasan block (pending)
#   3) balasan normal admin ke user (reply)
# =========================
@dp.message(F.from_user.id.in_(ADMINS))
async def admin_message_handler(m: Message):
    admin_id = m.from_user.id

    # 1) pending set photo: admin kirim foto
    pending_photo = PENDING_SET_PHOTO.get(admin_id)
    if pending_photo and m.photo:
        category = pending_photo["category"]
        file_id = m.photo[-1].file_id
        await set_setting(setting_key_reply_photo(category), file_id)
        PENDING_SET_PHOTO.pop(admin_id, None)
        await m.answer(f"✅ Banner untuk <b>{CATEGORY_LABEL.get(category, category)}</b> berhasil diset.")
        return

    # 2) pending block reason
    pending_block = PENDING_BLOCK_REASON.get(admin_id)
    if pending_block and m.text and not m.text.startswith("/"):
        user_id = pending_block["user_id"]
        category = pending_block["category"] or "system"
        reason = m.text.strip()

        until = await set_ban_1day(user_id, admin_id, reason)

        # index pesan alasan admin (supaya bisa dibersihkan saat endchat)
        await index_message(user_id, category, admin_id, m.message_id, "admin_user")

        # log system
        await log_message(
            direction="system",
            category=category,
            user_id=user_id,
            admin_id=admin_id,
            text=f"BLOCK_1D until={until.isoformat()} reason={reason}",
            tg_message_id=m.message_id
        )

        # catat ke archive channel
        await bot.send_message(
            ARCHIVE_CHANNEL_ID,
            "⛔ <b>BLOCK 1 HARI</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Admin: <code>{admin_id}</code>\n"
            f"Sampai: <b>{fmt_dt(until)}</b>\n"
            f"Alasan: <i>{reason}</i>",
            parse_mode=ParseMode.HTML
        )

        # notif user
        try:
            await send_user_ban_notice(user_id, {"until": until, "reason": reason, "by": admin_id})
        except Exception:
            pass

        PENDING_BLOCK_REASON.pop(admin_id, None)
        await m.answer("✅ User berhasil diblok 1 hari dan dicatat ke archive channel.")
        return

    # ignore admin slash commands besides /start
    if m.text and m.text.startswith("/"):
        return

    # 3) normal reply admin -> user
    if not m.reply_to_message:
        await m.answer("⚠️ Untuk balas user, gunakan <b>Reply</b> ke pesan INCOMING.")
        return

    replied_admin_msg_id = m.reply_to_message.message_id
    user_id, category = await resolve_target_from_admin_message(admin_id, replied_admin_msg_id)
    if not user_id:
        await m.answer("⚠️ Target user tidak ditemukan. Pastikan reply ke pesan INCOMING dari bot.")
        return

    expected_admin = CATEGORY_TO_ADMIN.get(category)
    if expected_admin != admin_id:
        await m.answer("⚠️ Kamu bukan admin untuk kategori chat ini.")
        return

    # ambil banner per kategori
    reply_photo_file_id = await get_setting(setting_key_reply_photo(category))
    prefix = f"💬 <b>{CATEGORY_LABEL.get(category, category)}</b>\n"

    # index pesan admin (human) di sisi admin supaya bisa dihapus saat endchat
    await index_message(user_id, category, admin_id, m.message_id, "admin_user")

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

        await index_message(user_id, category, user_id, sent_u.message_id, "user_bot")
        await log_message(
            direction="admin_to_user",
            category=category,
            user_id=user_id,
            admin_id=admin_id,
            text=m.text,
            tg_message_id=m.message_id,
            tg_reply_to_id=replied_admin_msg_id
        )
        return

    # non-text admin -> user
    copied = await bot.copy_message(chat_id=user_id, from_chat_id=m.chat.id, message_id=m.message_id)
    try:
        await index_message(user_id, category, user_id, copied.message_id, "user_bot")
    except Exception:
        pass
    await log_message(
        direction="admin_to_user",
        category=category,
        user_id=user_id,
        admin_id=admin_id,
        text=m.caption or "[non-text message]",
        tg_message_id=m.message_id,
        tg_reply_to_id=replied_admin_msg_id
    )


# =========================
# USER MESSAGE ROUTER
# =========================
@dp.message(F.from_user.id.not_in(ADMINS))
async def user_message_router(m: Message):
    await upsert_user(m)

    # block any command typing -> force buttons
    if m.text and m.text.startswith("/"):
        category = await get_user_category(m.from_user.id)
        if category:
            await send_user_session_locked(m.from_user.id, category)
        else:
            await send_user_menu(m.from_user.id, note="⚠️ Gunakan tombol untuk memilih kategori ya.")
        return

    # check ban
    ban = await get_active_ban(m.from_user.id)
    if ban:
        await send_user_ban_notice(m.from_user.id, ban)
        return

    category = await get_user_category(m.from_user.id)
    if not category:
        await send_user_menu(m.from_user.id, note="⚠️ Pilih dulu salah satu kategori (hanya 1).")
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
        sent = await bot.send_message(
            admin_id,
            header + m.text,
            reply_markup=kb_admin_actions()
        )
        await map_admin_message(admin_id, sent.message_id, m.from_user.id, category)
        await index_message(m.from_user.id, category, admin_id, sent.message_id, "admin_bot")

        await log_message(
            direction="user_to_admin",
            category=category,
            user_id=m.from_user.id,
            admin_id=admin_id,
            text=m.text,
            tg_message_id=m.message_id
        )
        return

    # media/non-text: send header with actions, then forward media
    sent_header = await bot.send_message(
        admin_id,
        header + "<i>[media forwarded below]</i>",
        reply_markup=kb_admin_actions()
    )
    await map_admin_message(admin_id, sent_header.message_id, m.from_user.id, category)
    await index_message(m.from_user.id, category, admin_id, sent_header.message_id, "admin_bot")

    fwd = await bot.forward_message(chat_id=admin_id, from_chat_id=m.chat.id, message_id=m.message_id)
    await map_admin_message(admin_id, fwd.message_id, m.from_user.id, category)
    await index_message(m.from_user.id, category, admin_id, fwd.message_id, "admin_bot")

    await log_message(
        direction="user_to_admin",
        category=category,
        user_id=m.from_user.id,
        admin_id=admin_id,
        text=m.caption or "[non-text message]",
        tg_message_id=m.message_id
    )


# =========================
# ENDCHAT CORE
# =========================
async def do_endchat(target_user_id: int, ended_by: int, category_hint: str | None = None):
    category = category_hint or (await get_user_category(target_user_id)) or "system"

    # 1) transcript from DB
    transcript = await get_transcript_text(target_user_id)
    transcript_bytes = transcript.encode("utf-8", errors="ignore")
    bio = BytesIO(transcript_bytes)
    bio.seek(0)

    filename = f"bicolink_chat_{target_user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    doc = BufferedInputFile(bio.read(), filename=filename)

    # 2) archive 1 bubble (document) to channel
    await bot.send_document(
        chat_id=ARCHIVE_CHANNEL_ID,
        document=doc,
        caption=(
            f"📦 <b>CHAT ARCHIVE</b>\n"
            f"User: <code>{target_user_id}</code>\n"
            f"Ended by: <code>{ended_by}</code>\n"
            f"Category: <b>{CATEGORY_LABEL.get(category, category)}</b>\n"
            f"Time: <b>{fmt_dt(now_utc())}</b>"
        ),
        parse_mode=ParseMode.HTML
    )

    # 3) delete indexed messages (best effort)
    rows = await get_indexed_messages(target_user_id)
    deleted = 0
    for chat_id, message_id in rows:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except Exception:
            pass

    # 4) wipe DB for that user
    await cleanup_user_data(target_user_id)

    # 5) show social buttons first + option chat again
    try:
        msg = await bot.send_message(
            target_user_id,
            "✅ Chat kamu sudah diakhiri.\n\n"
            "Sebelum chat lagi, cek komunitas & tutorial Bicolink dulu ya 👇",
            reply_markup=kb_after_endchat_social()
        )
        # index so it can be deleted on next endchat (best effort)
        await index_message(target_user_id, "system", target_user_id, msg.message_id, "user_bot")
    except Exception:
        pass

    logging.info(f"ENDCHAT user={target_user_id} ended_by={ended_by} deleted_best_effort={deleted}")


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
