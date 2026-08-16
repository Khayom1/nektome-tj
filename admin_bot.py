import os
import sqlite3
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("ADMIN_BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

DB_FILE = "nektome.db"

if not TOKEN:
    raise RuntimeError("ADMIN_BOT_TOKEN is not set")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set")


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("NektomeAdmin")


# ============================================================
# DATABASE
# ============================================================

conn = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

conn.row_factory = sqlite3.Row


def db_one(query, params=()):
    return conn.execute(
        query,
        params
    ).fetchone()


def db_all(query, params=()):
    return conn.execute(
        query,
        params
    ).fetchall()


def db_exec(query, params=()):
    with conn:
        return conn.execute(
            query,
            params
        )


# ============================================================
# SECURITY
# ============================================================

def is_admin(update):

    return (
        update.effective_user
        and str(update.effective_user.id) == str(ADMIN_ID)
    )


async def deny(update):

    if update.callback_query:
        await update.callback_query.answer(
            "⛔ Дастрасӣ манъ аст.",
            show_alert=True
        )
    elif update.message:
        await update.message.reply_text(
            "⛔ Дастрасӣ манъ аст."
        )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    await update.message.reply_text(
        "🛡 <b>Nektome TJ — Admin Panel</b>\n\n"
        "🚨 /reports — report-ҳои интизорӣ\n"
        "👤 /user ID — маълумоти корбар\n"
        "🔓 /unblock — кушодани блок\n"
        "📊 /stats — статистика",
        parse_mode="HTML"
    )


# ============================================================
# REPORTS
# ============================================================

async def reports(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    rows = db_all(
        """
        SELECT *
        FROM reports
        WHERE status='pending'
        ORDER BY id DESC
        LIMIT 20
        """
    )

    if not rows:

        await update.message.reply_text(
            "✅ Ҳоло report-и pending нест."
        )

        return

    for row in rows:

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🚫 BLOCK",
                    callback_data=f"block:{row['reported']}"
                ),
                InlineKeyboardButton(
                    "✅ REJECT",
                    callback_data=f"reject:{row['id']}"
                )
            ]
        ])

        text = (
            "🚨 <b>REPORT</b>\n\n"
            f"Report ID: <code>{row['id']}</code>\n"
            f"Reporter: <code>{row['reporter_nektome_id']}</code>\n"
            f"Reported: <code>{row['reported_nektome_id']}</code>\n"
            f"Time: {row['created_at']}\n"
            f"Status: {row['status']}"
        )

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )


# ============================================================
# BLOCK
# ============================================================

async def block_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        await deny(update)
        return

    query = update.callback_query

    await query.answer()

    target = query.data.replace(
        "block:",
        "",
        1
    )

    user = db_one(
        """
        SELECT *
        FROM users
        WHERE telegram_id=?
        """,
        (target,)
    )

    if not user:

        await query.edit_message_text(
            "❌ Корбар ёфт нашуд."
        )

        return

    db_exec(
        """
        INSERT OR IGNORE INTO blocks(
            blocker,
            blocked
        )
        VALUES (?, ?)
        """,
        (
            str(ADMIN_ID),
            str(target)
        )
    )

    await query.edit_message_text(
        "🚫 <b>Корбар блок шуд.</b>\n\n"
        f"Nektome ID: <code>{user['nektome_id']}</code>",
        parse_mode="HTML"
    )

    # Try notifying the blocked user.
    try:

        await context.bot.send_message(
            chat_id=int(target),
            text=(
                "🚫 <b>Дастрасӣ маҳдуд шуд.</b>\n\n"
                "Шумо аз истифодаи Nektome TJ блок шудаед.\n\n"
                "Агар фикр мекунед, ки ин қарор хато аст, "
                "ба техподдержка муроҷиат кунед."
            ),
            parse_mode="HTML"
        )

    except Exception as e:

        logger.warning(
            "Could not notify blocked user: %s",
            e
        )

    # Mark related reports as resolved.
    db_exec(
        """
        UPDATE reports
        SET status='blocked'
        WHERE reported=?
          AND status='pending'
        """,
        (target,)
    )


# ============================================================
# REJECT
# ============================================================

async def reject_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        await deny(update)
        return

    query = update.callback_query

    await query.answer()

    report_id = query.data.replace(
        "reject:",
        "",
        1
    )

    row = db_one(
        """
        SELECT *
        FROM reports
        WHERE id=?
        """,
        (report_id,)
    )

    if not row:

        await query.edit_message_text(
            "❌ Report ёфт нашуд."
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

    await query.edit_message_text(
        "✅ <b>Report рад карда шуд.</b>\n\n"
        f"Report ID: <code>{report_id}</code>",
        parse_mode="HTML"
    )


# ============================================================
# UNBLOCK
# ============================================================

async def unblock_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        await deny(update)
        return

    context.user_data["waiting_unblock_id"] = True

    await update.message.reply_text(
        "🔓 <b>Unblock</b>\n\n"
        "Nektome ID-и корбарро фиристед.\n\n"
        "Масалан:\n"
        "<code>123456</code>",
        parse_mode="HTML"
    )


async def unblock_receive(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        return

    if not context.user_data.get(
        "waiting_unblock_id"
    ):
        return

    nektome_id = update.message.text.strip()

    if not nektome_id.isdigit():

        await update.message.reply_text(
            "❗ ID бояд танҳо рақам бошад."
        )

        return

    user = db_one(
        """
        SELECT *
        FROM users
        WHERE nektome_id=?
        """,
        (nektome_id,)
    )

    if not user:

        await update.message.reply_text(
            "❌ Ин Nektome ID вуҷуд надорад."
        )

        context.user_data.pop(
            "waiting_unblock_id",
            None
        )

        return

    db_exec(
        """
        DELETE FROM blocks
        WHERE blocker=?
          AND blocked=?
        """,
        (
            str(ADMIN_ID),
            str(user["telegram_id"])
        )
    )

    context.user_data.pop(
        "waiting_unblock_id",
        None
    )

    await update.message.reply_text(
        "🔓 <b>Корбар unblock шуд.</b>\n\n"
        f"🆔 Nektome ID: <code>{user['nektome_id']}</code>",
        parse_mode="HTML"
    )

    try:

        await context.bot.send_message(
            chat_id=int(user["telegram_id"]),
            text=(
                "✅ <b>Дастрасии шумо барқарор шуд.</b>\n\n"
                "Шумо метавонед дубора Nektome TJ-ро истифода баред."
            ),
            parse_mode="HTML"
        )

    except Exception as e:

        logger.warning(
            "Could not notify user: %s",
            e
        )


# ============================================================
# USER LOOKUP
# ============================================================

async def user_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:

        await update.message.reply_text(
            "Истифода:\n"
            "/user 123456"
        )

        return

    nektome_id = context.args[0]

    user = db_one(
        """
        SELECT *
        FROM users
        WHERE nektome_id=?
        """,
        (nektome_id,)
    )

    if not user:

        await update.message.reply_text(
            "❌ Корбар ёфт нашуд."
        )

        return

    blocked = db_one(
        """
        SELECT 1
        FROM blocks
        WHERE blocker=?
          AND blocked=?
        """,
        (
            str(ADMIN_ID),
            str(user["telegram_id"])
        )
    )

    status = "🚫 BLOCKED" if blocked else "✅ ACTIVE"

    await update.message.reply_text(
        "👤 <b>USER</b>\n\n"
        f"🆔 Nektome ID: <code>{user['nektome_id']}</code>\n"
        f"Telegram ID: <code>{user['telegram_id']}</code>\n"
        f"🎂 Age: {user['age']}\n"
        f"🚻 Gender: {user['gender']}\n"
        f"📌 Status: {status}\n"
        f"📅 Created: {user['created_at']}",
        parse_mode="HTML"
    )


# ============================================================
# STATS
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        await deny(update)
        return

    users = db_one(
        "SELECT COUNT(*) AS n FROM users"
    )["n"]

    reports_count = db_one(
        "SELECT COUNT(*) AS n FROM reports"
    )["n"]

    pending = db_one(
        """
        SELECT COUNT(*) AS n
        FROM reports
        WHERE status='pending'
        """
    )["n"]

    blocked = db_one(
        """
        SELECT COUNT(*) AS n
        FROM blocks
        WHERE blocker=?
        """,
        (str(ADMIN_ID),)
    )["n"]

    await update.message.reply_text(
        "📊 <b>Nektome TJ Stats</b>\n\n"
        f"👤 Users: {users}\n"
        f"🚨 Reports: {reports_count}\n"
        f"⏳ Pending: {pending}\n"
        f"🚫 Blocked: {blocked}",
        parse_mode="HTML"
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        await deny(update)
        return

    data = update.callback_query.data

    if data.startswith("block:"):

        await block_callback(
            update,
            context
        )

        return

    if data.startswith("reject:"):

        await reject_callback(
            update,
            context
        )

        return


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context
):

    logger.exception(
        "Admin bot error:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 40)
    print(" Nektome TJ — ADMIN BOT")
    print("=" * 40)

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "reports",
            reports
        )
    )

    app.add_handler(
        CommandHandler(
            "unblock",
            unblock_start
        )
    )

    app.add_handler(
        CommandHandler(
            "user",
            user_command
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_router,
            pattern=r"^(block:|reject:)"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            unblock_receive
        )
    )

    app.add_error_handler(
        error_handler
    )

    print("🛡 ADMIN BOT RUNNING")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
