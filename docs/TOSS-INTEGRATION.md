# 💳 Toss페이먼츠 연동 설계

> payhere 종속을 탈피하고 **Toss페이먼츠**로 결제를 직접 처리하기 위한 연동 설계.
> P0(현재)는 **인터페이스·흐름·환경변수**까지. 실 승인 호출은 키 주입 시 동작(Mock 폴백 제공).

---

## 1. 어떤 Toss 제품을 쓰나

| 시나리오 | Toss 제품 | 비고 |
| --- | --- | --- |
| **매장 단말 결제(카드)** | Toss페이먼츠 **단말기 연동**(키오스크/POS) | 단말 페어링·승인 (P1 하드웨어) |
| **앱/웹 간편결제** | Toss페이먼츠 **결제위젯/표준결제** | 고객 셀프결제·선주문 (P1) |
| **테스트/스캐폴드** | 테스트 시크릿키 + 결제 승인 API | 본 문서 범위 |

핵심은 어느 경로든 **서버가 최종 승인(confirm)** 을 책임진다는 점. 클라이언트는 `paymentKey`·`orderId`·`amount`를 받아 서버로 넘기고, 서버가 Toss `confirm` API로 금액 위변조 없이 확정한다.

---

## 2. 표준 결제 승인 흐름 (서버 confirm)

```
[POS/웹]                 [우리 서버]                 [Toss]
  │  결제 요청(금액)          │                          │
  │ ───────────────────────▶ │  주문 생성(orderId, amount)│
  │  ◀─ orderId/amount ───── │                          │
  │  Toss SDK로 결제 진행 ───────────────────────────────▶│
  │  ◀── paymentKey 등 인증결과 ──────────────────────────│
  │  POST /transactions ────▶│                          │
  │  (paymentKey,orderId,amt)│  POST /v1/payments/confirm ─▶│
  │                          │  ◀── 승인 결과(approved) ──│
  │  ◀── 거래확정·적립 결과 ─ │  (포인트 적립·미션 갱신)   │
```

### 승인 API (서버 → Toss)
```
POST https://api.tosspayments.com/v1/payments/confirm
Authorization: Basic base64(SECRET_KEY + ":")
Content-Type: application/json

{ "paymentKey": "...", "orderId": "slowstep-...", "amount": 5500 }
```
- **금액 검증:** 서버가 보관한 `net_amount`와 요청 `amount`가 일치해야 승인. 위변조 차단.
- **멱등:** `orderId`는 우리 거래의 멱등키. 중복 승인 방지(409).

---

## 3. 환경변수

| 변수 | 설명 | 기본(미주입 시) |
| --- | --- | --- |
| `TOSS_SECRET_KEY` | Toss 시크릿키(서버 전용) | 없음 → **Mock 승인** |
| `TOSS_CLIENT_KEY` | 클라이언트 키(프론트) | 테스트 공개키 |
| `TOSS_API_BASE` | API 베이스 | `https://api.tosspayments.com` |
| `TOSS_WEBHOOK_SECRET` | 웹훅 서명 검증 키 | 없음 |

> **Mock 모드:** `TOSS_SECRET_KEY` 미설정 시 `payments.py`는 실제 호출 없이 승인 성공을 반환한다. → 스캐폴드/데모에서 전체 적립·게이미피케이션 로직을 키 없이 검증 가능.

---

## 4. 서버 모듈 (`membership/payments.py`)

```python
class TossClient:
    def confirm(self, payment_key, order_id, amount) -> TossResult: ...
```
- `TOSS_SECRET_KEY` 있으면 실제 `confirm` 호출, 없으면 Mock 성공.
- 실패 시 `TossError` → 뷰에서 502 매핑, 거래는 `pending` 유지.

---

## 5. 웹훅 (P1)

`POST /api/v1/payments/toss/webhook` — 결제 취소·정산 등 비동기 상태를 수신.
- `TOSS_WEBHOOK_SECRET`로 서명 검증 후 `Transaction.status` 동기화.
- 취소 이벤트 → 거래 `canceled` + 포인트 원복(PointEntry `cancel`).

---

## 6. 보안 체크리스트

- [ ] 시크릿키는 **서버에만**. 프론트엔 클라이언트 키만.
- [ ] 승인 시 **서버 보관 금액과 대조** (클라이언트 금액 신뢰 금지).
- [ ] `orderId` 멱등 처리로 중복 승인 차단.
- [ ] 웹훅 서명 검증 필수.
- [ ] HTTPS 전구간, 결제 로그에 카드 원번호 미저장(Toss가 보관).

---

## 7. payhere 대비 이점

| 항목 | payhere(현행) | 자체 + Toss(목표) |
| --- | --- | --- |
| 고객 DB 소유 | 결제사 종속, 프리미엄 유료 | **우리 DB, 자유 활용** |
| 마케팅 | 결제사 기능 구매 필요 | **우리가 직접 세그먼트·발송** |
| 멤버십 로직 | 제한적 | **적립·등급·미션 자유 설계** |
| 결제 | payhere | **Toss페이먼츠 직승인** |
