# 🗄️ 데이터 모델 — 슬로우스텝 멤버십 POS

Django 앱 `membership`. 단일 매장 가정(다매장은 P3). 모든 금액은 **원(KRW) 정수**.

## ERD (개념)

```
Store 1───* Member 1───* Transaction *───1 Store
                 │            │
                 │            └──* PointEntry  (적립/사용 원장)
                 │
                 ├──* PointEntry
                 └──* MemberMission *───1 Mission
```

## 모델 상세

### Store — 매장
| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `name` | Char | 매장명 (예: 슬로우스텝) |
| `point_earn_rate` | Decimal | 적립률 (기본 0.05 = 5%) |
| `stamp_goal` | PositiveSmallInt | 스탬프 리워드 목표 개수 (기본 10) |
| `created_at` | DateTime | 생성 시각 |

### Member — 회원
| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `store` | FK→Store | 소속 매장 |
| `phone` | Char(unique) | **회원번호 = 연락처** (식별 키) |
| `name` | Char | 이름 |
| `points` | Int | 보유 포인트 (원장 합과 일치, 캐시) |
| `total_spent` | Int | 누적 실결제액 |
| `visit_count` | Int | 방문(결제 확정) 횟수 |
| `tier` | Char(choices) | `BRONZE`/`SILVER`/`GOLD` (누적액 기반 자동) |
| `stamps` | Int | 현재 스탬프 수 (목표 도달 시 리셋·리워드) |
| `joined_at` | DateTime | 가입 시각 |
| `marketing_opt_in` | Bool | 마케팅 수신 동의 |

> **등급 규칙(기본):** 누적액 `< 50,000` → BRONZE, `< 200,000` → SILVER, 그 이상 GOLD.

### Transaction — 거래(결제)
| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `store` | FK→Store | 매장 |
| `member` | FK→Member (nullable) | 회원(비회원 결제 허용) |
| `gross_amount` | Int | 주문 총액 |
| `points_used` | Int | 사용 포인트 (1P=1원) |
| `net_amount` | Int | 실결제액 = gross − points_used |
| `points_earned` | Int | 적립 포인트 |
| `payment_method` | Char(choices) | `TOSS_CARD`/`TOSS_EASY`/`CASH` |
| `status` | Char(choices) | `pending`/`paid`/`canceled` |
| `toss_payment_key` | Char | Toss 결제 키 (승인 후) |
| `toss_order_id` | Char | 주문 ID (멱등키) |
| `created_at` / `paid_at` | DateTime | 생성/승인 시각 |

### PointEntry — 포인트 원장
| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `member` | FK→Member | 회원 |
| `transaction` | FK→Transaction (nullable) | 연관 거래 |
| `delta` | Int | 증감(+적립/−사용/±조정) |
| `reason` | Char(choices) | `earn`/`redeem`/`adjust`/`mission`/`cancel` |
| `balance_after` | Int | 반영 후 잔액(감사용) |
| `created_at` | DateTime | 시각 |

> 잔액은 **원장이 진실의 원천**. `Member.points`는 빠른 조회용 캐시이며 원장과 동기화.

### Mission — 미션 정의
| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `store` | FK→Store | 매장 |
| `title` / `description` | Char/Text | 미션 내용 |
| `condition_type` | Char(choices) | `visit_count`/`total_spent` |
| `target_value` | Int | 목표값 (예: 3회, 50000원) |
| `reward_points` | Int | 달성 보상 포인트 |
| `is_active` | Bool | 노출 여부 |

### MemberMission — 회원별 미션 진행
| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `member` | FK→Member | 회원 |
| `mission` | FK→Mission | 미션 |
| `progress` | Int | 현재 진행값 |
| `is_completed` | Bool | 달성 여부 |
| `completed_at` | DateTime | 달성 시각 |

`unique_together = (member, mission)`.

## 핵심 트랜잭션 로직 (결제 확정 시)

원자적(`transaction.atomic`)으로 처리:

1. `net_amount = gross_amount − points_used` 계산, 유효성 검사(보유 포인트 ≥ 사용액).
2. Toss 승인(또는 현금) → `status='paid'`, `paid_at` 기록.
3. **포인트 사용**: `points_used > 0`이면 PointEntry(`redeem`, −) 기록.
4. **포인트 적립**: `points_earned = round(net_amount × earn_rate)` → PointEntry(`earn`, +).
5. `member.total_spent += net_amount`, `visit_count += 1`, `stamps += 1`.
6. **스탬프 목표** 도달 시 리워드 처리 후 리셋.
7. **등급** 재계산.
8. **미션** 진행값 갱신, 달성 시 보너스 PointEntry(`mission`, +).
9. `member.points`를 원장 잔액과 동기화 후 저장.
