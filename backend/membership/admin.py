"""관리자 등록 (점주용 회원/거래 조회)."""
from django import forms
from django.contrib import admin, messages
from django.db import transaction as db_transaction

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
    list_display = ["name", "category", "price", "cost", "temp_option", "decaf_available", "oatmilk_available", "shot_available", "stock", "is_available", "has_recipe", "sort_order"]
    list_filter = ["category", "temp_option", "is_available"]
    search_fields = ["name", "recipe"]
    fieldsets = (
        (None, {"fields": ("store", "name", "category", "price", "cost", "sort_order",
                           "is_available", "stock", "emoji")}),
        ("옵션", {"fields": ("temp_option", "decaf_available", "oatmilk_available",
                            "shot_available")}),
        ("레시피", {
            "fields": ("recipe", "recipe_hot", "topping", "recipe_note"),
            "description": "POS 결제 완료 화면과 메뉴 길게 누르기에서 보입니다. "
                           "HOT 배합이 다를 때만 '레시피(HOT)'를 채우세요.",
        }),
    )

    @admin.display(boolean=True, description="레시피")
    def has_recipe(self, obj):
        return obj.has_recipe
    list_editable = ["price", "cost", "stock", "is_available", "sort_order"]
    search_fields = ["name"]


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ["name", "point_earn_rate", "stamp_goal", "option_price", "option_cost", "vat_rate", "happy_start", "happy_end", "happy_multiplier"]


class PointAdjustForm(forms.Form):
    """포인트 수기 조정 — 원장에 남기고 잔액을 함께 옮긴다."""

    delta = forms.IntegerField(
        label="증감 포인트", help_text="지급은 양수(1000), 회수는 음수(-500)."
    )


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "tier", "points", "total_spent", "visit_count", "stamps", "marketing_opt_in", "referral_code"]
    list_filter = ["tier", "marketing_opt_in"]
    search_fields = ["name", "phone"]
    # **원장·거래에서 파생되는 값은 손으로 고치지 못하게 막는다.**
    # 여기서 points 를 직접 바꾸면 원장(PointEntry) 합계와 어긋나고,
    # 그 순간부터 손님 화면의 '적립 내역'과 잔액이 서로 다른 말을 한다.
    # 조정이 필요하면 아래 '포인트 조정' 작업을 쓴다(원장에 남는다).
    readonly_fields = [
        "points", "total_spent", "visit_count", "stamps", "tier",
        "tier_rewarded", "spins", "baseline_visit_count", "baseline_total_spent",
        "joined_at",
    ]
    actions = ["adjust_points"]

    @admin.action(description="포인트 조정(원장에 기록)")
    def adjust_points(self, request, queryset):
        """
        선택한 회원의 포인트를 더하거나 뺀다. 잔액만 만지지 않고
        원장에 함께 남겨, 잔액과 내역이 갈라지지 않게 한다.
        """
        form = PointAdjustForm(request.POST if "apply" in request.POST else None)
        if "apply" in request.POST and form.is_valid():
            delta = form.cleaned_data["delta"]
            done, skipped = 0, 0
            for member in queryset:
                with db_transaction.atomic():
                    m = Member.objects.select_for_update().get(pk=member.pk)
                    if m.points + delta < 0:
                        skipped += 1
                        continue
                    m.points += delta
                    m.save(update_fields=["points"])
                    PointEntry.objects.create(
                        member=m, delta=delta,
                        reason=PointEntry.Reason.ADJUST, balance_after=m.points,
                    )
                    done += 1
            self.message_user(request, f"{done}명 조정 완료 ({delta:+}P)", messages.SUCCESS)
            if skipped:
                self.message_user(
                    request, f"{skipped}명은 잔액이 음수가 되어 건너뛰었습니다.",
                    messages.WARNING,
                )
            return None

        from django.template.response import TemplateResponse

        return TemplateResponse(request, "admin/point_adjust.html", {
            "title": "포인트 조정",
            "members": queryset,
            "form": form or PointAdjustForm(),
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        })


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
