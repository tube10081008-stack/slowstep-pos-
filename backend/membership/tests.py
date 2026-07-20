"""
결제 파이프라인의 저장성·정합성 테스트.

이번 업그레이드로 추가된 보호 장치를 검증한다:
- 멱등 재생: 같은 order_id 재전송 시 중복 결제가 생기지 않고 기존 거래 반환
- DB 유니크 제약: PAID + 동일 order_id 2건은 DB 레벨에서 거부
- 재고: 조건부 차감으로 초과판매 방지, 취소 시 원복
- 이중 취소 방지
- /api/v1/health 저장 모드 보고
"""
from django.db import IntegrityError, transaction as db_transaction
from django.test import TestCase
from django.utils import timezone

from .models import Member, MenuItem, Store, Transaction
from .services import CheckoutError, cancel_transaction, checkout


def make_store(**kw):
    return Store.objects.create(name="슬로우스텝", **kw)


class CheckoutIdempotencyTests(TestCase):
    def setUp(self):
        self.store = make_store()
        self.member = Member.objects.create(
            store=self.store, phone="01011112222", name="조구미", points=1000
        )
        self.menu = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000, stock=10
        )

    def _order(self, order_id, qty=1):
        return checkout(
            member=self.member,
            gross_amount=0,
            points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": qty}],
            toss_order_id=order_id,
        )

    def test_same_order_id_replays_instead_of_duplicating(self):
        first = self._order("order-abc")
        self.assertFalse(first.idempotent_replay)

        replay = self._order("order-abc")
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.transaction.pk, first.transaction.pk)
        # 거래는 1건만 존재
        self.assertEqual(
            Transaction.objects.filter(toss_order_id="order-abc").count(), 1
        )
        # 재시도가 재고를 추가로 깎지 않음
        self.menu.refresh_from_db()
        self.assertEqual(self.menu.stock, 9)

    def test_db_constraint_blocks_duplicate_paid_order_id(self):
        self._order("order-dup")
        with self.assertRaises(IntegrityError):
            with db_transaction.atomic():
                Transaction.objects.create(
                    store=self.store,
                    gross_amount=1000,
                    net_amount=1000,
                    payment_method=Transaction.Method.CARD,
                    toss_order_id="order-dup",
                    status=Transaction.Status.PAID,
                    paid_at=timezone.now(),
                )

    def test_empty_order_id_not_constrained(self):
        # order_id 없는 거래(레거시)는 여러 건 허용
        for _ in range(2):
            Transaction.objects.create(
                store=self.store,
                gross_amount=1000,
                net_amount=1000,
                payment_method=Transaction.Method.CASH,
                status=Transaction.Status.PAID,
                paid_at=timezone.now(),
            )
        self.assertEqual(
            Transaction.objects.filter(toss_order_id="").count(), 2
        )


class StockIntegrityTests(TestCase):
    def setUp(self):
        self.store = make_store()
        self.menu = MenuItem.objects.create(
            store=self.store, name="치즈케이크", price=6000,
            category=MenuItem.Category.DESSERT, stock=2,
        )

    def _order(self, qty, order_id):
        return checkout(
            member=None,
            gross_amount=0,
            points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": qty}],
            toss_order_id=order_id,
        )

    def test_stock_decrement_and_oversell_rejected(self):
        self._order(2, "s1")
        self.menu.refresh_from_db()
        self.assertEqual(self.menu.stock, 0)

        with self.assertRaises(CheckoutError):
            self._order(1, "s2")
        # 실패한 주문은 거래도 남지 않음(원자성)
        self.assertFalse(
            Transaction.objects.filter(toss_order_id="s2").exists()
        )

    def test_cancel_restores_stock(self):
        result = self._order(2, "s3")
        cancel_transaction(result.transaction)
        self.menu.refresh_from_db()
        self.assertEqual(self.menu.stock, 2)


class CancelTests(TestCase):
    def setUp(self):
        self.store = make_store()
        self.member = Member.objects.create(
            store=self.store, phone="01033334444", name="회원", points=0
        )

    def test_double_cancel_rejected(self):
        result = checkout(
            member=self.member,
            gross_amount=10000,
            points_to_use=0,
            payment_method=Transaction.Method.CARD,
            toss_order_id="c1",
        )
        cancel_transaction(result.transaction)
        with self.assertRaises(CheckoutError):
            cancel_transaction(result.transaction)
        # 취소 원복이 한 번만 적용됨
        self.member.refresh_from_db()
        self.assertEqual(self.member.total_spent, 0)
        self.assertEqual(self.member.visit_count, 0)


class MemberImportTests(TestCase):
    """CSV 일괄 등록(payhere 이관) — 검증·원장·중복·dry_run."""

    URL = "/api/v1/members/import"

    def setUp(self):
        self.store = make_store()

    def _post_csv(self, csv_text, dry_run=False):
        return self.client.post(
            self.URL,
            data={"csv": csv_text, "dry_run": dry_run},
            content_type="application/json",
        )

    def test_import_creates_member_with_ledger_and_tier(self):
        csv_text = (
            "이름,연락처,포인트,누적결제액,방문횟수,스탬프,마케팅동의,가입일\n"
            "김이관,010-5555-6666,\"3,200\",250000,42,4,Y,2024-03-15\n"
        )
        res = self._post_csv(csv_text)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["errors"], 0)

        m = Member.objects.get(phone="01055556666")
        self.assertEqual(m.name, "김이관")
        self.assertEqual(m.points, 3200)
        self.assertEqual(m.total_spent, 250000)
        self.assertEqual(m.visit_count, 42)
        self.assertEqual(m.stamps, 4)
        self.assertTrue(m.marketing_opt_in)
        # 등급은 누적액으로 재계산(20만 이상 → GOLD)
        self.assertEqual(m.tier, Member.Tier.GOLD)
        # 원래 가입일 보존(auto_now_add 우회)
        self.assertEqual(timezone.localtime(m.joined_at).date().isoformat(), "2024-03-15")
        # 초기 포인트는 원장(adjust)에 기록 — 잔액의 진실 원천 유지
        entry = m.point_entries.get()
        self.assertEqual(entry.delta, 3200)
        self.assertEqual(entry.reason, "adjust")
        self.assertEqual(entry.balance_after, 3200)

    def test_duplicates_and_invalid_phone(self):
        Member.objects.create(store=self.store, phone="01011112222", name="기존")
        csv_text = (
            "이름,전화번호\n"
            "기존회원,01011112222\n"      # DB에 이미 있음 → skipped
            "새회원,010-7777-8888\n"      # 등록
            "중복행,01077778888\n"        # 파일 내 중복 → skipped
            "이상한번호,02-123-4567\n"    # 유효하지 않음 → error
        )
        res = self._post_csv(csv_text)
        body = res.json()
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["skipped"], 2)
        self.assertEqual(body["errors"], 1)
        # 기존 회원은 덮어쓰지 않음
        self.assertEqual(Member.objects.get(phone="01011112222").name, "기존")
        self.assertTrue(Member.objects.filter(phone="01077778888").exists())

    def test_dry_run_writes_nothing(self):
        res = self._post_csv("이름,연락처\n미리보기,01099998888\n", dry_run=True)
        body = res.json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["created"], 1)
        self.assertFalse(Member.objects.filter(phone="01099998888").exists())

    def test_cp949_file_upload(self):
        # 한국 엑셀 저장 파일(CP949)도 자동 판별
        from io import BytesIO
        data = "이름,연락처,포인트\n박엑셀,01033335555,500\n".encode("cp949")
        f = BytesIO(data)
        f.name = "members.csv"
        res = self.client.post(self.URL, data={"file": f, "dry_run": "false"})
        self.assertEqual(res.status_code, 200)
        m = Member.objects.get(phone="01033335555")
        self.assertEqual(m.name, "박엑셀")
        self.assertEqual(m.points, 500)

    def test_missing_required_header_rejected(self):
        res = self._post_csv("포인트,누적결제액\n100,2000\n")
        self.assertEqual(res.status_code, 400)
        self.assertIn("이름·연락처", res.json()["detail"])


class HealthEndpointTests(TestCase):
    def test_health_reports_persistent_storage(self):
        res = self.client.get("/api/v1/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["db"]["ok"])
        # 테스트는 로컬 SQLite(비서버리스) → 영구 저장으로 보고
        self.assertTrue(body["db"]["persistent"])
        self.assertNotIn("warning", body)
