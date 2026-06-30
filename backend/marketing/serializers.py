"""마케팅 시리얼라이저."""
from rest_framework import serializers

from .models import Campaign, MessageLog, Segment


class SegmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Segment
        fields = [
            "id", "name", "description", "tier", "min_visits", "min_spent",
            "inactive_days", "require_opt_in", "created_at",
        ]


class SegmentPreviewSerializer(serializers.Serializer):
    """저장 없이 필터로 대상 미리보기."""
    tier = serializers.ChoiceField(
        choices=["", "BRONZE", "SILVER", "GOLD"], required=False, default=""
    )
    min_visits = serializers.IntegerField(min_value=0, default=0)
    min_spent = serializers.IntegerField(min_value=0, default=0)
    inactive_days = serializers.IntegerField(min_value=0, default=0)
    require_opt_in = serializers.BooleanField(default=True)


class CampaignSerializer(serializers.ModelSerializer):
    segment_name = serializers.CharField(source="segment.name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Campaign
        fields = [
            "id", "name", "segment", "segment_name", "channel", "message_template",
            "is_ad", "status", "status_display", "recipient_count", "sent_count",
            "failed_count", "skipped_count", "created_at", "sent_at",
        ]
        read_only_fields = [
            "status", "recipient_count", "sent_count", "failed_count",
            "skipped_count", "sent_at",
        ]


class MessageLogSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source="member.name", read_only=True, default=None)

    class Meta:
        model = MessageLog
        fields = [
            "id", "member_name", "phone", "rendered_message",
            "status", "reason", "created_at",
        ]
