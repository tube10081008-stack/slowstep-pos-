"""
Vercel 서버리스 진입점 — Django(WSGI) 전체를 하나의 함수로 서빙.

- API(/api/v1) + 웹페이지(/pos /member /dashboard)를 같은 함수가 처리.
- 콜드스타트 시 DB 스키마가 없으면 자동 migrate + 데모 시드(터미널 불필요).
- DATABASE_URL(Neon 등) 주입 시 영구 Postgres, 미주입 시 /tmp SQLite(임시).

복원력 설계:
- 부팅 시 DB 준비(migrate/seed)가 실패해도 인스턴스를 죽이지 않고,
  이후 매 요청 진입 시 준비를 재시도한다(성공하면 플래그로 스킵).
  → Neon 콜드스타트(수 초) 타이밍에 깨어난 인스턴스가 영구 500에 빠지지 않음.
- Postgres에서는 advisory lock으로 동시 콜드스타트 인스턴스들의
  migrate/seed 동시 실행(레이스)을 막는다.
"""
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger("slowstep")

# backend/ 를 import 경로에 추가 (이 파일은 <repo>/api/index.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

# migrate/seed 동시 실행 방지용 advisory lock 키(임의 고정값).
_MIGRATION_LOCK_KEY = 815_001


def _ensure_database() -> None:
    """스키마가 없으면 migrate, 비어 있으면 데모 데이터 시드(멱등).

    Postgres에서는 advisory lock으로 감싸 동시 인스턴스의 중복 실행을 막는다.
    """
    from django.core.management import call_command
    from django.db import connection
    from django.db.utils import OperationalError, ProgrammingError

    from membership.models import Store

    is_pg = connection.vendor == "postgresql"
    if is_pg:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", [_MIGRATION_LOCK_KEY])
    try:
        try:
            seeded = Store.objects.exists()
        except (OperationalError, ProgrammingError):
            call_command("migrate", "--noinput")
            seeded = Store.objects.exists()

        if not seeded:
            try:
                call_command("seed_demo")
                call_command("seed_marketing")
            except Exception as exc:  # 시드 실패는 치명적이지 않음
                log.warning("seed skipped: %s", exc)
    finally:
        if is_pg:
            try:
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s)", [_MIGRATION_LOCK_KEY]
                    )
            except Exception:  # 연결이 이미 닫혔으면 세션 종료로 자동 해제됨
                pass


_db_ready = False
_db_lock = threading.Lock()


def _prepare_database() -> None:
    """DB 준비를 1회 성공할 때까지 재시도 가능하게 감싼 래퍼(스레드 안전)."""
    global _db_ready
    if _db_ready:
        return
    with _db_lock:
        if _db_ready:
            return
        _ensure_database()
        _db_ready = True


# 부팅 시 1차 시도 — 실패해도 인스턴스는 살리고 요청 시 재시도.
try:
    _prepare_database()
except Exception as exc:
    log.error("DB setup deferred (will retry per-request): %s", exc)

from django.core.wsgi import get_wsgi_application  # noqa: E402

_django_app = get_wsgi_application()


def app(environ, start_response):
    """WSGI 진입점: DB가 아직 준비 안 됐으면 요청마다 재시도 후 위임."""
    if not _db_ready:
        try:
            _prepare_database()
        except Exception as exc:
            log.error("DB setup retry failed: %s", exc)
    return _django_app(environ, start_response)


# Vercel @vercel/python 은 `app`(WSGI/ASGI)을 자동 인식한다.
application = app
