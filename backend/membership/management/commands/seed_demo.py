"""
데모 데이터 시드: 매장 1 + 미션 + 샘플 회원.

사용: python manage.py seed_demo
멱등(이미 있으면 갱신). 데모/스캐폴드 검증용.
"""
from django.core.management.base import BaseCommand

from membership.models import Member, MenuItem, Mission, Store

# (이름, 가격, 카테고리, 온도옵션, 디카페인, 오트밀크, 이모지, 정렬)
#   온도: hotice=핫/아이스, ice=아이스만, none=선택없음(디저트)
MENU = [
    # ── 커피 (디카페인 선택 가능) ──
    ("아메리카노", 4000, "coffee", "hotice", True, False, "☕", 1),
    ("카페 라떼", 4500, "coffee", "hotice", True, True, "🥛", 2),
    ("바닐라 라떼", 5000, "coffee", "hotice", True, True, "🍦", 3),
    ("오렌지 커피", 5000, "coffee", "ice", True, False, "🍊", 4),
    ("오렌지 비앙코", 5500, "coffee", "ice", True, False, "🍊", 5),
    ("골든애플커피", 5200, "coffee", "ice", True, False, "🍏", 6),
    # ── 콜드브루 (디카페인 불가) ──
    ("콜드브루", 4500, "coldbrew", "ice", False, False, "🧊", 1),
    ("콜드브루 라떼", 5000, "coldbrew", "ice", False, True, "🥛", 2),
    ("콜드브루 슈페너", 5500, "coldbrew", "ice", False, False, "🧊", 3),
    ("콜드브루 라떼 슈페너", 5800, "coldbrew", "ice", False, True, "🥛", 4),
    # ── 스무디·에이드 ──
    ("플레인 요거트 스무디", 5000, "ade", "ice", False, False, "🥤", 1),
    ("딸기 요거트 스무디", 5500, "ade", "ice", False, False, "🍓", 2),
    ("시트러스 요거트 스무디", 5500, "ade", "ice", False, False, "🍊", 3),
    ("자몽 알갱이 에이드", 5500, "ade", "ice", False, False, "🍹", 4),
    ("레드 청포도 스파클링", 5500, "ade", "ice", False, False, "🍇", 5),
    ("토마토 바질 에이드", 5500, "ade", "ice", False, False, "🍅", 6),
    ("쿨라임 민트 에이드", 5500, "ade", "ice", False, False, "🌿", 7),
    # ── 논커피 (오트밀크 옵션 없음) ──
    ("아이스티", 4000, "noncoffee", "ice", False, False, "🧊", 1),
    ("딸기 라떼", 4500, "noncoffee", "ice", False, False, "🍓", 2),
    ("쫀득한 미숫가루 크림 라떼", 5500, "noncoffee", "ice", False, False, "🥛", 3),
    ("딥초코멜로우 (기라델리)", 5500, "noncoffee", "hotice", False, False, "🍫", 4),
    ("허니 자몽 크림 라떼", 5500, "noncoffee", "hotice", False, False, "🍊", 5),
    # ── 티 ──
    ("히비스커스", 4000, "tea", "hotice", False, False, "🌺", 1),
    ("루이보스", 4000, "tea", "hotice", False, False, "🍵", 2),
    ("캐모마일", 4000, "tea", "hotice", False, False, "🌼", 3),
    ("민트", 4000, "tea", "hotice", False, False, "🌿", 4),
    # ── 디저트 ──
    ("플레인 휘낭시에", 2500, "dessert", "none", False, False, "🧁", 1),
    ("꿀고구마 휘낭시에", 3000, "dessert", "none", False, False, "🍠", 2),
    ("라즈베리크럼블 휘낭시에", 3200, "dessert", "none", False, False, "🍰", 3),
    ("얼그레이 마들렌", 3000, "dessert", "none", False, False, "🫖", 4),
    ("밀키 마들렌", 3000, "dessert", "none", False, False, "🧈", 5),
]


class Command(BaseCommand):
    help = "슬로우스텝 데모 데이터(매장·미션·샘플 회원) 시드"

    def handle(self, *args, **options):
        store, _ = Store.objects.get_or_create(
            name="슬로우스텝",
            defaults={
                "point_earn_rate": "0.03",
                "stamp_goal": 10,
                "stamp_reward_points": 3000,
            },
        )
        # 정책 갱신(기존 매장도 반영)
        store.point_earn_rate = "0.03"
        store.save(update_fields=["point_earn_rate"])
        self.stdout.write(f"매장: {store.name} (적립 {float(store.point_earn_rate)*100:.0f}%)")

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

        # 메뉴 시드 (실제 메뉴 + 옵션). 에스프레소 샷은 커피류에만.
        names = []
        for name, price, cat, temp, decaf, oat, emoji, order in MENU:
            MenuItem.objects.update_or_create(
                store=store, name=name,
                defaults={
                    "price": price, "category": cat, "temp_option": temp,
                    "decaf_available": decaf, "oatmilk_available": oat,
                    "shot_available": cat == "coffee",
                    "emoji": emoji, "sort_order": order, "is_available": True,
                },
            )
            names.append(name)
        # 이전 데모 메뉴 등 목록에 없는 항목 제거(주문 이력은 SET_NULL로 보존)
        stale = MenuItem.objects.filter(store=store).exclude(name__in=names)
        removed = stale.count()
        stale.delete()
        self.stdout.write(f"메뉴 {len(names)}종 시드 (정리 {removed}종)")

        self.stdout.write(self.style.SUCCESS("시드 완료 ✅"))
