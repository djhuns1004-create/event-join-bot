# 신사 이벤트 참여봇 V6.1 FIX

## 수정 내용

- 기존 V5/V6 Railway 볼륨 DB 자동 마이그레이션
- `events` 테이블에 없는 HTML 및 승인/거절 문구 컬럼 자동 추가
- 기존 이벤트 데이터를 새 컬럼으로 자동 복사
- 이벤트 새로 등록 버튼 무반응 수정
- 이벤트 관리, 승인 대기, 신청 현황 버튼 콜백 오류 표시
- 버튼 오류 발생 시 무반응 대신 관리자에게 오류 종류 표시
- 기존 신청 기록과 이벤트 기록 유지

## Railway Variables

```env
BOT_TOKEN=봇토큰
ADMIN_ID=관리자 숫자 ID
DB_FILE=/data/event_bot.db
```

Custom Start Command:

```text
python bot.py
```

배포 후:

```text
/ping
/admin
```

`/ping` 결과가 `신사 이벤트 참여봇 V6.1 정상 작동 중`이면 새 코드가 적용된 상태입니다.
