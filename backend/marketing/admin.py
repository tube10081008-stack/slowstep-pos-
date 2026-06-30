"""마케팅 관리자."""
from django.contrib import admin

from .models import Campaign, MessageLog, Segment


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    list_display = ["name", "tier", "min_visits", "min_spent", "inactive_days", "require_opt_in"]


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ["name", "segment", "status", "recipient_count", "sent_count", "failed_count", "skipped_count", "sent_at"]
    list_filter = ["status", "channel", "is_ad"]


@admin.register(MessageLog)
class MessageLogAdmin(admin.ModelAdmin):
    list_display = ["campaign", "phone", "member", "status", "reason", "created_at"]
    list_filter = ["status"]
    search_fields = ["phone", "member__name"]
