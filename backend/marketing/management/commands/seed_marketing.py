"""
대시보드·마케팅 데모용 풍부한 데이터 시드.

기존 회원/매장(membership seed_demo)을 보강해 다양한 등급·휴면·매출
이력을 만든다. 멱등(이미 있으면 건너뜀)이며 flush 후 사용 권장.

사용: python manage.py seed_marketing
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from marketing.models import Campaign, Segment
from membership.models import Member, PointEntry, Store, Transaction

NAMES = [
    "김슬로우", "이천천", "박스텝", "최여유", "정한모금", "한가람", "오로라",
    "서지안", "문도윤", "배하준", "남유진", "조은별",
]


class Command(BaseCommand):
    help = "대시보드/마케팅 데모 데이터(다양한 등급·휴면·매출) 시드"

    def handle(self, *args, **options):
        store, _ = Store.objects.get_or_create(
            name="슬로우스텝",
            defaults={"point_earn_rate": "0.05", "stamp_goal": 10, "stamp_reward_points": 3000},
        )
        rng = random.Random(42)
        now = timezone.now()
        rate = float(store.point_earn_rate)

        created = 0
        for i, name in enumerate(NAMES):
            phone = "010" + f"{10000000 + i * 111111:08d}"
            member, is_new = Member.objects.get_or_create(
                phone=phone,
                defaults={"name": name, "store": store, "marketing_opt_in": rng.random() > 0.2},
            )
            if not is_new:
                continue
            created += 1

            # 회원별 방문 횟수·최근성 다양화 (일부는 휴면)
            visits = rng.choice([1, 2, 3, 5, 8, 12, 20])
            last_gap = rng.choice([1, 3, 7, 14, 25, 40, 70])  # 마지막 방문 며칠 전
            total_spent = 0
            points = 0
            for v in range(visits):
                amount = rng.choice([4500, 5500, 6500, 8000, 12000, 15000])
                # 방문을 과거~최근에 분포. 마지막 방문이 last_gap일 전.
                days_ago = last_gap + (visits - 1 - v) * rng.randint(2, 9)
                paid_at = now - timedelta(days=days_ago, hours=rng.randint(0, 10))
                earned = round(amount * rate)
                txn = Transaction.objects.create(
                    store=store, member=member, gross_amount=amount,
                    points_used=0, net_amount=amount, points_earned=earned,
                    payment_method=rng.choice(["TOSS_CARD", "TOSS_EASY", "CASH"]),
                    status=Transaction.Status.PAID, paid_at=paid_at,
                    toss_order_id=f"seed-{member.id}-{v}",
                )
                Transaction.objects.filter(pk=txn.pk).update(created_at=paid_at)
                total_spent += amount
                points += earned
                PointEntry.objects.create(
                    member=member, transaction=txn, delta=earned,
                    reason=PointEntry.Reason.EARN, balance_after=points,
                )

            member.total_spent = total_spent
            member.visit_count = visits
            member.points = points
            member.stamps = visits % store.stamp_goal
            member.tier = member.compute_tier()
            member.save()

        self.stdout.write(f"회원 {created}명 + 거래 시드 완료")

        # 샘플 세그먼트
        seg_dormant, _ = Segment.objects.get_or_create(
            name="휴면 고객(30일+)",
            defaults={
                "description": "30일 이상 미방문 + 수신동의 회원 재방문 유도",
                "inactive_days": 30, "require_opt_in": True,
            },
        )
        Segment.objects.get_or_create(
            name="VIP(골드 등급)",
            defaults={"description": "골드 등급 단골 대상 특별 혜택", "tier": "GOLD"},
        )
        Segment.objects.get_or_create(
            name="단골(5회+ 방문)",
            defaults={"description": "5회 이상 방문한 충성 고객", "min_visits": 5},
        )
        self.stdout.write("세그먼트 3종 생성")

        # 샘플 캠페인(작성중)
        Campaign.objects.get_or_create(
            name="휴면 고객 컴백 쿠폰",
            defaults={
                "segment": seg_dormant, "is_ad": True,
                "message_template": (
                    "{이름}님, 오랜만이에요 ☕\n슬로우스텝에서 보고 싶었어요. "
                    "지금 보유 포인트 {포인트}P로 따뜻한 한 잔 어떠세요?\n"
                    "이번 주 방문 시 아메리카노 1+1!"
                ),
            },
        )
        self.stdout.write(self.style.SUCCESS("마케팅 데모 시드 완료 ✅"))
