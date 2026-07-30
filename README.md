# 신사 이벤트 참여봇 V5

## 관리자 사용 흐름

```text
/admin
→ 새 이벤트 등록
→ 제목 수정
→ 이벤트 내용 수정
→ 참여시간 수정
→ 참여조건 수정
→ 시작 공지 수정
→ 종료 공지 수정
→ 참여불가 문구 수정
→ 미리보기
→ 이벤트 시작
```

이벤트 시작 시 `GROUP_CHAT_ID` 그룹방에 이벤트 내용, 참여시간, 참여조건이 자동 안내됩니다.

이벤트 종료 시 종료 공지가 그룹방에 전송되고, 회원이 봇을 이용하면 `현재 참여할 수 있는 이벤트가 없습니다` 문구가 표시됩니다.

종료된 이벤트는 삭제할 수 있으며 신청 이력은 보존됩니다.

## Railway Variables

```env
BOT_TOKEN=봇토큰
ADMIN_ID=관리자 숫자 ID
GROUP_CHAT_ID=그룹 숫자 ID
DB_FILE=/data/event_bot.db
```

Volume Mount Path:

```text
/data
```

Custom Start Command:

```text
python bot.py
```

## 명령어

```text
/start
/admin
/ping
/status
/history 2026-07-30
/userhistory 숫자ID
```
