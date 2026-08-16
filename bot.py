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

    data = query.data.replace("findage:", "")

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

    await update.message.reply_text(
        "🚻 Ҷинси худро интихоб кунед:",
        reply_markup=gender_keyboard()
    )


# ============================================================
# REGISTRATION GENDER
# ============================================================

async def registration_gender(update, context):

    query = update.callback_query
    await query.answer()

    gender = query.data.replace("findgender:", "")

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
        "✅ <b>Профил сохта шуд!</b>\n\n"
        f"🆔 ID-и шумо: <code>{nektome_id}</code>\n\n"
        "Ин ID-ро метавонед ба дигарон диҳед.",
        parse_mode="HTML"
    )

    await context.bot.send_message(
        tg_id,
        "Менюи асосӣ:",
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
            "Каме интизор шавед..."
        )
        return

    context.user_data["find_gender"] = None
    context.user_data["find_age"] = None

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

    await query.edit_message_text(
        "🔎 <b>Ҳамсӯҳбат ҷустуҷӯ мешавад...</b>\n\n"
        "Лутфан каме интизор шавед.",
        parse_mode="HTML"
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

        if not can_match(
            user_id,
            my_pref,
            candidate,
            other_pref
        ):
            continue

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

    text = update.message.text or ""

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

    if text == "❌ Қатъ кардани ҷустуҷӯ":
        await stop_search(update, context)
        return

    if user_id in waiting_users:

        await update.message.reply_text(
            "🔎 Ҳоло ҳамсӯҳбат ҷустуҷӯ мешавад...\n\n"
            "Лутфан интизор шавед."
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
