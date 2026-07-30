
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


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = table_columns(conn, table)
    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db() -> None:
    with db_connect() as conn:
        # 기존 V5 events 테이블이 있어도 새 컬럼을 자동으로 추가합니다.
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
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                ended_at TEXT,
                deleted_at TEXT
            )
        """)

        event_columns = (
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("content", "TEXT NOT NULL DEFAULT ''"),
            ("content_html", "TEXT NOT NULL DEFAULT ''"),
            ("deadline_at", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_title", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_content", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_time", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_deadline", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_conditions", "TEXT NOT NULL DEFAULT ''"),
            ("title_html", "TEXT NOT NULL DEFAULT ''"),
            ("participation_time", "TEXT NOT NULL DEFAULT ''"),
            ("participation_time_html", "TEXT NOT NULL DEFAULT ''"),
            ("conditions", "TEXT NOT NULL DEFAULT ''"),
            ("conditions_html", "TEXT NOT NULL DEFAULT ''"),
            ("approval_text", "TEXT NOT NULL DEFAULT ''"),
            ("approval_html", "TEXT NOT NULL DEFAULT ''"),
            ("rejection_text", "TEXT NOT NULL DEFAULT ''"),
            ("rejection_html", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'draft'"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ("started_at", "TEXT"),
            ("ended_at", "TEXT"),
            ("deleted_at", "TEXT"),
        )

        for column, definition in event_columns:
            ensure_column(conn, "events", column, definition)

        # V5 데이터를 V6 표시용 HTML 컬럼으로 자동 보완합니다.
        conn.execute("""
            UPDATE events
            SET content = CASE
                    WHEN content IS NULL OR content = ''
                    THEN '이벤트 내용을 입력해주세요.'
                    ELSE content
                END,
                content_html = CASE
                    WHEN content_html IS NULL OR content_html = ''
                    THEN COALESCE(content, '이벤트 내용을 입력해주세요.')
                    ELSE content_html
                END,
                title_html = CASE
                    WHEN title_html IS NULL OR title_html = ''
                    THEN COALESCE(title, '')
                    ELSE title_html
                END,
                participation_time_html = CASE
                    WHEN participation_time_html IS NULL
                         OR participation_time_html = ''
                    THEN COALESCE(participation_time, '')
                    ELSE participation_time_html
                END,
                conditions_html = CASE
                    WHEN conditions_html IS NULL OR conditions_html = ''
                    THEN COALESCE(conditions, '')
                    ELSE conditions_html
                END,
                approval_text = CASE
                    WHEN approval_text IS NULL OR approval_text = ''
                    THEN '✅ 참가승인이 되었습니다.'
                    ELSE approval_text
                END,
                approval_html = CASE
                    WHEN approval_html IS NULL OR approval_html = ''
                    THEN '✅ 참가승인이 되었습니다.'
                    ELSE approval_html
                END,
                rejection_text = CASE
                    WHEN rejection_text IS NULL OR rejection_text = ''
                    THEN '❌ 참가신청이 거절되었습니다.'
                    ELSE rejection_text
                END,
                rejection_html = CASE
                    WHEN rejection_html IS NULL OR rejection_html = ''
                    THEN '❌ 참가신청이 거절되었습니다.'
                    ELSE rejection_html
                END
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

        application_columns = (
            ("event_id", "INTEGER NOT NULL DEFAULT 0"),
            ("event_title", "TEXT NOT NULL DEFAULT ''"),
            ("user_id", "INTEGER NOT NULL DEFAULT 0"),
            ("name", "TEXT"),
            ("username", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'collecting'"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("submitted_at", "TEXT"),
            ("processed_at", "TEXT"),
            ("processed_by", "INTEGER"),
            ("admin_notified", "INTEGER NOT NULL DEFAULT 0"),
        )

        for column, definition in application_columns:
            ensure_column(
                conn,
                "applications_v6",
                column,
                definition,
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS application_photos_v6 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        photo_columns = (
            ("application_id", "INTEGER NOT NULL DEFAULT 0"),
            ("file_id", "TEXT NOT NULL DEFAULT ''"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
        )

        for column, definition in photo_columns:
            ensure_column(
                conn,
                "application_photos_v6",
                column,
                definition,
            )

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
    events = get_active_events()
    return events[0] if events else None


def get_active_events() -> list[sqlite3.Row]:
    close_expired_events()
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM events
            WHERE status = 'active'
            ORDER BY id DESC
            """
        ).fetchall()


def get_all_events() -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM events
            WHERE status != 'deleted'
            ORDER BY
                CASE status
                    WHEN 'active' THEN 0
                    WHEN 'draft' THEN 1
                    WHEN 'ended' THEN 2
                    ELSE 3
                END,
                id DESC
            """
        ).fetchall()


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
                content, content_html,
                participation_time, participation_time_html,
                deadline_at,
                conditions, conditions_html,
                approval_text, approval_html,
                rejection_text, rejection_html,
                emoji_title, emoji_content, emoji_time,
                emoji_deadline, emoji_conditions,
                status, created_at, updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                '', '', '', '', '',
                'draft', ?, ?
            )
            """,
            (
                "새 이벤트",
                "새 이벤트",
                "이벤트 내용을 입력해주세요.",
                "이벤트 내용을 입력해주세요.",
                "참가시간을 입력해주세요.",
                "참가시간을 입력해주세요.",
                "",
                "참여조건을 입력해주세요.",
                "참여조건을 입력해주세요.",
                "참가승인이 되었습니다.",
                "참가승인이 되었습니다.",
                "참가신청이 거절되었습니다.",
                "참가신청이 거절되었습니다.",
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
        "content": ("content", "content_html"),
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
            (plain_value, html_value, now_kst(), event_id),
        )


def update_event_emoji(
    event_id: int,
    field: str,
    html_value: str,
) -> None:
    column_map = {
        "emoji_title": "emoji_title",
        "emoji_content": "emoji_content",
        "emoji_time": "emoji_time",
        "emoji_deadline": "emoji_deadline",
        "emoji_conditions": "emoji_conditions",
    }

    column = column_map.get(field)
    if not column:
        raise ValueError("수정할 수 없는 이모지 항목입니다.")

    with db_connect() as conn:
        conn.execute(
            f"UPDATE events SET {column} = ?, updated_at = ? WHERE id = ?",
            (html_value, now_kst(), event_id),
        )


def update_event_deadline(event_id: int, value: str) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE events
            SET deadline_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (value, now_kst(), event_id),
        )


def parse_deadline(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=KST)
    except ValueError:
        return None


def event_is_open(event: sqlite3.Row) -> bool:
    if event["status"] != "active":
        return False

    deadline = parse_deadline(event["deadline_at"])
    return deadline is None or datetime.now(KST) <= deadline


def close_expired_events() -> None:
    now = datetime.now(KST)

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, deadline_at
            FROM events
            WHERE status = 'active'
              AND deadline_at IS NOT NULL
              AND deadline_at != ''
            """
        ).fetchall()

        for row in rows:
            deadline = parse_deadline(row["deadline_at"])
            if deadline and now > deadline:
                conn.execute(
                    """
                    UPDATE events
                    SET status = 'ended',
                        ended_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now_kst(), now_kst(), row["id"]),
                )


def start_event(event_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE events
            SET status = 'active',
                started_at = COALESCE(started_at, ?),
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


def field_prefix(event: sqlite3.Row, column: str) -> str:
    value = event[column] or ""
    return f"{value} " if value else ""


def event_content_html(event: sqlite3.Row) -> str:
    return event["content_html"] or html.escape(event["content"] or "")


def event_card(event: sqlite3.Row, admin: bool = False) -> str:
    status = {
        "draft": "등록 대기",
        "active": "신청 가능",
        "ended": "종료",
    }.get(event["status"], event["status"])

    deadline_text = event["deadline_at"] or "별도 마감시간 없음"

    text = (
        f"<b>{field_prefix(event, 'emoji_title')}"
        f"{event_title_html(event)}</b>\n\n"
        f"{CARD_LINE}\n\n"
        f"<b>{field_prefix(event, 'emoji_content')}이벤트 내용</b>\n"
        f"{event_content_html(event)}\n\n"
        f"<b>{field_prefix(event, 'emoji_time')}참가시간</b>\n"
        f"{event_time_html(event)}\n\n"
        f"<b>{field_prefix(event, 'emoji_deadline')}참여 마감시간</b>\n"
        f"{html.escape(deadline_text)}\n\n"
        f"<b>{field_prefix(event, 'emoji_conditions')}참여조건</b>\n"
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
        [InlineKeyboardButton("📨 이 이벤트 참여", callback_data=f"user:apply:{event_id}")],
        [InlineKeyboardButton("⬅ 진행 이벤트 목록", callback_data="user:event_list")],
        [InlineKeyboardButton("📋 내 신청 상태", callback_data="user:status")],
    ])


def member_event_list_keyboard(events: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"🎉 {(event['title'] or f'이벤트 #{event['id']}')[:30]}",
            callback_data=f"user:event:{event['id']}",
        )]
        for event in events
    ]
    rows.append([InlineKeyboardButton("📋 내 신청 상태", callback_data="user:status")])
    return InlineKeyboardMarkup(rows)


def member_no_event_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 내 신청 상태", callback_data="user:status")]
    ])


def active_events_card(events: list[sqlite3.Row]) -> str:
    if not events:
        return no_event_card()

    lines = [
        "<b>🎉 진행 중인 이벤트</b>",
        "",
        CARD_LINE,
        "",
        "참여할 이벤트를 아래 버튼에서 선택해주세요.",
        "",
    ]
    for index, event in enumerate(events, 1):
        lines.append(f"{index}. {event_title_html(event)}")
    lines.extend(["", CARD_LINE])
    return "\\n".join(lines)


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 이벤트 새로 등록", callback_data="admin:new_event")],
        [InlineKeyboardButton("📚 전체 이벤트 관리", callback_data="admin:event_list")],
        [
            InlineKeyboardButton("📋 전체 승인 대기", callback_data="admin:pending"),
            InlineKeyboardButton("📊 전체 신청 현황", callback_data="admin:stats"),
        ],
        [InlineKeyboardButton("❌ 관리자 메뉴 닫기", callback_data="admin:close")],
    ])


def admin_event_list_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for event in get_all_events():
        status_icon = {"active": "🟢", "draft": "⚪", "ended": "🔴"}.get(
            event["status"], "▫️"
        )
        rows.append([
            InlineKeyboardButton(
                f"{status_icon} #{event['id']} {event['title'][:24]}",
                callback_data=f"event:manage:{event['id']}",
            )
        ])

    rows.append([InlineKeyboardButton("➕ 이벤트 새로 등록", callback_data="admin:new_event")])
    rows.append([InlineKeyboardButton("⬅ 관리자 메뉴", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)


def event_manage_keyboard(event: sqlite3.Row) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                "이벤트명 수정",
                callback_data=f"edit:title:{event['id']}",
            ),
            InlineKeyboardButton(
                "내용 수정",
                callback_data=f"edit:content:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "참가시간 수정",
                callback_data=f"edit:participation_time:{event['id']}",
            ),
            InlineKeyboardButton(
                "마감시간 설정",
                callback_data=f"deadline:set:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "참여조건 수정",
                callback_data=f"edit:conditions:{event['id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "승인문구 수정",
                callback_data=f"edit:approval:{event['id']}",
            ),
            InlineKeyboardButton(
                "거절문구 수정",
                callback_data=f"edit:rejection:{event['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "이모지 설정",
                callback_data=f"emoji:menu:{event['id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "미리보기",
                callback_data=f"event:preview:{event['id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "이 이벤트 승인 대기",
                callback_data=f"event:pending:{event['id']}",
            ),
            InlineKeyboardButton(
                "이 이벤트 현황",
                callback_data=f"event:stats:{event['id']}",
            ),
        ],
    ]

    if event["status"] == "active":
        rows.append([
            InlineKeyboardButton(
                "이벤트 종료",
                callback_data=f"event:end:{event['id']}",
            )
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "이벤트 시작",
                callback_data=f"event:start:{event['id']}",
            )
        ])

    if event["status"] != "active":
        rows.append([
            InlineKeyboardButton(
                "이벤트 삭제",
                callback_data=f"event:delete_confirm:{event['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅ 전체 이벤트",
            callback_data="admin:event_list",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅ 관리자 메뉴",
            callback_data="admin:home",
        )
    ])

    return InlineKeyboardMarkup(rows)


def emoji_manage_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "제목 이모지",
                callback_data=f"emoji_edit:emoji_title:{event_id}",
            ),
            InlineKeyboardButton(
                "내용 이모지",
                callback_data=f"emoji_edit:emoji_content:{event_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "참가시간 이모지",
                callback_data=f"emoji_edit:emoji_time:{event_id}",
            ),
            InlineKeyboardButton(
                "마감시간 이모지",
                callback_data=f"emoji_edit:emoji_deadline:{event_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "참여조건 이모지",
                callback_data=f"emoji_edit:emoji_conditions:{event_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "모든 이모지 제거",
                callback_data=f"emoji:clear:{event_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅ 이벤트 관리",
                callback_data=f"event:manage:{event_id}",
            )
        ],
    ])


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
    events = get_active_events()

    if not events:
        await update.effective_message.reply_text(
            no_event_card(),
            parse_mode=ParseMode.HTML,
            reply_markup=member_no_event_keyboard(),
        )
        return

    await update.effective_message.reply_text(
        active_events_card(events),
        parse_mode=ParseMode.HTML,
        reply_markup=member_event_list_keyboard(events),
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
        "✅ 신사 이벤트 참여봇 V8 정상 작동 중"
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
        with db_connect() as conn:
            collecting = conn.execute(
                """
                SELECT id
                FROM applications_v6
                WHERE user_id = ? AND status = 'collecting'
                ORDER BY id DESC
                LIMIT 1
                """,
                (user.id,),
            ).fetchone()
        application_id = collecting["id"] if collecting else None

    if not application_id:
        await message.reply_text(
            "먼저 진행 중인 이벤트를 선택하고 참여 신청 버튼을 눌러주세요."
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


async def send_pending(
    message,
    event_id: Optional[int] = None,
) -> None:
    with db_connect() as conn:
        if event_id is None:
            rows = conn.execute(
                """
                SELECT * FROM applications_v6
                WHERE status = 'pending'
                ORDER BY id DESC LIMIT 50
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM applications_v6
                WHERE status = 'pending' AND event_id = ?
                ORDER BY id DESC LIMIT 50
                """,
                (event_id,),
            ).fetchall()

    event = get_event(event_id) if event_id else None
    back = event_manage_keyboard(event) if event else admin_home_keyboard()

    if not rows:
        await message.reply_text("📭 승인 대기 신청이 없습니다.", reply_markup=back)
        return

    lines = ["📋 <b>승인 대기 목록</b>\\n"]
    for row in rows:
        lines.append(
            f"📌 #{row['id']} / {html.escape(row['event_title'])}\\n"
            f"👤 {html.escape(row['name'] or '-')}\\n"
            f"🆔 <code>{row['user_id']}</code>\\n"
            f"📸 {application_photo_count(row['id'])}장\\n"
            "──────────────"
        )

    await message.reply_text(
        "\\n".join(lines)[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=back,
    )


async def send_stats(
    message,
    event_id: Optional[int] = None,
) -> None:
    event = get_event(event_id) if event_id else None

    with db_connect() as conn:
        if event_id is None:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM applications_v6 GROUP BY status"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM applications_v6
                WHERE event_id = ?
                GROUP BY status
                """,
                (event_id,),
            ).fetchall()

    counts = {
        "collecting": 0,
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "notify_failed": 0,
    }
    for row in rows:
        counts[row["status"]] = row["count"]

    await message.reply_text(
        "📊 <b>이벤트 신청 현황</b>\\n\\n"
        f"🎉 {event_title_html(event) if event else '전체 이벤트'}\\n\\n"
        f"📸 사진 등록 중 : {counts['collecting']}건\\n"
        f"⏳ 승인 대기 : {counts['pending']}건\\n"
        f"✅ 승인 : {counts['approved']}건\\n"
        f"❌ 거절 : {counts['rejected']}건\\n"
        f"⚠️ 전달 실패 : {counts['notify_failed']}건",
        parse_mode=ParseMode.HTML,
        reply_markup=event_manage_keyboard(event) if event else admin_home_keyboard(),
    )


async def callback_handler_impl(
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
                reply_markup=member_no_event_keyboard(),
            )
            return

        if action == "event_list":
            events = get_active_events()
            await query.edit_message_text(
                active_events_card(events),
                parse_mode=ParseMode.HTML,
                reply_markup=(
                    member_event_list_keyboard(events)
                    if events else member_no_event_keyboard()
                ),
            )
            return

        if action == "event":
            event_id = int(parts[2])
            event = get_event(event_id)

            if not event or not event_is_open(event):
                await query.answer(
                    "현재 진행 중인 이벤트가 아닙니다.",
                    show_alert=True,
                )
                return

            await query.edit_message_text(
                event_card(event),
                parse_mode=ParseMode.HTML,
                reply_markup=member_event_keyboard(event_id),
            )
            return

        if action == "apply":
            event_id = int(parts[2])
            event = get_event(event_id)
            if (
                not event
                or not event_is_open(event)
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

    if data == "admin:event_list":
        await query.edit_message_text(
            "📚 <b>전체 이벤트 관리</b>\n\n"
            "관리할 이벤트를 선택해주세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_event_list_keyboard(),
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

    if data.startswith("deadline:"):
        _, action, event_id_text = data.split(":")
        event_id = int(event_id_text)
        event = get_event(event_id)

        if not event:
            return

        if action == "set":
            context.user_data.clear()
            context.user_data["edit_deadline_event_id"] = event_id

            await query.edit_message_text(
                "<b>참여 마감시간 설정</b>\n\n"
                "다음 형식으로 입력해주세요.\n"
                "<code>YYYY-MM-DD HH:MM</code>\n\n"
                "예: <code>2026-07-31 23:30</code>\n"
                "마감시간을 없애려면 <code>없음</code>이라고 입력하세요.",
                parse_mode=ParseMode.HTML,
                reply_markup=edit_back_keyboard(event_id),
            )
            return

    if data.startswith("emoji:"):
        _, action, event_id_text = data.split(":")
        event_id = int(event_id_text)
        event = get_event(event_id)

        if not event:
            return

        if action == "menu":
            await query.edit_message_text(
                "<b>이모지 설정</b>\n\n"
                "항목을 선택한 뒤 일반 이모지 또는 "
                "텔레그램 프리미엄 이모지 하나를 보내주세요.",
                parse_mode=ParseMode.HTML,
                reply_markup=emoji_manage_keyboard(event_id),
            )
            return

        if action == "clear":
            with db_connect() as conn:
                conn.execute(
                    """
                    UPDATE events
                    SET emoji_title = '',
                        emoji_content = '',
                        emoji_time = '',
                        emoji_deadline = '',
                        emoji_conditions = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now_kst(), event_id),
                )

            await query.edit_message_text(
                "모든 항목 이모지를 제거했습니다.",
                reply_markup=emoji_manage_keyboard(event_id),
            )
            return

    if data.startswith("emoji_edit:"):
        _, field, event_id_text = data.split(":")
        event_id = int(event_id_text)

        context.user_data.clear()
        context.user_data["edit_emoji_event_id"] = event_id
        context.user_data["edit_emoji_field"] = field

        await query.edit_message_text(
            "<b>이모지 등록</b>\n\n"
            "사용할 이모지 하나를 보내주세요.\n"
            "이모지를 없애려면 <code>없음</code>이라고 입력하세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=emoji_manage_keyboard(event_id),
        )
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

        if action == "pending":
            await send_pending(query.message, event_id)
            return

        if action == "stats":
            await send_stats(query.message, event_id)
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
            "content": "이벤트 내용",
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


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    try:
        await callback_handler_impl(update, context)

    except Exception as exc:
        logger.exception(
            "버튼 처리 오류 callback_data=%s",
            query.data if query else None,
        )

        if query:
            try:
                await query.answer(
                    "버튼 처리 중 오류가 발생했습니다.",
                    show_alert=True,
                )
            except Exception:
                pass

            try:
                await query.message.reply_text(
                    "⚠️ 버튼 처리 중 오류가 발생했습니다.\n\n"
                    f"오류 종류: <code>{html.escape(type(exc).__name__)}</code>\n"
                    "Railway 로그에서 자세한 내용을 확인해주세요.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=(
                        admin_home_keyboard()
                        if is_admin(query.from_user.id)
                        else None
                    ),
                )
            except Exception:
                pass


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user

    if is_admin(user.id):
        deadline_event_id = context.user_data.get(
            "edit_deadline_event_id"
        )

        if deadline_event_id:
            value = (message.text or "").strip()

            if value == "없음":
                update_event_deadline(deadline_event_id, "")
            else:
                deadline = parse_deadline(value)
                if not deadline:
                    await message.reply_text(
                        "형식이 올바르지 않습니다.\n"
                        "예: 2026-07-31 23:30"
                    )
                    return

                update_event_deadline(
                    deadline_event_id,
                    deadline.strftime("%Y-%m-%d %H:%M"),
                )

            context.user_data.clear()
            event = get_event(deadline_event_id)

            await message.reply_text(
                "참여 마감시간을 저장했습니다.",
                reply_markup=event_manage_keyboard(event),
            )
            return

        emoji_event_id = context.user_data.get(
            "edit_emoji_event_id"
        )
        emoji_field = context.user_data.get(
            "edit_emoji_field"
        )

        if emoji_event_id and emoji_field:
            value = (message.text or "").strip()

            html_value = (
                ""
                if value == "없음"
                else message_to_html(message)
            )

            update_event_emoji(
                emoji_event_id,
                emoji_field,
                html_value,
            )

            context.user_data.clear()

            await message.reply_text(
                "이모지 설정을 저장했습니다.",
                reply_markup=emoji_manage_keyboard(emoji_event_id),
            )
            return

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

    events = get_active_events()

    if events:
        await message.reply_text(
            active_events_card(events),
            parse_mode=ParseMode.HTML,
            reply_markup=member_event_list_keyboard(events),
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
        "신사 이벤트 참여봇 V8 DEADLINE EMOJI 실행 | ADMIN_ID=%s | DB=%s",
        ADMIN_ID,
        DB_FILE,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
