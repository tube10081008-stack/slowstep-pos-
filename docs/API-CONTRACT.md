# 🔌 API 계약 — 슬로우스텝 멤버십 POS

Base URL: `/api/v1` · 형식: JSON · 금액: 원(KRW) 정수

## 인증 — 매장 PIN

점주·직원 화면(POS·대시보드)의 API는 **매장 PIN 토큰**이 필요하다.
고객 멤버십 조회는 **공개** 유지(QR로 바로 열려야 하므로).

### `POST /api/v1/auth/pin`
```json
요청 { "pin": "0000" }
200  { "token": "store:1abc..." }     ← 이후 요청에 X-Store-Token 헤더로 전달
401  { "detail": "PIN이 올바르지 않습니다." }
429  { "detail": "시도 횟수를 초과했습니다. 잠시 후 다시 시도하세요." }
```
### `GET /api/v1/auth/pin`
현재 토큰 유효성 확인 → `{ "authorized": true }`.

- PIN은 환경변수 **`STORE_PIN`** 으로 설정(미설정 시 기본값). 공개 저장소면 반드시 재정의.
- 토큰은 `SECRET_KEY`로 서명(유효기간 30일). `SECRET_KEY`를 바꾸면 전부 무효화된다.
- 무차별 대입 방지: IP당 5분에 7회 초과 시 429.

| 구분 | 엔드포인트 |
| --- | --- |
| 🔒 **매장 전용** | `transactions*` · `sales/summary` · `margins/summary` · `dashboard/stats` · `store/session` · `members`(목록·가입·`import`) · `segments*` · `campaigns*` |
| 🌐 **공개** | `health` · `menu` · `store` · `missions` · `member-qr` · `hall-of-fame` · `members/lookup` · `members/{id}/dashboard`·`missions`·`points`·`referral`·`spin` |

> 토큰 없이 매장 전용 API 호출 시 **403**.

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

### `POST /api/v1/members`  🔒
회원 가입. **이름은 선택** — 비우면 서버가 '행동 + 동물' 닉네임을 자동 부여한다
(예: `느긋한 수달`). 연락처만으로 식별되므로 실명은 수집하지 않는 것이 기본이며,
개인정보 최소 수집 원칙에 맞춘 설계다. 이관된 기존 고객처럼 이름을 주면 그대로 쓴다.
```json
요청 { "phone": "01012345678", "marketing_opt_in": true }
201  { "id": 1, "phone": "...", "name": "느긋한 수달", "points": 0, "tier": "BRONZE", ... }
```

### `POST /api/v1/members/import`
**기존 고객 CSV 일괄 등록** (payhere 등 타 시스템 이관용).
- 입력: multipart `file`(엑셀 CP949·UTF-8 자동 판별) 또는 JSON `csv`(텍스트) + `dry_run`.
- CSV 첫 행은 헤더. 필수 열 `이름`·`연락처`, 선택 열 `포인트`·`누적결제액`·`방문횟수`·
  `스탬프`·`마케팅동의`(Y/N)·`가입일`(YYYY-MM-DD). 열 이름은 유사 표기 자동 매핑
  (전화번호/휴대폰, 보유포인트/잔여적립금 등).
- 규칙: 초기 포인트는 **원장(PointEntry, `adjust`)에 기록**, 등급은 누적액으로 재계산,
  **이미 등록된 연락처는 건너뜀**(덮어쓰지 않음), 원래 가입일 보존.
- **이미 충족한 등급·미션은 보상 없이 달성 처리한다.** 이관 회원은 등급도 미션
  조건도 채운 채로 들어오므로, 그대로 두면 첫 결제 한 번에 등급 쿠폰과 미션
  보상이 소급 지급된다(누적 38만원 회원 기준 2,500P + 1+1 쿠폰 4장). 보상은
  우리 앱에서 **앞으로 하는 것**에 붙어야 한다. 지난 기록은 등급·방문수로 이미
  인정하고 있으므로 여기서 또 주면 이중이다.
  → 이관을 거치지 않고 들어온 회원은 `python manage.py apply_baseline` 로 정리.
- `dry_run=true` → 검증·집계만(저장 없음). 대시보드의 "① 검증하기"가 사용.
```json
200 { "dry_run": false, "total": 220, "created": 210, "skipped": 8, "errors": 2,
      "results": [ { "row": 2, "name": "김이관", "phone": "01055556666",
                     "status": "created", "reason": "", "points": 3200 }, ... ] }
400 { "detail": "헤더에서 이름·연락처 열을 찾을 수 없습니다. ..." }
```

### `GET /api/v1/member-qr`
멤버십 페이지로 가는 **QR**(공개). `{ "url": ".../member/", "svg": "<svg …>" }`.
- **개인정보도 토큰도 담지 않는다.** 모든 손님에게 동일한 고정 주소만 인코딩하고,
  조회는 손님이 자기 폰에서 자기 번호를 입력해 한다.
  → 계산대 화면을 촬영해도 얻을 게 없고, 만료가 없어 **인쇄해 붙여도** 동작한다.
- SVG 문자열이라 고객 화면은 서버를 호출하지 않고 그대로 그린다(POS가 1회 받아 전달).

### `GET /api/v1/members/{id}`
회원 상세 + 진행 중 미션 + 최근 적립 내역.

> **`GET /api/v1/members/{id}/dashboard`** 는 게이미피케이션 전체를 한 번에 준다:
> `roulette`, `coupons`, `streak`, `badges`, `title`, `quests`,
> `taste`(최애 메뉴·카테고리 분포), `collection`(메뉴 도장깨기·다음 도전 메뉴),
> `hall_of_fame`(이달의 단골), `referral`(내 초대 코드),
> `next_tier`, `ranking`, `timeline`, `missions`.

#### `roulette` · `POST /api/v1/members/{id}/spin` — 행운의 룰렛
스탬프를 채우거나 연속 방문 조건을 달성하면 **기회(spins)** 가 쌓이고,
손님이 **자기 폰에서 직접** 돌린다. 결제 화면에서 자동으로 돌리지 않는 이유는
직접 돌리는 순간이 재미의 전부이기 때문이다.
```json
"roulette": { "spins": 2, "segments": [
  { "kind": "discount_5", "label": "5%", "sub": "할인 쿠폰", "name": "5% 할인" }, … ] }
```
```json
POST → 200 { "index": 0, "spins_left": 1,
             "coupon": { "id": 12, "kind": "discount_5", "name": "5% 할인",
                         "expires_at": "2026-11-06T…" } }
400 { "detail": "룰렛 기회가 없어요." }
```
- **당첨은 서버가 정한다.** 응답의 `index`는 화면이 그 칸에 멈추도록 하는 연출값일
  뿐이고, 확률은 클라이언트로 내려보내지 않는다.
- 확률 — 할인쿠폰 합계 60%(5% 40 / 10% 20), 1+1 20%, 무료 음료 20%,
  **원두 200g 0%**. 원두 칸은 화면에만 있는 '보여주기용'이다. 눈에 보이는 큰
  상품이 있어야 돌릴 맛이 나고, 실제 지급 부담은 지지 않는다.
- 기회 차감은 회원 행을 잠그고 처리한다 — 버튼 두 번 누르기·두 기기 동시 접속으로
  기회 1번에 쿠폰 두 장이 나가는 걸 막는다.

#### `coupons` · `GET /api/v1/members/{id}/coupons` 🔒 — 보유 쿠폰
룰렛·등급 승급·랭킹 시상으로 발행된다. **포인트 원장과 섞지 않는다** — 할인율
쿠폰과 음료 쿠폰은 원가 성격이 달라 따로 관리해야 한다. 기본 유효기간 90일.
```json
{ "id": 12, "kind": "bogo", "name": "음료 1+1", "source": "등급 승급",
  "note": "실버 승급", "discount_pct": 0, "days_left": 89 }
```
- 사용·만료된 쿠폰은 목록에서 빠진다.
- `/members/{id}/coupons` 는 같은 목록의 **직원용**(POS 결제 화면이 호출).

##### 쿠폰 사용 — `POST /api/v1/transactions` 의 `coupon_id`
쿠폰은 따로 '사용 처리'하지 않는다. **결제에 실어 보내면 금액이 깎이면서 그
거래에 묶여 소진된다.** 별도 API로 소진하면 "쿠폰은 썼는데 결제가 실패"하는
어긋난 상태가 생긴다.

| 쿠폰 | 깎이는 금액 |
| --- | --- |
| 5% · 10% 할인 | 주문 총액의 그 비율(원 단위 버림) |
| 음료 1+1 | 음료 2잔 이상일 때 **싼 쪽 1잔** 값 |
| 무료 음료 | 음료 중 **가장 비싼 1잔** 값 |
| 원두 200g | 물건으로 나가므로 금액 할인 0 |

- 할인액은 **주문 내용에 따라 달라지므로 결제 시점에 서버가 계산**한다.
  POS도 같은 식으로 미리 보여주지만 판정은 서버가 한다.
- 세트 할인 위에 더해지고, 포인트 사용보다 **먼저** 적용된다 —
  적립은 실제로 받은 금액 기준이어야 한다.
- 거래가 **확정되는 순간에만** 소진하고, 취소하면 되돌린다. 취소했는데
  쿠폰만 사라지면 손님이 손해를 본다.
- 400 사유 — 다른 회원의 쿠폰 / 이미 사용 / 기한 만료 /
  `1+1 쿠폰은 음료 2잔 이상일 때 사용할 수 있어요.`
- 관리자 화면(**쿠폰**)에서도 상태 확인과 수기 사용 처리가 가능하다.

#### `streak` — 연속 방문 (두 갈래)
```json
"streak": { "daily":  { "days": 4,  "goal": 5, "left": 1, "alive": true },
            "weekly": { "weeks": 2, "goal": 4, "left": 2, "alive": true } }
```
- **5일 연속** 또는 **4주 연속**(주 1회 이상)이면 룰렛 기회 1번.
- 매일 오는 손님만 보상하면 주 1회 단골이 소외되고, 주 단위만 보면 매일 오는
  손님이 심심하다. 둘 다 열어 두고 각자의 리듬으로 도전하게 한다.
- 보상은 **주기마다 반복**된다(10일·15일에도 다시). 한 번 받고 끝나면 이어갈
  이유가 없어지는 게 연속 방문 보상에서 가장 흔한 실패다.
- 오늘 아직 안 오셨어도 어제까지 이어졌다면 `alive: true` — 오늘 오시면 연장.

#### `badges` — 배지
방문·누적에 더해 **시간대·요일·옵션·컬렉션·스트릭** 기반 22종.
```json
{ "key": "morning", "icon": "☀", "title": "아침형 인간",
  "desc": "오전 방문 5회", "earned": true, "rarity": 12.5 }
```
- `rarity` — 그 배지를 가진 회원 비율(%). 전 회원을 훑으므로 **10분 캐시**한다.
- **단계형**(`cups` 10/50/100잔, `spender` 5만/20만/50만원)은 `level`·`max_level`이
  붙고 제목에 로마숫자가 따라온다 — `커피 애호가 Ⅰ`, `desc: "다음 단계까지 38잔"`.
  한 번 따고 끝나지 않아 오래 다닌 손님일수록 티가 난다.
- **히든**(오늘의 첫 손님 / 늦은 밤의 위로 / 룰렛 잭팟 / 세트 마스터)은
  **획득 전에는 목록에 아예 없다.** 조건을 미리 알리지 않는 게 목적이다.

#### `title` — 대표 칭호
획득한 배지 중 **가장 희귀한 것**. 닉네임 앞에 붙는다. 없으면 `null`.
```json
{ "key": "club10", "icon": "X", "title": "10잔 클럽", "rarity": 25.0 }
```

#### `quests` — 개인 맞춤 미션 (한 번에 한 챕터)
매장 공통 `missions`와 달리 **DB에 미리 만들어 두지 않는다.** 조회할 때 그 사람의
주문·방문 데이터로 후보를 뽑아 진행률을 계산하고, **달성했을 때만** 기록해
보상을 1회 지급한다(회원 220명 × 퀘스트 수만큼 레코드가 불어나지 않는다).

성격이 다른 목표를 한꺼번에 던지면 뭘 하라는 건지 안 잡히므로 **테마 그룹으로
묶어 한 챕터씩만 활성화**한다. 미달성 퀘스트가 남은 첫 그룹이 통째로 나오고,
그 그룹을 다 깨면 완주 보너스가 붙으면서 다음 챕터가 열린다.
활성 챕터가 없으면 `null`.
```json
"quests": {
  "key": "taste", "title": "취향 탐험대",
  "description": "아직 안 드셔본 갈래를 하나씩",
  "bonus": 1000, "bonus_earned": false, "done": 1, "total": 3,
  "items": [
    { "key": "taste:dessert", "kind": "taste", "group": "taste",
      "title": "디저트 처음 만나기", "description": "플레인 휘낭시에 어떠세요?",
      "progress": 1, "target": 1, "reward": 500, "is_completed": true }
  ]
}
```
- 그룹 우선순위 — `comeback`(평소 주기의 1.5배 넘게 안 오심) → `collection`(1~2종
  남은 카테고리) → `taste`(미경험 갈래) → `rhythm`(이번 주 + 월간 도전) →
  `option`(오트·디카페인·샷) → `timeslot`(오전·저녁). 그룹당 최대 3개.
- **이미 깬 퀘스트도 `items`에 남는다.** 빠지면 `done/total`이 말이 안 되고,
  묶은 이유(완주감)가 사라진다. 클라이언트는 `is_completed`로 체크 표시만 한다.
- `rhythm` 그룹의 월간 도전(`kind: "stretch"`) 목표는 **최근 3개월 개인 평균
  ×1.2**로 잡는다 — 주 3회 오는 손님과 월 1회 오는 손님에게 같은 숫자를
  들이밀지 않는다.
- 보상은 퀘스트당 **2,000P 상한**, 결제 1건당 **총 3,000P 상한**. 상한에 걸린
  보상은 기록하지 않으므로 **다음 방문에 지급**된다(적립비용이 마진에서
  차감되므로 한 번에 몰리면 곤란하다).
- 지급은 결제 시점에 서버에서 판정해 응답 `rewards[]`에 실린다 —
  퀘스트는 `{"type":"quest"}`, 챕터 완주 보너스는 `{"type":"quest_group"}`.

### `POST /api/v1/members/{id}/referral`
**친구 초대 코드 적용**(공개 — 손님 폰에서 호출). `{ "code": "7K2M9Q" }`
→ 초대한 사람과 받은 사람 **모두** 1,000P. 받는 쪽은 한 번만, 자기 코드는 불가,
가입 초기(방문 3회 이하)에만 사용할 수 있다. 초대하는 쪽은 **하루 1명**까지 —
코드를 뿌려 하루에 수십 명을 넣는 걸 막는다. 실패 시 400 + 사유.

### `GET /api/v1/hall-of-fame`
**이달의 단골 TOP3**(공개). 닉네임으로 표시하므로 매장 화면에 띄워도 된다.
**금액·횟수 두 부문**을 따로 매긴다 — 한 줄로 세우면 객단가 높은 손님과 자주
오는 손님 중 한쪽이 늘 진다. 매달 1일에 초기화된다.
```json
200 { "month": "2026-08",
      "prizes": ["아메리카노 + 플레인 휘낭시에", "아메리카노", "플레인 휘낭시에"],
      "boards": [
        { "key": "spent",  "label": "금액", "unit": "원",
          "top": [ { "rank":1, "nickname":"큰손 고양이", "value":128000,
                     "prize":"아메리카노 + 플레인 휘낭시에", "member_id":7 } ] },
        { "key": "visits", "label": "횟수", "unit": "회", "top": [ … ] } ],
      "top": [ … ] }
```
> `top`은 횟수 부문과 같은 내용의 하위호환 필드다.
> 시상 자체(쿠폰 발행)는 **월말에 사람이 확인하고 지급**한다 — 자동 지급은
> 어뷰징 확인 없이 상품이 나가므로 일부러 넣지 않았다.

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

### `POST /api/v1/orders/parse`  🔒
**자연어 주문 → 장바구니 항목.** POS 상단 입력창이 호출한다.
```json
요청 { "text": "아아 두 잔이랑 라떼 하나 따뜻하게, 휘낭시에 2개" }
200  { "source": "gemini",
       "items": [ { "action": "add", "menu_item_id": 1, "name": "아메리카노", "quantity": 2,
                    "temperature": "ice", "decaf": false, "oatmilk": false, "shot": false }, ... ] }
400  { "detail": "주문에서 메뉴를 찾지 못했습니다. ..." }
```
- **2단 구조**: `GEMINI_API_KEY` 설정 시 **Gemini**(`gemini-3.5-flash-lite`),
  키가 없거나 호출 실패면 **규칙 기반 폴백**(`source: "rule"`). 키 없이도 동작한다.
- **`action`**: `"add"`(기본) 또는 `"remove"`. 빼기를 못 읽으면 빼려던 걸 한 잔 더
  담게 되므로 별도 필드로 내려준다.
  - 한국어는 동사가 끝에 온다 → `"아아랑 라떼 빼줘"` 는 **둘 다** remove.
    표시가 없는 조각은 **뒤에 오는** 가장 가까운 표시를 따르고, 앞으로는 전파하지
    않는다(`"아아 빼고 라떼 하나"` 의 라떼는 add).
  - `"A 말고 B"` → A는 remove, B는 add.
  - `"얼음 빼고"`·`"샷 빼고"` 처럼 재료 뒤에 붙은 '빼'는 remove로 보지 않는다.
  - POS는 remove를 받으면 **옵션까지 같은 줄을 먼저** 지우고, 없으면 같은 메뉴의
    다른 줄에서 뺀다 — `"아메리카노 빼줘"` 만으로는 핫인지 아이스인지 알 수 없다.
    장바구니에 없으면 담지 않고 "뺄 수 없습니다"라고 알린다.
- **모델 출력을 신뢰하지 않는다**: 반환된 `menu_item_id`가 실제 판매 중인 메뉴인지,
  옵션이 그 메뉴에 허용되는지, 수량이 1~20인지 서버가 전부 재검증한다. 품절 메뉴는 제외.
- 금액은 계산하지 않는다 — 결제 시 `POST /transactions`가 서버 가격으로 계산.

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
- 라인별 옵션: `temperature`(`hot`/`ice`), `decaf`, `oatmilk`, `shot`. 서버가 단가(옵션 포함)와
  세트 할인을 계산한다. 응답에 `discount`, 주문 항목(`items[]`, `option_label` 포함)이 담긴다.
- **결제수단(`payment_method`)**: `CARD`·`NAVERPAY`·`EASYPAY`·`CASH` 은 **외부 단말
  (네이버페이 커넥트 멀티패드 등)에서 결제** → 앱은 기록만(PG 호출 없음).
  `approval_no`(선택)로 단말 승인번호를 정산용 저장. `TOSS_CARD`·`TOSS_EASY` 는 Toss PG 실연동용.
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

### `POST /api/v1/transactions/{id}/cancel`
결제 취소/환불. `paid`→`canceled` 전환 + **포인트 원복**(사용분 환급·적립분 회수)
+ 누적/방문/스탬프 되돌림 + **재고 원복**. (실 Toss 연동 시 환불 API 호출 지점)

> 메뉴 옵션: `shot`(에스프레소 샷, 커피류) 추가 지원 — `decaf`/`oatmilk`와 동일하게 +`option_price`.
> 재고: `MenuItem.stock`(null=무제한). 결제 시 차감, 부족하면 400, `sold_out`이면 주문 불가.

---

## 미션 (Mission)

### `GET /api/v1/missions`
활성 미션 목록(매장 단위).

---

## 매장 (Store) · POS 운영

### `GET /api/v1/store`
기본 매장 설정(적립률 3%·스탬프 목표·옵션가·세트할인·영업상태 `is_open`).

### `POST /api/v1/store/session`
영업 시작/마감. `{ "action": "open" | "close" }` → 갱신된 store 반환.

### `GET /api/v1/sales/summary`
오늘 정산: `{ count, gross, discount, net, points, by_method, is_open, opened_at, margin }`.
`margin` = 오늘 기여이익 `{ supply_revenue, material_cost, reward_cost, contribution, margin_rate }`.

### `GET /api/v1/margins/summary?days=30`
**원가·마진 분석**(점주 전용). 기준: **공급가(매출÷(1+vat))** − **재료원가** −
**적립비용(포인트·스탬프·미션, 적립 시점 인식)** = 기여이익. 인건비·임대료 등 고정비는
범위 밖(재료비 기준). 취소 거래는 매출·원가·비용 모두에서 제외.
```json
200 {
  "days": 30, "vat_rate": 0.1, "tx_count": 3,
  "revenue_incl_vat": 33000, "supply_revenue": 30000,
  "material_cost": 9300, "reward_cost": 1990,
  "contribution": 18710, "margin_rate": 62.4, "cost_rate": 31.0,
  "menu": [ { "name": "카페 라떼", "qty": 3, "supply_revenue": 13636,
             "material_cost": 4200, "margin": 9436, "margin_rate": 69.2,
             "cost_rate": 30.8, "has_cost": true }, ... ]
}
```
- **메뉴별 마진**(`menu[]`)은 각 메뉴 정가(옵션 포함 단가) 공급가 − 재료원가.
  세트할인·포인트는 거래 단위라 개별 메뉴에 배분하지 않음(상품 자체 수익성).
- 원가는 **관리자 → 메뉴**의 `재료원가`, 옵션 추가원가는 `Store.option_cost`,
  부가세율은 `Store.vat_rate`. 원가는 결제 시점에 `OrderItem.unit_cost`로 스냅샷되어
  나중에 원가를 바꿔도 과거 마진은 불변. `has_cost=false`는 원가 미입력 메뉴.

### `GET /api/v1/transactions`
주문 내역: 최근 결제완료 100건(주문 항목·회원명·수단 포함).

### `GET /api/v1/members?q=<검색어>`
고객 관리: 이름·연락처로 검색(누적결제 내림차순).

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
