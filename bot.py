import os
import sqlite3
import logging
import random
import string
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip()

DB_FILE = "nektome.db"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("NektomeTJ")

# ============================================================
# DATABASE
# ============================================================

conn = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)
conn.row_factory = sqlite3.Row

conn.executescript("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id TEXT PRIMARY KEY,
    nektome_id TEXT UNIQUE NOT NULL,
    age INTEGER NOT NULL,
    gender TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    blocker TEXT NOT NULL,
    blocked TEXT NOT NULL,
    PRIMARY KEY (blocker, blocked)
);

CREATE TABLE IF NOT EXISTS ratings (
    from_user TEXT NOT NULL,
    to_user TEXT NOT NULL,
    rating TEXT NOT NULL,
    PRIMARY KEY (from_user, to_user)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter TEXT NOT NULL,
    reported TEXT NOT NULL,
    reporter_nektome_id TEXT NOT NULL,
    reported_nektome_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_users_nektome
ON users(nektome_id);

CREATE INDEX IF NOT EXISTS idx_blocks_blocker
ON blocks(blocker);

CREATE INDEX IF NOT EXISTS idx_blocks_blocked
ON blocks(blocked);
""")

conn.commit()

# ============================================================
# IN-MEMORY CHAT STATE
# ============================================================

# user_id -> partner_id
active_chats = {}

# user_id -> matching preferences
waiting_users = {}

# pair_key -> temporary chat transcript
chat_logs = {}

# user_id -> previous partner after chat
last_partners = {}

# ============================================================
# CONSTANTS
# ============================================================

GENDER_MALE = "M"
GENDER_FEMALE = "F"
GENDER_ALL = "ALL"

AGE_ALL = "ALL"
AGE_MINOR = "MINOR"
AGE_17_20 = "17_20"
AGE_21_25 = "21_25"
AGE_ADULT = "ADULT"

# ============================================================
# DATABASE HELPERS
# ============================================================

def db_one(query, params=()):
    return conn.execute(query, params).fetchone()


def db_all(query, params=()):
    return conn.execute(query, params).fetchall()


def db_exec(query, params=()):
    with conn:
        return conn.execute(query, params)


def get_user(tg_id):
    return db_one(
        "SELECT * FROM users WHERE telegram_id=?",
        (str(tg_id),)
    )


def get_user_by_nektome_id(nektome_id):
    return db_one(
        "SELECT * FROM users WHERE nektome_id=?",
        (str(nektome_id),)
    )


def is_blocked(user1, user2):
    row = db_one(
        """
        SELECT 1 FROM blocks
        WHERE (blocker=? AND blocked=?)
           OR (blocker=? AND blocked=?)
        LIMIT 1
        """,
        (
            str(user1),
            str(user2),
            str(user2),
            str(user1),
        )
    )
    return row is not None


def is_user_blocked(tg_id):
    row = db_one(
        "SELECT 1 FROM blocks WHERE blocked=? LIMIT 1",
        (str(tg_id),)
    )
    return row is not None


def generate_nektome_id():
    while True:
        number = str(random.randint(100000, 999999))

        if not get_user_by_nektome_id(number):
            return number


def gender_text(gender):
    if gender == GENDER_MALE:
        return "Мард"
    if gender == GENDER_FEMALE:
        return "Зан"
    if gender == GENDER_ALL:
        return "Ҳама"
    return "Номаълум"


def age_filter_text(value):
    if value == AGE_MINOR:
        return "13–16"
    if value == AGE_17_20:
        return "17–20"
    if value == AGE_21_25:
        return "21–25"
    if value == AGE_ADULT:
        return "26+"
    if value == AGE_ALL:
        return "Ҳама"
    return "Номаълум"


def age_matches(age, age_filter):
    if age_filter == AGE_MINOR:
        return 13 <= age <= 16

    if age_filter == AGE_17_20:
        return 17 <= age <= 20

    if age_filter == AGE_21_25:
        return 21 <= age <= 25

    if age_filter == AGE_ADULT:
        return age >= 26

    if age_filter == AGE_ALL:
        return True

    return False


def gender_matches(gender, wanted):
    return wanted == GENDER_ALL or gender == wanted


def pair_key(a, b):
    return ":".join(sorted([str(a), str(b)]))


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["👤 Профили ман", "👥 Ёфтани ҳамсӯҳбат"],
            ["🛠 Техподдержка"],
        ],
        resize_keyboard=True
    )


def chat_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["❌ Баромадан аз чат"],
        ],
        resize_keyboard=True
    )


def search_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["❌ Қатъ кардани ҷустуҷӯ"],
        ],
        resize_keyboard=True
    )


def search_inline_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Қатъ кардани ҷустуҷӯ",
                callback_data="search:cancel"
            )
        ]
    ])


def gender_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨 Мард",
                callback_data="gender:M"
            ),
            InlineKeyboardButton(
                "👩 Зан",
                callback_data="gender:F"
            ),
        ]
    ])


def find_gender_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨 Мард",
                callback_data="findgender:M"
            ),
            InlineKeyboardButton(
                "👩 Зан",
                callback_data="findgender:F"
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 Ҳама",
                callback_data="findgender:ALL"
            )
        ],
    ])


def find_age_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "13–16",
                callback_data="findage:MINOR"
            ),
            InlineKeyboardButton(
                "17–20",
                callback_data="findage:17_20"
            ),
        ],
        [
            InlineKeyboardButton(
                "21–25",
                callback_data="findage:21_25"
            ),
            InlineKeyboardButton(
                "26+",
                callback_data="findage:ADULT"
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 Ҳама",
                callback_data="findage:ALL"
            )
        ],
    ])


def age_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "13–16",
                callback_data="age:MINOR"
            ),
            InlineKeyboardButton(
                "17–20",
                callback_data="age:17_20"
            ),
        ],
        [
            InlineKeyboardButton(
                "21–25",
                callback_data="age:21_25"
            ),
            InlineKeyboardButton(
                "26+",
                callback_data="age:26_PLUS"
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 Ҳама",
                callback_data="age:ALL"
            )
        ],
    ])


def rating_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤩",
                callback_data="rate:super"
            ),
            InlineKeyboardButton(
                "👍",
                callback_data="rate:good"
            ),
            InlineKeyboardButton(
                "👎",
                callback_data="rate:bad"
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 Репорт",
                callback_data="report"
            )
        ]
    ])


def support_keyboard():
    if SUPPORT_USERNAME:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🛠 Техподдержка",
                    url="https://t.me/" + SUPPORT_USERNAME.lstrip("@")
                )
            ]
        ])

    return None


# ============================================================
# RATING
# ============================================================

def get_rating_counts(user_id):
    rows = db_all(
        """
        SELECT rating, COUNT(*) AS count
        FROM ratings
        WHERE to_user=?
        GROUP BY rating
        """,
        (str(user_id),)
    )

    result = {
        "super": 0,
        "good": 0,
        "bad": 0,
    }

    for row in rows:
        if row["rating"] in result:
            result[row["rating"]] = row["count"]

    return result


def rating_text(user_id):
    r = get_rating_counts(user_id)

    return (
        f"🤩 {r['super']}   "
        f"👍 {r['good']}   "
        f"👎 {r['bad']}"
    )


# ============================================================
# PROFILE
# ============================================================

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = get_user(update.effective_user.id)

    if not user:
        await update.message.reply_text(
            "❗ Аввал /start кунед."
        )
        return

    text = (
        "👤 <b>Профили шумо</b>\n\n"
        f"🆔 ID: <code>{user['nektome_id']}</code>\n"
        f"🎂 Синну сол: {user['age']}\n"
        f"🚻 Ҷинс: {gender_text(user['gender'])}\n\n"
        f"{rating_text(update.effective_user.id)}"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# START / REGISTRATION
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    tg_id = str(update.effective_user.id)

    if is_user_blocked(tg_id):

        await update.message.reply_text(
            "🚫 <b>Шумо аз истифодаи Nektome TJ блок шудаед.</b>\n\n"
            "Агар фикр мекунед, ки ин хато аст, "
            "ба техподдержка муроҷиат кунед.",
            parse_mode="HTML",
            reply_markup=support_keyboard()
        )
        return

    user = get_user(tg_id)

    if user:

        await update.message.reply_text(
            "👋 Хуш омадед ба <b>Nektome TJ</b>!",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return

    # /start resets unfinished registration/search state.
    waiting_users.pop(tg_id, None)
    context.user_data.clear()

    await update.message.reply_text(
        "🔥 <b>Хуш омадед ба Nektome TJ</b>\n\n"
        "Барои сохтани профил аввал синну солатонро интихоб кунед:",
        parse_mode="HTML",
        reply_markup=age_keyboard()
    )

    context.user_data["registration"] = True


# ============================================================
# REGISTRATION AGE
# ============================================================

async def registration_age(update: Update, context):

    query = update.callback_query
    await query.answer()

    data = query.data.replace("age:", "")

    if not context.user_data.get("registration"):
        await query.edit_message_text(
            "⚠️ Ин қадами регистрация дигар фаъол нест."
        )
        return

    if data == "MINOR":
        context.user_data["age_range"] = (13, 16)

    elif data == "17_20":
        context.user_data["age_range"] = (17, 20)

    elif data == "21_25":
        context.user_data["age_range"] = (21, 25)

    elif data == "26_PLUS":
        context.user_data["age_range"] = (26, 80)

    elif data == "ALL":
        await query.edit_message_text(
            "❗ Барои профил синну соли дақиқ лозим аст.\n\n"
            "Яке аз диапазонҳоро интихоб кунед:",
            reply_markup=age_keyboard()
        )
        return

    context.user_data["registration_age_range"] = True

    await query.edit_message_text(
        "🎂 Акнун синну соли дақиқатонро нависед.\n\n"
        "Масалан: <code>19</code>",
        parse_mode="HTML"
    )


async def registration_age_text(update, context):

    if not context.user_data.get("registration_age_range"):
        return

    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text(
            "❗ Танҳо рақам нависед."
        )
        return

    age = int(text)

    allowed = context.user_data["age_range"]

    if age < allowed[0] or age > allowed[1]:
        await update.message.reply_text(
            f"❗ Синну сол бояд дар диапазони "
            f"{allowed[0]}–{allowed[1]} бошад."
        )
        return

    context.user_data["age"] = age

    # Age was accepted. Do not treat following menu text as age.
    context.user_data["registration_age_range"] = False

    await update.message.reply_text(
        "✅ <b>Синну сол қабул шуд!</b>\n\n"
        f"🎂 Синну соли шумо: <b>{age}</b>\n\n"
        "🚻 Акнун ҷинси худро интихоб кунед:",
        parse_mode="HTML",
        reply_markup=gender_keyboard()
    )


# ============================================================
# REGISTRATION GENDER
# ============================================================

async def registration_gender(update, context):

    query = update.callback_query
    await query.answer()

    gender = query.data.replace("gender:", "")

    if gender not in (GENDER_MALE, GENDER_FEMALE):
        await query.edit_message_text(
            "❗ Интихоби ҷинс нодуруст аст."
        )
        return

    if "age" not in context.user_data:
        await query.edit_message_text(
            "❗ Аввал синну солро интихоб кунед."
        )
        return

    context.user_data["gender"] = gender

    tg_id = str(update.effective_user.id)
    nektome_id = generate_nektome_id()

    db_exec(
        """
        INSERT INTO users(
            telegram_id,
            nektome_id,
            age,
            gender,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            tg_id,
            nektome_id,
            context.user_data["age"],
            gender,
            datetime.utcnow().isoformat()
        )
    )

    context.user_data.clear()

    await query.edit_message_text(
        "🎉 <b>Табрик! Профили шумо бомуваффақият сохта шуд.</b>\n\n"
        f"🆔 ID-и шумо: <code>{nektome_id}</code>\n\n"
        "📜 <b>Қоидаҳои Nektome TJ:</b>\n"
        "• 🤝 Ба ҳамсӯҳбат эҳтиром гузоред.\n"
        "• 🚫 Таҳқир, таҳдид ва спам манъ аст.\n"
        "• 🔐 Маълумоти шахсии худро ба шахси ношинос нафиристед.\n"
        "• 🚨 Барои вайрон кардани қоидаҳо аз Репорт истифода баред.\n\n"
        "✨ Акнун метавонед ҳамсӯҳбати нав пайдо кунед!",
        parse_mode="HTML"
    )

    await context.bot.send_message(
        tg_id,
        "🏠 <b>Менюи асосӣ</b>\n\n"
        "Барои оғоз «👥 Ёфтани ҳамсӯҳбат»-ро пахш кунед.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# FIND CHAT
# ============================================================

async def find_chat_start(update: Update, context):

    tg_id = str(update.effective_user.id)

    if is_user_blocked(tg_id):
        await update.message.reply_text(
            "🚫 Шумо блок шудаед."
        )
        return

    if tg_id in active_chats:
        await update.message.reply_text(
            "💬 Шумо аллакай дар чат ҳастед.",
            reply_markup=chat_keyboard()
        )
        return

    if tg_id in waiting_users:
        await update.message.reply_text(
            "🔎 Шумо аллакай дар ҷустуҷӯи ҳамсӯҳбат ҳастед.\n\n"
            "Каме интизор шавед...\n\n"
            "Барои бекор кардан тугмаи поёнро пахш кунед.",
            reply_markup=search_keyboard()
        )
        return

    context.user_data["find_gender"] = None
    context.user_data["find_age"] = None

    # A new search starts a new matching session.
    last_partners.pop(tg_id, None)

    await update.message.reply_text(
        "👥 <b>Кадом ҳамсӯҳбатро меҷӯед?</b>\n\n"
        "Ҷинсро интихоб кунед:",
        parse_mode="HTML",
        reply_markup=find_gender_keyboard()
    )


async def find_gender(update, context):

    query = update.callback_query
    await query.answer()

    gender = query.data.replace("findgender:", "")

    if gender not in (
        GENDER_MALE,
        GENDER_FEMALE,
        GENDER_ALL,
    ):
        await query.edit_message_text(
            "❗ Интихоби ҷинс нодуруст аст."
        )
        return

    context.user_data["find_gender"] = gender

    await query.edit_message_text(
        "🎂 Синну соли ҳамсӯҳбатро интихоб кунед:",
        reply_markup=find_age_keyboard()
    )


async def find_age(update, context):

    query = update.callback_query
    await query.answer()

    data = query.data.replace("findage:", "")

    valid_age_filters = {
        AGE_ALL,
        AGE_MINOR,
        AGE_17_20,
        AGE_21_25,
        AGE_ADULT,
    }

    if data not in valid_age_filters:
        await query.edit_message_text(
            "❗ Интихоби синну сол нодуруст аст."
        )
        return

    tg_id = str(update.effective_user.id)

    # A user cannot enter the queue while already chatting.
    if tg_id in active_chats:
        await query.edit_message_text(
            "💬 Шумо аллакай дар чат ҳастед.",
            reply_markup=None
        )
        return

    gender_filter = context.user_data.get(
        "find_gender",
        GENDER_ALL
    )

    if gender_filter not in (
        GENDER_MALE,
        GENDER_FEMALE,
        GENDER_ALL,
    ):
        gender_filter = GENDER_ALL

    waiting_users[tg_id] = {
        "gender": gender_filter,
        "age": data,
    }

    context.user_data["find_age"] = data

    logger.info(
        "QUEUE ENTER: user=%s gender=%s age=%s waiting=%s",
        tg_id,
        gender_filter,
        data,
        list(waiting_users.keys())
    )

    await query.edit_message_text(
        "🔎 <b>Ҳамсӯҳбат ҷустуҷӯ мешавад...</b>\n\n"
        "⏳ Лутфан каме интизор шавед.\n\n"
        "Барои бекор кардан тугмаи поёнро пахш кунед.",
        parse_mode="HTML",
        reply_markup=search_inline_keyboard()
    )

    await try_match(tg_id, context)


# ============================================================
# SEARCH CALLBACK
# ============================================================

async def search_callback(update, context):

    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)

    if query.data == "search:cancel":

        removed = waiting_users.pop(user_id, None)

        if removed is None:
            await query.edit_message_text(
                "ℹ️ Шумо дигар дар ҷустуҷӯ нестед."
            )
            return

        await query.edit_message_text(
            "❌ <b>Ҷустуҷӯ қатъ карда шуд.</b>",
            parse_mode="HTML"
        )

        await context.bot.send_message(
            chat_id=user_id,
            text="🏠 Менюи асосӣ",
            reply_markup=main_keyboard()
        )

        logger.info(
            "QUEUE CANCEL: user=%s",
            user_id
        )


# ============================================================
# MATCHING
# ============================================================

def can_match(user_a, pref_a, user_b, pref_b):

    # --------------------------------------------------------
    # BASIC VALIDATION
    # --------------------------------------------------------

    if not user_a or not user_b:
        return False

    if str(user_a) == str(user_b):
        return False

    # --------------------------------------------------------
    # LOAD BOTH PROFILES ONCE
    # --------------------------------------------------------

    profile_a = get_user(str(user_a))
    profile_b = get_user(str(user_b))

    # A queue entry without a real profile is invalid.
    if profile_a is None or profile_b is None:
        logger.error(
            "MATCH PROFILE MISSING: A=%s exists=%s | B=%s exists=%s",
            user_a,
            profile_a is not None,
            user_b,
            profile_b is not None,
        )
        return False

    # --------------------------------------------------------
    # BLOCK / ACTIVE CHAT CHECKS
    # --------------------------------------------------------

    if is_blocked(str(user_a), str(user_b)):
        return False

    if str(user_a) in active_chats:
        return False

    if str(user_b) in active_chats:
        return False

    # --------------------------------------------------------
    # VALIDATE PREFERENCES
    # --------------------------------------------------------

    if not isinstance(pref_a, dict) or not isinstance(pref_b, dict):
        return False

    wanted_gender_a = pref_a.get("gender", GENDER_ALL)
    wanted_gender_b = pref_b.get("gender", GENDER_ALL)

    wanted_age_a = pref_a.get("age", AGE_ALL)
    wanted_age_b = pref_b.get("age", AGE_ALL)

    valid_genders = {
        GENDER_MALE,
        GENDER_FEMALE,
        GENDER_ALL,
    }

    valid_ages = {
        AGE_ALL,
        AGE_MINOR,
        AGE_17_20,
        AGE_21_25,
        AGE_ADULT,
    }

    if wanted_gender_a not in valid_genders:
        return False

    if wanted_gender_b not in valid_genders:
        return False

    if wanted_age_a not in valid_ages:
        return False

    if wanted_age_b not in valid_ages:
        return False

    # --------------------------------------------------------
    # MUTUAL GENDER + AGE MATCH
    # --------------------------------------------------------

    # A must accept B.
    if not gender_matches(
        profile_b["gender"],
        wanted_gender_a
    ):
        return False

    if not age_matches(
        profile_b["age"],
        wanted_age_a
    ):
        return False

    # B must accept A.
    if not gender_matches(
        profile_a["gender"],
        wanted_gender_b
    ):
        return False

    if not age_matches(
        profile_a["age"],
        wanted_age_b
    ):
        return False

    return True


async def try_match(user_id, context):

    if user_id not in waiting_users:
        logger.info(
            "MATCH SKIP: user=%s not in queue",
            user_id
        )
        return False

    if user_id in active_chats:
        waiting_users.pop(user_id, None)
        logger.warning(
            "MATCH CLEANUP: user=%s was already in active chat",
            user_id
        )
        return False

    my_pref = waiting_users.get(user_id)

    if not my_pref:
        waiting_users.pop(user_id, None)
        logger.warning(
            "MATCH CLEANUP: user=%s has empty preferences",
            user_id
        )
        return False

    logger.info(
        "MATCH START: user=%s queue=%s",
        user_id,
        dict(waiting_users)
    )

    candidates = list(waiting_users.keys())
    random.shuffle(candidates)

    for candidate in candidates:

        if candidate == user_id:
            continue

        if candidate not in waiting_users:
            continue

        if candidate in active_chats:
            waiting_users.pop(candidate, None)
            continue

        other_pref = waiting_users.get(candidate)

        if not can_match(
            user_id,
            my_pref,
            candidate,
            other_pref
        ):
            continue

        # Final atomic-style validation before pairing.
        if user_id not in waiting_users:
            return False

        if candidate not in waiting_users:
            continue

        if user_id in active_chats or candidate in active_chats:
            continue

        waiting_users.pop(user_id, None)
        waiting_users.pop(candidate, None)

        active_chats[user_id] = candidate
        active_chats[candidate] = user_id

        key = pair_key(user_id, candidate)
        chat_logs[key] = []

        logger.info(
            "MATCH SUCCESS: %s <-> %s | active_chats=%s | queue=%s",
            user_id,
            candidate,
            active_chats,
            waiting_users
        )

        try:
            await send_match_message(
                user_id,
                candidate,
                context
            )

            await send_match_message(
                candidate,
                user_id,
                context
            )

            logger.info(
                "MATCH NOTIFICATIONS SENT: %s <-> %s",
                user_id,
                candidate
            )

        except Exception:
            logger.exception(
                "MATCH NOTIFICATION FAILED: %s <-> %s",
                user_id,
                candidate
            )

        return True

    logger.info(
        "MATCH NOT FOUND: user=%s queue=%s",
        user_id,
        dict(waiting_users)
    )

    return False


async def send_match_message(user_id, partner_id, context):

    partner = get_user(partner_id)

    if not partner:
        return

    text = (
        "💬 <b>Ҳамсӯҳбат ёфт шуд!</b>\n\n"
        f"👤 Шумо бо <b>{gender_text(partner['gender'])}</b> "
        f"суҳбат мекунед.\n\n"
        f"{rating_text(partner_id)}\n\n"
        "Ҳамсӯҳбат намедонад шумо кистед.\n"
        "Паём нависед 👇"
    )

    await context.bot.send_message(
        user_id,
        text,
        parse_mode="HTML",
        reply_markup=chat_keyboard()
    )


# ============================================================
# CHAT
# ============================================================

async def relay_chat_message(update: Update, context):

    if not update.message:
        return

    user_id = str(update.effective_user.id)

    if user_id not in active_chats:
        return

    partner_id = active_chats[user_id]

    if is_blocked(user_id, partner_id):
        return

    text = update.message.text

    if not text:
        await update.message.reply_text(
            "ℹ️ Дар ин версия танҳо паёмҳои матнӣ дастгирӣ мешаванд."
        )
        return

    key = pair_key(user_id, partner_id)

    if key not in chat_logs:
        chat_logs[key] = []

    sender = get_user(user_id)

    chat_logs[key].append(
        {
            "sender": sender["nektome_id"],
            "text": text,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    )

    try:
        await context.bot.send_message(
            partner_id,
            text
        )
    except Exception as e:
        logger.error("Send message error: %s", e)


# ============================================================
# LEAVE CHAT
# ============================================================

async def leave_chat(update, context):

    user_id = str(update.effective_user.id)

    if user_id not in active_chats:

        await update.message.reply_text(
            "ℹ️ Шумо дар чат нестед.",
            reply_markup=main_keyboard()
        )
        return

    partner_id = active_chats.get(user_id)

    active_chats.pop(user_id, None)

    if partner_id:
        active_chats.pop(partner_id, None)

    if partner_id:
        last_partners[user_id] = partner_id
        last_partners[partner_id] = user_id

        try:
            await context.bot.send_message(
                partner_id,
                "❌ Ҳамсӯҳбат чатро ба анҷом расонд.\n\n"
                "Шумо метавонед ӯро баҳо диҳед:",
                reply_markup=rating_keyboard()
            )
        except Exception:
            pass

    await update.message.reply_text(
        "❌ Чат ба анҷом расид.\n\n"
        "Шумо метавонед ҳамсӯҳбатро баҳо диҳед:",
        reply_markup=rating_keyboard()
    )


# ============================================================
# RATING CALLBACK
# ============================================================

async def rating_callback(update, context):

    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)

    if query.data == "report":

        partner_id = last_partners.get(user_id)

        if not partner_id:
            await query.edit_message_text(
                "❗ Ҳамсӯҳбат ёфт нашуд."
            )
            return

        await create_report(
            user_id,
            partner_id,
            context
        )

        await query.edit_message_text(
            "🚨 Репорт фиристода шуд.\n\n"
            "Ташаккур."
        )
        return

    rating = query.data.replace("rate:", "")

    partner_id = last_partners.get(user_id)

    if not partner_id:
        await query.edit_message_text(
            "❗ Ҳамсӯҳбат дигар дастрас нест."
        )
        return

    db_exec(
        """
        INSERT INTO ratings(
            from_user,
            to_user,
            rating
        )
        VALUES (?, ?, ?)
        ON CONFLICT(from_user, to_user)
        DO UPDATE SET rating=excluded.rating
        """,
        (
            user_id,
            partner_id,
            rating
        )
    )

    last_partners.pop(user_id, None)

    await query.edit_message_text(
        "✅ Баҳо қабул шуд."
    )


# ============================================================
# REPORT
# ============================================================

async def create_report(reporter, reported, context):

    reporter_user = get_user(reporter)
    reported_user = get_user(reported)

    if not reporter_user or not reported_user:
        return

    db_exec(
        """
        INSERT INTO reports(
            reporter,
            reported,
            reporter_nektome_id,
            reported_nektome_id,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            reporter,
            reported,
            reporter_user["nektome_id"],
            reported_user["nektome_id"],
            datetime.now().isoformat()
        )
    )

    report_id = db_one(
        "SELECT last_insert_rowid() AS id"
    )["id"]

    key = pair_key(reporter, reported)

    messages = chat_logs.get(key, [])

    filename = f"report_{report_id}.txt"

    with open(filename, "w", encoding="utf-8") as f:

        f.write("NEKTOME TJ — CHAT REPORT\n")
        f.write("=" * 50 + "\n\n")

        f.write(
            f"Report ID: {report_id}\n"
            f"Reporter: {reporter_user['nektome_id']}\n"
            f"Reported: {reported_user['nektome_id']}\n"
            f"Reporter age: {reporter_user['age']}\n"
            f"Reporter gender: {gender_text(reporter_user['gender'])}\n"
            f"Reported age: {reported_user['age']}\n"
            f"Reported gender: {gender_text(reported_user['gender'])}\n"
            f"Created: {datetime.now()}\n\n"
        )

        f.write("=" * 50 + "\n")
        f.write("CHAT\n")
        f.write("=" * 50 + "\n\n")

        for message in messages:
            f.write(
                f"[{message['time']}] "
                f"ID {message['sender']}: "
                f"{message['text']}\n"
            )

    if ADMIN_ID:

        try:

            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🚫 BLOCK",
                        callback_data=f"admin:block:{reported}"
                    ),
                    InlineKeyboardButton(
                        "✅ REJECT",
                        callback_data=f"admin:reject:{report_id}"
                    )
                ]
            ])

            caption = (
                "🚨 <b>NEW REPORT</b>\n\n"
                f"Report ID: <code>{report_id}</code>\n"
                f"Reporter ID: <code>{reporter_user['nektome_id']}</code>\n"
                f"Reported ID: <code>{reported_user['nektome_id']}</code>"
            )

            await context.bot.send_document(
                chat_id=int(ADMIN_ID),
                document=open(filename, "rb"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=buttons
            )

        except Exception as e:
            logger.error(
                "Admin report error: %s",
                e
            )

    try:
        os.remove(filename)
    except Exception:
        pass

    # After report, temporary transcript is deleted.
    chat_logs.pop(key, None)


# ============================================================
# ADMIN REPORT ACTIONS
# ============================================================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    admin_id = str(update.effective_user.id)

    # Only configured admin may use these buttons.
    if not ADMIN_ID or admin_id != ADMIN_ID:
        await query.answer(
            "🚫 Дастрасӣ манъ аст.",
            show_alert=True
        )
        return

    data = query.data

    # --------------------------------------------------------
    # REJECT REPORT
    # --------------------------------------------------------

    if data.startswith("admin:reject:"):

        try:
            report_id = int(data.split(":")[2])
        except (ValueError, IndexError):
            await query.edit_message_caption(
                caption="❌ Report ID нодуруст аст."
            )
            return

        report = db_one(
            "SELECT * FROM reports WHERE id=?",
            (report_id,)
        )

        if not report:
            await query.edit_message_caption(
                caption="❌ Report ёфт нашуд."
            )
            return

        db_exec(
            """
            UPDATE reports
            SET status='rejected'
            WHERE id=?
            """,
            (report_id,)
        )

        await query.edit_message_caption(
            caption=(
                "✅ <b>REPORT REJECTED</b>\n\n"
                f"Report ID: <code>{report_id}</code>\n"
                "Статус: rejected"
            ),
            parse_mode="HTML"
        )

        return

    # --------------------------------------------------------
    # BLOCK USER
    # --------------------------------------------------------

    if data.startswith("admin:block:"):

        try:
            blocked_user = data.split(":")[2]
        except IndexError:
            await query.edit_message_caption(
                caption="❌ User ID нодуруст аст."
            )
            return

        user = get_user(blocked_user)

        if not user:
            await query.edit_message_caption(
                caption="❌ Корбар ёфт нашуд."
            )
            return

        # Do not allow blocking the configured admin.
        if blocked_user == ADMIN_ID:
            await query.answer(
                "🚫 Admin-ро block кардан мумкин нест.",
                show_alert=True
            )
            return

        # Add permanent block.
        db_exec(
            """
            INSERT OR IGNORE INTO blocks(blocker, blocked)
            VALUES (?, ?)
            """,
            (ADMIN_ID, blocked_user)
        )

        # Mark all pending reports concerning this user as handled.
        db_exec(
            """
            UPDATE reports
            SET status='blocked'
            WHERE reported=?
              AND status='pending'
            """,
            (blocked_user,)
        )

        # Remove from matchmaking queue.
        waiting_users.pop(blocked_user, None)

        # Close active chat if one exists.
        partner_id = active_chats.pop(blocked_user, None)

        if partner_id:
            active_chats.pop(partner_id, None)

            last_partners[blocked_user] = partner_id
            last_partners[partner_id] = blocked_user

            try:
                await context.bot.send_message(
                    partner_id,
                    "❌ Чат бо сабаби вайрон шудани қоидаҳо баста шуд.",
                    reply_markup=main_keyboard()
                )
            except Exception as e:
                logger.warning(
                    "Could not notify partner after admin block: %s",
                    e
                )

        # Remove old partner state.
        last_partners.pop(blocked_user, None)

        # Notify blocked user.
        try:
            await context.bot.send_message(
                blocked_user,
                "🚫 <b>Шумо аз Nektome TJ блок шудед.</b>\n\n"
                "Сабаб: вайрон кардани қоидаҳои хизматрасонӣ.\n\n"
                "Агар фикр мекунед, ки ин қарор хато аст, "
                "ба техподдержка муроҷиат кунед.",
                parse_mode="HTML",
                reply_markup=support_keyboard()
            )
        except Exception as e:
            logger.warning(
                "Could not notify blocked user: %s",
                e
            )

        await query.edit_message_caption(
            caption=(
                "🚫 <b>USER BLOCKED</b>\n\n"
                f"Report user: <code>{user['nektome_id']}</code>\n"
                f"Telegram ID: <code>{blocked_user}</code>\n"
                "Статус: blocked"
            ),
            parse_mode="HTML"
        )

        return

    await query.answer(
        "❓ Амалиёт номаълум аст.",
        show_alert=True
    )



# ============================================================
# SUPPORT
# ============================================================

async def support(update, context):

    if SUPPORT_USERNAME:

        await update.message.reply_text(
            "🛠 <b>Техподдержка</b>\n\n"
            "Барои савол, пешниҳод, идея ё шикоят ба ман муроҷиат кунед.",
            parse_mode="HTML",
            reply_markup=support_keyboard()
        )

    else:

        await update.message.reply_text(
            "🛠 Техподдержка ҳоло танзим нашудааст."
        )


# ============================================================
# COMMANDS
# ============================================================

async def stop_search(update, context):

    user_id = str(update.effective_user.id)

    if user_id in waiting_users:

        waiting_users.pop(user_id, None)

        await update.message.reply_text(
            "❌ Ҷустуҷӯ қатъ карда шуд.",
            reply_markup=main_keyboard()
        )

        return

    await update.message.reply_text(
        "ℹ️ Шумо ҳоло дар ҷустуҷӯ нестед.",
        reply_markup=main_keyboard()
    )


# ============================================================
# MAIN MESSAGE ROUTER
# ============================================================

async def message_router(update, context):

    if not update.message:
        return

    user_id = str(update.effective_user.id)

    if is_user_blocked(user_id):

        await update.message.reply_text(
            "🚫 Шумо блок шудаед.",
            reply_markup=support_keyboard()
        )
        return

    text = (update.message.text or "").strip()

    # Stop-search must work immediately while user is in queue.
    if text == "❌ Қатъ кардани ҷустуҷӯ":
        await stop_search(update, context)
        return

    # Chat messages have priority
    if user_id in active_chats:

        if text == "❌ Баромадан аз чат":
            await leave_chat(update, context)
            return

        await relay_chat_message(update, context)
        return

    if text == "👤 Профили ман":
        await my_profile(update, context)
        return

    if text == "👥 Ёфтани ҳамсӯҳбат":
        await find_chat_start(update, context)
        return

    if text == "🛠 Техподдержка":
        await support(update, context)
        return

    if user_id in waiting_users:

        await update.message.reply_text(
            "🔎 Шумо ҳоло дар ҷустуҷӯ ҳастед.\n\n"
            "Барои бекор кардан «❌ Қатъ кардани ҷустуҷӯ»-ро пахш кунед.",
            reply_markup=search_keyboard()
        )
        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):

    logger.exception(
        "Unhandled error:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Nektome TJ is running")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"🌐 Health server listening on port {port}")
    server.serve_forever()


def main():

    print("=" * 40)
    print(" Nektome TJ")
    print(" Simple Anonymous Chat")
    print("=" * 40)

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    # Registration
    app.add_handler(
        CallbackQueryHandler(
            registration_age,
            pattern=r"^age:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            registration_gender,
            pattern=r"^gender:"
        )
    )

    # Finding
    app.add_handler(
        CallbackQueryHandler(
            search_callback,
            pattern=r"^search:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            find_gender,
            pattern=r"^findgender:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            find_age,
            pattern=r"^findage:"
        )
    )

    # Admin report actions
    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:(block|reject):"
        )
    )

    # Rating / report
    app.add_handler(
        CallbackQueryHandler(
            rating_callback,
            pattern=r"^(rate:|report$)"
        )
    )

    # Commands
    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("stop", stop_search)
    )

    # Text
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            registration_age_text
        ),
        group=1
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_router
        ),
        group=2
    )

    app.add_error_handler(error_handler)

    print("🤖 BOT RUNNING")

    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
