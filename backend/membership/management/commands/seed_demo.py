"""
데모 데이터 시드: 매장 1 + 미션 + 샘플 회원.

사용: python manage.py seed_demo
멱등(이미 있으면 갱신). 데모/스캐폴드 검증용.
"""
from django.core.management.base import BaseCommand

from membership.models import Member, MenuItem, Mission, Store

# (이름, 가격, 재료원가, 카테고리, 온도옵션, 디카페인, 오트밀크, 이모지, 정렬)
#   온도: hotice=핫/아이스, ice=아이스만, none=선택없음(디저트)
#   원가는 데모용 예시값(매입원가) — 실제는 관리자에서 점주가 입력.
MENU = [
    # ── 커피 (디카페인 선택 가능) ──
    ("아메리카노", 4000, 600, "coffee", "hotice", True, False, "☕", 1),
    ("카페 라떼", 4500, 1100, "coffee", "hotice", True, True, "🥛", 2),
    ("바닐라 라떼", 5000, 1300, "coffee", "hotice", True, True, "🍦", 3),
    ("오렌지 커피", 5000, 1400, "coffee", "ice", True, False, "🍊", 4),
    ("오렌지 비앙코", 5500, 1500, "coffee", "ice", True, False, "🍊", 5),
    ("골든애플커피", 5200, 1400, "coffee", "ice", True, False, "🍏", 6),
    # ── 콜드브루 (디카페인 불가) ──
    ("콜드브루", 4500, 900, "coldbrew", "ice", False, False, "🧊", 1),
    ("콜드브루 라떼", 5000, 1300, "coldbrew", "ice", False, True, "🥛", 2),
    ("콜드브루 슈페너", 5500, 1500, "coldbrew", "ice", False, False, "🧊", 3),
    ("콜드브루 라떼 슈페너", 5800, 1700, "coldbrew", "ice", False, True, "🥛", 4),
    # ── 스무디·에이드 ──
    ("플레인 요거트 스무디", 5000, 1600, "ade", "ice", False, False, "🥤", 1),
    ("딸기 요거트 스무디", 5500, 1900, "ade", "ice", False, False, "🍓", 2),
    ("시트러스 요거트 스무디", 5500, 1800, "ade", "ice", False, False, "🍊", 3),
    ("자몽 알갱이 에이드", 5500, 1700, "ade", "ice", False, False, "🍹", 4),
    ("레드 청포도 스파클링", 5500, 1600, "ade", "ice", False, False, "🍇", 5),
    ("토마토 바질 에이드", 5500, 1700, "ade", "ice", False, False, "🍅", 6),
    ("쿨라임 민트 에이드", 5500, 1500, "ade", "ice", False, False, "🌿", 7),
    # ── 논커피 (오트밀크 옵션 없음) ──
    ("아이스티", 4000, 700, "noncoffee", "ice", False, False, "🧊", 1),
    ("딸기 라떼", 4500, 1500, "noncoffee", "ice", False, False, "🍓", 2),
    ("쫀득한 미숫가루 크림 라떼", 5500, 1600, "noncoffee", "ice", False, False, "🥛", 3),
    ("딥초코멜로우 (기라델리)", 5500, 1700, "noncoffee", "hotice", False, False, "🍫", 4),
    ("허니 자몽 크림 라떼", 5500, 1700, "noncoffee", "hotice", False, False, "🍊", 5),
    # ── 티 ──
    ("히비스커스", 4000, 600, "tea", "hotice", False, False, "🌺", 1),
    ("루이보스", 4000, 600, "tea", "hotice", False, False, "🍵", 2),
    ("캐모마일", 4000, 600, "tea", "hotice", False, False, "🌼", 3),
    ("민트", 4000, 600, "tea", "hotice", False, False, "🌿", 4),
    # ── 디저트 ──
    ("플레인 휘낭시에", 2500, 1100, "dessert", "none", False, False, "🧁", 1),
    ("꿀고구마 휘낭시에", 3000, 1300, "dessert", "none", False, False, "🍠", 2),
    ("라즈베리크럼블 휘낭시에", 3200, 1400, "dessert", "none", False, False, "🍰", 3),
    ("얼그레이 마들렌", 3000, 1200, "dessert", "none", False, False, "🫖", 4),
    ("밀키 마들렌", 3000, 1200, "dessert", "none", False, False, "🧈", 5),
]


# 사이즈업 추가금 — 메뉴마다 다르므로 매장 공통 옵션가와 별개로 둔다.
# 여기 없는 메뉴는 사이즈업을 팔지 않는다(추가금 0).
SIZE_UP = {
    "아메리카노": 1500,
    "카페 라떼": 1500,
    "아이스티": 1500,
    "바닐라 라떼": 2000,
}


class Command(BaseCommand):
    help = "슬로우스텝 기본 데이터(매장·메뉴·미션) 시드 (+샘플 회원)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-members", action="store_true",
            help="샘플 회원을 만들지 않는다(실매장 배포용)",
        )

    def handle(self, *args, **options):
        store, _ = Store.objects.get_or_create(
            name="슬로우스텝",
            defaults={
                "point_earn_rate": "0.03",
                "stamp_goal": 10,
                "stamp_reward_points": 0,
            },
        )
        # 정책 갱신(기존 매장도 반영)
        store.point_earn_rate = "0.03"
        store.option_cost = 300  # 옵션(오트밀크·샷 등) 1개당 추가 원가 데모값
        # 해피아워는 **꺼진 상태가 기본**이다. 어느 시간대가 한가한지는 매장이 정할
        # 일이고, 시드가 매번 켜 버리면 관리자에서 끈 설정이 되살아난다.
        # 쓰시려면 관리자 화면에서 시작·종료 시각을 다르게 넣으면 켜진다.
        store.save(update_fields=["point_earn_rate", "option_cost"])
        self.stdout.write(f"매장: {store.name} (적립 {float(store.point_earn_rate)*100:.0f}%)")

        missions = [
            {
                "title": "이번 시즌 5회 방문",
                "description": "5번 방문하고 1,000P 받기",
                "condition_type": Mission.Condition.VISIT_COUNT,
                "target_value": 5,
                "reward_points": 1000,
            },
            {
                "title": "단골 인증 10회 방문",
                "description": "10번 방문하면 500P",
                "condition_type": Mission.Condition.VISIT_COUNT,
                "target_value": 10,
                "reward_points": 500,
            },
            {
                "title": "누적 5만원 달성",
                "description": "누적 결제 50,000원 달성 시 500P",
                "condition_type": Mission.Condition.TOTAL_SPENT,
                "target_value": 50000,
                "reward_points": 500,
            },
        ]
        for m in missions:
            obj, created = Mission.objects.update_or_create(
                store=store, title=m["title"], defaults=m
            )
            self.stdout.write(("생성: " if created else "갱신: ") + obj.title)

        # 샘플 회원은 개발용이다. 실매장 배포에서 만들면 랭킹·명예의 전당에
        # 가짜 이름이 섞이고 지표가 오염된다(--no-members).
        members = [] if options.get("no_members") else [
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
        for name, price, cost, cat, temp, decaf, oat, emoji, order in MENU:
            MenuItem.objects.update_or_create(
                store=store, name=name,
                defaults={
                    "price": price, "cost": cost, "category": cat, "temp_option": temp,
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

        # 사이즈업 추가금 — 사장님이 관리 화면에서 바꾼 값은 건드리지 않는다
        for name, up in SIZE_UP.items():
            MenuItem.objects.filter(name=name, size_up_price=0).update(size_up_price=up)

        self.stdout.write(self.style.SUCCESS("시드 완료 ✅"))
