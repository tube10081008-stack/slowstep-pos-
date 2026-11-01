"""
사장님이 정리한 레시피를 메뉴에 붙인다.

    python manage.py seed_recipes

메뉴명이 안 맞는 레시피와 레시피가 없는 메뉴를 **끝에 보고**한다.
조용히 넘어가면 "왜 저 메뉴만 레시피가 안 뜨지"로 나중에 헤맨다.

이후 수정은 관리자 화면(메뉴 → 레시피)에서 한다. 이 명령은 최초 1회
일괄 입력용이며, 이미 적어둔 레시피는 덮어쓰지 않는다(--force 로 덮어쓰기).
"""
from django.core.management.base import BaseCommand

from membership.models import MenuItem, Store

# 매장 공통 밑작업 — 메뉴마다 반복해 적을 내용이 아니라 매장에 둔다
PREP = (
    "우유 밑작업 — 우유 1000ml + 무가당 연유 220g 혼합 (모든 우유 메뉴 기본)\n"
    "수제 크림 — 크림 300g + 카라멜시럽 80g (취향에 따라 60~70g 조절)"
)

# 메뉴명 → (레시피, HOT 전용 레시피, 토핑·데코, 비고)
RECIPES = {
    # ── 스무디 · 에이드 ──
    "플레인 요거트 스무디": (
        "우유 160g + 파우더 70g + 얼음 240g 블렌딩", "", "", ""),
    "딸기 요거트 스무디": (
        "우유 140g + 파우더 50g + 얼음 190g + 냉동딸기 50g + 딸기청 55g 블렌딩", "", "", ""),
    "시트러스 요거트 스무디": (
        "우유 180g + 파우더 40g + 한라봉 베이스 55g + 얼음 240g 블렌딩",
        "", "말린 오렌지", ""),
    "자몽 알갱이 에이드": (
        "자몽청 50g + Friss 농축액 20g + 얼음 + 탄산수", "", "", ""),
    "토마토 바질 에이드": (
        "시럽 5g + 청 액체 60g + 토마토 소스 30g + 얼음 + 탄산수 + 토마토 3알",
        "", "바질 2개", ""),
    "쿨라임 민트 에이드": (
        "라임청 액체 60g + 쿨라임 민트 30g + 얼음 + 탄산수 + 라임 과육 2개", "", "", ""),
    "레드 청포도 스파클링": (
        "잔: 청포도 베이스 50g\n"
        "별도: 탄산수 1캔 + 히비스커스 10g + 쿨라임 10g 혼합 후 합치기", "", "", ""),

    # ── 커피 ──
    "카페 라떼": (
        "우유 180g + 얼음 가득 + 샷", "우유 200g + 샷", "", "14oz"),
    "바닐라 라떼": (
        "바닐라시럽 25g + 샷 + 우유 175g + 얼음 가득",
        "바닐라시럽 25g + 샷 + 우유 180g", "", "14oz"),
    "오렌지 커피": (
        "한라봉 베이스 35g + 오렌지쥬스 180g + 얼음 + 샷", "", "말린 오렌지", ""),
    "오렌지 비앙코": (
        "한라봉 베이스 40g + 우유 120g + 샷 + 크림 35g", "", "", "14oz"),
    "골든애플커피": (
        "1. 꿀 15g + 사과쥬스 120g 섞기\n"
        "2. 샷 추출 후 큰 잔에서 거품 내기\n"
        "3. 쥬스 컵에 얼음 반 스쿱\n"
        "4. 거품 낸 샷 붓기", "", "라임 2조각", "14oz"),

    # ── 콜드브루 ──
    "콜드브루": (
        "원액 80g + 물 120g 잘 섞은 후 얼음 가득", "", "", "EK 분쇄도 5 / 14oz"),
    "콜드브루 라떼": (
        "원액 90g + 우유 110g + 얼음", "", "", "시럽 추가 추천 / 14oz"),
    "콜드브루 슈페너": (
        "원액 110g + 시럽 20g 섞기 + 얼음 반 스쿱 + 크림 35~40g",
        "", "코코아 파우더", "크림:연유 = 40:1 / 14oz"),
    "콜드브루 라떼 슈페너": (
        "원액 100g + 우유 40g + 시럽 20g + 얼음 반 스쿱 + 크림 가득",
        "", "코코아 파우더", "14oz"),

    # ── 논커피 ──
    "딸기 라떼": (
        "딸기청 50g + 얼음 가득 + 우유 180g", "", "", ""),
    "쫀득한 미숫가루 크림 라떼": (
        "찬우유 230g + 꿀 20g + 미숫가루 50g + 얼음 조금 + 수제크림 30g",
        "", "인절미떡 5개", ""),
    "딥초코멜로우 (기라델리)": (
        "기라델리 30g + 헤이즐넛 시럽 5g + 얼음 2/3 + 찬우유 130g + 크림 30g",
        "기라델리 20g + 헤이즐넛 시럽 5g + 스팀우유 170g + 크림 30g",
        "판초콜릿 2개 + 마시멜로 2개(HOT은 토치) + 카카오파우더 마무리",
        "ICE 14oz"),
    "허니 자몽 크림 라떼": (
        "자몽청 60g + 꿀 15g + 찬우유 80g + 얼음 2/3 + 수제크림 30g",
        "자몽청 50g + 꿀 15g + 스팀우유 120g + 수제크림 30g",
        "말린 자몽 1개", "ICE 14oz"),
    "아이스티": (
        "베이스 200ml + 얼음 가득", "", "", ""),

    # ── 아직 메뉴로 등록되지 않은 레시피 ──
    # 지우지 않고 남겨 둔다. 메뉴를 등록한 뒤 이 명령을 다시 돌리면 그대로 붙는다.
    # (지금은 실행할 때마다 "메뉴에 없다"고 경고한다)
    "엑셀런트 라떼": (
        "아몬드시럽 5g + 헤이즐넛시럽 10g + 우유 120g + 샷 + 얼음 반 스쿱",
        "", "엑셀런트 2개", "14oz"),
    "우베 크림라떼": (
        "크림 제조: 휘핑크림 50g + 우베리큐드 20g + 바닐라소스 10g\n"
        "음료 제조: 우유 150g + 바닐라소스 10g + 얼음 55g + 커피샷 + 제조크림 50g",
        "", "건조코코넛", ""),
    "우베 라떼": (
        "우유 150g + 바닐라소스 10g + 얼음 반 스쿱 + 우베리큐드 25g",
        "우유 160g + 바닐라소스 15g + 우베리큐드 25g", "건조코코넛", ""),
    "수박쥬스": (
        "시럽 5g + 수박시럽 5g + 수박즙 220g + 나타드코코 20g",
        "", "냉동수박 5개", ""),
}


class Command(BaseCommand):
    help = "메뉴에 레시피를 일괄 입력한다(기존 레시피는 보존, --force 로 덮어쓰기)"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="이미 적혀 있는 레시피도 덮어쓴다")

    def handle(self, *args, **options):
        force = options["force"]
        store = Store.objects.first()
        if store is None:
            self.stderr.write("매장 설정이 없습니다. seed_demo 를 먼저 실행하세요.")
            return

        if force or not store.prep_notes:
            store.prep_notes = PREP
            store.save(update_fields=["prep_notes"])
            self.stdout.write("공통 밑작업 저장")

        by_name = {m.name: m for m in MenuItem.objects.all()}
        written, kept, unmatched = 0, 0, []

        for name, (recipe, hot, topping, note) in RECIPES.items():
            item = by_name.get(name)
            if item is None:
                unmatched.append(name)
                continue
            if item.has_recipe and not force:
                kept += 1
                continue
            item.recipe, item.recipe_hot = recipe, hot
            item.topping, item.recipe_note = topping, note
            item.save(update_fields=["recipe", "recipe_hot", "topping", "recipe_note"])
            written += 1

        self.stdout.write(self.style.SUCCESS(
            f"레시피 {written}건 입력" + (f" · {kept}건 유지(이미 작성됨)" if kept else "")
        ))

        # ── 어긋난 것들을 눈에 보이게 ──
        if unmatched:
            self.stdout.write(self.style.WARNING(
                "\n메뉴에 없어서 넣지 못한 레시피:"))
            for n in unmatched:
                self.stdout.write(f"  · {n}")
            self.stdout.write("  → 판매할 메뉴라면 관리자에서 먼저 등록해 주세요.")

        missing = [
            m.name for m in MenuItem.objects.filter(is_available=True).order_by("category")
            if not m.has_recipe
        ]
        if missing:
            self.stdout.write(self.style.WARNING("\n레시피가 아직 없는 판매 메뉴:"))
            for n in missing:
                self.stdout.write(f"  · {n}")
