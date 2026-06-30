"""
슬로우스텝 멤버십 POS 백엔드 설정.

기본값은 로컬 개발(PoC)에 맞춰져 있고, 환경변수를 주입하면 운영 모드로 승격된다.
- 환경변수 없음 → 개발: SQLite, DEBUG=True, CORS 전체 허용
- 환경변수 주입 → 운영: PostgreSQL(DATABASE_URL), DEBUG=False, CORS 제한, 보안 헤더
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, str(default)).lower() in ("1", "true", "yes", "on")


def env_list(key: str) -> list[str]:
    return [v.strip() for v in os.environ.get(key, "").split(",") if v.strip()]


# ── 보안 ────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-slowstep-pos-dev-key-do-not-use-in-prod",
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS") or ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 서드파티
    "rest_framework",
    "corsheaders",
    # 로컬 앱
    "membership",
    "marketing",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ── 데이터베이스 ────────────────────────────────────────────────
# DATABASE_URL 주입 시 PostgreSQL, 아니면 SQLite.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL:
    try:
        import dj_database_url

        DATABASES = {
            "default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)
        }
    except ImportError:
        raise RuntimeError(
            "DATABASE_URL이 설정됐지만 dj-database-url 미설치. "
            "requirements-prod.txt를 설치하세요."
        )
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── DRF ─────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        # P0 데모: 인증 생략. P1에서 직원 인증으로 교체.
        "rest_framework.permissions.AllowAny",
    ],
}

# ── CORS ────────────────────────────────────────────────────────
_cors_origins = env_list("CORS_ALLOWED_ORIGINS")
if _cors_origins:
    CORS_ALLOWED_ORIGINS = _cors_origins
else:
    CORS_ALLOW_ALL_ORIGINS = True

# ── Toss페이먼츠 ────────────────────────────────────────────────
# 미주입 시 payments.py가 Mock 승인으로 폴백(스캐폴드/데모).
TOSS_SECRET_KEY = os.environ.get("TOSS_SECRET_KEY", "")
TOSS_CLIENT_KEY = os.environ.get(
    "TOSS_CLIENT_KEY", "test_ck_docs_placeholder"
)
TOSS_API_BASE = os.environ.get("TOSS_API_BASE", "https://api.tosspayments.com")
TOSS_WEBHOOK_SECRET = os.environ.get("TOSS_WEBHOOK_SECRET", "")

# ── 알림톡(카카오) ──────────────────────────────────────────────
# 미주입 시 alimtalk.py가 Mock 발송으로 폴백(스캐폴드/데모).
# 실연동은 NHN Cloud / Solapi / Aligo 등 발송 대행사 키를 주입.
ALIMTALK_API_KEY = os.environ.get("ALIMTALK_API_KEY", "")
ALIMTALK_SENDER_KEY = os.environ.get("ALIMTALK_SENDER_KEY", "")  # 발신 프로필 키
ALIMTALK_API_BASE = os.environ.get("ALIMTALK_API_BASE", "")
# 발신번호(매장 대표번호) — 알림톡 실패 시 문자(LMS) 대체발송에 사용
ALIMTALK_SENDER_PHONE = os.environ.get("ALIMTALK_SENDER_PHONE", "")

# ── 운영 보안 헤더 (DEBUG=False일 때) ───────────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "3600"))
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
