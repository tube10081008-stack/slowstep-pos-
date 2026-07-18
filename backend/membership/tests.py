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
