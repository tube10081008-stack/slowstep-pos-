"""
마케팅 세그먼트·캠페인 모델.

핵심 가치: 우리가 소유한 회원 데이터로 결제사 동의 없이 직접 마케팅한다.
- Segment: 발송 대상 필터(등급/방문/누적/휴면) 정의
- Campaign: 메시지 + 대상 + 발송 결과
- MessageLog: 회원별 발송 기록(감사·수신거부 추적)
"""
from django.db import models

from membership.models import Member


class Segment(models.Model):
    """발송 대상 세그먼트(필터 정의)."""

    name = models.CharField("이름", max_length=100)
    description = models.CharField("설명", max_length=300, blank=True, default="")

    # ── 필터 (빈/0 = 미적용) ──
    tier = models.CharField(
        "등급", max_length=10, blank=True, default="",
        choices=Member.Tier.choices, help_text="비우면 전체 등급",
    )
    min_visits = models.IntegerField("최소 방문 횟수", default=0)
    min_spent = models.IntegerField("최소 누적 결제액", default=0)
    # N일 이상 미방문(휴면) 회원 대상. 0이면 미적용.
    inactive_days = models.IntegerField("휴면 기준(일)", default=0)
    # 광고성 발송은 수신 동의 필수(법적 요구). 정보성은 해제 가능.
    require_opt_in = models.BooleanField("마케팅 수신 동의자만", default=True)

    created_at = models.DateTimeField("생성 시각", auto_now_add=True)

    class Meta:
        verbose_name = "세그먼트"
        verbose_name_plural = "세그먼트"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class Campaign(models.Model):
    """마케팅 캠페인(알림톡 발송 단위)."""

    class Channel(models.TextChoices):
        ALIMTALK = "ALIMTALK", "알림톡"

    class Status(models.TextChoices):
        DRAFT = "draft", "작성중"
        SENT = "sent", "발송완료"

    name = models.CharField("캠페인명", max_length=100)
    segment = models.ForeignKey(
        Segment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="campaigns", verbose_name="세그먼트",
    )
    channel = models.CharField(
        "채널", max_length=20, choices=Channel.choices, default=Channel.ALIMTALK
    )
    # 메시지 본문. {이름}{포인트}{등급}{스탬프} 치환 변수 지원.
    message_template = models.TextField("메시지")
    is_ad = models.BooleanField("광고성", default=True)

    status = models.CharField(
        "상태", max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    recipient_count = models.IntegerField("대상 수", default=0)
    sent_count = models.IntegerField("발송 성공", default=0)
    failed_count = models.IntegerField("발송 실패", default=0)
    skipped_count = models.IntegerField("제외(미동의 등)", default=0)

    created_at = models.DateTimeField("생성 시각", auto_now_add=True)
    sent_at = models.DateTimeField("발송 시각", null=True, blank=True)

    class Meta:
        verbose_name = "캠페인"
        verbose_name_plural = "캠페인"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} [{self.get_status_display()}]"


class MessageLog(models.Model):
    """회원별 발송 기록."""

    class Status(models.TextChoices):
        SENT = "sent", "성공"
        FAILED = "failed", "실패"
        SKIPPED = "skipped", "제외"

    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="logs"
    )
    member = models.ForeignKey(
        Member, on_delete=models.SET_NULL, null=True, related_name="message_logs"
    )
    phone = models.CharField("수신 번호", max_length=20)
    rendered_message = models.TextField("발송 본문")
    status = models.CharField("상태", max_length=10, choices=Status.choices)
    reason = models.CharField("사유", max_length=200, blank=True, default="")
    created_at = models.DateTimeField("시각", auto_now_add=True)

    class Meta:
        verbose_name = "발송 로그"
        verbose_name_plural = "발송 로그"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.phone} [{self.status}]"
