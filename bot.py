
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0").strip() or "0")
DB_FILE = os.getenv("DB_FILE", "/data/event_bot.db").strip()

KST = timezone(timedelta(hours=9))
CARD_LINE = "━━━━━━━━━━━━━━"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sinsa_event_bot_v5")

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
    "no_event_text": (
        "📭 <b>현재 참여할 수 있는 이벤트가 없습니다.</b>\n\n"
        "새 이벤트가 시작되면 다시 이용해주세요."
    ),
    "event_closed_group_text": (
        "🛑 <b>이벤트가 종료되었습니다.</b>\n\n"
        "참여해주신 모든 회원분들께 감사드립니다."
    ),
    "received_text": (
        "📨 <b>이벤트 참여 신청이 접수되었습니다.</b>\n\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    ),
    "received_album_text": (
        "📨 <b>이벤트 참여 신청이 접수되었습니다.</b>\n\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "사진 : <b>{count}장</b>\n"
        "관리자 확인 후 결과를 안내드리겠습니다."
    ),
    "approved_text": (
        "✅ <b>이벤트 참여가 승인되었습니다.</b>\n\n"
        "이벤트 : {event_title}\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "처리시간 : {processed_at}"
    ),
    "rejected_text": (
        "❌ <b>이벤트 참여가 반려되었습니다.</b>\n\n"
        "이벤트 : {event_title}\n"
        "신청번호 : <code>#{application_id}</code>\n"
        "참여 조건과 인증자료를 확인한 뒤 다시 신청해주세요."
    ),
    "blocked_text": (
        "🚫 <b>이벤트 신청이 제한되었습니다.</b>\n\n"
        "자세한 내용은 관리자에게 문의해주세요."
    ),
    "pending_text": (
        "⏳ 이미 해당 이벤트 신청이 접수되어 관리자 확인 대기 중입니다."
    ),
    "already_approved_text": (
        "✅ 해당 이벤트 참여가 이미 승인되었습니다."
    ),
    "admin_send_failed_text": (
        "⚠️ <b>신청 접수 중 오류가 발생했습니다.</b>\n\n"
        "담당자에게 인증자료를 전달하지 못했습니다.\n"
        "잠시 후 다시 제출해주세요."
    ),
}


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
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def init_db() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                participation_time TEXT NOT NULL DEFAULT '',
                conditions TEXT NOT NULL DEFAULT '',
                start_group_text TEXT NOT NULL DEFAULT '',
                end_group_text TEXT NOT NULL DEFAULT '',
                no_event_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                deleted_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS application_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                event_title TEXT DEFAULT '',
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

        columns = table_columns(conn, "application_history")
        for name, sql_type in (
            ("event_id", "INTEGER"),
            ("event_title", "TEXT DEFAULT ''"),
        ):
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE application_history ADD COLUMN {name} {sql_type}"
                )

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_history
            ON application_history(event_id, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_history
            ON application_history(user_id, created_at DESC)
        """)

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )


def get_setting(key: str) -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


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


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def get_event(event_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE id = ? AND status != 'deleted'",
            (event_id,),
        ).fetchone()


def get_active_event() -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM events
            WHERE status = 'active'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


def get_latest_event() -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM events
            WHERE status != 'deleted'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


def create_event() -> int:
    now = now_kst()

    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO events(
                title, content, participation_time, conditions,
                start_group_text, end_group_text, no_event_text,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                "새 이벤트",
                "이벤트 내용을 입력해주세요.",
                "참여시간을 입력해주세요.",
                "참여조건을 입력해주세요.",
                (
                    "🎉 <b>{title}</b>\n\n"
                    "{content}\n\n"
                    "🕒 <b>참여시간</b>\n{participation_time}\n\n"
                    "📌 <b>참여조건</b>\n{conditions}\n\n"
                    "아래 참여봇에서 인증자료를 제출해주세요."
                ),
                get_setting("event_closed_group_text"),
                get_setting("no_event_text"),
                now,
                now,
            ),
        )
        return cursor.lastrowid


def update_event_field(event_id: int, field: str, value: str) -> None:
    allowed = {
        "title",
        "content",
        "participation_time",
        "conditions",
        "start_group_text",
        "end_group_text",
        "no_event_text",
    }

    if field not in allowed:
        raise ValueError("수정할 수 없는 이벤트 항목입니다.")

    with db_connect() as conn:
        conn.execute(
            f"""
            UPDATE events
            SET {field} = ?, updated_at = ?
            WHERE id = ?
            """,
            (value, now_kst(), event_id),
        )


def set_event_status(event_id: int, status: str) -> None:
    with db_connect() as conn:
        if status == "active":
            conn.execute(
                """
                UPDATE events
                SET status = 'ended', ended_at = ?
                WHERE status = 'active' AND id != ?
                """,
                (now_kst(), event_id),
            )
            conn.execute(
                """
                UPDATE events
                SET status = 'active',
                    started_at = ?,
                    ended_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_kst(), now_kst(), event_id),
            )

        elif status == "ended":
            conn.execute(
                """
                UPDATE events
                SET status = 'ended',
                    ended_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_kst(), now_kst(), event_id),
            )

        elif status == "deleted":
            conn.execute(
                """
                UPDATE events
                SET status = 'deleted',
                    deleted_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_kst(), now_kst(), event_id),
            )


def format_event_template(template: str, event: sqlite3.Row) -> str:
    values = {
        "title": escape(event["title"]),
        "content": escape(event["content"]),
        "participation_time": escape(event["participation_time"]),
        "conditions": escape(event["conditions"]),
    }

    try:
        return template.format_map(values)
    except Exception:
        logger.exception("이벤트 문구 렌더링 실패 event_id=%s", event["id"])
        return escape(template)


def event_card(event: sqlite3.Row, admin: bool = False) -> str:
    status = {
        "draft": "⚪ 등록 대기",
        "active": "🟢 진행 중",
        "ended": "🔴 종료",
    }.get(event["status"], event["status"])

    text = (
        f"<b>🎉 {escape(event['title'])}</b>\n\n"
        f"{CARD_LINE}\n\n"
        f"<b>📝 이벤트 내용</b>\n{escape(event['content'])}\n\n"
        f"<b>🕒 참여시간</b>\n{escape(event['participation_time'])}\n\n"
        f"<b>📌 참여조건</b>\n{escape(event['conditions'])}\n\n"
        f"{CARD_LINE}"
    )

    if admin:
        text += (
            f"\n\n<b>상태</b> : {status}\n"
            f"<b>이벤트 번호</b> : <code>#{event['id']}</code>"
        )

    return text


def member_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📋 내 신청 상태",
                callback_data="user:status",
            ),
            InlineKeyboardButton(
                "📸 참여방법",
                callback_data="user:guide",
            ),
        ]
    ])


def admin_home_keyboard() -> InlineKeyboardMarkup:
    active = get_active_event()
    latest = get_latest_event()

    rows = [
        [
            InlineKeyboardButton(
                "➕ 새 이벤트 등록",
                callback_data="admin:event_new",
            )
        ]
    ]

    if latest:
        rows.append([
            InlineKeyboardButton(
                "📝 이벤트 관리",
                callback_data=f"event:manage:{latest['id']}",
            )
        ])

    if active:
        rows.append([
            InlineKeyboardButton(
                "🛑 진행 이벤트 종료",
                callback_data=f"event:end:{active['id']}",
            )
        ])

    rows.extend([
        [
            InlineKeyboardButton(
                "📋 승인 대기",
                callback_data="admin:pending",
            ),
            InlineKeyboardButton(
                "📊 참여 통계",
                callback_data="admin:stats",
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
                "✏️ 공통 문구 관리",
                callback_data="admin:common_texts",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ 관리자 메뉴 닫기",
                callback_data="admin:close",
            )
        ],
    ])

    return InlineKeyboardMarkup(rows)


def event_manage_keyboard(event: sqlite3.Row) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                "🏷 제목 수정",
                callback_data=f"event_edit:title:{event['id']}",
            ),
            InlineKeyboardButton(
                "📝 내용 수정",
                callback_data=f"event_edit:content:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🕒 참여시간 수정",
                callback_data=f"event_edit:participation_time:{event['id']}",
            ),
            InlineKeyboardButton(
                "📌 참여조건 수정",
                callback_data=f"event_edit:conditions:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 시작 공지 수정",
                callback_data=f"event_edit:start_group_text:{event['id']}",
            ),
            InlineKeyboardButton(
                "🛑 종료 공지 수정",
                callback_data=f"event_edit:end_group_text:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "📭 참여불가 문구 수정",
                callback_data=f"event_edit:no_event_text:{event['id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "👀 미리보기",
                callback_data=f"event:preview:{event['id']}",
            )
        ],
    ]

    if event["status"] != "active":
        rows.append([
            InlineKeyboardButton(
                "🟢 이벤트 시작",
                callback_data=f"event:start:{event['id']}",
            )
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "🛑 이벤트 종료",
                callback_data=f"event:end:{event['id']}",
            )
        ])

    if event["status"] in {"draft", "ended"}:
        rows.append([
            InlineKeyboardButton(
                "🗑 이벤트 삭제",
                callback_data=f"event:delete_confirm:{event['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅ 관리자 메뉴",
            callback_data="admin:home",
        )
    ])

    return InlineKeyboardMarkup(rows)


def delete_confirm_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🗑 정말 삭제",
                callback_data=f"event:delete:{event_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅ 이벤트 관리",
                callback_data=f"event:manage:{event_id}",
            )
        ],
    ])


def admin_application_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 승인",
                callback_data=f"application:approve:{application_id}",
            ),
            InlineKeyboardButton(
                "❌ 거절",
                callback_data=f"application:reject:{application_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚫 차단",
                callback_data=f"application:block:{application_id}",
            )
        ],
    ])


def get_application(application_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM application_history WHERE id = ?",
            (application_id,),
        ).fetchone()


def get_event_application(
    event_id: int,
    user_id: int,
) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE event_id = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (event_id, user_id),
        ).fetchone()


def get_latest_user_application(user_id: int) -> Optional[sqlite3.Row]:
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


def save_application(
    event: sqlite3.Row,
    user,
    media_type: str,
    media_count: int,
) -> int:
    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO application_history(
                event_id, event_title,
                user_id, name, username,
                status, event_date, created_at,
                media_type, media_count, admin_notified
            )
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 0)
            """,
            (
                event["id"],
                event["title"],
                user.id,
                user.full_name,
                f"@{user.username}" if user.username else "없음",
                today_kst(),
                now_kst(),
                media_type,
                media_count,
            ),
        )
        return cursor.lastrowid


def update_application_status(
    application_id: int,
    status: str,
    processed_by: int,
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE application_history
            SET status = ?, processed_at = ?, processed_by = ?
            WHERE id = ?
            """,
            (status, now_kst(), processed_by, application_id),
        )


def mark_admin_notified(application_id: int, value: bool) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE application_history
            SET admin_notified = ?
            WHERE id = ?
            """,
            (1 if value else 0, application_id),
        )


def application_status_card(row: Optional[sqlite3.Row]) -> str:
    if not row:
        return (
            "📋 <b>내 신청 상태</b>\n\n"
            f"{CARD_LINE}\n\n"
            "📭 신청 내역이 없습니다.\n\n"
            f"{CARD_LINE}"
        )

    return (
        "📋 <b>내 신청 상태</b>\n\n"
        f"{CARD_LINE}\n\n"
        f"<b>🎉 이벤트</b>\n{escape(row['event_title'] or '-')}\n\n"
        f"<b>📌 신청번호</b>\n<code>#{row['id']}</code>\n\n"
        f"<b>📊 상태</b>\n"
        f"{STATUS_NAMES.get(row['status'], escape(row['status']))}\n\n"
        f"<b>🕒 신청시간</b>\n{escape(row['created_at'])}\n\n"
        f"{CARD_LINE}"
    )


def admin_caption(
    event: sqlite3.Row,
    user,
    application_id: int,
    media_type: str,
    media_count: int,
) -> str:
    return (
        "📩 <b>이벤트 참여 신청</b>\n\n"
        f"{CARD_LINE}\n\n"
        f"<b>🎉 이벤트</b>\n{escape(event['title'])}\n\n"
        f"<b>📌 신청번호</b>\n<code>#{application_id}</code>\n\n"
        f"<b>👤 이름</b>\n{escape(user.full_name or '이름 없음')}\n\n"
        f"<b>🔗 아이디</b>\n"
        f"{('@' + escape(user.username)) if user.username else '없음'}\n\n"
        f"<b>🆔 고유 ID</b>\n<code>{user.id}</code>\n\n"
        f"<b>📸 자료</b>\n{escape(media_type)} / {media_count}개\n\n"
        f"<b>🕒 신청시간</b>\n{now_kst()}\n\n"
        f"{CARD_LINE}"
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
    except Exception:
        logger.exception("사용자 알림 실패 user_id=%s", user_id)
        return False


async def send_group_message(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
    if GROUP_CHAT_ID == 0:
        logger.warning("GROUP_CHAT_ID가 설정되지 않아 그룹 공지를 생략합니다.")
        return False

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception:
        logger.exception("그룹 공지 실패 group_id=%s", GROUP_CHAT_ID)
        return False


async def safe_admin_delivery(
    operation,
    application_id: int,
) -> bool:
    try:
        await operation()
        mark_admin_notified(application_id, True)
        return True
    except Exception:
        logger.exception(
            "관리자 신청 알림 실패 application_id=%s",
            application_id,
        )
        mark_admin_notified(application_id, False)
        update_application_status(
            application_id,
            "notify_failed",
            ADMIN_ID,
        )
        return False


async def check_submission(message, user) -> Optional[sqlite3.Row]:
    event = get_active_event()

    if not event:
        latest = get_latest_event()
        text = (
            latest["no_event_text"]
            if latest and latest["no_event_text"]
            else get_setting("no_event_text")
        )
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
        )
        return None

    row = get_event_application(event["id"], user.id)

    if row and row["status"] == "pending":
        await message.reply_text(
            get_setting("pending_text"),
            parse_mode=ParseMode.HTML,
        )
        return None

    if row and row["status"] == "approved":
        await message.reply_text(
            get_setting("already_approved_text"),
            parse_mode=ParseMode.HTML,
        )
        return None

    if row and row["status"] == "blocked":
        await message.reply_text(
            get_setting("blocked_text"),
            parse_mode=ParseMode.HTML,
        )
        return None

    return event


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    event = get_active_event()

    if not event:
        latest = get_latest_event()
        text = (
            latest["no_event_text"]
            if latest and latest["no_event_text"]
            else get_setting("no_event_text")
        )
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=member_menu(),
        )
        return

    await update.effective_message.reply_text(
        event_card(event),
        parse_mode=ParseMode.HTML,
        reply_markup=member_menu(),
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
        "⚙️ <b>이벤트 관리자 메뉴</b>\n\n"
        "등록·수정·시작·종료할 메뉴를 선택해주세요.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def ping_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "✅ 신사 이벤트 참여봇 V5 정상 작동 중"
    )


async def myid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        f"🆔 <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        application_status_card(
            get_latest_user_application(update.effective_user.id)
        ),
        parse_mode=ParseMode.HTML,
    )


async def handle_single_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message
    event = await check_submission(message, user)

    if not event:
        return

    application_id = save_application(
        event,
        user,
        "사진",
        1,
    )

    async def send():
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=message.photo[-1].file_id,
            caption=admin_caption(
                event,
                user,
                application_id,
                "사진",
                1,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_application_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await message.reply_text(
            get_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        get_setting("received_text").format_map({
            "application_id": application_id,
        }),
        parse_mode=ParseMode.HTML,
    )


async def process_album(
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
    event = get_active_event()

    if not event:
        await context.bot.send_message(
            chat_id=chat_id,
            text=get_setting("no_event_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    existing = get_event_application(event["id"], user.id)
    if existing and existing["status"] in {"pending", "approved", "blocked"}:
        return

    application_id = save_application(
        event,
        user,
        "사진 묶음",
        len(photos),
    )

    media = []
    caption = admin_caption(
        event,
        user,
        application_id,
        "사진 묶음",
        len(photos),
    )

    for index, file_id in enumerate(photos):
        media.append(
            InputMediaPhoto(
                media=file_id,
                caption=caption if index == 0 else None,
                parse_mode=ParseMode.HTML if index == 0 else None,
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
                f"📎 사진 묶음 신청 #{application_id}\n"
                f"총 {len(photos)}장"
            ),
            reply_markup=admin_application_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await context.bot.send_message(
            chat_id=chat_id,
            text=get_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=get_setting("received_album_text").format_map({
            "application_id": application_id,
            "count": len(photos),
        }),
        parse_mode=ParseMode.HTML,
    )


async def handle_album_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    media_group_id = message.media_group_id

    if media_group_id not in album_cache:
        event = await check_submission(message, user)
        if not event:
            return

        album_cache[media_group_id] = {
            "user": user,
            "chat_id": message.chat_id,
            "photos": [],
        }

        album_tasks[media_group_id] = asyncio.create_task(
            process_album(context, media_group_id)
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
    event = await check_submission(message, user)

    if not event:
        return

    application_id = save_application(
        event,
        user,
        "GIF",
        1,
    )

    async def send():
        await context.bot.send_animation(
            chat_id=ADMIN_ID,
            animation=message.animation.file_id,
            caption=admin_caption(
                event,
                user,
                application_id,
                "GIF",
                1,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_application_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await message.reply_text(
            get_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        get_setting("received_text").format_map({
            "application_id": application_id,
        }),
        parse_mode=ParseMode.HTML,
    )


async def handle_image_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message
    event = await check_submission(message, user)

    if not event:
        return

    application_id = save_application(
        event,
        user,
        "이미지 파일",
        1,
    )

    async def send():
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=message.document.file_id,
            caption=admin_caption(
                event,
                user,
                application_id,
                "이미지 파일",
                1,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_application_keyboard(application_id),
        )

    delivered = await safe_admin_delivery(send, application_id)

    if not delivered:
        await message.reply_text(
            get_setting("admin_send_failed_text"),
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        get_setting("received_text").format_map({
            "application_id": application_id,
        }),
        parse_mode=ParseMode.HTML,
    )


async def send_pending(message) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE status = 'pending'
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

    if not rows:
        await message.reply_text(
            "📭 승인 대기 신청이 없습니다.",
            reply_markup=admin_home_keyboard(),
        )
        return

    lines = ["📋 <b>승인 대기 목록</b>\n"]

    for row in rows:
        lines.append(
            f"📌 #{row['id']} / {escape(row['event_title'] or '-')}\n"
            f"👤 {escape(row['name'] or '-')}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"🕒 {escape(row['created_at'])}\n"
            "──────────────"
        )

    await message.reply_text(
        "\n".join(lines)[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def send_stats(message) -> None:
    active = get_active_event()

    with db_connect() as conn:
        if active:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM application_history
                WHERE event_id = ?
                GROUP BY status
                """,
                (active["id"],),
            ).fetchall()
        else:
            rows = []

    counts = {key: 0 for key in STATUS_NAMES}
    for row in rows:
        counts[row["status"]] = row["cnt"]

    title = active["title"] if active else "진행 중인 이벤트 없음"

    await message.reply_text(
        "📊 <b>이벤트 참여 통계</b>\n\n"
        f"🎉 {escape(title)}\n\n"
        f"⏳ 대기 {counts['pending']}건\n"
        f"✅ 승인 {counts['approved']}건\n"
        f"❌ 거절 {counts['rejected']}건\n"
        f"🚫 차단 {counts['blocked']}건",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


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
            await query.message.reply_text(
                application_status_card(
                    get_latest_user_application(query.from_user.id)
                ),
                parse_mode=ParseMode.HTML,
            )

        elif action == "guide":
            event = get_active_event()
            if event:
                await query.message.reply_text(
                    event_card(event),
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.message.reply_text(
                    get_setting("no_event_text"),
                    parse_mode=ParseMode.HTML,
                )
        return

    if not is_admin(query.from_user.id):
        await query.answer(
            "관리자만 사용할 수 있습니다.",
            show_alert=True,
        )
        return

    if data == "admin:home":
        context.user_data.clear()
        await query.edit_message_text(
            "⚙️ <b>이벤트 관리자 메뉴</b>\n\n"
            "관리할 메뉴를 선택해주세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_keyboard(),
        )
        return

    if data == "admin:close":
        context.user_data.clear()
        await query.edit_message_text("관리자 메뉴를 닫았습니다.")
        return

    if data == "admin:event_new":
        event_id = create_event()
        event = get_event(event_id)
        await query.edit_message_text(
            event_card(event, admin=True),
            parse_mode=ParseMode.HTML,
            reply_markup=event_manage_keyboard(event),
        )
        return

    if data == "admin:pending":
        await send_pending(query.message)
        return

    if data == "admin:stats":
        await send_stats(query.message)
        return

    if data in {
        "admin:history",
        "admin:user_history",
        "admin:common_texts",
    }:
        await query.message.reply_text(
            "이 메뉴는 다음 단계 입력을 받도록 준비되어 있습니다.\n"
            "날짜별 조회는 /history YYYY-MM-DD,\n"
            "회원 이력은 /userhistory 숫자ID를 사용해주세요.",
            reply_markup=admin_home_keyboard(),
        )
        return

    if data.startswith("event:"):
        _, action, event_id_text = data.split(":", 2)
        event_id = int(event_id_text)
        event = get_event(event_id)

        if not event:
            await query.message.reply_text(
                "이벤트를 찾을 수 없습니다.",
                reply_markup=admin_home_keyboard(),
            )
            return

        if action == "manage":
            await query.edit_message_text(
                event_card(event, admin=True),
                parse_mode=ParseMode.HTML,
                reply_markup=event_manage_keyboard(event),
            )
            return

        if action == "preview":
            await query.message.reply_text(
                event_card(event),
                parse_mode=ParseMode.HTML,
                reply_markup=event_manage_keyboard(event),
            )
            return

        if action == "start":
            required = [
                event["title"],
                event["content"],
                event["participation_time"],
                event["conditions"],
            ]

            if any(not value.strip() for value in required):
                await query.answer(
                    "제목·내용·참여시간·참여조건을 모두 입력해주세요.",
                    show_alert=True,
                )
                return

            set_event_status(event_id, "active")
            event = get_event(event_id)

            group_text = format_event_template(
                event["start_group_text"],
                event,
            )
            sent = await send_group_message(context, group_text)

            await query.edit_message_text(
                event_card(event, admin=True)
                + (
                    "\n\n✅ 그룹 공지까지 발송했습니다."
                    if sent
                    else "\n\n⚠️ 이벤트는 시작됐지만 그룹 공지는 발송하지 못했습니다."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=event_manage_keyboard(event),
            )
            return

        if action == "end":
            set_event_status(event_id, "ended")
            event = get_event(event_id)

            group_text = format_event_template(
                event["end_group_text"],
                event,
            )
            await send_group_message(context, group_text)

            await query.edit_message_text(
                event_card(event, admin=True)
                + "\n\n🛑 이벤트를 종료했습니다.",
                parse_mode=ParseMode.HTML,
                reply_markup=event_manage_keyboard(event),
            )
            return

        if action == "delete_confirm":
            await query.edit_message_text(
                "🗑 <b>이벤트 삭제 확인</b>\n\n"
                f"{escape(event['title'])}\n\n"
                "신청 이력은 보존하고 이벤트 목록에서만 삭제합니다.",
                parse_mode=ParseMode.HTML,
                reply_markup=delete_confirm_keyboard(event_id),
            )
            return

        if action == "delete":
            if event["status"] == "active":
                await query.answer(
                    "진행 중인 이벤트는 먼저 종료해주세요.",
                    show_alert=True,
                )
                return

            set_event_status(event_id, "deleted")

            await query.edit_message_text(
                "✅ 이벤트를 삭제했습니다.\n"
                "기존 신청 이력은 그대로 보존됩니다.",
                reply_markup=admin_home_keyboard(),
            )
            return

    if data.startswith("event_edit:"):
        _, field, event_id_text = data.split(":", 2)
        event_id = int(event_id_text)
        event = get_event(event_id)

        if not event:
            return

        labels = {
            "title": "이벤트 제목",
            "content": "이벤트 내용",
            "participation_time": "참여시간",
            "conditions": "참여조건",
            "start_group_text": "그룹 시작 공지",
            "end_group_text": "그룹 종료 공지",
            "no_event_text": "참여할 이벤트 없음 문구",
        }

        context.user_data.clear()
        context.user_data["edit_event_id"] = event_id
        context.user_data["edit_event_field"] = field

        await query.edit_message_text(
            f"✏️ <b>{labels.get(field, field)} 수정</b>\n\n"
            "새 내용을 한 번에 입력해주세요.\n\n"
            "시작·종료 공지에서는 다음 변수를 사용할 수 있습니다.\n"
            "<code>{title}</code> <code>{content}</code>\n"
            "<code>{participation_time}</code> <code>{conditions}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅ 이벤트 관리",
                        callback_data=f"event:manage:{event_id}",
                    )
                ]
            ]),
        )
        return

    if data.startswith("application:"):
        _, action, application_id_text = data.split(":", 2)
        application_id = int(application_id_text)
        row = get_application(application_id)

        if not row:
            await query.answer(
                "신청 내역을 찾을 수 없습니다.",
                show_alert=True,
            )
            return

        if row["status"] in {"approved", "rejected", "blocked"}:
            await query.answer(
                "이미 처리된 신청입니다.",
                show_alert=True,
            )
            return

        if action == "approve":
            status = "approved"
            result = "✅ 승인 완료"
            template_key = "approved_text"
        elif action == "reject":
            status = "rejected"
            result = "❌ 거절 완료"
            template_key = "rejected_text"
        else:
            status = "blocked"
            result = "🚫 차단 완료"
            template_key = "blocked_text"

        update_application_status(
            application_id,
            status,
            query.from_user.id,
        )

        template = get_setting(template_key)
        text = template.format_map({
            "event_title": escape(row["event_title"] or "-"),
            "application_id": application_id,
            "processed_at": now_kst(),
        })

        await safe_send_user(
            context,
            row["user_id"],
            text,
        )

        suffix = (
            "\n\n"
            f"{CARD_LINE}\n"
            f"<b>{result}</b>\n"
            f"👮 처리자 <code>{query.from_user.id}</code>\n"
            f"🕒 {now_kst()}\n"
            f"{CARD_LINE}"
        )

        try:
            if query.message.caption is not None:
                await query.edit_message_caption(
                    caption=f"{query.message.caption}{suffix}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            else:
                await query.edit_message_text(
                    f"{query.message.text or ''}{suffix}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
        except BadRequest:
            await query.message.reply_text(
                suffix,
                parse_mode=ParseMode.HTML,
            )
        return


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user

    if is_admin(user.id):
        event_id = context.user_data.get("edit_event_id")
        field = context.user_data.get("edit_event_field")

        if event_id and field:
            value = message.text.strip()

            if not value:
                await message.reply_text(
                    "빈 내용은 저장할 수 없습니다."
                )
                return

            update_event_field(event_id, field, value)
            context.user_data.clear()
            event = get_event(event_id)

            await message.reply_text(
                "✅ 수정 내용을 저장했습니다.",
                reply_markup=event_manage_keyboard(event),
            )
            return

    event = get_active_event()

    if event:
        await message.reply_text(
            event_card(event),
            parse_mode=ParseMode.HTML,
            reply_markup=member_menu(),
        )
    else:
        latest = get_latest_event()
        text = (
            latest["no_event_text"]
            if latest and latest["no_event_text"]
            else get_setting("no_event_text")
        )
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=member_menu(),
        )


async def history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_admin(update.effective_user.id):
        return

    date = context.args[0] if context.args else today_kst()

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE event_date = ?
            ORDER BY id DESC
            LIMIT 100
            """,
            (date,),
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            f"📭 {date} 참여내역이 없습니다."
        )
        return

    lines = [f"📅 <b>{escape(date)} 참여내역</b>\n"]

    for row in rows:
        lines.append(
            f"📌 #{row['id']} / {escape(row['event_title'] or '-')}\n"
            f"👤 {escape(row['name'] or '-')}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"📊 {STATUS_NAMES.get(row['status'], row['status'])}\n"
            "──────────────"
        )

    await update.effective_message.reply_text(
        "\n".join(lines)[:4000],
        parse_mode=ParseMode.HTML,
    )


async def user_history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_admin(update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "사용법: /userhistory 숫자ID"
        )
        return

    user_id = int(context.args[0])

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM application_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 100
            """,
            (user_id,),
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "참여이력이 없습니다."
        )
        return

    lines = [f"🆔 <b>{user_id} 참여이력</b>\n"]

    for row in rows:
        lines.append(
            f"🎉 {escape(row['event_title'] or '-')}\n"
            f"📅 {escape(row['event_date'])}\n"
            f"📊 {STATUS_NAMES.get(row['status'], row['status'])}\n"
            "──────────────"
        )

    await update.effective_message.reply_text(
        "\n".join(lines)[:4000],
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
        raise ValueError("BOT_TOKEN 환경변수가 없습니다.")

    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID 환경변수가 없습니다.")

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("status", status_command))
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
        "신사 이벤트 참여봇 V5 실행 | ADMIN_ID=%s | GROUP_CHAT_ID=%s | DB=%s",
        ADMIN_ID,
        GROUP_CHAT_ID,
        DB_FILE,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
