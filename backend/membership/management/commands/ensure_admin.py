"""
관리자 계정 보장 — 환경변수로 1회 생성(멱등).

서버리스(Vercel)는 셸이 없어 `createsuperuser`를 실행할 수 없다. 이 명령은
콜드스타트 부팅 시 호출돼, 환경변수가 있고 계정이 아직 없으면 만들어 준다.

사용 환경변수:
  DJANGO_SUPERUSER_USERNAME · DJANGO_SUPERUSER_PASSWORD · DJANGO_SUPERUSER_EMAIL(선택)

이미 같은 아이디가 있으면 **아무것도 하지 않는다**(비밀번호를 덮어쓰지 않음).
→ 나중에 관리자에서 비밀번호를 바꿔도 배포할 때마다 되돌아가지 않는다.
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "환경변수(DJANGO_SUPERUSER_*)로 관리자 계정을 1회 생성한다(멱등)."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()

        if not username or not password:
            self.stdout.write("관리자 환경변수 미설정 — 건너뜀")
            return

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"관리자 '{username}' 이미 존재 — 유지")
            return

        User.objects.create_superuser(
            username=username, email=email or "", password=password
        )
        # 비밀번호는 절대 로그에 남기지 않는다.
        self.stdout.write(self.style.SUCCESS(f"관리자 '{username}' 생성 완료"))
