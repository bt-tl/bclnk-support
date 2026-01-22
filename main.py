import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
)
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

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


def fmt_dt(dt: datetime) -> str:
    # tampilkan UTC agar konsisten (bisa kamu ubah ke WIB kalau mau)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


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

-- user memilih jalur support apa (aktif). user hanya boleh 1 session aktif.
CREATE TABLE IF NOT EXISTS user_sessions (
    user_id     BIGINT PRIMARY KEY REFERENCES tg_users(user_id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- semua chat dicatat (direction bebas: user_to_admin/admin_to_user/system)
CREATE TABLE IF NOT EXISTS chat_messages (
    id              BIGSERIAL PRIMARY KEY,
    direction       TEXT NOT NULL,
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

-- simpan setting file_id banner balasan
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- index pesan yang BOT kirim/forward/copy agar bisa delete best-effort saat endchat
CREATE TABLE IF NOT EXISTS tg_message_index (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    role TEXT NOT NULL, -- 'admin' atau 'user'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- banned users 1 hari
CREATE TABLE IF NOT EXISTS banned_users (
    user_id BIGINT PRIMARY KEY,
    banned_until TIMESTAMPTZ NOT NULL,
    reason TEXT NOT NULL,
    banned_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- pending action untuk admin (mis. menunggu alasan ban)
CREATE TABLE IF NOT EXISTS admin_pending_actions (
    admin_id BIGINT PRIMARY KEY,
    action TEXT NOT NULL,              -- 'ban_reason'
    target_user_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    ref_admin_message_id BIGINT,
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


async def log_message(
    direction: str,
    category: str,
    user_id: int,
    admin_id: int,
    text: str | None,
    tg_message_id: int | None = None,
    tg_reply_to_id: int | None = None,
):
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

    lines: list[str] = []
    for created_at, direction, category, text in rows:
        ts = created_at.strftime("%Y-%m-%d %H:%M:%S")
        cat = CATEGORY_LABEL.get(category, category)
        lines.append(f"[{ts}] [{cat}] {direction}: {text}")
    return "\n".join(lines) if lines else "(no messages)"


async def cleanup_user_data(user_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM user_sessions WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM admin_inbox_map WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM chat_messages WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM tg_message_index WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM admin_pending_actions WHERE target_user_id=%s", (user_id,))
        await conn.commit()


# ---------- BAN ----------
async def get_active_ban(user_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT banned_until, reason, banned_by FROM banned_users WHERE user_id=%s",
                (user_id,),
            )
            row = await cur.fetchone()

    if not row:
        return None

    banned_until, reason, banned_by = row
    if banned_until <= now_utc():
        # ban expired -> cleanup
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))
            await conn.commit()
        return None

    return {"banned_until": banned_until, "reason": reason, "banned_by": banned_by}


async def set_ban_1day(user_id: int, admin_id: int, reason: str):
    until = now_utc() + timedelta(days=1)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO banned_users(user_id, banned_until, reason, banned_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET banned_until=EXCLUDED.banned_until,
                    reason=EXCLUDED.reason,
                    banned_by=EXCLUDED.banned_by,
                    created_at=NOW()
                """,
                (user_id, until, reason, admin_id),
            )
        await conn.commit()
    return until


async def set_pending_action(admin_id: int, action: str, target_user_id: int, category: str, ref_admin_message_id: int | None):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO admin_pending_actions(admin_id, action, target_user_id, category, ref_admin_message_id, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (admin_id) DO UPDATE
                SET action=EXCLUDED.action,
                    target_user_id=EXCLUDED.target_user_id,
                    category=EXCLUDED.category,
                    ref_admin_message_id=EXCLUDED.ref_admin_message_id,
                    created_at=NOW()
                """,
                (admin_id, action, target_user_id, category, ref_admin_message_id),
            )
        await conn.commit()


async def pop_pending_action(admin_id: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT action, target_user_id, category, ref_admin_message_id FROM admin_pending_actions WHERE admin_id=%s",
                (admin_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            await cur.execute("DELETE FROM admin_pending_actions WHERE admin_id=%s", (admin_id,))
        await conn.commit()
    action, target_user_id, category, ref_admin_message_id = row
    return {
        "action": action,
        "target_user_id": target_user_id,
        "category": category,
        "ref_admin_message_id": ref_admin_message_id,
    }


# =========================
# UI KEYBOARDS
# =========================
def kb_user_pick_or_end(active_category: str | None):
    kb = InlineKeyboardBuilder()

    if active_category:
        kb.button(text=f"✅ Aktif: {CATEGORY_LABEL.get(active_category, active_category)}", callback_data="noop")
        kb.button(text="🛑 End Chat", callback_data="user:endchat")
        kb.adjust(1, 1)
        return kb.as_markup()

    kb.button(text="🌐 Web Support", callback_data="pick:websupport")
    kb.button(text="📣 Advertise", callback_data="pick:advertise")
    kb.button(text="🚨 Report Link", callback_data="pick:reportlink")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def kb_admin_incoming(user_id: int, category: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛑 End Chat", callback_data=f"admin:endchat:{user_id}")
    kb.button(text="⛔ Block 1 hari", callback_data=f"admin:block:{user_id}:{category}")
    kb.adjust(2)
    return kb.as_markup()


def kb_admin_panel():
    kb = InlineKeyboardBuilder()
    kb.button(text="🖼️ Set Foto Balasan (kirim foto + caption)", callback_data="admin:how_setphoto")
    kb.adjust(1)
    return kb.as_markup()


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
# START (welcome only for non-admin)
# =========================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await upsert_user(m)

    if m.from_user.id in ADMINS:
        txt = (
            "✅ <b>Admin Panel</b>\n"
            "• Balas (reply) pesan <b>INCOMING</b> untuk menjawab user\n"
            "• Gunakan tombol <b>Block 1 hari</b> / <b>End Chat</b> di pesan INCOMING\n"
            "• Set foto balasan: kirim foto landscape 320×180 ke bot dengan caption <b>/setreplyphoto</b>\n"
        )
        await m.answer(txt, reply_markup=kb_admin_panel())
        return

    active = await get_user_category(m.from_user.id)
    if active:
        txt = (
            f"👋 Halo! Ini <b>{SUPPORT_BRAND}</b> via Telegram.\n\n"
            f"Kamu sedang berada di sesi: <b>{CATEGORY_LABEL.get(active, active)}</b>\n"
            "Untuk pindah tujuan, kamu wajib <b>End Chat</b> dulu."
        )
        await m.answer(txt, reply_markup=kb_user_pick_or_end(active))
        return

    txt = (
        f"👋 Halo! Ini <b>{SUPPORT_BRAND}</b> via Telegram.\n\n"
        "Sebelum chat masuk ke admin, pilih dulu tujuan kamu (1 saja):"
    )
    await m.answer(txt, reply_markup=kb_user_pick_or_end(None))


# =========================
# USER PICK CATEGORY (buttons)
# =========================
@dp.callback_query(F.data.startswith("pick:"))
async def user_pick_category(cb: CallbackQuery):
    user_id = cb.from_user.id
    if user_id in ADMINS:
        await cb.answer("Admin tidak perlu memilih kategori.", show_alert=True)
        return

    await upsert_user(cb.message)  # safe: message has from_user

    current = await get_user_category(user_id)
    if current:
        await cb.answer("Kamu sudah punya sesi aktif. End Chat dulu untuk pindah.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=kb_user_pick_or_end(current))
        except Exception:
            pass
        return

    category = cb.data.split(":", 1)[1].strip()
    if category not in CATEGORY_TO_ADMIN:
        await cb.answer("Kategori tidak valid.", show_alert=True)
        return

    await set_user_category(user_id, category)
    await cb.answer("✅ Berhasil dipilih.", show_alert=False)

    msg = (
        f"✅ Oke! Kamu masuk ke <b>{CATEGORY_LABEL.get(category, category)}</b>.\n"
        "Silakan tulis pesan kamu sekarang."
    )
    try:
        await cb.message.edit_text(msg, reply_markup=kb_user_pick_or_end(category))
    except Exception:
        await cb.message.answer(msg, reply_markup=kb_user_pick_or_end(category))


@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# =========================
# USER END CHAT (button)
# =========================
@dp.callback_query(F.data == "user:endchat")
async def user_endchat_button(cb: CallbackQuery):
    if cb.from_user.id in ADMINS:
        await cb.answer("Admin tidak pakai tombol ini.", show_alert=True)
        return
    await cb.answer()
    await end_chat_for_user(
        actor_id=cb.from_user.id,
        target_user_id=cb.from_user.id,
        actor_is_admin=False,
        notify_actor_chat_id=cb.from_user.id,
    )
    try:
        await cb.message.edit_text(
            "✅ Chat kamu sudah diakhiri.\nKlik /start untuk mulai lagi.",
            reply_markup=None,
        )
    except Exception:
        pass


# =========================
# ADMIN BUTTONS (End Chat / Block)
# =========================
@dp.callback_query(F.data.startswith("admin:endchat:"))
async def admin_endchat_button(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        await cb.answer("Khusus admin.", show_alert=True)
        return
    await cb.answer()

    try:
        target_user_id = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer("Data invalid.", show_alert=True)
        return

    await end_chat_for_user(
        actor_id=cb.from_user.id,
        target_user_id=target_user_id,
        actor_is_admin=True,
        notify_actor_chat_id=cb.from_user.id,
    )

    # delete the admin message bubble that has the buttons (best effort)
    try:
        await cb.message.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("admin:block:"))
async def admin_block_button(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        await cb.answer("Khusus admin.", show_alert=True)
        return
    await cb.answer()

    parts = cb.data.split(":")
    # admin:block:<user_id>:<category>
    if len(parts) < 4:
        await cb.answer("Data invalid.", show_alert=True)
        return

    target_user_id = int(parts[2])
    category = parts[3]

    # simpan pending action: menunggu alasan
    await set_pending_action(
        admin_id=cb.from_user.id,
        action="ban_reason",
        target_user_id=target_user_id,
        category=category,
        ref_admin_message_id=cb.message.message_id if cb.message else None,
    )

    await cb.message.answer(
        f"⛔ <b>Block 1 hari</b>\n"
        f"Target: <code>{target_user_id}</code>\n\n"
        "Silakan kirim <b>alasan</b> (1 pesan) sekarang.\n"
        "Contoh: <i>spam / bahasa kasar / flood</i>",
    )


@dp.callback_query(F.data == "admin:how_setphoto")
async def admin_how_setphoto(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        await cb.answer("Khusus admin.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(
        "Untuk set foto kecil 320×180 yang ikut di setiap balasan admin:\n"
        "1) Kirim foto landscape 320×180 ke bot\n"
        "2) Isi caption: <b>/setreplyphoto</b>\n"
        "Selesai ✅"
    )


# =========================
# SET REPLY PHOTO (still command; button-only for selection, not for file-id)
# =========================
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


# =========================
# CORE: END CHAT (archive -> delete best-effort -> wipe DB)
# =========================
async def archive_to_channel(target_user_id: int, transcript: str, extra_caption: str | None = None):
    transcript_bytes = transcript.encode("utf-8", errors="ignore")
    bio = BytesIO(transcript_bytes)
    bio.seek(0)

    filename = f"bicolink_chat_{target_user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    doc = BufferedInputFile(bio.read(), filename=filename)

    caption = f"📦 <b>Chat Archive</b>\nUser ID: <code>{target_user_id}</code>"
    if extra_caption:
        caption += f"\n{extra_caption}"

    await bot.send_document(
        chat_id=ARCHIVE_CHANNEL_ID,
        document=doc,
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


async def delete_indexed_messages(target_user_id: int) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT chat_id, message_id FROM tg_message_index WHERE user_id=%s",
                (target_user_id,),
            )
            rows = await cur.fetchall()

    deleted = 0
    for chat_id, message_id in rows:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except Exception:
            pass
    return deleted


async def end_chat_for_user(
    actor_id: int,
    target_user_id: int,
    actor_is_admin: bool,
    notify_actor_chat_id: int,
):
    # transcript from DB
    transcript = await get_transcript_text(target_user_id)

    # archive (single bubble = 1 file)
    await archive_to_channel(
        target_user_id=target_user_id,
        transcript=transcript,
        extra_caption=f"Ended by: <code>{actor_id}</code>",
    )

    # delete messages best-effort (bot-sent/forwarded)
    deleted = await delete_indexed_messages(target_user_id)

    # wipe DB data for user (including ban and pending)
    await cleanup_user_data(target_user_id)

    # notify actor
    if actor_is_admin:
        await bot.send_message(
            notify_actor_chat_id,
            f"✅ End Chat sukses untuk user <code>{target_user_id}</code>.\n"
            f"Arsip sudah masuk channel.\n"
            f"Deleted (best effort): {deleted}",
        )
        # notify user
        try:
            await bot.send_message(
                target_user_id,
                "✅ Chat kamu sudah diakhiri.\nKalau perlu bantuan lagi, tekan /start ya.",
            )
        except Exception:
            pass
    else:
        # user end chat
        try:
            await bot.send_message(
                notify_actor_chat_id,
                "✅ Chat kamu sudah diakhiri.\nKalau perlu bantuan lagi, tekan /start ya.",
            )
        except Exception:
            pass


# =========================
# USER -> ADMIN ROUTER
# =========================
@dp.message(F.from_user.id.not_in(ADMINS))
async def user_message_router(m: Message):
    await upsert_user(m)

    # block check
    ban = await get_active_ban(m.from_user.id)
    if ban:
        until = ban["banned_until"]
        reason = ban["reason"]
        await m.answer(
            "⛔ <b>Akun kamu sedang dibatasi</b>\n"
            f"Durasi: sampai <b>{fmt_dt(until)}</b>\n"
            f"Alasan: <i>{reason}</i>\n\n"
            "Jika kamu merasa ini keliru, silakan coba lagi setelah masa blokir selesai.",
        )
        return

    # ignore commands (we don't advertise; but still allow /start)
    if m.text and m.text.startswith("/"):
        return

    category = await get_user_category(m.from_user.id)
    if not category:
        await m.answer(
            "⚠️ Kamu belum memilih tujuan.\nKlik /start lalu pilih salah satu tombol."
        )
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

    # Send to admin with action buttons (End Chat / Block 1 day)
    if m.text:
        sent = await bot.send_message(
            admin_id,
            header + m.text,
            reply_markup=kb_admin_incoming(m.from_user.id, category),
        )
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

    # media / non-text: send header + forward
    sent_header = await bot.send_message(
        admin_id,
        header + "<i>[media/message forwarded below]</i>",
        reply_markup=kb_admin_incoming(m.from_user.id, category),
    )
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


# =========================
# ADMIN: capture pending ban reason
# =========================
@dp.message(F.from_user.id.in_(ADMINS))
async def admin_message_handler(m: Message):
    # ignore commands except /setreplyphoto (handled) and /start (handled)
    if m.text and m.text.startswith("/"):
        return

    pending = await pop_pending_action(m.from_user.id)
    if pending and pending["action"] == "ban_reason":
        target_user_id = int(pending["target_user_id"])
        category = pending["category"]
        reason = (m.text or "").strip()

        if not reason:
            await m.answer("⚠️ Alasan tidak boleh kosong. Klik Block lagi kalau mau ulang.")
            return

        banned_until = await set_ban_1day(target_user_id, m.from_user.id, reason)

        # log system event
        await log_message(
            direction="system",
            category=category,
            user_id=target_user_id,
            admin_id=m.from_user.id,
            text=f"USER BLOCKED 1 DAY until {fmt_dt(banned_until)} | reason: {reason}",
        )

        # archive ban event to channel (single bubble message)
        await bot.send_message(
            ARCHIVE_CHANNEL_ID,
            (
                "⛔ <b>Ban Event</b>\n"
                f"User ID: <code>{target_user_id}</code>\n"
                f"Category: <b>{CATEGORY_LABEL.get(category, category)}</b>\n"
                f"Banned by: <code>{m.from_user.id}</code>\n"
                f"Until: <b>{fmt_dt(banned_until)}</b>\n"
                f"Reason: <i>{reason}</i>"
            ),
            parse_mode=ParseMode.HTML,
        )

        # notify admin + user
        await m.answer(
            f"✅ User <code>{target_user_id}</code> diblokir 1 hari.\n"
            f"Sampai: <b>{fmt_dt(banned_until)}</b>\n"
            f"Alasan: <i>{reason}</i>"
        )
        try:
            await bot.send_message(
                target_user_id,
                "⛔ <b>Kamu diblokir dari support selama 1 hari</b>\n"
                f"Sampai: <b>{fmt_dt(banned_until)}</b>\n"
                f"Alasan: <i>{reason}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    # If not pending, treat as normal admin reply (must reply)
    if not m.reply_to_message:
        await m.answer("⚠️ Balas (reply) pesan INCOMING user untuk menjawab user.")
        return

    admin_id = m.from_user.id
    replied_id = m.reply_to_message.message_id

    user_id, category = await resolve_reply_target(admin_id, replied_id)
    if not user_id:
        await m.answer("⚠️ Target user tidak ditemukan. Reply ke pesan yang bot kirim/forward.")
        return

    expected_admin = CATEGORY_TO_ADMIN.get(category)
    if expected_admin != admin_id:
        await m.answer("⚠️ Kamu bukan admin untuk kategori chat ini.")
        return

    reply_photo_file_id = await get_setting("reply_photo_file_id")
    prefix = f"💬 <b>{CATEGORY_LABEL.get(category, category)}</b>\n"

    # If user got banned after incoming, still allow admin to reply (opsional).
    if m.text:
        if reply_photo_file_id:
            sent_u = await bot.send_photo(
                chat_id=user_id,
                photo=reply_photo_file_id,
                caption=prefix + m.text,
                parse_mode=ParseMode.HTML,
            )
        else:
            sent_u = await bot.send_message(user_id, prefix + m.text)

        # index bot message in user chat for best-effort delete on endchat
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

    # admin sends media -> user
    copied = await bot.copy_message(chat_id=user_id, from_chat_id=m.chat.id, message_id=m.message_id)
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
# OPTIONAL: keep /endchat command as fallback (not advertised)
# =========================
@dp.message(Command("endchat"))
async def endchat_command_fallback(m: Message):
    if m.from_user.id in ADMINS:
        if not m.reply_to_message:
            await m.answer("Admin: /endchat harus reply ke pesan INCOMING user.")
            return
        target_user_id, _cat = await resolve_reply_target(m.from_user.id, m.reply_to_message.message_id)
        if not target_user_id:
            await m.answer("Target user tidak ditemukan.")
            return
        await end_chat_for_user(
            actor_id=m.from_user.id,
            target_user_id=target_user_id,
            actor_is_admin=True,
            notify_actor_chat_id=m.from_user.id,
        )
        return

    # user end chat dirinya sendiri
    await end_chat_for_user(
        actor_id=m.from_user.id,
        target_user_id=m.from_user.id,
        actor_is_admin=False,
        notify_actor_chat_id=m.from_user.id,
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
