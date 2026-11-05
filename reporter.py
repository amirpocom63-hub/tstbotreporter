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
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8179859382:AAHnhHIa5-DXV923UfRdbEgOUnLO5P79qIs"
ADMIN_IDS: set[int] = {8503523539}
ADMIN_USERNAME = "@Saeqehe"

ADMIN_CARD_NUMBER = "6219861453153586"  # ← شماره کارت خودت را اینجا بگذار
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

user_stop: dict[int, bool] = {}


# ────────────────────────────────────────────────────────────────────────
#  FSM
# ────────────────────────────────────────────────────────────────────────
class Form(StatesGroup):
    phone            = State()
    password         = State()
    code             = State()
    main_menu        = State()
    report_guid      = State()
    report_resolve   = State()
    report_types     = State()
    report_other_text = State()
    report_count     = State()
    report_delay     = State()
    report_accounts  = State()
    receipt          = State()
    support_message  = State()
    admin_panel      = State()
    broadcast        = State()
    admin_reply      = State()


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
                coins              INTEGER DEFAULT 0,
                total_reports      INTEGER DEFAULT 0,
                total_invites      INTEGER DEFAULT 0,
                last_daily_reward  TEXT,
                created_at         TEXT DEFAULT (datetime('now'))
            )
        """)
        for col, ctype in [("rubika_auth", "TEXT"), ("rubika_private_key", "TEXT"), ("last_daily_reward", "TEXT")]:
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
    """تمام کاربران ثبت‌شده در ربات را برمی‌گرداند."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM users").fetchall()
    return [dict(r) for r in rows]

def count_users() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def get_all_valid_sessions() -> list[str]:
    """تمام سشن‌های معتبر (فایل موجود) اضافه‌شده به ربات را برمی‌گرداند."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT rubika_session FROM users WHERE rubika_session IS NOT NULL"
        ).fetchall()
    return [r["rubika_session"] for r in rows if session_exists(r["rubika_session"])]

def is_premium_active(user: dict) -> bool:
    if not user.get("is_premium"):
        return False
    until = user.get("premium_until")
    if not until:
        return False
    return datetime.now() < datetime.fromisoformat(until)

def set_premium(telegram_id: int, months: int = 1) -> None:
    until = (datetime.now() + timedelta(days=30 * months)).isoformat()
    upsert_user(telegram_id, is_premium=1, premium_until=until)


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


# پیشوندهای واقعی شناسه‌های روبیکا (کاربر، کانال، گروه، ربات و ...)
GUID_PREFIXES = ("u0", "c0", "g0", "b0", "m0", "s0", "o0", "p0", "ch0", "t0", "e0")


def looks_like_guid(text: str) -> bool:
    """فقط شناسه‌های واقعی روبیکا را تشخیص می‌دهد (نه یوزرنیم)."""
    text = text.strip()
    if any(text.startswith(p) for p in GUID_PREFIXES):
        return bool(re.match(r"^(u0|c0|g0|b0|m0|s0|o0|p0|ch0|t0|e0)[A-Za-z0-9_\-]{6,}$", text))
    return False

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
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.KICKED):
                return False
        except Exception as exc:
            logger.warning("check_member @%s user=%d: %s (بات ادمین کانال نیست؟)", channel, user_id, exc)
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
# جایگزین تابع rubika_send_code در کد خود کنید
async def rubika_send_code(phone: str, pass_key: str = None) -> dict:
    # استفاده از نام سشن به جای مسیر کامل فایل
    session_name = f"tmp_{phone}"
    client = RubikaClient(name=session_name)  # name باید نام سشن باشد
    
    try:
        await client.connect()
        
        # در صورتی که حساب تایید دو مرحله‌ای دارد، pass_key را ارسال کنید
        kwargs = {"phone_number": phone, "send_type": "SMS"}
        if pass_key is not None:
            kwargs["pass_key"] = pass_key
            
        result = await client.send_code(**kwargs)
        
        # اگر وضعیت SendPassKey بود، یعنی کد ارسال نشده و نیاز به رمز است
        if result.status == "SendPassKey":
            logger.warning("حساب نیاز به رمز دو مرحله‌ای دارد. hint: %s", getattr(result, "hint_pass_key", None))
            return {
                "status": result.status,
                "phone_code_hash": result.phone_code_hash,
                "hint_pass_key": getattr(result, "hint_pass_key", None),
            }
        
        return {
            "status": result.status,
            "phone_code_hash": result.phone_code_hash,
        }
    except Exception as e:
        logger.error("خطا در ارسال کد به %s: %s", phone, e, exc_info=True)  # لاگ کامل خطا
        raise RuntimeError(f"خطا در ارسال کد: {e}")
    finally:
        await client.disconnect()
        # پاک‌سازی فایل‌های موقت سشن
        cleanup_session_files(os.path.join(SESSIONS_DIR, session_name))

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
#  Keyboards with colors
# ────────────────────────────────────────────────────────────────────────
def colored_button(text: str, callback_data: str = None, url: str = None, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """ساخت دکمه شیشه‌ای رنگی"""
    if url:
        return InlineKeyboardButton(text=text, url=url, style=style)
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)

def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ارسال شماره تماس", request_contact=True)]],
        resize_keyboard=True,
    )

def kb_main(premium: bool) -> InlineKeyboardMarkup:
    plan_label = "⭐ ویژه" if premium else "🔹خرید اشتراک"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚨 گزارش تخلف", callback_data="start_report", style=ButtonStyle.DANGER),
            InlineKeyboardButton(text="📊 وضعیت حساب", callback_data="account_status", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text=f"💎 {plan_label}", callback_data="subscription", style=ButtonStyle.SUCCESS),
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
            InlineKeyboardButton(text="👑 تمدید اشتراک", callback_data="admin_grant", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="💬 پشتیبانی", callback_data="admin_support", style=ButtonStyle.DANGER),
        ],
        [
            InlineKeyboardButton(text="📨 لیست کاربران", callback_data="admin_list", style=ButtonStyle.PRIMARY),
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
        [
            InlineKeyboardButton(
                text="🔍 دریافت شناسه با یوزرنیم/آیدی",
                callback_data="get_by_username",
                style=ButtonStyle.DANGER,
            )
        ],
        [
            InlineKeyboardButton(
                text="✍️ وارد کردن شناسه دستی",
                callback_data="enter_guid",
                style=ButtonStyle.PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_menu", style=ButtonStyle.PRIMARY),
        ],
    ])


async def resolve_object_guid(session_path: str, text: str) -> Optional[str]:
    """متن را می‌گیرد: اگر خودش شناسه باشد برمی‌گرداند، وگرنه با یوزرنیم حل می‌کند."""
    text = text.strip().lstrip("@").strip()
    if looks_like_guid(text):
        return text
    try:
        client = RubikaClient(name=session_path)
        async with client:
            result = await client.get_object_by_username(text)
            # فقط مقداری که واقعاً شکل شناسه روبیکا دارد را برگردان
            # اولویت با object_guid (یوزرنیم نیست)، بعد user_guid
            for attr in ("object_guid", "user_guid", "guid", "group_guid", "channel_guid"):
                val = getattr(result, attr, None)
                if val and looks_like_guid(val):
                    return val
            # اگر result شیء تودرتو یا دیکشنری باشد، جستجوی عمقی انجام بده
            found = _search_guid_in(result)
            if found:
                return found
            logger.error("resolve_object_guid: هیچ شناسه معتبری در پاسخ پیدا نشد: %s", result)
            return None
    except Exception as exc:
        logger.error("resolve_object_guid: %s", exc)
        return None


def _search_guid_in(obj, _seen: set | None = None) -> Optional[str]:
    """جستجوی بازگشتی برای اولین مقدار شبیه شناسه در پاسخ rubpy."""
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
        premium = is_premium_active(user)
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
        phone_raw = message.text.strip()

    normalized = _normalize_phone(phone_raw)
    if not normalized:
        await message.answer(
            "❌ شماره نامعتبر.\nیک شماره معتبر ایرانی وارد کنید.",
            reply_markup=kb_phone(),
        )
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
                f"راهنمایی: {hint}\n\n"
                "لطفاً رمز عبور را وارد کنید:"
            )
            await state.set_state(Form.password)
            return

        await state.update_data(needs_password=False)
        await message.answer(
            "✅ کد تایید ارسال شد!\n"
            "کد ۶ رقمی را از پیامک روبیکا بخوانید و اینجا بفرستید:"
        )
        await state.set_state(Form.code)

    except Exception as exc:
        logger.error("send_code: %s", exc)
        await message.answer(f"❌ {exc}\nبا /start دوباره تلاش کن.")
        await state.clear()


async def receive_password(message: Message, state: FSMContext) -> None:
    password = message.text.strip() if message.text else ""
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
                upsert_user(referrer_id, is_premium=1, premium_until=new_until.isoformat())
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
        premium = is_premium_active(user)
        await callback.message.answer("✅ عضویت تایید شد! 👋", reply_markup=kb_main(premium))
        await state.set_state(Form.main_menu)
        return

    await callback.message.answer(
        "✅ عضویت تایید شد!\n\n📱 شماره روبیکا را بفرست:",
        reply_markup=kb_phone(),
    )
    await state.set_state(Form.phone)


async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tg_id = callback.from_user.id
    user  = get_user(tg_id)
    premium = is_premium_active(user) if user else False
    await callback.message.answer("🏠 منو:", reply_markup=kb_main(premium))
    await state.set_state(Form.main_menu)


async def process_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data  = callback.data
    tg_id = callback.from_user.id
    bot  = callback.bot
    user = get_user(tg_id)
    logger.warning("CALLBACK data=%s from=%s state=%s has_user=%s",
                   data, tg_id, await state.get_state(), bool(user))

    if data == "back_menu":
        premium = is_premium_active(user) if user else False
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
        premium = is_premium_active(user)
        limit   = PREMIUM_LIMIT if premium else FREE_LIMIT
        plan    = "👑 اشتراکی" if premium else "🆓 رایگان"
        await callback.message.answer(
            f"📊 وضعیت حساب\n\n"
            f"پلن: {plan}\n"
            f"محدودیت: {limit} از هر نوع\n"
            f"📊 گزارشات: {user.get('total_reports', 0)}\n"
            f"📱 شماره: {user.get('phone', '-')}",
            reply_markup=kb_menu_return(),
        )
        return

    if data == "subscription":
        premium = is_premium_active(user) if user else False
        if premium:
            await callback.message.answer(
                f"👑 اشتراک تا {user['premium_until'][:10]} فعال است.",
                reply_markup=kb_menu_return(),
            )
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
            f"💎 اشتراکی: {PREMIUM_LIMIT} گزارش\n\n"
            "برای توقف /stop",
            reply_markup=kb_menu_return(),
        )
        return

    if data == "referral_link":
        if not user:
            await callback.message.answer("ابتدا /start بزن.")
            return
        bot_info = await bot.get_me()
        bot_username = bot_info.username
        ref_link = f"https://t.me/{bot_username}?start=ref_{tg_id}"
        invites = user.get("total_invites", 0)
        await callback.message.answer(
            f"🔗 لینک دعوت شما\n\n"
            f"📱 لینک:\n{ref_link}\n\n"
            f"👥 تعداد دعوت‌ها: {invites}\n\n"
            f"🎁 به ازای هر نفری که با لینک شما عضو شود\n"
            f"   ۱ ساعت اشتراک رایگان دریافت می‌کنید!",
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
        await asyncio.sleep(0.5)
        await dice_msg.edit_text("🎲 .")
        await asyncio.sleep(0.5)
        await dice_msg.edit_text("🎲 ..")
        await asyncio.sleep(0.5)
        await dice_msg.edit_text("🎲 ...")
        await asyncio.sleep(1)

        roll = random.randint(1, 6)

        if roll == 6:
            prize_days = 3
            prize_text = "🏆 شماره ۶! اشتراک ۳ روزه پرو برنده شدید!"
            emoji = "🏆"
        elif roll in (4, 5):
            prize_days = 1
            prize_text = "🎉 شماره ۴ یا ۵! اشتراک ۱ روزه پرو برنده شدید!"
            emoji = "🎉"
        else:
            prize_days = 0
            prize_text = f"😔 شماره {roll} آمد. برنده نشدید!"
            emoji = "😔"

        upsert_user(tg_id, last_daily_reward=now.isoformat())

        result_text = (
            f"{emoji} نتیجه تاس:\n\n"
            f"🎲 ━━━━━━━━━━━━━━━━ 🎲\n"
            f"       [ {roll} ]\n"
            f"🎲 ━━━━━━━━━━━━━━━━ 🎲\n\n"
            f"{prize_text}"
        )

        if prize_days > 0:
            if is_premium_active(user):
                current_until = datetime.fromisoformat(user["premium_until"])
                new_until = current_until + timedelta(days=prize_days)
            else:
                new_until = now + timedelta(days=prize_days)
            until = new_until.isoformat()
            upsert_user(tg_id, is_premium=1, premium_until=until)

            user = get_user(tg_id)
            premium = is_premium_active(user) if user else False

            result_text += (
                f"\n\n👑 اشتراک تا {until[:10]} فعال است.\n"
                f"📊 محدودیت: {PREMIUM_LIMIT} گزارش در هر نوع"
            )
            await dice_msg.edit_text(result_text, reply_markup=kb_main(premium))
        else:
            result_text += "\n\n💡 فردا دوباره امتحان کنید!"
            await dice_msg.edit_text(result_text, reply_markup=kb_menu_return())
        return

    if data.startswith("rt_"):
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

            premium = is_premium_active(user) if user else False
            limit   = PREMIUM_LIMIT if premium else FREE_LIMIT
            labels  = [REPORT_TYPES_MAP[k][0] for k in selected]
            await callback.message.answer(
                f"انتخاب: {', '.join(labels)}\n"
                f"چند گزارش از هر نوع؟ (حداکثر {limit})",
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
        return


async def receive_guid(message: Message, state: FSMContext) -> None:
    guid = message.text.strip()
    tg_id = message.from_user.id

    if looks_like_guid(guid):
        await state.update_data(object_guid=guid, selected_types=set())
        await message.answer("نوع گزارش را انتخاب کن:", reply_markup=kb_report_types(set()))
        await state.set_state(Form.report_types)
        return

    # اگر کاربر یوزرنیم/آیدی داد، مثل مسیر «دریافت با یوزرنیم» حلش می‌کنیم
    user = get_user(tg_id)
    if not user or not user.get("rubika_session") or not session_exists(user["rubika_session"]):
        await message.answer("❌ سشن معتبر نیست. /start بزن.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return

    await message.answer("⏳ در حال جستجوی شناسه...")
    resolved = await resolve_object_guid(user["rubika_session"], guid)
    if not resolved:
        await message.answer(
            "❌ شناسه‌ای برای این یوزرنیم/آیدی پیدا نشد.",
            reply_markup=kb_menu_return(),
        )
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
    text = message.text.strip()
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
    text = message.text.strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)

    if not text or len(text) < 3:
        await message.answer("❌ حداقل ۳ کاراکتر بنویس.", reply_markup=kb_menu_return())
        return

    await state.update_data(other_report_text=text)
    state_data = await state.get_data()
    premium = is_premium_active(user) if user else False
    limit   = PREMIUM_LIMIT if premium else FREE_LIMIT
    selected: set = state_data.get("selected_types", set())
    labels = [REPORT_TYPES_MAP[k][0] for k in selected]

    await message.answer(
        f"انتخاب: {', '.join(labels)}\nچند گزارش از هر نوع؟ (حداکثر {limit})",
        reply_markup=kb_menu_return(),
    )
    await state.set_state(Form.report_count)


async def receive_count(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)

    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ عدد مثبت وارد کن.", reply_markup=kb_menu_return())
        return

    count = int(text)
    premium = is_premium_active(user) if user else False
    limit   = PREMIUM_LIMIT if premium else FREE_LIMIT

    if count > limit:
        msg = f"❌ محدودیت پلن: {limit} گزارش."
        if not premium:
            msg += f"\n💎 اشتراک: {ADMIN_USERNAME}"
        await message.answer(msg + "\nعدد کمتر:", reply_markup=kb_menu_return())
        return

    await state.update_data(count=count)
    await message.answer(
        "⏱️ هر چند ثانیه یک گزارش ارسال شود؟\nمثال: ۳",
        reply_markup=kb_menu_return(),
    )
    await state.set_state(Form.report_delay)
    return


async def receive_delay(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)

    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ عدد مثبت (ثانیه) وارد کن.", reply_markup=kb_menu_return())
        return

    delay = int(text)
    all_sessions = get_all_valid_sessions()
    total = len(all_sessions)

    if total == 0:
        await message.answer(
            "❌ هیچ اکانتی در ربات ثبت نشده. ابتدا حداقل یک حساب روبیکا اضافه کن.",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.main_menu)
        return

    await state.update_data(report_delay_delay=delay)
    await message.answer(
        f"👥 تعداد کل اکانت‌های ثبت‌شده در ربات: {total}\n"
        f"با چند اکانت (سشن) گزارش بزنم؟ (۱ تا {total})",
        reply_markup=kb_menu_return(),
    )
    await state.set_state(Form.report_accounts)
    return


async def receive_accounts(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    tg_id = message.from_user.id
    user = get_user(tg_id)

    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ عدد مثبت وارد کن.", reply_markup=kb_menu_return())
        return

    all_sessions = get_all_valid_sessions()
    total = len(all_sessions)
    if total == 0:
        await message.answer(
            "❌ هیچ اکانتی در ربات ثبت نشده. ابتدا حداقل یک حساب روبیکا اضافه کن.",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.main_menu)
        return

    num_accounts = int(text)
    if num_accounts > total:
        await message.answer(
            f"❌ فقط {total} اکانت در ربات موجود است. عدد کمتر:",
            reply_markup=kb_menu_return(),
        )
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
        await message.answer("❌ شناسه کاربر یافت نشد. /start بزن.", reply_markup=kb_menu_return())
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
        f"🚀 ارسال {count} گزارش (هر {delay} ثانیه) با {num_accounts} اکانت...\n"
        f"⛔ /stop برای توقف"
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
    return


async def cmd_stop(message: Message) -> None:
    user_stop[message.from_user.id] = True
    await message.answer("⛔ توقف")


# ────────────────────────────────────────────────────────────────────────
#  Subscription / Card Number
# ────────────────────────────────────────────────────────────────────────
async def receive_receipt(message: Message, state: FSMContext) -> None:
    tg_id = message.from_user.id
    user = get_user(tg_id)

    if not message.photo:
        await message.answer(
            "❌ لطفاً عکس رسید پرداخت (فیش واریز) را بفرستید.",
            reply_markup=kb_menu_return(),
        )
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
                    f"👤 کاربر: {user.get('phone', '-')}\n"
                    f"🆔 تلگرام: {tg_id}\n"
                    f"💰 مبلغ: {PREMIUM_PRICE}\n"
                    f"⏰ تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
                reply_markup=approve_keyboard,
            )
        except Exception as exc:
            logger.error("Failed to send receipt to admin %d: %s", admin_id, exc)

    await message.answer(
        "✅ رسید شما برای ادمین ارسال شد!\n\n"
        f"💰 مبلغ: {PREMIUM_PRICE}\n\n"
        "⏳ منتظر تایید ادمین بمانید؛ پس از تایید اشتراک یک ماهه فعال می‌شود.",
        reply_markup=kb_menu_return(),
    )
    await state.set_state(Form.main_menu)


async def admin_approve_sub(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return

    data = callback.data
    if data.startswith("approve_sub_"):
        target_id = int(data.split("_")[-1])
        set_premium(target_id, PREMIUM_MONTHS)
        await callback.message.edit_caption(caption=f"✅ اشتراک فعال شد!\n👤 کاربر: {target_id}", reply_markup=None)
        try:
            user = get_user(target_id)
            premium_until = user.get("premium_until", "")[:10] if user else ""
            await callback.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎉 اشتراک شما فعال شد!\n\n"
                    f"👑 اشتراک تا {premium_until} فعال است.\n"
                    f"📊 محدودیت: {PREMIUM_LIMIT} گزارش در هر نوع"
                ),
            )
        except Exception as exc:
            logger.error("Failed to notify user %d: %s", target_id, exc)

    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[-1])
        await callback.message.edit_caption(caption=f"❌ درخواست رد شد.\n👤 کاربر: {target_id}", reply_markup=None)
        try:
            await callback.bot.send_message(
                chat_id=target_id,
                text="❌ درخواست اشتراک شما توسط ادمین رد شد.\nبرای اطلاعات بیشتر با ادمین تماس بگیرید.",
            )
        except Exception as exc:
            logger.error("Failed to notify user %d: %s", target_id, exc)


# ────────────────────────────────────────────────────────────────────────
#  Pipeline
# ────────────────────────────────────────────────────────────────────────
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

    async def reply(msg: str) -> None:
        try:
            await status_message.answer(msg)
        except Exception:
            pass

    if not session_paths:
        await reply("❌ هیچ سشن معتبری برای گزارش یافت نشد.")
        return

    n = len(session_paths)

    total_sent = 0
    lines = []
    for label, rt_enum, other_text in selected_types:
        # توزیع تعداد گزارش‌ها بین سشن‌ها
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
        for r in results:
            if isinstance(r, Exception):
                lines.append(f"❌ خطا ({label}): {r}")
            else:
                s, f = r
                sent += s
                failed += f
        total_sent += sent
        lines.append(f"✅ {label}: {sent} ارسال | {failed} ناموفق")

    add_stats(tg_id, total_sent)
    user    = get_user(tg_id)
    premium = is_premium_active(user) if user else False

    await reply("📊 گزارش:\n" + "\n".join(lines))

    try:
        await status_message.answer("🔵 منو:", reply_markup=kb_main(premium))
    except Exception:
        pass


async def _single_loop(
    session_path: str,
    object_guid: str,
    report_type_enum: ReportType,
    other_text: str,
    count: int,
    delay: int,
    label: str,
    tg_id: int,
) -> tuple[int, int]:
    sent   = 0
    failed = 0
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
                    logger.error("[%s][%d/%d] %s", label, i, count, exc)
                if i < count:
                    await asyncio.sleep(delay)
    except Exception as exc:
        logger.error("session %s error: %s", session_path, exc)
    return sent, failed


# ────────────────────────────────────────────────────────────────────────
#  Admin
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
        months    = int(args[1]) if len(args) > 1 else 1
        set_premium(target_id, months)
        await message.answer(f"✅ اشتراک {months} ماهه برای {target_id}")
    except (ValueError, IndexError):
        await message.answer("خطا در آرگومان‌ها.")


async def cmd_stats(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        prem  = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
        reps  = conn.execute("SELECT SUM(total_reports) FROM users").fetchone()[0] or 0
    await message.answer(f"📊 آمار\n👥 {total}\n👑 {prem}\n📢 {reps}")


# ────────────────────────────────────────────────────────────────────────
#  Support (ارتباط با ما / پشتیبانی)
# ────────────────────────────────────────────────────────────────────────
async def receive_support_message(message: Message, state: FSMContext) -> None:
    tg_id = message.from_user.id
    user  = get_user(tg_id)

    sent = False
    for admin_id in ADMIN_IDS:
        try:
            forwarded = await message.forward(chat_id=admin_id)
            await forwarded.reply(
                f"📨 پیام پشتیبانی جدید\n"
                f"👤 کاربر: {user.get('phone', '-') if user else '-'}\n"
                f"🆔 تلگرام: {tg_id}",
                reply_markup=kb_admin_reply(tg_id),
            )
            sent = True
        except Exception as exc:
            logger.error("forward support to admin %d: %s", admin_id, exc)

    if sent:
        await message.answer(
            "✅ پیام شما برای پشتیبانی ارسال شد.\n"
            "منتظر پاسخ بمانید؛ پاسخ از همینجا برایتان ارسال می‌شود.",
            reply_markup=kb_menu_return(),
        )
    else:
        await message.answer("❌ ارسال پیام به پشتیبانی ممکن نشد. بعداً تلاش کنید.")
    await state.set_state(Form.main_menu)


async def admin_reply_to_user(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return
    target_id = int(callback.data.split("_")[-1])
    await state.update_data(reply_target=target_id)
    await state.set_state(Form.admin_reply)
    await callback.message.answer(
        f"💬 در حال پاسخ به کاربر {target_id}\n"
        "پیام، عکس یا فایل خود را بفرستید تا برای کاربر ارسال شود:",
        reply_markup=kb_menu_return(),
    )


async def admin_close_ticket(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        closed_text = (callback.message.text or "") + "\n\n🚫 تیکت بسته شد."
        await callback.message.edit_text(closed_text)
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
            set_premium(target_id, months)
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
        await message.answer("✅ پاسخ برای کاربر ارسال شد.", reply_markup=kb_admin_panel())
    except Exception as exc:
        logger.error("admin reply to %d: %s", target_id, exc)
        await message.answer(f"❌ ارسال پاسخ ناموفق: {exc}", reply_markup=kb_admin_panel())
    await state.set_state(Form.admin_panel)


# ────────────────────────────────────────────────────────────────────────
#  Admin panel
# ────────────────────────────────────────────────────────────────────────
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🛠 پنل مدیریت", reply_markup=kb_admin_panel())
    await state.set_state(Form.admin_panel)


async def admin_panel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ فقط ادمین", show_alert=True)
        return
    data = callback.data
    bot  = callback.bot

    if data == "admin_exit":
        await callback.message.answer("🚪 خروج از پنل.", reply_markup=kb_menu_return())
        await state.set_state(Form.main_menu)
        return

    if data == "admin_broadcast":
        await callback.message.answer(
            "📢 پیام همگانی\n"
            "پیام، عکس یا فایل خود را بفرستید تا به تمام کاربران ربات ارسال شود:",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.broadcast)
        return

    if data == "admin_stats":
        total = count_users()
        users = get_all_users()
        prem  = sum(1 for u in users if is_premium_active(u))
        reps  = sum(u.get("total_reports", 0) for u in users)
        with sqlite3.connect(DB_PATH) as conn:
            sess = conn.execute(
                "SELECT COUNT(*) FROM users WHERE rubika_session IS NOT NULL"
            ).fetchone()[0]
        await callback.message.answer(
            f"📊 آمار ربات\n\n"
            f"👥 کل کاربران: {total}\n"
            f"👑 اشتراکی‌ها: {prem}\n"
            f"🔐 سشن‌های فعال: {sess}\n"
            f"📢 کل گزارشات: {reps}",
            reply_markup=kb_admin_panel(),
        )
        return

    if data == "admin_grant":
        await callback.message.answer(
            "👑 تمدید اشتراک\n"
            "فرمت: <telegram_id> <months>\nمثال: 123456789 1",
            reply_markup=kb_menu_return(),
        )
        await state.set_state(Form.admin_reply)
        await state.update_data(grant_mode=True)
        return

    if data == "admin_support":
        await callback.message.answer(
            "💬 پشتیبانی\n"
            "وقتی کاربری پیام پشتیبانی بفرستد، پیام همراه با دکمه «پاسخ» برایتان فوروارد می‌شود.\n"
            "روی «پاسخ به کاربر» بزنید و پاسخ را بفرستید.",
            reply_markup=kb_admin_panel(),
        )
        return

    if data == "admin_list":
        users = get_all_users()
        if not users:
            await callback.message.answer("هیچ کاربری ثبت نشده.", reply_markup=kb_admin_panel())
            return
        lines = []
        for u in users[-30:]:
            pid = u.get("telegram_id")
            phone = u.get("phone", "-")
            prem = "👑" if is_premium_active(u) else "🆓"
            lines.append(f"{prem} {pid} | {phone}")
        text = "📨 لیست کاربران (آخرین ۳۰ نفر):\n" + "\n".join(lines)
        await callback.message.answer(text, reply_markup=kb_admin_panel())
        return


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


# ────────────────────────────────────────────────────────────────────────
#  Global error handler (برای دیدن خطای کالبک‌ها)
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
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()
    router = Router()

   # ── پیام‌ها ──
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
    router.message.register(cmd_stop, Command("stop"))
    router.message.register(cmd_grant, Command("grant"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_admin, Command("admin", "panel"))
    router.message.register(cmd_reply, Command("reply"))

    # ── کالبک‌ها ──
    # هندلرهای خاص را اول ثبت کن (اولویت بالاتر) تا توسط process_callback سایه نشوند
    router.callback_query.register(admin_approve_sub, F.data.startswith("approve_sub_") | F.data.startswith("reject_sub_"))
    router.callback_query.register(check_join_callback, F.data == "check_join")

    # ── ادمین (باید قبل از process_callback ثبت شوند تا توسط فیلتر استیت کور نشوند) ──
    router.callback_query.register(admin_reply_to_user, F.data.startswith("admin_reply_"))
    router.callback_query.register(admin_close_ticket, F.data == "admin_close_ticket")
    router.callback_query.register(admin_panel_callback, F.data.startswith("admin_"))

    # منوی اصلی و بخش‌های گزارش (محدود به استیت)
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
    router.callback_query.register(back_to_menu, Form.admin_panel, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.broadcast, F.data == "back_menu")
    router.callback_query.register(back_to_menu, Form.admin_reply, F.data == "back_menu")

    # fallback: اگر استیت از دست رفته باشد (مثلاً بعد از ری‌استارت بات)
    # باز هم دکمه‌ها جواب بدهند
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
