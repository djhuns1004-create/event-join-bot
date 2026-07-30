
import os
import io
import csv
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Optional, Iterable

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# 신사 이벤트 참여봇 V4
# - 신청마다 고유 신청번호 및 날짜별 이력 저장
# - 사진/앨범/GIF/이미지파일 관리자 알림
# - 관리자 승인/거절/차단 및 처리자/처리시간 기록
# - 날짜별 내역, 회원 ID별 이력, CSV 다운로드
# - 기존 V2 applications 데이터 자동 이전
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
DB_FILE = os.getenv("DB_FILE", "/data/event_bot.db").strip()
KST = timezone(timedelta(hours=9))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sinsa_event_bot_v4")

album_cache: dict[str, dict] = {}
album_tasks: dict[str, asyncio.Task] = {}

STATUS_NAMES = {
    "pending": "⏳ 승인 대기",
    "approved": "✅ 승인 완료",
    "rejected": "❌ 거절",
    "blocked": "🚫 차단",
    "notify_failed": "⚠️ 관리자 전달 실패",
}

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
        "{emoji_send} <b>이벤트 참여 신청이 접수되었습니다.</b>\n\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    ),

    "received_album_text": (
        "{emoji_send} <b>이벤트 참여 신청이 접수되었습니다.</b>\n\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "사진 : <b>{count}장</b>\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    ),

    "approved_text": (
        "{emoji_approve} <b>이벤트 참여가 승인되었습니다.</b>\n\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "처리시간 : {processed_at}"
    ),

    "rejected_text": (
        "{emoji_reject} <b>이벤트 참여가 반려되었습니다.</b>\n\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "참여 조건을 확인한 뒤 다시 제출해주세요."
    ),

    "blocked_text": (
        "{emoji_block} <b>이벤트 신청이 제한되었습니다.</b>\n\n"
        "자세한 내용은 관리자에게 문의해주세요."
    ),

    "pending_text": (
        "{emoji_wait} 이미 오늘 신청이 접수되어 관리자 확인 대기 중입니다."
    ),

    "already_approved_text": (
        "{emoji_approve} 오늘 이벤트 참여가 이미 완료되었습니다."
    ),

    "admin_send_failed_text": (
        "⚠️ <b>신청 접수 중 오류가 발생했습니다.</b>\n\n"
        "담당자에게 인증자료를 전달하지 못했습니다.\n"
        "잠시 후 다시 제출해주세요."
    ),

    "admin_caption": (
        "{emoji_mail} <b>이벤트 참여 신청</b>\n\n"
        "📌 <b>신청번호</b> : <code>#{application_id}</code>\n"
        "📅 <b>이벤트 날짜</b> : {event_date}\n"
        "{emoji_user} <b>이름</b> : {name}\n"
        "{emoji_id} <b>아이디</b> : {username}\n"
        "{emoji_key} <b>고유 ID</b> : <code>{user_id}</code>\n"
        "{emoji_time} <b>신청시간</b> : {created_at}\n"
        "{emoji_photo} <b>자료 형태</b> : {media_type}\n"
        "{emoji_photo} <b>자료 수</b> : {media_count}개\n\n"
        "캡처본 확인 후 처리해주세요."
    ),

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


class SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def db_connect() -> sqlite3.Connection:
    folder = os.path.dirname(DB_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        # 기존 V2 데이터 이전용 테이블
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
            CREATE TABLE IF NOT EXISTS application_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT,
                username TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                event_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                processed_by INTEGER,
                media_type TEXT DEFAULT '',
                media_count INTEGER NOT NULL DEFAULT 1,
                admin_notified INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_user
            ON application_history(user_id, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_date
            ON application_history(event_date, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_status
            ON application_history(status, created_at DESC)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )

        migrated = conn.execute(
            "SELECT value FROM settings WHERE key = 'history_migrated_v4'"
        ).fetchone()

        if not migrated:
            old_rows = conn.execute(
                "SELECT user_id, name, username, status, created_at FROM applications"
            ).fetchall()

            for row in old_rows:
                created_at = row["created_at"] or now_kst()
                event_date = (
                    created_at[:10]
                    if len(created_at) >= 10
                    else today_kst()
                )

                exists = conn.execute(
                    """
                    SELECT 1
                    FROM application_history
                    WHERE user_id = ? AND created_at = ?
                    LIMIT 1
                    """,
                    (row["user_id"], created_at),
                ).fetchone()

                if not exists:
                    conn.execute(
                        """
                        INSERT INTO application_history(
                            user_id, name, username, status,
                            event_date, created_at,
                            media_type, media_count, admin_notified
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 'legacy', 1, 1)
                        """,
                        (
                            row["user_id"],
                            row["name"],
                            row["username"],
                            row["status"] or "pending",
                            event_date,
                            created_at,
                        ),
                    )

            conn.execute(
                """
                INSERT INTO settings(key, value)
                VALUES ('history_migrated_v4', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
                """
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
            INSERT INTO settings(key, value)
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
        logger.exception("설정 문구 렌더링 실패 key=%s", key)
        return template


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def event_enabled() -> bool:
    return get_setting("event_enabled", "1") == "1"


def get_application_by_id(application_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM application_history WHERE id = ?",
            (application_id,),
        ).fetchone()


def get_latest_application(user_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def get_today_application(user_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE user_id = ? AND event_date = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, today_kst()),
        ).fetchone()


def save_application(
    user_id: int,
    name: str,
    username: str,
    media_type: str,
    media_count: int,
) -> int:
    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO application_history(
                user_id, name, username, status,
                event_date, created_at,
                media_type, media_count,
                admin_notified
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0)
            """,
            (
                user_id,
                name,
                username,
                today_kst(),
                now_kst(),
                media_type,
                media_count,
            ),
        )
        return cursor.lastrowid


def mark_admin_notified(application_id: int, notified: bool) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE application_history
            SET admin_notified = ?
            WHERE id = ?
            """,
            (1 if notified else 0, application_id),
        )


def update_status(
    application_id: int,
    status: str,
    processed_by: Optional[int] = None,
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE application_history
            SET status = ?,
                processed_at = ?,
                processed_by = ?
            WHERE id = ?
            """,
            (status, now_kst(), processed_by, application_id),
        )


def delete_application(application_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM application_history WHERE id = ?",
            (application_id,),
        )


def admin_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 승인",
                callback_data=f"approve:{application_id}",
            ),
            InlineKeyboardButton(
                "❌ 거절",
                callback_data=f"reject:{application_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚫 차단",
                callback_data=f"block:{application_id}",
            ),
            InlineKeyboardButton(
                "🗑 신청삭제",
                callback_data=f"delete:{application_id}",
            ),
        ],
    ])


def main_menu_keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            "📋 내 신청 상태",
            callback_data="user:status",
        ),
        InlineKeyboardButton(
            "📸 참여 방법",
            callback_data="user:guide",
        ),
    ]]

    if admin:
        rows.append([
            InlineKeyboardButton(
                "⚙️ 관리자 메뉴",
                callback_data="admin:home",
            )
        ])

    return InlineKeyboardMarkup(rows)


def admin_home_keyboard() -> InlineKeyboardMarkup:
    state_text = (
        "🔴 이벤트 종료"
        if event_enabled()
        else "🟢 이벤트 시작"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                state_text,
                callback_data="admin:toggle",
            ),
            InlineKeyboardButton(
                "📊 통계",
                callback_data="admin:stats",
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 승인 대기",
                callback_data="admin:pending",
            ),
            InlineKeyboardButton(
                "🔍 회원 검색",
                callback_data="admin:search",
            ),
        ],
        [
            InlineKeyboardButton(
                "📅 날짜별 참여내역",
                callback_data="admin:history",
            ),
            InlineKeyboardButton(
                "🆔 ID별 참여이력",
                callback_data="admin:user_history",
            ),
        ],
        [
            InlineKeyboardButton(
                "📥 오늘 CSV 다운로드",
                callback_data="admin:csv_today",
            )
        ],
        [
            InlineKeyboardButton(
                "📝 문구 관리",
                callback_data="admin:texts",
            ),
            InlineKeyboardButton(
                "✨ 이모지 관리",
                callback_data="admin:emojis",
            ),
        ],
        [
            InlineKeyboardButton(
                "🏷 이벤트명 수정",
                callback_data="edit:event_name",
            )
        ],
        [
            InlineKeyboardButton(
                "◀️ 닫기",
                callback_data="admin:close",
            )
        ],
    ])


def text_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏠 시작 안내",
                callback_data="edit:start_text",
            ),
            InlineKeyboardButton(
                "📸 참여 안내",
                callback_data="edit:guide_text",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔴 종료 안내",
                callback_data="edit:closed_text",
            ),
            InlineKeyboardButton(
                "📨 접수 안내",
                callback_data="edit:received_text",
            ),
        ],
        [
            InlineKeyboardButton(
                "✅ 승인 문구",
                callback_data="edit:approved_text",
            ),
            InlineKeyboardButton(
                "❌ 거절 문구",
                callback_data="edit:rejected_text",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚫 차단 문구",
                callback_data="edit:blocked_text",
            ),
            InlineKeyboardButton(
                "⏳ 대기 문구",
                callback_data="edit:pending_text",
            ),
        ],
        [
            InlineKeyboardButton(
                "📩 관리자 신청 카드",
                callback_data="edit:admin_caption",
            )
        ],
        [
            InlineKeyboardButton(
                "◀️ 관리자 메뉴",
                callback_data="admin:home",
            )
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
    items = list(EMOJI_SETTING_LABELS.items())
    rows = []

    for index in range(0, len(items), 2):
        row = []
        for key, label in items[index:index + 2]:
            row.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"edit:{key}",
                )
            )
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            "♻️ 기본 이모지 복원",
            callback_data="admin:emoji_reset",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "◀️ 관리자 메뉴",
            callback_data="admin:home",
        )
    ])

    return InlineKeyboardMarkup(rows)


def split_text(text: str, max_length: int = 3800) -> Iterable[str]:
    while len(text) > max_length:
        cut = text.rfind("\n", 0, max_length)
        if cut <= 0:
            cut = max_length
        yield text[:cut]
        text = text[cut:].lstrip("\n")

    if text:
        yield text


def status_card(row: Optional[sqlite3.Row]) -> str:
    if not row:
        return (
            "📋 <b>내 신청 상태</b>\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "📭 오늘 신청 내역이 없습니다.\n\n"
            "━━━━━━━━━━━━━━"
        )

    status = STATUS_NAMES.get(row["status"], escape(row["status"]))
    return (
        "📋 <b>내 신청 상태</b>\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"📌 <b>신청번호</b>\n<code>#{row['id']}</code>\n\n"
        f"📅 <b>이벤트 날짜</b>\n{escape(row['event_date'])}\n\n"
        f"📊 <b>현재 상태</b>\n{status}\n\n"
        f"🕒 <b>신청시간</b>\n{escape(row['created_at'])}\n\n"
        f"✅ <b>처리시간</b>\n{escape(row['processed_at'] or '-')}\n\n"
        "━━━━━━━━━━━━━━"
    )


def make_admin_caption(
    user,
    application_id: int,
    media_type: str,
    media_count: int,
) -> str:
    username = (
        f"@{escape(user.username)}"
        if user.username
        else "없음"
    )

    media_labels = {
        "photo": "사진",
        "album": "사진 묶음",
        "animation": "GIF",
        "document": "이미지 파일",
    }

    return render_setting(
        "admin_caption",
        application_id=application_id,
        event_date=today_kst(),
        name=escape(user.full_name or "이름 없음"),
        username=username,
        user_id=user.id,
        created_at=now_kst(),
        media_type=media_labels.get(media_type, media_type),
        media_count=media_count,
    )


async def safe_send_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
) -> bool:
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return True

    except (Forbidden, BadRequest, TelegramError) as exc:
        logger.warning(
            "사용자 알림 전송 실패 user_id=%s error=%s",
            user_id,
            exc,
        )
        return False


async def safe_admin_delivery(
    send_operation,
    application_id: int,
) -> bool:
    try:
        await send_operation()
        mark_admin_notified(application_id, True)
        return True

    except Exception:
        logger.exception(
            "관리자 알림 전송 실패 application_id=%s admin_id=%s",
            application_id,
            ADMIN_ID,
        )
        mark_admin_notified(application_id, False)
        update_status(application_id, "notify_failed")
        return False


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user

    text = (
        render_setting("closed_text")
        if not event_enabled() and not is_admin(user.id)
        else render_setting("start_text")
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin(user.id)),
    )


async def ping_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "✅ 신사 이벤트 참여봇 V4 정상 작동 중"
    )


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "관리자만 사용할 수 있습니다."
        )
        return

    context.user_data.clear()

    await update.effective_message.reply_text(
        "⚙️ <b>관리자 설정</b>\n\n"
        "관리할 메뉴를 선택해주세요.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def myid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "🆔 당신의 텔레그램 숫자 ID\n\n"
        f"<code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await send_status(
        update.effective_message,
        update.effective_user.id,
    )


async def send_status(message, user_id: int) -> None:
    await message.reply_text(
        status_card(get_today_application(user_id)),
        parse_mode=ParseMode.HTML,
    )


async def today_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "관리자만 확인할 수 있습니다."
        )
        return

    await send_stats(update.effective_message)


async def send_stats(message) -> None:
    date = today_kst()

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM application_history
            WHERE event_date = ?
            GROUP BY status
            """,
            (date,),
        ).fetchall()

        total_all = conn.execute(
            "SELECT COUNT(*) AS cnt FROM application_history"
        ).fetchone()["cnt"]

    counts = {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "blocked": 0,
        "notify_failed": 0,
    }

    for row in rows:
        counts[row["status"]] = row["cnt"]

    total_today = sum(counts.values())

    text = (
        "📊 <b>이벤트 참여 통계</b>\n\n"
        f"📅 오늘 날짜 : <b>{date}</b>\n"
        f"📨 오늘 신청 : <b>{total_today}건</b>\n"
        f"⏳ 승인 대기 : <b>{counts['pending']}건</b>\n"
        f"✅ 승인 완료 : <b>{counts['approved']}건</b>\n"
        f"❌ 거절 : <b>{counts['rejected']}건</b>\n"
        f"🚫 차단 : <b>{counts['blocked']}건</b>\n"
        f"⚠️ 전달 실패 : <b>{counts['notify_failed']}건</b>\n\n"
        f"🗂 전체 누적 : <b>{total_all}건</b>"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def list_pending_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "관리자만 확인할 수 있습니다."
        )
        return

    await send_pending(update.effective_message)


async def send_pending(message) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, username, user_id, created_at
            FROM application_history
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall()

    if not rows:
        await message.reply_text(
            "📭 승인 대기 중인 신청자가 없습니다.",
            reply_markup=admin_home_keyboard(),
        )
        return

    lines = ["📋 <b>승인 대기 목록</b>\n"]

    for row in rows:
        lines.append(
            f"📌 신청 #{row['id']}\n"
            f"👤 {escape(row['name'] or '이름 없음')}\n"
            f"🔗 {escape(row['username'] or '없음')}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"🕒 {escape(row['created_at'])}\n"
            "──────────────"
        )

    chunks = list(split_text("\n".join(lines)))

    for index, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=(
                admin_home_keyboard()
                if index == len(chunks) - 1
                else None
            ),
        )


async def check_user_before_submit(message, user) -> bool:
    if ADMIN_ID == 0:
        await message.reply_text(
            "관리자 설정이 아직 완료되지 않았습니다."
        )
        return False

    if not event_enabled() and not is_admin(user.id):
        await message.reply_text(
            render_setting("closed_text"),
            parse_mode=ParseMode.HTML,
        )
        return False

    row = get_today_application(user.id)
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

    # rejected, notify_failed 상태는 재신청 허용
    return True


async def handle_single_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message

    if not await check_user_before_submit(message, user):
        return

    application_id = save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
        "photo",
        1,
    )

    async def send():
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=message.photo[-1].file_id,
            caption=make_admin_caption(
                user,
                application_id,
                "photo",
                1,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await message.reply_text(
            render_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        render_setting(
            "received_text",
            application_id=application_id,
        ),
        parse_mode=ParseMode.HTML,
    )


async def process_album_group(
    context: ContextTypes.DEFAULT_TYPE,
    media_group_id: str,
) -> None:
    await asyncio.sleep(2)

    data = album_cache.pop(media_group_id, None)
    album_tasks.pop(media_group_id, None)

    if not data:
        return

    user = data["user"]
    chat_id = data["chat_id"]
    photos = data["photos"][:10]

    row = get_today_application(user.id)
    if row and row["status"] in {
        "pending",
        "approved",
        "blocked",
    }:
        return

    application_id = save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
        "album",
        len(photos),
    )

    media = []
    caption = make_admin_caption(
        user,
        application_id,
        "album",
        len(photos),
    )

    for index, file_id in enumerate(photos):
        media.append(
            InputMediaPhoto(
                media=file_id,
                caption=caption if index == 0 else None,
                parse_mode=(
                    ParseMode.HTML
                    if index == 0
                    else None
                ),
            )
        )

    async def send():
        await context.bot.send_media_group(
            chat_id=ADMIN_ID,
            media=media,
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "📎 <b>사진 묶음 처리</b>\n\n"
                f"📌 신청번호 : <code>#{application_id}</code>\n"
                f"사진 수 : <b>{len(photos)}장</b>\n"
                f"고유 ID : <code>{user.id}</code>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await context.bot.send_message(
            chat_id=chat_id,
            text=render_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=render_setting(
            "received_album_text",
            application_id=application_id,
            count=len(photos),
        ),
        parse_mode=ParseMode.HTML,
    )


async def handle_album_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message
    media_group_id = message.media_group_id

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

    album_cache[media_group_id]["photos"].append(
        message.photo[-1].file_id
    )


async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_message.media_group_id:
        await handle_album_photo(update, context)
    else:
        await handle_single_photo(update, context)


async def handle_animation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message

    if not await check_user_before_submit(message, user):
        return

    application_id = save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
        "animation",
        1,
    )

    async def send():
        await context.bot.send_animation(
            chat_id=ADMIN_ID,
            animation=message.animation.file_id,
            caption=make_admin_caption(
                user,
                application_id,
                "animation",
                1,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await message.reply_text(
            render_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        render_setting(
            "received_text",
            application_id=application_id,
        ),
        parse_mode=ParseMode.HTML,
    )


async def handle_image_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message

    if not await check_user_before_submit(message, user):
        return

    application_id = save_application(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "없음",
        "document",
        1,
    )

    async def send():
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=message.document.file_id,
            caption=make_admin_caption(
                user,
                application_id,
                "document",
                1,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await message.reply_text(
            render_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        render_setting(
            "received_text",
            application_id=application_id,
        ),
        parse_mode=ParseMode.HTML,
    )


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user

    if is_admin(user.id) and context.user_data.get("edit_key"):
        await handle_admin_setting_input(update, context)
        return

    if is_admin(user.id) and context.user_data.get("search_mode"):
        await handle_admin_search_input(update, context)
        return

    if is_admin(user.id) and context.user_data.pop(
        "history_date_mode",
        False,
    ):
        value = message.text.strip()

        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            await message.reply_text(
                "날짜 형식은 YYYY-MM-DD 입니다.\n"
                "예: 2026-07-30",
                reply_markup=admin_home_keyboard(),
            )
            return

        await send_date_history(message, value)
        return

    if is_admin(user.id) and context.user_data.pop(
        "user_history_mode",
        False,
    ):
        value = message.text.strip()

        if not value.isdigit():
            await message.reply_text(
                "텔레그램 숫자 ID만 입력해주세요.",
                reply_markup=admin_home_keyboard(),
            )
            return

        await send_user_history(message, int(value))
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


async def handle_admin_setting_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    key = context.user_data.pop("edit_key", None)

    if not key:
        return

    value = update.effective_message.text.strip()

    if value == "/cancel":
        await update.effective_message.reply_text(
            "수정을 취소했습니다.",
            reply_markup=admin_home_keyboard(),
        )
        return

    if not value:
        await update.effective_message.reply_text(
            "빈 내용은 저장할 수 없습니다."
        )
        return

    set_setting(key, value)

    label = EMOJI_SETTING_LABELS.get(key, key)

    await update.effective_message.reply_text(
        f"✅ <b>{escape(label)}</b> 설정을 저장했습니다.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def handle_admin_search_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    context.user_data.pop("search_mode", None)
    keyword = update.effective_message.text.strip()

    with db_connect() as conn:
        if keyword.isdigit():
            rows = conn.execute(
                """
                SELECT *
                FROM application_history
                WHERE CAST(user_id AS TEXT) LIKE ?
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (f"%{keyword}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM application_history
                WHERE name LIKE ? OR username LIKE ?
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (f"%{keyword}%", f"%{keyword}%"),
            ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "검색 결과가 없습니다.",
            reply_markup=admin_home_keyboard(),
        )
        return

    lines = ["🔍 <b>회원 검색 결과</b>\n"]

    for row in rows:
        lines.append(
            f"📌 신청 #{row['id']}\n"
            f"👤 {escape(row['name'] or '이름 없음')}\n"
            f"🔗 {escape(row['username'] or '없음')}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"📊 {STATUS_NAMES.get(row['status'], escape(row['status']))}\n"
            f"📅 {escape(row['event_date'])}\n"
            f"🕒 {escape(row['created_at'])}\n"
            "──────────────"
        )

    chunks = list(split_text("\n".join(lines)))

    for index, chunk in enumerate(chunks):
        await update.effective_message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=(
                admin_home_keyboard()
                if index == len(chunks) - 1
                else None
            ),
        )


async def send_date_history(message, date: str) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE event_date = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (date,),
        ).fetchall()

    if not rows:
        await message.reply_text(
            f"📭 <b>{escape(date)}</b> 참여 내역이 없습니다.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_keyboard(),
        )
        return

    lines = [f"📅 <b>{escape(date)} 참여 내역</b>\n"]

    for row in rows:
        lines.append(
            f"📌 신청 #{row['id']}\n"
            f"👤 {escape(row['name'] or '이름 없음')}\n"
            f"🔗 {escape(row['username'] or '없음')}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"📊 {STATUS_NAMES.get(row['status'], escape(row['status']))}\n"
            f"🕒 신청 {escape(row['created_at'])}\n"
            f"✅ 처리 {escape(row['processed_at'] or '-')}\n"
            f"👮 처리자 {escape(str(row['processed_by'] or '-'))}\n"
            "──────────────"
        )

    chunks = list(split_text("\n".join(lines)))

    for index, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=(
                admin_home_keyboard()
                if index == len(chunks) - 1
                else None
            ),
        )


async def send_user_history(message, user_id: int) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 200
            """,
            (user_id,),
        ).fetchall()

    if not rows:
        await message.reply_text(
            f"📭 <code>{user_id}</code> 회원의 참여 이력이 없습니다.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_keyboard(),
        )
        return

    first = rows[0]

    lines = [
        "🆔 <b>회원 참여 이력</b>\n",
        f"👤 {escape(first['name'] or '이름 없음')}",
        f"🔗 {escape(first['username'] or '없음')}",
        f"🆔 <code>{user_id}</code>\n",
    ]

    for row in rows:
        lines.append(
            f"📅 {escape(row['event_date'])} / 신청 #{row['id']}\n"
            f"📊 {STATUS_NAMES.get(row['status'], escape(row['status']))}\n"
            f"🕒 신청 {escape(row['created_at'])}\n"
            f"✅ 처리 {escape(row['processed_at'] or '-')}\n"
            "──────────────"
        )

    chunks = list(split_text("\n".join(lines)))

    for index, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=(
                admin_home_keyboard()
                if index == len(chunks) - 1
                else None
            ),
        )


async def history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "관리자만 확인할 수 있습니다."
        )
        return

    date = context.args[0] if context.args else today_kst()

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await update.effective_message.reply_text(
            "날짜 형식은 YYYY-MM-DD 입니다.\n"
            "예: /history 2026-07-30"
        )
        return

    await send_date_history(update.effective_message, date)


async def user_history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "관리자만 확인할 수 있습니다."
        )
        return

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "사용법: /userhistory 숫자ID"
        )
        return

    await send_user_history(
        update.effective_message,
        int(context.args[0]),
    )


def make_csv_bytes(date: str) -> bytes:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, event_date, user_id, name, username,
                status, created_at, processed_at, processed_by,
                media_type, media_count, admin_notified
            FROM application_history
            WHERE event_date = ?
            ORDER BY created_at
            """,
            (date,),
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "신청번호",
        "이벤트날짜",
        "회원ID",
        "이름",
        "텔레그램아이디",
        "상태",
        "신청시간",
        "처리시간",
        "처리관리자ID",
        "자료형태",
        "자료수",
        "관리자알림성공",
    ])

    for row in rows:
        writer.writerow([
            row["id"],
            row["event_date"],
            row["user_id"],
            row["name"],
            row["username"],
            STATUS_NAMES.get(row["status"], row["status"]),
            row["created_at"],
            row["processed_at"] or "",
            row["processed_by"] or "",
            row["media_type"],
            row["media_count"],
            "성공" if row["admin_notified"] else "실패",
        ])

    return output.getvalue().encode("utf-8-sig")


async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if not query:
        return

    try:
        await query.answer()
    except BadRequest:
        pass

    data = query.data or ""

    if data.startswith("user:"):
        action = data.split(":", 1)[1]

        if action == "status":
            await send_status(
                query.message,
                query.from_user.id,
            )

        elif action == "guide":
            await query.message.reply_text(
                render_setting("guide_text"),
                parse_mode=ParseMode.HTML,
            )

        return

    if data.startswith((
        "admin:",
        "edit:",
        "approve:",
        "reject:",
        "block:",
        "delete:",
    )):
        if not is_admin(query.from_user.id):
            await query.answer(
                "관리자만 사용할 수 있습니다.",
                show_alert=True,
            )
            return

    if data == "admin:home":
        context.user_data.clear()

        await query.edit_message_text(
            "⚙️ <b>관리자 설정</b>\n\n"
            "관리할 메뉴를 선택해주세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_keyboard(),
        )
        return

    if data == "admin:close":
        context.user_data.clear()
        await query.edit_message_text("관리자 메뉴를 닫았습니다.")
        return

    if data == "admin:toggle":
        new_value = "0" if event_enabled() else "1"
        set_setting("event_enabled", new_value)

        state = "시작" if new_value == "1" else "종료"

        await query.edit_message_text(
            f"{'🟢' if new_value == '1' else '🔴'} "
            f"이벤트를 <b>{state}</b> 상태로 변경했습니다.",
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

    if data == "admin:history":
        context.user_data.clear()
        context.user_data["history_date_mode"] = True

        await query.edit_message_text(
            "📅 <b>날짜별 참여내역</b>\n\n"
            "확인할 날짜를 YYYY-MM-DD 형식으로 입력해주세요.\n"
            f"오늘 날짜: <code>{today_kst()}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "admin:user_history":
        context.user_data.clear()
        context.user_data["user_history_mode"] = True

        await query.edit_message_text(
            "🆔 <b>ID별 참여이력</b>\n\n"
            "확인할 회원의 텔레그램 숫자 ID를 입력해주세요.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "admin:csv_today":
        csv_bytes = make_csv_bytes(today_kst())

        await query.message.reply_document(
            document=InputFile(
                io.BytesIO(csv_bytes),
                filename=f"event_history_{today_kst()}.csv",
            ),
            caption=f"📥 {today_kst()} 참여내역 CSV",
        )
        return

    if data == "admin:texts":
        await query.edit_message_text(
            "📝 <b>문구 관리</b>\n\n"
            "수정할 문구를 선택해주세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=text_settings_keyboard(),
        )
        return

    if data == "admin:emojis":
        await query.edit_message_text(
            "✨ <b>이모지 관리</b>\n\n"
            "일반 이모지 또는 커스텀 이모지 HTML을 입력할 수 있습니다.",
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
        context.user_data.clear()
        context.user_data["search_mode"] = True

        await query.edit_message_text(
            "🔍 <b>회원 검색</b>\n\n"
            "이름, @아이디 또는 숫자 ID를 입력해주세요.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("edit:"):
        key = data.split(":", 1)[1]

        if key not in DEFAULT_SETTINGS:
            await query.answer(
                "수정할 수 없는 항목입니다.",
                show_alert=True,
            )
            return

        context.user_data.clear()
        context.user_data["edit_key"] = key

        current = get_setting(
            key,
            DEFAULT_SETTINGS[key],
        )

        await query.edit_message_text(
            "✏️ <b>새 내용을 입력해주세요.</b>\n\n"
            f"<b>현재 설정</b>\n<pre>{escape(current)}</pre>\n\n"
            "취소하려면 <code>/cancel</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if ":" not in data:
        return

    action, application_id_text = data.split(":", 1)

    if not application_id_text.isdigit():
        return

    application_id = int(application_id_text)
    row = get_application_by_id(application_id)

    if not row:
        await query.answer(
            "신청 내역을 찾을 수 없습니다.",
            show_alert=True,
        )
        return

    if row["status"] in {
        "approved",
        "rejected",
        "blocked",
    }:
        await query.answer(
            "이미 처리된 신청입니다.",
            show_alert=True,
        )
        return

    user_id = row["user_id"]

    if action == "approve":
        update_status(
            application_id,
            "approved",
            query.from_user.id,
        )

        processed_at = now_kst()

        await safe_send_user(
            context,
            user_id,
            render_setting(
                "approved_text",
                application_id=application_id,
                processed_at=processed_at,
            ),
        )

        result = "✅ 승인 완료"

    elif action == "reject":
        update_status(
            application_id,
            "rejected",
            query.from_user.id,
        )

        await safe_send_user(
            context,
            user_id,
            render_setting(
                "rejected_text",
                application_id=application_id,
            ),
        )

        result = "❌ 거절 완료"

    elif action == "block":
        update_status(
            application_id,
            "blocked",
            query.from_user.id,
        )

        await safe_send_user(
            context,
            user_id,
            render_setting("blocked_text"),
        )

        result = "🚫 차단 완료"

    elif action == "delete":
        delete_application(application_id)
        result = "🗑 신청 내역 삭제"

    else:
        return

    result_text = (
        "\n\n━━━━━━━━━━━━━━\n"
        f"<b>{result}</b>\n"
        f"📌 신청번호 <code>#{application_id}</code>\n"
        f"🆔 회원 ID <code>{user_id}</code>\n"
        f"👮 처리자 <code>{query.from_user.id}</code>\n"
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
            await query.edit_message_reply_markup(
                reply_markup=None
            )
            await query.message.reply_text(
                result_text,
                parse_mode=ParseMode.HTML,
            )

    except BadRequest as exc:
        logger.warning(
            "관리자 처리 메시지 수정 실패 application_id=%s error=%s",
            application_id,
            exc,
        )

        await query.message.reply_text(
            result_text,
            parse_mode=ParseMode.HTML,
        )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception(
        "업데이트 처리 중 오류",
        exc_info=context.error,
    )


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN 환경변수가 설정되지 않았습니다."
        )

    if ADMIN_ID == 0:
        raise ValueError(
            "ADMIN_ID 환경변수가 설정되지 않았습니다."
        )

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("list", list_pending_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(
        CommandHandler("userhistory", user_history_command)
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )
    app.add_handler(
        MessageHandler(filters.ANIMATION, handle_animation)
    )
    app.add_handler(
        MessageHandler(
            filters.Document.IMAGE,
            handle_image_document,
        )
    )
    app.add_handler(
        CallbackQueryHandler(handle_callback)
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    app.add_error_handler(error_handler)

    logger.info(
        "신사 이벤트 참여봇 V4 실행 중 | ADMIN_ID=%s | DB=%s",
        ADMIN_ID,
        DB_FILE,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
