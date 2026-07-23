import os
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# 신사 이벤트 참여봇 V2
# - 기존 applications 테이블 및 신청 데이터 유지
# - 관리자 패널에서 문구/이모지/이벤트 상태 수정
# - 사진 여러 장, GIF, 이미지 문서 지원
# - HTML 및 텔레그램 커스텀 이모지 지원
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_FILE = os.getenv("DB_FILE", "event_bot.db")
KST = timezone(timedelta(hours=9))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

album_cache: dict[str, dict] = {}
album_tasks: dict[str, asyncio.Task] = {}

DEFAULT_SETTINGS = {
    "event_enabled": "1",
    "event_name": "신사소통방 이벤트",
    "start_text": (
        "{emoji_party} <b>{event_name} 참여봇</b>\n\n"
        "{emoji_notice} 아래 조건 중 하나를 충족한 캡처본을 보내주세요.\n\n"
        "{emoji_chat} 당일 누적 채팅 캡처본\n"
        "{emoji_money} 당일 신사 제휴사 이용내역 캡처본\n\n"
        "{emoji_photo} 사진은 여러 장을 한 번에 보내도 됩니다.\n"
        "{emoji_check} 관리자 확인 후 참여 완료 안내를 드립니다."
    ),
    "guide_text": (
        "{emoji_photo} <b>캡처본 이미지를 보내주세요.</b>\n\n"
        "{emoji_chat} 당일 누적 채팅 300개 이상\n"
        "{emoji_money} 제휴사 3만원 이상 이용내역\n\n"
        "사진 여러 장, GIF, 이미지 파일을 보낼 수 있습니다."
    ),
    "closed_text": (
        "{emoji_stop} <b>현재 이벤트 참여가 종료되었습니다.</b>\n\n"
        "다음 이벤트가 시작되면 다시 이용해주세요."
    ),
    "received_text": (
        "{emoji_send} <b>이벤트 참여 신청이 접수되었습니다.</b>\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    ),
    "received_album_text": (
        "{emoji_send} <b>이벤트 참여 신청이 접수되었습니다.</b>\n"
        "사진 <b>{count}장</b>이 관리자에게 전달되었습니다.\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    ),
    "approved_text": (
        "{emoji_approve} <b>이벤트 참여가 완료되었습니다.</b>\n\n"
        "관리자 승인이 정상적으로 처리되었습니다."
    ),
    "rejected_text": (
        "{emoji_reject} <b>이벤트 참여가 반려되었습니다.</b>\n\n"
        "참여 조건을 확인한 뒤 다시 제출해주세요."
    ),
    "blocked_text": (
        "{emoji_block} <b>이벤트 신청이 제한되었습니다.</b>\n\n"
        "자세한 내용은 관리자에게 문의해주세요."
    ),
    "pending_text": (
        "{emoji_wait} 이미 신청이 접수되어 관리자 확인 대기 중입니다."
    ),
    "already_approved_text": (
        "{emoji_approve} 이미 이벤트 참여가 완료되었습니다."
    ),
    "admin_caption": (
        "{emoji_mail} <b>이벤트 참여 신청</b>\n\n"
        "{emoji_user} <b>이름</b> : {name}\n"
        "{emoji_id} <b>아이디</b> : {username}\n"
        "{emoji_key} <b>고유 ID</b> : <code>{user_id}</code>\n"
        "{emoji_time} <b>신청시간</b> : {created_at}\n\n"
        "{emoji_photo} 캡처본 확인 후 처리해주세요."
    ),

    # 일반 이모지가 기본값입니다.
    # 관리자 패널에서 아래 값을 텔레그램 커스텀 이모지 HTML로 교체할 수 있습니다.
    # 예: <tg-emoji emoji-id="5368324170671202286">⭐</tg-emoji>
    "emoji_party": "🎉",
    "emoji_notice": "📢",
    "emoji_chat": "💬",
    "emoji_money": "💸",
    "emoji_photo": "📸",
    "emoji_check": "✅",
    "emoji_stop": "🔴",
    "emoji_send": "📨",
    "emoji_approve": "✅",
    "emoji_reject": "❌",
    "emoji_block": "🚫",
    "emoji_wait": "⏳",
    "emoji_mail": "📩",
    "emoji_user": "👤",
    "emoji_id": "🔗",
    "emoji_key": "🆔",
    "emoji_time": "🕒",
    "emoji_chart": "📊",
    "emoji_settings": "⚙️",
    "emoji_back": "◀️",
}


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                status TEXT,
                created_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


def get_setting(key: str, default: str = "") -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def get_all_settings() -> dict[str, str]:
    data = dict(DEFAULT_SETTINGS)
    with db_connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        data[row["key"]] = row["value"]
    return data


def render_setting(key: str, **kwargs) -> str:
    settings = get_all_settings()
    template = settings.get(key, DEFAULT_SETTINGS.get(key, ""))

    values = dict(settings)
    values.update({
        "event_name": escape(settings.get("event_name", "이벤트")),
        **kwargs,
    })

    try:
        return template.format_map(SafeFormatDict(values))
    except Exception:
        logger.exception("설정 문구 렌더링 실패: %s", key)
        return template


class SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def get_application(user_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def save_application(user_id: int, name: str, username: str) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO applications (user_id, name, username, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username,
                status = 'pending',
                created_at = excluded.created_at
            """,
            (user_id, name, username, now_kst()),
        )


def update_status(user_id: int, status: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE applications SET status = ? WHERE user_id = ?",
            (status, user_id),
        )


def delete_application(user_id: int) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM applications WHERE user_id = ?", (user_id,))


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def event_enabled() -> bool:
    return get_setting("event_enabled", "1") == "1"


def admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 승인", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton("❌ 거절", callback_data=f"reject:{user_id}"),
        ],
        [
            InlineKeyboardButton("🚫 차단", callback_data=f"block:{user_id}"),
            InlineKeyboardButton("🗑 신청삭제", callback_data=f"delete:{user_id}"),
        ],
    ])


def main_menu_keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📋 내 신청 상태", callback_data="user:status"),
            InlineKeyboardButton("📸 참여 방법", callback_data="user:guide"),
        ]
    ]
    if admin:
        rows.append([
            InlineKeyboardButton("⚙️ 관리자 메뉴", callback_data="admin:home")
        ])
    return InlineKeyboardMarkup(rows)


def admin_home_keyboard() -> InlineKeyboardMarkup:
    state_text = "🔴 이벤트 종료" if event_enabled() else "🟢 이벤트 시작"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(state_text, callback_data="admin:toggle"),
            InlineKeyboardButton("📊 통계", callback_data="admin:stats"),
        ],
        [
            InlineKeyboardButton("📝 문구 관리", callback_data="admin:texts"),
            InlineKeyboardButton("✨ 이모지 관리", callback_data="admin:emojis"),
        ],
        [
            InlineKeyboardButton("📋 승인 대기", callback_data="admin:pending"),
            InlineKeyboardButton("🔍 회원 검색", callback_data="admin:search"),
        ],
        [
            InlineKeyboardButton("🏷 이벤트명 수정", callback_data="edit:event_name"),
        ],
        [
            InlineKeyboardButton("◀️ 닫기", callback_data="admin:close"),
        ],
    ])


def text_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏠 시작 안내", callback_data="edit:start_text"),
            InlineKeyboardButton("📸 참여 안내", callback_data="edit:guide_text"),
        ],
        [
            InlineKeyboardButton("🔴 종료 안내", callback_data="edit:closed_text"),
            InlineKeyboardButton("📨 접수 안내", callback_data="edit:received_text"),
        ],
        [
            InlineKeyboardButton("✅ 승인 문구", callback_data="edit:approved_text"),
            InlineKeyboardButton("❌ 거절 문구", callback_data="edit:rejected_text"),
        ],
        [
            InlineKeyboardButton("🚫 차단 문구", callback_data="edit:blocked_text"),
            InlineKeyboardButton("⏳ 대기 문구", callback_data="edit:pending_text"),
        ],
        [
            InlineKeyboardButton("📩 관리자 신청 카드", callback_data="edit:admin_caption"),
        ],
        [
            InlineKeyboardButton("◀️ 관리자 메뉴", callback_data="admin:home"),
        ],
    ])


EMOJI_SETTING_LABELS = {
    "emoji_party": "이벤트",
    "emoji_notice": "공지",
    "emoji_chat": "채팅",
    "emoji_money": "제휴",
    "emoji_photo": "사진",
    "emoji_check": "확인",
    "emoji_stop": "종료",
    "emoji_send": "접수",
    "emoji_approve": "승인",
    "emoji_reject": "거절",
    "emoji_block": "차단",
    "emoji_wait": "대기",
    "emoji_mail": "신청",
    "emoji_user": "회원",
    "emoji_id": "아이디",
    "emoji_key": "고유 ID",
    "emoji_time": "시간",
    "emoji_chart": "통계",
    "emoji_settings": "설정",
}


def emoji_settings_keyboard() -> InlineKeyboardMarkup:
    keys = list(EMOJI_SETTING_LABELS.items())
    rows = []
    for idx in range(0, len(keys), 2):
        row = []
        for key, label in keys[idx:idx + 2]:
            row.append(InlineKeyboardButton(label, callback_data=f"edit:{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("♻️ 기본 이모지 복원", callback_data="admin:emoji_reset")])
    rows.append([InlineKeyboardButton("◀️ 관리자 메뉴", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)


async def safe_send_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> bool:
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return True
    except (Forbidden, BadRequest) as exc:
        logger.warning("사용자 알림 전송 실패 user_id=%s: %s", user_id, exc)
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not event_enabled() and not is_admin(user.id):
        text = render_setting("closed_text")
    else:
        text = render_setting("start_text")

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin(user.id)),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("관리자만 사용할 수 있습니다.")
        return

    await update.effective_message.reply_text(
        "⚙️ <b>관리자 설정</b>\n\n"
        "버튼을 눌러 이벤트 상태와 안내 문구를 수정할 수 있습니다.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"🆔 당신의 텔레그램 숫자 ID\n\n<code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_status(update.effective_message, update.effective_user.id)


async def send_status(message, user_id: int) -> None:
    row = get_application(user_id)
    if not row:
        text = "📭 아직 이벤트 신청 내역이 없습니다."
    else:
        status_map = {
            "pending": render_setting("pending_text"),
            "approved": render_setting("approved_text"),
            "rejected": render_setting("rejected_text"),
            "blocked": render_setting("blocked_text"),
        }
        text = status_map.get(row["status"], "알 수 없는 상태입니다.")

    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("관리자만 확인할 수 있습니다.")
        return
    await send_stats(update.effective_message)


async def send_stats(message) -> None:
    date = today_kst()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM applications
            WHERE created_at LIKE ?
            GROUP BY status
            """,
            (f"{date}%",),
        ).fetchall()
        total_all = conn.execute("SELECT COUNT(*) AS cnt FROM applications").fetchone()["cnt"]

    counts = {"pending": 0, "approved": 0, "rejected": 0, "blocked": 0}
    for row in rows:
        counts[row["status"]] = row["cnt"]

    total_today = sum(counts.values())
    text = (
        "📊 <b>이벤트 참여 통계</b>\n\n"
        f"📅 오늘 날짜 : <b>{date}</b>\n"
        f"📨 오늘 신청 : <b>{total_today}명</b>\n"
        f"⏳ 승인 대기 : <b>{counts['pending']}명</b>\n"
        f"✅ 승인 완료 : <b>{counts['approved']}명</b>\n"
        f"❌ 거절 : <b>{counts['rejected']}명</b>\n"
        f"🚫 차단 : <b>{counts['blocked']}명</b>\n\n"
        f"🗂 전체 누적 : <b>{total_all}명</b>"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def list_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("관리자만 확인할 수 있습니다.")
        return
    await send_pending(update.effective_message)


async def send_pending(message) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT name, username, user_id, created_at
            FROM applications
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()

    if not rows:
        await message.reply_text("📭 승인 대기 중인 신청자가 없습니다.")
        return

    lines = ["📋 <b>승인 대기 목록</b>\n"]
    for row in rows:
        username = escape(row["username"] or "없음")
        lines.append(
            f"👤 {escape(row['name'] or '이름 없음')}\n"
            f"🔗 {username}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"🕒 {row['created_at']}\n"
            "──────────────"
        )

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def check_user_before_submit(message, user) -> bool:
    if ADMIN_ID == 0:
        await message.reply_text("관리자 설정이 아직 완료되지 않았습니다.")
        return False

    if not event_enabled() and not is_admin(user.id):
        await message.reply_text(
            render_setting("closed_text"),
            parse_mode=ParseMode.HTML,
        )
        return False

    row = get_application(user.id)
    status = row["status"] if row else None

    if status == "approved":
        await message.reply_text(
            render_setting("already_approved_text"),
            parse_mode=ParseMode.HTML,
        )
        return False

    if status == "pending":
        await message.reply_text(
            render_setting("pending_text"),
            parse_mode=ParseMode.HTML,
        )
        return False

    if status == "blocked":
        await message.reply_text(
            render_setting("blocked_text"),
            parse_mode=ParseMode.HTML,
        )
        return False

    return True


def make_admin_caption(user) -> str:
    username = f"@{escape(user.username)}" if user.username else "없음"
    return render_setting(
        "admin_caption",
        name=escape(user.full_name or "이름 없음"),
        username=username,
        user_id=user.id,
        created_at=now_kst(),
    )


async def handle_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not await check_user_before_submit(message, user):
        return

    save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
    )

    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=make_admin_caption(user),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(user.id),
    )

    await message.reply_text(
        render_setting("received_text"),
        parse_mode=ParseMode.HTML,
    )


async def process_album_group(context: ContextTypes.DEFAULT_TYPE, media_group_id: str) -> None:
    await asyncio.sleep(1.8)

    data = album_cache.pop(media_group_id, None)
    album_tasks.pop(media_group_id, None)

    if not data:
        return

    user = data["user"]
    chat_id = data["chat_id"]
    photos = data["photos"][:10]

    row = get_application(user.id)
    if row and row["status"] in {"pending", "approved", "blocked"}:
        return

    save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
    )

    media = []
    caption = make_admin_caption(user)

    for index, file_id in enumerate(photos):
        media.append(
            InputMediaPhoto(
                media=file_id,
                caption=caption if index == 0 else None,
                parse_mode=ParseMode.HTML if index == 0 else None,
            )
        )

    await context.bot.send_media_group(chat_id=ADMIN_ID, media=media)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📎 <b>사진 묶음 신청</b>\n\n"
            f"사진 수 : <b>{len(photos)}장</b>\n"
            f"고유 ID : <code>{user.id}</code>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(user.id),
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=render_setting("received_album_text", count=len(photos)),
        parse_mode=ParseMode.HTML,
    )


async def handle_album_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    media_group_id = message.media_group_id

    # 앨범 첫 사진에서만 상태 검사
    if media_group_id not in album_cache:
        if not await check_user_before_submit(message, user):
            return

        album_cache[media_group_id] = {
            "user": user,
            "chat_id": message.chat_id,
            "photos": [],
        }
        album_tasks[media_group_id] = asyncio.create_task(
            process_album_group(context, media_group_id)
        )

    album_cache[media_group_id]["photos"].append(message.photo[-1].file_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message.media_group_id:
        await handle_album_photo(update, context)
    else:
        await handle_single_photo(update, context)


async def handle_animation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not await check_user_before_submit(message, user):
        return

    save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
    )

    await context.bot.send_animation(
        chat_id=ADMIN_ID,
        animation=message.animation.file_id,
        caption=make_admin_caption(user),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(user.id),
    )

    await message.reply_text(
        render_setting("received_text"),
        parse_mode=ParseMode.HTML,
    )


async def handle_image_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not await check_user_before_submit(message, user):
        return

    save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
    )

    await context.bot.send_document(
        chat_id=ADMIN_ID,
        document=message.document.file_id,
        caption=make_admin_caption(user),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(user.id),
    )

    await message.reply_text(
        render_setting("received_text"),
        parse_mode=ParseMode.HTML,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if is_admin(user.id) and context.user_data.get("edit_key"):
        await handle_admin_setting_input(update, context)
        return

    if is_admin(user.id) and context.user_data.get("search_mode"):
        await handle_admin_search_input(update, context)
        return

    if not event_enabled() and not is_admin(user.id):
        await message.reply_text(
            render_setting("closed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        render_setting("guide_text"),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin(user.id)),
    )


async def handle_admin_setting_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = context.user_data.pop("edit_key", None)
    if not key:
        return

    value = update.effective_message.text.strip()
    if value == "/cancel":
        await update.effective_message.reply_text("수정을 취소했습니다.")
        return

    if not value:
        await update.effective_message.reply_text("빈 내용은 저장할 수 없습니다.")
        return

    set_setting(key, value)

    label = EMOJI_SETTING_LABELS.get(key, key)
    await update.effective_message.reply_text(
        f"✅ <b>{escape(label)}</b> 설정을 저장했습니다.\n\n"
        f"<b>적용 미리보기</b>\n{render_setting(key) if key in DEFAULT_SETTINGS else escape(value)}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def handle_admin_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("search_mode", None)
    keyword = update.effective_message.text.strip()

    with db_connect() as conn:
        if keyword.isdigit():
            rows = conn.execute(
                """
                SELECT * FROM applications
                WHERE CAST(user_id AS TEXT) LIKE ?
                ORDER BY created_at DESC LIMIT 20
                """,
                (f"%{keyword}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM applications
                WHERE name LIKE ? OR username LIKE ?
                ORDER BY created_at DESC LIMIT 20
                """,
                (f"%{keyword}%", f"%{keyword}%"),
            ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "검색 결과가 없습니다.",
            reply_markup=admin_home_keyboard(),
        )
        return

    status_names = {
        "pending": "⏳ 대기",
        "approved": "✅ 승인",
        "rejected": "❌ 거절",
        "blocked": "🚫 차단",
    }

    lines = ["🔍 <b>회원 검색 결과</b>\n"]
    for row in rows:
        lines.append(
            f"👤 {escape(row['name'] or '이름 없음')}\n"
            f"🔗 {escape(row['username'] or '없음')}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"📌 {status_names.get(row['status'], row['status'])}\n"
            f"🕒 {row['created_at']}\n"
            "──────────────"
        )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""

    if data.startswith("user:"):
        action = data.split(":", 1)[1]
        if action == "status":
            await send_status(query.message, query.from_user.id)
        elif action == "guide":
            await query.message.reply_text(
                render_setting("guide_text"),
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith(("admin:", "edit:", "approve:", "reject:", "block:", "delete:")):
        if not is_admin(query.from_user.id):
            await query.answer("관리자만 사용할 수 있습니다.", show_alert=True)
            return

    if data == "admin:home":
        await query.edit_message_text(
            "⚙️ <b>관리자 설정</b>\n\n"
            "수정할 메뉴를 선택해주세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_keyboard(),
        )
        return

    if data == "admin:close":
        await query.edit_message_text("관리자 메뉴를 닫았습니다.")
        return

    if data == "admin:toggle":
        new_value = "0" if event_enabled() else "1"
        set_setting("event_enabled", new_value)
        state = "시작" if new_value == "1" else "종료"
        await query.edit_message_text(
            f"{'🟢' if new_value == '1' else '🔴'} 이벤트를 <b>{state}</b> 상태로 변경했습니다.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_keyboard(),
        )
        return

    if data == "admin:stats":
        await send_stats(query.message)
        return

    if data == "admin:pending":
        await send_pending(query.message)
        return

    if data == "admin:texts":
        await query.edit_message_text(
            "📝 <b>문구 관리</b>\n\n수정할 문구를 선택해주세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=text_settings_keyboard(),
        )
        return

    if data == "admin:emojis":
        await query.edit_message_text(
            "✨ <b>이모지 관리</b>\n\n"
            "일반 이모지 또는 커스텀 이모지 HTML을 입력할 수 있습니다.\n\n"
            "<code>&lt;tg-emoji emoji-id=\"이모지ID\"&gt;⭐&lt;/tg-emoji&gt;</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=emoji_settings_keyboard(),
        )
        return

    if data == "admin:emoji_reset":
        for key in EMOJI_SETTING_LABELS:
            set_setting(key, DEFAULT_SETTINGS[key])
        await query.edit_message_text(
            "♻️ 기본 이모지로 복원했습니다.",
            reply_markup=admin_home_keyboard(),
        )
        return

    if data == "admin:search":
        context.user_data["search_mode"] = True
        await query.edit_message_text(
            "🔍 <b>회원 검색</b>\n\n"
            "이름, @아이디 또는 숫자 ID를 입력해주세요.\n"
            "다음 메시지 한 개를 검색어로 사용합니다.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("edit:"):
        key = data.split(":", 1)[1]
        if key not in DEFAULT_SETTINGS:
            await query.answer("수정할 수 없는 항목입니다.", show_alert=True)
            return

        context.user_data["edit_key"] = key
        current = get_setting(key, DEFAULT_SETTINGS[key])

        extra = ""
        if key in EMOJI_SETTING_LABELS:
            extra = (
                "\n\n커스텀 이모지 사용 예시:\n"
                "<code>&lt;tg-emoji emoji-id=\"이모지ID\"&gt;⭐&lt;/tg-emoji&gt;</code>"
            )
        else:
            extra = (
                "\n\n사용 가능한 치환값:\n"
                "<code>{event_name}</code>, <code>{name}</code>, "
                "<code>{username}</code>, <code>{user_id}</code>, "
                "<code>{created_at}</code>, <code>{count}</code>\n"
                "이모지는 <code>{emoji_party}</code> 같은 형식으로 사용할 수 있습니다."
            )

        await query.edit_message_text(
            "✏️ <b>새 내용을 입력해주세요.</b>\n\n"
            f"<b>현재 설정</b>\n<pre>{escape(current)}</pre>"
            f"{extra}\n\n취소하려면 <code>/cancel</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if ":" not in data:
        return

    action, user_id_text = data.split(":", 1)
    if not user_id_text.isdigit():
        return
    user_id = int(user_id_text)

    row = get_application(user_id)
    if action != "delete" and not row:
        await query.answer("신청 내역을 찾을 수 없습니다.", show_alert=True)
        return

    if action == "approve":
        update_status(user_id, "approved")
        await safe_send_user(context, user_id, render_setting("approved_text"))
        result = "✅ 승인 완료"

    elif action == "reject":
        update_status(user_id, "rejected")
        await safe_send_user(context, user_id, render_setting("rejected_text"))
        result = "❌ 거절 완료"

    elif action == "block":
        update_status(user_id, "blocked")
        await safe_send_user(context, user_id, render_setting("blocked_text"))
        result = "🚫 차단 완료"

    elif action == "delete":
        delete_application(user_id)
        result = "🗑 신청 내역 삭제"
    else:
        return

    result_text = (
        "\n\n━━━━━━━━━━━━━━\n"
        f"<b>{result}</b>\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🕒 {now_kst()}\n"
        "━━━━━━━━━━━━━━"
    )

    try:
        if query.message.caption is not None:
            await query.edit_message_caption(
                caption=f"{query.message.caption}{result_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        elif query.message.text is not None:
            await query.edit_message_text(
                text=f"{query.message.text}{result_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(result_text, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        logger.warning("처리 메시지 수정 실패: %s", exc)
        await query.message.reply_text(result_text, parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("업데이트 처리 중 오류", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")
    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID 환경변수가 설정되지 않았습니다.")

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("list", list_pending_command))

    # 앨범 사진은 PHOTO 핸들러에서 묶어서 처리
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.ANIMATION, handle_animation))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_image_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("신사 이벤트 참여봇 V2 실행 중")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
