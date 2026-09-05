"""데이터 정합성 점검 — `python manage.py check_integrity`."""
from django.core.management.base import BaseCommand

from membership.integrity import run_all


class Command(BaseCommand):
    help = "포인트 원장·거래 금액·누적치 등 돈과 관련된 불변식을 점검한다."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10, help="검사별 예시 개수")

    def handle(self, *args, **options):
        r = run_all(limit=options["limit"])
        c = r["counts"]
        self.stdout.write(
            f"회원 {c['members']} · 거래 {c['transactions']} · "
            f"원장 {c['point_entries']} · 쿠폰 {c['coupons']}\n"
        )
        for chk in r["checks"]:
            mark = "OK " if chk["bad"] == 0 else "!! "
            style = self.style.SUCCESS if chk["bad"] == 0 else self.style.ERROR
            self.stdout.write(style(f"{mark}{chk['name']}: {chk['bad']}건"))
            for s in chk["sample"]:
                self.stdout.write(f"     {s}")
            if chk["bad"]:
                self.stdout.write(f"     → {chk['hint']}")
        self.stdout.write("")
        if r["ok"]:
            self.stdout.write(self.style.SUCCESS("정합성 이상 없음 ✅"))
        else:
            self.stdout.write(self.style.ERROR(f"문제 {r['total_problems']}건"))
