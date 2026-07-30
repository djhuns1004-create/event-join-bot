
import os
import re
import html
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
DB_FILE = os.getenv("DB_FILE", "/data/event_bot.db").strip()

KST = timezone(timedelta(hours=9))
CARD_LINE = "━━━━━━━━━━━━━━"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sinsa_event_bot_v6")

STATUS_TEXT = {
    "collecting": "📸 인증사진 등록 중",
    "pending": "⏳ 관리자 승인 대기",
    "approved": "✅ 참가 승인",
    "rejected": "❌ 참가 거절",
    "notify_failed": "⚠️ 관리자 전달 실패",
}


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


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
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                title_html TEXT NOT NULL DEFAULT '',
                participation_time TEXT NOT NULL DEFAULT '',
                participation_time_html TEXT NOT NULL DEFAULT '',
                conditions TEXT NOT NULL DEFAULT '',
                conditions_html TEXT NOT NULL DEFAULT '',
                approval_text TEXT NOT NULL DEFAULT '',
                approval_html TEXT NOT NULL DEFAULT '',
                rejection_text TEXT NOT NULL DEFAULT '',
                rejection_html TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                deleted_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS applications_v6 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                event_title TEXT NOT NULL DEFAULT '',
                user_id INTEGER NOT NULL,
                name TEXT,
                username TEXT,
                status TEXT NOT NULL DEFAULT 'collecting',
                created_at TEXT NOT NULL,
                submitted_at TEXT,
                processed_at TEXT,
                processed_by INTEGER,
                admin_notified INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS application_photos_v6 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_v6_event_user
            ON applications_v6(event_id, user_id, id DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_v6_status
            ON applications_v6(status, id DESC)
        """)


def plain_from_html(value: str) -> str:
    value = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value)


def message_to_html(message) -> str:
    text = message.text or ""
    entities = message.entities or []

    if not entities:
        return html.escape(text)

    utf16 = text.encode("utf-16-le")
    replacements = []

    for entity in entities:
        try:
            raw = message.parse_entity(entity)
        except Exception:
            continue

        escaped = html.escape(raw)
        entity_type = str(entity.type)

        if entity_type == "custom_emoji" and entity.custom_emoji_id:
            rendered = (
                f'<tg-emoji emoji-id="{entity.custom_emoji_id}">'
                f"{escaped}</tg-emoji>"
            )
        elif entity_type == "bold":
            rendered = f"<b>{escaped}</b>"
        elif entity_type == "italic":
            rendered = f"<i>{escaped}</i>"
        elif entity_type == "underline":
            rendered = f"<u>{escaped}</u>"
        elif entity_type == "strikethrough":
            rendered = f"<s>{escaped}</s>"
        elif entity_type == "code":
            rendered = f"<code>{escaped}</code>"
        else:
            continue

        replacements.append(
            (entity.offset, entity.length, rendered)
        )

    result = []
    cursor = 0

    for offset, length, rendered in sorted(replacements):
        if offset < cursor:
            continue

        plain = utf16[cursor * 2:offset * 2].decode("utf-16-le")
        result.append(html.escape(plain))
        result.append(rendered)
        cursor = offset + length

    result.append(
        html.escape(
            utf16[cursor * 2:].decode("utf-16-le")
        )
    )
    return "".join(result)


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def get_event(event_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM events
            WHERE id = ? AND status != 'deleted'
            """,
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
                title, title_html,
                participation_time, participation_time_html,
                conditions, conditions_html,
                approval_text, approval_html,
                rejection_text, rejection_html,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                "새 이벤트",
                "새 이벤트",
                "참가시간을 입력해주세요.",
                "참가시간을 입력해주세요.",
                "참여조건을 입력해주세요.",
                "참여조건을 입력해주세요.",
                "✅ 참가승인이 되었습니다.",
                "✅ 참가승인이 되었습니다.",
                "❌ 참가신청이 거절되었습니다.",
                "❌ 참가신청이 거절되었습니다.",
                now,
                now,
            ),
        )
        return cursor.lastrowid


def update_event_text(
    event_id: int,
    field: str,
    plain_value: str,
    html_value: str,
) -> None:
    field_map = {
        "title": ("title", "title_html"),
        "participation_time": (
            "participation_time",
            "participation_time_html",
        ),
        "conditions": ("conditions", "conditions_html"),
        "approval": ("approval_text", "approval_html"),
        "rejection": ("rejection_text", "rejection_html"),
    }

    if field not in field_map:
        raise ValueError("수정할 수 없는 항목입니다.")

    plain_column, html_column = field_map[field]

    with db_connect() as conn:
        conn.execute(
            f"""
            UPDATE events
            SET {plain_column} = ?,
                {html_column} = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                plain_value,
                html_value,
                now_kst(),
                event_id,
            ),
        )


def start_event(event_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE events
            SET status = 'ended',
                ended_at = ?,
                updated_at = ?
            WHERE status = 'active' AND id != ?
            """,
            (now_kst(), now_kst(), event_id),
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


def end_event(event_id: int) -> None:
    with db_connect() as conn:
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


def delete_event(event_id: int) -> None:
    with db_connect() as conn:
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


def event_title_html(event: sqlite3.Row) -> str:
    return event["title_html"] or html.escape(event["title"])


def event_time_html(event: sqlite3.Row) -> str:
    return (
        event["participation_time_html"]
        or html.escape(event["participation_time"])
    )


def event_conditions_html(event: sqlite3.Row) -> str:
    return (
        event["conditions_html"]
        or html.escape(event["conditions"])
    )


def event_card(event: sqlite3.Row, admin: bool = False) -> str:
    status = {
        "draft": "⚪ 등록 대기",
        "active": "🟢 신청 가능",
        "ended": "🔴 종료",
    }.get(event["status"], event["status"])

    text = (
        f"<b>🎉 {event_title_html(event)}</b>\n\n"
        f"{CARD_LINE}\n\n"
        f"<b>🕒 참가시간</b>\n"
        f"{event_time_html(event)}\n\n"
        f"<b>📌 참여조건</b>\n"
        f"{event_conditions_html(event)}\n\n"
        f"{CARD_LINE}"
    )

    if admin:
        text += (
            f"\n\n<b>상태</b> : {status}\n"
            f"<b>이벤트 번호</b> : <code>#{event['id']}</code>"
        )

    return text


def no_event_card() -> str:
    return (
        "📭 <b>현재 참여할 수 있는 이벤트가 없습니다.</b>\n\n"
        "새 이벤트가 등록되면 다시 이용해주세요."
    )


def member_event_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📨 참여 신청",
                callback_data=f"user:apply:{event_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 내 신청 상태",
                callback_data="user:status",
            )
        ],
    ])


def member_no_event_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📋 내 신청 상태",
                callback_data="user:status",
            )
        ]
    ])


def admin_home_keyboard() -> InlineKeyboardMarkup:
    latest = get_latest_event()
    active = get_active_event()

    rows = [
        [
            InlineKeyboardButton(
                "➕ 이벤트 새로 등록",
                callback_data="admin:new_event",
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
                "🛑 현재 이벤트 종료",
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
                "📊 신청 현황",
                callback_data="admin:stats",
            ),
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
                "🏷 이벤트명 수정",
                callback_data=f"edit:title:{event['id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "🕒 참가시간 수정",
                callback_data=f"edit:participation_time:{event['id']}",
            ),
            InlineKeyboardButton(
                "📌 참여조건 수정",
                callback_data=f"edit:conditions:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "✅ 승인문구 수정",
                callback_data=f"edit:approval:{event['id']}",
            ),
            InlineKeyboardButton(
                "❌ 거절문구 수정",
                callback_data=f"edit:rejection:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "👀 미리보기",
                callback_data=f"event:preview:{event['id']}",
            )
        ],
    ]

    if event["status"] == "active":
        rows.append([
            InlineKeyboardButton(
                "🛑 이벤트 종료",
                callback_data=f"event:end:{event['id']}",
            )
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "🟢 이벤트 시작",
                callback_data=f"event:start:{event['id']}",
            )
        ])

    if event["status"] != "active":
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


def edit_back_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅ 이벤트 관리",
                callback_data=f"event:manage:{event_id}",
            )
        ]
    ])


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


def submission_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 사진 제출 완료",
                callback_data=f"submit:finish:{application_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 신청 취소",
                callback_data=f"submit:cancel:{application_id}",
            )
        ],
    ])


def admin_application_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 참가 승인",
                callback_data=f"application:approve:{application_id}",
            ),
            InlineKeyboardButton(
                "❌ 참가 거절",
                callback_data=f"application:reject:{application_id}",
            ),
        ]
    ])


def get_application(application_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM applications_v6
            WHERE id = ?
            """,
            (application_id,),
        ).fetchone()


def get_latest_user_application(user_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM applications_v6
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def get_user_event_application(
    event_id: int,
    user_id: int,
) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM applications_v6
            WHERE event_id = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (event_id, user_id),
        ).fetchone()


def create_application(event: sqlite3.Row, user) -> int:
    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO applications_v6(
                event_id, event_title,
                user_id, name, username,
                status, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'collecting', ?)
            """,
            (
                event["id"],
                event["title"],
                user.id,
                user.full_name,
                f"@{user.username}" if user.username else "없음",
                now_kst(),
            ),
        )
        return cursor.lastrowid


def application_photo_count(application_id: int) -> int:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM application_photos_v6
            WHERE application_id = ?
            """,
            (application_id,),
        ).fetchone()["count"]


def add_application_photo(
    application_id: int,
    file_id: str,
) -> bool:
    if application_photo_count(application_id) >= 5:
        return False

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO application_photos_v6(
                application_id, file_id, created_at
            )
            VALUES (?, ?, ?)
            """,
            (application_id, file_id, now_kst()),
        )

    return True


def get_application_photos(application_id: int) -> list[str]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT file_id
            FROM application_photos_v6
            WHERE application_id = ?
            ORDER BY id
            """,
            (application_id,),
        ).fetchall()

    return [row["file_id"] for row in rows]


def set_application_status(
    application_id: int,
    status: str,
    processed_by: Optional[int] = None,
) -> None:
    with db_connect() as conn:
        if status == "pending":
            conn.execute(
                """
                UPDATE applications_v6
                SET status = 'pending',
                    submitted_at = ?
                WHERE id = ?
                """,
                (now_kst(), application_id),
            )
        else:
            conn.execute(
                """
                UPDATE applications_v6
                SET status = ?,
                    processed_at = ?,
                    processed_by = ?
                WHERE id = ?
                """,
                (
                    status,
                    now_kst(),
                    processed_by,
                    application_id,
                ),
            )


def delete_application(application_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            DELETE FROM application_photos_v6
            WHERE application_id = ?
            """,
            (application_id,),
        )
        conn.execute(
            """
            DELETE FROM applications_v6
            WHERE id = ? AND status = 'collecting'
            """,
            (application_id,),
        )


def status_card(row: Optional[sqlite3.Row]) -> str:
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
        f"<b>🎉 이벤트</b>\n"
        f"{html.escape(row['event_title'])}\n\n"
        f"<b>📌 신청번호</b>\n"
        f"<code>#{row['id']}</code>\n\n"
        f"<b>📊 상태</b>\n"
        f"{STATUS_TEXT.get(row['status'], html.escape(row['status']))}\n\n"
        f"<b>🕒 신청시간</b>\n"
        f"{html.escape(row['created_at'])}\n\n"
        f"{CARD_LINE}"
    )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    event = get_active_event()

    if not event:
        await update.effective_message.reply_text(
            no_event_card(),
            parse_mode=ParseMode.HTML,
            reply_markup=member_no_event_keyboard(),
        )
        return

    await update.effective_message.reply_text(
        event_card(event),
        parse_mode=ParseMode.HTML,
        reply_markup=member_event_keyboard(event["id"]),
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
        "이벤트를 등록하거나 관리해주세요.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def ping_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "✅ 신사 이벤트 참여봇 V6 정상 작동 중"
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        status_card(
            get_latest_user_application(
                update.effective_user.id
            )
        ),
        parse_mode=ParseMode.HTML,
    )


async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message
    application_id = context.user_data.get(
        "collecting_application_id"
    )

    if not application_id:
        await message.reply_text(
            "먼저 이벤트 화면에서 `참여 신청` 버튼을 눌러주세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    application = get_application(application_id)

    if (
        not application
        or application["user_id"] != user.id
        or application["status"] != "collecting"
    ):
        context.user_data.pop("collecting_application_id", None)
        await message.reply_text(
            "진행 중인 사진 인증 신청을 찾을 수 없습니다."
        )
        return

    added = add_application_photo(
        application_id,
        message.photo[-1].file_id,
    )

    count = application_photo_count(application_id)

    if not added:
        await message.reply_text(
            "인증사진은 최대 5장까지 등록할 수 있습니다.",
            reply_markup=submission_keyboard(application_id),
        )
        return

    await message.reply_text(
        f"📸 인증사진이 등록되었습니다.\n\n"
        f"현재 등록: {count}/5장\n\n"
        "사진을 더 보내거나 제출 완료를 눌러주세요.",
        reply_markup=submission_keyboard(application_id),
    )


async def send_application_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    application: sqlite3.Row,
    photos: list[str],
) -> None:
    event = get_event(application["event_id"])

    media = []

    for index, file_id in enumerate(photos):
        caption = None

        if index == 0:
            caption = (
                "📩 <b>이벤트 참가 신청</b>\n\n"
                f"{CARD_LINE}\n\n"
                f"<b>🎉 이벤트</b>\n"
                f"{html.escape(application['event_title'])}\n\n"
                f"<b>📌 신청번호</b>\n"
                f"<code>#{application['id']}</code>\n\n"
                f"<b>👤 회원</b>\n"
                f"{html.escape(application['name'] or '이름 없음')}\n\n"
                f"<b>🔗 아이디</b>\n"
                f"{html.escape(application['username'] or '없음')}\n\n"
                f"<b>🆔 숫자 ID</b>\n"
                f"<code>{application['user_id']}</code>\n\n"
                f"<b>📸 인증사진</b>\n"
                f"{len(photos)}장\n\n"
                f"{CARD_LINE}"
            )

        media.append(
            InputMediaPhoto(
                media=file_id,
                caption=caption,
                parse_mode=(
                    ParseMode.HTML
                    if caption
                    else None
                ),
            )
        )

    await context.bot.send_media_group(
        chat_id=ADMIN_ID,
        media=media,
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📋 신청 #{application['id']} 처리\n\n"
            f"회원 ID: <code>{application['user_id']}</code>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_application_keyboard(
            application["id"]
        ),
    )


async def send_pending(message) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM applications_v6
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
            f"📌 #{row['id']} / "
            f"{html.escape(row['event_title'])}\n"
            f"👤 {html.escape(row['name'] or '-')}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"📸 {application_photo_count(row['id'])}장\n"
            "──────────────"
        )

    await message.reply_text(
        "\n".join(lines)[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def send_stats(message) -> None:
    active = get_active_event()

    if not active:
        await message.reply_text(
            "📭 현재 진행 중인 이벤트가 없습니다.",
            reply_markup=admin_home_keyboard(),
        )
        return

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM applications_v6
            WHERE event_id = ?
            GROUP BY status
            """,
            (active["id"],),
        ).fetchall()

    counts = {
        "collecting": 0,
        "pending": 0,
        "approved": 0,
        "rejected": 0,
    }

    for row in rows:
        counts[row["status"]] = row["count"]

    await message.reply_text(
        "📊 <b>이벤트 신청 현황</b>\n\n"
        f"🎉 {event_title_html(active)}\n\n"
        f"📸 사진 등록 중 : {counts['collecting']}건\n"
        f"⏳ 승인 대기 : {counts['pending']}건\n"
        f"✅ 승인 : {counts['approved']}건\n"
        f"❌ 거절 : {counts['rejected']}건",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def callback_handler(
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
        parts = data.split(":")
        action = parts[1]

        if action == "status":
            await query.message.reply_text(
                status_card(
                    get_latest_user_application(
                        query.from_user.id
                    )
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if action == "apply":
            event_id = int(parts[2])
            event = get_event(event_id)
            active = get_active_event()

            if (
                not event
                or not active
                or active["id"] != event_id
            ):
                await query.message.reply_text(
                    no_event_card(),
                    parse_mode=ParseMode.HTML,
                )
                return

            existing = get_user_event_application(
                event_id,
                query.from_user.id,
            )

            if existing and existing["status"] in {
                "collecting",
                "pending",
                "approved",
            }:
                await query.message.reply_text(
                    status_card(existing),
                    parse_mode=ParseMode.HTML,
                )
                return

            application_id = create_application(
                event,
                query.from_user,
            )

            context.user_data[
                "collecting_application_id"
            ] = application_id

            await query.message.reply_text(
                event_card(event)
                + (
                    "\n\n<b>📸 인증사진 등록</b>\n"
                    "인증사진을 1장부터 최대 5장까지 보내주세요.\n"
                    "사진 등록이 끝나면 `사진 제출 완료`를 눌러주세요."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=submission_keyboard(application_id),
            )
            return

    if data.startswith("submit:"):
        _, action, application_id_text = data.split(":")
        application_id = int(application_id_text)
        application = get_application(application_id)

        if (
            not application
            or application["user_id"] != query.from_user.id
            or application["status"] != "collecting"
        ):
            await query.answer(
                "처리할 수 없는 신청입니다.",
                show_alert=True,
            )
            return

        if action == "cancel":
            delete_application(application_id)
            context.user_data.pop(
                "collecting_application_id",
                None,
            )

            await query.edit_message_text(
                "🗑 이벤트 참가 신청을 취소했습니다."
            )
            return

        photos = get_application_photos(application_id)

        if not 1 <= len(photos) <= 5:
            await query.answer(
                "인증사진을 1장 이상 등록해주세요.",
                show_alert=True,
            )
            return

        try:
            await send_application_to_admin(
                context,
                application,
                photos,
            )
        except Exception:
            logger.exception(
                "관리자 신청 전달 실패 application_id=%s",
                application_id,
            )
            set_application_status(
                application_id,
                "notify_failed",
            )
            await query.message.reply_text(
                "⚠️ 담당자에게 신청을 전달하지 못했습니다.\n"
                "잠시 후 다시 신청해주세요."
            )
            return

        set_application_status(
            application_id,
            "pending",
        )
        context.user_data.pop(
            "collecting_application_id",
            None,
        )

        await query.edit_message_text(
            "📨 <b>참가 신청이 접수되었습니다.</b>\n\n"
            f"신청번호 : <code>#{application_id}</code>\n"
            f"인증사진 : {len(photos)}장\n\n"
            "관리자 확인 후 결과를 안내드립니다.",
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
            "이벤트를 등록하거나 관리해주세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_keyboard(),
        )
        return

    if data == "admin:close":
        context.user_data.clear()
        await query.edit_message_text(
            "관리자 메뉴를 닫았습니다."
        )
        return

    if data == "admin:new_event":
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

    if data.startswith("event:"):
        _, action, event_id_text = data.split(":")
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
                reply_markup=edit_back_keyboard(event_id),
            )
            return

        if action == "start":
            start_event(event_id)
            event = get_event(event_id)

            await query.edit_message_text(
                event_card(event, admin=True)
                + "\n\n✅ 회원 신청이 가능하도록 시작했습니다.",
                parse_mode=ParseMode.HTML,
                reply_markup=event_manage_keyboard(event),
            )
            return

        if action == "end":
            end_event(event_id)
            event = get_event(event_id)

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
                f"{event_title_html(event)}\n\n"
                "이벤트만 삭제하며 기존 신청 기록은 유지됩니다.",
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

            delete_event(event_id)

            await query.edit_message_text(
                "✅ 이벤트를 삭제했습니다.\n"
                "기존 신청 기록은 보존됩니다.",
                reply_markup=admin_home_keyboard(),
            )
            return

    if data.startswith("edit:"):
        _, field, event_id_text = data.split(":")
        event_id = int(event_id_text)

        labels = {
            "title": "이벤트명",
            "participation_time": "참가시간",
            "conditions": "참여조건",
            "approval": "승인문구",
            "rejection": "거절문구",
        }

        context.user_data.clear()
        context.user_data["edit_event_id"] = event_id
        context.user_data["edit_event_field"] = field

        await query.edit_message_text(
            f"✏️ <b>{labels[field]} 수정</b>\n\n"
            "새 내용을 보내주세요.\n"
            "프리미엄 이모지와 줄바꿈도 그대로 저장됩니다.",
            parse_mode=ParseMode.HTML,
            reply_markup=edit_back_keyboard(event_id),
        )
        return

    if data.startswith("application:"):
        _, action, application_id_text = data.split(":")
        application_id = int(application_id_text)
        application = get_application(application_id)

        if not application:
            await query.answer(
                "신청 내역을 찾을 수 없습니다.",
                show_alert=True,
            )
            return

        if application["status"] in {
            "approved",
            "rejected",
        }:
            await query.answer(
                "이미 처리된 신청입니다.",
                show_alert=True,
            )
            return

        event = get_event(application["event_id"])

        if action == "approve":
            status = "approved"
            result = "✅ 참가 승인 완료"
            member_text = (
                event["approval_html"]
                if event
                else "✅ 참가승인이 되었습니다."
            )
        else:
            status = "rejected"
            result = "❌ 참가 거절 완료"
            member_text = (
                event["rejection_html"]
                if event
                else "❌ 참가신청이 거절되었습니다."
            )

        set_application_status(
            application_id,
            status,
            query.from_user.id,
        )

        try:
            await context.bot.send_message(
                chat_id=application["user_id"],
                text=member_text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception(
                "회원 결과 알림 실패 user_id=%s",
                application["user_id"],
            )

        await query.edit_message_text(
            f"{result}\n\n"
            f"신청번호 : #{application_id}\n"
            f"회원 ID : {application['user_id']}\n"
            f"처리시간 : {now_kst()}"
        )
        return


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user

    if is_admin(user.id):
        event_id = context.user_data.get("edit_event_id")
        field = context.user_data.get("edit_event_field")

        if event_id and field:
            html_value = message_to_html(message)
            plain_value = message.text or plain_from_html(html_value)

            if not plain_value.strip():
                await message.reply_text(
                    "빈 내용은 저장할 수 없습니다."
                )
                return

            update_event_text(
                event_id,
                field,
                plain_value,
                html_value,
            )

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
            reply_markup=member_event_keyboard(event["id"]),
        )
    else:
        await message.reply_text(
            no_event_card(),
            parse_mode=ParseMode.HTML,
            reply_markup=member_no_event_keyboard(),
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
    app.add_handler(CommandHandler("status", status_command))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO,
            photo_handler,
        )
    )
    app.add_handler(
        CallbackQueryHandler(callback_handler)
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(error_handler)

    logger.info(
        "신사 이벤트 참여봇 V6 실행 | ADMIN_ID=%s | DB=%s",
        ADMIN_ID,
        DB_FILE,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
