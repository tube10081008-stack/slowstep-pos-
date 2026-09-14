"""
데모 시드 데이터만 정확히 제거 — 실매장 전환용.

콜드스타트 자동 시드(seed_demo·seed_marketing)가 넣은 **가짜 고객·거래·세그먼트**를
지운다. 실제 고객 데이터는 건드리지 않는다(시드 서명으로만 대상 선별).

지우는 것:
- seed_demo 샘플 회원 3명 / seed_marketing 데모 회원 12명 (연락처가 시드 패턴과 일치)
- 시드 거래(`toss_order_id`가 `seed-`로 시작) + 위 회원들에게 달린 거래
- 데모 세그먼트 3종 · 데모 캠페인 1종
지우지 않는 것: **매장 설정·메뉴(원가 포함)·미션** — 운영에 그대로 쓴다.

서버리스에는 셸이 없으므로 환경변수 `PURGE_DEMO=true` 로도 1회 실행된다
(대상이 없으면 아무 일도 하지 않으므로 켜둔 채로 둬도 안전).

사용: python manage.py purge_demo [--dry-run]
"""
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction

from membership.models import Member, PointEntry, Transaction

# seed_demo 가 만드는 샘플 회원
SEED_DEMO_PHONES = ["01012345678", "01023456789", "01034567890"]
# seed_marketing 이 만드는 데모 회원: "010" + (10000000 + i*111111)
SEED_MARKETING_PHONES = [
    "010" + f"{10000000 + i * 111111:08d}" for i in range(12)
]
DEMO_PHONES = set(SEED_DEMO_PHONES) | set(SEED_MARKETING_PHONES)

DEMO_SEGMENTS = ["휴면 고객(30일+)", "VIP(골드 등급)", "단골(5회+ 방문)"]
DEMO_CAMPAIGNS = ["휴면 고객 컴백 쿠폰"]


class Command(BaseCommand):
    help = "데모 시드 데이터(가짜 회원·거래·세그먼트)만 제거. 메뉴·매장 설정은 유지."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="삭제 없이 대상만 집계"
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]

        demo_members = Member.objects.filter(phone__in=DEMO_PHONES)
        member_ids = list(demo_members.values_list("id", flat=True))

        # 시드 거래 + 데모 회원에게 달린 거래.
        # (회원을 먼저 지우면 거래가 member=NULL 로 남아 '비회원 매출'로
        #  집계에 계속 잡히므로 거래를 먼저 지운다.)
        demo_txns = Transaction.objects.filter(toss_order_id__startswith="seed-")
        if member_ids:
            demo_txns = demo_txns | Transaction.objects.filter(member_id__in=member_ids)
        demo_txns = demo_txns.distinct()

        counts = {
            "회원": demo_members.count(),
            "거래": demo_txns.count(),
            "포인트내역": PointEntry.objects.filter(member_id__in=member_ids).count(),
        }

        try:
            from marketing.models import Campaign, Segment

            segs = Segment.objects.filter(name__in=DEMO_SEGMENTS)
            camps = Campaign.objects.filter(name__in=DEMO_CAMPAIGNS)
            counts["세그먼트"] = segs.count()
            counts["캠페인"] = camps.count()
        except Exception:  # marketing 앱이 없을 수도 있음
            segs = camps = None

        summary = ", ".join(f"{k} {v}건" for k, v in counts.items())
        if not any(counts.values()):
            self.stdout.write("데모 데이터 없음 — 건너뜀")
            return

        if dry:
            self.stdout.write(f"[모의 실행] 삭제 대상: {summary}")
            return

        with db_transaction.atomic():
            if camps is not None:
                camps.delete()  # MessageLog 는 cascade
                segs.delete()
            demo_txns.delete()
            # 회원 삭제 시 PointEntry·MemberMission 은 cascade
            demo_members.delete()

        self.stdout.write(self.style.SUCCESS(f"데모 데이터 삭제 완료: {summary}"))
