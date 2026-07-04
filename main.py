import os
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_FILE = "event_bot.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            status TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_application(user_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("SELECT status FROM applications WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    conn.close()
    return row[0] if row else None


def save_application(user_id, name, username):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR REPLACE INTO applications
        (user_id, name, username, status, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        user_id,
        name,
        username,
        "pending",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()
    conn.close()


def update_status(user_id, status):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        "UPDATE applications SET status = ? WHERE user_id = ?",
        (status, user_id)
    )

    conn.commit()
    conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎉 신사소통방 이벤트 참여봇입니다.\n\n"
        "아래 조건 중 하나를 충족한 캡처본을 보내주세요.\n\n"
        "✅ 당일 누적 채팅 500개 이상\n"
        "✅ 제휴사 5만원 이상 이용내역\n\n"
        "📌 캡처본을 보내주시면 관리자 확인 후 안내드립니다."
    )
    await update.message.reply_text(text)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"당신의 텔레그램 숫자 ID는:\n\n{update.effective_user.id}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_status = get_application(user_id)

    if not current_status:
        await update.message.reply_text("아직 이벤트 신청 내역이 없습니다.")
        return

    status_text = {
        "pending": "⏳ 관리자 확인 대기 중입니다.",
        "approved": "✅ 이벤트 참여가 완료되었습니다.",
        "rejected": "❌ 반려되었습니다. 조건 확인 후 다시 제출해주세요.",
        "blocked": "🚫 신청이 제한된 상태입니다."
    }.get(current_status, "알 수 없는 상태입니다.")

    await update.message.reply_text(status_text)


async def handle_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if ADMIN_ID == 0:
        await message.reply_text("관리자 설정이 아직 완료되지 않았습니다.")
        return

    current_status = get_application(user.id)

    if current_status == "approved":
        await message.reply_text("이미 이벤트 참여가 완료되었습니다.")
        return

    if current_status == "blocked":
        await message.reply_text("신청이 제한된 상태입니다.")
        return

    username = f"@{user.username}" if user.username else "없음"
    name = user.full_name
    user_id = user.id

    save_application(user_id, name, username)

    caption = (
        "📩 이벤트 참여 신청\n\n"
        f"이름: {name}\n"
        f"아이디: {username}\n"
        f"고유ID: {user_id}\n"
        f"신청시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "캡처본 확인 후 처리해주세요."
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ 승인", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton("❌ 거절", callback_data=f"reject:{user_id}"),
        ],
        [
            InlineKeyboardButton("🚫 차단", callback_data=f"block:{user_id}")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if message.photo:
        file_id = message.photo[-1].file_id
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=file_id,
            caption=caption,
            reply_markup=reply_markup
        )

    elif message.document:
        file_id = message.document.file_id
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=file_id,
            caption=caption,
            reply_markup=reply_markup
        )

    await message.reply_text(
        "📨 이벤트 참여 신청이 접수되었습니다.\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("관리자만 처리할 수 있습니다.", show_alert=True)
        return

    action, user_id_text = query.data.split(":")
    user_id = int(user_id_text)

    if action == "approve":
        update_status(user_id, "approved")
        await context.bot.send_message(
            chat_id=user_id,
            text="✅ 이벤트 참여가 완료되었습니다."
        )
        result = "✅ 승인 완료"

    elif action == "reject":
        update_status(user_id, "rejected")
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ 이벤트 참여가 반려되었습니다.\n조건 확인 후 다시 제출해주세요."
        )
        result = "❌ 거절 완료"

    elif action == "block":
        update_status(user_id, "blocked")
        await context.bot.send_message(
            chat_id=user_id,
            text="🚫 이벤트 신청이 제한되었습니다."
        )
        result = "🚫 차단 완료"

    else:
        return

    old_caption = query.message.caption or ""
    await query.edit_message_caption(
        caption=old_caption + f"\n\n처리결과: {result}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "캡처본 이미지를 보내주세요.\n\n"
        "✅ 당일 누적 채팅 300개 이상\n"
        "✅ 제휴사 3만원 이상 이용내역"
    )


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(MessageHandler(filters.PHOTO, handle_submit))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_submit))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(handle_callback))

    print("이벤트 참여봇 실행 중")
    app.run_polling()


if __name__ == "__main__":
    main()
