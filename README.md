# 신사 이벤트 참여봇 V8.2 프리미엄 버튼

## 수정 내용

- 이벤트 버튼에 Telegram 프리미엄 커스텀 이모지 적용
- `내 신청 상태` 버튼에도 프리미엄 커스텀 이모지 적용
- 프리미엄 이모지의 `custom_emoji_id`를 별도 저장
- 기존 이벤트 본문에 저장된 프리미엄 이모지 ID 자동 추출
- 일반 이모지도 계속 지원
- python-telegram-bot 22.7로 업데이트

## 사용 조건

Telegram Bot API의 버튼 프리미엄 이모지는 다음 조건에서 사용할 수 있습니다.

- 봇 소유자가 Telegram Premium을 사용 중
- 봇이 직접 보낸 개인·그룹·슈퍼그룹 메시지
- 또는 Fragment 추가 사용자명을 구매한 봇

## 관리자

```text
/admin
→ 내 신청상태 버튼 이모지
```

프리미엄 이모지 하나를 보내면 버튼 아이콘으로 저장됩니다.

각 이벤트:

```text
전체 이벤트 관리
→ 이벤트 선택
→ 이모지 설정
→ 제목 이모지
```

등록한 제목 프리미엄 이모지가 회원 이벤트 버튼에 적용됩니다.

## Railway

```env
BOT_TOKEN=봇토큰
ADMIN_ID=관리자 숫자 ID
DB_FILE=/data/event_bot.db
```

Custom Start Command:

```text
python bot.py
```
