# 🚀 배포 가이드 — 공개 URL로 대시보드 열기

목표: 인터넷 주소(예: `https://slowstep-pos.onrender.com`)로 **설치 없이**
대시보드·POS·멤버십 페이지에 접속한다. 무료 호스팅 **Render**를 사용한다.

> Django 한 개가 API와 웹페이지를 같은 주소로 함께 서빙하도록 이미 구성돼 있다.
> 따라서 서비스 **하나만** 만들면 된다.

---

## 미리 알아둘 점

- **무료 플랜**이라 15분간 접속이 없으면 잠들고, 다음 접속 시 깨어나는 데 **30초~1분** 걸린다(첫 화면이 느릴 수 있음).
- 무료 플랜은 저장공간이 임시라서, 재시작하면 **데이터가 초기화**된다.
  대신 시작할 때마다 **데모 데이터를 자동으로 다시 채우므로** 대시보드는 항상
  채워진 상태로 보인다. (실제 매장 데이터를 영구 보관하려면 맨 아래 'PostgreSQL' 참고)

---

## 단계별 (수동 생성 · 권장)

### 1. Render 가입
1. https://render.com 접속 → **Get Started** → GitHub 계정으로 가입/로그인.
2. 가입 중 GitHub 저장소 접근 권한을 허용한다(이 저장소를 읽을 수 있어야 함).

### 2. 새 Web Service 만들기
1. 대시보드 우상단 **New +** → **Web Service**.
2. 저장소 목록에서 **`tube10081008-stack/Urge-surfing`** 선택 → **Connect**.

### 3. 설정값 입력 (아래 표 그대로)

| 항목 | 입력값 |
| --- | --- |
| **Name** | `slowstep-pos` (원하는 이름) |
| **Branch** | `claude/new-project-setup-hz2qo8` |
| **Root Directory** | `slowstep-pos/backend` |
| **Runtime / Language** | `Python 3` |
| **Build Command** | `pip install -r requirements-prod.txt && python manage.py collectstatic --noinput` |
| **Start Command** | `python manage.py migrate && python manage.py seed_demo && python manage.py seed_marketing && gunicorn config.wsgi:application` |
| **Instance Type** | `Free` |

### 4. 환경변수 추가 (Advanced → Add Environment Variable)

| Key | Value |
| --- | --- |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | `.onrender.com,localhost,127.0.0.1` |
| `DJANGO_SECURE_SSL_REDIRECT` | `False` |
| `DJANGO_SECRET_KEY` | (아무 긴 무작위 문자열. 예: `slowstep-1q2w3e4r5t6y7u8i9o0p-secret`) |
| `PYTHON_VERSION` | `3.11.9` |

### 5. 배포
1. **Create Web Service** 클릭 → 빌드가 시작된다(첫 배포 약 2~4분).
2. 로그에 `Booting worker` / `Listening at` 가 보이면 성공.
3. 상단의 주소 **`https://slowstep-pos.onrender.com`** 를 클릭.

### 6. 접속 주소
- 📊 점주 대시보드 → `https://<이름>.onrender.com/`  (루트가 대시보드로 이동)
- ☕ 직원 POS → `https://<이름>.onrender.com/pos/`
- 🎟️ 고객 멤버십 → `https://<이름>.onrender.com/member/?phone=01012345678`
- 🛠️ 관리자 → `https://<이름>.onrender.com/admin/` (사용하려면 아래 superuser 생성)

> 휴대폰에서도 같은 주소로 접속된다. POS는 모바일 화면에 맞춰져 있다.

---

## (선택) 관리자 계정 만들기

Render 서비스 페이지 → **Shell** 탭에서:
```bash
python manage.py createsuperuser
```

## (선택) Blueprint로 자동 생성 — 고급

`slowstep-pos/render.yaml` 에 위 설정이 그대로 들어 있다. 이 파일을 저장소
**루트**로 옮기면 Render **New → Blueprint** 한 번으로 자동 생성된다.
(단, 루트에는 다른 프로젝트용 `render.yaml`이 이미 있으니 교체·병합에 주의)

## (선택) 데이터 영구 보관 — PostgreSQL

데모가 아니라 실제 매장 데이터를 쌓으려면:
1. Render **New + → PostgreSQL**(Free) 생성.
2. Web Service 환경변수에 `DATABASE_URL` 추가 →
   값은 DB의 **Internal Connection String**.
3. Start Command에서 `seed_demo`/`seed_marketing` 을 빼면(초기 1회만 시드)
   재배포해도 데이터가 유지된다.

설정 상세(환경변수 전체)는 [`docs/TOSS-INTEGRATION.md`](./docs/TOSS-INTEGRATION.md)·
[`backend/config/settings.py`](./backend/config/settings.py) 참고.
