"""DRF 시리얼라이저."""
from rest_framework import serializers

from .models import (
    Member,
    MemberMission,
    Mission,
    PointEntry,
    Store,
    Transaction,
)


class StoreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Store
        fields = ["id", "name", "point_earn_rate", "stamp_goal", "stamp_reward_points"]


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
    class Meta:
        model = Member
        fields = ["phone", "name", "marketing_opt_in"]

    def create(self, validated_data):
        store = Store.objects.first()
        if store is None:
            raise serializers.ValidationError("매장 설정이 없습니다.")
        return Member.objects.create(store=store, **validated_data)


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
    class Meta:
        model = Transaction
        fields = [
            "id", "gross_amount", "points_used", "net_amount", "points_earned",
            "payment_method", "status", "toss_order_id", "created_at", "paid_at",
        ]


class QuoteRequestSerializer(serializers.Serializer):
    member_id = serializers.IntegerField(required=False, allow_null=True)
    gross_amount = serializers.IntegerField(min_value=1)
    points_to_use = serializers.IntegerField(min_value=0, default=0)


class CheckoutRequestSerializer(serializers.Serializer):
    member_id = serializers.IntegerField(required=False, allow_null=True)
    gross_amount = serializers.IntegerField(min_value=1)
    points_to_use = serializers.IntegerField(min_value=0, default=0)
    payment_method = serializers.ChoiceField(choices=Transaction.Method.choices)
    toss_payment_key = serializers.CharField(required=False, allow_blank=True, default="")
    toss_order_id = serializers.CharField(required=False, allow_blank=True, default="")
