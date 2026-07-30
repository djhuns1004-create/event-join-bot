# 신사 이벤트 참여봇 V4

## 주요 기능

- 신청마다 고유 신청번호 생성
- 날짜별 신청 이력 보존
- 회원 숫자 ID별 전체 참여 이력
- 사진, 사진 묶음, GIF, 이미지파일 지원
- 관리자 승인, 거절, 차단
- 처리 관리자 ID와 처리시간 저장
- 관리자 알림 실패 기록
- 오늘 참여내역 CSV 다운로드
- 기존 V2 데이터 자동 이전
- 같은 날 승인, 대기, 차단 회원 중복 신청 방지
- 거절 또는 알림 실패 회원은 재신청 가능

## 관리자 명령어

```text
/admin
/today
/list
/history
/history 2026-07-30
/userhistory 122190868
/ping
```

## Railway Variables

```env
BOT_TOKEN=봇토큰
ADMIN_ID=담당자 텔레그램 숫자 ID
DB_FILE=/data/event_bot.db
```

Volume Mount Path:

```text
/data
```

Start Command:

```text
python bot.py
```

담당자는 봇 개인채팅에서 `/start`를 한 번 실행해야 관리자 알림을 받을 수 있습니다.
