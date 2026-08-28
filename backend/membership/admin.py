"""관리자 등록 (점주용 회원/거래 조회)."""
from django.contrib import admin

from django.utils import timezone

from .models import (
    Coupon,
    Member,
    MemberMission,
    MemberQuest,
    MenuItem,
    Mission,
    OrderItem,
    PointEntry,
    Store,
    Transaction,
)


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "price", "cost", "temp_option", "decaf_available", "oatmilk_available", "shot_available", "stock", "is_available", "sort_order"]
    list_filter = ["category", "temp_option", "is_available"]
    list_editable = ["price", "cost", "stock", "is_available", "sort_order"]
    search_fields = ["name"]


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ["name", "point_earn_rate", "stamp_goal", "option_price", "option_cost", "vat_rate", "happy_start", "happy_end", "happy_multiplier"]


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "tier", "points", "total_spent", "visit_count", "stamps", "marketing_opt_in", "referral_code"]
    list_filter = ["tier", "marketing_opt_in"]
    search_fields = ["name", "phone"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["id", "member", "gross_amount", "points_used", "net_amount", "points_earned", "payment_method", "approval_no", "status", "created_at"]
    list_filter = ["status", "payment_method"]
    search_fields = ["member__name", "member__phone", "toss_order_id", "approval_no"]
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


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    """
    쿠폰 조회·사용 처리.

    손님이 폰으로 쿠폰을 보여주면 직원이 여기서 '사용 처리'해야 한다.
    안 그러면 같은 쿠폰을 몇 번이든 다시 쓸 수 있다.
    """

    list_display = ["member", "kind", "source", "note", "state", "issued_at", "expires_at"]
    list_filter = ["kind", "source", "used_at"]
    search_fields = ["member__name", "member__phone"]
    date_hierarchy = "issued_at"
    actions = ["mark_used"]

    @admin.display(description="상태")
    def state(self, obj):
        if obj.used_at:
            return "사용함"
        return "만료" if obj.is_expired else "사용 가능"

    @admin.action(description="선택한 쿠폰을 사용 처리")
    def mark_used(self, request, queryset):
        n = queryset.filter(used_at__isnull=True).update(used_at=timezone.now())
        self.message_user(request, f"{n}장을 사용 처리했습니다.")


@admin.register(MemberQuest)
class MemberQuestAdmin(admin.ModelAdmin):
    list_display = ["member", "title", "kind", "reward_points", "completed_at"]
    list_filter = ["kind"]
    search_fields = ["member__name", "member__phone", "key"]
