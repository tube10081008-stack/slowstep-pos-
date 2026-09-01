"""
결제 파이프라인의 저장성·정합성 테스트.

이번 업그레이드로 추가된 보호 장치를 검증한다:
- 멱등 재생: 같은 order_id 재전송 시 중복 결제가 생기지 않고 기존 거래 반환
- DB 유니크 제약: PAID + 동일 order_id 2건은 DB 레벨에서 거부
- 재고: 조건부 차감으로 초과판매 방지, 취소 시 원복
- 이중 취소 방지
- /api/v1/health 저장 모드 보고
"""
from datetime import timedelta

from django.db import IntegrityError, transaction as db_transaction
from django.test import TestCase
from django.utils import timezone

from .models import Member, MenuItem, Store, Transaction
from .services import CheckoutError, cancel_transaction, checkout


def make_store(**kw):
    return Store.objects.create(name="슬로우스텝", **kw)


def authenticate(client):
    """테스트 클라이언트에 매장 PIN 토큰을 붙인다(점주·직원 API용)."""
    from .auth import issue_token

    client.defaults["HTTP_X_STORE_TOKEN"] = issue_token()
    return client


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
        authenticate(self.client)

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
        # 등급은 누적액으로 재계산(10만 이상 → SILVER, 30만부터 GOLD)
        self.assertEqual(m.tier, Member.Tier.SILVER)
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


class MarginTests(TestCase):
    """원가·마진: 원가 스냅샷, 공급가 기준, 적립비용(적립 시점), 메뉴별 마진."""

    def setUp(self):
        self.store = make_store(point_earn_rate="0.03", option_cost=300, vat_rate="0.10")
        self.member = Member.objects.create(
            store=self.store, phone="01088889999", name="마진", points=0
        )
        self.latte = MenuItem.objects.create(
            store=self.store, name="라떼", price=5000, cost=1200,
            category=MenuItem.Category.COFFEE, oatmilk_available=True, stock=100,
        )
        authenticate(self.client)

    def test_unit_cost_snapshot_includes_option(self):
        checkout(
            member=self.member, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.latte.id, "quantity": 2, "oatmilk": True}],
            toss_order_id="m1",
        )
        from .models import OrderItem
        oi = OrderItem.objects.get(name="라떼")
        # 단가 = 5000 + 옵션가 500, 원가 = 1200 + 옵션원가 300
        self.assertEqual(oi.unit_price, 5500)
        self.assertEqual(oi.unit_cost, 1500)
        # 원가 변경해도 과거 스냅샷 불변
        self.latte.cost = 9999
        self.latte.save(update_fields=["cost"])
        oi.refresh_from_db()
        self.assertEqual(oi.unit_cost, 1500)

    def test_margin_summary_supply_basis_and_reward_cost(self):
        from .margins import margin_summary, to_supply
        # 라떼 1잔(옵션 없음): 정가 5000, net 5000, 적립 3% = 150P
        checkout(
            member=self.member, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.latte.id, "quantity": 1}],
            toss_order_id="m2",
        )
        s = margin_summary(days=30)
        self.assertEqual(s["revenue_incl_vat"], 5000)
        self.assertEqual(s["supply_revenue"], to_supply(5000))  # 5000/1.1 ≈ 4545
        self.assertEqual(s["material_cost"], 1200)
        self.assertEqual(s["reward_cost"], 150)  # 적립 시점 인식
        self.assertEqual(s["contribution"], s["supply_revenue"] - 1200 - 150)

    def test_canceled_excluded_from_margin(self):
        from .margins import margin_summary
        r = checkout(
            member=self.member, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.latte.id, "quantity": 1}],
            toss_order_id="m3",
        )
        cancel_transaction(r.transaction)
        s = margin_summary(days=30)
        self.assertEqual(s["supply_revenue"], 0)
        self.assertEqual(s["material_cost"], 0)
        self.assertEqual(s["reward_cost"], 0)

    def test_menu_item_margins_ranking(self):
        from .margins import menu_item_margins
        checkout(
            member=None, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CASH,
            items=[{"menu_item_id": self.latte.id, "quantity": 3}],
            toss_order_id="m4",
        )
        rows = menu_item_margins(days=30)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "라떼")
        self.assertEqual(row["qty"], 3)
        self.assertEqual(row["material_cost"], 3600)  # 1200 × 3
        self.assertTrue(row["has_cost"])
        self.assertGreater(row["margin_rate"], 0)

    def test_margins_endpoint(self):
        checkout(
            member=None, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CASH,
            items=[{"menu_item_id": self.latte.id, "quantity": 1}],
            toss_order_id="m5",
        )
        res = self.client.get("/api/v1/margins/summary?days=7")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["days"], 7)
        self.assertIn("contribution", body)
        self.assertEqual(len(body["menu"]), 1)


class StorePinAuthTests(TestCase):
    """매장 PIN: 토큰 발급·보호 엔드포인트 차단·공개 경로 유지."""

    def setUp(self):
        self.store = make_store()
        self.member = Member.objects.create(
            store=self.store, phone="01077776666", name="공개", points=100
        )
        from django.core.cache import cache
        cache.clear()  # 시도 카운터 초기화

    def _token(self, pin="0812"):
        res = self.client.post(
            "/api/v1/auth/pin", data={"pin": pin}, content_type="application/json"
        )
        return res

    def test_correct_pin_issues_token(self):
        res = self._token()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["token"])

    def test_wrong_pin_rejected(self):
        res = self._token("9999")
        self.assertEqual(res.status_code, 401)

    def test_protected_endpoints_require_token(self):
        # 원가·매출·고객명단·결제는 토큰 없이 접근 불가
        for url in (
            "/api/v1/margins/summary",
            "/api/v1/sales/summary",
            "/api/v1/dashboard/stats",
            "/api/v1/members",
            "/api/v1/transactions",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_protected_endpoints_pass_with_token(self):
        token = self._token().json()["token"]
        hdr = {"HTTP_X_STORE_TOKEN": token}
        for url in ("/api/v1/margins/summary", "/api/v1/sales/summary", "/api/v1/members"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, **hdr).status_code, 200)

    def test_customer_pages_stay_public(self):
        # 고객 멤버십 조회는 PIN 없이 열려야 한다
        r1 = self.client.get("/api/v1/members/lookup?phone=01077776666")
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get(f"/api/v1/members/{self.member.id}/dashboard")
        self.assertEqual(r2.status_code, 200)
        # 메뉴·매장·헬스도 공개
        for url in ("/api/v1/menu", "/api/v1/store", "/api/v1/health"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_checkout_blocked_without_pin(self):
        # 결제 생성도 매장 전용 — 외부에서 가짜 주문 못 넣음
        res = self.client.post(
            "/api/v1/transactions",
            data={"gross_amount": 1000, "payment_method": "CASH"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 403)

    def test_brute_force_rate_limited(self):
        from .auth import ATTEMPT_LIMIT
        for _ in range(ATTEMPT_LIMIT):
            self._token("0000")
        res = self._token("0000")
        self.assertEqual(res.status_code, 429)
        # 제한 중에는 올바른 PIN도 막힘(창 만료까지)
        self.assertEqual(self._token("0812").status_code, 429)

    def test_tampered_token_rejected(self):
        token = self._token().json()["token"]
        res = self.client.get(
            "/api/v1/margins/summary", HTTP_X_STORE_TOKEN=token + "x"
        )
        self.assertEqual(res.status_code, 403)


class EnsureAdminTests(TestCase):
    """서버리스에서 셸 없이 관리자 계정을 보장하는 명령."""

    def _run(self, **env):
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        out = StringIO()
        with mock.patch.dict("os.environ", env, clear=False):
            call_command("ensure_admin", stdout=out)
        return out.getvalue()

    def test_creates_superuser_from_env(self):
        from django.contrib.auth import get_user_model

        self._run(
            DJANGO_SUPERUSER_USERNAME="owner",
            DJANGO_SUPERUSER_PASSWORD="s1owstep!pw",
            DJANGO_SUPERUSER_EMAIL="owner@example.com",
        )
        u = get_user_model().objects.get(username="owner")
        self.assertTrue(u.is_superuser and u.is_staff)
        self.assertTrue(u.check_password("s1owstep!pw"))

    def test_existing_admin_password_not_overwritten(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        User.objects.create_superuser(username="owner", password="original-pw")
        self._run(
            DJANGO_SUPERUSER_USERNAME="owner",
            DJANGO_SUPERUSER_PASSWORD="different-pw",
        )
        u = User.objects.get(username="owner")
        # 재배포마다 비밀번호가 되돌아가면 안 됨
        self.assertTrue(u.check_password("original-pw"))

    def test_noop_without_env(self):
        from django.contrib.auth import get_user_model

        out = self._run(DJANGO_SUPERUSER_USERNAME="", DJANGO_SUPERUSER_PASSWORD="")
        self.assertIn("건너뜀", out)
        self.assertEqual(get_user_model().objects.count(), 0)


class PurgeDemoTests(TestCase):
    """데모 시드만 정확히 제거하고 실제 고객·메뉴는 보존하는지."""

    def setUp(self):
        self.store = make_store()
        # 데모 회원(시드 패턴) + 실제 고객
        self.demo = Member.objects.create(
            store=self.store, phone="01012345678", name="김슬로우", points=500
        )
        self.real = Member.objects.create(
            store=self.store, phone="01055551234", name="진짜고객", points=1000
        )
        self.menu = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000, cost=600
        )
        for m, oid in ((self.demo, "seed-1-0"), (self.real, "real-1")):
            Transaction.objects.create(
                store=self.store, member=m, gross_amount=5000, net_amount=5000,
                points_earned=150, payment_method=Transaction.Method.CARD,
                status=Transaction.Status.PAID, paid_at=timezone.now(),
                toss_order_id=oid,
            )

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("purge_demo", *args, stdout=out)
        return out.getvalue()

    def test_purges_demo_keeps_real(self):
        self._run()
        self.assertFalse(Member.objects.filter(phone="01012345678").exists())
        self.assertTrue(Member.objects.filter(phone="01055551234").exists())
        # 데모 거래는 삭제, 실제 거래는 보존
        self.assertFalse(Transaction.objects.filter(toss_order_id="seed-1-0").exists())
        self.assertTrue(Transaction.objects.filter(toss_order_id="real-1").exists())
        # 메뉴·매장 설정은 유지(원가 포함)
        self.menu.refresh_from_db()
        self.assertEqual(self.menu.cost, 600)
        self.assertTrue(Store.objects.exists())

    def test_demo_member_transactions_removed_not_orphaned(self):
        # 회원만 지우면 거래가 member=NULL 로 남아 '비회원 매출'로 잡히는 문제 방지
        Transaction.objects.create(
            store=self.store, member=self.demo, gross_amount=9000, net_amount=9000,
            payment_method=Transaction.Method.CASH, status=Transaction.Status.PAID,
            paid_at=timezone.now(), toss_order_id="",
        )
        self._run()
        self.assertEqual(
            Transaction.objects.filter(member__isnull=True).count(), 0
        )
        self.assertEqual(Transaction.objects.count(), 1)  # 실제 거래만 남음

    def test_dry_run_deletes_nothing(self):
        out = self._run("--dry-run")
        self.assertIn("모의 실행", out)
        self.assertTrue(Member.objects.filter(phone="01012345678").exists())

    def test_idempotent_noop(self):
        self._run()
        out = self._run()
        self.assertIn("건너뜀", out)


class AiOrderParseTests(TestCase):
    """자연어 주문 파싱 — 규칙 폴백(키 없이 동작) + 안전 검증."""

    def setUp(self):
        self.store = make_store()
        self.amer = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000,
            category=MenuItem.Category.COFFEE, temp_option=MenuItem.Temp.HOTICE,
            decaf_available=True, shot_available=True,
        )
        self.latte = MenuItem.objects.create(
            store=self.store, name="카페 라떼", price=4500,
            category=MenuItem.Category.COFFEE, temp_option=MenuItem.Temp.HOTICE,
            decaf_available=True, oatmilk_available=True, shot_available=True,
        )
        self.fin = MenuItem.objects.create(
            store=self.store, name="플레인 휘낭시에", price=2500,
            category=MenuItem.Category.DESSERT, temp_option=MenuItem.Temp.NONE,
        )
        authenticate(self.client)

    def _parse(self, text):
        from .ai_order import parse_order

        return parse_order(text)

    def test_abbreviation_and_quantity(self):
        r = self._parse("아아 두 잔")
        self.assertEqual(r["source"], "rule")  # 키 없음 → 폴백
        (it,) = r["items"]
        self.assertEqual(it["menu_item_id"], self.amer.id)
        self.assertEqual(it["quantity"], 2)
        self.assertEqual(it["temperature"], "ice")  # 아아 = 아이스

    def test_hot_keyword_and_multi_item(self):
        r = self._parse("라떼 하나 따뜻하게, 휘낭시에 2개")
        by_id = {i["menu_item_id"]: i for i in r["items"]}
        self.assertEqual(by_id[self.latte.id]["temperature"], "hot")
        self.assertEqual(by_id[self.latte.id]["quantity"], 1)
        self.assertEqual(by_id[self.fin.id]["quantity"], 2)
        # 디저트는 온도 옵션 없음
        self.assertEqual(by_id[self.fin.id]["temperature"], "")

    # ── 빼기(취소) 의도 ──
    def test_remove_intent(self):
        """'아아 하나 빼줘'를 한 잔 추가로 읽던 버그."""
        (it,) = self._parse("아아 하나 빼줘")["items"]
        self.assertEqual(it["action"], "remove")
        self.assertEqual(it["menu_item_id"], self.amer.id)
        self.assertEqual(it["quantity"], 1)

    def test_plain_order_defaults_to_add(self):
        (it,) = self._parse("아아 하나")["items"]
        self.assertEqual(it["action"], "add")

    def test_trailing_remove_covers_earlier_items(self):
        """한국어는 동사가 끝에 온다 — '아아랑 라떼 빼줘'는 둘 다 빼는 것."""
        items = self._parse("아아랑 라떼 빼줘")["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["action"] == "remove" for i in items))

    def test_remove_does_not_leak_forward(self):
        """'아아 빼고 라떼 하나' — 라떼는 담는 것이다."""
        by_id = {i["menu_item_id"]: i for i in self._parse("아아 빼고, 라떼 하나")["items"]}
        self.assertEqual(by_id[self.amer.id]["action"], "remove")
        self.assertEqual(by_id[self.latte.id]["action"], "add")

    def test_malgo_swaps(self):
        """'A 말고 B' — A는 빼고 B는 담는다."""
        by_id = {i["menu_item_id"]: i for i in self._parse("아아 말고 라떼로 주세요")["items"]}
        self.assertEqual(by_id[self.amer.id]["action"], "remove")
        self.assertEqual(by_id[self.latte.id]["action"], "add")

    def test_ingredient_removal_is_not_line_removal(self):
        """'샷 빼고'는 재료 얘기지 메뉴를 빼라는 게 아니다."""
        (it,) = self._parse("아아 얼음 빼고 하나")["items"]
        self.assertEqual(it["action"], "add")

    def test_various_remove_words(self):
        for phrase in ("아아 취소", "아아 하나 지워줘", "아아 삭제", "아아 하나 제외"):
            (it,) = self._parse(phrase)["items"]
            self.assertEqual(it["action"], "remove", phrase)

    def test_options_only_when_allowed(self):
        r = self._parse("아메리카노 디카페인 오트밀크 한 잔")
        (it,) = r["items"]
        self.assertTrue(it["decaf"])
        # 아메리카노엔 오트밀크 옵션이 없으므로 무시돼야 함
        self.assertFalse(it["oatmilk"])

    def test_sold_out_excluded(self):
        self.latte.stock = 0
        self.latte.save(update_fields=["stock"])
        with self.assertRaises(Exception):
            self._parse("라떼 한 잔")  # 품절 → 담을 항목 없음

    def test_unknown_menu_rejected(self):
        from .ai_order import OrderParseError

        with self.assertRaises(OrderParseError):
            self._parse("피자 한 판")

    def test_model_output_is_sanitized(self):
        """모델이 엉뚱한 id·과한 수량·허용 안 된 옵션을 줘도 서버가 걸러낸다."""
        from .ai_order import _sanitize

        raw = [
            {"menu_item_id": 999999, "quantity": 1},               # 없는 메뉴
            {"menu_item_id": self.amer.id, "quantity": 9999,       # 수량 폭주
             "oatmilk": True, "temperature": "미지근"},             # 허용 안 된 옵션·온도
        ]
        clean = _sanitize(raw, [self.amer, self.latte, self.fin])
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["quantity"], 20)      # MAX_QTY로 제한
        self.assertFalse(clean[0]["oatmilk"])           # 아메리카노는 오트 불가
        self.assertEqual(clean[0]["temperature"], "ice")  # 잘못된 값 → 기본 아이스

    def test_endpoint_requires_pin(self):
        anon = self.client_class()
        res = anon.post(
            "/api/v1/orders/parse",
            data={"text": "아아 하나"}, content_type="application/json",
        )
        self.assertEqual(res.status_code, 403)

    def test_endpoint_returns_items(self):
        res = self.client.post(
            "/api/v1/orders/parse",
            data={"text": "아아 세 잔"}, content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["items"][0]["quantity"], 3)
        self.assertEqual(body["items"][0]["name"], "아메리카노")

    def test_empty_input_rejected(self):
        res = self.client.post(
            "/api/v1/orders/parse", data={"text": "  "}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 400)


class NicknameTests(TestCase):
    """실명 대신 '행동 + 동물' 닉네임 자동 부여."""

    def setUp(self):
        self.store = make_store()
        authenticate(self.client)

    def test_generated_format(self):
        from .nickname import ACTIONS, ANIMALS, generate_nickname

        name = generate_nickname(existing=set())
        action, animal = name.split(" ", 1)
        self.assertIn(action, ACTIONS)
        self.assertIn(animal, ANIMALS)

    def test_avoids_existing_names(self):
        from .nickname import ACTIONS, ANIMALS, generate_nickname

        # 한 조합만 남기고 전부 사용 중이면 그 하나가 나와야 한다
        everything = {f"{a} {b}" for a in ACTIONS for b in ANIMALS}
        target = f"{ACTIONS[0]} {ANIMALS[0]}"
        everything.remove(target)
        self.assertEqual(generate_nickname(existing=everything), target)

    def test_exhausted_pool_appends_number(self):
        from .nickname import ACTIONS, ANIMALS, generate_nickname

        everything = {f"{a} {b}" for a in ACTIONS for b in ANIMALS}
        name = generate_nickname(existing=everything)
        self.assertNotIn(name, everything)
        self.assertTrue(name.rstrip("0123456789 ") != name)  # 숫자 접미사

    def test_signup_without_name_gets_nickname(self):
        res = self.client.post(
            "/api/v1/members",
            data={"phone": "01044443333", "marketing_opt_in": True},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        name = res.json()["name"]
        self.assertIn(" ", name)              # "행동 동물"
        self.assertEqual(Member.objects.get(phone="01044443333").name, name)

    def test_explicit_name_still_respected(self):
        # 이관된 기존 고객처럼 이름이 주어지면 그대로 쓴다
        res = self.client.post(
            "/api/v1/members",
            data={"phone": "01044445555", "name": "김단골"},
            content_type="application/json",
        )
        self.assertEqual(res.json()["name"], "김단골")

    def test_signups_do_not_collide(self):
        names = set()
        for i in range(25):
            res = self.client.post(
                "/api/v1/members",
                data={"phone": f"0105555{i:04d}"},
                content_type="application/json",
            )
            names.add(res.json()["name"])
        self.assertEqual(len(names), 25)   # 25명 모두 서로 다른 닉네임

    def test_csv_import_blank_name_gets_nickname(self):
        from .imports import import_members_csv

        r = import_members_csv(csv_text="이름,연락처\n,01066667777\n")
        self.assertEqual(r["created"], 1)
        name = Member.objects.get(phone="01066667777").name
        self.assertIn(" ", name)
        self.assertNotIn("고객", name)


class MemberQrTests(TestCase):
    """멤버십 QR — 개인정보도 토큰도 담지 않는 고정 주소."""

    def setUp(self):
        self.store = make_store()
        self.member = Member.objects.create(
            store=self.store, phone="01012349999", name="느긋한 수달", points=500
        )

    def test_qr_is_public_and_static(self):
        # 손님 폰에서 열리는 화면이므로 PIN 없이 접근 가능해야 한다
        res = self.client.get("/api/v1/member-qr")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("<svg", body["svg"])
        self.assertTrue(body["url"].endswith("/member/"))

    def test_qr_contains_no_personal_data(self):
        body = self.client.get("/api/v1/member-qr").json()
        # 어떤 회원 정보도 담기지 않는다 — 화면을 찍어도 얻을 게 없다
        self.assertNotIn("01012349999", body["url"])
        self.assertNotIn("01012349999", body["svg"])
        self.assertNotIn("t=", body["url"])
        self.assertNotIn("phone", body["url"])

    def test_same_qr_for_everyone(self):
        Member.objects.create(store=self.store, phone="01088887777", name="졸린 판다")
        a = self.client.get("/api/v1/member-qr").json()["url"]
        b = self.client.get("/api/v1/member-qr").json()["url"]
        self.assertEqual(a, b)   # 고정이므로 인쇄해 붙여도 된다

    def test_member_page_lookup_still_public(self):
        # 손님이 자기 번호로 조회하는 경로가 열려 있어야 한다
        res = self.client.get("/api/v1/members/lookup?phone=01012349999")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "느긋한 수달")


class DeploymentDepsTests(TestCase):
    """
    배포 의존성 목록이 어긋나는 사고 방지.

    Vercel은 **저장소 루트의 requirements.txt** 를 설치한다. backend/ 쪽에만
    패키지를 추가하면 배포에서 ImportError가 나고, 그 임포트가 views 체인에
    걸려 있으면 API 전체가 500이 된다(실제로 qrcode 누락으로 발생).
    """

    def _names(self, path):
        import re
        from pathlib import Path

        names = set()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            names.add(re.split(r"[<>=\[]", line, 1)[0].strip().lower())
        return names

    def test_root_requirements_cover_backend(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        backend_deps = self._names(root / "backend" / "requirements.txt")
        root_deps = self._names(root / "requirements.txt")
        missing = backend_deps - root_deps
        self.assertEqual(
            missing,
            set(),
            f"루트 requirements.txt에 빠진 배포 의존성: {sorted(missing)} "
            "— Vercel이 설치하지 못해 배포에서 500이 납니다.",
        )

    def test_qr_endpoint_degrades_without_library(self):
        """QR 라이브러리가 없어도 나머지 API는 살아 있어야 한다."""
        from unittest import mock

        make_store()
        with mock.patch("membership.member_qr.QR_AVAILABLE", False):
            res = self.client.get("/api/v1/member-qr")
            self.assertEqual(res.status_code, 503)
            self.assertIn("/member/", res.json()["url"])   # 주소는 여전히 안내
            # 결제·조회 경로는 정상
            self.assertEqual(self.client.get("/api/v1/menu").status_code, 200)
            self.assertEqual(self.client.get("/api/v1/health").status_code, 200)


class GamificationTests(TestCase):
    """확장된 게이미피케이션: 룰렛·해피아워·스트릭·취향·컬렉션·초대·명예의 전당."""

    def setUp(self):
        self.store = make_store(point_earn_rate="0.03", stamp_goal=3)
        self.m = Member.objects.create(
            store=self.store, phone="01011110000", name="느긋한 수달"
        )
        self.amer = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000,
            category=MenuItem.Category.COFFEE, decaf_available=True, shot_available=True,
        )
        self.latte = MenuItem.objects.create(
            store=self.store, name="카페 라떼", price=4500,
            category=MenuItem.Category.COFFEE, oatmilk_available=True,
        )
        self.cake = MenuItem.objects.create(
            store=self.store, name="치즈케이크", price=6000,
            category=MenuItem.Category.DESSERT, temp_option=MenuItem.Temp.NONE,
        )

    def _buy(self, menu, qty=1, oid=None, **opts):
        return checkout(
            member=self.m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": menu.id, "quantity": qty, **opts}],
            toss_order_id=oid or f"g-{timezone.now().timestamp()}-{menu.id}",
        )

    # ── 룰렛 ──
    def test_stamp_completion_grants_a_spin(self):
        """스탬프를 채우면 바로 돌리지 않고 **기회**를 준다(손님이 직접 돌린다)."""
        for i in range(3):                       # stamp_goal=3
            r = self._buy(self.amer, oid=f"r{i}")
        spins = [x for x in r.rewards if x["type"] == "spin"]
        self.assertEqual(len(spins), 1)
        self.m.refresh_from_db()
        self.assertEqual(self.m.spins, 1)
        self.assertEqual(self.m.stamps, 0)        # 리셋
        # 스탬프만으로는 포인트가 나가지 않는다
        self.assertFalse(self.m.point_entries.filter(reason="stamp").exists())

    def test_spin_issues_coupon_and_consumes_chance(self):
        from .models import Coupon
        from .services import spin

        for i in range(3):
            self._buy(self.amer, oid=f"sp{i}")
        self.m.refresh_from_db()
        result = spin(self.m)
        self.assertEqual(result["spins_left"], 0)
        coupon = Coupon.objects.get(member=self.m)
        self.assertEqual(coupon.kind, result["coupon"]["kind"])
        self.assertTrue(coupon.is_usable)
        # 화면 연출이 멈출 칸과 실제 당첨이 일치해야 한다
        from .rewards import ROULETTE
        self.assertEqual(ROULETTE[result["index"]][0], coupon.kind)

    def test_spin_without_chance_rejected(self):
        from .services import SpinError, spin

        with self.assertRaises(SpinError):
            spin(self.m)

    def test_roulette_probabilities(self):
        """할인쿠폰 합계 60% · 1+1과 무료음료 각 20% · 원두는 보여주기용(0%)."""
        from .models import Coupon
        from .rewards import ROULETTE

        w = dict(ROULETTE)
        total = sum(w.values())
        self.assertEqual(total, 100)
        discount = w[Coupon.Kind.DISCOUNT_5] + w[Coupon.Kind.DISCOUNT_10]
        self.assertEqual(discount, 60)
        self.assertEqual(w[Coupon.Kind.BOGO], 20)
        self.assertEqual(w[Coupon.Kind.FREE_DRINK], 20)
        self.assertEqual(w[Coupon.Kind.BEANS_200], 0)

    def test_beans_never_win(self):
        """가중치 0인 칸은 아무리 돌려도 당첨되지 않는다."""
        from .models import Coupon
        from .rewards import spin_roulette

        kinds = {spin_roulette()[0] for _ in range(500)}
        self.assertNotIn(Coupon.Kind.BEANS_200, kinds)

    # ── 해피아워 ──
    def test_happy_hour_doubles_earning(self):
        from .services import calc_points_earned

        base = calc_points_earned(10000, self.store)
        self.assertEqual(base, 300)               # 3%
        self.store.happy_start, self.store.happy_end = 0, 24
        self.store.happy_multiplier = 2
        self.store.save()
        boosted = calc_points_earned(10000, self.store)
        self.assertEqual(boosted, 600)            # 2배

    def test_happy_hour_inactive_when_unset(self):
        self.assertFalse(self.store.is_happy_hour())

    def test_happy_hour_across_midnight(self):
        self.store.happy_start, self.store.happy_end = 21, 2
        self.store.save()
        # 현지 시각으로 만들어야 한다(now()는 UTC 기준)
        local = timezone.localtime(timezone.now())
        late = local.replace(hour=23, minute=0)
        noon = local.replace(hour=12, minute=0)
        self.assertTrue(self.store.is_happy_hour(late))
        self.assertFalse(self.store.is_happy_hour(noon))

    # ── 취향 · 컬렉션 ──
    def test_taste_and_collection(self):
        from .profile import build_member_dashboard

        self._buy(self.amer, qty=3, oid="t1")
        self._buy(self.cake, qty=1, oid="t2")
        d = build_member_dashboard(self.m)
        self.assertEqual(d["taste"]["favorite"], "아메리카노")
        self.assertEqual(d["taste"]["favorite_qty"], 3)
        # 3종 중 2종 맛봄
        self.assertEqual(d["collection"]["tried"], 2)
        self.assertEqual(d["collection"]["total"], 3)
        # 아직 안 먹어본 메뉴를 추천용으로 알려준다
        coffee = [c for c in d["collection"]["categories"] if c["key"] == "coffee"][0]
        self.assertEqual(coffee["next"], "카페 라떼")

    # ── 스트릭 ──
    def test_streak_counts_consecutive_weeks(self):
        from .streaks import weekly_streak as _streak

        now = timezone.now()
        for weeks_ago in (0, 1, 2):
            t = self._buy(self.amer, oid=f"s{weeks_ago}").transaction
            Transaction.objects.filter(pk=t.pk).update(
                paid_at=now - timedelta(days=7 * weeks_ago)
            )
        s = _streak(self.m)
        self.assertEqual(s["weeks"], 3)
        self.assertTrue(s["alive"] and s["visited_this_week"])

    def test_streak_breaks_after_gap(self):
        from .streaks import weekly_streak as _streak

        t = self._buy(self.amer, oid="s-old").transaction
        Transaction.objects.filter(pk=t.pk).update(
            paid_at=timezone.now() - timedelta(days=30)
        )
        s = _streak(self.m)
        self.assertEqual(s["weeks"], 0)
        self.assertFalse(s["alive"])

    # ── 배지 ──
    def test_option_and_time_badges(self):
        from .profile import build_member_dashboard

        for i in range(3):
            self._buy(self.latte, oid=f"o{i}", oatmilk=True)
        badges = {b["key"]: b["earned"] for b in build_member_dashboard(self.m)["badges"]}
        self.assertTrue(badges["oat"])
        self.assertFalse(badges["decaf"])

    # ── 랭킹 표시 이름 ──
    def test_nickname_shown_but_real_name_masked(self):
        from .profile import _display_name

        self.assertEqual(_display_name("느긋한 수달"), "느긋한 수달")   # 닉네임 그대로
        self.assertEqual(_display_name("김슬로우"), "김***")            # 이관 실명은 가림

    # ── 초대 ──
    def test_referral_rewards_both(self):
        from .rewards import REFERRAL_REWARD
        from .services import apply_referral

        host = Member.objects.create(
            store=self.store, phone="01022220000", name="졸린 판다"
        )
        code = host.ensure_referral_code()
        result = apply_referral(self.m, code)
        self.m.refresh_from_db(); host.refresh_from_db()
        self.assertEqual(result["reward"], REFERRAL_REWARD)
        self.assertEqual(self.m.points, REFERRAL_REWARD)
        self.assertEqual(host.points, REFERRAL_REWARD)
        self.assertEqual(self.m.referred_by_id, host.id)

    def test_referral_rejects_self_reuse_and_unknown(self):
        from .services import ReferralError, apply_referral

        my_code = self.m.ensure_referral_code()
        with self.assertRaises(ReferralError):
            apply_referral(self.m, my_code)          # 자기 코드
        with self.assertRaises(ReferralError):
            apply_referral(self.m, "ZZZZZZ")         # 없는 코드

        host = Member.objects.create(
            store=self.store, phone="01033330000", name="춤추는 알파카"
        )
        apply_referral(self.m, host.ensure_referral_code())
        with self.assertRaises(ReferralError):
            apply_referral(self.m, host.referral_code)   # 두 번째 사용 불가

    def test_referral_codes_are_unique(self):
        codes = {
            Member.objects.create(
                store=self.store, phone=f"010444{i:05d}", name=f"회원{i}"
            ).ensure_referral_code()
            for i in range(20)
        }
        self.assertEqual(len(codes), 20)

    # ── 이달의 단골 ──
    def test_hall_of_fame_uses_nickname(self):
        from .profile import hall_of_fame

        self._buy(self.amer, oid="h1")
        self._buy(self.amer, oid="h2")
        hof = hall_of_fame()
        self.assertEqual(hof["top"][0]["nickname"], "느긋한 수달")
        self.assertEqual(hof["top"][0]["visits"], 2)

    def test_hall_of_fame_endpoint_public(self):
        res = self.client.get("/api/v1/hall-of-fame")   # 고객 화면용 — PIN 없음
        self.assertEqual(res.status_code, 200)
        self.assertIn("top", res.json())


class QuestTests(TestCase):
    """개인 맞춤 퀘스트 — 생성·달성·1회 지급."""

    def setUp(self):
        self.store = make_store(stamp_goal=99)     # 룰렛이 끼어들지 않게
        self.m = Member.objects.create(
            store=self.store, phone="01055550000", name="느긋한 수달"
        )
        self.amer = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000,
            category=MenuItem.Category.COFFEE, oatmilk_available=True,
        )
        self.latte = MenuItem.objects.create(
            store=self.store, name="카페 라떼", price=4500,
            category=MenuItem.Category.COFFEE, oatmilk_available=True,
        )
        self.cake = MenuItem.objects.create(
            store=self.store, name="치즈케이크", price=6000,
            category=MenuItem.Category.DESSERT, temp_option=MenuItem.Temp.NONE,
        )

    def _buy(self, menu, oid, **opts):
        return checkout(
            member=self.m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": menu.id, "quantity": 1, **opts}],
            toss_order_id=oid,
        )

    def _active_keys(self):
        from .quests import active_group

        g = active_group(self.m)
        return {q["key"] for q in (g or {}).get("items", [])}

    def test_taste_quest_offered_for_untried_category(self):
        from .quests import build_candidates

        self._buy(self.amer, "q1")
        # 디저트 미경험 → 후보에 오른다
        self.assertIn("taste:dessert", {q.key for q in build_candidates(self.m)})
        # 다만 지금 열린 챕터는 우선순위가 높은 '도장깨기'다
        self.assertEqual(self._active_keys(), {"collection:coffee"})
        # 그 챕터를 깨면 '취향 탐험대'가 열린다
        self._buy(self.latte, "q2")
        self.assertIn("taste:dessert", self._active_keys())

    def test_quest_completed_awards_points_once(self):
        from .models import MemberQuest

        self._buy(self.amer, "q1")
        before = self.m.points
        # 디저트를 사면 taste:dessert 달성
        r = self._buy(self.cake, "q2")
        quest_rewards = [x for x in r.rewards if x["type"] == "quest"]
        self.assertTrue(quest_rewards)
        self.m.refresh_from_db()
        self.assertGreater(self.m.points, before)
        self.assertTrue(MemberQuest.objects.filter(member=self.m, key="taste:dessert").exists())

        # 다시 사도 중복 지급되지 않는다
        pts = self.m.points
        r2 = self._buy(self.cake, "q3")
        self.assertFalse([x for x in r2.rewards if x.get("title", "").startswith("디저트 처음")])
        self.assertEqual(
            MemberQuest.objects.filter(member=self.m, key="taste:dessert").count(), 1
        )
        # 챕터를 다 깼으니 다음 챕터가 열린다
        self.assertNotIn("taste:dessert", self._active_keys())

    def test_option_quest(self):
        self._buy(self.amer, "o1")
        r = self._buy(self.latte, "o2", oatmilk=True)
        self.assertTrue([x for x in r.rewards if "오트밀크" in x["title"]])

    def test_only_one_group_is_active(self):
        """서로 다른 성격의 목표를 한꺼번에 던지지 않는다 — 한 챕터씩."""
        from .quests import GROUP_SIZE, active_group

        self._buy(self.amer, "m1")
        g = active_group(self.m)
        self.assertIsNotNone(g)
        self.assertLessEqual(len(g["items"]), GROUP_SIZE)
        self.assertEqual({q["group"] for q in g["items"]}, {g["key"]})

    def test_group_clear_awards_bonus_once(self):
        """챕터를 완주하면 보너스가 한 번 붙는다."""
        from .models import MemberQuest
        from .quests import GROUP_META, group_key

        self._buy(self.amer, "g1")                      # taste 챕터 = 디저트 하나
        r = self._buy(self.cake, "g2")                  # 완주
        bonus = [x for x in r.rewards if x["type"] == "quest_group"]
        self.assertTrue(bonus)
        self.assertEqual(bonus[0]["points"], GROUP_META["taste"][2])
        self.assertTrue(
            MemberQuest.objects.filter(member=self.m, key=group_key("taste")).exists()
        )
        r2 = self._buy(self.cake, "g3")                 # 두 번은 없다
        self.assertFalse([x for x in r2.rewards if x["type"] == "quest_group"])

    def test_reward_capped(self):
        from .quests import MAX_REWARD, active_group

        for i in range(6):
            self._buy(self.amer, f"c{i}")
        for q in active_group(self.m)["items"]:
            self.assertLessEqual(q["reward"], MAX_REWARD)

    def test_checkout_reward_budget(self):
        """한 번의 결제에서 나가는 퀘스트 보상 총액을 묶어 둔다."""
        from .quests import MAX_REWARD_PER_CHECKOUT

        for i in range(6):
            self._buy(self.amer, f"b{i}")
        r = self._buy(self.latte, "bx", oatmilk=True, shot=True)
        paid = sum(x["points"] for x in r.rewards if x["type"].startswith("quest"))
        self.assertLessEqual(paid, MAX_REWARD_PER_CHECKOUT)

    def test_personal_difficulty_scales_with_habit(self):
        """월간 도전 목표는 그 사람 평소 방문 수에 맞춰 정해진다."""
        from .quests import build_candidates

        for i in range(8):
            t = self._buy(self.amer, f"h{i}").transaction
            Transaction.objects.filter(pk=t.pk).update(
                paid_at=timezone.now() - timedelta(days=i)
            )
        self.m.refresh_from_db()
        stretch = [q for q in build_candidates(self.m) if q.kind == "stretch"]
        self.assertTrue(stretch)
        self.assertGreaterEqual(stretch[0].target, 2)

    def test_avg_interval_needs_enough_visits(self):
        from .quests import avg_interval_days

        self.assertIsNone(avg_interval_days(self.m))    # 방문 0회


class BadgeExtensionTests(TestCase):
    """배지 확장 — 단계·희귀도·대표 칭호·히든."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.store = make_store()
        self.m = Member.objects.create(
            store=self.store, phone="01066660000", name="졸린 판다",
            visit_count=60, total_spent=250_000, tier=Member.Tier.GOLD,
        )

    def test_leveled_badge_advances(self):
        from .profile import build_badges_only

        badges = {b["key"]: b for b in build_badges_only(self.m)}
        cups = badges["cups"]
        self.assertEqual(cups["level"], 2)              # 60잔 → Ⅱ
        self.assertIn("Ⅱ", cups["title"])
        self.assertIn("다음 단계까지", cups["desc"])

        self.m.visit_count = 150
        top = {b["key"]: b for b in build_badges_only(self.m)}["cups"]
        self.assertEqual(top["level"], 3)
        self.assertEqual(top["desc"], "최고 단계 달성")

    def test_rarity_reflects_population(self):
        from .profile import badge_rarity

        # 방문 0인 회원 3명 추가 → '10잔 클럽'은 4명 중 1명만 보유
        for i in range(3):
            Member.objects.create(
                store=self.store, phone=f"010777700{i:02d}", name=f"신규 {i}"
            )
        rarity = badge_rarity()
        self.assertEqual(rarity["club10"], 25.0)
        self.assertEqual(rarity["first"], 25.0)

    def test_title_picks_rarest_badge(self):
        from .profile import build_member_dashboard

        for i in range(9):
            Member.objects.create(
                store=self.store, phone=f"010888800{i:02d}", name=f"흔한 {i}",
                visit_count=1,
            )
        d = build_member_dashboard(self.m)
        self.assertIsNotNone(d["title"])
        # 흔한 배지(first, 전원 보유)가 칭호가 되면 안 된다
        self.assertNotEqual(d["title"]["key"], "first")
        self.assertLessEqual(d["title"]["rarity"], 100.0)

    def test_hidden_badge_appears_only_when_earned(self):
        from .profile import build_badges_only

        keys = {b["key"] for b in build_badges_only(self.m)}
        self.assertNotIn("jackpot", keys)      # 아직 없음 → 목록에 없다

        from .models import Coupon
        Coupon.objects.create(member=self.m, kind=Coupon.Kind.FREE_DRINK)
        keys2 = {b["key"] for b in build_badges_only(self.m)}
        self.assertIn("jackpot", keys2)        # 달성하면 나타난다

    def test_dashboard_exposes_rarity_on_badges(self):
        from .profile import build_member_dashboard

        d = build_member_dashboard(self.m)
        earned = [b for b in d["badges"] if b.get("earned")]
        self.assertTrue(all("rarity" in b for b in earned))


class HealthEndpointTests(TestCase):
    def test_health_reports_pending_migrations(self):
        """스키마가 뒤처지면 눈에 보여야 한다(배포에서 500을 낸 원인)."""
        res = self.client.get("/api/v1/health")
        self.assertEqual(res.json()["db"]["pending_migrations"], 0)

        from unittest import mock

        with mock.patch(
            "django.db.migrations.executor.MigrationExecutor.migration_plan",
            return_value=[("membership", "0009_x"), ("membership", "0010_y")],
        ):
            res = self.client.get("/api/v1/health")
        body = res.json()
        self.assertEqual(body["db"]["pending_migrations"], 2)
        self.assertEqual(body["status"], "degraded")
        self.assertIn("마이그레이션", body["warning"])

    def test_health_reports_persistent_storage(self):
        res = self.client.get("/api/v1/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["db"]["ok"])
        # 테스트는 로컬 SQLite(비서버리스) → 영구 저장으로 보고
        self.assertTrue(body["db"]["persistent"])
        self.assertNotIn("warning", body)


class CouponAndTierTests(TestCase):
    """쿠폰 발행 — 등급 승급 · 룰렛 · 만료."""

    def setUp(self):
        self.store = make_store(stamp_goal=99)
        self.m = Member.objects.create(
            store=self.store, phone="01066660000", name="느긋한 수달"
        )
        self.menu = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=100_000
        )

    def _buy(self, oid, qty=1):
        return checkout(
            member=self.m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": qty}],
            toss_order_id=oid,
        )

    def test_silver_upgrade_issues_one_bogo(self):
        from .models import Coupon

        self._buy("t1")                       # 10만원 → 실버
        self.m.refresh_from_db()
        self.assertEqual(self.m.tier, Member.Tier.SILVER)
        self.assertEqual(
            Coupon.objects.filter(member=self.m, kind=Coupon.Kind.BOGO).count(), 1
        )

    def test_gold_upgrade_issues_three_more(self):
        from .models import Coupon

        self._buy("t1")                       # 10만 → 실버(1장)
        self._buy("t2", qty=2)                # 누적 30만 → 골드(3장)
        self.m.refresh_from_db()
        self.assertEqual(self.m.tier, Member.Tier.GOLD)
        self.assertEqual(Coupon.objects.filter(member=self.m).count(), 4)

    def test_skipping_to_gold_gives_both_tiers(self):
        """한 번에 골드까지 올라도 건너뛴 실버 몫을 함께 준다."""
        from .models import Coupon

        self._buy("t1", qty=3)                # 30만원 한 번에
        self.m.refresh_from_db()
        self.assertEqual(self.m.tier, Member.Tier.GOLD)
        self.assertEqual(Coupon.objects.filter(member=self.m).count(), 4)

    def test_tier_coupons_not_reissued(self):
        from .models import Coupon

        self._buy("t1")
        self._buy("t2")                       # 여전히 실버 구간 위
        self.assertEqual(Coupon.objects.filter(member=self.m).count(), 1)

    def test_expired_coupon_hidden_from_dashboard(self):
        from .models import Coupon
        from .profile import coupon_list

        c = Coupon.objects.create(member=self.m, kind=Coupon.Kind.BOGO)
        self.assertEqual(len(coupon_list(self.m)), 1)
        Coupon.objects.filter(pk=c.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(len(coupon_list(self.m)), 0)


class StreakSpinTests(TestCase):
    """연속 방문 → 룰렛 기회."""

    def setUp(self):
        self.store = make_store(stamp_goal=99)
        self.m = Member.objects.create(
            store=self.store, phone="01066661111", name="부지런한 다람쥐"
        )
        self.menu = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000
        )

    def _visit_on(self, days_ago, oid):
        r = checkout(
            member=self.m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": 1}],
            toss_order_id=oid,
        )
        Transaction.objects.filter(pk=r.transaction.pk).update(
            paid_at=timezone.now() - timedelta(days=days_ago)
        )
        return r

    def test_five_day_streak_grants_spin(self):
        """5일째 결제 시점에 룰렛 기회가 붙는다(지급은 결제 안에서 일어난다)."""
        from .streaks import grant_spins

        for d in range(4, 0, -1):
            self._visit_on(d, f"d{d}")
        self.m.refresh_from_db()
        self.assertEqual(self.m.spins, 0)          # 아직 4일
        r = self._visit_on(0, "d0")                # 5일째 — 오늘
        self.assertTrue([x for x in r.rewards if x["type"] == "spin"])
        self.m.refresh_from_db()
        self.assertEqual(self.m.spins, 1)
        # 같은 주기로 두 번 받지 않는다
        self.assertEqual(grant_spins(self.m), [])
        self.assertEqual(self.m.spins, 1)

    def test_streak_reward_repeats_next_cycle(self):
        """5일에서 끝나면 이어갈 이유가 없다 — 10일째에 다시 한 번."""
        from .streaks import _cycle_key

        self.assertNotEqual(_cycle_key("daily", 5, 5), _cycle_key("daily", 10, 5))
        self.assertEqual(_cycle_key("daily", 5, 5), _cycle_key("daily", 9, 5))

    def test_four_week_streak_grants_spin(self):
        for w in range(3, 0, -1):
            self._visit_on(w * 7, f"w{w}")
        r = self._visit_on(0, "w0")                # 4주째
        self.assertTrue(
            [x for x in r.rewards if x["type"] == "spin" and x["title"].startswith("4주")]
        )

    def test_daily_streak_survives_today_not_visited_yet(self):
        from .streaks import daily_streak

        for d in (3, 2, 1):
            self._visit_on(d, f"y{d}")
        self.m.refresh_from_db()
        s = daily_streak(self.m)
        self.assertEqual(s["days"], 3)
        self.assertTrue(s["alive"])
        self.assertFalse(s["visited_today"])


class ReferralLimitTests(TestCase):
    """초대 — 하루 1명, 각 1,000P."""

    def setUp(self):
        self.store = make_store()
        self.host = Member.objects.create(
            store=self.store, phone="01055551111", name="초대한 사람"
        )
        self.code = self.host.ensure_referral_code()

    def _guest(self, n):
        return Member.objects.create(
            store=self.store, phone=f"0105555{n:04d}", name=f"손님 {n}"
        )

    def test_reward_is_1000_each(self):
        from .services import apply_referral

        g = self._guest(1)
        r = apply_referral(g, self.code)
        self.assertEqual(r["reward"], 1000)
        g.refresh_from_db()
        self.host.refresh_from_db()
        self.assertEqual(g.points, 1000)
        self.assertEqual(self.host.points, 1000)

    def test_second_invite_same_day_rejected(self):
        from .services import ReferralError, apply_referral

        apply_referral(self._guest(1), self.code)
        with self.assertRaises(ReferralError):
            apply_referral(self._guest(2), self.code)

    def test_invite_allowed_again_next_day(self):
        from .services import apply_referral

        g1 = self._guest(1)
        apply_referral(g1, self.code)
        Member.objects.filter(pk=g1.pk).update(
            referral_used_at=timezone.now() - timedelta(days=1)
        )
        apply_referral(self._guest(2), self.code)   # 어제 것은 오늘 한도에 안 든다
        self.host.refresh_from_db()
        self.assertEqual(self.host.points, 2000)


class MissionClearTests(TestCase):
    """미션 500P + 전부 달성 시 1,000P 보너스."""

    def setUp(self):
        from .models import Mission

        self.store = make_store(stamp_goal=99)
        self.m = Member.objects.create(
            store=self.store, phone="01044440000", name="성실한 토끼"
        )
        self.menu = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000
        )
        Mission.objects.create(
            store=self.store, title="1회 방문", condition_type=Mission.Condition.VISIT_COUNT,
            target_value=1, reward_points=500,
        )
        Mission.objects.create(
            store=self.store, title="2회 방문", condition_type=Mission.Condition.VISIT_COUNT,
            target_value=2, reward_points=500,
        )

    def _buy(self, oid):
        return checkout(
            member=self.m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": 1}],
            toss_order_id=oid,
        )

    def test_clear_bonus_once(self):
        from .services import MISSION_CLEAR_BONUS

        self._buy("m1")                     # 미션 1개 달성
        r = self._buy("m2")                 # 나머지 달성 → 완주 보너스
        clear = [x for x in r.rewards if x["type"] == "mission_clear"]
        self.assertEqual(len(clear), 1)
        self.assertEqual(clear[0]["points"], MISSION_CLEAR_BONUS)
        r2 = self._buy("m3")
        self.assertFalse([x for x in r2.rewards if x["type"] == "mission_clear"])


class MonthlyRankingTests(TestCase):
    """월간 랭킹 — 금액·횟수 두 부문."""

    def setUp(self):
        self.store = make_store(stamp_goal=99)
        self.menu = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=1000
        )
        self.big = Member.objects.create(
            store=self.store, phone="01033330001", name="큰손 고양이"
        )
        self.often = Member.objects.create(
            store=self.store, phone="01033330002", name="자주 오는 여우"
        )

    def _buy(self, member, qty, oid):
        return checkout(
            member=member, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": qty}],
            toss_order_id=oid,
        )

    def test_two_boards_rank_differently(self):
        from .profile import hall_of_fame

        self._buy(self.big, 50, "b1")                 # 1회 · 5만원
        for i in range(4):
            self._buy(self.often, 1, f"o{i}")         # 4회 · 4천원

        hof = hall_of_fame()
        spent = {b["key"]: b for b in hof["boards"]}["spent"]
        visits = {b["key"]: b for b in hof["boards"]}["visits"]
        self.assertEqual(spent["top"][0]["nickname"], "큰손 고양이")
        self.assertEqual(visits["top"][0]["nickname"], "자주 오는 여우")
        # 시상 내역이 순위와 함께 내려간다
        self.assertEqual(spent["top"][0]["prize"], "아메리카노 + 플레인 휘낭시에")
        self.assertEqual(visits["top"][1]["prize"], "아메리카노")


class SpinEndpointTests(TestCase):
    """룰렛 API — 손님 폰에서 PIN 없이 호출."""

    def setUp(self):
        self.store = make_store()
        self.m = Member.objects.create(
            store=self.store, phone="01022220000", name="궁금한 너구리", spins=1
        )

    def test_spin_public_and_issues_coupon(self):
        res = self.client.post(f"/api/v1/members/{self.m.id}/spin")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["spins_left"], 0)
        self.assertIn("coupon", body)
        self.assertIn(body["index"], range(5))

    def test_spin_without_chance_returns_400(self):
        self.client.post(f"/api/v1/members/{self.m.id}/spin")
        res = self.client.post(f"/api/v1/members/{self.m.id}/spin")
        self.assertEqual(res.status_code, 400)

    def test_dashboard_exposes_wheel_and_spins(self):
        res = self.client.get(f"/api/v1/members/{self.m.id}/dashboard")
        r = res.json()["roulette"]
        self.assertEqual(r["spins"], 1)
        self.assertEqual(len(r["segments"]), 5)
        # 확률은 화면으로 내려보내지 않는다(조작 방지)
        self.assertNotIn("weight", r["segments"][0])


class ImportBaselineTests(TestCase):
    """이관 회원에게 지난 기록에 대한 보상이 소급 지급되지 않아야 한다."""

    def setUp(self):
        from .models import Mission

        self.store = make_store(stamp_goal=99)
        self.menu = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000
        )
        for title, target, reward in (
            ("3회 방문", 3, 500), ("10회 방문", 10, 500),
        ):
            Mission.objects.create(
                store=self.store, title=title,
                condition_type=Mission.Condition.VISIT_COUNT,
                target_value=target, reward_points=reward,
            )
        Mission.objects.create(
            store=self.store, title="누적 5만원", condition_type=Mission.Condition.TOTAL_SPENT,
            target_value=50_000, reward_points=500,
        )

    def _import(self, spent, visits):
        from .imports import import_members_csv

        import_members_csv(csv_text=(
            "이름,연락처,포인트,누적결제액,방문횟수\n"
            f"김단골,010-9999-0001,0,{spent},{visits}\n"
        ))
        return Member.objects.get(phone="01099990001")

    def _buy(self, m):
        return checkout(
            member=m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": 1}],
            toss_order_id="imp-1",
        )

    def test_no_retroactive_tier_coupons(self):
        """이미 골드인 채로 들어온 회원에게 승급 쿠폰이 나가면 안 된다."""
        from .models import Coupon

        m = self._import(380_000, 95)
        self.assertEqual(m.tier, Member.Tier.GOLD)
        self.assertEqual(m.tier_rewarded, Member.Tier.GOLD)   # 이관 시점에 정산됨
        self._buy(m)
        self.assertEqual(Coupon.objects.filter(member=m).count(), 0)

    def test_no_retroactive_mission_points(self):
        """이미 채운 미션의 보상·완주 보너스가 첫 결제에 쏟아지면 안 된다."""
        m = self._import(380_000, 95)
        r = self._buy(m)
        self.assertFalse([x for x in r.rewards if x["type"].startswith("mission")])
        m.refresh_from_db()
        self.assertEqual(m.points, r.transaction.points_earned)   # 적립분만

    def test_partial_progress_still_rewarded_going_forward(self):
        """아직 못 채운 미션은 그대로 살아 있어야 한다 — 앞으로 하는 건 보상한다."""
        m = self._import(10_000, 1)          # 어느 미션도 미달
        r = self._buy(m)
        self.assertFalse([x for x in r.rewards if x["type"] == "mission"])  # 2회차 — 아직 미달
        got = []
        for i in range(2):
            r = checkout(
                member=m, gross_amount=0, points_to_use=0,
                payment_method=Transaction.Method.CARD,
                items=[{"menu_item_id": self.menu.id, "quantity": 1}],
                toss_order_id=f"imp-f{i}",
            )
            got += [x["title"] for x in r.rewards if x["type"] == "mission"]
        self.assertIn("3회 방문", got)         # 방문 3회를 우리 앱에서 채우면 지급된다

    def test_tier_coupon_still_issued_when_actually_upgrading(self):
        """이관 후 실제로 등급이 올라가면 그때는 쿠폰이 나가야 한다."""
        from .models import Coupon

        m = self._import(90_000, 5)          # 브론즈
        self.assertEqual(m.tier, Member.Tier.BRONZE)
        big = MenuItem.objects.create(store=self.store, name="원두 1kg", price=20_000)
        checkout(
            member=m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": big.id, "quantity": 1}],
            toss_order_id="up-1",
        )
        m.refresh_from_db()
        self.assertEqual(m.tier, Member.Tier.SILVER)
        self.assertEqual(Coupon.objects.filter(member=m).count(), 1)


class CouponRedeemTests(TestCase):
    """쿠폰 사용 — 금액 차감 · 1회 소진 · 취소 시 원복."""

    def setUp(self):
        from .models import Coupon

        self.store = make_store(stamp_goal=99, set_discount_amount=0)
        self.m = Member.objects.create(
            store=self.store, phone="01077770000", name="느긋한 수달"
        )
        self.amer = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000, category=MenuItem.Category.COFFEE
        )
        self.latte = MenuItem.objects.create(
            store=self.store, name="카페 라떼", price=5000, category=MenuItem.Category.COFFEE
        )
        self.cake = MenuItem.objects.create(
            store=self.store, name="플레인 휘낭시에", price=2500,
            category=MenuItem.Category.DESSERT, temp_option=MenuItem.Temp.NONE,
        )
        self.Coupon = Coupon

    def _coupon(self, kind):
        return self.Coupon.objects.create(member=self.m, kind=kind)

    def _buy(self, items, oid, coupon=None):
        return checkout(
            member=self.m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD, items=items,
            toss_order_id=oid, coupon_id=coupon.id if coupon else None,
        )

    def test_percent_coupon_discounts_total(self):
        c = self._coupon(self.Coupon.Kind.DISCOUNT_10)
        r = self._buy([{"menu_item_id": self.amer.id, "quantity": 2}], "c1", c)
        self.assertEqual(r.transaction.gross_amount, 8000)
        self.assertEqual(r.transaction.discount, 800)      # 10%
        self.assertEqual(r.transaction.net_amount, 7200)

    def test_bogo_takes_the_cheaper_drink(self):
        c = self._coupon(self.Coupon.Kind.BOGO)
        r = self._buy([{"menu_item_id": self.amer.id, "quantity": 1},
                       {"menu_item_id": self.latte.id, "quantity": 1}], "c2", c)
        self.assertEqual(r.transaction.discount, 4000)     # 싼 쪽(아메리카노)
        self.assertEqual(r.transaction.net_amount, 5000)

    def test_free_drink_takes_the_priciest(self):
        c = self._coupon(self.Coupon.Kind.FREE_DRINK)
        r = self._buy([{"menu_item_id": self.amer.id, "quantity": 1},
                       {"menu_item_id": self.latte.id, "quantity": 1}], "c3", c)
        self.assertEqual(r.transaction.discount, 5000)     # 비싼 쪽(라떼)

    def test_bogo_needs_two_drinks(self):
        from .services import CouponError

        c = self._coupon(self.Coupon.Kind.BOGO)
        with self.assertRaises(CouponError):
            self._buy([{"menu_item_id": self.amer.id, "quantity": 1}], "c4", c)

    def test_dessert_is_not_a_drink(self):
        """1+1은 음료 쿠폰이다 — 디저트를 두 번째 잔으로 치면 안 된다."""
        from .services import CouponError

        c = self._coupon(self.Coupon.Kind.BOGO)
        with self.assertRaises(CouponError):
            self._buy([{"menu_item_id": self.amer.id, "quantity": 1},
                       {"menu_item_id": self.cake.id, "quantity": 1}], "c5", c)

    def test_coupon_consumed_once(self):
        from .services import CouponError

        c = self._coupon(self.Coupon.Kind.DISCOUNT_5)
        self._buy([{"menu_item_id": self.amer.id, "quantity": 1}], "c6", c)
        c.refresh_from_db()
        self.assertIsNotNone(c.used_at)
        with self.assertRaises(CouponError):
            self._buy([{"menu_item_id": self.amer.id, "quantity": 1}], "c7", c)

    def test_expired_coupon_rejected(self):
        from .services import CouponError

        c = self._coupon(self.Coupon.Kind.DISCOUNT_5)
        self.Coupon.objects.filter(pk=c.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        c.refresh_from_db()
        with self.assertRaises(CouponError):
            self._buy([{"menu_item_id": self.amer.id, "quantity": 1}], "c8", c)

    def test_other_members_coupon_rejected(self):
        from .services import CouponError

        other = Member.objects.create(store=self.store, phone="01077771111", name="다른 손님")
        c = self.Coupon.objects.create(member=other, kind=self.Coupon.Kind.DISCOUNT_5)
        with self.assertRaises(CouponError):
            self._buy([{"menu_item_id": self.amer.id, "quantity": 1}], "c9", c)

    def test_cancel_restores_coupon(self):
        """취소했는데 쿠폰만 사라지면 손님이 손해를 본다."""
        c = self._coupon(self.Coupon.Kind.BOGO)
        r = self._buy([{"menu_item_id": self.amer.id, "quantity": 2}], "c10", c)
        cancel_transaction(r.transaction)
        c.refresh_from_db()
        self.assertIsNone(c.used_at)
        self.assertTrue(c.is_usable)

    def test_earning_is_on_the_discounted_amount(self):
        """적립은 실제로 받은 돈 기준이어야 한다."""
        c = self._coupon(self.Coupon.Kind.DISCOUNT_10)
        r = self._buy([{"menu_item_id": self.amer.id, "quantity": 1}], "c11", c)
        self.assertEqual(r.transaction.net_amount, 3600)
        self.assertEqual(r.transaction.points_earned, 108)   # 3600 × 3%

    def test_staff_can_list_member_coupons(self):
        self._coupon(self.Coupon.Kind.BOGO)
        authenticate(self.client)
        res = self.client.get(f"/api/v1/members/{self.m.id}/coupons")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)


class SizeUpTests(TestCase):
    """사이즈업 — 메뉴마다 추가금이 다르다."""

    def setUp(self):
        self.store = make_store(stamp_goal=99, set_discount_amount=0)
        self.m = Member.objects.create(
            store=self.store, phone="01022220001", name="느긋한 수달"
        )
        self.amer = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000,
            category=MenuItem.Category.COFFEE, size_up_price=1500,
        )
        self.vanilla = MenuItem.objects.create(
            store=self.store, name="바닐라 라떼", price=5000,
            category=MenuItem.Category.COFFEE, size_up_price=2000,
        )
        self.tea = MenuItem.objects.create(
            store=self.store, name="히비스커스", price=4000,
            category=MenuItem.Category.TEA,          # 사이즈업 없음(0)
        )

    def _buy(self, items, oid):
        return checkout(
            member=self.m, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD, items=items, toss_order_id=oid,
        )

    def test_price_differs_per_menu(self):
        r = self._buy([
            {"menu_item_id": self.amer.id, "quantity": 1, "size_up": True},
            {"menu_item_id": self.vanilla.id, "quantity": 1, "size_up": True},
        ], "u1")
        prices = {i.name: i.unit_price for i in r.transaction.items.all()}
        self.assertEqual(prices["아메리카노"], 5500)      # 4000 + 1500
        self.assertEqual(prices["바닐라 라떼"], 7000)     # 5000 + 2000

    def test_ignored_when_menu_has_no_size_up(self):
        """추가금이 0인 메뉴에 사이즈업을 보내도 돈을 더 받지 않는다."""
        r = self._buy([{"menu_item_id": self.tea.id, "quantity": 1, "size_up": True}], "u2")
        item = r.transaction.items.get()
        self.assertEqual(item.unit_price, 4000)
        self.assertFalse(item.size_up)

    def test_same_menu_with_and_without_size_up_are_separate_lines(self):
        r = self._buy([
            {"menu_item_id": self.amer.id, "quantity": 1, "size_up": True},
            {"menu_item_id": self.amer.id, "quantity": 2},
        ], "u3")
        self.assertEqual(r.transaction.gross_amount, 5500 + 8000)

    def test_option_label_shows_size_up(self):
        r = self._buy([{"menu_item_id": self.amer.id, "quantity": 1,
                        "temperature": "ice", "size_up": True}], "u4")
        self.assertIn("사이즈업", r.transaction.items.get().option_label)


class MenuAdminApiTests(TestCase):
    """POS에서 메뉴 추가·수정·삭제 — 디저트가 매일 바뀐다."""

    def setUp(self):
        self.store = make_store()
        self.menu = MenuItem.objects.create(
            store=self.store, name="플레인 휘낭시에", price=2500,
            category=MenuItem.Category.DESSERT, temp_option=MenuItem.Temp.NONE,
        )
        authenticate(self.client)

    def _post(self, **data):
        return self.client.post("/api/v1/menu", data=data, content_type="application/json")

    def test_add_dessert(self):
        res = self._post(name="말차 휘낭시에", price=3500, category="dessert",
                         temp_option="none")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(MenuItem.objects.filter(name="말차 휘낭시에").exists())

    def test_duplicate_name_rejected(self):
        res = self._post(name="플레인 휘낭시에", price=2500, category="dessert")
        self.assertEqual(res.status_code, 400)

    def test_price_must_be_positive(self):
        res = self._post(name="공짜 디저트", price=0, category="dessert")
        self.assertEqual(res.status_code, 400)

    def test_requires_staff_token(self):
        anon = self.client_class()
        res = anon.post("/api/v1/menu", data={"name": "몰래 추가", "price": 100},
                        content_type="application/json")
        self.assertEqual(res.status_code, 403)
        self.assertFalse(MenuItem.objects.filter(name="몰래 추가").exists())

    def test_toggle_availability(self):
        res = self.client.patch(f"/api/v1/menu/{self.menu.id}",
                                data={"is_available": False},
                                content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.menu.refresh_from_db()
        self.assertFalse(self.menu.is_available)
        # 판매중지된 메뉴는 주문 화면 목록에서 빠진다
        names = [m["name"] for m in self.client.get("/api/v1/menu").json()]
        self.assertNotIn("플레인 휘낭시에", names)

    def test_delete_keeps_past_orders(self):
        """메뉴를 지워도 지난 매출은 남아야 한다."""
        r = checkout(
            member=None, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.menu.id, "quantity": 1}],
            toss_order_id="d1",
        )
        # 오늘 판 메뉴는 실수 방지로 한 번 막는다
        res = self.client.delete(f"/api/v1/menu/{self.menu.id}")
        self.assertEqual(res.status_code, 409)
        res = self.client.delete(f"/api/v1/menu/{self.menu.id}?force=1")
        self.assertEqual(res.status_code, 200)

        item = r.transaction.items.get()
        item.refresh_from_db()
        self.assertEqual(item.name, "플레인 휘낭시에")     # 이름 스냅샷 보존
        self.assertEqual(item.unit_price, 2500)
        self.assertIsNone(item.menu_item)                  # 참조만 끊긴다
        r.transaction.refresh_from_db()
        self.assertEqual(r.transaction.gross_amount, 2500)

    def test_unsold_menu_deletes_without_force(self):
        res = self.client.delete(f"/api/v1/menu/{self.menu.id}")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(MenuItem.objects.filter(pk=self.menu.id).exists())

    def test_all_flag_needs_token(self):
        anon = self.client_class()
        self.assertEqual(anon.get("/api/v1/menu?all=1").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/menu?all=1").status_code, 200)


class ManualDiscountTests(TestCase):
    """결제 화면 수기 할인 — 매장이 허용한 할인율만."""

    def setUp(self):
        self.store = make_store(stamp_goal=99, set_discount_amount=0,
                                discount_rates="5,10")
        self.m = Member.objects.create(
            store=self.store, phone="01011110009", name="느긋한 수달"
        )
        self.amer = MenuItem.objects.create(
            store=self.store, name="아메리카노", price=4000,
            category=MenuItem.Category.COFFEE,
        )

    def _buy(self, oid, pct=0, qty=2, member=None, coupon=None):
        return checkout(
            member=member, gross_amount=0, points_to_use=0,
            payment_method=Transaction.Method.CARD,
            items=[{"menu_item_id": self.amer.id, "quantity": qty}],
            toss_order_id=oid, discount_pct=pct,
            coupon_id=coupon.id if coupon else None,
        )

    def test_applies_percentage(self):
        t = self._buy("d1", 10).transaction
        self.assertEqual(t.gross_amount, 8000)
        self.assertEqual(t.discount, 800)
        self.assertEqual(t.net_amount, 7200)
        self.assertEqual(t.manual_discount_pct, 10)

    def test_rate_not_allowed_is_ignored(self):
        """화면을 조작해 50%를 보내도 먹지 않는다."""
        t = self._buy("d2", 50).transaction
        self.assertEqual(t.discount, 0)
        self.assertEqual(t.net_amount, 8000)
        self.assertEqual(t.manual_discount_pct, 0)

    def test_store_can_change_rates(self):
        self.store.discount_rates = "15"
        self.store.save(update_fields=["discount_rates"])
        self.assertEqual(self._buy("d3", 15).transaction.discount, 1200)
        self.assertEqual(self._buy("d4", 10).transaction.discount, 0)   # 이제 허용 안 됨

    def test_rate_list_parses_and_ignores_garbage(self):
        self.store.discount_rates = " 10 , 5, 어쩌고, 0, 150 "
        self.assertEqual(self.store.discount_rate_list, [5, 10])

    def test_empty_rates_disables_manual_discount(self):
        self.store.discount_rates = ""
        self.store.save(update_fields=["discount_rates"])
        self.assertEqual(self.store.discount_rate_list, [])
        self.assertEqual(self._buy("d5", 10).transaction.discount, 0)

    def test_stacks_with_coupon(self):
        from .models import Coupon

        c = Coupon.objects.create(member=self.m, kind=Coupon.Kind.DISCOUNT_10)
        t = self._buy("d6", 5, member=self.m, coupon=c).transaction
        self.assertEqual(t.discount, 800 + 400)      # 쿠폰 10% + 수기 5%
        self.assertEqual(t.net_amount, 6800)

    def test_earning_follows_discounted_amount(self):
        """할인해 준 만큼 적립도 줄어야 한다 — 적립은 실제로 받은 돈 기준."""
        t = self._buy("d7", 10, member=self.m).transaction
        self.assertEqual(t.points_earned, 216)       # 7200 × 3%

    def test_discount_never_exceeds_order(self):
        self.store.discount_rates = "99"
        self.store.save(update_fields=["discount_rates"])
        t = self._buy("d8", 99).transaction
        self.assertLessEqual(t.discount, t.gross_amount)
        self.assertGreaterEqual(t.net_amount, 0)


class MenuReorderTests(TestCase):
    """메뉴 순서 저장 — 드래그 한 번에 한 요청."""

    def setUp(self):
        self.store = make_store()
        self.items = [
            MenuItem.objects.create(store=self.store, name=n, price=4000,
                                    category=MenuItem.Category.COFFEE, sort_order=i)
            for i, n in enumerate(["아메리카노", "카페 라떼", "바닐라 라떼"], start=1)
        ]
        authenticate(self.client)

    def _order(self, ids):
        return self.client.post("/api/v1/menu/reorder", data={"ids": ids},
                                content_type="application/json")

    def _names(self):
        return list(
            MenuItem.objects.order_by("sort_order", "id").values_list("name", flat=True)
        )

    def test_reorder(self):
        a, b, c = self.items
        res = self._order([c.id, a.id, b.id])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._names(), ["바닐라 라떼", "아메리카노", "카페 라떼"])

    def test_menu_list_follows_new_order(self):
        a, b, c = self.items
        self._order([c.id, b.id, a.id])
        names = [m["name"] for m in self.client.get("/api/v1/menu").json()]
        self.assertEqual(names, ["바닐라 라떼", "카페 라떼", "아메리카노"])

    def test_unknown_id_rejected_without_partial_write(self):
        """일부만 반영되면 순서가 어긋난 채로 남는다 — 통째로 거절한다."""
        a, b, c = self.items
        before = self._names()
        res = self._order([c.id, a.id, 99999])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._names(), before)

    def test_bad_payload(self):
        self.assertEqual(self._order([]).status_code, 400)
        self.assertEqual(self._order(["가나다"]).status_code, 400)

    def test_requires_staff_token(self):
        anon = self.client_class()
        res = anon.post("/api/v1/menu/reorder",
                        data={"ids": [self.items[0].id]}, content_type="application/json")
        self.assertEqual(res.status_code, 403)
