"""
슬로우스텝 멤버십 POS 데이터 모델.

설계 상세는 docs/DATA-MODEL.md 참조. 모든 금액은 원(KRW) 정수.
"""
from datetime import timedelta

from django.db import models
from django.utils import timezone


class Store(models.Model):
    """매장. (단일 매장 가정, 다매장은 P3)"""

    name = models.CharField("매장명", max_length=100)
    # 적립률 (0.03 = 3%)
    point_earn_rate = models.DecimalField(
        "적립률", max_digits=4, decimal_places=3, default=0.03
    )
    # 신규 가입 축하 포인트. 0이면 지급하지 않는다.
    # 이관 회원에게는 주지 않는다 — 그쪽은 기존 잔액을 그대로 옮겨 받는다.
    signup_bonus_points = models.PositiveIntegerField("가입 축하 포인트", default=1000)
    stamp_goal = models.PositiveSmallIntegerField("스탬프 목표", default=10)
    # 스탬프를 다 모으면 나가는 건 포인트가 아니라 **룰렛 기회 1번**이다.
    # 이 값은 어디서도 읽지 않는다 — 관리자에서 '3000점이 나간다'고 오해하지
    # 않도록 이름과 설명을 사실에 맞춘다. (열 자체는 과거 데이터 때문에 남겨둔다)
    stamp_reward_points = models.PositiveIntegerField(
        "스탬프 보상 포인트(미사용)",
        default=0,
        help_text="사용하지 않는 값입니다. 스탬프를 다 모으면 룰렛 기회 1번이 지급됩니다.",
    )
    # 커피+디저트 세트 시 디저트 1건당 할인액
    set_discount_amount = models.IntegerField("세트 할인액", default=500)
    # 결제 화면에서 직원이 바로 누를 수 있는 할인율(%). 쉼표로 구분.
    # 비우면 수기 할인 버튼이 아예 안 뜬다.
    discount_rates = models.CharField(
        "수기 할인율(%)", max_length=50, blank=True, default="5,10"
    )
    # 디카페인·오트밀크 등 옵션 추가금
    option_price = models.IntegerField("옵션 추가금", default=500)
    # 옵션 1개당 추가 재료원가(오트밀크·샷 등) — 마진 분석용
    option_cost = models.IntegerField("옵션 추가 원가", default=0)
    # 모든 메뉴에 공통으로 들어가는 밑작업(우유 배합·수제 크림 등).
    # 레시피 화면 맨 위에 항상 띄운다 — 메뉴마다 반복해 적을 내용이 아니다.
    prep_notes = models.TextField("공통 밑작업", blank=True, default="")
    # 부가세율(마진은 공급가=매출÷(1+vat) 기준으로 계산)
    vat_rate = models.DecimalField(
        "부가세율", max_digits=4, decimal_places=3, default=0.10
    )
    # ── 해피아워: 한가한 시간대에 적립을 올려 피크를 분산한다 ──
    # start==end 이면 비활성. 자정을 넘기는 구간(예: 21~1시)도 지원.
    happy_start = models.PositiveSmallIntegerField("해피아워 시작(시)", default=0)
    happy_end = models.PositiveSmallIntegerField("해피아워 종료(시)", default=0)
    happy_multiplier = models.DecimalField(
        "해피아워 적립 배수", max_digits=3, decimal_places=1, default=1.5
    )
    # 영업 상태
    is_open = models.BooleanField("영업중", default=False)
    opened_at = models.DateTimeField("영업 시작 시각", null=True, blank=True)
    created_at = models.DateTimeField("생성 시각", auto_now_add=True)

    class Meta:
        verbose_name = "매장"
        verbose_name_plural = "매장"

    def __str__(self) -> str:
        return self.name

    @property
    def discount_rate_list(self) -> list[int]:
        """'5,10' → [5, 10]. 잘못 적힌 값은 조용히 버린다."""
        out = []
        for chunk in (self.discount_rates or "").split(","):
            chunk = chunk.strip()
            if chunk.isdigit() and 0 < int(chunk) < 100:
                out.append(int(chunk))
        return sorted(set(out))

    @property
    def happy_hour_active(self) -> bool:
        return self.happy_start != self.happy_end

    def is_happy_hour(self, when=None) -> bool:
        """지금이 해피아워인가. 자정을 넘기는 구간도 처리."""
        if not self.happy_hour_active:
            return False
        h = timezone.localtime(when or timezone.now()).hour
        if self.happy_start < self.happy_end:
            return self.happy_start <= h < self.happy_end
        return h >= self.happy_start or h < self.happy_end   # 자정 넘김


class Member(models.Model):
    """회원. 회원번호 = 연락처(phone)."""

    class Tier(models.TextChoices):
        BRONZE = "BRONZE", "브론즈"
        SILVER = "SILVER", "실버"
        GOLD = "GOLD", "골드"

    # 누적 결제액 기반 등급 임계값 (원)
    TIER_THRESHOLDS = (
        (300_000, Tier.GOLD),
        (100_000, Tier.SILVER),
        (0, Tier.BRONZE),
    )
    # 등급이 오를 때 나가는 1+1 쿠폰 장수 (적립률은 등급과 무관하게 동일)
    TIER_COUPONS = {Tier.SILVER: 1, Tier.GOLD: 3}

    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, related_name="members", verbose_name="매장"
    )
    phone = models.CharField("연락처(회원번호)", max_length=20, unique=True)
    name = models.CharField("이름", max_length=50)
    points = models.IntegerField("보유 포인트", default=0)
    total_spent = models.IntegerField("누적 결제액", default=0)
    visit_count = models.IntegerField("방문 횟수", default=0)
    tier = models.CharField(
        "등급", max_length=10, choices=Tier.choices, default=Tier.BRONZE
    )
    stamps = models.IntegerField("스탬프", default=0)
    # 남은 룰렛 기회. 스탬프 완성·연속방문으로 쌓이고, 손님이 직접 돌려 쓴다.
    spins = models.IntegerField("룰렛 기회", default=0)
    # 이미 지급한 등급 쿠폰의 최고 등급 — 강등 후 재승급으로 중복 지급되는 걸 막는다
    tier_rewarded = models.CharField(
        "등급 보상 지급분", max_length=10, choices=Tier.choices, default=Tier.BRONZE
    )
    marketing_opt_in = models.BooleanField("마케팅 수신 동의", default=False)
    # 친구 초대: 내 코드로 친구가 등록하면 둘 다 보상
    referral_code = models.CharField(
        "초대 코드", max_length=12, unique=True, null=True, blank=True
    )
    referred_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="referrals", verbose_name="추천인",
    )
    # 초대 코드를 쓴 시각 — 초대한 쪽의 '하루 1명' 제한을 세는 기준
    referral_used_at = models.DateTimeField("초대 사용 시각", null=True, blank=True)
    # 이관 시점의 방문·누적 스냅샷. **미션은 이 기준선 이후만 센다.**
    # 25번 오신 단골을 그냥 넣으면 '3회 방문' 미션이 처음부터 달성 상태가 되어
    # 보상을 받을 길이 없다 — 오늘 처음 온 손님은 같은 미션으로 500P를 받는데
    # 오래 다닌 분만 0P가 되는 셈이라 거꾸로다.
    # 등급·랭킹은 기준선을 빼지 않는다(그건 지난 기록을 인정해야 하는 값).
    baseline_visit_count = models.IntegerField("이관 시점 방문수", default=0)
    baseline_total_spent = models.IntegerField("이관 시점 누적결제", default=0)
    joined_at = models.DateTimeField("가입 시각", auto_now_add=True)

    class Meta:
        verbose_name = "회원"
        verbose_name_plural = "회원"
        ordering = ["-joined_at"]

    def __str__(self) -> str:
        return f"{self.name}({self.phone})"

    def ensure_referral_code(self) -> str:
        """초대 코드가 없으면 만들어 저장(헷갈리는 0·O·1·I 제외)."""
        if self.referral_code:
            return self.referral_code
        import random

        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        for _ in range(30):
            code = "".join(random.choice(alphabet) for _ in range(6))
            if not Member.objects.filter(referral_code=code).exists():
                self.referral_code = code
                Member.objects.filter(pk=self.pk).update(referral_code=code)
                return code
        raise RuntimeError("초대 코드 생성 실패")

    def compute_tier(self) -> str:
        """누적 결제액으로 등급 계산."""
        for threshold, tier in self.TIER_THRESHOLDS:
            if self.total_spent >= threshold:
                return tier
        return self.Tier.BRONZE


class MenuItem(models.Model):
    """매장 메뉴(POS 주문 화면용). 사장님이 관리자에서 관리."""

    class Category(models.TextChoices):
        COFFEE = "coffee", "커피"
        COLDBREW = "coldbrew", "콜드브루"
        ADE = "ade", "스무디·에이드"
        NONCOFFEE = "noncoffee", "논커피"
        TEA = "tea", "티"
        DESSERT = "dessert", "디저트"

    class Temp(models.TextChoices):
        NONE = "none", "선택없음"
        ICE = "ice", "아이스만"
        HOTICE = "hotice", "핫/아이스"

    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, related_name="menu_items"
    )
    name = models.CharField("메뉴명", max_length=100)
    price = models.IntegerField("가격")
    # 재료원가(1잔·1개당). 마진 = 공급가 − 원가. 0이면 원가 미입력.
    cost = models.IntegerField("재료원가", default=0)
    category = models.CharField(
        "카테고리", max_length=20, choices=Category.choices, default=Category.COFFEE
    )
    # 온도 선택: 없음(디저트) / 아이스만 / 핫·아이스
    temp_option = models.CharField(
        "온도 옵션", max_length=10, choices=Temp.choices, default=Temp.HOTICE
    )
    # 디카페인 변경 가능(커피류) · 오트밀크 변경 가능(라떼류) · 샷 추가 — 각 +옵션추가금
    decaf_available = models.BooleanField("디카페인 선택", default=False)
    oatmilk_available = models.BooleanField("오트밀크 선택", default=False)
    shot_available = models.BooleanField("샷 추가 선택", default=False)
    # 사이즈업 추가금. 0이면 이 메뉴는 사이즈업을 팔지 않는다.
    # 옵션 추가금(디카페인·오트·샷)과 달리 메뉴마다 값이 달라 여기에 둔다.
    size_up_price = models.IntegerField("사이즈업 추가금", default=0)
    # ── 레시피 (직원 참고용) ──
    # HOT/ICE로 배합이 달라지는 메뉴가 있어 따로 둔다. recipe_hot 이 비어 있으면
    # HOT 주문에도 recipe 를 쓴다 — 대부분은 온도만 다르고 배합은 같다.
    recipe = models.TextField("레시피", blank=True, default="")
    recipe_hot = models.TextField("레시피(HOT)", blank=True, default="")
    topping = models.CharField("토핑·데코", max_length=200, blank=True, default="")
    recipe_note = models.CharField("비고(잔·분쇄도 등)", max_length=200, blank=True, default="")
    emoji = models.CharField("이모지", max_length=8, blank=True, default="")
    is_available = models.BooleanField("판매중", default=True)
    # 재고: null=무제한. 0이면 품절 처리.
    stock = models.IntegerField("재고(빈칸=무제한)", null=True, blank=True, default=None)
    sort_order = models.IntegerField("정렬", default=0)

    @property
    def sold_out(self) -> bool:
        return self.stock is not None and self.stock <= 0

    @property
    def has_recipe(self) -> bool:
        return bool(self.recipe or self.recipe_hot)

    def recipe_for(self, temperature: str = "") -> str:
        """그 주문 줄의 온도에 맞는 레시피. HOT 전용이 없으면 기본을 쓴다."""
        if temperature == "hot" and self.recipe_hot:
            return self.recipe_hot
        return self.recipe

    class Meta:
        verbose_name = "메뉴"
        verbose_name_plural = "메뉴"
        ordering = ["category", "sort_order", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.price:,}원)"


class Transaction(models.Model):
    """거래(결제) 1건."""

    class Method(models.TextChoices):
        # 외부 단말(네이버페이 커넥트 멀티패드 등)에서 결제 → 앱은 기록만
        CARD = "CARD", "카드"
        NAVERPAY = "NAVERPAY", "네이버페이"
        EASYPAY = "EASYPAY", "간편결제"  # 삼성/애플페이 등
        CASH = "CASH", "현금"
        # (옵션) Toss PG 실연동용 — 서버가 승인 API 호출
        TOSS_CARD = "TOSS_CARD", "토스-카드(PG)"
        TOSS_EASY = "TOSS_EASY", "토스-간편(PG)"

    class Status(models.TextChoices):
        PENDING = "pending", "승인 전"
        PAID = "paid", "결제완료"
        CANCELED = "canceled", "취소"

    @property
    def is_split(self) -> bool:
        return bool(self.split_method) and self.split_amount > 0

    def amounts_by_method(self) -> dict:
        """{수단: 금액}. 분할이면 둘로 나뉜다. 정산과 화면이 같은 값을 쓴다."""
        if not self.is_split:
            return {self.payment_method: self.net_amount}
        rest = self.net_amount - self.split_amount
        out = {self.split_method: self.split_amount}
        if rest:
            out[self.payment_method] = out.get(self.payment_method, 0) + rest
        return out

    store = models.ForeignKey(
        Store, on_delete=models.PROTECT, related_name="transactions"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
        verbose_name="회원",
    )
    gross_amount = models.IntegerField("주문 총액")
    discount = models.IntegerField("할인 합계", default=0)
    # 직원이 결제 화면에서 직접 누른 할인율(%). 0이면 수기 할인 없음.
    # discount 합계에 이미 포함돼 있고, 여기 따로 남기는 건 **나중에 감사하기
    # 위해서**다 — 세트·쿠폰과 섞이면 누가 얼마를 깎아 줬는지 알 수 없다.
    manual_discount_pct = models.PositiveSmallIntegerField("수기 할인율", default=0)
    points_used = models.IntegerField("사용 포인트", default=0)
    net_amount = models.IntegerField("실결제액")
    points_earned = models.IntegerField("적립 포인트", default=0)
    payment_method = models.CharField(
        "결제수단", max_length=20, choices=Method.choices
    )
    # ── 분할 결제 (현금 얼마 + 카드 나머지) ──────────────────────────
    # 손님이 두 가지로 나눠 내는 경우. split_amount 는 **보조 수단이 받은 금액**,
    # 나머지(net_amount − split_amount)를 payment_method 가 받는다.
    # 결제수단을 표 하나로 늘리지 않고 두 칸으로 둔 이유: 카페에서 셋으로
    # 쪼개는 일은 없고, 이 값을 읽는 곳이 정산의 수단별 집계 한 군데뿐이다.
    split_method = models.CharField(
        "분할 결제수단", max_length=20, choices=Method.choices, blank=True, default=""
    )
    split_amount = models.IntegerField("분할 금액", default=0)
    # 외부 단말 승인번호(정산 대사용, 선택 입력)
    approval_no = models.CharField(
        "단말 승인번호", max_length=50, blank=True, default=""
    )
    status = models.CharField(
        "상태", max_length=10, choices=Status.choices, default=Status.PENDING
    )
    toss_payment_key = models.CharField(
        "토스 결제키", max_length=200, blank=True, default=""
    )
    toss_order_id = models.CharField(
        "주문 ID(멱등키)", max_length=100, blank=True, default="", db_index=True
    )
    created_at = models.DateTimeField("생성 시각", auto_now_add=True)
    paid_at = models.DateTimeField("승인 시각", null=True, blank=True)

    class Meta:
        verbose_name = "거래"
        verbose_name_plural = "거래"
        ordering = ["-created_at"]
        constraints = [
            # 같은 order_id로 결제완료 거래는 1건만 — 재시도/동시요청의
            # 중복 결제를 DB 레벨에서 차단(서비스 로직의 최후 방어선).
            models.UniqueConstraint(
                fields=["toss_order_id"],
                condition=models.Q(status="paid") & ~models.Q(toss_order_id=""),
                name="uniq_paid_toss_order_id",
            ),
        ]

    def __str__(self) -> str:
        return f"거래#{self.pk} {self.net_amount}원 [{self.status}]"


class OrderItem(models.Model):
    """거래에 포함된 주문 항목(메뉴·수량 스냅샷)."""

    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name="items"
    )
    menu_item = models.ForeignKey(
        MenuItem, on_delete=models.SET_NULL, null=True, blank=True
    )
    name = models.CharField("메뉴명(스냅샷)", max_length=100)
    unit_price = models.IntegerField("단가(옵션 포함)")
    # 결제 시점 재료원가 스냅샷(옵션 원가 포함). 원가 변경돼도 과거 마진 불변.
    unit_cost = models.IntegerField("단가 원가(스냅샷)", default=0)
    quantity = models.PositiveIntegerField("수량", default=1)
    # 옵션 스냅샷
    temperature = models.CharField("온도", max_length=4, blank=True, default="")  # "", "ice", "hot"
    decaf = models.BooleanField("디카페인", default=False)
    oatmilk = models.BooleanField("오트밀크", default=False)
    shot = models.BooleanField("샷 추가", default=False)
    size_up = models.BooleanField("사이즈업", default=False)

    class Meta:
        verbose_name = "주문 항목"
        verbose_name_plural = "주문 항목"

    @property
    def line_total(self) -> int:
        return self.unit_price * self.quantity

    @property
    def cost_total(self) -> int:
        return self.unit_cost * self.quantity

    @property
    def option_label(self) -> str:
        parts = []
        if self.temperature == "hot":
            parts.append("HOT")
        elif self.temperature == "ice":
            parts.append("ICE")
        if self.decaf:
            parts.append("디카페인")
        if self.oatmilk:
            parts.append("오트밀크")
        if self.shot:
            parts.append("샷추가")
        if self.size_up:
            parts.append("사이즈업")
        return " · ".join(parts)

    def __str__(self) -> str:
        return f"{self.name} x{self.quantity}"


class PointEntry(models.Model):
    """포인트 원장(적립/사용/조정). 잔액의 진실 원천."""

    class Reason(models.TextChoices):
        EARN = "earn", "적립"
        REDEEM = "redeem", "사용"
        ADJUST = "adjust", "조정"
        MISSION = "mission", "미션 보상"
        STAMP = "stamp", "스탬프 보상"
        REFERRAL = "referral", "초대 보상"
        SIGNUP = "signup", "가입 축하"
        CANCEL = "cancel", "취소 원복"

    member = models.ForeignKey(
        Member, on_delete=models.CASCADE, related_name="point_entries"
    )
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="point_entries",
    )
    delta = models.IntegerField("증감")
    reason = models.CharField("사유", max_length=10, choices=Reason.choices)
    balance_after = models.IntegerField("반영 후 잔액")
    created_at = models.DateTimeField("시각", auto_now_add=True)

    class Meta:
        verbose_name = "포인트 내역"
        verbose_name_plural = "포인트 내역"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        sign = "+" if self.delta >= 0 else ""
        return f"{self.member.name} {sign}{self.delta}P ({self.get_reason_display()})"


class Mission(models.Model):
    """미션 정의."""

    class Condition(models.TextChoices):
        VISIT_COUNT = "visit_count", "방문 횟수"
        TOTAL_SPENT = "total_spent", "누적 결제액"

    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, related_name="missions"
    )
    title = models.CharField("제목", max_length=100)
    description = models.TextField("설명", blank=True, default="")
    condition_type = models.CharField(
        "조건 유형", max_length=20, choices=Condition.choices
    )
    target_value = models.IntegerField("목표값")
    reward_points = models.IntegerField("보상 포인트")
    is_active = models.BooleanField("활성", default=True)
    created_at = models.DateTimeField("생성 시각", auto_now_add=True)

    class Meta:
        verbose_name = "미션"
        verbose_name_plural = "미션"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    def member_value(self, member: Member) -> int:
        """
        회원의 현재 조건 진행값 — **이관 기준선 이후만** 센다.

        이관해 온 누적치를 그대로 쓰면 오래 다닌 손님일수록 미션이 처음부터
        달성 상태로 들어와 보상을 받을 길이 없다. 미션은 지난 기록에 대한
        상이 아니라 앞으로의 행동에 붙는 것이라, 모두 같은 출발선에서 센다.
        (이관하지 않은 회원은 기준선이 0이라 계산이 그대로다)
        """
        if self.condition_type == self.Condition.VISIT_COUNT:
            return max(0, member.visit_count - member.baseline_visit_count)
        if self.condition_type == self.Condition.TOTAL_SPENT:
            return max(0, member.total_spent - member.baseline_total_spent)
        return 0


class MemberQuest(models.Model):
    """
    개인 맞춤 퀘스트 **달성 기록**.

    퀘스트 자체는 회원 데이터에서 매번 생성하므로(quests.py) 저장하지 않는다.
    여기에는 '보상을 지급했다'는 사실만 남겨 중복 지급을 막는다.
    """

    member = models.ForeignKey(
        Member, on_delete=models.CASCADE, related_name="quests"
    )
    key = models.CharField("퀘스트 키", max_length=64)
    kind = models.CharField("유형", max_length=20, blank=True, default="")
    title = models.CharField("제목", max_length=100, blank=True, default="")
    reward_points = models.IntegerField("지급 보상", default=0)
    completed_at = models.DateTimeField("달성 시각", auto_now_add=True)

    class Meta:
        verbose_name = "개인 퀘스트 달성"
        verbose_name_plural = "개인 퀘스트 달성"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "key"], name="uniq_member_quest"
            )
        ]
        ordering = ["-completed_at"]

    def __str__(self) -> str:
        return f"{self.member.name} · {self.title}"


class MemberMission(models.Model):
    """회원별 미션 진행 상태."""

    member = models.ForeignKey(
        Member, on_delete=models.CASCADE, related_name="member_missions"
    )
    mission = models.ForeignKey(
        Mission, on_delete=models.CASCADE, related_name="member_missions"
    )
    progress = models.IntegerField("진행값", default=0)
    is_completed = models.BooleanField("달성", default=False)
    completed_at = models.DateTimeField("달성 시각", null=True, blank=True)

    class Meta:
        verbose_name = "회원 미션"
        verbose_name_plural = "회원 미션"
        unique_together = ("member", "mission")

    def __str__(self) -> str:
        state = "완료" if self.is_completed else f"{self.progress}/{self.mission.target_value}"
        return f"{self.member.name} · {self.mission.title} ({state})"

    def mark_completed(self) -> None:
        self.is_completed = True
        self.completed_at = timezone.now()

class Coupon(models.Model):
    """
    회원이 보유한 쿠폰. 룰렛·등급 승급·랭킹 시상으로 발행된다.

    포인트와 달리 **잔액이 아니라 장 단위**다. 할인율 쿠폰은 결제 때 금액을
    깎고, 음료 쿠폰은 물건으로 나간다 — 원가 성격이 달라 원장(PointEntry)에
    섞지 않고 따로 관리한다.
    """

    class Kind(models.TextChoices):
        DISCOUNT_10 = "discount_10", "10% 할인"
        DISCOUNT_20 = "discount_20", "20% 할인"
        BOGO = "bogo", "음료 1+1"
        FREE_DRINK = "free_drink", "무료 음료"
        BEANS_200 = "beans_200", "원두 200g"

    class Source(models.TextChoices):
        ROULETTE = "roulette", "룰렛"
        TIER = "tier", "등급 승급"
        RANKING = "ranking", "월간 랭킹"
        MANUAL = "manual", "수기 지급"

    # 할인율 쿠폰은 결제 금액에서 그만큼 깎는다(0이면 금액 할인이 아님)
    DISCOUNT_PCT = {Kind.DISCOUNT_10: 10, Kind.DISCOUNT_20: 20}
    DEFAULT_VALID_DAYS = 90

    member = models.ForeignKey(
        Member, on_delete=models.CASCADE, related_name="coupons", verbose_name="회원"
    )
    kind = models.CharField("종류", max_length=20, choices=Kind.choices)
    source = models.CharField(
        "발행 사유", max_length=20, choices=Source.choices, default=Source.ROULETTE
    )
    note = models.CharField("메모", max_length=100, blank=True, default="")
    issued_at = models.DateTimeField("발행 시각", auto_now_add=True)
    expires_at = models.DateTimeField("만료 시각", null=True, blank=True)
    used_at = models.DateTimeField("사용 시각", null=True, blank=True)
    used_transaction = models.ForeignKey(
        Transaction, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="coupons", verbose_name="사용 거래",
    )

    class Meta:
        verbose_name = "쿠폰"
        verbose_name_plural = "쿠폰"
        ordering = ["-issued_at"]
        indexes = [models.Index(fields=["member", "used_at"])]

    def __str__(self) -> str:
        return f"{self.member.name} · {self.get_kind_display()}"

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.expires_at = timezone.now() + timedelta(days=self.DEFAULT_VALID_DAYS)
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and not self.is_expired

    @property
    def discount_pct(self) -> int:
        return self.DISCOUNT_PCT.get(self.kind, 0)
