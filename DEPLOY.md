# 🚀 배포 가이드 — Vercel로 공개 URL 만들기

목표: 인터넷 주소(예: `https://slowstep-pos.vercel.app`)로 **설치 없이**
대시보드·POS·멤버십에 접속한다. **Vercel**(git 푸시로 자동 배포)을 사용한다.

> Django 한 개가 API와 웹페이지(POS·멤버십·대시보드)를 같은 주소로 함께 서빙하도록
> 이미 구성돼 있다. Vercel 서버리스 함수 하나로 전부 돈다.

---

## 두 단계로 이해하기

1. **그냥 배포하면** → 바로 동작한다. (Vercel 임시 저장소 + 데모 데이터 자동 시드)
   단, 임시 저장이라 일정 시간 뒤 데이터가 초기화된다(데모 데이터는 다시 채워짐).
2. **무료 DB(Neon)를 붙이면** → 입력한 데이터가 **영구 저장**되고 여러 기기·직원이 공유한다.

먼저 1단계로 띄워 눈으로 확인하고, 2단계로 DB를 붙이는 순서를 권한다.

---

## 1단계 — Vercel에 배포 (약 3분)

### 1. 가입
- https://vercel.com → **Sign Up** → **Continue with GitHub** 으로 가입/로그인.

### 2. 프로젝트 가져오기
1. 대시보드 → **Add New… → Project**.
2. 저장소 목록에서 **`tube10081008-stack/Urge-surfing`** → **Import**.
   (안 보이면 **Adjust GitHub App Permissions** 로 이 저장소 접근 허용)

### 3. 설정 (중요)

| 항목 | 값 |
| --- | --- |
| **Root Directory** | **`slowstep-pos`** ← *반드시 변경* (Edit 눌러 선택) |
| Framework Preset | `Other` (자동) |
| Build/Output | 비워둠 (vercel.json이 처리) |

> **Production Branch**: 이 프로젝트는 `claude/new-project-setup-hz2qo8` 브랜치에 있다.
> Vercel은 보통 저장소 기본 브랜치를 배포하므로, 가져온 뒤
> **Settings → Git → Production Branch** 를 `claude/new-project-setup-hz2qo8` 로 바꾸고
> 한 번 더 배포(Deployments → Redeploy)한다.

### 4. 배포
- **Deploy** 클릭 → 1~2분 뒤 완료. 나온 주소 클릭.

### 5. 접속 주소
- 📊 대시보드 → `https://<프로젝트>.vercel.app/`  (루트가 대시보드로 이동)
- ☕ 직원 POS → `https://<프로젝트>.vercel.app/pos/`
- 🎟️ 고객 멤버십 → `https://<프로젝트>.vercel.app/member/?phone=01012345678`
- 🛠️ 관리자 → `https://<프로젝트>.vercel.app/admin/`

> 휴대폰에서도 같은 주소로 열린다. 첫 접속은 함수가 깨어나며 잠깐 느릴 수 있다.

---

## 2단계 — 무료 DB(Neon) 붙여 영구 저장

### 1. Neon 가입 & DB 생성
1. https://neon.tech → GitHub로 가입.
2. **Create Project** → 이름 입력 → 생성.
3. 대시보드의 **Connection string** 복사.
   `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require` 형태여야 한다.

### 2. Vercel에 환경변수 등록
Vercel 프로젝트 → **Settings → Environment Variables** 에서 추가:

| Key | Value |
| --- | --- |
| `DATABASE_URL` | (Neon에서 복사한 연결 문자열) |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_SECRET_KEY` | 아무 긴 무작위 문자열 (예: `slowstep-1q2w3e4r5t6y7u8i-secret`) |
| `DJANGO_SECURE_SSL_REDIRECT` | `False` |
| `DJANGO_ALLOWED_HOSTS` | `.vercel.app` |

> 관리자(/admin) 로그인까지 쓰려면 `DJANGO_CSRF_TRUSTED_ORIGINS` 에
> `https://<프로젝트>.vercel.app` 도 추가한다.

### 3. 재배포
- **Deployments → 최신 항목 ⋯ → Redeploy**.
- 첫 접속 시 자동으로 테이블 생성(migrate) + 데모 데이터 시드가 1회 실행된다.
- 이후부터 입력한 데이터가 **영구 보존**된다.

### 4. (선택) 관리자 계정
서버리스라 셸이 없으므로, 로컬에서 같은 `DATABASE_URL` 로 한 번만 만든다.
(파이썬 가능 PC에서) `slowstep-pos/backend` 에서:
```bash
set DATABASE_URL=postgresql://...   # Windows
python manage.py createsuperuser
```

---

## 동작 원리 (참고)

- `slowstep-pos/vercel.json` — 모든 요청을 Python 함수 `api/index.py` 로 라우팅.
- `slowstep-pos/api/index.py` — Django(WSGI)를 적재하고, DB가 비어 있으면
  자동으로 migrate + 데모 시드.
- `slowstep-pos/requirements.txt` — Vercel이 설치하는 의존성(Django·DRF·
  psycopg·whitenoise 등).
- 정적/웹페이지는 WhiteNoise가 `web/` 폴더를 사이트 루트로 서빙.

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| 404 / 빈 화면 | **Root Directory가 `slowstep-pos`** 인지, Production Branch가 맞는지 |
| 500 에러 | Vercel **Deployments → Functions 로그** 확인. `DATABASE_URL` 형식·`sslmode=require` 여부 |
| 데이터가 사라짐 | 아직 Neon 미연결(임시 저장). 2단계 진행 |
| admin 로그인 실패 | `DJANGO_CSRF_TRUSTED_ORIGINS` 에 `https://<프로젝트>.vercel.app` 추가 |
