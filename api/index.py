"""
Vercel 서버리스 진입점 — Django(WSGI) 전체를 하나의 함수로 서빙.

- API(/api/v1) + 웹페이지(/pos /member /dashboard)를 같은 함수가 처리.
- 콜드스타트 시 DB 스키마가 없으면 자동 migrate + 데모 시드(터미널 불필요).
- DATABASE_URL(Neon 등) 주입 시 영구 Postgres, 미주입 시 /tmp SQLite(임시).
"""
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("slowstep")

# backend/ 를 import 경로에 추가 (이 파일은 slowstep-pos/api/index.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()


def _ensure_database() -> None:
    """스키마가 없으면 migrate, 비어 있으면 데모 데이터 시드(멱등)."""
    from django.core.management import call_command
    from django.db.utils import OperationalError, ProgrammingError

    from membership.models import Store

    try:
        seeded = Store.objects.exists()
    except (OperationalError, ProgrammingError):
        call_command("migrate", "--noinput")
        seeded = False

    if not seeded:
        try:
            call_command("seed_demo")
            call_command("seed_marketing")
        except Exception as exc:  # 시드 실패는 치명적이지 않음
            log.warning("seed skipped: %s", exc)


try:
    _ensure_database()
except Exception as exc:  # 부팅은 계속, 로그만 남김
    log.error("DB setup error: %s", exc)

from django.core.wsgi import get_wsgi_application  # noqa: E402

# Vercel @vercel/python 은 `app`(WSGI/ASGI)을 자동 인식한다.
app = get_wsgi_application()
application = app
