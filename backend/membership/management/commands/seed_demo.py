"""
데모 데이터 시드: 매장 1 + 미션 + 샘플 회원.

사용: python manage.py seed_demo
멱등(이미 있으면 갱신). 데모/스캐폴드 검증용.
"""
from django.core.management.base import BaseCommand

from membership.models import Member, Mission, Store


class Command(BaseCommand):
    help = "슬로우스텝 데모 데이터(매장·미션·샘플 회원) 시드"

    def handle(self, *args, **options):
        store, _ = Store.objects.get_or_create(
            name="슬로우스텝",
            defaults={
                "point_earn_rate": "0.05",
                "stamp_goal": 10,
                "stamp_reward_points": 3000,
            },
        )
        self.stdout.write(f"매장: {store.name}")

        missions = [
            {
                "title": "이번 시즌 3회 방문",
                "description": "3번 방문하고 1,000P 받기",
                "condition_type": Mission.Condition.VISIT_COUNT,
                "target_value": 3,
                "reward_points": 1000,
            },
            {
                "title": "단골 인증 10회 방문",
                "description": "10번 방문하면 5,000P",
                "condition_type": Mission.Condition.VISIT_COUNT,
                "target_value": 10,
                "reward_points": 5000,
            },
            {
                "title": "누적 5만원 달성",
                "description": "누적 결제 50,000원 달성 시 2,000P",
                "condition_type": Mission.Condition.TOTAL_SPENT,
                "target_value": 50000,
                "reward_points": 2000,
            },
        ]
        for m in missions:
            obj, created = Mission.objects.update_or_create(
                store=store, title=m["title"], defaults=m
            )
            self.stdout.write(("생성: " if created else "갱신: ") + obj.title)

        members = [
            {"phone": "01012345678", "name": "김슬로우", "marketing_opt_in": True},
            {"phone": "01023456789", "name": "이천천", "marketing_opt_in": True},
            {"phone": "01034567890", "name": "박스텝", "marketing_opt_in": False},
        ]
        for mem in members:
            obj, created = Member.objects.get_or_create(
                phone=mem["phone"],
                defaults={**mem, "store": store},
            )
            self.stdout.write(("생성: " if created else "존재: ") + str(obj))

        self.stdout.write(self.style.SUCCESS("시드 완료 ✅"))
