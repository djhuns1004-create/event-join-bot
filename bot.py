import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaAnimation,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# 신사 이벤트 참여봇 V12.5 STRICT TIME
# - 기존 V8 계열 DB 자동 보완
# - 여러 이벤트
# - KST 자동 시작/마감
# - 그룹 시작/마감 공지
# - 즉시 그룹 공지
# - 대표 사진/GIF
# - 인증방식 선택 제거: 회원은 바로 이미지 인증
# - 인증사진 1~5장
# - 인증 안내문 관리자 수정 가능
# - 승인/거절 + 거절사유
# - 이벤트 카드 7개 항목 프리미엄 이모지 설정 가능
# - 이벤트 카드의 고정 일반 이모지 제거
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
DB_FILE = os.getenv("DB_FILE", os.getenv("DB_PATH", "/data/event_bot.db")).strip()

KST = timezone(timedelta(hours=9))
CARD_LINE = "━━━━━━━━━━━━"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sinsa_event_bot_v12_5_strict_time")

STATUS_TEXT = {
    "collecting": "📸 인증사진 등록 중",
    "pending": "⏳ 관리자 승인 대기",
    "approved": "✅ 참가 승인",
    "rejected": "❌ 참가 거절",
    "notify_failed": "⚠️ 관리자 전달 실패",
    "cancelled": "🚫 신청 취소",
}

PROOF_TEXT = {
    "chat": "당일 채팅기록 인증",
    "partner": "당일 제휴 이용내역 인증",
}

REJECT_REASONS = {
    "photo": "인증사진 확인이 어렵습니다.",
    "date": "당일 기록을 확인할 수 없습니다.",
    "condition": "이벤트 참여조건 미달입니다.",
    "duplicate": "중복 신청으로 확인되었습니다.",
}


def now_dt() -> datetime:
    return datetime.now(KST)


def now_kst() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def db_connect() -> sqlite3.Connection:
    folder = os.path.dirname(DB_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    """기존 V8 DB가 있어도 삭제하지 않고 필요한 컬럼만 추가합니다."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                title_html TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                content_html TEXT NOT NULL DEFAULT '',
                participation_time TEXT NOT NULL DEFAULT '',
                participation_time_html TEXT NOT NULL DEFAULT '',
                deadline_at TEXT NOT NULL DEFAULT '',
                conditions TEXT NOT NULL DEFAULT '',
                conditions_html TEXT NOT NULL DEFAULT '',
                approval_text TEXT NOT NULL DEFAULT '',
                approval_html TEXT NOT NULL DEFAULT '',
                rejection_text TEXT NOT NULL DEFAULT '',
                rejection_html TEXT NOT NULL DEFAULT '',
                emoji_title TEXT NOT NULL DEFAULT '',
                emoji_title_id TEXT NOT NULL DEFAULT '',
                emoji_content TEXT NOT NULL DEFAULT '',
                emoji_time TEXT NOT NULL DEFAULT '',
                emoji_start TEXT NOT NULL DEFAULT '',
                emoji_deadline TEXT NOT NULL DEFAULT '',
                emoji_conditions TEXT NOT NULL DEFAULT '',
                emoji_proof TEXT NOT NULL DEFAULT '',
                emoji_approval TEXT NOT NULL DEFAULT '',
                emoji_rejection TEXT NOT NULL DEFAULT '',
                proof_guide TEXT NOT NULL DEFAULT '당일 채팅기록 또는 당일 제휴 이용내역 등 이벤트 조건을 확인할 수 있는 이미지를 등록해주세요.',
                proof_guide_html TEXT NOT NULL DEFAULT '당일 채팅기록 또는 당일 제휴 이용내역 등 이벤트 조건을 확인할 수 있는 이미지를 등록해주세요.',
                start_notice_text TEXT NOT NULL DEFAULT '',
                start_notice_html TEXT NOT NULL DEFAULT '',
                end_notice_text TEXT NOT NULL DEFAULT '',
                end_notice_html TEXT NOT NULL DEFAULT '',
                pre_notice_text TEXT NOT NULL DEFAULT '',
                pre_notice_html TEXT NOT NULL DEFAULT '',
                pre_notice_enabled INTEGER NOT NULL DEFAULT 0,
                pre_notice_announced INTEGER NOT NULL DEFAULT 0,
                pin_start_notice INTEGER NOT NULL DEFAULT 0,
                pin_end_notice INTEGER NOT NULL DEFAULT 0,
                pre_media_file_id TEXT NOT NULL DEFAULT '',
                pre_media_type TEXT NOT NULL DEFAULT '',
                start_media_file_id TEXT NOT NULL DEFAULT '',
                start_media_type TEXT NOT NULL DEFAULT '',
                end_media_file_id TEXT NOT NULL DEFAULT '',
                end_media_type TEXT NOT NULL DEFAULT '',
                pre_notice_message_id INTEGER NOT NULL DEFAULT 0,
                start_notice_message_id INTEGER NOT NULL DEFAULT 0,
                end_notice_message_id INTEGER NOT NULL DEFAULT 0,
                pre_notice_message_type TEXT NOT NULL DEFAULT '',
                start_notice_message_type TEXT NOT NULL DEFAULT '',
                end_notice_message_type TEXT NOT NULL DEFAULT '',
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
            ("title_html", "TEXT NOT NULL DEFAULT ''"),
            ("content", "TEXT NOT NULL DEFAULT ''"),
            ("content_html", "TEXT NOT NULL DEFAULT ''"),
            ("participation_time", "TEXT NOT NULL DEFAULT ''"),
            ("participation_time_html", "TEXT NOT NULL DEFAULT ''"),
            ("deadline_at", "TEXT NOT NULL DEFAULT ''"),
            ("conditions", "TEXT NOT NULL DEFAULT ''"),
            ("conditions_html", "TEXT NOT NULL DEFAULT ''"),
            ("approval_text", "TEXT NOT NULL DEFAULT ''"),
            ("approval_html", "TEXT NOT NULL DEFAULT ''"),
            ("rejection_text", "TEXT NOT NULL DEFAULT ''"),
            ("rejection_html", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_title", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_title_id", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_content", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_time", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_start", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_deadline", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_conditions", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_proof", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_approval", "TEXT NOT NULL DEFAULT ''"),
            ("emoji_rejection", "TEXT NOT NULL DEFAULT ''"),
            ("proof_guide", "TEXT NOT NULL DEFAULT '당일 채팅기록 또는 당일 제휴 이용내역 등 이벤트 조건을 확인할 수 있는 이미지를 등록해주세요.'"),
            ("proof_guide_html", "TEXT NOT NULL DEFAULT '당일 채팅기록 또는 당일 제휴 이용내역 등 이벤트 조건을 확인할 수 있는 이미지를 등록해주세요.'"),
            ("start_notice_text", "TEXT NOT NULL DEFAULT ''"),
            ("start_notice_html", "TEXT NOT NULL DEFAULT ''"),
            ("end_notice_text", "TEXT NOT NULL DEFAULT ''"),
            ("end_notice_html", "TEXT NOT NULL DEFAULT ''"),
            ("pre_notice_text", "TEXT NOT NULL DEFAULT ''"),
            ("pre_notice_html", "TEXT NOT NULL DEFAULT ''"),
            ("pre_notice_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("pre_notice_announced", "INTEGER NOT NULL DEFAULT 0"),
            ("pin_start_notice", "INTEGER NOT NULL DEFAULT 0"),
            ("pin_end_notice", "INTEGER NOT NULL DEFAULT 0"),
            ("pre_media_file_id", "TEXT NOT NULL DEFAULT ''"),
            ("pre_media_type", "TEXT NOT NULL DEFAULT ''"),
            ("start_media_file_id", "TEXT NOT NULL DEFAULT ''"),
            ("start_media_type", "TEXT NOT NULL DEFAULT ''"),
            ("end_media_file_id", "TEXT NOT NULL DEFAULT ''"),
            ("end_media_type", "TEXT NOT NULL DEFAULT ''"),
            ("pre_notice_message_id", "INTEGER NOT NULL DEFAULT 0"),
            ("start_notice_message_id", "INTEGER NOT NULL DEFAULT 0"),
            ("end_notice_message_id", "INTEGER NOT NULL DEFAULT 0"),
            ("pre_notice_message_type", "TEXT NOT NULL DEFAULT ''"),
            ("start_notice_message_type", "TEXT NOT NULL DEFAULT ''"),
            ("end_notice_message_type", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'draft'"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ("started_at", "TEXT"),
            ("ended_at", "TEXT"),
            ("deleted_at", "TEXT"),
            # V9 추가
            ("start_at", "TEXT NOT NULL DEFAULT ''"),
            ("proof_mode", "TEXT NOT NULL DEFAULT 'both'"),
            ("max_photos", "INTEGER NOT NULL DEFAULT 5"),
            ("media_file_id", "TEXT NOT NULL DEFAULT ''"),
            ("media_type", "TEXT NOT NULL DEFAULT ''"),
            ("announce_start", "INTEGER NOT NULL DEFAULT 1"),
            ("announce_end", "INTEGER NOT NULL DEFAULT 1"),
            ("start_announced", "INTEGER NOT NULL DEFAULT 0"),
            ("end_announced", "INTEGER NOT NULL DEFAULT 0"),
        )
        for column, definition in event_columns:
            ensure_column(conn, "events", column, definition)

        # V9.6: 모든 기존/신규 이벤트 인증사진을 1~5장으로 통일
        conn.execute("UPDATE events SET max_photos=5 WHERE max_photos IS NULL OR max_photos != 5")
        conn.execute("""
            UPDATE events
            SET proof_guide = CASE
                    WHEN proof_guide IS NULL OR proof_guide = ''
                    THEN '당일 채팅기록 또는 당일 제휴 이용내역 등 이벤트 조건을 확인할 수 있는 이미지를 등록해주세요.'
                    ELSE proof_guide
                END,
                proof_guide_html = CASE
                    WHEN proof_guide_html IS NULL OR proof_guide_html = ''
                    THEN COALESCE(NULLIF(proof_guide,''), '당일 채팅기록 또는 당일 제휴 이용내역 등 이벤트 조건을 확인할 수 있는 이미지를 등록해주세요.')
                    ELSE proof_guide_html
                END
        """)
        conn.commit()

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
        for column, definition in (
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
            ("proof_type", "TEXT NOT NULL DEFAULT ''"),
            ("reject_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            ensure_column(conn, "applications_v6", column, definition)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS application_photos_v6 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_media_v11 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'photo',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_media_v11_event
            ON event_media_v11(event_id, id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings_v8 (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)

        defaults = {
            "status_button_emoji": "",
            "status_button_emoji_id": "",
            "no_event_emoji": "📭",
            "no_event_emoji_id": "",
            "group_id": "",
            "group_title": "",
            "group_start_notice_enabled": "1",
            "group_end_notice_enabled": "1",
            "group_start_text": "",
            "group_end_text": "",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings_v8(key,value) VALUES (?,?)", (k, v))

        # V11.1: 기존 단일 대표미디어 자동 이전
        try:
            for legacy in conn.execute("""
                SELECT id, media_file_id, media_type
                FROM events
                WHERE media_file_id IS NOT NULL AND media_file_id != ''
            """).fetchall():
                exists = conn.execute(
                    "SELECT 1 FROM event_media_v11 WHERE event_id=? LIMIT 1",
                    (legacy["id"],),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO event_media_v11(event_id,file_id,media_type,created_at) VALUES(?,?,?,?)",
                        (legacy["id"], legacy["media_file_id"], legacy["media_type"] or "photo", now_kst()),
                    )
        except Exception:
            logger.exception("기존 대표미디어 자동 이전 실패")

        # V11.2: 그룹 공지는 별도 ON/OFF 없이 자동 사용
        try:
            conn.execute("""
                UPDATE events
                SET announce_start=1,
                    announce_end=1
                WHERE status != 'deleted'
            """)
            conn.execute(
                "INSERT INTO settings_v8(key,value) VALUES('group_start_notice_enabled','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            conn.execute(
                "INSERT INTO settings_v8(key,value) VALUES('group_end_notice_enabled','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
        except Exception:
            logger.exception("자동공지 기본값 적용 실패")

        # V12: 시작/종료 그룹공지는 별도 설정 없이 항상 자동 사용
        try:
            conn.execute("""
                UPDATE events
                SET announce_start=1,
                    announce_end=1
                WHERE status != 'deleted'
            """)
            conn.execute(
                "INSERT INTO settings_v8(key,value) VALUES('group_start_notice_enabled','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            conn.execute(
                "INSERT INTO settings_v8(key,value) VALUES('group_end_notice_enabled','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
        except Exception:
            logger.exception("자동 그룹공지 기본값 적용 실패")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_v9_event_user ON applications_v6(event_id,user_id,id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_v9_status ON applications_v6(status,id DESC)")
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings_v8 WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings_v8(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def plain_from_html(value: str) -> str:
    value = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", value or "")
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value)


def message_to_html(message) -> str:
    text = message.text or ""
    entities = message.entities or []
    if not entities:
        return html.escape(text)
    # PTB의 text_html이 프리미엄 이모지까지 가장 안정적으로 보존됨
    try:
        return message.text_html
    except Exception:
        return html.escape(text)


def extract_custom_emoji_id_from_html(value: str) -> str:
    m = re.search(r'<tg-emoji\s+emoji-id="([^"]+)"', value or "")
    return m.group(1) if m else ""


def extract_custom_emoji_id_from_message(message) -> str:
    for entity in message.entities or []:
        if str(entity.type) == "custom_emoji" and entity.custom_emoji_id:
            return entity.custom_emoji_id
    return ""


def text_emoji_html(custom_emoji_id: str = "", fallback_emoji: str = "") -> str:
    custom_emoji_id = (custom_emoji_id or "").strip()
    fallback_emoji = (fallback_emoji or "").strip()
    if custom_emoji_id:
        fallback = html.escape(fallback_emoji or "▪")
        return f'<tg-emoji emoji-id="{html.escape(custom_emoji_id)}">{fallback}</tg-emoji>'
    return html.escape(fallback_emoji)


def button_emoji_from_html(value: str) -> str:
    value = re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", "", value or "", flags=re.DOTALL)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()[:4]


def button_label(prefix: str, text: str) -> str:
    prefix = (prefix or "").strip()
    return f"{prefix} {text}".strip()


def premium_button(text: str, callback_data: str, custom_emoji_id: str = "", fallback_emoji: str = "") -> InlineKeyboardButton:
    kwargs = {"text": button_label(fallback_emoji, text), "callback_data": callback_data}
    if custom_emoji_id:
        kwargs["icon_custom_emoji_id"] = custom_emoji_id
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(**kwargs)


def parse_kst(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=KST)
        except ValueError:
            pass
    return None


def fmt_kst(value: str, empty="미설정") -> str:
    dt = parse_kst(value)
    return dt.strftime("%Y.%m.%d %H:%M") if dt else empty


def compact_event_period(start_value: str, end_value: str) -> str:
    start = parse_kst(start_value)
    end = parse_kst(end_value)

    if not start and not end:
        return "시간 미설정"
    if start and not end:
        return start.strftime("%Y.%m.%d %H:%M") + " ~"
    if end and not start:
        return "~ " + end.strftime("%Y.%m.%d %H:%M")

    if start.date() == end.date():
        return f"{start.strftime('%Y.%m.%d %H:%M')} ~ {end.strftime('%H:%M')}"

    return f"{start.strftime('%Y.%m.%d %H:%M')} ~ {end.strftime('%Y.%m.%d %H:%M')}"


def get_event_media(event_id: int) -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT id,event_id,file_id,media_type,created_at FROM event_media_v11 WHERE event_id=? ORDER BY id ASC LIMIT 5",
            (event_id,),
        ).fetchall()


def event_media_count(event_id: int) -> int:
    with db_connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS count FROM event_media_v11 WHERE event_id=?",
            (event_id,),
        ).fetchone()["count"]


def add_event_media(event_id: int, file_id: str, media_type: str) -> bool:
    if media_type not in {"photo", "animation"}:
        return False
    with db_connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM event_media_v11 WHERE event_id=?",
            (event_id,),
        ).fetchone()["count"]
        if count >= 5:
            return False
        conn.execute(
            "INSERT INTO event_media_v11(event_id,file_id,media_type,created_at) VALUES(?,?,?,?)",
            (event_id, file_id, media_type, now_kst()),
        )
        conn.commit()
    return True


def clear_event_media(event_id: int) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM event_media_v11 WHERE event_id=?", (event_id,))
        conn.execute(
            "UPDATE events SET media_file_id='',media_type='',updated_at=? WHERE id=?",
            (now_kst(), event_id),
        )
        conn.commit()


def get_event(event_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM events WHERE id=? AND status!='deleted'", (event_id,)).fetchone()


def get_all_events() -> list[sqlite3.Row]:
    refresh_event_states_sync()
    with db_connect() as conn:
        return conn.execute("""
            SELECT * FROM events WHERE status!='deleted'
            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'scheduled' THEN 1 WHEN 'draft' THEN 2 WHEN 'ended' THEN 3 ELSE 4 END,
                     COALESCE(NULLIF(start_at,''), created_at) DESC, id DESC
        """).fetchall()


def get_active_events() -> list[sqlite3.Row]:
    """
    회원에게는 '현재 시간이 실제 이벤트 시간 범위 안'인 이벤트만 표시합니다.
    status가 잘못 남아 있어도 시간 범위 밖이면 절대 노출하지 않습니다.
    """
    refresh_event_states_sync()

    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE status='active' ORDER BY id DESC"
        ).fetchall()

    return [row for row in rows if event_time_window_open(row)]


def create_event() -> int:
    now = now_kst()
    with db_connect() as conn:
        cur = conn.execute("""
            INSERT INTO events(
                title,title_html,content,content_html,participation_time,participation_time_html,
                deadline_at,conditions,conditions_html,approval_text,approval_html,rejection_text,rejection_html,
                status,created_at,updated_at,start_at,proof_mode,max_photos,announce_start,announce_end
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,5,1,1)
        """, (
            "새 이벤트", "새 이벤트",
            "이벤트 내용을 입력해주세요.", "이벤트 내용을 입력해주세요.",
            "참가시간을 입력해주세요.", "참가시간을 입력해주세요.",
            "", "참여조건을 입력해주세요.", "참여조건을 입력해주세요.",
            "이벤트 승인이 완료되었습니다.", "이벤트 승인이 완료되었습니다.",
            "이벤트 신청이 거절되었습니다.", "이벤트 신청이 거절되었습니다.",
            now, now, "", "both",
        ))
        conn.commit()
        return cur.lastrowid


def update_event_text(event_id: int, field: str, plain_value: str, html_value: str) -> None:
    field_map = {
        "title": ("title", "title_html"),
        "content": ("content", "content_html"),
        "participation_time": ("participation_time", "participation_time_html"),
        "conditions": ("conditions", "conditions_html"),
        "proof_guide": ("proof_guide", "proof_guide_html"),
        "start_notice": ("start_notice_text", "start_notice_html"),
        "end_notice": ("end_notice_text", "end_notice_html"),
        "pre_notice": ("pre_notice_text", "pre_notice_html"),
        "approval": ("approval_text", "approval_html"),
        "rejection": ("rejection_text", "rejection_html"),
    }
    if field not in field_map:
        raise ValueError("수정할 수 없는 항목입니다.")
    pcol, hcol = field_map[field]
    with db_connect() as conn:
        conn.execute(f"UPDATE events SET {pcol}=?,{hcol}=?,updated_at=? WHERE id=?", (plain_value, html_value, now_kst(), event_id))
        if field == "title":
            custom_id = extract_custom_emoji_id_from_html(html_value)
            if custom_id:
                conn.execute("UPDATE events SET emoji_title_id=? WHERE id=?", (custom_id, event_id))
        conn.commit()


def update_event_emoji(event_id: int, field: str, html_value: str, custom_emoji_id: str = "") -> None:
    allowed = {"emoji_title", "emoji_content", "emoji_time", "emoji_start", "emoji_deadline", "emoji_conditions", "emoji_proof", "emoji_approval", "emoji_rejection"}
    if field not in allowed:
        raise ValueError("수정할 수 없는 이모지 항목입니다.")
    with db_connect() as conn:
        if field == "emoji_title":
            conn.execute("UPDATE events SET emoji_title=?,emoji_title_id=?,updated_at=? WHERE id=?", (html_value, custom_emoji_id, now_kst(), event_id))
        else:
            conn.execute(f"UPDATE events SET {field}=?,updated_at=? WHERE id=?", (html_value, now_kst(), event_id))
        conn.commit()


def set_event_field(event_id: int, field: str, value) -> None:
    allowed = {
        "start_at", "deadline_at", "proof_mode", "max_photos", "media_file_id", "media_type",
        "announce_start", "announce_end", "start_announced", "end_announced", "status",
        "pre_notice_enabled", "pre_notice_announced", "pin_start_notice", "pin_end_notice",
        "pre_media_file_id", "pre_media_type", "start_media_file_id", "start_media_type",
        "end_media_file_id", "end_media_type",
        "pre_notice_message_id", "start_notice_message_id", "end_notice_message_id",
        "pre_notice_message_type", "start_notice_message_type", "end_notice_message_type"
    }
    if field not in allowed:
        raise ValueError("잘못된 이벤트 설정입니다.")
    with db_connect() as conn:
        conn.execute(f"UPDATE events SET {field}=?,updated_at=? WHERE id=?", (value, now_kst(), event_id))
        conn.commit()


def start_event(event_id: int, manual: bool = True) -> None:
    with db_connect() as conn:
        conn.execute("UPDATE events SET status='active',started_at=COALESCE(started_at,?),ended_at=NULL,updated_at=? WHERE id=?", (now_kst(), now_kst(), event_id))
        if manual:
            conn.execute("UPDATE events SET start_announced=0 WHERE id=?", (event_id,))
        conn.commit()


def end_event(event_id: int) -> None:
    with db_connect() as conn:
        conn.execute("UPDATE events SET status='ended',ended_at=?,updated_at=? WHERE id=?", (now_kst(), now_kst(), event_id))
        conn.commit()


def delete_event(event_id: int) -> None:
    with db_connect() as conn:
        conn.execute("UPDATE events SET status='deleted',deleted_at=?,updated_at=? WHERE id=?", (now_kst(), now_kst(), event_id))
        conn.commit()


def event_time_window_open(event: sqlite3.Row) -> bool:
    """설정된 시작/마감시간 범위 안에서만 True."""
    now = now_dt()
    start = parse_kst(event["start_at"])
    end = parse_kst(event["deadline_at"])

    # 시작시간이나 마감시간 둘 중 하나라도 없으면 회원 참여 불가
    if not start or not end:
        return False

    return start <= now < end


def refresh_event_states_sync() -> None:
    """
    현재 KST 기준으로 이벤트 상태를 엄격하게 동기화합니다.
    - 시작 전: scheduled
    - 시작~마감 전: active
    - 마감 도달/경과: ended
    """
    now = now_dt()

    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id,status,start_at,deadline_at FROM events WHERE status!='deleted'"
        ).fetchall()

        for row in rows:
            start = parse_kst(row["start_at"])
            end = parse_kst(row["deadline_at"])

            # 시간이 완전히 설정되지 않은 이벤트는 회원에게 절대 공개하지 않음
            if not start or not end:
                if row["status"] not in {"draft", "deleted"}:
                    conn.execute(
                        "UPDATE events SET status='draft',updated_at=? WHERE id=?",
                        (now_kst(), row["id"]),
                    )
                continue

            if now < start:
                new_status = "scheduled"
            elif start <= now < end:
                new_status = "active"
            else:
                new_status = "ended"

            if row["status"] != new_status:
                if new_status == "active":
                    conn.execute(
                        """
                        UPDATE events
                        SET status='active',
                            started_at=COALESCE(started_at,?),
                            ended_at=NULL,
                            updated_at=?
                        WHERE id=?
                        """,
                        (now_kst(), now_kst(), row["id"]),
                    )
                elif new_status == "ended":
                    conn.execute(
                        """
                        UPDATE events
                        SET status='ended',
                            ended_at=COALESCE(ended_at,?),
                            updated_at=?
                        WHERE id=?
                        """,
                        (now_kst(), now_kst(), row["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE events
                        SET status='scheduled',
                            ended_at=NULL,
                            updated_at=?
                        WHERE id=?
                        """,
                        (now_kst(), row["id"]),
                    )

        conn.commit()


def event_is_open(event: sqlite3.Row) -> bool:
    refresh_event_states_sync()
    fresh = get_event(event["id"])
    return bool(
        fresh
        and fresh["status"] == "active"
        and event_time_window_open(fresh)
    )


def event_title_html(event) -> str:
    return event["title_html"] or html.escape(event["title"] or "")


def event_content_html(event) -> str:
    return event["content_html"] or html.escape(event["content"] or "")


def event_time_html(event) -> str:
    return event["participation_time_html"] or html.escape(event["participation_time"] or "")


def event_conditions_html(event) -> str:
    return event["conditions_html"] or html.escape(event["conditions"] or "")


def field_prefix(event, col: str) -> str:
    return f"{event[col]} " if event[col] else ""


def proof_mode_text(mode: str) -> str:
    return {
        "both": "당일 채팅기록 / 당일 제휴 이용내역",
        "chat": "당일 채팅기록",
        "partner": "당일 제휴 이용내역",
    }.get(mode, "당일 채팅기록 / 당일 제휴 이용내역")


def proof_guide_html(event: sqlite3.Row) -> str:
    value = ""
    if "proof_guide_html" in event.keys():
        value = event["proof_guide_html"] or ""
    if value:
        return value
    plain = event["proof_guide"] if "proof_guide" in event.keys() else ""
    return html.escape(
        plain or
        "당일 채팅기록 또는 당일 제휴 이용내역 등 이벤트 조건을 확인할 수 있는 이미지를 등록해주세요."
    )


def event_card(event: sqlite3.Row, admin: bool = False) -> str:
    text = (
        f"<b>{field_prefix(event, 'emoji_title')}{event_title_html(event)}</b>\n"
        f"{CARD_LINE}\n\n"
        f"<b>{field_prefix(event, 'emoji_content')}이벤트 내용</b>\n"
        f"{event_content_html(event)}\n\n"
        f"<b>{field_prefix(event, 'emoji_time')}진행기간</b>\n"
        f"{html.escape(compact_event_period(event['start_at'], event['deadline_at']))}\n\n"
        f"<b>{field_prefix(event, 'emoji_conditions')}참여조건</b>\n"
        f"{event_conditions_html(event)}\n"
        f"{CARD_LINE}"
    )

    if admin:
        status = {
            "draft": "등록 대기",
            "scheduled": "예약",
            "active": "진행중",
            "ended": "종료",
        }.get(event["status"], event["status"])
        text += (
            f"\n상태 : {status}"
            f"\n대표 이미지/GIF : {event_media_count(event['id'])}/5개"
            f"\n이벤트 번호 : <code>#{event['id']}</code>"
        )

    return text


def no_event_card() -> str:
    icon = text_emoji_html(
        get_setting("no_event_emoji_id", ""),
        get_setting("no_event_emoji", "📭"),
    )
    prefix = f"{icon} " if icon else ""
    return (
        f"{prefix}<b>현재 참여할 수 있는 이벤트가 없습니다.</b>\n\n"
        "새 이벤트가 등록되면 다시 이용해주세요."
    )


def member_no_event_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[premium_button(
        "내 신청 상태", "user:status",
        get_setting("status_button_emoji_id", ""),
        get_setting("status_button_emoji", "")
    )]])


def member_event_list_keyboard(events: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for event in events:
        custom_id = event["emoji_title_id"] or extract_custom_emoji_id_from_html(event["emoji_title"] or "")
        fallback = button_emoji_from_html(event["emoji_title"] or "")
        rows.append([premium_button((event["title"] or f"이벤트 #{event['id']}")[:35], f"user:event:{event['id']}", custom_id, fallback)])
    rows.append([premium_button("내 신청 상태", "user:status", get_setting("status_button_emoji_id", ""), get_setting("status_button_emoji", ""))])
    return InlineKeyboardMarkup(rows)


def member_event_keyboard(event: sqlite3.Row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("이벤트 참여", callback_data=f"user:apply:{event['id']}")],
        [InlineKeyboardButton("진행 이벤트 목록", callback_data="user:event_list")],
        [premium_button(
            "내 신청 상태",
            "user:status",
            get_setting("status_button_emoji_id", ""),
            get_setting("status_button_emoji", "")
        )],
    ])


def proof_keyboard(event_id: int, mode: str) -> InlineKeyboardMarkup:
    rows = []
    if mode in {"both", "chat"}:
        rows.append([InlineKeyboardButton("💬 당일 채팅 인증", callback_data=f"proof:chat:{event_id}")])
    if mode in {"both", "partner"}:
        rows.append([InlineKeyboardButton("🤝 당일 제휴 이용 인증", callback_data=f"proof:partner:{event_id}")])
    rows.append([InlineKeyboardButton("⬅ 이벤트로 돌아가기", callback_data=f"user:event:{event_id}")])
    return InlineKeyboardMarkup(rows)


def event_media_manage_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("등록 완료", callback_data=f"media:done:{event_id}")],
        [InlineKeyboardButton("대표 이미지/GIF 전체 삭제", callback_data=f"media:clear:{event_id}")],
        [InlineKeyboardButton("이벤트 관리", callback_data=f"event:manage:{event_id}")],
    ])


def submission_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("인증 제출", callback_data=f"submit:finish:{application_id}")],
        [InlineKeyboardButton("신청 취소", callback_data=f"submit:cancel:{application_id}")],
    ])


def admin_application_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("참가 승인", callback_data=f"application:approve:{application_id}"),
        InlineKeyboardButton("참가 거절", callback_data=f"application:reject:{application_id}"),
    ]])


def reject_reason_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 인증사진 확인 불가", callback_data=f"reject:photo:{application_id}")],
        [InlineKeyboardButton("📅 당일 기록 확인 불가", callback_data=f"reject:date:{application_id}")],
        [InlineKeyboardButton("❗ 참여조건 미달", callback_data=f"reject:condition:{application_id}")],
        [InlineKeyboardButton("🔁 중복 신청", callback_data=f"reject:duplicate:{application_id}")],
        [InlineKeyboardButton("✏ 직접 사유 입력", callback_data=f"reject:custom:{application_id}")],
    ])


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("이벤트 새로 등록", callback_data="admin:new_event")],
        [InlineKeyboardButton("전체 이벤트 관리", callback_data="admin:event_list")],
        [
            InlineKeyboardButton("전체 승인 대기", callback_data="admin:pending"),
            InlineKeyboardButton("전체 신청 현황", callback_data="admin:stats")
        ],
        [InlineKeyboardButton("이모지 설정", callback_data="admin:emoji")],
        [InlineKeyboardButton("관리자 메뉴 닫기", callback_data="admin:close")],
    ])



def admin_emoji_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("이벤트 항목 이모지 설정", callback_data="emoji_admin:event_list")],
        [InlineKeyboardButton("대기화면 이모지", callback_data="admin:no_event_emoji")],
        [InlineKeyboardButton("내 신청상태 버튼 이모지", callback_data="admin:status_emoji")],
        [InlineKeyboardButton("관리자 메뉴", callback_data="admin:home")],
    ])


def emoji_event_select_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for event in get_all_events()[:50]:
        rows.append([
            InlineKeyboardButton(
                f"#{event['id']} {(event['title'] or '')[:28]}",
                callback_data=f"emoji_admin:event:{event['id']}",
            )
        ])
    if not rows:
        rows.append([InlineKeyboardButton("등록된 이벤트가 없습니다", callback_data="emoji_admin:none")])
    rows.append([InlineKeyboardButton("이모지 설정", callback_data="admin:emoji")])
    return InlineKeyboardMarkup(rows)


def admin_event_list_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for event in get_all_events()[:50]:
        rows.append([
            InlineKeyboardButton(
                f"#{event['id']} {(event['title'] or '')[:28]}",
                callback_data=f"event:manage:{event['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("이벤트 새로 등록", callback_data="admin:new_event")])
    rows.append([InlineKeyboardButton("관리자 메뉴", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)


def event_manage_keyboard(event: sqlite3.Row) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("이벤트명", callback_data=f"edit:title:{event['id']}"),
            InlineKeyboardButton("이벤트 내용", callback_data=f"edit:content:{event['id']}")
        ],
        [
            InlineKeyboardButton("시작시간", callback_data=f"schedule:start:{event['id']}"),
            InlineKeyboardButton("마감시간", callback_data=f"schedule:end:{event['id']}")
        ],
        [InlineKeyboardButton("이벤트 조건", callback_data=f"edit:conditions:{event['id']}")],
        [InlineKeyboardButton("대표 이미지/GIF", callback_data=f"media:set:{event['id']}")],
        [
            InlineKeyboardButton("승인 문구", callback_data=f"edit:approval:{event['id']}"),
            InlineKeyboardButton("거절 문구", callback_data=f"edit:rejection:{event['id']}")
        ],
        [
            InlineKeyboardButton("시작공지 문구", callback_data=f"edit:start_notice:{event['id']}"),
            InlineKeyboardButton("종료공지 문구", callback_data=f"edit:end_notice:{event['id']}")
        ],
        [InlineKeyboardButton("이모지 설정", callback_data=f"emoji:menu:{event['id']}")],
        [InlineKeyboardButton("미리보기", callback_data=f"event:preview:{event['id']}")],
        [
            InlineKeyboardButton("승인 대기", callback_data=f"event:pending:{event['id']}"),
            InlineKeyboardButton("신청 현황", callback_data=f"event:stats:{event['id']}")
        ],
    ]

    if event["status"] == "active":
        rows.append([InlineKeyboardButton("이벤트 종료", callback_data=f"event:end:{event['id']}")])
    else:
        rows.append([InlineKeyboardButton("이벤트 시작", callback_data=f"event:start:{event['id']}")])

    if event["status"] != "active":
        rows.append([InlineKeyboardButton("이벤트 삭제", callback_data=f"event:delete_confirm:{event['id']}")])

    rows.append([InlineKeyboardButton("전체 이벤트", callback_data="admin:event_list")])
    rows.append([InlineKeyboardButton("관리자 메뉴", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)


def emoji_manage_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("이벤트명 이모지", callback_data=f"emoji_edit:emoji_title:{event_id}"),
            InlineKeyboardButton("내용 이모지", callback_data=f"emoji_edit:emoji_content:{event_id}")
        ],
        [
            InlineKeyboardButton("시간 이모지", callback_data=f"emoji_edit:emoji_time:{event_id}"),
            InlineKeyboardButton("조건 이모지", callback_data=f"emoji_edit:emoji_conditions:{event_id}")
        ],
        [
            InlineKeyboardButton("승인 이모지", callback_data=f"emoji_edit:emoji_approval:{event_id}"),
            InlineKeyboardButton("거절 이모지", callback_data=f"emoji_edit:emoji_rejection:{event_id}")
        ],
        [InlineKeyboardButton("모든 이모지 제거", callback_data=f"emoji:clear:{event_id}")],
        [InlineKeyboardButton("이벤트 관리", callback_data=f"event:manage:{event_id}")],
    ])


def group_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("관리자 메뉴", callback_data="admin:home")]
    ])


def simple_back(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("이벤트 관리", callback_data=f"event:manage:{event_id}")]])


def get_user_event_application(event_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM applications_v6 WHERE event_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (event_id, user_id)).fetchone()


def get_latest_user_application(user_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM applications_v6 WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()


def get_application(application_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM applications_v6 WHERE id=?", (application_id,)).fetchone()


def create_application(event: sqlite3.Row, user, proof_type: str = "image") -> int:
    with db_connect() as conn:
        cur = conn.execute("""
            INSERT INTO applications_v6(event_id,event_title,user_id,name,username,status,created_at,proof_type)
            VALUES(?,?,?,?,?,'collecting',?,?)
        """, (event["id"], event["title"], user.id, user.full_name, f"@{user.username}" if user.username else "없음", now_kst(), proof_type))
        conn.commit()
        return cur.lastrowid


def application_photo_count(application_id: int) -> int:
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) c FROM application_photos_v6 WHERE application_id=?", (application_id,)).fetchone()["c"]


def add_application_photo(application_id: int, file_id: str, max_photos: int) -> bool:
    if application_photo_count(application_id) >= max_photos:
        return False
    with db_connect() as conn:
        conn.execute("INSERT INTO application_photos_v6(application_id,file_id,created_at) VALUES(?,?,?)", (application_id, file_id, now_kst()))
        conn.commit()
    return True


def get_application_photos(application_id: int) -> list[str]:
    with db_connect() as conn:
        rows = conn.execute("SELECT file_id FROM application_photos_v6 WHERE application_id=? ORDER BY id", (application_id,)).fetchall()
    return [r["file_id"] for r in rows]


def set_application_status(application_id: int, status: str, processed_by: Optional[int] = None, reason: str = "") -> None:
    with db_connect() as conn:
        if status == "pending":
            conn.execute("UPDATE applications_v6 SET status='pending',submitted_at=?,admin_notified=1 WHERE id=?", (now_kst(), application_id))
        else:
            conn.execute("UPDATE applications_v6 SET status=?,processed_at=?,processed_by=?,reject_reason=? WHERE id=?", (status, now_kst(), processed_by, reason, application_id))
        conn.commit()


def delete_application(application_id: int) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM application_photos_v6 WHERE application_id=?", (application_id,))
        conn.execute("DELETE FROM applications_v6 WHERE id=? AND status='collecting'", (application_id,))
        conn.commit()


def status_card(row: Optional[sqlite3.Row]) -> str:
    if not row:
        return f"📋 <b>내 신청 상태</b>\n\n{CARD_LINE}\n\n📭 신청 내역이 없습니다.\n\n{CARD_LINE}"
    reason = f"\n\n<b>거절사유</b>\n{html.escape(row['reject_reason'])}" if row["reject_reason"] else ""
    proof = ""
    return (
        f"📋 <b>내 신청 상태</b>\n\n{CARD_LINE}\n\n"
        f"<b>🎉 이벤트</b>\n{html.escape(row['event_title'])}\n\n"
        f"<b>📌 신청번호</b>\n<code>#{row['id']}</code>\n\n"
        f"<b>📊 상태</b>\n{STATUS_TEXT.get(row['status'], html.escape(row['status']))}"
        f"{proof}{reason}\n\n<b>🕒 신청시간</b>\n{html.escape(row['created_at'])}\n\n{CARD_LINE}"
    )


async def send_event_media(bot, chat_id: int, event_id: int) -> int:
    sent = 0
    for item in get_event_media(event_id)[:5]:
        try:
            if item["media_type"] == "animation":
                await bot.send_animation(chat_id, animation=item["file_id"])
            else:
                await bot.send_photo(chat_id, photo=item["file_id"])
            sent += 1
        except Exception:
            logger.exception("대표미디어 전송 실패 event_id=%s media_id=%s", event_id, item["id"])
    return sent


async def send_event_card(bot, chat_id: int, event: sqlite3.Row, reply_markup=None, admin=False):
    await send_event_media(bot, chat_id, event["id"])
    await bot.send_message(
        chat_id,
        text=event_card(event, admin=admin),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def bot_deep_link(application: Application, event_id: int) -> str:
    username = application.bot.username
    if not username:
        me = await application.bot.get_me()
        username = me.username
    return f"https://t.me/{username}?start=event_{event_id}"


def render_event_notice(event: sqlite3.Row, kind: str) -> str:
    if kind == "start":
        template = event["start_notice_html"] or ""
        if template:
            replacements = {
                "{title}": f"<b>{field_prefix(event,'emoji_title')}{event_title_html(event)}</b>",
                "{content}": event_content_html(event),
                "{start_at}": html.escape(fmt_kst(event["start_at"], "지금부터")),
                "{deadline_at}": html.escape(fmt_kst(event["deadline_at"], "별도 마감 없음")),
                "{conditions}": event_conditions_html(event),
                "{period}": html.escape(compact_event_period(event["start_at"], event["deadline_at"])),
            }
            rendered = template
            for key, value in replacements.items():
                rendered = rendered.replace(key, value)
            return rendered

        return (
            f"<b>{field_prefix(event,'emoji_title')}{event_title_html(event)}</b>\n"
            f"{CARD_LINE}\n\n"
            f"<b>{field_prefix(event,'emoji_content')}이벤트 내용</b>\n"
            f"{event_content_html(event)}\n\n"
            f"<b>{field_prefix(event,'emoji_time')}진행기간</b>\n"
            f"{html.escape(compact_event_period(event['start_at'], event['deadline_at']))}\n\n"
            f"<b>{field_prefix(event,'emoji_conditions')}참여조건</b>\n"
            f"{event_conditions_html(event)}\n"
            f"{CARD_LINE}"
        )

    template = event["end_notice_html"] or ""
    if template:
        return template.replace(
            "{title}",
            f"<b>{field_prefix(event,'emoji_title')}{event_title_html(event)}</b>"
        )

    return (
        f"<b>{field_prefix(event,'emoji_title')}{event_title_html(event)}</b>\n\n"
        "이벤트가 종료되었습니다."
    )


async def _edit_or_send_notice(application: Application, group_id: int, event: sqlite3.Row, kind: str, text: str, markup):
    prefix = {"pre": "pre_notice", "start": "start_notice", "end": "end_notice"}[kind]
    message_id = int(event[f"{prefix}_message_id"] or 0)
    old_type = event[f"{prefix}_message_type"] or ""

    if kind == "pre":
        media_file_id = event["pre_media_file_id"] or ""
        media_type = event["pre_media_type"] or ""
    elif kind == "start":
        media_file_id = event["start_media_file_id"] or ""
        media_type = event["start_media_type"] or ""
    else:
        media_file_id = event["end_media_file_id"] or ""
        media_type = event["end_media_type"] or ""

    # 기존 공지가 있으면 삭제하지 않고 같은 메시지를 수정
    if message_id:
        try:
            if media_file_id:
                if media_type == "animation":
                    media = InputMediaAnimation(media=media_file_id, caption=text, parse_mode=ParseMode.HTML)
                else:
                    media = InputMediaPhoto(media=media_file_id, caption=text, parse_mode=ParseMode.HTML)

                if old_type in {"photo", "animation"}:
                    await application.bot.edit_message_media(
                        chat_id=group_id,
                        message_id=message_id,
                        media=media,
                        reply_markup=markup,
                    )
                    set_event_field(event["id"], f"{prefix}_message_type", media_type)
                    return message_id

            if not media_file_id and old_type == "text":
                await application.bot.edit_message_text(
                    chat_id=group_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                return message_id

            if old_type in {"photo", "animation"} and not media_file_id:
                await application.bot.edit_message_caption(
                    chat_id=group_id,
                    message_id=message_id,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                return message_id

            if old_type in {"photo", "animation"} and media_file_id:
                await application.bot.edit_message_caption(
                    chat_id=group_id,
                    message_id=message_id,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                return message_id

        except BadRequest as exc:
            # 내용이 동일하거나 기존 메시지를 수정할 수 없는 경우 새 메시지 전송으로 복구
            if "Message is not modified" in str(exc):
                return message_id
            logger.warning("기존 공지 수정 실패, 새 공지로 복구 kind=%s event_id=%s error=%s", kind, event["id"], exc)

    # 기존 공지가 없거나 수정 불가하면 새 메시지 전송
    if media_file_id and media_type == "animation":
        sent = await application.bot.send_animation(
            group_id, media_file_id, caption=text,
            parse_mode=ParseMode.HTML, reply_markup=markup
        )
        sent_type = "animation"
    elif media_file_id and media_type == "photo":
        sent = await application.bot.send_photo(
            group_id, media_file_id, caption=text,
            parse_mode=ParseMode.HTML, reply_markup=markup
        )
        sent_type = "photo"
    else:
        sent = await application.bot.send_message(
            group_id, text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
        sent_type = "text"

    set_event_field(event["id"], f"{prefix}_message_id", sent.message_id)
    set_event_field(event["id"], f"{prefix}_message_type", sent_type)
    return sent.message_id


async def disable_start_notice_button(application: Application, event: sqlite3.Row) -> None:
    group_id = get_setting("group_id", "").strip()
    message_id = int(event["start_notice_message_id"] or 0)
    if not group_id or not message_id:
        return
    try:
        await application.bot.edit_message_reply_markup(
            chat_id=int(group_id),
            message_id=message_id,
            reply_markup=None,
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.warning("시작공지 참여버튼 비활성화 실패 event_id=%s error=%s", event["id"], exc)
    except Exception:
        logger.exception("시작공지 참여버튼 비활성화 실패 event_id=%s", event["id"])


async def send_group_notice(application: Application, event_id: int, kind: str, manual=False) -> tuple[bool, str]:
    group_id = get_setting("group_id", "").strip()
    if not group_id:
        return False, "등록된 그룹이 없습니다. 그룹방에서 /setgroup 을 먼저 실행해주세요."

    event = get_event(event_id)
    if not event:
        return False, "이벤트를 찾을 수 없습니다."

    if kind == "start":
        text = render_event_notice(event, "start")
        link = await bot_deep_link(application, event_id)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("이벤트 참여하기", url=link)]])

    else:
        text = render_event_notice(event, "end")
        markup = None

    try:
        if kind == "start" and not int(event["start_notice_message_id"] or 0):
            await send_event_media(application.bot, int(group_id), event_id)
        message_id = await _edit_or_send_notice(application, int(group_id), event, kind, text, markup)
    except Exception as exc:
        logger.exception("그룹 공지 처리 실패 kind=%s event_id=%s", kind, event_id)
        return False, f"그룹 공지 처리 실패: {type(exc).__name__}"

    # 고정 설정
    try:
        if kind == "start" and event["pin_start_notice"]:
            await application.bot.pin_chat_message(int(group_id), message_id, disable_notification=True)
        elif kind == "end" and event["pin_end_notice"]:
            await application.bot.pin_chat_message(int(group_id), message_id, disable_notification=True)
    except Exception:
        logger.exception("공지 고정 실패 kind=%s event_id=%s", kind, event_id)

    if kind == "pre":
        set_event_field(event_id, "pre_notice_announced", 1)
        return True, "그룹 예고공지를 반영했습니다."
    if kind == "start":
        set_event_field(event_id, "start_announced", 1)
        return True, "그룹 시작공지를 반영했습니다."

    set_event_field(event_id, "end_announced", 1)
    await disable_start_notice_button(application, get_event(event_id))
    return True, "그룹 마감공지를 반영했습니다."


async def scheduler_loop(application: Application):
    await asyncio.sleep(3)
    logger.info("KST 이벤트 스케줄러 시작")
    while True:
        try:
            now = now_dt()
            with db_connect() as conn:
                rows = conn.execute("SELECT * FROM events WHERE status!='deleted'").fetchall()

            for row in rows:
                event = get_event(row["id"])
                if not event:
                    continue

                start = parse_kst(event["start_at"])
                end = parse_kst(event["deadline_at"])


                # Railway 재시작 등으로 시작시간을 놓쳤어도,
                # 아직 마감 전이면 이벤트 활성화 + 시작공지 복구
                if start and end and start <= now < end:
                    if event["status"] in {"draft", "scheduled"}:
                        start_event(event["id"], manual=False)
                        event = get_event(event["id"])

                    if event and not event["start_announced"]:
                        await send_group_notice(application, event["id"], "start", manual=False)

                # 마감시간 도달 시 무조건 자동 종료
                if end and now >= end:
                    event = get_event(event["id"])
                    if event and event["status"] != "ended":
                        end_event(event["id"])
                        event = get_event(event["id"])

                    # 시작공지 참여버튼 즉시 제거
                    if event:
                        await disable_start_notice_button(application, event)

                    # Railway 재시작 후 마감공지를 놓친 경우에도 복구
                    if event and not event["end_announced"]:
                        await send_group_notice(application, event["id"], "end", manual=False)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("스케줄러 오류")

        await asyncio.sleep(20)


async def post_init(application: Application) -> None:
    application.create_task(scheduler_loop(application), name="event_scheduler")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    args = context.args or []
    if args and args[0].startswith("event_"):
        try:
            event_id = int(args[0].split("_", 1)[1])
            event = get_event(event_id)
            if event and event_is_open(event):
                await send_event_card(context.bot, update.effective_chat.id, get_event(event_id), member_event_keyboard(get_event(event_id)))
                return
        except Exception:
            pass
    events = get_active_events()
    if not events:
        await update.effective_message.reply_text(no_event_card(), parse_mode=ParseMode.HTML, reply_markup=member_no_event_keyboard())
        return
    await update.effective_message.reply_text("<b>진행 중인 이벤트를 선택해주세요.</b>", parse_mode=ParseMode.HTML, reply_markup=member_event_list_keyboard(events))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("관리자만 사용할 수 있습니다.")
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("관리자 메뉴는 봇 개인채팅에서 /admin 으로 이용해주세요.")
        return
    context.user_data.clear()
    await update.effective_message.reply_text("⚙️ <b>이벤트 관리자 메뉴</b>\n\n이벤트를 등록하거나 관리해주세요.", parse_mode=ParseMode.HTML, reply_markup=admin_home_keyboard())


async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("등록할 그룹방에서 /setgroup 을 입력해주세요.")
        return
    set_setting("group_id", str(update.effective_chat.id))
    set_setting("group_title", update.effective_chat.title or "그룹")
    await update.effective_message.reply_text(
        f"✅ 이 그룹을 이벤트 공지방으로 등록했습니다.\n\n그룹: {html.escape(update.effective_chat.title or '그룹')}\nID: <code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("✅ 신사 이벤트 참여봇 V12.5 STRICT TIME 정상 작동 중")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(status_card(get_latest_user_application(update.effective_user.id)), parse_mode=ParseMode.HTML)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    # 관리자 공지별 사진 등록 상태
    if is_admin(user.id) and context.user_data.get("notice_media_event_id"):
        event_id = context.user_data["notice_media_event_id"]
        kind = context.user_data.get("notice_media_kind")
        field = {"pre":"pre_media_file_id", "start":"start_media_file_id", "end":"end_media_file_id"}[kind]
        type_field = {"pre":"pre_media_type", "start":"start_media_type", "end":"end_media_type"}[kind]
        set_event_field(event_id, field, message.photo[-1].file_id)
        set_event_field(event_id, type_field, "photo")
        context.user_data.clear()
        await message.reply_text("공지용 이미지를 저장했습니다.", reply_markup=event_manage_keyboard(get_event(event_id)))
        return

    # 관리자 이벤트 대표 이미지 등록: 사진/GIF 합계 최대 5개
    if is_admin(user.id) and context.user_data.get("media_event_id"):
        event_id = context.user_data["media_event_id"]
        added = add_event_media(event_id, message.photo[-1].file_id, "photo")
        count = event_media_count(event_id)
        if not added:
            await message.reply_text(
                "대표 이미지/GIF는 최대 5개까지 등록할 수 있습니다.",
                reply_markup=event_media_manage_keyboard(event_id),
            )
            return
        await message.reply_text(
            f"대표 이미지가 등록되었습니다.\n\n현재 등록: {count}/5개\n"
            "사진이나 GIF를 더 보내거나 등록 완료를 눌러주세요.",
            reply_markup=event_media_manage_keyboard(event_id),
        )
        return

    application_id = context.user_data.get("collecting_application_id")
    if not application_id:
        with db_connect() as conn:
            row = conn.execute("SELECT id FROM applications_v6 WHERE user_id=? AND status='collecting' ORDER BY id DESC LIMIT 1", (user.id,)).fetchone()
        application_id = row["id"] if row else None
    if not application_id:
        return

    approw = get_application(application_id)
    if not approw or approw["user_id"] != user.id or approw["status"] != "collecting":
        context.user_data.pop("collecting_application_id", None)
        return
    event = get_event(approw["event_id"])
    if not event or not event_is_open(event):
        await message.reply_text("현재 이벤트 참여 시간이 아닙니다. 시작시간 이후부터 마감시간 전까지만 신청할 수 있습니다.")
        return
    max_photos = 5
    added = add_application_photo(application_id, message.photo[-1].file_id, max_photos)
    count = application_photo_count(application_id)
    if not added:
        await message.reply_text("인증 이미지는 최대 5장까지 등록할 수 있습니다.", reply_markup=submission_keyboard(application_id))
        return
    await message.reply_text(f"인증 이미지가 등록되었습니다.\n\n현재 등록: {count}/5장\n\n이미지를 더 보내거나 인증 제출을 눌러주세요.", reply_markup=submission_keyboard(application_id))


async def animation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return

    notice_event_id = context.user_data.get("notice_media_event_id")
    if notice_event_id:
        kind = context.user_data.get("notice_media_kind")
        field = {"pre":"pre_media_file_id", "start":"start_media_file_id", "end":"end_media_file_id"}[kind]
        type_field = {"pre":"pre_media_type", "start":"start_media_type", "end":"end_media_type"}[kind]
        set_event_field(notice_event_id, field, update.effective_message.animation.file_id)
        set_event_field(notice_event_id, type_field, "animation")
        context.user_data.clear()
        await update.effective_message.reply_text("공지용 GIF를 저장했습니다.", reply_markup=event_manage_keyboard(get_event(notice_event_id)))
        return

    event_id = context.user_data.get("media_event_id")
    if not event_id:
        return
    added = add_event_media(event_id, update.effective_message.animation.file_id, "animation")
    count = event_media_count(event_id)
    if not added:
        await update.effective_message.reply_text(
            "대표 이미지/GIF는 최대 5개까지 등록할 수 있습니다.",
            reply_markup=event_media_manage_keyboard(event_id),
        )
        return
    await update.effective_message.reply_text(
        f"대표 GIF가 등록되었습니다.\n\n현재 등록: {count}/5개\n"
        "사진이나 GIF를 더 보내거나 등록 완료를 눌러주세요.",
        reply_markup=event_media_manage_keyboard(event_id),
    )


async def send_application_to_admin(context: ContextTypes.DEFAULT_TYPE, application: sqlite3.Row, photos: list[str]) -> None:
    event = get_event(application["event_id"])
    if not event:
        raise ValueError("이벤트를 찾을 수 없습니다.")

    media = []
    for i, file_id in enumerate(photos[:5]):
        caption = None
        if i == 0:
            caption = (
                "<b>이벤트 참가 신청</b>\n"
                f"{CARD_LINE}\n\n"
                f"<b>{field_prefix(event,'emoji_title')}{html.escape(application['event_title'])}</b>\n\n"
                f"신청번호 : <code>#{application['id']}</code>\n"
                f"회원 : {html.escape(application['name'] or '이름 없음')}\n"
                f"아이디 : {html.escape(application['username'] or '없음')}\n"
                f"회원 ID : <code>{application['user_id']}</code>\n"
                f"인증 이미지 : {len(photos[:5])}장\n"
                f"{CARD_LINE}"
            )

        media.append(
            InputMediaPhoto(
                media=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML if caption else None,
            )
        )

    await context.bot.send_media_group(chat_id=ADMIN_ID, media=media)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"<b>신청 #{application['id']} 처리</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_application_keyboard(application["id"]),
    )


async def send_pending(message, event_id: Optional[int] = None) -> None:
    with db_connect() as conn:
        if event_id is None:
            rows = conn.execute("SELECT * FROM applications_v6 WHERE status='pending' ORDER BY id DESC LIMIT 50").fetchall()
        else:
            rows = conn.execute("SELECT * FROM applications_v6 WHERE status='pending' AND event_id=? ORDER BY id DESC LIMIT 50", (event_id,)).fetchall()
    if not rows:
        await message.reply_text("📭 승인 대기 신청이 없습니다.", reply_markup=event_manage_keyboard(get_event(event_id)) if event_id else admin_home_keyboard())
        return
    lines = ["<b>승인 대기 목록</b>\n"]
    for row in rows:
        lines.append(f"📌 #{row['id']} / {html.escape(row['event_title'])}\n👤 {html.escape(row['name'] or '-')}\n🆔 <code>{row['user_id']}</code>\n📸 {application_photo_count(row['id'])}장\n──────────────")
    await message.reply_text("\n".join(lines)[:4000], parse_mode=ParseMode.HTML, reply_markup=event_manage_keyboard(get_event(event_id)) if event_id else admin_home_keyboard())


async def send_stats(message, event_id: Optional[int] = None) -> None:
    with db_connect() as conn:
        if event_id is None:
            rows = conn.execute("SELECT status,COUNT(*) count FROM applications_v6 GROUP BY status").fetchall()
        else:
            rows = conn.execute("SELECT status,COUNT(*) count FROM applications_v6 WHERE event_id=? GROUP BY status", (event_id,)).fetchall()
    counts = {k: 0 for k in STATUS_TEXT}
    for row in rows:
        counts[row["status"]] = row["count"]
    event = get_event(event_id) if event_id else None
    await message.reply_text(
        "<b>이벤트 신청 현황</b>\n\n"
        f"🎉 {event_title_html(event) if event else '전체 이벤트'}\n\n"
        f"📸 사진 등록 중 : {counts.get('collecting',0)}건\n"
        f"⏳ 승인 대기 : {counts.get('pending',0)}건\n"
        f"✅ 승인 : {counts.get('approved',0)}건\n"
        f"❌ 거절 : {counts.get('rejected',0)}건\n"
        f"⚠️ 전달 실패 : {counts.get('notify_failed',0)}건",
        parse_mode=ParseMode.HTML,
        reply_markup=event_manage_keyboard(event) if event else admin_home_keyboard(),
    )


async def callback_handler_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass
    data = query.data or ""

    # ---------------- 회원 ----------------
    if data.startswith("user:"):
        parts = data.split(":")
        action = parts[1]
        if action == "status":
            await query.message.reply_text(status_card(get_latest_user_application(query.from_user.id)), parse_mode=ParseMode.HTML, reply_markup=member_no_event_keyboard())
            return
        if action == "event_list":
            events = get_active_events()
            await query.edit_message_text("<b>진행 중인 이벤트를 선택해주세요.</b>" if events else no_event_card(), parse_mode=ParseMode.HTML, reply_markup=member_event_list_keyboard(events) if events else member_no_event_keyboard())
            return
        if action == "event":
            event_id = int(parts[2])
            event = get_event(event_id)
            if not event or not event_is_open(event):
                await query.answer("현재 진행 중인 이벤트가 아닙니다.", show_alert=True)
                return
            await send_event_card(context.bot, query.from_user.id, get_event(event_id), member_event_keyboard(get_event(event_id)))
            return
        if action == "apply":
            event_id = int(parts[2])
            event = get_event(event_id)
            if not event or not event_is_open(event):
                await query.message.reply_text("현재 이벤트 참여 시간이 아닙니다.\n\n설정된 시작시간 이후부터 마감시간 전까지만 참여할 수 있습니다.", parse_mode=ParseMode.HTML)
                return
            existing = get_user_event_application(event_id, query.from_user.id)
            if existing and existing["status"] in {"collecting", "pending", "approved"}:
                await query.message.reply_text(status_card(existing), parse_mode=ParseMode.HTML)
                return
            application_id = create_application(event, query.from_user, "image")
            context.user_data["collecting_application_id"] = application_id
            await query.message.reply_text(
                "<b>이벤트 참여 인증</b>\n\n"
                f"<b>{field_prefix(event,'emoji_conditions')}이벤트 조건</b>\n"
                f"{event_conditions_html(event)}\n\n"
                "인증 이미지를 1~5장 등록해주세요.\n"
                "이미지를 모두 보낸 뒤 인증 제출을 눌러주세요.",
                parse_mode=ParseMode.HTML,
                reply_markup=submission_keyboard(application_id),
            )
            return

    if data.startswith("submit:"):
        _, action, aid = data.split(":")
        application_id = int(aid)
        approw = get_application(application_id)
        if not approw or approw["user_id"] != query.from_user.id or approw["status"] != "collecting":
            await query.answer("처리할 수 없는 신청입니다.", show_alert=True)
            return
        if action == "cancel":
            delete_application(application_id)
            context.user_data.pop("collecting_application_id", None)
            await query.edit_message_text("이벤트 참가 신청을 취소했습니다.")
            return
        event = get_event(approw["event_id"])
        if not event or not event_is_open(event):
            await query.answer("현재 이벤트 참여 시간이 아닙니다.", show_alert=True)
            return
        photos = get_application_photos(application_id)
        if not photos:
            await query.answer("인증사진을 1장 이상 등록해주세요.", show_alert=True)
            return
        try:
            await send_application_to_admin(context, approw, photos)
        except Exception:
            logger.exception("관리자 신청 전달 실패 application_id=%s", application_id)
            set_application_status(application_id, "notify_failed")
            await query.message.reply_text("⚠️ 담당자에게 신청을 전달하지 못했습니다. 잠시 후 다시 신청해주세요.")
            return
        set_application_status(application_id, "pending")
        context.user_data.pop("collecting_application_id", None)
        await query.edit_message_text(f"📨 <b>참가 신청이 접수되었습니다.</b>\n\n신청번호 : <code>#{application_id}</code>\n인증사진 : {len(photos)}장\n\n관리자 확인 후 결과를 안내드립니다.", parse_mode=ParseMode.HTML)
        return

    # ---------------- 관리자 ----------------
    if not is_admin(query.from_user.id):
        await query.answer("관리자만 사용할 수 있습니다.", show_alert=True)
        return

    if data == "admin:home":
        context.user_data.clear()
        await query.edit_message_text("⚙️ <b>이벤트 관리자 메뉴</b>\n\n이벤트를 등록하거나 관리해주세요.", parse_mode=ParseMode.HTML, reply_markup=admin_home_keyboard())
        return
    if data == "admin:close":
        context.user_data.clear()
        await query.edit_message_text("관리자 메뉴를 닫았습니다.")
        return
    if data == "admin:event_list":
        await query.edit_message_text("📚 <b>전체 이벤트 관리</b>\n\n관리할 이벤트를 선택해주세요.", parse_mode=ParseMode.HTML, reply_markup=admin_event_list_keyboard())
        return
    if data == "admin:new_event":
        event_id = create_event()
        event = get_event(event_id)
        await query.edit_message_text(event_card(event, admin=True), parse_mode=ParseMode.HTML, reply_markup=event_manage_keyboard(event))
        return
    if data == "admin:pending":
        await send_pending(query.message)
        return
    if data == "admin:stats":
        await send_stats(query.message)
        return
    if data == "admin:emoji":
        context.user_data.clear()
        await query.edit_message_text(
            "<b>이모지 설정</b>\n\n"
            "이벤트 카드 항목 이모지와 내 신청상태 버튼 이모지를 한 곳에서 관리할 수 있습니다.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_emoji_keyboard(),
        )
        return

    if data.startswith("emoji_admin:"):
        parts = data.split(":")
        action = parts[1]

        if action == "event_list":
            await query.edit_message_text(
                "<b>이벤트 항목 이모지 설정</b>\n\n"
                "이모지를 설정할 이벤트를 선택해주세요.\n\n"
                "설정 가능 항목\n"
                "이벤트명 / 이벤트 내용 / 참가시간 / 자동 시작 / 참여 마감시간 / 참여조건 / 인증안내",
                parse_mode=ParseMode.HTML,
                reply_markup=emoji_event_select_keyboard(),
            )
            return

        if action == "event":
            event_id = int(parts[2])
            event = get_event(event_id)
            if not event:
                await query.answer("이벤트를 찾을 수 없습니다.", show_alert=True)
                return
            await query.edit_message_text(
                f"<b>{event_title_html(event)}</b>\n\n"
                "설정할 항목을 선택한 뒤 일반 이모지 또는 텔레그램 프리미엄 이모지를 보내주세요.",
                parse_mode=ParseMode.HTML,
                reply_markup=emoji_manage_keyboard(event_id),
            )
            return

        if action == "none":
            await query.answer("먼저 이벤트를 등록해주세요.", show_alert=True)
            return

    if data == "admin:no_event_emoji":
        context.user_data.clear()
        context.user_data["edit_no_event_emoji"] = True
        await query.edit_message_text(
            "<b>대기화면 이모지</b>\n\n"
            "현재 참여할 이벤트가 없을 때 제목 앞에 표시할 "
            "일반 이모지 또는 프리미엄 이모지 하나를 보내주세요.\n"
            "제거하려면 <code>없음</code> 입력.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_emoji_keyboard(),
        )
        return

    if data == "admin:status_emoji":
        context.user_data.clear()
        context.user_data["edit_status_button_emoji"] = True
        await query.edit_message_text(
            "<b>내 신청 상태 버튼 이모지</b>\n\n"
            "일반 이모지 또는 프리미엄 이모지 하나를 보내주세요.\n"
            "제거하려면 <code>없음</code> 입력.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_emoji_keyboard(),
        )
        return
    if data == "admin:group" or data.startswith("group:"):
        await query.answer(
            "그룹 공지 설정 메뉴는 사용하지 않습니다. 그룹에서 /setgroup 한 번만 등록하면 자동 공지됩니다.",
            show_alert=True,
        )
        return

    if data.startswith("event:"):
        _, action, event_id_text = data.split(":")
        event_id = int(event_id_text)
        event = get_event(event_id)
        if not event:
            return
        if action == "manage":
            await query.edit_message_text(event_card(event, admin=True), parse_mode=ParseMode.HTML, reply_markup=event_manage_keyboard(event)); return
        if action == "preview":
            await send_event_card(context.bot, query.from_user.id, event, simple_back(event_id)); return
        if action == "pending":
            await send_pending(query.message, event_id); return
        if action == "stats":
            await send_stats(query.message, event_id); return
        if action == "start":
            start_event(event_id)
            event = get_event(event_id)
            await query.edit_message_text(event_card(event, admin=True)+"\n\n✅ 회원 신청이 가능하도록 시작했습니다.", parse_mode=ParseMode.HTML, reply_markup=event_manage_keyboard(event)); return
        if action == "end":
            end_event(event_id)
            event = get_event(event_id)
            await disable_start_notice_button(context.application, event)
            await query.edit_message_text(
                event_card(event, admin=True)+"\n\n이벤트를 종료했습니다.",
                parse_mode=ParseMode.HTML,
                reply_markup=event_manage_keyboard(event)
            )
            return
        if action == "delete_confirm":
            await query.edit_message_text("🗑 <b>이벤트 삭제 확인</b>\n\n기존 신청 기록은 유지됩니다.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 정말 삭제", callback_data=f"event:delete:{event_id}")],[InlineKeyboardButton("⬅ 취소", callback_data=f"event:manage:{event_id}")]])); return
        if action == "delete":
            delete_event(event_id)
            await query.edit_message_text("🗑 이벤트를 삭제했습니다.", reply_markup=admin_event_list_keyboard()); return

    if data.startswith("edit:"):
        _, field, event_id_text = data.split(":")
        event_id = int(event_id_text)
        labels = {"title":"이벤트명", "content":"이벤트 내용", "participation_time":"이벤트 시간", "conditions":"이벤트 조건", "pre_notice":"예고공지 문구", "start_notice":"시작공지 문구", "end_notice":"종료공지 문구", "approval":"승인 문구", "rejection":"거절 문구"}
        context.user_data.clear(); context.user_data["edit_event_id"] = event_id; context.user_data["edit_event_field"] = field
        extra = ""
        if field == "pre_notice":
            extra = "\n\n사용 가능 변수: <code>{title}</code> <code>{start_at}</code> <code>{deadline_at}</code>"
        elif field == "start_notice":
            extra = "\n\n사용 가능 변수: <code>{title}</code> <code>{start_at}</code> <code>{deadline_at}</code> <code>{conditions}</code> <code>{proof_guide}</code>"
        elif field == "end_notice":
            extra = "\n\n사용 가능 변수: <code>{title}</code> <code>{deadline_at}</code>"
        await query.edit_message_text(f"<b>{labels.get(field,field)} 수정</b>\n\n새 내용을 보내주세요. 일반/프리미엄 이모지도 사용할 수 있습니다.{extra}", parse_mode=ParseMode.HTML, reply_markup=simple_back(event_id)); return

    if data.startswith("schedule:"):
        _, which, event_id_text = data.split(":")
        event_id = int(event_id_text)
        context.user_data.clear(); context.user_data["schedule_event_id"] = event_id; context.user_data["schedule_which"] = which
        label = "자동 시작시간" if which == "start" else "마감시간"
        await query.edit_message_text(f"<b>{label} 설정</b>\n\n한국시간 기준 <code>YYYY-MM-DD HH:MM</code> 형식으로 입력해주세요.\n예: <code>2026-08-07 18:00</code>\n\n자동 시작을 없애려면 <code>없음</code> 입력.", parse_mode=ParseMode.HTML, reply_markup=simple_back(event_id)); return

    if data.startswith("proofmode:") or data.startswith("proofset:"):
        await query.answer("V9.6부터 인증방식 선택 없이 이미지 1~5장으로 통합되었습니다.", show_alert=True)
        return

    if data.startswith("media:"):
        _, action, event_id_text = data.split(":")
        event_id = int(event_id_text)

        if action == "set":
            context.user_data.clear()
            context.user_data["media_event_id"] = event_id
            count = event_media_count(event_id)
            await query.edit_message_text(
                f"<b>대표 이미지/GIF 설정</b>\n\n"
                f"현재 등록: {count}/5개\n\n"
                "사진 또는 GIF를 보내주세요.\n"
                "사진과 GIF를 섞어서 최대 5개까지 등록할 수 있습니다.\n"
                "등록이 끝나면 등록 완료를 눌러주세요.",
                parse_mode=ParseMode.HTML,
                reply_markup=event_media_manage_keyboard(event_id),
            )
            return

        if action == "done":
            context.user_data.clear()
            await query.edit_message_text(
                f"대표 이미지/GIF 설정 완료\n\n현재 등록: {event_media_count(event_id)}/5개",
                reply_markup=event_manage_keyboard(get_event(event_id)),
            )
            return

        if action == "clear":
            clear_event_media(event_id)
            context.user_data.clear()
            context.user_data["media_event_id"] = event_id
            await query.edit_message_text(
                "대표 이미지/GIF를 모두 삭제했습니다.\n\n"
                "새 사진 또는 GIF를 보내거나 등록 완료를 눌러주세요.",
                reply_markup=event_media_manage_keyboard(event_id),
            )
            return

    if data.startswith("notice_media:"):
        _, kind, event_id_text = data.split(":")
        event_id = int(event_id_text)
        context.user_data.clear()
        context.user_data["notice_media_event_id"] = event_id
        context.user_data["notice_media_kind"] = kind
        label = {"pre":"10분전 예고공지", "start":"시작공지", "end":"마감공지"}[kind]
        await query.edit_message_text(
            f"<b>{label} 이미지/GIF 설정</b>\n\n"
            "사용할 사진 또는 GIF를 보내주세요.\n"
            "제거하려면 아래 버튼을 누르세요.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("공지 미디어 제거", callback_data=f"notice_media_clear:{kind}:{event_id}")],
                [InlineKeyboardButton("이벤트 관리", callback_data=f"event:manage:{event_id}")]
            ])
        )
        return

    if data.startswith("notice_media_clear:"):
        _, kind, event_id_text = data.split(":")
        event_id = int(event_id_text)
        field = {"pre":"pre_media_file_id", "start":"start_media_file_id", "end":"end_media_file_id"}[kind]
        type_field = {"pre":"pre_media_type", "start":"start_media_type", "end":"end_media_type"}[kind]
        set_event_field(event_id, field, "")
        set_event_field(event_id, type_field, "")
        context.user_data.clear()
        await query.edit_message_text("공지 미디어를 제거했습니다.", reply_markup=event_manage_keyboard(get_event(event_id)))
        return

    if data.startswith("toggle:"):
        _, which, event_id_text = data.split(":")
        event_id = int(event_id_text)
        event = get_event(event_id)
        field_map = {
            "pre_notice": "pre_notice_enabled",
            "start_notice": "announce_start",
            "end_notice": "announce_end",
            "pin_start": "pin_start_notice",
            "pin_end": "pin_end_notice",
        }
        field = field_map.get(which)
        if not field:
            await query.answer("잘못된 설정입니다.", show_alert=True)
            return
        set_event_field(event_id, field, 0 if event[field] else 1)
        await query.edit_message_text(
            event_card(get_event(event_id), admin=True),
            parse_mode=ParseMode.HTML,
            reply_markup=event_manage_keyboard(get_event(event_id))
        )
        return

    if data.startswith("announce:"):
        _, kind, event_id_text = data.split(":")
        ok, msg = await send_group_notice(context.application, int(event_id_text), kind, manual=True)
        await query.message.reply_text(msg, reply_markup=event_manage_keyboard(get_event(int(event_id_text)))); return

    if data.startswith("emoji:"):
        _, action, event_id_text = data.split(":")
        event_id = int(event_id_text)
        if action == "menu":
            await query.edit_message_text("<b>이모지 설정</b>\n\n항목을 선택한 뒤 일반 이모지 또는 텔레그램 프리미엄 이모지 하나를 보내주세요.", parse_mode=ParseMode.HTML, reply_markup=emoji_manage_keyboard(event_id)); return
        if action == "clear":
            with db_connect() as conn:
                conn.execute("UPDATE events SET emoji_title='',emoji_title_id='',emoji_content='',emoji_time='',emoji_start='',emoji_deadline='',emoji_conditions='',emoji_proof='',emoji_approval='',emoji_rejection='',updated_at=? WHERE id=?", (now_kst(), event_id)); conn.commit()
            await query.edit_message_text("모든 항목 이모지를 제거했습니다.", reply_markup=emoji_manage_keyboard(event_id)); return

    if data.startswith("emoji_edit:"):
        _, field, event_id_text = data.split(":")
        event_id = int(event_id_text)
        context.user_data.clear(); context.user_data["edit_emoji_event_id"] = event_id; context.user_data["edit_emoji_field"] = field
        await query.edit_message_text("<b>이모지 등록</b>\n\n사용할 이모지 하나를 보내주세요.\n없애려면 <code>없음</code> 입력.", parse_mode=ParseMode.HTML, reply_markup=emoji_manage_keyboard(event_id)); return

    if data.startswith("application:"):
        _, action, aid = data.split(":")
        application_id = int(aid); approw = get_application(application_id)
        if not approw:
            await query.answer("신청 내역을 찾을 수 없습니다.", show_alert=True); return
        if approw["status"] in {"approved", "rejected"}:
            await query.answer("이미 처리된 신청입니다.", show_alert=True); return
        if action == "approve":
            event = get_event(approw["event_id"])
            if not event:
                await query.answer("이벤트를 찾을 수 없습니다.", show_alert=True)
                return

            set_application_status(application_id, "approved", query.from_user.id)

            approval_body = (
                event["approval_html"]
                or "<b>이벤트 승인이 완료되었습니다.</b>"
            )
            approval_prefix = field_prefix(event, "emoji_approval")
            member_text = f"{approval_prefix}{approval_body}" if approval_prefix else approval_body

            try:
                await context.bot.send_message(
                    approw["user_id"],
                    member_text,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("회원 승인 알림 실패")

            await query.edit_message_text(
                f"참가 승인 완료\n\n"
                f"신청번호 : #{application_id}\n"
                f"회원 ID : {approw['user_id']}\n"
                f"처리시간 : {now_kst()}"
            )
            return

        await query.edit_message_text(
            f"신청 #{application_id} 거절 사유를 선택해주세요.",
            reply_markup=reject_reason_keyboard(application_id),
        )
        return

    if data.startswith("reject:"):
        _, reason_key, aid = data.split(":")
        application_id = int(aid)
        if reason_key == "custom":
            context.user_data.clear(); context.user_data["reject_custom_id"] = application_id
            await query.edit_message_text("거절 사유를 직접 입력해주세요."); return
        await finish_reject(context, application_id, REJECT_REASONS.get(reason_key, "관리자 확인 결과 거절되었습니다."), query.from_user.id)
        await query.edit_message_text(f"❌ 참가 거절 완료\n\n신청번호 : #{application_id}\n사유 : {REJECT_REASONS.get(reason_key,'')}"); return


async def finish_reject(context: ContextTypes.DEFAULT_TYPE, application_id: int, reason: str, admin_id: int) -> None:
    approw = get_application(application_id)
    if not approw or approw["status"] in {"approved", "rejected"}:
        return

    event = get_event(approw["event_id"])
    if not event:
        return

    set_application_status(application_id, "rejected", admin_id, reason)

    rejection_body = (
        event["rejection_html"]
        or "<b>이벤트 신청이 거절되었습니다.</b>"
    )
    rejection_prefix = field_prefix(event, "emoji_rejection")
    base = f"{rejection_prefix}{rejection_body}" if rejection_prefix else rejection_body

    try:
        await context.bot.send_message(
            approw["user_id"],
            f"{base}\n\n<b>사유</b>\n{html.escape(reason)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("회원 거절 알림 실패")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await callback_handler_impl(update, context)
    except Exception as exc:
        logger.exception("버튼 처리 오류 callback_data=%s", query.data if query else None)
        if query:
            try:
                await query.answer("버튼 처리 중 오류가 발생했습니다.", show_alert=True)
            except Exception:
                pass
            try:
                await query.message.reply_text(f"⚠️ 버튼 처리 중 오류가 발생했습니다.\n\n오류 종류: <code>{html.escape(type(exc).__name__)}</code>\nRailway 로그에서 자세한 내용을 확인해주세요.", parse_mode=ParseMode.HTML, reply_markup=admin_home_keyboard() if is_admin(query.from_user.id) else None)
            except Exception:
                pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not is_admin(user.id):
        return

    if context.user_data.get("edit_no_event_emoji"):
        value = (message.text or "").strip()
        if value == "없음":
            set_setting("no_event_emoji", "")
            set_setting("no_event_emoji_id", "")
        else:
            rich = message_to_html(message)
            emoji_value = button_emoji_from_html(rich)
            custom_id = extract_custom_emoji_id_from_message(message)
            if not emoji_value and not custom_id:
                await message.reply_text("일반 이모지 또는 프리미엄 이모지를 보내주세요.")
                return
            set_setting("no_event_emoji", emoji_value)
            set_setting("no_event_emoji_id", custom_id)
        context.user_data.clear()
        await message.reply_text(
            "대기화면 이모지를 저장했습니다.",
            reply_markup=admin_emoji_keyboard(),
        )
        return

    if context.user_data.get("edit_status_button_emoji"):
        value = (message.text or "").strip()
        if value == "없음":
            set_setting("status_button_emoji", ""); set_setting("status_button_emoji_id", "")
        else:
            rich = message_to_html(message)
            set_setting("status_button_emoji", button_emoji_from_html(rich))
            set_setting("status_button_emoji_id", extract_custom_emoji_id_from_message(message))
        context.user_data.clear(); await message.reply_text("내 신청상태 버튼 이모지를 저장했습니다.", reply_markup=admin_home_keyboard()); return

    if context.user_data.get("edit_group_text"):
        which = context.user_data["edit_group_text"]
        value = (message.text or "").strip()
        set_setting("group_start_text" if which == "start" else "group_end_text", "" if value == "기본" else value)
        context.user_data.clear(); await message.reply_text("✅ 그룹 공지 문구를 저장했습니다.", reply_markup=group_settings_keyboard()); return

    event_id = context.user_data.get("schedule_event_id")
    if event_id:
        which = context.user_data.get("schedule_which")
        value = (message.text or "").strip()
        if value == "없음" and which == "start":
            set_event_field(event_id, "start_at", "")
            set_event_field(event_id, "status", "draft")
        else:
            dt = parse_kst(value)
            if not dt:
                await message.reply_text("형식이 올바르지 않습니다.\n예: 2026-08-07 18:00"); return
            if which == "start":
                end = parse_kst(get_event(event_id)["deadline_at"])
                if end and dt >= end:
                    await message.reply_text("시작시간은 마감시간보다 앞이어야 합니다."); return
                set_event_field(event_id, "start_at", dt.strftime("%Y-%m-%d %H:%M")); set_event_field(event_id, "status", "scheduled" if dt > now_dt() else "active"); set_event_field(event_id, "pre_notice_announced", 0); set_event_field(event_id, "start_announced", 0)
            else:
                start = parse_kst(get_event(event_id)["start_at"])
                if start and dt <= start:
                    await message.reply_text("마감시간은 시작시간보다 뒤여야 합니다."); return
                set_event_field(event_id, "deadline_at", dt.strftime("%Y-%m-%d %H:%M")); set_event_field(event_id, "end_announced", 0)
        context.user_data.clear(); await message.reply_text("시간을 저장했습니다.", reply_markup=event_manage_keyboard(get_event(event_id))); return

    emoji_event_id = context.user_data.get("edit_emoji_event_id")
    emoji_field = context.user_data.get("edit_emoji_field")
    if emoji_event_id and emoji_field:
        value = (message.text or "").strip()
        if value == "없음":
            update_event_emoji(emoji_event_id, emoji_field, "", "")
        else:
            update_event_emoji(emoji_event_id, emoji_field, message_to_html(message), extract_custom_emoji_id_from_message(message))
        context.user_data.clear(); await message.reply_text("이모지 설정을 저장했습니다.", reply_markup=emoji_manage_keyboard(emoji_event_id)); return

    edit_event_id = context.user_data.get("edit_event_id")
    field = context.user_data.get("edit_event_field")
    if edit_event_id and field:
        html_value = message_to_html(message); plain_value = message.text or plain_from_html(html_value)
        if not plain_value.strip():
            await message.reply_text("빈 내용은 저장할 수 없습니다."); return
        update_event_text(edit_event_id, field, plain_value, html_value)
        context.user_data.clear(); await message.reply_text("✅ 수정 내용을 저장했습니다.", reply_markup=event_manage_keyboard(get_event(edit_event_id))); return

    reject_id = context.user_data.get("reject_custom_id")
    if reject_id:
        reason = (message.text or "").strip()
        if not reason:
            await message.reply_text("거절 사유를 입력해주세요."); return
        await finish_reject(context, reject_id, reason, user.id)
        context.user_data.clear(); await message.reply_text(f"❌ 참가 거절 완료\n\n신청번호 : #{reject_id}\n사유 : {reason}", reply_markup=admin_home_keyboard()); return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("업데이트 처리 중 오류", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN 환경변수가 없습니다.")
    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID 환경변수가 없습니다.")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("setgroup", setgroup_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.ANIMATION, animation_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    logger.info("신사 이벤트 참여봇 V12.5 STRICT TIME 실행 | ADMIN_ID=%s | DB=%s", ADMIN_ID, DB_FILE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
