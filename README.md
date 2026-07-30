# 신사 이벤트 참여봇 V6 심플버전

## 회원 흐름

```text
/start
→ 진행 중인 이벤트 확인
→ 참여 신청
→ 참여시간 및 참여조건 확인
→ 인증사진 1~5장 등록
→ 사진 제출 완료
→ 관리자 승인 또는 거절
→ 결과 문구 수신
```

## 관리자 흐름

```text
/admin
→ 이벤트 새로 등록
→ 이벤트명 수정
→ 참가시간 수정
→ 참여조건 수정
→ 승인문구 수정
→ 거절문구 수정
→ 이벤트 시작
```

종료된 이벤트는 삭제할 수 있습니다. 기존 신청 기록은 보존됩니다.

## 프리미엄 이모지

이벤트명, 참가시간, 참여조건, 승인문구, 거절문구를 수정할 때 텔레그램 프리미엄 이모지를 넣으면 HTML 형식으로 보존됩니다.

인라인 버튼 글자에는 텔레그램 제한 때문에 프리미엄 이모지를 사용할 수 없습니다.

## Railway Variables

```env
BOT_TOKEN=봇토큰
ADMIN_ID=관리자 텔레그램 숫자 ID
DB_FILE=/data/event_bot.db
```

`GROUP_CHAT_ID`는 필요하지 않습니다.

Volume Mount Path:

```text
/data
```

Custom Start Command:

```text
python bot.py
```

## 확인 명령어

```text
/ping
/start
/admin
/status
```
