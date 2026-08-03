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
    def test_stamp_completion_spins_roulette(self):
        from .rewards import ROULETTE

        segs = [p for p, _ in ROULETTE]
        for i in range(3):                       # stamp_goal=3
            r = self._buy(self.amer, oid=f"r{i}")
        wheel = [x for x in r.rewards if x["type"] == "roulette"]
        self.assertEqual(len(wheel), 1)
        w = wheel[0]
        self.assertIn(w["points"], segs)          # 실제 칸 중 하나
        self.assertEqual(w["wheel"], segs)
        self.assertEqual(segs[w["index"]], w["points"])   # 화면 연출과 결과가 일치
        # 원장에 스탬프 보상으로 기록
        self.m.refresh_from_db()
        entry = self.m.point_entries.filter(reason="stamp").get()
        self.assertEqual(entry.delta, w["points"])
        self.assertEqual(self.m.stamps, 0)        # 리셋

    def test_roulette_expected_value_close_to_fixed_reward(self):
        """비용이 크게 늘지 않아야 한다(기댓값 ≈ 기존 3,000P)."""
        from .rewards import ROULETTE

        ev = sum(p * w for p, w in ROULETTE) / sum(w for _, w in ROULETTE)
        self.assertLess(abs(ev - 3000), 400)

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
        from .profile import _streak

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
        from .profile import _streak

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
