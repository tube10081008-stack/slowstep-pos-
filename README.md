# ☕ 슬로우스텝 멤버십 POS

> **결제사 종속에서 벗어난 자체 모바일 POS · 단말결제 · 게이미피케이션 멤버십 시스템**
> 카페 *슬로우스텝* 이 직접 모은 멤버십 고객(약 220명)을 **온전히 우리 자산으로** 운영하기 위한 프로젝트.

이 저장소는 구 모노레포(`Urge-surfing`)에서 분리된 **독립 저장소**입니다. (모든 파일이 저장소 루트에 있습니다.)

---

## 🎯 왜 만드나 (병목)

현재 슬로우스텝은 국내 결제사 **payhere** 프로그램을 사용 중입니다.

- 멤버십 고객을 **220명 가까이** 직접 모았으나,
- 이들에게 마케팅을 하려면 결제사의 **고객관리 프리미엄 기능을 별도 구매**해야 합니다.
- 즉, **내가 모은 고객인데도 결제사 동의 없이는 마케팅을 실행할 수 없는** 종속 상태입니다.

**해결:** 결제 단말기 + **Toss페이먼츠**를 결합해 **자체 모바일 POS / 단말결제 시스템**을 구축합니다.
고객 데이터의 소유권을 우리가 갖고, 적립·등급·미션 등 **게이미피케이션 멤버십**을 자유롭게 운영합니다.

---

## 🧩 핵심 기능

| # | 기능 | 한 줄 설명 |
| --- | --- | --- |
| 1 | **모바일 POS** | 스마트폰/태블릿이 곧 단말. 직원이 결제·적립을 한 화면에서 처리 |
| 2 | **단말 결제 (Toss)** | Toss페이먼츠로 카드/간편결제 승인. payhere 종속 탈피 |
| 3 | **회원번호(연락처) 조회** | QR코드 스캔 → 본인 회원번호로 즉시 식별·적립 |
| 4 | **포인트 적립/사용** | 결제 금액 기반 자동 적립, 다음 결제에서 차감 사용 |
| 5 | **게이미피케이션 멤버십** | 방문 스탬프·등급(브론즈~골드)·미션/챌린지로 재방문 유도 |
| 6 | **우리 소유의 고객 DB** | 마케팅 발송·세그먼트의 기반. 결제사 동의 불필요 |
| 7 | **마케팅 세그먼트·알림톡** | 등급·휴면·방문 기준으로 타깃 추출 → 알림톡 발송(수신동의·수신거부 자동) |
| 8 | **점주 대시보드** | 매출·회원·등급·포인트부채 지표 + 캠페인 발송을 한 화면에서 |

---

## 🏗️ 기술 스택

| 영역 | 선택 | 비고 |
| --- | --- | --- |
| 백엔드 | **Django 5 + DRF** | 기존 레포와 동일 컨벤션, 이 환경에 즉시 실행 가능 |
| 데이터 | SQLite(개발) → PostgreSQL(운영) | 환경변수 주입 시 운영 승격 |
| 결제 | **Toss Payments** | 단말/온라인 결제 승인·웹훅 |
| 클라이언트 | **웹앱(반응형)** | 직원용 POS + 고객용 QR 멤버십 페이지. 설치 없이 즉시 사용 |

---

## 📂 폴더 구조

```
.  (저장소 루트)
├─ README.md                ← (이 문서)
├─ docs/
│  ├─ PRD.md                ← 제품 기획 (문제·목표·기능·로드맵)
│  ├─ DATA-MODEL.md         ← 데이터 모델 설계
│  ├─ API-CONTRACT.md       ← REST API 계약 (/api/v1)
│  └─ TOSS-INTEGRATION.md   ← Toss페이먼츠 연동 설계
├─ backend/                 ← Django + DRF
│  ├─ manage.py
│  ├─ requirements.txt
│  ├─ config/               ← settings/urls/wsgi
│  ├─ membership/           ← 회원·거래·포인트·미션 도메인
│  └─ marketing/            ← 세그먼트·캠페인(알림톡)·대시보드 집계
└─ web/
   ├─ pos/index.html        ← 직원용 모바일 POS
   ├─ member/index.html     ← 고객용 QR 멤버십 페이지
   └─ dashboard/index.html  ← 점주 대시보드 + 마케팅 발송
```

---

## 🚀 빠른 시작 (백엔드)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo       # 매장/미션/기본 회원 시드
python manage.py seed_marketing  # 대시보드용 다양한 회원·거래·세그먼트 시드
python manage.py runserver
```

- API 베이스: `http://localhost:8000/api/v1/`
- 관리자: `http://localhost:8000/admin/` (`createsuperuser` 후)

## 🖥️ 웹 클라이언트 (정적)

```bash
cd web
python -m http.server 5500
# 직원 POS   : http://localhost:5500/pos/
# 고객 QR    : http://localhost:5500/member/?phone=01012345678
# 점주 대시보드 : http://localhost:5500/dashboard/
```
> POS/멤버 페이지는 백엔드 `API_BASE`를 가리킵니다. 다른 호스트면 각 HTML 상단의 `API_BASE` 값을 수정하세요.

---

## 🗺️ 로드맵 (요약)

- **P0 (완료)** — 데이터 모델, 회원/적립/거래 API, Toss 연동 설계, 동작하는 웹 POS·QR 페이지
- **P2 마케팅 (완료)** — 세그먼트 빌더, 알림톡 캠페인 발송(수신동의·수신거부·치환), 점주 대시보드(매출·회원·등급·포인트부채 지표)
- **P1** — Toss 실결제 승인·웹훅 연동, 단말 페어링, 거래 취소/환불, 정산 리포트
- **P2+** — 알림톡 실 대행사 연동·템플릿 검수, 미션 엔진·푸시, 예약 발송, A/B
- **P3** — 멀티 매장, 정산/회계 연동, 심화 분석 대시보드

자세한 내용은 [`docs/PRD.md`](./docs/PRD.md) 참조.

---

## 🌐 공개 URL로 배포 (Vercel)

설치 없이 인터넷 주소로 열려면 **Vercel**(git 푸시 자동 배포)에 올린다.
Django 하나가 API와 웹페이지를 같은 주소로 함께 서빙하도록 구성돼 있어
**서버리스 함수 하나로** 전부 동작한다. 단계별 안내: [`DEPLOY.md`](./DEPLOY.md).

- **그냥 배포** → 즉시 동작(임시 저장 + 데모 데이터 자동 시드)
- **무료 DB(Neon) 연결** → 데이터 영구 저장·여러 기기 공유로 승격
- 루트(`/`) → 대시보드 · `/pos/` 직원 POS · `/member/?phone=...` 고객 · `/admin/` 관리자

> Vercel 핵심 파일: [`vercel.json`](./vercel.json) · [`api/index.py`](./api/index.py) · [`requirements.txt`](./requirements.txt)
> (Render 등 상시 서버 배포용 [`render.yaml`](./render.yaml) 도 함께 제공)
