"""
슬로우스텝 멤버십 POS 데이터 모델.

설계 상세는 docs/DATA-MODEL.md 참조. 모든 금액은 원(KRW) 정수.
"""
from django.db import models
from django.utils import timezone


class Store(models.Model):
    """매장. (단일 매장 가정, 다매장은 P3)"""

    name = models.CharField("매장명", max_length=100)
    # 적립률 (0.05 = 5%)
    point_earn_rate = models.DecimalField(
        "적립률", max_digits=4, decimal_places=3, default=0.05
    )
    stamp_goal = models.PositiveSmallIntegerField("스탬프 목표", default=10)
    stamp_reward_points = models.PositiveIntegerField(
        "스탬프 달성 보상 포인트", default=3000
    )
    # 커피+디저트 세트 시 디저트 1건당 할인액
    set_discount_amount = models.IntegerField("세트 할인액", default=500)
    # 디카페인·오트밀크 등 옵션 추가금
    option_price = models.IntegerField("옵션 추가금", default=500)
    created_at = models.DateTimeField("생성 시각", auto_now_add=True)

    class Meta:
        verbose_name = "매장"
        verbose_name_plural = "매장"

    def __str__(self) -> str:
        return self.name


class Member(models.Model):
    """회원. 회원번호 = 연락처(phone)."""

    class Tier(models.TextChoices):
        BRONZE = "BRONZE", "브론즈"
        SILVER = "SILVER", "실버"
        GOLD = "GOLD", "골드"

    # 누적 결제액 기반 등급 임계값 (원)
    TIER_THRESHOLDS = (
        (200_000, Tier.GOLD),
        (50_000, Tier.SILVER),
        (0, Tier.BRONZE),
    )

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
    marketing_opt_in = models.BooleanField("마케팅 수신 동의", default=False)
    joined_at = models.DateTimeField("가입 시각", auto_now_add=True)

    class Meta:
        verbose_name = "회원"
        verbose_name_plural = "회원"
        ordering = ["-joined_at"]

    def __str__(self) -> str:
        return f"{self.name}({self.phone})"

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
    category = models.CharField(
        "카테고리", max_length=20, choices=Category.choices, default=Category.COFFEE
    )
    # 온도 선택: 없음(디저트) / 아이스만 / 핫·아이스
    temp_option = models.CharField(
        "온도 옵션", max_length=10, choices=Temp.choices, default=Temp.HOTICE
    )
    # 디카페인 변경 가능(커피류) · 오트밀크 변경 가능(라떼류) — 각 +옵션추가금
    decaf_available = models.BooleanField("디카페인 선택", default=False)
    oatmilk_available = models.BooleanField("오트밀크 선택", default=False)
    emoji = models.CharField("이모지", max_length=8, blank=True, default="")
    is_available = models.BooleanField("판매중", default=True)
    sort_order = models.IntegerField("정렬", default=0)

    class Meta:
        verbose_name = "메뉴"
        verbose_name_plural = "메뉴"
        ordering = ["category", "sort_order", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.price:,}원)"


class Transaction(models.Model):
    """거래(결제) 1건."""

    class Method(models.TextChoices):
        TOSS_CARD = "TOSS_CARD", "토스-카드"
        TOSS_EASY = "TOSS_EASY", "토스-간편결제"
        CASH = "CASH", "현금"

    class Status(models.TextChoices):
        PENDING = "pending", "승인 전"
        PAID = "paid", "결제완료"
        CANCELED = "canceled", "취소"

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
    discount = models.IntegerField("세트 할인", default=0)
    points_used = models.IntegerField("사용 포인트", default=0)
    net_amount = models.IntegerField("실결제액")
    points_earned = models.IntegerField("적립 포인트", default=0)
    payment_method = models.CharField(
        "결제수단", max_length=20, choices=Method.choices
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
    quantity = models.PositiveIntegerField("수량", default=1)
    # 옵션 스냅샷
    temperature = models.CharField("온도", max_length=4, blank=True, default="")  # "", "ice", "hot"
    decaf = models.BooleanField("디카페인", default=False)
    oatmilk = models.BooleanField("오트밀크", default=False)

    class Meta:
        verbose_name = "주문 항목"
        verbose_name_plural = "주문 항목"

    @property
    def line_total(self) -> int:
        return self.unit_price * self.quantity

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
        """회원의 현재 조건 진행값."""
        if self.condition_type == self.Condition.VISIT_COUNT:
            return member.visit_count
        if self.condition_type == self.Condition.TOTAL_SPENT:
            return member.total_spent
        return 0


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
