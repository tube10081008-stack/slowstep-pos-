"""
데모 데이터 시드: 매장 1 + 미션 + 샘플 회원.

사용: python manage.py seed_demo
멱등(이미 있으면 갱신). 데모/스캐폴드 검증용.
"""
from django.core.management.base import BaseCommand

from membership.models import Member, MenuItem, Mission, Store

MENU = [
    # (이름, 가격, 카테고리, 이모지, 정렬)
    ("아메리카노", 4500, "coffee", "☕", 1),
    ("카페라떼", 5000, "coffee", "🥛", 2),
    ("카푸치노", 5000, "coffee", "☕", 3),
    ("바닐라라떼", 5500, "coffee", "🍦", 4),
    ("카페모카", 5500, "coffee", "🍫", 5),
    ("콜드브루", 5500, "coffee", "🧊", 6),
    ("디카페인 아메리카노", 5000, "coffee", "🌙", 7),
    ("초코라떼", 5500, "noncoffee", "🍫", 1),
    ("녹차라떼", 5500, "noncoffee", "🍵", 2),
    ("고구마라떼", 5500, "noncoffee", "🍠", 3),
    ("밀크티", 5500, "ade", "🧋", 1),
    ("자몽에이드", 6000, "ade", "🍊", 2),
    ("청귤에이드", 6000, "ade", "🍋", 3),
    ("딸기라떼", 6000, "ade", "🍓", 4),
    ("크로플", 6500, "dessert", "🧇", 1),
    ("치즈케이크", 6800, "dessert", "🍰", 2),
    ("초코쿠키", 3500, "dessert", "🍪", 3),
    ("휘낭시에", 4000, "dessert", "🧁", 4),
]


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

        # 메뉴 시드
        menu_n = 0
        for name, price, cat, emoji, order in MENU:
            _, created = MenuItem.objects.update_or_create(
                store=store, name=name,
                defaults={
                    "price": price, "category": cat,
                    "emoji": emoji, "sort_order": order, "is_available": True,
                },
            )
            menu_n += 1
        self.stdout.write(f"메뉴 {menu_n}종 시드")

        self.stdout.write(self.style.SUCCESS("시드 완료 ✅"))
