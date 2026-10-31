"""
이미 등록된 회원의 '지난 기록'을 보상 없이 달성 처리한다.

CSV 이관은 등록 시점에 자동으로 처리하지만(imports.apply_baseline), 그 전에
들어온 회원이나 관리자 화면에서 직접 만든 회원은 그대로 남아 있다. 오픈 전에
한 번 돌려 두면 첫 결제에서 등급 쿠폰·미션 보상이 소급 지급되는 일이 없다.

    python manage.py apply_baseline            # 실제 반영
    python manage.py apply_baseline --dry-run  # 무엇이 바뀌는지만 확인
"""
from django.core.management.base import BaseCommand

from membership.imports import apply_baseline
from membership.models import Member, MemberMission, Mission


class Command(BaseCommand):
    help = "기존 회원의 이미 충족한 등급·미션을 보상 없이 달성 처리"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="반영하지 않고 대상만 출력")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        changed = 0
        for m in Member.objects.select_related("store"):
            missions = Mission.objects.filter(store_id=m.store_id, is_active=True)
            pending = [
                mi for mi in missions
                if mi.member_value(m) >= mi.target_value
                and not MemberMission.objects.filter(
                    member=m, mission=mi, is_completed=True
                ).exists()
            ]
            tier_gap = m.tier_rewarded != m.tier
            if not pending and not tier_gap:
                continue
            changed += 1
            note = []
            if tier_gap:
                note.append(f"등급 {m.tier_rewarded}→{m.tier}")
            if pending:
                note.append(f"미션 {len(pending)}건")
            self.stdout.write(f"  {m.name}({m.phone}) — {' · '.join(note)}")
            if not dry:
                apply_baseline(m)

        verb = "대상" if dry else "정리"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {changed}명 / 전체 {Member.objects.count()}명"
        ))
        if dry and changed:
            self.stdout.write("--dry-run 이라 아무것도 바꾸지 않았습니다.")
