# -*- coding: utf-8 -*-
"""
coding by amirwebcode : telegram = @saeqehe
pip install aiogram rubpy pycryptodome
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15 as pkcs1_15_sig

from rubpy import Client as RubikaClient
from rubpy.crypto import Crypto
from rubpy.enums import ReportType

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Update
from aiogram.enums import ButtonStyle, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ForceReply,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8179859382:AAHnhHIa5-DXV923UfRdbEgOUnLO5P79qIs"
ADMIN_IDS: set[int] = {8503523539}
ADMIN_USERNAME = "@Saeqehe"

ADMIN_CARD_NUMBER = "6219861453153586"
PREMIUM_MONTHS   = 1

REQUIRED_CHANNELS = ["mrvpn294", "amirwebcode1"]

FREE_LIMIT    = 100
PREMIUM_LIMIT = 1000
PREMIUM_PRICE = "350,000 تومان"
REPORT_DELAY  = 3

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(BASE_DIR, "users.db")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

REPORT_TYPES_MAP: dict[str, tuple[str, ReportType]] = {
    "1": ("🔞 محتوای مستهجن", ReportType.PORNOGRAPHY),
    "2": ("⚔️ خشونت",         ReportType.VIOLENCE),
    "3": ("📛 اسپم",          ReportType.SPAM),
    "4": ("👶 کودک‌آزاری",    ReportType.CHILD_ABUSE),
    "5": ("©️ نقض حق‌نشر",    ReportType.COPYRIGHT),
    "6": ("🎣 فیشینگ",        ReportType.FISHING),
    "7": ("📝 سایر",          ReportType.OTHER),
}

# کلیدواژه‌هایی که نشان می‌دهند هدف تعلیق/مسدود شده
SUSPEND_KEYWORDS = (
    "ban", "suspend", "deactiv", "block", "not found",
    "invalid peer", "peer_id_invalid", "user_banned", "chat_banned",
    "access denied", "forbidden",
)

# وضعیت زنده گزارش‌دهی (in-memory) — برای نمایش به ادمین
ACTIVE_REPORTS: dict[int, dict] = {}
# { tg_id: { "start": datetime, "target": guid, "count": total, "progress": n, "types": [labels] } }

user_stop: dict[int, bool] = {}


# ────────────────────────────────────────────────────────────────────────
#  FSM
# ────────────────────────────────────────────────────────────────────────
class Form(StatesGroup):
    phone             = State()
    password          = State()
    code              = State()
    main_menu         = State()
    report_guid       = State()
    report_resolve    = State()
    report_types      = State()
    report_other_text = State()
    report_count      = State()
    report_delay      = State()
    report_accounts   = State()
    receipt           = State()
    support_message   = State()
    admin_panel       = State()
    broadcast         = State()
    admin_reply       = State()


class AdminGrantFlow(StatesGroup):
    waiting_user     = State()
    waiting_quota    = State()
    waiting_duration = State()


class AdminUserFlow(StatesGroup):
    waiting_search = State()


# ────────────────────────────────────────────────────────────────────────
#  DB
# ────────────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id        INTEGER PRIMARY KEY,
                phone              TEXT,
                rubika_session     TEXT,
                rubika_auth        TEXT,
                rubika_private_key TEXT,
                is_premium         INTEGER DEFAULT 0,
                premium_until      TEXT,
                custom_quota       INTEGER,
                custom_expiry      TEXT,
                subscription_source TEXT DEFAULT 'free',
                coins              INTEGER DEFAULT 0,
                total_reports      INTEGER DEFAULT 0,
                total_invites      INTEGER DEFAULT 0,
                last_daily_reward  TEXT,
                is_reporting       INTEGER DEFAULT 0,
                created_at         TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER NOT NULL,
                target_guid     TEXT NOT NULL,
                target_type     TEXT DEFAULT 'unknown',
                target_username TEXT,
                report_type     TEXT,
                count           INTEGER DEFAULT 1,
                status          TEXT DEFAULT 'sent',
                is_suspended    INTEGER DEFAULT 0,
                reported_at     TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_tg ON reports(telegram_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_target ON reports(target_guid)")

        # migration: اضافه کردن ستون‌های جدید به جدول قدیمی
        for col, ctype in [
            ("rubika_auth", "TEXT"),
            ("rubika_private_key", "TEXT"),
            ("last_daily_reward", "TEXT"),
            ("custom_quota", "INTEGER"),
            ("custom_expiry", "TEXT"),
            ("subscription_source", "TEXT DEFAULT 'free'"),
            ("is_reporting", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ctype}")
            except sqlite3.OperationalError:
                pass
        conn.commit()


def get_user(telegram_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def upsert_user(telegram_id: int, **kwargs) -> None:
    existing = get_user(telegram_id)
    with sqlite3.connect(DB_PATH) as conn:
        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            conn.execute(
                f"UPDATE users SET {sets} WHERE telegram_id = ?",
                (*kwargs.values(), telegram_id),
            )
        else:
            kwargs["telegram_id"] = telegram_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({placeholders})",
                tuple(kwargs.values()),
            )
        conn.commit()


def add_stats(telegram_id: int, sent: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET total_reports = total_reports + ? WHERE telegram_id = ?",
            (sent, telegram_id),
        )
        conn.commit()


def get_all_users() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM users").fetchall()
    return [dict(r) for r in rows]


def count_users() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_all_valid_sessions() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT rubika_session FROM users WHERE rubika_session IS NOT NULL"
        ).fetchall()
    return [r["rubika_session"] for r in rows if session_exists(r["rubika_session"])]


def is_premium_active(user: dict) -> bool:
    if not user or not user.get("is_premium"):
        return False
    until = user.get("premium_until")
    if not until:
        return False
    try:
        return datetime.now() < datetime.fromisoformat(until)
    except (ValueError, TypeError):
        return False


def is_custom_active(user: dict) -> bool:
    if not user:
        return False
    expiry = user.get("custom_expiry")
    quota = user.get("custom_quota")
    if not expiry or not quota:
        return False
    try:
        return datetime.now() < datetime.fromisoformat(expiry)
    except (ValueError, TypeError):
        return False


def get_effective_limit(user: dict | None) -> int:
    """سقف تعداد گزارش در هر نوع؛ اولویت با اشتراک سفارشی."""
    if not user:
        return FREE_LIMIT
    if is_custom_active(user):
        try:
            return max(int(user["custom_quota"]), 1)
        except (ValueError, TypeError):
            pass
    if is_premium_active(user):
        return PREMIUM_LIMIT
    return FREE_LIMIT


def set_premium(telegram_id: int, months: int = 1, source: str = "purchased") -> None:
    until = (datetime.now() + timedelta(days=30 * months)).isoformat()
    upsert_user(
        telegram_id,
        is_premium=1,
        premium_until=until,
        subscription_source=source,
    )


def set_custom_subscription(telegram_id: int, quota: int, duration: timedelta, admin_id: int | None = None) -> None:
    until = (datetime.now() + duration).isoformat()
    upsert_user(
        telegram_id,
        custom_quota=int(quota),
        custom_expiry=until,
        subscription_source="admin_gift",
        is_premium=1,
    )


def clear_subscription(telegram_id: int) -> None:
    upsert_user(
        telegram_id,
        is_premium=0,
        premium_until=None,
        custom_quota=None,
        custom_expiry=None,
        subscription_source="free",
    )


# ─── گزارش‌ها ────────────────────────────────────────────────────────────
def log_report_run(
    telegram_id: int,
    target_guid: str,
    target_type: str,
    target_username: str | None,
    report_type_label: str,
    count: int,
    status: str = "sent",
    is_suspended: bool = False,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO reports
               (telegram_id, target_guid, target_type, target_username,
                report_type, count, status, is_suspended)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                telegram_id, target_guid, target_type,
                target_username, report_type_label, count, status,
                1 if is_suspended else 0,
            ),
        )
        conn.commit()


def mark_target_suspended(telegram_id: int, target_guid: str, suspended: bool) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE reports SET is_suspended = ? WHERE telegram_id = ? AND target_guid = ?",
            (1 if suspended else 0, telegram_id, target_guid),
        )
        conn.commit()


def get_user_unique_targets(tg_id: int) -> list[dict]:
    """لیست یکتای اهدافی که کاربر روی آن‌ها گزارش داده، همراه با آمار."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT target_guid,
                   MAX(target_username)  AS target_username,
                   MAX(target_type)      AS target_type,
                   SUM(count)            AS total_count,
                   MAX(reported_at)      AS last_report,
                   MAX(is_suspended)     AS suspended
            FROM reports
            WHERE telegram_id = ?
            GROUP BY target_guid
            ORDER BY last_report DESC
        """, (tg_id,)).fetchall()
    return [dict(r) for r in rows]


def get_all_reports_for_target(telegram_id: int, target_guid: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM reports
               WHERE telegram_id = ? AND target_guid = ?
               ORDER BY reported_at DESC""",
            (telegram_id, target_guid),
        ).fetchall()
    return [dict(r) for r in rows]


def count_user_targets(tg_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT COUNT(DISTINCT target_guid) FROM reports WHERE telegram_id = ?",
            (tg_id,),
        ).fetchone()[0]


def get_users_reporting_db() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT telegram_id FROM users WHERE is_reporting = 1").fetchall()
    return [r[0] for r in rows]


# ────────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────────
def _normalize_phone(phone: str) -> Optional[str]:
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    phone = phone.translate(trans)
    phone = re.sub(r"[^\d+]", "", phone)
    if phone.startswith("+98"):
        phone = "98" + phone[3:]
    elif phone.startswith("0098"):
        phone = "98" + phone[4:]
    elif phone.startswith("0"):
        phone = "98" + phone[1:]
    elif not phone.startswith("98") and len(phone) == 10:
        phone = "98" + phone
    if re.match(r"^98\d{10}$", phone):
        return phone
    return None


SESSION_EXTENSIONS = (".rp", ".rubika", ".session")


def session_exists(session_path: str | None) -> bool:
    if not session_path:
        return False
    return any(os.path.isfile(session_path + ext) for ext in SESSION_EXTENSIONS) or os.path.isfile(session_path)


GUID_PREFIXES = ("u0", "c0", "g0", "b0", "m0", "s0", "o0", "p0", "ch0", "t0", "e0")


def looks_like_guid(text: str) -> bool:
    text = text.strip()
    if any(text.startswith(p) for p in GUID_PREFIXES):
        return bool(re.match(r"^(u0|c0|g0|b0|m0|s0|o0|p0|ch0|t0|e0)[A-Za-z0-9_\-]{6,}$", text))
    return False


def detect_target_type(guid: str) -> str:
    if not guid:
        return "unknown"
    if guid.startswith("u0"):
        return "user"
    if guid.startswith("c0"):
        return "channel"
    if guid.startswith("g0"):
        return "group"
    if guid.startswith("b0"):
        return "bot"
    return "unknown"


TARGET_TYPE_FA = {
    "user": "کاربر",
    "channel": "چنل",
    "group": "گروه",
    "bot": "ربات",
    "unknown": "نامشخص",
}


def is_suspension_error(err_str: str) -> bool:
    err = (err_str or "").lower()
    return any(kw in err for kw in SUSPEND_KEYWORDS)


def parse_duration(s: str) -> timedelta | None:
    s = (s or "").strip().lower()
    m = re.match(r"^(\d+)([dhms])$", s)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    return {
        "d": timedelta(days=n),
        "h": timedelta(hours=n),
        "m": timedelta(minutes=n),
        "s": timedelta(seconds=n),
    }[m.group(2)]


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total < 60:
        return f"{total} ثانیه"
    if total < 3600:
        return f"{total // 60} دقیقه"
    if total < 86400:
        return f"{total // 3600} ساعت"
    return f"{total // 86400} روز"


def mask_phone(phone: str | None) -> str:
    """برای نمایش در پنل ادمین (بدون نقض حریم خصوصی)."""
    if not phone or len(phone) < 6:
        return "—"
    return phone[:4] + "*" * (len(phone) - 8) + phone[-4:]


def cleanup_session_files(session_path: str | None) -> None:
    if not session_path:
        return
    for f in [session_path] + [session_path + ext for ext in SESSION_EXTENSIONS]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────
#  Force Join
# ────────────────────────────────────────────────────────────────────────
async def check_user_membership(bot: Bot, user_id: int) -> bool:
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                return False
        except Exception as exc:
            logger.warning("check_member @%s user=%d: %s", channel, user_id, exc)
            return False
    return True


def kb_force_join() -> InlineKeyboardMarkup:
    buttons = []
    for ch in REQUIRED_CHANNELS:
        buttons.append([InlineKeyboardButton(text=f"📢 @{ch}", url=f"https://t.me/{ch}", style=ButtonStyle.PRIMARY)])
    buttons.append([InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="check_join", style=ButtonStyle.SUCCESS)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ────────────────────────────────────────────────────────────────────────
#  Rubika auth
# ────────────────────────────────────────────────────────────────────────
async def rubika_send_code(phone: str, pass_key: str = None) -> dict:
    tmp_path = os.path.join(SESSIONS_DIR, f"tmp_{phone}")
    client = RubikaClient(name=tmp_path)
    await client.connect()
    try:
        kwargs = {"phone_number": phone, "send_type": "SMS"}
        if pass_key is not None:
            kwargs["pass_key"] = pass_key
        result = await client.send_code(**kwargs)
        return {
            "status": result.status,
            "phone_code_hash": result.phone_code_hash,
            "hint_pass_key": getattr(result, "hint_pass_key", None),
        }
    except Exception as e:
        raise RuntimeError(f"خطا در ارسال کد: {e}")
    finally:
        await client.disconnect()
        cleanup_session_files(tmp_path)


async def rubika_sign_in(phone: str, code: str, phone_code_hash: str) -> dict:
    session_name = f"user_{phone}"
    session_path = os.path.join(SESSIONS_DIR, session_name)
    client = RubikaClient(name=session_path)
    await client.connect()
    try:
        public_key, private_key = Crypto.create_keys()
        result = await client.sign_in(
            phone_code=code,
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            public_key=public_key,
        )
        if result.status != "OK":
            raise RuntimeError(f"خطا در ورود: {result.status}")

        decrypted_auth = Crypto.decrypt_RSA_OAEP(private_key, result.auth)
        client.auth = decrypted_auth
        client.key = Crypto.passphrase(decrypted_auth)
        client.decode_auth = Crypto.decode_auth(decrypted_auth)
        client.import_key = pkcs1_15_sig.new(RSA.import_key(private_key.encode()))
        client.private_key = private_key
        client.guid = result.user.user_guid

        client.session.insert(
            auth=decrypted_auth,
            guid=result.user.user_guid,
            user_agent=client.user_agent,
            phone_number=phone,
            private_key=private_key,
        )
        await client.register_device(device_model=session_name)
        await client.disconnect()

        return {
            "session_path": session_path,
            "auth": decrypted_auth,
            "private_key": private_key,
        }
    except Exception as e:
        await client.disconnect()
        cleanup_session_files(session_path)
        raise RuntimeError(f"خطا در sign_in: {e}")


# ────────────────────────────────────────────────────────────────────────
#  Suspension check
# ────────────────────────────────────────────────────────────────────────
async def check_suspension_via_rubpy(session_path: str, target_guid: str) -> bool | None:
    """True اگر هدف تعلیق شده باشد، False اگر فعال، None اگر نامشخص."""
    if not session_path or not session_exists(session_path):
        return None
    try:
        client = RubikaClient(name=session_path)
        async with client:
            try:
                if target_guid.startswith(("u0", "b0")):
                    info = await client.get_user_info(target_guid)
                else:
                    info = await client.get_chat(target_guid)

                # بررسی فیلدهای رایج ban
                for attr in ("banned", "is_banned", "is_deleted", "deactivated",
                             "user_ban_changed_banned", "is_blocked"):
                    val = getattr(info, attr, None)
                    if val is True:
                        return True
                status = getattr(info, "status", None)
                if isinstance(status, str) and status.lower() in ("banned", "deleted", "blocked"):
                    return True
                return False
            except Exception as e:
                err = str(e).lower()
                if any(kw in err for kw in SUSPEND_KEYWORDS):
                    return True
                logger.warning("check_suspension: %s", e)
                return None
    except Exception as e:
        logger.error("check_suspension outer: %s", e)
        return None


async def _background_suspension_check(tg_id: int, target_guid: str, session_path: str | None):
    if not session_path:
        return
    suspended = await check_suspension_via_rubpy(session_path, target_guid)
    if suspended is not None:
        mark_target_suspended(tg_id, target_guid, suspended)


# ────────────────────────────────────────────────────────────────────────
#  Keyboards
# ────────────────────────────────────────────────────────────────────────
def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ارسال شماره تماس", request_contact=True)]],
        resize_keyboard=True,
    )


def kb_main(premium: bool) -> InlineKeyboardMarkup:
    plan_label = "⭐ اشتراک ویژه" if premium else "💎 خرید اشتراک"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚨 گزارش تخلف", callback_data="start_report", style=ButtonStyle.DANGER),
            InlineKeyboardButton(text="📊 وضعیت حساب", callback_data="account_status", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="📋 گزارش‌های من", callback_data="my_reports_0", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="🔄 بروزرسانی وضعیت اهداف", callback_data="refresh_statuses", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text=f"{plan_label}", callback_data="subscription", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="🎁 جایزه روزانه", callback_data="daily_reward", style=ButtonStyle.SUCCESS),
        ],
        [
            InlineKeyboardButton(text="🔗 لینک دعوت", callback_data="referral_link", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="❓ راهنما", callback_data="help", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="📞 ارتباط با ما / پشتیبانی", callback_data="support", style=ButtonStyle.DANGER),
        ],
    ])


def kb_menu_return() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_menu", style=ButtonStyle.PRIMARY)],
    ])


def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📢 پیام همگانی", callback_data="admin_broadcast", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="📊 آمار ربات", callback_data="admin_stats", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="👥 مدیریت کاربران", callback_data="admin_users_0", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="💎 اعطای اشتراک سفارشی", callback_data="admin_custom_sub", style=ButtonStyle.SUCCESS),
        ],
        [
            InlineKeyboardButton(text="🟢 در حال اجرا", callback_data="admin_live", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="📋 لاگ گزارش‌ها", callback_data="admin_logs_0", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="💬 پشتیبانی", callback_data="admin_support", style=ButtonStyle.DANGER),
            InlineKeyboardButton(text="🚪 خروج از پنل", callback_data="admin_exit", style=ButtonStyle.DANGER),
        ],
    ])


def kb_support() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_menu", style=ButtonStyle.PRIMARY)],
    ])


def kb_admin_reply(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 پاسخ به کاربر", callback_data=f"admin_reply_{user_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🚫 بستن", callback_data="admin_close_ticket", style=ButtonStyle.DANGER)],
    ])


def kb_report_types(selected: set) -> InlineKeyboardMarkup:
    buttons = []
    for k, (label, _) in REPORT_TYPES_MAP.items():
        mark = "✅" if k in selected else "⬜"
        style = ButtonStyle.SUCCESS if k in selected else ButtonStyle.PRIMARY
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"rt_{k}", style=style)])
    buttons.append([InlineKeyboardButton(text="▶️ شروع گزارش", callback_data="rt_confirm", style=ButtonStyle.SUCCESS)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_report_guid_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 دریافت شناسه با یوزرنیم/آیدی", callback_data="get_by_username", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="✍️ وارد کردن شناسه دستی", callback_data="enter_guid", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_menu", style=ButtonStyle.PRIMARY)],
    ])


def kb_user_reports(page: int, total_pages: int, owner_id: int | None = None) -> InlineKeyboardMarkup:
    """کیبورد ناوبری برای لیست گزارش‌ها. اگر owner_id داده شود، نمای ادمین است."""
    prefix = "aur" if owner_id is not None else "myreports"
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ صفحه قبل",
            callback_data=f"{prefix}_{page - 1}" + (f"_{owner_id}" if owner_id else ""),
            style=ButtonStyle.PRIMARY,
        ))
    nav.append(InlineKeyboardButton(text=f"📄 {page + 1}/{max(total_pages, 1)}", callback_data="noop", style=ButtonStyle.PRIMARY))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(
            text="صفحه بعد ➡️",
            callback_data=f"{prefix}_{page + 1}" + (f"_{owner_id}" if owner_id else ""),
            style=ButtonStyle.PRIMARY,
        ))
    rows = [nav]
    rows.append([InlineKeyboardButton(
        text="🔙 بازگشت",
        callback_data="back_menu" if owner_id is None else f"admin_user_{owner_id}",
        style=ButtonStyle.PRIMARY,
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_user_detail(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 گزارش‌های این کاربر", callback_data=f"aur_0_{owner_id}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="💎 اعطای اشتراک", callback_data=f"admin_gift_{owner_id}", style=ButtonStyle.SUCCESS),
        ],
        [
            InlineKeyboardButton(text="🗑 حذف اشتراک فعلی", callback_data=f"admin_clear_sub_{owner_id}", style=ButtonStyle.DANGER),
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_users_0", style=ButtonStyle.PRIMARY),
        ],
    ])


def kb_admin_users(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_users_{page - 1}", style=ButtonStyle.PRIMARY))
    nav.append(InlineKeyboardButton(text=f"📄 {page + 1}/{max(total_pages, 1)}", callback_data="noop", style=ButtonStyle.PRIMARY))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_users_{page + 1}", style=ButtonStyle.PRIMARY))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel", style=ButtonStyle.PRIMARY)],
    ])


def kb_admin_logs(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_logs_{page - 1}", style=ButtonStyle.PRIMARY))
    nav.append(InlineKeyboardButton(text=f"📄 {page + 1}/{max(total_pages, 1)}", callback_data="noop", style=ButtonStyle.PRIMARY))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_logs_{page + 1}", style=ButtonStyle.PRIMARY))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel", style=ButtonStyle.PRIMARY)],
    ])


def kb_admin_live() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="admin_live", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel", style=ButtonStyle.PRIMARY)],
    ])


def kb_cancel_admin_flow() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="admin_panel", style=ButtonStyle.DANGER)],
    ])


# ────────────────────────────────────────────────────────────────────────
#  Resolve GUID
# ────────────────────────────────────────────────────────────────────────
async def resolve_object_guid(session_path: str, text: str) -> Optional[str]:
    text = text.strip().lstrip("@").strip()
    if looks_like_guid(text):
        return text
    try:
        client = RubikaClient(name=session_path)
        async with client:
            result = await client.get_object_by_username(text)
            for attr in ("object_guid", "user_guid", "guid", "group_guid", "channel_guid"):
                val = getattr(result, attr, None)
                if val and looks_like_guid(val):
                    return val
            found = _search_guid_in(result)
            if found:
                return found
            return None
    except Exception as exc:
        logger.error("resolve_object_guid: %s", exc)
        return None


def _search_guid_in(obj, _seen: set | None = None) -> Optional[str]:
    if _seen is None:
        _seen = set()
    if obj is None or isinstance(obj, (int, float, bool)):
        return None
    if isinstance(obj, str):
        return obj if looks_like_guid(obj) else None
    try:
        if id(obj) in _seen:
            return None
        _seen.add(id(obj))
    except TypeError:
        pass
    if isinstance(obj, dict):
        for v in obj.values():
            r = _search_guid_in(v, _seen)
            if r:
                return r
    else:
        for attr in dir(obj):
            if attr.startswith("_"):
                continue
            try:
                v = getattr(obj, attr)
            except Exception:
                continue
            if callable(v):
                continue
            r = _search_guid_in(v, _seen)
            if r:
                return r
    return None


# ────────────────────────────────────────────────────────────────────────
#  Start
# ────────────────────────────────────────────────────────────────────────
async def cmd_start(message: Message, command: CommandStart, state: FSMContext) -> None:
    bot = message.bot
    tg_id = message.from_user.id
    await state.clear()

    args = (command.args or "").split()
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0][4:])
            if referrer_id != tg_id:
                await state.update_data(referrer_id=referrer_id)
        except (ValueError, IndexError):
            pass

    is_member = await check_user_membership(bot, tg_id)
    if not is_member:
        await message.answer(
            "⛔ برای استفاده از ربات ابتدا باید در کانال‌های زیر عضو شوید:\n\n"
            "بعد از عضویت، دکمه «بررسی عضویت» را بزنید:",
            reply_markup=kb_force_join(),
        )
        return

    user = get_user(tg_id)
    if user and user.get("rubika_session") and session_exists(user["rubika_session"]):
        premium = is_premium_active(user) or is_custom_active(user)
        await message.answer("👋 خوش برگشتی!", reply_markup=kb_main(premium))
        await state.set_state(Form.main_menu)
        return

    await message.answer(
        "📱 شماره روبیکا (یا دکمه ارسال تماس) را بفرست:\nمثال: 09123456789",
        reply_markup=kb_phone(),
    )
    await state.set_state(Form.phone)


async def receive_phone(message: Message, state: FSMContext) -> None:
    if message.contact:
        phone_raw = message.contact.phone_number
    else:
        phone_raw = (message.text or "").strip()

    normalized = _normalize_phone(phone_raw)
    if not normalized:
        await message.answer("❌ شماره نامعتبر.\nیک شماره معتبر ایرانی وارد کنید.", reply_markup=kb_phone())
        return

    await state.update_data(phone=normalized)
    await message.answer("⏳ ارسال کد تایید...", reply_markup=ReplyKeyboardRemove())

    try:
        result = await rubika_send_code(normalized)
        await state.update_data(phone_code_hash=result["phone_code_hash"])

        if result.get("status") == "SendPassKey":
            hint = result.get("hint_pass_key", "ندارد")
            await state.update_data(needs_password=True)
            await message.answer(
                "🔑 این حساب رمز دو مرحله‌ای دارد.\n"
                f"راهنمایی: {hint}\n\nلطفاً رمز عبور را وارد کنید:"
            )
            await state.set_state(Form.password)
            return

        await state.update_data(needs_password=False)
        await message.answer(
            "✅ کد تایید ارسال شد!\nکد ۶ رقمی را از پیامک روبیکا بخوانید و اینجا بفرستید:"
        )
        await state.set_state(Form.code)
    except Exception as exc:
        logger.error("send_code: %s", exc)
        await message.answer(f"❌ {exc}\nبا /start دوباره تلاش کن.")
        await state.clear()


async def receive_password(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    data = await state.get_data()
    phone = data.get("phone", "")
    if not password:
        await message.answer("❌ رمز نمی‌تواند خالی باشد. دوباره:")
        return
    await message.answer("⏳ بررسی رمز عبور...")
    try:
        result = await rubika_send_code(phone, pass_key=password)
        await state.update_data(phone_code_hash=result["phone_code_hash"])
        if result.get("status") == "SendPassKey":
            hint = result.get("hint_pass_key", "ندارد")
            await message.answer(f"❌ رمز اشتباه است. راهنمایی: {hint}\nدوباره:")
            return
        await message.answer("✅ رمز تایید شد!\nکد ۶ رقمی را از پیامک بفرستید:")
        await state.set_state(Form.code)
    except Exception as exc:
        logger.error("send_code passkey: %s", exc)
        await message.answer(f"❌ {exc}")


async def receive_code(message: Message, state: FSMContext) -> None:
    code_digits = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    phone = data.get("phone", "")
    phone_code_hash = data.get("phone_code_hash", "")
    tg_id = message.from_user.id

    if len(code_digits) < 4 or len(code_digits) > 8:
        await message.answer("❌ کد ۴ تا ۸ رقم باید باشد. دوباره:")
        return

    await message.answer("⏳ در حال ورود...")
    try:
        login_data = await rubika_sign_in(phone, code_digits, phone_code_hash)
        upsert_user(
            tg_id,
            phone=phone,
            rubika_session=login_data["session_path"],
            rubika_auth=login_data["auth"],
            rubika_private_key=login_data["private_key"],
        )

        referrer_id = data.get("referrer_id")
        if referrer_id:
            referrer = get_user(referrer_id)
            if referrer:
                upsert_user(referrer_id, total_invites=(referrer.get("total_invites", 0) + 1))
                now = datetime.now()
                if is_premium_active(referrer):
                    current_until = datetime.fromisoformat(referrer["premium_until"])
                    new_until = current_until + timedelta(hours=1)
                else:
                    new_until = now + timedelta(hours=1)
                upsert_user(referrer_id, is_premium=1, premium_until=new_until.isoformat(),
                            subscription_source="prize")
                try:
                    await message.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            f"🎁 یک نفر با لینک دعوت شما عضو شد!\n"
                            f"👑 اشتراک ۱ ساعته به شما اضافه شد.\n"
                            f"📊 مجموع دعوت‌ها: {referrer.get('total_invites', 0) + 1}"
                        ),
                    )
                except Exception:
                    pass
            await state.update_data(referrer_id=None)

        await asyncio.sleep(0.5)
        await message.answer("✅ ورود موفق!", reply_markup=kb_main(False))
        await state.set_state(Form.main_menu)
    except Exception as exc:
        logger.error("sign_in: %s", exc)
        await message.answer(f"❌ {exc}")


# ────────────────────────────────────────────────────────────────────────
#  Callbacks
# ────────────────────────────────────────────────────────────────────────
async def check_join_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    bot = callback.bot
    tg_id = callback.from_user.id
    is_member = await check_user_membership(bot, tg_id)
    if not is_member:
        await callback.message.answer(
            "❌ هنوز در کانال‌ها عضو نشدید!\n\n"
            "لطفاً ابتدا در هر دو کانال عضو شوید و سپس «بررسی عضویت» را بزنید:",
            reply_markup=kb_force_join(),
        )
        return
    user = get_user(tg_id)
    if user and user.get("rubika_session") and session_exists(user["rubika_session"]):
        premium = is_premium_active(user) or is_custom_active(user)
        await callback.message.answer("✅ عضویت تایید شد! 👋", reply_markup=kb_main(premium))
        await state.set_state(Form.main_menu)
        return
    await callback.message.answer("✅ عضویت تایید شد!\n\n📱 شماره روبیکا را بفرست:", reply_markup=kb_phone())
    await state.set_state(Form.phone)


async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    premium = (is_premium_active(user) or is_custom_active(user)) if user else False
    await callback.message.answer("🏠 منو:", reply_markup=kb_main(premium))
    await state.set_state(Form.main_menu)


# ─── گزارش‌های من (کاربر) ─────────────────────────────────────────────────
async def show_my_reports(callback: CallbackQuery, page: int = 0) -> None:
    tg_id = callback.from_user.id
    targets = get_user_unique_targets(tg_id)
    per_page = 5
    total_pages = max((len(targets) + per_page - 1) // per_page, 1)
    page = max(0, min(page, total_pages - 1))

    if not targets:
        text = "📋 هنوز هیچ گزارشی ثبت نکرده‌اید."
    else:
        chunk = targets[page * per_page:(page + 1) * per_page]
        lines = [f"📋 گزارش‌های شما (صفحه {page + 1}/{total_pages})\n"]
        for i, t in enumerate(chunk, 1):
            guid = t["target_guid"] or "—"
            uname = t["target_username"] or ""
            ttype = TARGET_TYPE_FA.get(t.get("target_type", "unknown"), "نامشخص")
            count = t.get("total_count", 0)
            last = (t.get("last_report") or "")[:16]
            suspended = t.get("suspended", 0)
            badge = "🚫 تعلیق شده" if suspended else "🟢 فعال"
            short_guid = guid if len(guid) <= 22 else guid[:20] + "…"
            header = f"{i}. {badge} | {ttype}"
            sub = f"   🆔 <code>{short_guid}</code>"
            if uname:
                sub += f" | @{uname}"
            sub += f"\n   📅 آخرین: {last} | 📊 {count} گزارش"
            lines.append(header + "\n" + sub)
        text = "\n".join(lines)

    markup = kb_user_reports(page, total_pages)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


async def refresh_statuses(callback: CallbackQuery) -> None:
    """بروزرسانی وضعیت تمام اهداف کاربر با rubpy."""
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    if not user or not session_exists(user.get("rubika_session")):
        await callback.message.answer("❌ سشن معتبر نیست.", reply_markup=kb_menu_return())
        return
    await callback.message.edit_text("⏳ در حال بررسی وضعیت اهداف...")
    targets = get_user_unique_targets(tg_id)
    for t in targets[:20]:  # محدودیت ۲۰ هدف برای جلوگیری از کندی
        suspended = await check_suspension_via_rubpy(user["rubika_session"], t["target_guid"])
        if suspended is not None:
            mark_target_suspended(tg_id, t["target_guid"], suspended)
    await show_my_reports(callback, page=0)


# ─── منوی اصلی ────────────────────────────────────────────────────────────
async def process_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data
    tg_id = callback.from_user.id
    bot = callback.bot
    user = get_user(tg_id)

    if data == "noop":
        await callback.answer()
        return

    # ─── گزارش‌های من ───
    if data.startswith("myreports_"):
        try:
            page = int(data.split("_")[1])
        except (ValueError, IndexError):
            page = 0
        await callback.answer()
        await show_my_reports(callback, page)
        return

    if data == "refresh_statuses":
        await callback.answer()
        await refresh_statuses(callback)
        return

    await callback.answer()

    if data == "back_menu":
        premium = (is_premium_active(user) or is_custom_active(user)) if user else False
        await callback.message.answer("🏠 منو:", reply_markup=kb_main(premium))
        await state.set_state(Form.main_menu)
        return

    if data == "start_report":
        if not user or not user.get("rubika_session"):
            await callback.message.answer("❌ ابتدا /start بزن و وارد حساب روبیکا شو.")
            await state.set_state(Form.main_menu)
            return
        if not session_exists(user["rubika_session"]):
            await callback.message.answer("❌ سشن منقضی شده. لطفاً /start بزن و دوباره لاگین کن.")
            upsert_user(tg_id, rubika_session=None, rubika_auth=None, rubika_private_key=None)
            await state.set_state(Form.main_menu)
            return
        await callback.message.answer(
            "🎯 چطور شناسه هدف را وارد کنیم؟\n\n"
            "• اگر یوزرنیم/آیدی (مثل @username) را داری، ربات خودش شناسه را پیدا می‌کند.\n"
            "• یا شناسه (object_guid) را دستی بفرست.",
            reply_markup=kb_report_guid_choice(),
        )
        await state.set_state(Form.report_guid)
        return

    if data == "get_by_username":
        await callback.message.answer(
            "👤 یوزرنیم یا آیدی روبیکا را بفرست:\nمثال: @username یا username",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.report_resolve)
        return

    if data == "enter_guid":
        await callback.message.answer(
            "🎯 شناسه (object_guid) کاربر روبیکا را بفرست:\n\n"
            "مثال: u0A1bC2dE3fG4hI5jK6lM7nO8pQ9rS0T",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.report_guid)
        return

    if data == "account_status":
        if not user:
            await callback.message.answer("ابتدا /start بزن.")
            return
        limit = get_effective_limit(user)
        plan_parts = []
        if is_custom_active(user):
            plan_parts.append(f"💎 سفارشی ({user.get('custom_quota')} گزارش/نوع تا {str(user.get('custom_expiry'))[:16]})")
        if is_premium_active(user):
            plan_parts.append(f"👑 پریمیوم تا {user.get('premium_until', '')[:16]}")
        if not plan_parts:
            plan_parts.append("🆓 رایگان")
        plan = " + ".join(plan_parts)
        tg_targets = count_user_targets(tg_id)
        await callback.message.answer(
            f"📊 وضعیت حساب\n\n"
            f"پلن: {plan}\n"
            f"محدودیت هر نوع: {limit} گزارش\n"
            f"📊 کل گزارش‌ها: {user.get('total_reports', 0)}\n"
            f"🎯 اهداف یکتا: {tg_targets}\n"
            f"👥 دعوت‌ها: {user.get('total_invites', 0)}\n"
            f"🔐 سشن: {'فعال' if session_exists(user.get('rubika_session')) else 'نامعتبر'}",
            reply_markup=kb_menu_return(),
        )
        return

    if data == "subscription":
        premium = (is_premium_active(user) or is_custom_active(user)) if user else False
        if premium:
            txt = "💎 اشتراک‌های فعال شما:\n\n"
            if is_custom_active(user):
                txt += f"• سفارشی: {user.get('custom_quota')} گزارش/نوع تا {str(user.get('custom_expiry'))[:16]}\n"
            if is_premium_active(user):
                txt += f"• پریمیوم: تا {user.get('premium_until', '')[:16]}\n"
            await callback.message.answer(txt, reply_markup=kb_menu_return())
        else:
            await callback.message.answer(
                f"💎 خرید اشتراک یک ماهه\n"
                f"📊 {PREMIUM_LIMIT} گزارش در هر نوع\n"
                f"💰 مبلغ: {PREMIUM_PRICE}\n\n"
                f"💳 شماره کارت برای واریز:\n{ADMIN_CARD_NUMBER}\n\n"
                f"📸 لطفاً بعد از پرداخت، عکس رسید (فیش واریز) را بفرستید تا برای ادمین ارسال شود.",
                reply_markup=kb_menu_return(),
            )
            await state.set_state(Form.receipt)
        return

    if data == "help":
        await callback.message.answer(
            "❓ راهنما\n\n"
            "1. شناسه کاربر روبیکا را بفرست\n"
            "2. نوع گزارش را انتخاب کن\n"
            "3. تعداد گزارش از هر نوع را بگو\n"
            "4. فاصله (ثانیه) بین هر گزارش را بگو\n"
            "5. تعداد اکانت‌های ربات برای گزارش را بگو\n"
            "6. گزارش‌ها با همه اکانت‌ها خودکار ارسال می‌شوند\n\n"
            f"🆓 رایگان: {FREE_LIMIT} گزارش\n"
            f"💎 پریمیوم: {PREMIUM_LIMIT} گزارش\n"
            "💎 اشتراک سفارشی: طبق تنظیم ادمین\n\n"
            "برای توقف /stop",
            reply_markup=kb_menu_return(),
        )
        return

    if data == "referral_link":
        if not user:
            await callback.message.answer("ابتدا /start بزن.")
            return
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{tg_id}"
        await callback.message.answer(
            f"🔗 لینک دعوت شما\n\n📱 لینک:\n{ref_link}\n\n"
            f"👥 تعداد دعوت‌ها: {user.get('total_invites', 0)}\n\n"
            f"🎁 به ازای هر نفری که با لینک شما عضو شود ۱ ساعت اشتراک رایگان دریافت می‌کنید!",
            reply_markup=kb_menu_return(),
        )
        return

    if data == "support":
        await callback.message.answer(
            "📞 ارتباط با ما / پشتیبانی\n\n"
            "پیام، عکس، فیلم یا فایل خود را بفرستید تا برای پشتیبانی ارسال شود.\n"
            "پشتیبانی در اسرع وقت پاسخ می‌دهد.",
            reply_markup=kb_support(),
        )
        await state.set_state(Form.support_message)
        return

    if data == "daily_reward":
        await handle_daily_reward(callback, user, tg_id)
        return

    if data.startswith("rt_"):
        await handle_report_type_callback(callback, state, data, user)
        return


async def handle_daily_reward(callback: CallbackQuery, user: dict | None, tg_id: int) -> None:
    if not user:
        await callback.message.answer("ابتدا /start بزن.")
        return
    last_reward = user.get("last_daily_reward")
    now = datetime.now()
    if last_reward:
        try:
            last_dt = datetime.fromisoformat(last_reward)
            diff = now - last_dt
            if diff < timedelta(hours=24):
                remaining = timedelta(hours=24) - diff
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                await callback.message.answer(
                    f"⏳ جایزه بعدی در {hours} ساعت و {minutes} دقیقه",
                    reply_markup=kb_menu_return(),
                )
                return
        except (ValueError, TypeError):
            pass

    dice_msg = await callback.message.answer("🎲")
    for dots in [".", "..", "..."]:
        await asyncio.sleep(0.5)
        await dice_msg.edit_text(f"🎲 {dots}")
    await asyncio.sleep(1)

    roll = random.randint(1, 6)
    if roll == 6:
        prize_days, prize_text, emoji = 3, "🏆 شماره ۶! اشتراک ۳ روزه برنده شدید!", "🏆"
    elif roll in (4, 5):
        prize_days, prize_text, emoji = 1, "🎉 اشتراک ۱ روزه برنده شدید!", "🎉"
    else:
        prize_days, prize_text, emoji = 0, f"😔 شماره {roll} آمد. برنده نشدید!", "😔"

    upsert_user(tg_id, last_daily_reward=now.isoformat())
    result_text = (
        f"{emoji} نتیجه تاس:\n\n"
        f"🎲 ━━━━━━━━━━━━━━━━ 🎲\n       [ {roll} ]\n🎲 ━━━━━━━━━━━━━━━━ 🎲\n\n"
        f"{prize_text}"
    )
    if prize_days > 0:
        if is_premium_active(user):
            new_until = datetime.fromisoformat(user["premium_until"]) + timedelta(days=prize_days)
        else:
            new_until = now + timedelta(days=prize_days)
        upsert_user(tg_id, is_premium=1, premium_until=new_until.isoformat(),
                    subscription_source="daily_prize")
        result_text += f"\n\n👑 اشتراک تا {new_until.isoformat()[:10]} فعال است."
        await dice_msg.edit_text(result_text, reply_markup=kb_main(True))
    else:
        result_text += "\n\n💡 فردا دوباره امتحان کنید!"
        await dice_msg.edit_text(result_text, reply_markup=kb_menu_return())


async def handle_report_type_callback(callback: CallbackQuery, state: FSMContext, data: str, user: dict | None) -> None:
    key = data[3:]
    if key == "confirm":
        state_data = await state.get_data()
        selected: set = state_data.get("selected_types", set())
        if not selected:
            await callback.answer("حداقل یک نوع انتخاب کن.", show_alert=True)
            return
        if "7" in selected:
            await callback.message.answer("متن گزارش «سایر» را بنویس:", reply_markup=kb_menu_return())
            await state.set_state(Form.report_other_text)
            return
        limit = get_effective_limit(user)
        labels = [REPORT_TYPES_MAP[k][0] for k in selected]
        await callback.message.answer(
            f"انتخاب: {', '.join(labels)}\nچند گزارش از هر نوع؟ (حداکثر {limit})",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.report_count)
        return

    state_data = await state.get_data()
    selected: set = state_data.get("selected_types", set())
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    await state.update_data(selected_types=selected)
    await callback.message.edit_reply_markup(reply_markup=kb_report_types(selected))


# ─── ورود شناسه ────────────────────────────────────────────────────────────
async def receive_guid(message: Message, state: FSMContext) -> None:
    guid = (message.text or "").strip()
    tg_id = message.from_user.id
    if looks_like_guid(guid):
        await state.update_data(object_guid=guid, selected_types=set())
        await message.answer("نوع گزارش را انتخاب کن:", reply_markup=kb_report_types(set()))
        await state.set_state(Form.report_types)
        return

    user = get_user(tg_id)
    if not user or not user.get("rubika_session") or not session_exists(user["rubika_session"]):
        await message.answer("❌ سشن معتبر نیست. /start بزن.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return

    await message.answer("⏳ در حال جستجوی شناسه...")
    resolved = await resolve_object_guid(user["rubika_session"], guid)
    if not resolved:
        await message.answer("❌ شناسه‌ای برای این یوزرنیم/آیدی پیدا نشد.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return
    await state.update_data(object_guid=resolved, selected_types=set())
    await message.answer(
        f"✅ شناسه پیدا شد:\n<code>{resolved}</code>\n\nنوع گزارش را انتخاب کن:",
        reply_markup=kb_report_types(set()),
        parse_mode="HTML",
    )
    await state.set_state(Form.report_types)


async def receive_resolve(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not user or not user.get("rubika_session"):
        await message.answer("❌ سشن معتبر نیست. /start بزن.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return
    session_path = user["rubika_session"]
    if not session_exists(session_path):
        await message.answer("❌ سشن منقضی شده. /start بزن.", reply_markup=kb_menu_return())
        upsert_user(tg_id, rubika_session=None, rubika_auth=None, rubika_private_key=None)
        await state.set_state(Form.main_menu)
        return
    await message.answer("⏳ در حال جستجوی شناسه...")
    guid = await resolve_object_guid(session_path, text)
    if not guid:
        await message.answer(
            "❌ شناسه‌ای برای این یوزرنیم/آیدی پیدا نشد.\n"
            "یا یوزرنیم را درست وارد کن، یا «وارد کردن شناسه دستی» را انتخاب کن.",
            reply_markup=kb_report_guid_choice(),
        )
        await state.set_state(Form.report_guid)
        return
    await state.update_data(object_guid=guid, selected_types=set())
    await message.answer(
        f"✅ شناسه پیدا شد:\n<code>{guid}</code>\n\nنوع گزارش را انتخاب کن:",
        reply_markup=kb_report_types(set()),
        parse_mode="HTML",
    )
    await state.set_state(Form.report_types)


async def receive_other_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not text or len(text) < 3:
        await message.answer("❌ حداقل ۳ کاراکتر بنویس.", reply_markup=kb_menu_return())
        return
    await state.update_data(other_report_text=text)
    state_data = await state.get_data()
    limit = get_effective_limit(user)
    selected: set = state_data.get("selected_types", set())
    labels = [REPORT_TYPES_MAP[k][0] for k in selected]
    await message.answer(
        f"انتخاب: {', '.join(labels)}\nچند گزارش از هر نوع؟ (حداکثر {limit})",
        reply_markup=kb_menu_return(),
    )
    await state.set_state(Form.report_count)


async def receive_count(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ عدد مثبت وارد کن.", reply_markup=kb_menu_return())
        return
    count = int(text)
    limit = get_effective_limit(user)
    if count > limit:
        msg = f"❌ محدودیت پلن: {limit} گزارش."
        if not (is_premium_active(user) or is_custom_active(user)):
            msg += f"\n💎 اشتراک: {ADMIN_USERNAME}"
        await message.answer(msg + "\nعدد کمتر:", reply_markup=kb_menu_return())
        return
    await state.update_data(count=count)
    await message.answer("⏱️ هر چند ثانیه یک گزارش ارسال شود؟\nمثال: ۳", reply_markup=kb_menu_return())
    await state.set_state(Form.report_delay)


async def receive_delay(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ عدد مثبت (ثانیه) وارد کن.", reply_markup=kb_menu_return())
        return
    delay = int(text)
    all_sessions = get_all_valid_sessions()
    total = len(all_sessions)
    if total == 0:
        await message.answer("❌ هیچ اکانتی در ربات ثبت نشده.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return
    await state.update_data(report_delay_delay=delay)
    await message.answer(
        f"👥 تعداد کل اکانت‌های ثبت‌شده در ربات: {total}\n"
        f"با چند اکانت (سشن) گزارش بزنم؟ (۱ تا {total})",
        reply_markup=kb_menu_return(),
    )
    await state.set_state(Form.report_accounts)


async def receive_accounts(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ عدد مثبت وارد کن.", reply_markup=kb_menu_return())
        return
    all_sessions = get_all_valid_sessions()
    total = len(all_sessions)
    if total == 0:
        await message.answer("❌ هیچ اکانتی ثبت نشده.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return
    num_accounts = int(text)
    if num_accounts > total:
        await message.answer(f"❌ فقط {total} اکانت موجود است. عدد کمتر:", reply_markup=kb_menu_return())
        return

    state_data = await state.get_data()
    object_guid = state_data.get("object_guid", "")
    count       = state_data.get("count", 0)
    delay       = state_data.get("report_delay_delay", REPORT_DELAY)
    selected    = state_data.get("selected_types", set())
    other_text  = state_data.get("other_report_text", "")

    if not selected:
        await message.answer("❌ حداقل یک نوع گزارش انتخاب کن.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return
    if not object_guid:
        await message.answer("❌ شناسه کاربر یافت نشد.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return

    session_paths = all_sessions[:num_accounts]
    selected_types = [
        (REPORT_TYPES_MAP[k][0], REPORT_TYPES_MAP[k][1], other_text if k == "7" else "")
        for k in selected
    ]
    await state.update_data(object_guid=None, selected_types=None, other_report_text=None,
                            count=None, report_delay_delay=None)
    await message.answer(
        f"🚀 ارسال {count} گزارش (هر {delay} ثانیه) با {num_accounts} اکانت...\n⛔ /stop برای توقف"
    )
    asyncio.create_task(
        _pipeline(
            tg_id=tg_id,
            session_paths=session_paths,
            object_guid=object_guid,
            selected_types=selected_types,
            count=count,
            delay=delay,
            status_message=message,
        )
    )
    await state.set_state(Form.main_menu)


async def cmd_stop(message: Message) -> None:
    user_stop[message.from_user.id] = True
    await message.answer("⛔ توقف")


# ─── رسید اشتراک ──────────────────────────────────────────────────────────
async def receive_receipt(message: Message, state: FSMContext) -> None:
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not message.photo:
        await message.answer("❌ لطفاً عکس رسید پرداخت را بفرستید.", reply_markup=kb_menu_return())
        return
    photo = message.photo[-1]
    approve_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تایید", callback_data=f"approve_sub_{tg_id}", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="❌ رد", callback_data=f"reject_sub_{tg_id}", style=ButtonStyle.DANGER),
        ]
    ])
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_photo(
                chat_id=admin_id,
                photo=photo.file_id,
                caption=(
                    f"💎 رسید پرداخت اشتراک\n\n"
                    f"🆔 تلگرام: {tg_id}\n"
                    f"💰 مبلغ: {PREMIUM_PRICE}\n"
                    f"⏰ تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
                reply_markup=approve_keyboard,
            )
        except Exception as exc:
            logger.error("Failed to send receipt to admin %d: %s", admin_id, exc)
    await message.answer(
        f"✅ رسید شما برای ادمین ارسال شد!\n\n💰 مبلغ: {PREMIUM_PRICE}\n\n"
        "⏳ منتظر تایید ادمین بمانید.",
        reply_markup=kb_menu_return(),
    )
    await state.set_state(Form.main_menu)


async def admin_approve_sub(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return
    await callback.answer()
    data = callback.data
    if data.startswith("approve_sub_"):
        target_id = int(data.split("_")[-1])
        set_premium(target_id, PREMIUM_MONTHS, source="purchased")
        await callback.message.edit_caption(caption=f"✅ اشتراک فعال شد!\n🆔 {target_id}", reply_markup=None)
        try:
            user = get_user(target_id)
            premium_until = user.get("premium_until", "")[:10] if user else ""
            await callback.bot.send_message(
                chat_id=target_id,
                text=f"🎉 اشتراک شما فعال شد!\n👑 اشتراک تا {premium_until}",
            )
        except Exception as exc:
            logger.error("Failed to notify user %d: %s", target_id, exc)
    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[-1])
        await callback.message.edit_caption(caption=f"❌ درخواست رد شد.\n🆔 {target_id}", reply_markup=None)
        try:
            await callback.bot.send_message(chat_id=target_id, text="❌ درخواست اشتراک شما رد شد.")
        except Exception as exc:
            logger.error("Failed to notify user %d: %s", target_id, exc)


# ────────────────────────────────────────────────────────────────────────
#  Pipeline + Live status
# ────────────────────────────────────────────────────────────────────────
async def _reporting_keepalive(bot: Bot, chat_id: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue


async def _pipeline(
    tg_id: int,
    session_paths: list[str],
    object_guid: str,
    selected_types: list[tuple[str, ReportType, str]],
    count: int,
    delay: int,
    status_message: Message,
) -> None:
    user_stop[tg_id] = False
    bot = status_message.bot
    chat_id = status_message.chat.id

    target_type = detect_target_type(object_guid)
    target_username = None  # در صورت نیاز می‌توان resolve کرد
    started_at = datetime.now()

    # وضعیت زنده
    ACTIVE_REPORTS[tg_id] = {
        "start": started_at,
        "target": object_guid,
        "count": count * len(selected_types),
        "progress": 0,
        "types": [t[0] for t in selected_types],
    }
    upsert_user(tg_id, is_reporting=1)

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(_reporting_keepalive(bot, chat_id, stop_event))

    async def reply(msg: str) -> None:
        try:
            await status_message.answer(msg)
        except Exception:
            pass

    if not session_paths:
        await reply("❌ هیچ سشن معتبری برای گزارش یافت نشد.")
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass
        upsert_user(tg_id, is_reporting=0)
        ACTIVE_REPORTS.pop(tg_id, None)
        return

    n = len(session_paths)
    total_sent = 0
    lines = []
    overall_suspended = False

    try:
        for label, rt_enum, other_text in selected_types:
            per = count // n
            rem = count % n
            tasks = []
            for idx, sp in enumerate(session_paths):
                c = per + (1 if idx < rem else 0)
                if c > 0:
                    tasks.append(asyncio.create_task(
                        _single_loop(sp, object_guid, rt_enum, other_text, c, delay, label, tg_id)
                    ))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            sent = 0
            failed = 0
            suspended = False
            for r in results:
                if isinstance(r, Exception):
                    lines.append(f"❌ خطا ({label}): {r}")
                else:
                    s, f, susp = r
                    sent += s
                    failed += f
                    if susp:
                        suspended = True
            if suspended:
                overall_suspended = True
            total_sent += sent
            lines.append(f"✅ {label}: {sent} ارسال | {failed} ناموفق")

            # لاگ این ران
            log_report_run(
                telegram_id=tg_id,
                target_guid=object_guid,
                target_type=target_type,
                target_username=target_username,
                report_type_label=label,
                count=count,
                status="sent" if sent > 0 else "failed",
                is_suspended=suspended,
            )

            # بروزرسانی زنده
            if tg_id in ACTIVE_REPORTS:
                ACTIVE_REPORTS[tg_id]["progress"] += count

        add_stats(tg_id, total_sent)

        if overall_suspended:
            mark_target_suspended(tg_id, object_guid, True)
            lines.append("\n🚫 هدف احتمالاً تعلیق/مسدود شده است.")

        user = get_user(tg_id)
        premium = (is_premium_active(user) or is_custom_active(user)) if user else False
        await reply("📊 گزارش:\n" + "\n".join(lines))

        try:
            await status_message.answer("🏠 منو:", reply_markup=kb_main(premium))
        except Exception:
            pass

    finally:
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass
        upsert_user(tg_id, is_reporting=0)
        ACTIVE_REPORTS.pop(tg_id, None)

        # بررسی تعلیق در پس‌زمینه (با اولین سشن)
        if session_paths:
            asyncio.create_task(
                _background_suspension_check(tg_id, object_guid, session_paths[0])
            )


async def _single_loop(
    session_path: str,
    object_guid: str,
    report_type_enum: ReportType,
    other_text: str,
    count: int,
    delay: int,
    label: str,
    tg_id: int,
) -> tuple[int, int, bool]:
    sent = 0
    failed = 0
    suspended = False
    try:
        client = RubikaClient(name=session_path)
        async with client:
            for i in range(1, count + 1):
                if user_stop.get(tg_id):
                    break
                try:
                    if report_type_enum == ReportType.OTHER:
                        await client.report_object(object_guid, report_type_enum, description=other_text)
                    else:
                        await client.report_object(object_guid, report_type_enum)
                    sent += 1
                except Exception as exc:
                    failed += 1
                    err = str(exc)
                    if is_suspension_error(err):
                        suspended = True
                    logger.error("[%s][%d/%d] %s", label, i, count, exc)
                if i < count:
                    await asyncio.sleep(delay)
    except Exception as exc:
        logger.error("session %s error: %s", session_path, exc)
    return sent, failed, suspended


# ────────────────────────────────────────────────────────────────────────
#  Admin — پنل و مدیریت
# ────────────────────────────────────────────────────────────────────────
async def cmd_grant(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    args = (message.text or "").split()[1:]
    if not args:
        await message.answer("/grant <telegram_id> [months]")
        return
    try:
        target_id = int(args[0])
        months = int(args[1]) if len(args) > 1 else 1
        set_premium(target_id, months, source="admin_gift")
        await message.answer(f"✅ اشتراک {months} ماهه برای {target_id}")
    except (ValueError, IndexError):
        await message.answer("خطا در آرگومان‌ها.")


async def cmd_stats(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        prem = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
        reps = conn.execute("SELECT SUM(total_reports) FROM users").fetchone()[0] or 0
    await message.answer(f"📊 آمار\n👥 {total}\n👑 {prem}\n📢 {reps}")


async def cmd_admin(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🛠 پنل مدیریت", reply_markup=kb_admin_panel())
    await state.set_state(Form.admin_panel)


async def cmd_reply(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("/reply <telegram_id> متن پاسخ")
        return
    target_id = int(parts[1])
    text = message.text[len(parts[0]) + len(parts[1]) + 2:].strip()
    try:
        await message.bot.send_message(chat_id=target_id, text=f"💬 پاسخ پشتیبانی:\n{text}")
        await message.answer("✅ پاسخ ارسال شد.")
    except Exception as exc:
        await message.answer(f"❌ خطا: {exc}")


def _admin_user_line(u: dict) -> str:
    uid = u.get("telegram_id")
    custom = is_custom_active(u)
    prem = is_premium_active(u)
    if custom:
        badge = "💎"
    elif prem:
        badge = "👑"
    else:
        badge = "🆓"
    live = " 🟢" if u.get("is_reporting") else ""
    return f"{badge} <code>{uid}</code>{live} | 📊 {u.get('total_reports', 0)}"


def _admin_user_detail_text(u: dict) -> str:
    uid = u.get("telegram_id")
    lines = [f"👤 کاربر <code>{uid}</code>\n"]
    if is_custom_active(u):
        lines.append(f"💎 اشتراک سفارشی: {u.get('custom_quota')} گزارش/نوع")
        lines.append(f"   تا {str(u.get('custom_expiry'))[:16]}")
    if is_premium_active(u):
        lines.append(f"👑 پریمیوم تا {str(u.get('premium_until'))[:16]}")
    if not is_custom_active(u) and not is_premium_active(u):
        lines.append("🆓 رایگان")
    lines.append(f"📌 منبع اشتراک: {u.get('subscription_source', 'free')}")
    lines.append(f"📊 کل گزارش‌ها: {u.get('total_reports', 0)}")
    lines.append(f"👥 دعوت‌ها: {u.get('total_invites', 0)}")
    lines.append(f"📅 ثبت: {str(u.get('created_at', '-'))[:16]}")
    if u.get("is_reporting"):
        lines.append("🟢 در حال گزارش‌دهی (همین حالا)")
    return "\n".join(lines)


async def _show_admin_users(callback: CallbackQuery, page: int = 0) -> None:
    users = get_all_users()
    per = 8
    total_pages = max((len(users) + per - 1) // per, 1)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * per:(page + 1) * per]
    text = f"👥 لیست کاربران (صفحه {page + 1}/{total_pages})\n\n"
    if not chunk:
        text += "هیچ کاربری ثبت نشده."
    else:
        text += "\n".join(_admin_user_line(u) for u in chunk)
    try:
        await callback.message.edit_text(text, reply_markup=kb_admin_users(page, total_pages), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb_admin_users(page, total_pages), parse_mode="HTML")


async def _show_admin_logs(callback: CallbackQuery, page: int = 0) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        per = 8
        total_pages = max((total + per - 1) // per, 1)
        page = max(0, min(page, total_pages - 1))
        rows = conn.execute(
            "SELECT * FROM reports ORDER BY reported_at DESC LIMIT ? OFFSET ?",
            (per, page * per),
        ).fetchall()

    lines = [f"📋 لاگ گزارش‌ها (صفحه {page + 1}/{total_pages})\n"]
    if not rows:
        lines.append("هیچ گزارشی ثبت نشده.")
    else:
        for r in rows:
            badge = "🚫" if r["is_suspended"] else "✅"
            g = (r["target_guid"] or "—")
            g_short = g if len(g) <= 20 else g[:18] + "…"
            lines.append(
                f"{badge} {r['report_type']} | ×{r['count']}\n"
                f"   🆔 <code>{g_short}</code>\n"
                f"   👤 <code>{r['telegram_id']}</code> | 📅 {str(r['reported_at'])[:16]}"
            )
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb_admin_logs(page, total_pages), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb_admin_logs(page, total_pages), parse_mode="HTML")


async def _show_admin_live(callback: CallbackQuery) -> None:
    if not ACTIVE_REPORTS:
        text = "🟢 هیچ گزارشی در حال اجرا نیست."
    else:
        lines = ["🟢 گزارش‌های در حال اجرا:\n"]
        for tg_id, info in ACTIVE_REPORTS.items():
            elapsed = int((datetime.now() - info["start"]).total_seconds())
            g = info["target"]
            g_short = g if len(g) <= 20 else g[:18] + "…"
            lines.append(
                f"👤 <code>{tg_id}</code>\n"
                f"   🎯 <code>{g_short}</code>\n"
                f"   📊 {info['progress']}/{info['count']} | ⏱ {elapsed}s"
            )
        text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb_admin_live(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb_admin_live(), parse_mode="HTML")


async def admin_panel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return

    data = callback.data

    if data == "noop":
        await callback.answer()
        return

    # ─── لیست کاربران ───
    if data.startswith("admin_users_"):
        await callback.answer()
        try:
            page = int(data.split("_")[-1])
        except ValueError:
            page = 0
        await _show_admin_users(callback, page)
        return

    # ─── لاگ گزارش‌ها ───
    if data.startswith("admin_logs_"):
        await callback.answer()
        try:
            page = int(data.split("_")[-1])
        except ValueError:
            page = 0
        await _show_admin_logs(callback, page)
        return

    # ─── وضعیت زنده ───
    if data == "admin_live":
        await callback.answer()
        await _show_admin_live(callback)
        return

    # ─── بازگشت به پنل ادمین ───
    if data == "admin_panel":
        await callback.answer()
        try:
            await callback.message.edit_text("🛠 پنل مدیریت", reply_markup=kb_admin_panel())
        except Exception:
            await callback.message.answer("🛠 پنل مدیریت", reply_markup=kb_admin_panel())
        await state.set_state(Form.admin_panel)
        return

    # ─── آمار ───
    if data == "admin_stats":
        await callback.answer()
        users = get_all_users()
        total = len(users)
        prem = sum(1 for u in users if is_premium_active(u))
        custom = sum(1 for u in users if is_custom_active(u))
        reps = sum(u.get("total_reports", 0) for u in users)
        with sqlite3.connect(DB_PATH) as conn:
            sess = conn.execute("SELECT COUNT(*) FROM users WHERE rubika_session IS NOT NULL").fetchone()[0]
            total_reports_logged = conn.execute("SELECT COALESCE(SUM(count),0) FROM reports").fetchone()[0]
            suspended_targets = conn.execute(
                "SELECT COUNT(DISTINCT telegram_id || ':' || target_guid) FROM reports WHERE is_suspended = 1"
            ).fetchone()[0]
        await callback.message.answer(
            f"📊 آمار ربات\n\n"
            f"👥 کل کاربران: {total}\n"
            f"👑 پریمیوم فعال: {prem}\n"
            f"💎 اشتراک سفارشی فعال: {custom}\n"
            f"🔐 سشن‌های ثبت‌شده: {sess}\n"
            f"📢 مجموع گزارشات: {reps}\n"
            f"📋 رکوردهای گزارش: {total_reports_logged}\n"
            f"🚫 اهداف تعلیق‌شده: {suspended_targets}",
            reply_markup=kb_admin_panel(),
        )
        return

    # ─── پیام همگانی ───
    if data == "admin_broadcast":
        await callback.answer()
        await callback.message.answer(
            "📢 پیام همگانی\nپیام، عکس یا فایل خود را بفرستید تا به تمام کاربران ارسال شود:",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.broadcast)
        return

    # ─── پشتیبانی ───
    if data == "admin_support":
        await callback.answer()
        await callback.message.answer(
            "💬 پشتیبانی\nوقتی کاربر پیام بفرستد، با دکمه «پاسخ» فوروارد می‌شود.",
            reply_markup=kb_admin_panel(),
        )
        return

    # ─── خروج ───
    if data == "admin_exit":
        await callback.answer()
        await callback.message.answer("🚪 خروج از پنل.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return

    # ─── شروع ویزارد اشتراک سفارشی ───
    if data == "admin_custom_sub":
        await callback.answer()
        await state.set_state(AdminGrantFlow.waiting_user)
        await callback.message.answer(
            "💎 اعطای اشتراک سفارشی\n\n"
            "مرحله ۱/۳: آیدی تلگرام کاربر را بفرستید:\n"
            "مثال: <code>123456789</code>",
            reply_markup=kb_cancel_admin_flow(),
            parse_mode="HTML",
        )
        return

    # ─── نمایش جزئیات کاربر ───
    if data.startswith("admin_user_"):
        await callback.answer()
        try:
            uid = int(data.split("_")[-1])
        except ValueError:
            return
        user = get_user(uid)
        if not user:
            await callback.message.answer("❌ کاربر یافت نشد.", reply_markup=kb_admin_panel())
            return
        await callback.message.answer(
            _admin_user_detail_text(user),
            reply_markup=kb_user_detail(uid),
            parse_mode="HTML",
        )
        return

    # ─── حذف اشتراک ───
    if data.startswith("admin_clear_sub_"):
        await callback.answer()
        try:
            uid = int(data.split("_")[-1])
        except ValueError:
            return
        clear_subscription(uid)
        await callback.message.answer(f"🗑 اشتراک کاربر <code>{uid}</code> حذف شد.", parse_mode="HTML")
        return

    # ─── اعطای اشتراک به کاربر خاص (از صفحه جزئیات) ───
    if data.startswith("admin_gift_"):
        await callback.answer()
        try:
            uid = int(data.split("_")[-1])
        except ValueError:
            return
        await state.update_data(grant_target=uid)
        await state.set_state(AdminGrantFlow.waiting_quota)
        await callback.message.answer(
            f"💎 اعطای اشتراک برای <code>{uid}</code>\n\n"
            f"مرحله ۲/۳: تعداد گزارش در هر نوع را وارد کنید:\n"
            f"مثال: <code>2000</code>",
            reply_markup=kb_cancel_admin_flow(),
            parse_mode="HTML",
        )
        return


# ─── ویزارد اعطای اشتراک سفارشی ────────────────────────────────────────────
async def grant_flow_user(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("❌ آیدی عددی معتبر بفرستید.")
        return
    uid = int(text)
    user = get_user(uid)
    if not user:
        await message.answer("❌ کاربر یافت نشد. آیدی دیگری بفرستید یا انصراف دهید.")
        return
    await state.update_data(grant_target=uid)
    await state.set_state(AdminGrantFlow.waiting_quota)
    await message.answer(
        f"✅ کاربر <code>{uid}</code> پیدا شد.\n\n"
        f"مرحله ۲/۳: تعداد گزارش در هر نوع را وارد کنید:\n"
        f"مثال: <code>2000</code>",
        reply_markup=kb_cancel_admin_flow(),
        parse_mode="HTML",
    )


async def grant_flow_quota(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ عدد مثبت وارد کنید.")
        return
    quota = int(text)
    await state.update_data(grant_quota=quota)
    await state.set_state(AdminGrantFlow.waiting_duration)
    await message.answer(
        f"✅ تعداد {quota} گزارش در هر نوع ثبت شد.\n\n"
        f"مرحله ۳/۳: مدت اعتبار را وارد کنید:\n"
        f"فرمت: عدد + واحد (d=روز، h=ساعت، m=دقیقه)\n"
        f"مثال: <code>7d</code> (یک هفته) یا <code>1h</code> (یک ساعت) یا <code>30d</code>",
        reply_markup=kb_cancel_admin_flow(),
        parse_mode="HTML",
    )


async def grant_flow_duration(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    td = parse_duration(text)
    if td is None:
        await message.answer(
            "❌ فرمت نامعتبر. مثال: <code>7d</code>, <code>1h</code>, <code>30m</code>",
            parse_mode="HTML",
        )
        return
    data = await state.get_data()
    uid = data.get("grant_target")
    quota = data.get("grant_quota")
    if not uid or not quota:
        await message.answer("❌ اطلاعات ناقص. دوباره از پنل شروع کنید.", reply_markup=kb_admin_panel())
        await state.set_state(Form.admin_panel)
        return

    set_custom_subscription(uid, quota, td)
    await state.update_data(grant_target=None, grant_quota=None)

    expiry = (datetime.now() + td).isoformat()
    await message.answer(
        f"✅ اشتراک سفارشی اعطا شد.\n\n"
        f"👤 کاربر: <code>{uid}</code>\n"
        f"📊 تعداد گزارش در هر نوع: {quota}\n"
        f"⏱ مدت: {format_duration(td)}\n"
        f"📅 اعتبار تا: {expiry[:16]}",
        reply_markup=kb_admin_panel(),
        parse_mode="HTML",
    )
    await state.set_state(Form.admin_panel)

    try:
        await message.bot.send_message(
            chat_id=uid,
            text=(
                f"🎁 اشتراک ویژه به شما اعطا شد!\n\n"
                f"📊 تعداد گزارش در هر نوع: {quota}\n"
                f"⏱ مدت اعتبار: {format_duration(td)}\n"
                f"📅 تا: {expiry[:16]}"
            ),
        )
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────
#  Support
# ────────────────────────────────────────────────────────────────────────
async def receive_support_message(message: Message, state: FSMContext) -> None:
    tg_id = message.from_user.id
    sent = False
    for admin_id in ADMIN_IDS:
        try:
            forwarded = await message.forward(chat_id=admin_id)
            await forwarded.reply(
                f"📨 پیام پشتیبانی جدید\n🆔 تلگرام: {tg_id}",
                reply_markup=kb_admin_reply(tg_id),
            )
            sent = True
        except Exception as exc:
            logger.error("forward support to admin %d: %s", admin_id, exc)
    if sent:
        await message.answer(
            "✅ پیام شما برای پشتیبانی ارسال شد.",
            reply_markup=kb_menu_return(),
        )
    else:
        await message.answer("❌ ارسال پیام به پشتیبانی ممکن نشد.")
    await state.set_state(Form.main_menu)


async def admin_reply_to_user(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return
    await callback.answer()
    target_id = int(callback.data.split("_")[-1])
    await state.update_data(reply_target=target_id)
    await state.set_state(Form.admin_reply)
    await callback.message.answer(
        f"💬 در حال پاسخ به کاربر {target_id}\nپیام، عکس یا فایل خود را بفرستید:",
        reply_markup=kb_menu_return(),
    )


async def admin_close_ticket(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text((callback.message.text or "") + "\n\n🚫 تیکت بسته شد.")
    except Exception:
        pass


async def receive_admin_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()

    if data.get("grant_mode"):
        await state.update_data(grant_mode=False)
        parts = (message.text or "").split()
        try:
            target_id = int(parts[0])
            months = int(parts[1]) if len(parts) > 1 else 1
            set_premium(target_id, months, source="admin_gift")
            await message.answer(f"✅ اشتراک {months} ماهه برای {target_id} فعال شد.", reply_markup=kb_admin_panel())
        except (ValueError, IndexError):
            await message.answer("❌ فرمت نادرست. مثال: 123456789 1", reply_markup=kb_admin_panel())
        await state.set_state(Form.admin_panel)
        return

    target_id = data.get("reply_target")
    if not target_id:
        return
    try:
        await message.forward(chat_id=target_id)
        await message.answer("✅ پاسخ ارسال شد.", reply_markup=kb_admin_panel())
    except Exception as exc:
        logger.error("admin reply to %d: %s", target_id, exc)
        await message.answer(f"❌ ناموفق: {exc}", reply_markup=kb_admin_panel())
    await state.set_state(Form.admin_panel)


async def receive_broadcast(message: Message, state: FSMContext) -> None:
    users = get_all_users()
    total = len(users)
    success = 0
    fail = 0
    await message.answer(f"📤 در حال ارسال پیام همگانی به {total} کاربر...")
    for u in users:
        uid = u.get("telegram_id")
        if not uid:
            continue
        try:
            await message.forward(chat_id=uid)
            success += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await message.answer(
        f"✅ پیام همگانی ارسال شد.\nموفق: {success}\nناموفق: {fail}",
        reply_markup=kb_admin_panel(),
    )
    await state.set_state(Form.admin_panel)


# ────────────────────────────────────────────────────────────────────────
#  Global error handler
# ────────────────────────────────────────────────────────────────────────
async def global_error_handler(update: Update, exception: Exception) -> bool:
    logger.exception("UNHANDLED EXCEPTION: %s", exception)
    try:
        if update.callback_query:
            await update.callback_query.answer(f"❌ خطا:\n{exception}", show_alert=True)
        elif update.message:
            await update.message.answer(f"❌ خطا:\n{exception}")
    except Exception:
        pass
    return True


# ────────────────────────────────────────────────────────────────────────
#  Main
# ────────────────────────────────────────────────────────────────────────
def build_app() -> tuple[Bot, Dispatcher]:
    init_db()

    # ریست وضعیت‌های گیرکرده در راه‌اندازی مجدد
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_reporting = 0")
        conn.commit()

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()
    router = Router()

    # ── پیام‌ها ──
    router.message.register(cmd_start, CommandStart())
    router.message.register(receive_phone, Form.phone, F.contact | (F.text & ~F.text.startswith("/")))
    router.message.register(receive_password, Form.password, F.text & ~F.text.startswith("/"))
    router.message.register(receive_code, Form.code, F.text & ~F.text.startswith("/"))
    router.message.register(receive_guid, Form.report_guid, F.text & ~F.text.startswith("/"))
    router.message.register(receive_resolve, Form.report_resolve, F.text & ~F.text.startswith("/"))
    router.message.register(receive_other_text, Form.report_other_text, F.text & ~F.text.startswith("/"))
    router.message.register(receive_count, Form.report_count, F.text & ~F.text.startswith("/"))
    router.message.register(receive_delay, Form.report_delay, F.text & ~F.text.startswith("/"))
    router.message.register(receive_accounts, Form.report_accounts, F.text & ~F.text.startswith("/"))
    router.message.register(receive_receipt, Form.receipt)
    router.message.register(receive_support_message, Form.support_message)
    router.message.register(receive_broadcast, Form.broadcast)
    router.message.register(receive_admin_reply, Form.admin_reply)

    # ویزارد اشتراک سفارشی
    router.message.register(grant_flow_user, AdminGrantFlow.waiting_user, F.text & ~F.text.startswith("/"))
    router.message.register(grant_flow_quota, AdminGrantFlow.waiting_quota, F.text & ~F.text.startswith("/"))
    router.message.register(grant_flow_duration, AdminGrantFlow.waiting_duration, F.text & ~F.text.startswith("/"))

    router.message.register(cmd_stop, Command("stop"))
    router.message.register(cmd_grant, Command("grant"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_admin, Command("admin", "panel"))
    router.message.register(cmd_reply, Command("reply"))

    # ── کالبک‌ها: خاص‌ترین‌ها اول ──
    router.callback_query.register(admin_approve_sub, F.data.startswith("approve_sub_") | F.data.startswith("reject_sub_"))
    router.callback_query.register(check_join_callback, F.data == "check_join")

    router.callback_query.register(admin_reply_to_user, F.data.startswith("admin_reply_"))
    router.callback_query.register(admin_close_ticket, F.data == "admin_close_ticket")

    # همه callback های ادمین (admin_*) به‌جز approve/reject که بالا ثبت شدند
    router.callback_query.register(admin_panel_callback, F.data.startswith("admin_"))

    # گزارش‌های من (کاربر) — سراسری
    router.callback_query.register(process_callback, F.data.startswith("myreports_"))
    router.callback_query.register(process_callback, F.data == "refresh_statuses")

    # منوی اصلی و جریان گزارش (state-bound)
    router.callback_query.register(process_callback, Form.main_menu)
    router.callback_query.register(process_callback, Form.report_types)
    router.callback_query.register(process_callback, Form.report_guid)

    router.callback_query.register(back_to_menu, Form.report_resolve, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.report_other_text, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.report_count, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.report_delay, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.report_accounts, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.receipt, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.report_guid, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.support_message, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.broadcast, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.admin_reply, F.data == "back_menu")

    # انصراف از ویزارد ادمین
    router.callback_query.register(back_to_menu, AdminGrantFlow.waiting_user, F.data == "admin_panel")
    router.callback_query.register(back_to_menu, AdminGrantFlow.waiting_quota, F.data == "admin_panel")
    router.callback_query.register(back_to_menu, AdminGrantFlow.waiting_duration, F.data == "admin_panel")

    # fallback
    router.callback_query.register(process_callback)

    dp.include_router(router)
    dp.errors.register(global_error_handler)
    return bot, dp


async def main() -> None:
    bot, dp = build_app()
    logger.warning("[POLLING] starting...")
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
