"""DRF 시리얼라이저."""
from django.db import transaction as db_transaction
from rest_framework import serializers

from .models import (
    Member,
    MemberMission,
    MenuItem,
    Mission,
    OrderItem,
    PointEntry,
    Store,
    Transaction,
)


class MenuItemSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )

    sold_out = serializers.BooleanField(read_only=True)

    class Meta:
        model = MenuItem
        fields = [
            "id", "name", "price", "category", "category_display", "emoji",
            "temp_option", "decaf_available", "oatmilk_available", "shot_available",
            "size_up_price", "cost", "stock", "sold_out", "is_available", "sort_order",
            # 레시피 — POS가 제조 화면에서 쓴다(손님 화면에는 내려가지 않는다)
            "recipe", "recipe_hot", "topping", "recipe_note",
        ]


class OrderItemSerializer(serializers.ModelSerializer):
    line_total = serializers.IntegerField(read_only=True)
    option_label = serializers.CharField(read_only=True)
    # POS가 결제 완료 화면에서 이 id로 레시피를 찾는다(메뉴가 삭제됐으면 null)
    menu_item_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = OrderItem
        fields = [
            "name", "menu_item_id", "unit_price", "quantity", "line_total",
            "temperature", "decaf", "oatmilk", "shot", "size_up", "option_label",
        ]


class StoreSerializer(serializers.ModelSerializer):
    discount_rate_list = serializers.ListField(read_only=True)

    class Meta:
        model = Store
        fields = [
            "id", "name", "point_earn_rate", "stamp_goal", "stamp_reward_points",
            "set_discount_amount", "option_price", "is_open", "opened_at",
            "happy_start", "happy_end", "happy_multiplier", "prep_notes",
            "discount_rates", "discount_rate_list", "signup_bonus_points",
        ]


class MemberSerializer(serializers.ModelSerializer):
    stamp_goal = serializers.IntegerField(source="store.stamp_goal", read_only=True)
    tier_display = serializers.CharField(source="get_tier_display", read_only=True)

    class Meta:
        model = Member
        fields = [
            "id", "phone", "name", "points", "tier", "tier_display",
            "total_spent", "visit_count", "stamps", "stamp_goal",
            "marketing_opt_in", "joined_at",
        ]
        read_only_fields = [
            "points", "tier", "total_spent", "visit_count", "stamps", "joined_at",
        ]


class MemberCreateSerializer(serializers.ModelSerializer):
    # 이름은 받지 않아도 된다 — 비우면 '행동 + 동물' 닉네임을 자동 부여한다.
    # (연락처만으로 식별되므로 실명은 수집하지 않는 것이 기본)
    name = serializers.CharField(required=False, allow_blank=True, max_length=50)

    class Meta:
        model = Member
        fields = ["phone", "name", "marketing_opt_in"]

    @db_transaction.atomic
    def create(self, validated_data):
        store = Store.objects.first()
        if store is None:
            raise serializers.ValidationError("매장 설정이 없습니다.")
        if not (validated_data.get("name") or "").strip():
            from .nickname import generate_nickname

            validated_data["name"] = generate_nickname()

        # 가입 축하 포인트. 잔액만 올리지 않고 원장에도 남긴다 —
        # 원장이 진실의 원천이라, 여기서 빠뜨리면 잔액과 내역이 어긋난다.
        bonus = max(0, store.signup_bonus_points)
        member = Member.objects.create(store=store, points=bonus, **validated_data)
        if bonus:
            PointEntry.objects.create(
                member=member,
                delta=bonus,
                reason=PointEntry.Reason.SIGNUP,
                balance_after=bonus,
            )
        return member


class PointEntrySerializer(serializers.ModelSerializer):
    reason_display = serializers.CharField(source="get_reason_display", read_only=True)

    class Meta:
        model = PointEntry
        fields = ["id", "delta", "reason", "reason_display", "balance_after", "created_at"]


class MissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mission
        fields = [
            "id", "title", "description", "condition_type",
            "target_value", "reward_points", "is_active",
        ]


class MemberMissionSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source="mission.title", read_only=True)
    description = serializers.CharField(source="mission.description", read_only=True)
    target = serializers.IntegerField(source="mission.target_value", read_only=True)
    reward_points = serializers.IntegerField(source="mission.reward_points", read_only=True)

    class Meta:
        model = MemberMission
        fields = ["title", "description", "progress", "target", "reward_points", "is_completed"]


class TransactionSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    member_name = serializers.CharField(source="member.name", read_only=True, default=None)
    method_display = serializers.CharField(source="get_payment_method_display", read_only=True)

    class Meta:
        model = Transaction
        fields = [
            "id", "member_name", "gross_amount", "discount", "manual_discount_pct",
            "points_used", "net_amount",
            "points_earned", "payment_method", "method_display", "approval_no", "status",
            "toss_order_id", "created_at", "paid_at", "items",
        ]


class QuoteRequestSerializer(serializers.Serializer):
    member_id = serializers.IntegerField(required=False, allow_null=True)
    gross_amount = serializers.IntegerField(min_value=1)
    points_to_use = serializers.IntegerField(min_value=0, default=0)


class OrderLineSerializer(serializers.Serializer):
    menu_item_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    temperature = serializers.CharField(required=False, allow_blank=True, default="")
    decaf = serializers.BooleanField(required=False, default=False)
    oatmilk = serializers.BooleanField(required=False, default=False)
    shot = serializers.BooleanField(required=False, default=False)
    size_up = serializers.BooleanField(required=False, default=False)


class CheckoutRequestSerializer(serializers.Serializer):
    member_id = serializers.IntegerField(required=False, allow_null=True)
    # 메뉴 항목이 오면 총액은 서버가 계산. 없으면 gross_amount 필수.
    items = OrderLineSerializer(many=True, required=False)
    gross_amount = serializers.IntegerField(min_value=1, required=False)
    points_to_use = serializers.IntegerField(min_value=0, default=0)
    payment_method = serializers.ChoiceField(choices=Transaction.Method.choices)
    approval_no = serializers.CharField(required=False, allow_blank=True, default="")
    toss_payment_key = serializers.CharField(required=False, allow_blank=True, default="")
    toss_order_id = serializers.CharField(required=False, allow_blank=True, default="")
    coupon_id = serializers.IntegerField(required=False, allow_null=True)
    discount_pct = serializers.IntegerField(required=False, min_value=0, max_value=100, default=0)
    # 세트 할인은 직원이 눌렀을 때만 붙는다(자동 적용 아님).
    set_discount = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if not attrs.get("items") and not attrs.get("gross_amount"):
            raise serializers.ValidationError("items 또는 gross_amount가 필요합니다.")
        return attrs


class MenuItemWriteSerializer(serializers.ModelSerializer):
    """
    POS에서 메뉴를 바로 추가·수정할 때 쓴다.

    디저트가 매일 바뀌는데 그때마다 관리자 화면에 들어가는 건 현실적이지 않다.
    store 는 서버가 붙인다(클라이언트가 다른 매장을 지정하지 못하게).
    """

    class Meta:
        model = MenuItem
        fields = [
            "name", "price", "cost", "category", "temp_option",
            "decaf_available", "oatmilk_available", "shot_available",
            "size_up_price", "stock", "is_available", "sort_order",
            "recipe", "recipe_hot", "topping", "recipe_note",
        ]
        extra_kwargs = {f: {"required": False} for f in fields if f not in ("name", "price")}

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("메뉴 이름을 입력하세요.")
        qs = MenuItem.objects.filter(name=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("같은 이름의 메뉴가 이미 있습니다.")
        return value

    def validate_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("가격은 0보다 커야 합니다.")
        return value
