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

# Increase this number whenever the bot is updated.
BOT_VERSION = "1.1.0"

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

CREATE TABLE IF NOT EXISTS search_preferences (
    telegram_id TEXT PRIMARY KEY,
    gender TEXT NOT NULL DEFAULT 'ALL',
    age_filter TEXT NOT NULL DEFAULT 'ALL',
    updated_at TEXT NOT NULL
);

""")

conn.commit()


# ============================================================
# BOT UPDATE STATE
# ============================================================

conn.execute("""
CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT
)
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
    return "Зан"


def age_filter_text(value):
    if value == AGE_MINOR:
        return "13–16"
    if value == AGE_ADULT:
        return "17+"
    return "Ҳама"


def age_matches(age, age_filter):
    if age_filter == AGE_MINOR:
        return 13 <= age <= 16

    if age_filter == AGE_ADULT:
        return age >= 17

    return True


def gender_matches(gender, wanted):
    return wanted == GENDER_ALL or gender == wanted


def pair_key(a, b):
    return ":".join(sorted([str(a), str(b)]))



# ============================================================
# SAVED SEARCH SETTINGS
# ============================================================

def get_saved_search(user_id):
    row = db_one(
        """
        SELECT gender, age_filter
        FROM search_preferences
        WHERE telegram_id=?
        """,
        (str(user_id),)
    )

    if not row:
        return {
            "gender": GENDER_ALL,
            "age": AGE_ALL
        }

    return {
        "gender": row["gender"],
        "age": row["age_filter"]
    }


def save_search_settings(user_id, gender, age_filter):
    db_exec(
        """
        INSERT INTO search_preferences(
            telegram_id,
            gender,
            age_filter,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id)
        DO UPDATE SET
            gender=excluded.gender,
            age_filter=excluded.age_filter,
            updated_at=excluded.updated_at
        """,
        (
            str(user_id),
            gender,
            age_filter,
            datetime.utcnow().isoformat()
        )
    )


# ============================================================
# KEYBOARDS
# ============================================================


def settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔎 Тағйири ҷустуҷӯ",
                callback_data="settings:search"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Тағйири профил",
                callback_data="settings:profile"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Бозгашт",
                callback_data="settings:back"
            )
        ]
    ])


def settings_search_gender_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨 Мард",
                callback_data="settingsgender:M"
            ),
            InlineKeyboardButton(
                "👩 Зан",
                callback_data="settingsgender:F"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Ҳама",
                callback_data="settingsgender:ALL"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Бозгашт",
                callback_data="settings:open"
            )
        ]
    ])


def settings_search_age_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "13–16",
                callback_data="settingsage:MINOR"
            ),
            InlineKeyboardButton(
                "17–20",
                callback_data="settingsage:17_20"
            )
        ],
        [
            InlineKeyboardButton(
                "21–25",
                callback_data="settingsage:21_25"
            ),
            InlineKeyboardButton(
                "26+",
                callback_data="settingsage:ADULT"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Ҳама",
                callback_data="settingsage:ALL"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Бозгашт",
                callback_data="settings:open"
            )
        ]
    ])


def settings_profile_gender_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨 Мард",
                callback_data="profilegender:M"
            ),
            InlineKeyboardButton(
                "👩 Зан",
                callback_data="profilegender:F"
            )
        ]
    ])


def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["👤 Профили ман", "👥 Ёфтани ҳамсӯҳбат"],
            ["⚙️ Танзимот"],
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
        ],
        [
            InlineKeyboardButton(
                "👥 Ҳама",
                callback_data="gender:ALL"
            )
        ],
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
# BOT UPDATE NOTIFICATION
# ============================================================

async def notify_users_about_update(application):

    row = db_one(
        "SELECT value FROM bot_meta WHERE key=?",
        ("last_notified_version",)
    )

    last_version = row["value"] if row else None

    # Do not notify users on ordinary restarts.
    if last_version == BOT_VERSION:
        logger.info(
            "UPDATE NOTIFICATION: already sent for version %s",
            BOT_VERSION
        )
        return

    users = db_all(
        "SELECT telegram_id FROM users"
    )

    logger.info(
        "BOT UPDATE: version=%s | users=%s",
        BOT_VERSION,
        len(users)
    )

    message = (
        "🔄 <b>Nektome TJ навсозӣ шуд!</b>\n\n"
        "✨ Бот ба версияи нав гузашт.\n\n"
        "Барои идомаи истифода ботро аз нав оғоз кунед:\n"
        "👉 /start"
    )

    success = 0
    failed = 0

    for row in users:

        user_id = str(row["telegram_id"])

        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="HTML"
            )

            success += 1

        except Exception as e:

            failed += 1

            logger.warning(
                "UPDATE MESSAGE FAILED: user=%s error=%s",
                user_id,
                e
            )

    # Save only after notification attempt.
    db_exec(
        """
        INSERT INTO bot_meta(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        ("last_notified_version", BOT_VERSION)
    )

    logger.info(
        "UPDATE NOTIFICATION COMPLETE: version=%s success=%s failed=%s",
        BOT_VERSION,
        success,
        failed
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

def search_inline_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Қатъ кардани ҷустуҷӯ",
                callback_data="search:cancel"
            )
        ]
    ])


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
            "🔎 Шумо аллакай дар ҷустуҷӯ ҳастед.\n\n"
            "Барои бекор кардан тугмаи поёнро пахш кунед.",
            reply_markup=search_inline_keyboard()
        )
        return

    pref = get_saved_search(tg_id)

    # If no preference exists yet, keep ALL / ALL as the default.
    if not db_one(
        "SELECT 1 FROM search_preferences WHERE telegram_id=?",
        (tg_id,)
    ):
        save_search_settings(
            tg_id,
            GENDER_ALL,
            AGE_ALL
        )
        pref = {
            "gender": GENDER_ALL,
            "age": AGE_ALL
        }

    gender_filter = pref["gender"]
    age_filter = pref["age"]

    waiting_users[tg_id] = {
        "gender": gender_filter,
        "age": age_filter
    }

    context.user_data["find_gender"] = gender_filter
    context.user_data["find_age"] = age_filter

    logger.info(
        "QUEUE ENTER SAVED: user=%s gender=%s age=%s waiting=%s",
        tg_id,
        gender_filter,
        age_filter,
        list(waiting_users.keys())
    )

    await update.message.reply_text(
        "🔎 <b>Ҳамсӯҳбат ҷустуҷӯ мешавад...</b>\n\n"
        f"👤 Ҷинс: {gender_text(gender_filter)}\n"
        f"🎂 Синну сол: {age_filter_text(age_filter)}\n\n"
        "⏳ Лутфан каме интизор шавед.",
        parse_mode="HTML",
        reply_markup=search_inline_keyboard()
    )

    await try_match(tg_id, context)


async def find_gender(update, context):

    query = update.callback_query
    await query.answer()

    gender = query.data.replace("findgender:", "")

    context.user_data["find_gender"] = gender

    await query.edit_message_text(
        "🎂 Синну соли ҳамсӯҳбатро интихоб кунед:",
        reply_markup=find_age_keyboard()
    )


async def find_age(update, context):

    query = update.callback_query
    await query.answer()

    data = query.data.replace("findage:", "")

    context.user_data["find_age"] = data

    tg_id = str(update.effective_user.id)

    gender_filter = context.user_data.get(
        "find_gender",
        GENDER_ALL
    )

    age_filter = data

    waiting_users[tg_id] = {
        "gender": gender_filter,
        "age": age_filter,
    }

    logger.info(
        "QUEUE ENTER: user=%s gender=%s age=%s waiting=%s",
        tg_id,
        gender_filter,
        age_filter,
        list(waiting_users.keys())
    )

    await query.edit_message_text(
        "🔎 <b>Ҳамсӯҳбат ҷустуҷӯ мешавад...</b>\n\n"
        "Лутфан каме интизор шавед.",
        parse_mode="HTML"
    )

    await context.bot.send_message(
        chat_id=tg_id,
        text="⏳ Шумо ба навбати ҷустуҷӯ дохил шудед.\n"
             "Барои қатъ кардан тугмаи поёнро пахш кунед.",
        reply_markup=search_keyboard()
    )

    await try_match(tg_id, context)


# ============================================================
# MATCHING
# ============================================================

def can_match(user_a, pref_a, user_b, pref_b):

    if user_a == user_b:
        return False

    if is_blocked(user_a, user_b):
        return False

    a_gender = get_user(user_a)["gender"]
    b_gender = get_user(user_b)["gender"]

    a_age = get_user(user_a)["age"]
    b_age = get_user(user_b)["age"]

    if not gender_matches(b_gender, pref_a["gender"]):
        return False

    if not gender_matches(a_gender, pref_b["gender"]):
        return False

    if not age_matches(b_age, pref_a["age"]):
        return False

    if not age_matches(a_age, pref_b["age"]):
        return False

    return True


async def try_match(user_id, context):

    if user_id not in waiting_users:
        return

    my_pref = waiting_users[user_id]

    candidates = list(waiting_users.keys())
    random.shuffle(candidates)

    for candidate in candidates:

        if candidate == user_id:
            continue

        if candidate not in waiting_users:
            continue

        other_pref = waiting_users[candidate]

        logger.info(
            "MATCH CHECK: %s vs %s | my=%s other=%s",
            user_id,
            candidate,
            my_pref,
            other_pref
        )

        if not can_match(
            user_id,
            my_pref,
            candidate,
            other_pref
        ):
            logger.info(
                "MATCH REJECTED: %s vs %s",
                user_id,
                candidate
            )
            continue

        logger.info(
            "MATCH FOUND: %s <-> %s",
            user_id,
            candidate
        )

        waiting_users.pop(user_id, None)
        waiting_users.pop(candidate, None)

        active_chats[user_id] = candidate
        active_chats[candidate] = user_id

        key = pair_key(user_id, candidate)

        chat_logs[key] = []

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

        return


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
    partner_id = active_chats.get(user_id)

    if not partner_id:
        return

    if is_blocked(user_id, partner_id):
        return

    text = update.message.text
    if not text:
        await update.message.reply_text("ℹ️ Дар ин версия танҳо паёмҳои матнӣ дастгирӣ мешаванд.")
        return

    key = pair_key(user_id, partner_id)
    if key not in chat_logs:
        chat_logs[key] = []

    sender = get_user(user_id)
    chat_logs[key].append({
        "sender": sender.get("nektome_id", "Unknown"),
        "text": text,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

    try:
        await context.bot.send_message(chat_id=int(partner_id), text=text)
    except Exception as e:
        logger.error(f"Relay message error to {partner_id}: {e}")
        active_chats.pop(user_id, None)
        active_chats.pop(partner_id, None)
        try:
            await update.message.reply_text(
                "❌ Ҳамсӯҳбати шумо ботро блок кард ё чатро тарк намуд.",
                reply_markup=main_keyboard()
            )
        except Exception:
            pass

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

    # Replace the old chat keyboard with the main menu.
    await update.message.reply_text(
        "🏠 <b>Менюи асосӣ</b>\n\n"
        "Барои оғоз «👥 Ёфтани ҳамсӯҳбат»-ро пахш кунед.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
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
# SETTINGS
# ============================================================

async def settings_menu(update, context):

    user_id = str(update.effective_user.id)
    user = get_user(user_id)

    if not user:
        await update.message.reply_text(
            "❗ Аввал /start кунед."
        )
        return

    pref = get_saved_search(user_id)

    await update.message.reply_text(
        "⚙️ <b>Танзимот</b>\n\n"
        "👤 <b>Профили ман</b>\n"
        f"• Ҷинс: {gender_text(user['gender'])}\n"
        f"• Синну сол: {user['age']}\n\n"
        "🔎 <b>Ҷустуҷӯ</b>\n"
        f"• Ҷинс: {gender_text(pref['gender'])}\n"
        f"• Синну сол: {age_filter_text(pref['age'])}",
        parse_mode="HTML",
        reply_markup=settings_keyboard()
    )


async def settings_callback(update, context):

    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    data = query.data

    if data == "settings:open":

        user = get_user(user_id)

        if not user:
            await query.edit_message_text(
                "❗ Аввал /start кунед."
            )
            return

        pref = get_saved_search(user_id)

        await query.edit_message_text(
            "⚙️ <b>Танзимот</b>\n\n"
            "👤 <b>Профили ман</b>\n"
            f"• Ҷинс: {gender_text(user['gender'])}\n"
            f"• Синну сол: {user['age']}\n\n"
            "🔎 <b>Ҷустуҷӯ</b>\n"
            f"• Ҷинс: {gender_text(pref['gender'])}\n"
            f"• Синну сол: {age_filter_text(pref['age'])}",
            parse_mode="HTML",
            reply_markup=settings_keyboard()
        )
        return

    if data == "settings:back":

        await query.edit_message_text(
            "🏠 <b>Менюи асосӣ</b>\n\n"
            "Барои идома тугмаҳои менюро истифода баред.",
            parse_mode="HTML"
        )

        await context.bot.send_message(
            user_id,
            "🏠 Менюи асосӣ",
            reply_markup=main_keyboard()
        )
        return

    if data == "settings:search":

        await query.edit_message_text(
            "🔎 <b>Тағйири ҷустуҷӯ</b>\n\n"
            "Ҷинси ҳамсӯҳбатро интихоб кунед:",
            parse_mode="HTML",
            reply_markup=settings_search_gender_keyboard()
        )
        return

    if data == "settings:profile":

        context.user_data["settings_profile"] = True

        await query.edit_message_text(
            "👤 <b>Тағйири профил</b>\n\n"
            "Синну соли нави худро нависед.\n\n"
            "Масалан: <code>20</code>",
            parse_mode="HTML"
        )
        return


async def settings_gender_callback(update, context):

    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    gender = query.data.replace("settingsgender:", "")

    if gender not in (
        GENDER_MALE,
        GENDER_FEMALE,
        GENDER_ALL
    ):
        await query.edit_message_text(
            "❌ Интихоби ҷинс нодуруст аст."
        )
        return

    context.user_data["settings_search_gender"] = gender

    await query.edit_message_text(
        "🎂 <b>Синну соли ҳамсӯҳбатро интихоб кунед:</b>",
        parse_mode="HTML",
        reply_markup=settings_search_age_keyboard()
    )


async def settings_age_callback(update, context):

    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    age_filter = query.data.replace("settingsage:", "")

    valid = {
        AGE_ALL,
        AGE_MINOR,
        AGE_17_20,
        AGE_21_25,
        AGE_ADULT
    }

    if age_filter not in valid:
        await query.edit_message_text(
            "❌ Интихоби синну сол нодуруст аст."
        )
        return

    gender = context.user_data.get(
        "settings_search_gender",
        get_saved_search(user_id)["gender"]
    )

    save_search_settings(
        user_id,
        gender,
        age_filter
    )

    context.user_data.pop("settings_search_gender", None)

    await query.edit_message_text(
        "✅ <b>Ҷустуҷӯ тағйир ёфт.</b>\n\n"
        f"👤 Ҷинс: {gender_text(gender)}\n"
        f"🎂 Синну сол: {age_filter_text(age_filter)}\n\n"
        "Ин параметрҳо то тағйири навбатӣ нигоҳ дошта мешаванд.",
        parse_mode="HTML",
        reply_markup=settings_keyboard()
    )


async def settings_profile_age_text(update, context):

    if not context.user_data.get("settings_profile"):
        return False

    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text(
            "❗ Синну сол бояд рақам бошад."
        )
        return True

    age = int(text)

    if age < 13 or age > 100:
        await update.message.reply_text(
            "❗ Синну сол бояд аз 13 то 100 бошад."
        )
        return True

    context.user_data["settings_profile_age"] = age
    context.user_data.pop("settings_profile", None)

    await update.message.reply_text(
        "🚻 <b>Ҷинси нави худро интихоб кунед:</b>",
        parse_mode="HTML",
        reply_markup=settings_profile_gender_keyboard()
    )

    return True


async def settings_profile_gender_callback(update, context):

    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    gender = query.data.replace("profilegender:", "")

    if gender not in (
        GENDER_MALE,
        GENDER_FEMALE
    ):
        await query.edit_message_text(
            "❌ Ҷинс нодуруст аст."
        )
        return

    age = context.user_data.get("settings_profile_age")

    if age is None:
        await query.edit_message_text(
            "❗ Аввал синну солро интихоб кунед."
        )
        return

    db_exec(
        """
        UPDATE users
        SET age=?, gender=?
        WHERE telegram_id=?
        """,
        (age, gender, user_id)
    )

    context.user_data.pop("settings_profile_age", None)

    await query.edit_message_text(
        "✅ <b>Профил нав карда шуд.</b>\n\n"
        f"🎂 Синну сол: {age}\n"
        f"🚻 Ҷинс: {gender_text(gender)}",
        parse_mode="HTML",
        reply_markup=settings_keyboard()
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
# DEDICATED STOP SEARCH BUTTON
# ============================================================

async def stop_search_button(update, context):

    if not update.message:
        return

    user_id = str(update.effective_user.id)

    # Remove from queue immediately.
    waiting_users.pop(user_id, None)

    # Clear temporary search state.
    context.user_data.pop("find_gender", None)
    context.user_data.pop("find_age", None)

    logger.info(
        "SEARCH STOPPED: user=%s waiting=%s",
        user_id,
        user_id in waiting_users
    )

    await update.message.reply_text(
        "❌ <b>Ҷустуҷӯ қатъ карда шуд.</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

# ============================================================
# MAIN MESSAGE ROUTER
# ============================================================


async def settings_text_router(update, context):

    if not update.message:
        return

    handled = await settings_profile_age_text(update, context)

    if handled:
        return


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

    if text == "⚙️ Танзимот":
        await settings_menu(update, context)
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
        .post_init(notify_users_about_update)
        .build()
    )

    # Settings callbacks
    app.add_handler(
        CallbackQueryHandler(
            settings_callback,
            pattern=r"^settings:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            settings_gender_callback,
            pattern=r"^settingsgender:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            settings_age_callback,
            pattern=r"^settingsage:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            settings_profile_gender_callback,
            pattern=r"^profilegender:"
        )
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

    # Dedicated stop-search button.
    # Must run before registration and normal message routers.
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^❌ Қатъ кардани ҷустуҷӯ$"),
            stop_search_button
        ),
        group=0
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
