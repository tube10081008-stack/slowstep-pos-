"""관리자 등록 (점주용 회원/거래 조회)."""
from django.contrib import admin

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


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "price", "temp_option", "decaf_available", "oatmilk_available", "is_available", "sort_order"]
    list_filter = ["category", "temp_option", "is_available"]
    list_editable = ["price", "is_available", "sort_order"]
    search_fields = ["name"]


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ["name", "point_earn_rate", "stamp_goal", "stamp_reward_points"]


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "tier", "points", "total_spent", "visit_count", "stamps", "marketing_opt_in"]
    list_filter = ["tier", "marketing_opt_in"]
    search_fields = ["name", "phone"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["id", "member", "gross_amount", "points_used", "net_amount", "points_earned", "payment_method", "status", "created_at"]
    list_filter = ["status", "payment_method"]
    search_fields = ["member__name", "member__phone", "toss_order_id"]
    inlines = [OrderItemInline]


@admin.register(PointEntry)
class PointEntryAdmin(admin.ModelAdmin):
    list_display = ["member", "delta", "reason", "balance_after", "created_at"]
    list_filter = ["reason"]
    search_fields = ["member__name", "member__phone"]


@admin.register(Mission)
class MissionAdmin(admin.ModelAdmin):
    list_display = ["title", "condition_type", "target_value", "reward_points", "is_active"]
    list_filter = ["condition_type", "is_active"]


@admin.register(MemberMission)
class MemberMissionAdmin(admin.ModelAdmin):
    list_display = ["member", "mission", "progress", "is_completed", "completed_at"]
    list_filter = ["is_completed"]
