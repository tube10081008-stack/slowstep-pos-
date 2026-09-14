"""
문자 발송 테스트.

발송은 **되돌릴 수 없다.** 잘못 나간 광고 문자는 회수가 안 되고, 미동의자에게
한 통 나가면 과태료 대상이다. 그래서 여기서는 '보내진다'보다 **'안 보내진다'**
쪽을 더 촘촘히 잰다.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from membership.models import Member, Store

from .models import Campaign, MessageLog, Segment
from .segments import render_message
from .sender import MessageClient, SendError, ad_window_open
from .services import CampaignError, send_campaign
from .solapi import SolapiClient, SolapiError, message_type

LIVE = dict(
    SOLAPI_API_KEY="key-abc",
    SOLAPI_API_SECRET="secret-xyz",
    SMS_SENDER_PHONE="02-123-4567",
    SMS_OPT_OUT_NUMBER="080-111-2222",
)


def make_member(store, phone, name, opt_in=True):
    return Member.objects.create(
        store=store, phone=phone, name=name, marketing_opt_in=opt_in
    )


class MessageTypeTests(TestCase):
    """길이를 잘못 재면 발송이 통째로 거부된다."""

    def test_short_korean_is_sms(self):
        self.assertEqual(message_type("안녕하세요 슬로우스텝입니다"), "SMS")

    def test_long_korean_is_lms(self):
        self.assertEqual(message_type("가" * 50), "LMS")

    def test_boundary_is_90_bytes(self):
        self.assertEqual(message_type("가" * 45), "SMS")      # 90바이트
        self.assertEqual(message_type("가" * 45 + "!"), "LMS")  # 91바이트


class AdWindowTests(TestCase):
    """광고성 야간 발송 금지 — 정보통신망법 제50조 제3항."""

    def _at(self, hour):
        base = timezone.localtime(timezone.now()).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        return ad_window_open(base)

    def test_daytime_open(self):
        for h in (8, 12, 20):
            self.assertTrue(self._at(h), f"{h}시는 열려 있어야 한다")

    def test_night_closed(self):
        for h in (21, 23, 0, 7):
            self.assertFalse(self._at(h), f"{h}시는 막혀야 한다")


class DecorateTests(TestCase):
    @override_settings(**LIVE)
    def test_ad_gets_prefix_and_opt_out(self):
        body = MessageClient().decorate("신메뉴 나왔어요", is_ad=True)
        self.assertTrue(body.startswith("(광고) "))
        self.assertIn("무료수신거부 080-111-2222", body)

    @override_settings(**LIVE)
    def test_info_message_untouched(self):
        body = MessageClient().decorate("쿠폰이 발행되었습니다", is_ad=False)
        self.assertEqual(body, "쿠폰이 발행되었습니다")


class SolapiClientTests(TestCase):
    @override_settings(**LIVE)
    def test_auth_header_signs_date_and_salt(self):
        import hashlib
        import hmac

        header = SolapiClient()._auth_header()
        self.assertTrue(header.startswith("HMAC-SHA256 apiKey=key-abc,"))
        parts = dict(
            p.strip().split("=", 1) for p in header[len("HMAC-SHA256 "):].split(",")
        )
        expected = hmac.new(
            b"secret-xyz", (parts["date"] + parts["salt"]).encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(parts["signature"], expected)

    @override_settings(**LIVE)
    def test_secret_never_appears_in_header(self):
        self.assertNotIn("secret-xyz", SolapiClient()._auth_header())

    @override_settings(**LIVE)
    def test_sender_digits_only(self):
        self.assertEqual(SolapiClient().sender, "021234567")

    @override_settings(SOLAPI_API_KEY="", SOLAPI_API_SECRET="", SMS_SENDER_PHONE="")
    def test_not_live_without_keys(self):
        self.assertFalse(SolapiClient().is_live)
        with self.assertRaises(SolapiError):
            SolapiClient().send_many([("01011112222", "안녕")])

    @override_settings(**LIVE)
    def test_only_failed_numbers_are_marked_failed(self):
        """솔라피는 실패한 건만 돌려준다 — 나머지는 성공으로 남아야 한다."""
        body = {
            "groupInfo": {"groupId": "G1"},
            "failedMessageList": [
                {"to": "01033334444", "statusCode": "3021",
                 "statusMessage": "잘못된 수신번호"}
            ],
        }
        with patch.object(SolapiClient, "_post", return_value=body):
            res = SolapiClient().send_many(
                [("01011112222", "가"), ("01033334444", "나")]
            )
        got = res.by_phone()
        self.assertTrue(got["01011112222"].success)
        self.assertFalse(got["01033334444"].success)
        self.assertIn("잘못된 수신번호", got["01033334444"].reason)
        self.assertEqual(res.group_id, "G1")

    @override_settings(**LIVE)
    def test_unexpected_response_does_not_mark_everything_failed(self):
        with patch.object(SolapiClient, "_post", return_value={}):
            res = SolapiClient().send_many([("01011112222", "가")])
        self.assertTrue(res.outcomes[0].success)

    @override_settings(**LIVE)
    def test_chunks_large_batches(self):
        from .solapi import CHUNK

        calls = []

        def fake(self, path, payload):
            calls.append(len(payload["messages"]))
            return {}

        items = [(f"0101111{i:04d}", "가") for i in range(CHUNK * 2 + 5)]
        with patch.object(SolapiClient, "_post", fake):
            res = SolapiClient().send_many(items)
        self.assertEqual(calls, [CHUNK, CHUNK, 5])
        self.assertEqual(len(res.outcomes), len(items))

    @override_settings(**LIVE)
    def test_timeout_warns_about_possible_double_send(self):
        """타임아웃은 '실패'가 아니라 '모른다' — 다시 누르면 두 번 갈 수 있다."""
        from urllib import error as urlerror

        with patch("marketing.solapi.request.urlopen",
                   side_effect=urlerror.URLError(TimeoutError())):
            with self.assertRaises(SolapiError) as ctx:
                SolapiClient().send_many([("01011112222", "가")])
        self.assertIn("이미 발송됐을 수", str(ctx.exception))

    @override_settings(**LIVE)
    def test_http_error_does_not_leak_secret(self):
        from urllib import error as urlerror

        err = urlerror.HTTPError(
            "u", 401, "Unauthorized", {}, io.BytesIO(b'{"errorMessage":"bad key"}')
        )
        with patch("marketing.solapi.request.urlopen", side_effect=err):
            with self.assertRaises(SolapiError) as ctx:
                SolapiClient().send_many([("01011112222", "가")])
        msg = str(ctx.exception)
        self.assertIn("401", msg)
        self.assertNotIn("secret-xyz", msg)

    @override_settings(**LIVE)
    def test_payload_shape(self):
        seen = {}

        def fake(self, path, payload):
            seen["path"] = path
            seen["payload"] = payload
            return {}

        with patch.object(SolapiClient, "_post", fake):
            SolapiClient().send_many([("010-1111-2222", "안녕하세요")])
        msg = seen["payload"]["messages"][0]
        self.assertEqual(msg["to"], "01011112222")      # 하이픈 제거
        self.assertEqual(msg["from"], "021234567")
        self.assertEqual(msg["type"], "SMS")
        self.assertIn("send-many", seen["path"])


class ComposeTests(TestCase):
    """문구 자동 생성 — 모델이 돌려준 문장을 그대로 믿지 않는다."""

    def setUp(self):
        from membership.tests import authenticate

        self.store = Store.objects.create(name="슬로우스텝")
        authenticate(self.client)

    def _post(self, **data):
        return self.client.post(
            "/api/v1/campaigns/compose",
            data={"purpose": "comeback", **data},
            content_type="application/json",
        )

    def test_falls_back_without_api_key(self):
        """AI가 없다고 기능이 멈추면 안 된다."""
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
            body = self._post().json()
        self.assertEqual(body["source"], "rule")
        self.assertTrue(body["text"])

    def test_uses_gemini_when_available(self):
        with patch("marketing.compose._gemini", return_value="{이름}님, 오랜만이에요."):
            body = self._post().json()
        self.assertEqual(body["source"], "gemini")
        self.assertEqual(body["text"], "{이름}님, 오랜만이에요.")

    def test_strips_ad_notice_the_system_adds(self):
        """모델이 (광고)·수신거부를 쓰면 발송 때 두 번 들어간다."""
        from marketing.compose import sanitize

        out = sanitize("(광고) 신메뉴 나왔어요\n\n무료수신거부 080-111-2222", 200)
        self.assertNotIn("(광고)", out)
        self.assertNotIn("무료수신거부", out)
        self.assertIn("신메뉴", out)

    def test_strips_invented_variables(self):
        """모르는 변수는 치환이 안 된 채 손님에게 그대로 간다."""
        from marketing.compose import sanitize

        out = sanitize("{성함}님 {이름}님 {잔액}P", 200)
        self.assertNotIn("{성함}", out)
        self.assertNotIn("{잔액}", out)
        self.assertIn("{이름}", out)

    def test_strips_phone_and_link(self):
        from marketing.compose import sanitize

        out = sanitize("문의 031-900-9231 https://example.com 오세요", 200)
        self.assertNotIn("031", out)
        self.assertNotIn("http", out)

    def test_respects_sms_budget_for_ads(self):
        """광고성은 (광고)+수신거부가 34바이트를 먼저 먹는다."""
        from marketing.compose import budget_for

        self.assertEqual(budget_for(is_ad=True, long_form=False), 56)
        self.assertEqual(budget_for(is_ad=False, long_form=False), 90)

    def test_long_output_is_trimmed(self):
        long_text = "안녕하세요 슬로우스텝입니다. " * 20
        with patch("marketing.compose._gemini", return_value=long_text):
            body = self._post(is_ad=True).json()
        self.assertLessEqual(body["bytes"], body["budget"])
        self.assertEqual(body["type"], "SMS")

    def test_trim_prefers_sentence_end(self):
        from marketing.compose import trim_to

        out = trim_to("첫 문장입니다. 둘째 문장입니다. 셋째 문장입니다.", 20)
        self.assertTrue(out.endswith("다."), out)

    def test_benefit_survives_tight_ad_budget(self):
        """사장님이 적어 넣은 혜택이 잘려나가면 문자를 보낼 이유가 없다."""
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
            body = self._post(
                purpose="coupon", benefit="아메리카노 1+1, 이번 주까지", is_ad=True
            ).json()
        self.assertIn("아메리카노 1+1", body["text"])
        self.assertIn("이번 주까지", body["text"])
        self.assertLessEqual(body["bytes"], body["budget"])

    def test_bad_purpose_rejected(self):
        self.assertEqual(self._post(purpose="아무거나").status_code, 400)

    def test_needs_staff_token(self):
        res = self.client_class().post(
            "/api/v1/campaigns/compose",
            data={"purpose": "comeback"}, content_type="application/json",
        )
        self.assertEqual(res.status_code, 403)


class RecipientPickTests(TestCase):
    """체크해서 고른 사람에게만 나가야 한다."""

    def setUp(self):
        from membership.tests import authenticate

        self.store = Store.objects.create(name="슬로우스텝")
        self.a = make_member(self.store, "01011112222", "가손님", True)
        self.b = make_member(self.store, "01033334444", "나손님", True)
        self.c = make_member(self.store, "01055556666", "다손님", True)
        self.seg = Segment.objects.create(name="전체", require_opt_in=False)
        authenticate(self.client)

    def test_sends_only_to_chosen(self):
        c = Campaign.objects.create(
            name="고른 사람만", segment=self.seg,
            message_template="{이름}님 안녕하세요", is_ad=False,
        )
        c.recipients.set([self.a, self.c])       # 나손님은 뺐다
        send_campaign(c)
        got = set(MessageLog.objects.values_list("member__name", flat=True))
        self.assertEqual(got, {"가손님", "다손님"})
        self.assertEqual(c.sent_count, 2)

    def test_chosen_wins_over_segment(self):
        """조건에 맞아도 체크를 풀었으면 안 간다."""
        c = Campaign.objects.create(
            name="교집합 아님", segment=self.seg,
            message_template="안녕", is_ad=False,
        )
        c.recipients.set([self.b])
        send_campaign(c)
        self.assertEqual(c.recipient_count, 1)
        self.assertEqual(
            list(MessageLog.objects.values_list("member__name", flat=True)), ["나손님"]
        )

    def test_opt_in_still_enforced_on_chosen(self):
        """골랐다고 미동의자에게 광고가 나가면 안 된다 — 과태료 대상이다."""
        no = make_member(self.store, "01077778888", "미동의", False)
        c = Campaign.objects.create(
            name="광고", segment=self.seg, message_template="신메뉴", is_ad=True,
        )
        c.recipients.set([self.a, no])
        with patch("marketing.services.ad_window_open", return_value=True):
            send_campaign(c)
        self.assertEqual(c.sent_count, 1)
        self.assertEqual(c.skipped_count, 1)
        self.assertEqual(
            MessageLog.objects.get(member=no).reason, "마케팅 수신 미동의"
        )

    def test_falls_back_to_segment_when_none_chosen(self):
        c = Campaign.objects.create(
            name="세그먼트", segment=self.seg, message_template="안녕", is_ad=False,
        )
        send_campaign(c)
        self.assertEqual(c.recipient_count, 3)

    def test_no_target_at_all_is_rejected(self):
        c = Campaign.objects.create(name="대상없음", message_template="안녕")
        with self.assertRaises(CampaignError) as ctx:
            send_campaign(c)
        self.assertIn("받을 대상이 없습니다", str(ctx.exception))

    def test_preview_lists_members_for_checkboxes(self):
        """앞 10명만 줘서는 체크해서 고를 수가 없다."""
        res = self.client.post(
            "/api/v1/segments/preview",
            data={"require_opt_in": False}, content_type="application/json",
        ).json()
        self.assertEqual(res["count"], 3)
        self.assertEqual(len(res["members"]), 3)
        row = res["members"][0]
        for key in ("id", "name", "phone", "tier_display",
                    "marketing_opt_in", "has_phone"):
            self.assertIn(key, row)

    def test_preview_flags_unreachable(self):
        make_member(self.store, "없음", "번호이상", True)
        res = self.client.post(
            "/api/v1/segments/preview",
            data={"require_opt_in": False}, content_type="application/json",
        ).json()
        bad = [m for m in res["members"] if m["name"] == "번호이상"][0]
        self.assertFalse(bad["has_phone"])

    def test_campaign_api_accepts_recipients(self):
        res = self.client.post(
            "/api/v1/campaigns",
            data={"name": "API", "segment": self.seg.id,
                  "recipients": [self.a.id, self.b.id],
                  "message_template": "안녕", "is_ad": False},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        c = Campaign.objects.get(pk=res.json()["id"])
        self.assertEqual(c.recipients.count(), 2)


class SmsStatusTests(TestCase):
    """'안 온다'의 원인을 갈라 주는 진단 — 문자를 보내지 않는다."""

    def setUp(self):
        from membership.tests import authenticate

        self.store = Store.objects.create(name="슬로우스텝")
        authenticate(self.client)

    def test_reports_missing_env(self):
        with override_settings(SOLAPI_API_KEY="", SOLAPI_API_SECRET="",
                               SMS_SENDER_PHONE=""):
            r = self.client.get("/api/v1/sms/status").json()
        self.assertFalse(r["live"])
        self.assertEqual(r["auth"], "미설정")
        self.assertIn("재배포", r["hint"])

    @override_settings(**LIVE)
    def test_auth_failure_points_at_the_key(self):
        with patch.object(SolapiClient, "balance",
                          side_effect=SolapiError("솔라피 응답 401: bad key", "auth")):
            res = self.client.get("/api/v1/sms/status")
        self.assertEqual(res.status_code, 502)
        body = res.json()
        self.assertEqual(body["auth"], "실패")
        self.assertIn("401", body["error"])
        self.assertIn("API Key", body["hint"])

    @override_settings(**LIVE)
    def test_network_failure_does_not_blame_the_key(self):
        """연결 실패를 키 문제로 안내하면 멀쩡한 키를 계속 다시 넣게 된다."""
        with patch.object(
            SolapiClient, "balance",
            side_effect=SolapiError("연결하지 못했습니다: 403", "network")
        ):
            body = self.client.get("/api/v1/sms/status").json()
        self.assertEqual(body["kind"], "network")
        self.assertIn("네트워크", body["hint"])
        self.assertNotIn("API Key", body["hint"])

    @override_settings(**LIVE)
    def test_zero_balance_is_called_out(self):
        with patch.object(SolapiClient, "balance", return_value={"point": 0}):
            body = self.client.get("/api/v1/sms/status").json()
        self.assertEqual(body["auth"], "성공")
        self.assertIn("잔액이 0", body["hint"])

    @override_settings(**LIVE)
    def test_healthy_shows_sender_to_compare(self):
        with patch.object(SolapiClient, "balance", return_value={"point": 5000}):
            body = self.client.get("/api/v1/sms/status").json()
        self.assertEqual(body["auth"], "성공")
        self.assertEqual(body["sender"], "021234567")
        self.assertIn("5000", body["hint"])

    @override_settings(**LIVE)
    def test_sends_no_message(self):
        """진단이 문자를 보내면 확인할 때마다 요금이 나간다."""
        with patch.object(SolapiClient, "balance", return_value={"point": 1}), \
             patch.object(SolapiClient, "_post") as post:
            self.client.get("/api/v1/sms/status")
        post.assert_not_called()

    def test_needs_staff_token(self):
        self.assertEqual(
            self.client_class().get("/api/v1/sms/status").status_code, 403
        )


class SolapiAuthFormatTests(TestCase):
    """서명 date 형식 — 한 글자 틀리면 401 이라 발송이 통째로 안 나간다."""

    @override_settings(**LIVE)
    def test_date_has_no_utc_offset(self):
        header = SolapiClient()._auth_header()
        date = dict(
            p.strip().split("=", 1) for p in header[len("HMAC-SHA256 "):].split(",")
        )["date"]
        self.assertNotIn("+00:00", date)     # 공식 SDK 는 오프셋을 안 붙인다
        self.assertNotIn("Z", date)
        datetime.fromisoformat(date)         # 그래도 ISO8601 이어야 한다

    @override_settings(**LIVE)
    def test_balance_is_a_get(self):
        seen = {}

        def fake(self, method, path, payload):
            seen.update(method=method, path=path, payload=payload)
            return {"point": 100}

        with patch.object(SolapiClient, "_request", fake):
            SolapiClient().balance()
        self.assertEqual(seen["method"], "GET")
        self.assertIsNone(seen["payload"])   # GET 에 본문을 실으면 거부된다
        self.assertIn("balance", seen["path"])


class SmsTestSendTests(TestCase):
    """테스트 발송 — 실전 첫 발송이 42명에게 나가기 전에 한 대로 확인."""

    def setUp(self):
        from membership.tests import authenticate

        self.store = Store.objects.create(name="슬로우스텝")
        authenticate(self.client)

    def _post(self, **data):
        return self.client.post(
            "/api/v1/sms/test", data=data, content_type="application/json"
        )

    def test_mock_when_no_keys(self):
        res = self._post(phone="010-1111-2222", text="테스트입니다")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["mocked"])
        self.assertFalse(res.json()["live"])

    @override_settings(**LIVE)
    def test_live_send(self):
        seen = {}

        def fake(self, path, payload):
            seen["payload"] = payload
            return {}

        with patch.object(SolapiClient, "_post", fake):
            res = self._post(phone="010-1111-2222", text="테스트입니다")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["mocked"])
        self.assertEqual(seen["payload"]["messages"][0]["to"], "01011112222")

    @override_settings(**LIVE)
    def test_never_marked_as_ad(self):
        """테스트는 항상 정보성 — (광고) 표기가 붙으면 안 된다."""
        seen = {}
        with patch.object(
            SolapiClient, "_post",
            lambda s, p, pay: seen.update(payload=pay) or {}
        ):
            self._post(phone="01011112222", text="신메뉴 안내")
        self.assertEqual(seen["payload"]["messages"][0]["text"], "신메뉴 안내")

    def test_leaves_no_campaign_log(self):
        """캠페인 통계가 테스트 발송으로 오염되면 안 된다."""
        self._post(phone="01011112222", text="테스트")
        self.assertEqual(MessageLog.objects.count(), 0)

    def test_bad_phone_rejected(self):
        self.assertEqual(self._post(phone="123", text="테스트").status_code, 400)

    def test_empty_text_rejected(self):
        self.assertEqual(self._post(phone="01011112222", text="  ").status_code, 400)

    @override_settings(**LIVE)
    def test_provider_failure_reported(self):
        with patch.object(
            SolapiClient, "_post", side_effect=SolapiError("인증 실패")
        ):
            res = self._post(phone="01011112222", text="테스트")
        self.assertEqual(res.status_code, 502)
        self.assertIn("인증 실패", res.json()["detail"])

    def test_needs_staff_token(self):
        res = self.client_class().post(
            "/api/v1/sms/test",
            data={"phone": "01011112222", "text": "테스트"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 403)


class CampaignSendTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="슬로우스텝")
        self.seg = Segment.objects.create(name="전체", require_opt_in=False)
        self.yes = make_member(self.store, "01011112222", "동의 손님", True)
        self.no = make_member(self.store, "01033334444", "미동의 손님", False)

    def _campaign(self, is_ad=True, template="{이름}님 안녕하세요"):
        return Campaign.objects.create(
            name="테스트", segment=self.seg, message_template=template, is_ad=is_ad
        )

    def test_mock_send_without_keys(self):
        """키가 없어도 흐름 전체가 돈다 — 실제 발송 날 처음 보는 화면이 없게."""
        c = self._campaign(is_ad=False)
        send_campaign(c)
        self.assertEqual(c.status, Campaign.Status.SENT)
        self.assertEqual(c.sent_count, 2)
        self.assertEqual(c.failed_count, 0)
        logs = MessageLog.objects.filter(campaign=c)
        self.assertEqual(logs.count(), 2)
        self.assertTrue(all(l.status == MessageLog.Status.SENT for l in logs))

    def test_ad_skips_non_consenting(self):
        c = self._campaign(is_ad=True)
        with patch("marketing.services.ad_window_open", return_value=True):
            send_campaign(c)
        self.assertEqual(c.sent_count, 1)
        self.assertEqual(c.skipped_count, 1)
        skipped = MessageLog.objects.get(campaign=c, status=MessageLog.Status.SKIPPED)
        self.assertEqual(skipped.member, self.no)
        self.assertEqual(skipped.reason, "마케팅 수신 미동의")

    def test_ad_blocked_at_night(self):
        c = self._campaign(is_ad=True)
        with patch("marketing.services.ad_window_open", return_value=False):
            with self.assertRaises(CampaignError) as ctx:
                send_campaign(c)
        self.assertIn("08시~21시", str(ctx.exception))
        self.assertEqual(c.status, Campaign.Status.DRAFT)     # 잠기지 않는다
        self.assertEqual(MessageLog.objects.count(), 0)

    def test_info_message_sends_at_night(self):
        """정보성은 시간 제한을 받지 않는다."""
        c = self._campaign(is_ad=False)
        with patch("marketing.services.ad_window_open", return_value=False):
            send_campaign(c)
        self.assertEqual(c.sent_count, 2)

    def test_missing_phone_skipped(self):
        broken = make_member(self.store, "없음", "번호 이상", True)
        c = self._campaign(is_ad=False)
        send_campaign(c)
        log = MessageLog.objects.get(campaign=c, member=broken)
        self.assertEqual(log.status, MessageLog.Status.SKIPPED)
        self.assertEqual(log.reason, "연락처 없음")
        self.assertEqual(c.sent_count, 2)

    def test_template_substitution(self):
        c = self._campaign(is_ad=False, template="{이름}님 {포인트}P 있어요")
        self.yes.points = 3500
        self.yes.save()
        send_campaign(c)
        log = MessageLog.objects.get(campaign=c, member=self.yes)
        self.assertEqual(log.rendered_message, "동의 손님님 3,500P 있어요")

    def test_ad_body_carries_notice(self):
        c = self._campaign(is_ad=True, template="신메뉴!")
        with patch("marketing.services.ad_window_open", return_value=True):
            send_campaign(c)
        log = MessageLog.objects.get(campaign=c, member=self.yes)
        self.assertTrue(log.rendered_message.startswith("(광고) 신메뉴!"))
        self.assertIn("무료수신거부", log.rendered_message)

    def test_cannot_send_twice(self):
        c = self._campaign(is_ad=False)
        send_campaign(c)
        with self.assertRaises(CampaignError):
            send_campaign(c)

    def test_needs_segment(self):
        c = Campaign.objects.create(name="세그먼트 없음", message_template="안녕")
        with self.assertRaises(CampaignError):
            send_campaign(c)

    @override_settings(**LIVE)
    def test_provider_failure_leaves_campaign_resendable(self):
        """대행사 요청이 통째로 실패하면 발송완료로 잠그면 안 된다."""
        c = self._campaign(is_ad=False)
        with patch.object(
            MessageClient, "send_many", side_effect=SendError("인증 실패")
        ):
            with self.assertRaises(CampaignError):
                send_campaign(c)
        c.refresh_from_db()
        self.assertEqual(c.status, Campaign.Status.DRAFT)
        self.assertEqual(c.failed_count, 2)
        self.assertEqual(
            MessageLog.objects.filter(status=MessageLog.Status.FAILED).count(), 2
        )

    @override_settings(**LIVE)
    def test_partial_failure_is_recorded_per_member(self):
        c = self._campaign(is_ad=False)
        body = {
            "failedMessageList": [
                {"to": "01033334444", "statusCode": "3021",
                 "statusMessage": "수신거부"}
            ]
        }
        with patch.object(SolapiClient, "_post", return_value=body):
            send_campaign(c)
        self.assertEqual(c.sent_count, 1)
        self.assertEqual(c.failed_count, 1)
        self.assertEqual(
            MessageLog.objects.get(campaign=c, member=self.no).status,
            MessageLog.Status.FAILED,
        )

    @override_settings(**LIVE)
    def test_one_request_for_many_members(self):
        """220명이 요청 220번이 되면 서버리스 시간 제한에 걸린다."""
        for i in range(30):
            make_member(self.store, f"0102222{i:04d}", f"손님{i}", True)
        c = self._campaign(is_ad=False)
        calls = []
        with patch.object(
            SolapiClient, "_post", lambda s, p, pay: calls.append(1) or {}
        ):
            send_campaign(c)
        self.assertEqual(len(calls), 1)
        self.assertEqual(c.sent_count, 32)
