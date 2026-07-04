import os
import sqlite3
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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

album_cache = {}


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


def make_admin_caption(user):
    username = f"@{user.username}" if user.username else "없음"
    name = user.full_name
    user_id = user.id

    return (
        "📩 이벤트 참여 신청\n\n"
        f"이름: {name}\n"
        f"아이디: {username}\n"
        f"고유ID: {user_id}\n"
        f"신청시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "캡처본 확인 후 처리해주세요."
    )


def make_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 승인", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton("❌ 거절", callback_data=f"reject:{user_id}"),
        ],
        [
            InlineKeyboardButton("🚫 차단", callback_data=f"block:{user_id}")
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎉 신사소통방 이벤트 참여봇입니다.\n\n"
        "아래 조건 중 하나를 충족한 캡처본을 보내주세요.\n\n"
        "✅ 당일 누적 채팅 캡처본(내정보 입력후 확인)\n"
        "✅ 당일 신사 제휴사 이용내역 캡처본\n\n"
        "📌 사진은 여러 장을 한 번에 보내도 됩니다.\n"
        "📌 관리자 확인 후 참여 완료 안내를 드립니다."
    )
    await update.message.reply_text(text)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"당신의 텔레그램 숫자 ID는:\n\n{update.effective_user.id}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_status = get_application(update.effective_user.id)

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


async def check_user_before_submit(message, user):
    if ADMIN_ID == 0:
        await message.reply_text("관리자 설정이 아직 완료되지 않았습니다.")
        return False

    current_status = get_application(user.id)

    if current_status == "approved":
        await message.reply_text("이미 이벤트 참여가 완료되었습니다.")
        return False

    if current_status == "blocked":
        await message.reply_text("신청이 제한된 상태입니다.")
        return False

    return True


async def handle_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not await check_user_before_submit(message, user):
        return

    save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음"
    )

    file_id = message.photo[-1].file_id

    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=file_id,
        caption=make_admin_caption(user),
        reply_markup=make_keyboard(user.id)
    )

    await message.reply_text(
        "📨 이벤트 참여 신청이 접수되었습니다.\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    )


async def process_album_group(context: ContextTypes.DEFAULT_TYPE, media_group_id: str):
    await asyncio.sleep(1.5)

    data = album_cache.pop(media_group_id, None)
    if not data:
        return

    user = data["user"]
    chat_id = data["chat_id"]
    photos = data["photos"]

    save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음"
    )

    media = []
    caption = make_admin_caption(user)

    for index, file_id in enumerate(photos):
        if index == 0:
            media.append(InputMediaPhoto(media=file_id, caption=caption))
        else:
            media.append(InputMediaPhoto(media=file_id))

    await context.bot.send_media_group(
        chat_id=ADMIN_ID,
        media=media
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"위 신청을 처리하세요.\n고유ID: {user.id}",
        reply_markup=make_keyboard(user.id)
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📨 이벤트 참여 신청이 접수되었습니다.\n"
            f"사진 {len(photos)}장이 관리자에게 전달되었습니다.\n"
            "관리자 확인 후 결과를 안내드리겠습니다."
        )
    )


async def handle_album_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not await check_user_before_submit(message, user):
        return

    media_group_id = message.media_group_id
    file_id = message.photo[-1].file_id

    if media_group_id not in album_cache:
        album_cache[media_group_id] = {
            "user": user,
            "chat_id": message.chat_id,
            "photos": []
        }

        asyncio.create_task(process_album_group(context, media_group_id))

    album_cache[media_group_id]["photos"].append(file_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.media_group_id:
        await handle_album_photo(update, context)
    else:
        await handle_single_photo(update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not await check_user_before_submit(message, user):
        return

    save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음"
    )

    await context.bot.send_document(
        chat_id=ADMIN_ID,
        document=message.document.file_id,
        caption=make_admin_caption(user),
        reply_markup=make_keyboard(user.id)
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

    await query.edit_message_text(
        text=f"처리결과: {result}\n고유ID: {user_id}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "캡처본 이미지를 보내주세요.\n\n"
        "✅ 당일 누적 채팅 300개 이상\n"
        "✅ 제휴사 3만원 이상 이용내역\n\n"
        "사진 여러 장을 한 번에 보내도 됩니다."
    )


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(handle_callback))

    print("이벤트 참여봇 실행 중")
    app.run_polling()


if __name__ == "__main__":
    main()
