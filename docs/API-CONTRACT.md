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

## 메뉴 (Menu)

### `GET /api/v1/menu`
판매 중인 메뉴 목록(POS 주문 화면용).
```json
200 [ { "id":1, "name":"아메리카노", "price":4000,
        "category":"coffee", "category_display":"커피", "emoji":"☕",
        "temp_option":"hotice", "decaf_available":true, "oatmilk_available":false }, ... ]
```
- `temp_option`: `hotice`(핫/아이스 선택) · `ice`(아이스만) · `none`(디저트)
- `decaf_available`(커피류)·`oatmilk_available`(라떼류): 옵션 추가 시 각 +`option_price`(기본 500원)
- 세트 할인: 커피(음료)+디저트 동시 주문 시 `min(음료수, 디저트수) × set_discount_amount`(기본 500원)

---

## 거래 / 결제 (Transaction)

> **주문 기반 결제:** POS 키오스크는 `items`(메뉴+수량)를 보내고, **서버가 메뉴
> 가격으로 총액을 계산**한다(금액 위변조 방지). 회원 식별은 결제 시점에
> 고객이 단말기에 입력한 연락처로 조회한다.

### `POST /api/v1/transactions/quote`
결제 전 **견적**: 사용 가능 포인트·적립 예상치를 미리 계산(승인 X).
```json
요청 { "member_id": 1, "gross_amount": 6500, "points_to_use": 1000 }
200  { "gross_amount": 6500, "points_used": 1000, "net_amount": 5500,
       "points_earned": 275, "available_points": 3200 }
```

### `POST /api/v1/transactions`
거래 생성 + 결제 확정(현금/Toss). `items` 또는 `gross_amount` 중 하나 필수.
`items`가 있으면 서버가 총액을 계산하고 주문 항목을 기록한다. `member_id`가
없으면 비회원 결제(적립·게이미피케이션 없음).
```json
요청 {
  "member_id": 1,
  "items": [
    { "menu_item_id": 2, "quantity": 1, "temperature": "hot", "decaf": true, "oatmilk": true },
    { "menu_item_id": 30, "quantity": 2 }
  ],
  "points_to_use": 1000,
  "payment_method": "TOSS_CARD",
  "toss_payment_key": "tviva20250101...", "toss_order_id": "kiosk-...."
}
- 라인별 옵션: `temperature`(`hot`/`ice`), `decaf`, `oatmilk`. 서버가 단가(옵션 포함)와
  세트 할인을 계산한다. 응답에 `discount`, 주문 항목(`items[]`, `option_label` 포함)이 담긴다.
--- (직접 금액 방식도 지원) ---
요청 {
  "member_id": 1, "gross_amount": 6500, "points_to_use": 1000,
  "payment_method": "TOSS_CARD"
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

## 점주 대시보드 (Dashboard)

### `GET /api/v1/dashboard/stats`
점주 대시보드 핵심 지표 집계.
```json
200 {
  "members": { "total": 15, "opt_in": 12, "opt_in_rate": 80.0,
               "new_30d": 15, "active_30d": 7,
               "tier_breakdown": { "BRONZE": 7, "SILVER": 8 } },
  "revenue": { "total": 1174000, "tx_count": 131, "avg_basket": 8961, "revenue_30d": 286500 },
  "points_outstanding": 58700,
  "trend_14d": [ { "date": "06-17", "revenue": 18500 }, ... ],
  "top_members": [ { "id":1, "name":"한가람", "tier":"SILVER", "total_spent":176500, ... } ],
  "recent_transactions": [ { "id":1, "member__name":"한가람", "net_amount":4500, ... } ]
}
```

---

## 마케팅 세그먼트 (Segment)

내가 모은 회원 데이터로 **결제사 동의 없이** 직접 타깃을 정의한다.

### `POST /api/v1/segments/preview`
저장 없이 필터로 대상 수·샘플 미리보기.
```json
요청 { "tier": "", "min_visits": 0, "min_spent": 0,
       "inactive_days": 30, "require_opt_in": true }
200  { "count": 6, "sample": [ { "id":7, "name":"최여유", "tier":"SILVER", ... } ] }
```
| 필터 | 의미 |
| --- | --- |
| `tier` | 등급(비우면 전체) |
| `min_visits` / `min_spent` | 최소 방문/누적결제 |
| `inactive_days` | N일 이상 미방문(휴면) — 윈백 대상 |
| `require_opt_in` | 마케팅 수신 동의자만(광고성 필수) |

### `GET/POST /api/v1/segments`  ·  `GET /api/v1/segments/{id}`
세그먼트 CRUD(저장).

### `GET /api/v1/segments/{id}/members`
세그먼트에 속한 회원 목록.

---

## 마케팅 캠페인 (Campaign)

### `GET/POST /api/v1/campaigns`
캠페인 생성/목록.
```json
요청 {
  "name": "휴면 컴백 쿠폰", "segment": 1, "is_ad": true,
  "message_template": "{이름}님, 보유 {포인트}P로 한 잔 어떠세요?"
}
```
> 메시지 치환 변수: `{이름}` `{포인트}` `{등급}` `{스탬프}` `{방문}`.

### `POST /api/v1/campaigns/{id}/send`
대상 회원에게 **알림톡 발송**. 광고성인데 수신 미동의면 자동 제외(skipped).
키 미설정 시 Mock 발송.
```json
200 { "id":1, "status":"sent", "recipient_count":6,
      "sent_count":6, "failed_count":0, "skipped_count":0, "sent_at":"..." }
```

### `GET /api/v1/campaigns/{id}/logs`
회원별 발송 로그(렌더된 본문·상태·사유).

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
