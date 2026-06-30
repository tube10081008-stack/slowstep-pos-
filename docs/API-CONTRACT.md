# 🔌 API 계약 — 슬로우스텝 멤버십 POS

Base URL: `/api/v1` · 형식: JSON · 금액: 원(KRW) 정수

> P0 스캐폴드는 인증을 생략(데모). P1에서 직원 토큰/세션 인증 추가.

---

## 회원 (Member)

### `GET /api/v1/members/lookup?phone=01012345678`
회원번호(연락처)로 조회. QR 스캔 결과를 그대로 전달.
```json
200 {
  "id": 1, "phone": "01012345678", "name": "김슬로우",
  "points": 3200, "tier": "SILVER", "total_spent": 84000,
  "visit_count": 12, "stamps": 4, "stamp_goal": 10
}
404 { "detail": "회원을 찾을 수 없습니다." }
```

### `POST /api/v1/members`
회원 가입(이름·연락처).
```json
요청 { "phone": "01012345678", "name": "김슬로우", "marketing_opt_in": true }
201  { "id": 1, "phone": "...", "name": "...", "points": 0, "tier": "BRONZE", ... }
```

### `GET /api/v1/members/{id}`
회원 상세 + 진행 중 미션 + 최근 적립 내역.

### `GET /api/v1/members/{id}/missions`
회원의 미션 진행 목록.
```json
200 [{ "mission": "이번 달 3회 방문", "progress": 2, "target": 3,
       "reward_points": 1000, "is_completed": false }]
```

---

## 거래 / 결제 (Transaction)

### `POST /api/v1/transactions/quote`
결제 전 **견적**: 사용 가능 포인트·적립 예상치를 미리 계산(승인 X).
```json
요청 { "member_id": 1, "gross_amount": 6500, "points_to_use": 1000 }
200  { "gross_amount": 6500, "points_used": 1000, "net_amount": 5500,
       "points_earned": 275, "available_points": 3200 }
```

### `POST /api/v1/transactions`
거래 생성 + 결제 확정(현금/Toss). Toss는 클라이언트 승인 후 `toss_payment_key` 전달.
```json
요청 {
  "member_id": 1, "gross_amount": 6500, "points_to_use": 1000,
  "payment_method": "TOSS_CARD",
  "toss_payment_key": "tviva20250101...", "toss_order_id": "slowstep-...."
}
201 {
  "id": 42, "status": "paid", "gross_amount": 6500,
  "points_used": 1000, "net_amount": 5500, "points_earned": 275,
  "member": { "points": 2475, "tier": "SILVER", "stamps": 5 },
  "rewards": [ { "type": "mission", "title": "3회 방문", "points": 1000 } ]
}
400 { "detail": "사용 포인트가 보유 포인트를 초과합니다." }
```

### `POST /api/v1/transactions/{id}/cancel`  *(P1)*
결제 취소/환불 + 포인트 원복.

---

## 미션 (Mission)

### `GET /api/v1/missions`
활성 미션 목록(매장 단위).

---

## 매장 (Store)

### `GET /api/v1/store`
기본 매장 설정(적립률·스탬프 목표 등). POS 초기화에 사용.

---

## Toss 웹훅  *(P1, 설계만)*

### `POST /api/v1/payments/toss/webhook`
Toss 결제 상태 변경 수신(취소·정산 등). 서명 검증 후 거래 상태 동기화.
상세는 [`TOSS-INTEGRATION.md`](./TOSS-INTEGRATION.md).

---

## 공통 에러 포맷
```json
{ "detail": "사람이 읽을 메시지", "code": "optional_machine_code" }
```
| 코드 | 의미 |
| --- | --- |
| 400 | 잘못된 요청(검증 실패) |
| 404 | 리소스 없음 |
| 409 | 멱등 충돌(중복 `toss_order_id`) |
| 502 | Toss 승인 실패 |
