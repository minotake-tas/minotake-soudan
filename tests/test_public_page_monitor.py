"""架空の応答だけで確認する。外部通信・予約・メール・鍵は使わない。"""

import io
import json
import multiprocessing
import socket
import ssl
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import public_page_monitor as m


BEFORE = datetime(2026, 11, 8, 23, 59, 59, tzinfo=timezone.utc)
OPEN = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
ENTRY = (ROOT / "index.html").read_text(encoding="utf-8")
HERO = '<h1>相談会の事前登録</h1><div><strong>くらしのまるごと相談会</strong><p>11月19日（木）</p><p>午前の部：みそら薬局 四街道店　午後の部：みそら薬局 臼井駅前店</p></div>'
PENDING = HERO + '<section><strong>11月9日（月）午前9時から受付開始</strong><p>開始日時になりましたら、このページからお申し込みいただけます。</p></section>'
FORM = '<form><fieldset><legend>相談したい専門職と時間を選んでください</legend><select name="specialist_slots"><option>時間を選ばずに申し込む</option></select></fieldset><label>ふりがな<input name="c_kana" value=""></label><label>お電話<input name="c_phone" value=""></label><button type="submit"><span>選んだ内容で申し込む</span></button></form>'
READY = HERO + FORM


def reply(target="registration", html=None, **changes):
    defaults = {"status": 200, "url": m.URLS[target], "content_type": "text/html; charset=utf-8", "body": (html if html is not None else READY).encode("utf-8")}
    defaults.update(changes)
    return m.Reply(**defaults)


def slow_worker(target, channel):
    time.sleep(60)


def fixture_worker(target, channel):
    channel.send(m.Result(m.Code.CONNECTION))
    channel.close()


def invalid_worker(target, channel):
    channel.send("架空の文章は記録しない")
    channel.close()


class NoNetworkTest(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, "create_connection", side_effect=AssertionError("手元の試験では外部通信禁止"))
        self.network.start()
        self.addCleanup(self.network.stop)


class PageTests(NoNetworkTest):
    def test_real_entry_before_opening(self):
        self.assertEqual(m.check_page("entry", reply("entry", ENTRY), BEFORE).code, m.Code.OK)

    def test_real_entry_after_opening(self):
        self.assertEqual(m.check_page("entry", reply("entry", ENTRY), OPEN).code, m.Code.OK)

    def test_before_opening_accepts_announcement(self):
        self.assertEqual(m.check_page("registration", reply(html=PENDING), BEFORE).code, m.Code.OK)

    def test_opening_exactly_requires_form(self):
        self.assertEqual(m.check_page("registration", reply(html=PENDING), OPEN).code, m.Code.FORM)

    def test_opening_exactly_accepts_form(self):
        self.assertEqual(m.check_page("registration", reply(), OPEN).code, m.Code.OK)

    def test_after_opening_accepts_form(self):
        self.assertEqual(m.check_page("registration", reply(), OPEN + timedelta(days=1)).code, m.Code.OK)

    def test_before_opening_rejects_early_form(self):
        self.assertEqual(m.check_page("registration", reply(html=PENDING + FORM), BEFORE).code, m.Code.EARLY_FORM)

    def test_wrong_opening_hour(self):
        self.assertEqual(m.check_page("registration", reply(html=PENDING.replace("午前9時", "午後9時")), BEFORE).code, m.Code.OPENING)

    def test_wrong_opening_day(self):
        self.assertEqual(m.check_page("registration", reply(html=PENDING.replace("11月9日", "11月10日")), BEFORE).code, m.Code.OPENING)

    def test_spaces_fullwidth_numbers_and_entities(self):
        html = PENDING.replace("11月9日（月）午前9時から受付開始", "１１月９日(月)\n 午前９時&nbsp;から受付開始")
        self.assertEqual(m.check_page("registration", reply(html=html), BEFORE).code, m.Code.OK)

    def test_http_failure_statuses(self):
        for status in (204, 400, 403, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertEqual(m.check_page("registration", reply(status=status), OPEN), m.Result(m.Code.HTTP, status))

    def test_redirect_statuses(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertEqual(m.check_page("registration", reply(status=status), OPEN).code, m.Code.REDIRECT)

    def test_wrong_origin_path_or_query(self):
        for url in ("http://sodankai-app.vercel.app/", "https://example.invalid/login", m.REGISTRATION_URL + "?next=secret", m.REGISTRATION_URL + "/", m.ENTRY_URL):
            with self.subTest(url=url):
                self.assertEqual(m.check_page("registration", reply(url=url), OPEN).code, m.Code.REDIRECT)

    def test_json_response_with_expected_words_is_not_page(self):
        self.assertEqual(m.check_page("registration", reply(content_type="application/json"), OPEN).code, m.Code.MEDIA)

    def test_missing_content_type(self):
        self.assertEqual(m.check_page("registration", reply(content_type=""), OPEN).code, m.Code.MEDIA)

    def test_compressed_body_is_not_misread(self):
        self.assertEqual(m.check_page("registration", reply(content_encoding="gzip"), OPEN).code, m.Code.MEDIA)

    def test_invalid_utf8(self):
        self.assertEqual(m.check_page("registration", reply(body=b"\xff\xfe"), OPEN).code, m.Code.ENCODING)

    def test_oversized_body(self):
        self.assertEqual(m.check_page("registration", reply(body=b"x" * (m.MAX_BODY_BYTES + 1)), OPEN).code, m.Code.TOO_LARGE)

    def test_empty_page(self):
        self.assertEqual(m.check_page("registration", reply(html=""), OPEN).code, m.Code.HEADING)

    def test_heading_only_in_title_does_not_pass(self):
        html = READY.replace("<h1>相談会の事前登録</h1>", "<head><title>相談会の事前登録</title></head>")
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.HEADING)

    def test_heading_only_in_script_does_not_pass(self):
        self.assertEqual(m.check_page("registration", reply(html='<script>' + READY + '</script>'), OPEN).code, m.Code.HEADING)

    def test_form_only_in_ignored_elements_does_not_pass(self):
        for tag in ("script", "style", "template", "noscript"):
            with self.subTest(tag=tag):
                self.assertEqual(m.check_page("registration", reply(html=HERO + f'<{tag}>' + FORM + f'</{tag}>'), OPEN).code, m.Code.FORM)

    def test_form_only_in_comment_does_not_pass(self):
        self.assertEqual(m.check_page("registration", reply(html=HERO + '<!--' + FORM + '-->'), OPEN).code, m.Code.FORM)

    def test_hidden_form_does_not_pass(self):
        for attribute in ('hidden', 'aria-hidden="true"', 'style="display: none"', 'style="visibility: hidden"'):
            with self.subTest(attribute=attribute):
                self.assertEqual(m.check_page("registration", reply(html=HERO + f'<div {attribute}>' + FORM + '</div>'), OPEN).code, m.Code.FORM)

    def test_hidden_form_does_not_count_as_early(self):
        self.assertEqual(m.check_page("registration", reply(html=PENDING + '<template>' + FORM + '</template>'), BEFORE).code, m.Code.OK)

    def test_closed_announcement_in_script_does_not_pass(self):
        html = HERO + '<script>11月9日（月）午前9時から受付開始</script>'
        self.assertEqual(m.check_page("registration", reply(html=html), BEFORE).code, m.Code.OPENING)

    def test_wrong_event_or_venue(self):
        for before, after in (("11月19日", "11月7日"), ("四街道店", "別店舗"), ("臼井駅前店", "別店舗")):
            with self.subTest(before=before):
                self.assertEqual(m.check_page("registration", reply(html=READY.replace(before, after)), OPEN).code, m.Code.EVENT)

    def test_fictional_event_is_never_healthy(self):
        self.assertEqual(m.check_page("registration", reply(html=READY + '<p>動作確認用・架空の相談会</p>'), OPEN).code, m.Code.EVENT)

    def test_no_phone_input(self):
        self.assertEqual(m.check_page("registration", reply(html=READY.replace('name="c_phone"', 'name="other"')), OPEN).code, m.Code.FORM)

    def test_hidden_inputs_do_not_replace_real_inputs(self):
        html = READY.replace('<input name="c_phone"', '<input type="hidden" name="c_phone"')
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.FORM)

    def test_disabled_inputs_do_not_pass(self):
        html = READY.replace('<input name="c_phone"', '<input disabled name="c_phone"')
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.FORM)

    def test_inputs_outside_form_do_not_pass(self):
        html = READY.replace('<form>', '').replace('</form>', '')
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.FORM)

    def test_disabled_fieldset_does_not_pass(self):
        html = HERO + '<form><fieldset disabled>' + FORM.replace('<form>', '').replace('</form>', '') + '</fieldset></form>'
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.FORM)

    def test_missing_submit_does_not_pass(self):
        self.assertEqual(m.check_page("registration", reply(html=READY.replace('type="submit"', 'type="button"')), OPEN).code, m.Code.FORM)

    def test_disabled_submit_does_not_pass(self):
        self.assertEqual(m.check_page("registration", reply(html=READY.replace('type="submit"', 'type="submit" disabled')), OPEN).code, m.Code.FORM)

    def test_hidden_submit_does_not_pass(self):
        self.assertEqual(m.check_page("registration", reply(html=READY.replace('<button ', '<button hidden ')), OPEN).code, m.Code.FORM)

    def test_password_input_rejected(self):
        self.assertEqual(m.check_page("registration", reply(html=READY + '<input type="password">'), OPEN).code, m.Code.LOGIN)

    def test_prefilled_contact_rejected(self):
        html = READY.replace('name="c_kana" value=""', 'name="c_kana" value="かくうのしめい"')
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.PREFILL)

    def test_placeholders_are_not_contact_values(self):
        html = READY.replace('name="c_phone"', 'placeholder="架空の記入例" name="c_phone"')
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.OK)

    def test_wrong_entry_destination(self):
        html = ENTRY.replace(m.REGISTRATION_URL, "https://example.invalid/")
        self.assertEqual(m.check_page("entry", reply("entry", html), OPEN).code, m.Code.ENTRY_LINK)

    def test_hidden_entry_button(self):
        html = ENTRY.replace('<a class="registration-link"', '<a hidden class="registration-link"')
        self.assertEqual(m.check_page("entry", reply("entry", html), OPEN).code, m.Code.ENTRY_LINK)

    def test_missing_entry_announcement(self):
        html = ENTRY.replace('11月9日（月）午前9時', '受付準備中')
        self.assertEqual(m.check_page("entry", reply("entry", html), OPEN).code, m.Code.OPENING)


    def test_split_forms_do_not_combine_into_one_healthy_form(self):
        html = HERO + '<form><legend>相談したい専門職と時間を選んでください</legend><input name="c_kana"><button type="submit">選んだ内容で申し込む</button></form><form><input name="c_phone"></form>'
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.FORM)

    def test_button_in_another_form_does_not_pass(self):
        html = HERO + '<form><legend>相談したい専門職と時間を選んでください</legend><input name="c_kana"><input name="c_phone"></form><form><button type="submit">選んだ内容で申し込む</button></form>'
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.FORM)

    def test_legend_outside_form_does_not_pass(self):
        html = READY.replace('<legend>相談したい専門職と時間を選んでください</legend>', '') + '<p>相談したい専門職と時間を選んでください</p>'
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.FORM)

    def test_visible_input_outside_form_is_early(self):
        self.assertEqual(m.check_page("registration", reply(html=PENDING + '<input name="c_phone">'), BEFORE).code, m.Code.EARLY_FORM)

    def test_excessive_depth_is_bounded(self):
        html = READY + '<div>' * 130 + '架空の文'
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.TOO_LARGE)

    def test_excessive_elements_are_bounded(self):
        html = READY + '<br>' * 20001
        self.assertEqual(m.check_page("registration", reply(html=html), OPEN).code, m.Code.TOO_LARGE)


class Clock:
    def __init__(self, at):
        self.at = at
        self.waits = []

    def __call__(self):
        return self.at

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.at += timedelta(seconds=seconds)


class RunTests(NoNetworkTest):
    def invoke(self, enabled=True, at=OPEN, responses=None, fetch=None):
        clock = Clock(at)
        if fetch is None:
            fetch = Mock(side_effect=responses if responses is not None else [reply("entry", ENTRY), reply()])
        lines = []
        result = m.run(enabled, clock=clock, fetch=fetch, sleep=clock.sleep, output=lines.append)
        return result, fetch, clock, lines

    def test_stopped_setting_causes_zero_requests(self):
        code, fetch, clock, lines = self.invoke(enabled=False)
        self.assertEqual(code, 0)
        fetch.assert_not_called()
        self.assertEqual(clock.waits, [])
        self.assertIn("停止中", lines[0])

    def test_non_boolean_setting_causes_zero_requests(self):
        for value in ("false", "true", 1, 0, None):
            with self.subTest(value=value):
                code, fetch, clock, lines = self.invoke(enabled=value)
                self.assertEqual(code, 1)
                fetch.assert_not_called()

    def test_before_opening_checks_only_fixed_two_targets(self):
        code, fetch, clock, lines = self.invoke(at=BEFORE, responses=[reply("entry", ENTRY), reply(html=PENDING)])
        self.assertEqual(code, 0)
        self.assertEqual([call.args for call in fetch.call_args_list], [("entry",), ("registration",)])
        self.assertEqual(clock.waits, [])

    def test_opening_checks_only_fixed_two_targets(self):
        code, fetch, clock, lines = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)

    def test_exact_end_has_no_requests(self):
        code, fetch, clock, lines = self.invoke(at=END)
        self.assertEqual(code, 0)
        fetch.assert_not_called()

    def test_after_end_has_no_requests(self):
        code, fetch, clock, lines = self.invoke(at=END + timedelta(days=90))
        self.assertEqual(code, 0)
        fetch.assert_not_called()

    def test_one_second_before_end_still_checks(self):
        code, fetch, clock, lines = self.invoke(at=END - timedelta(seconds=1))
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)

    def test_retry_only_failed_target(self):
        code, fetch, clock, lines = self.invoke(responses=[reply("entry", ENTRY), m.Result(m.Code.TIMEOUT), reply()])
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["entry", "registration", "registration"])
        self.assertEqual(clock.waits, [15])

    def test_failure_after_one_retry_is_nonzero(self):
        code, fetch, clock, lines = self.invoke(responses=[reply("entry", ENTRY), m.Result(m.Code.HTTP, 503), m.Result(m.Code.HTTP, 503)])
        self.assertEqual(code, 1)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(clock.waits, [15])

    def test_both_failures_are_checked_and_bounded(self):
        code, fetch, clock, lines = self.invoke(responses=[m.Result(m.Code.CONNECTION)] * 4)
        self.assertEqual(code, 1)
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(clock.waits, [15, 15])

    def test_retry_uses_new_phase_at_opening(self):
        code, fetch, clock, lines = self.invoke(at=OPEN - timedelta(seconds=5), responses=[reply("entry", ENTRY), reply(), reply()])
        self.assertEqual(code, 0)
        self.assertEqual(clock.waits, [15])
        self.assertIn("受付開始前", lines[1])

    def test_end_reached_while_waiting_stops_retry(self):
        code, fetch, clock, lines = self.invoke(at=END - timedelta(seconds=1), responses=[m.Result(m.Code.TIMEOUT)])
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("中止", lines[-1])

    def test_end_reached_during_request_stops_next_target(self):
        clock = Clock(END - timedelta(seconds=1))
        fetch = Mock(side_effect=lambda target: (setattr(clock, "at", END), reply(target, ENTRY))[1])
        code = m.run(True, clock=clock, fetch=fetch, sleep=clock.sleep, output=lambda line: None)
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 1)

    def test_exception_text_is_not_logged(self):
        sensitive_fixture = "架空の氏名・架空の電話・鍵ではない試験文字列"
        code, fetch, clock, lines = self.invoke(fetch=Mock(side_effect=RuntimeError(sensitive_fixture)))
        self.assertEqual(code, 1)
        self.assertNotIn(sensitive_fixture, "".join(lines))
        self.assertNotIn("RuntimeError", "".join(lines))

    def test_response_body_and_unknown_links_are_not_logged_or_followed(self):
        body = READY.replace('name="c_phone" value=""', 'name="c_phone" value="架空の電話値"') + '<a href="https://example.invalid/private">架空の本文</a>'
        code, fetch, clock, lines = self.invoke(responses=[reply("entry", ENTRY), reply(html=body), reply(html=body)])
        self.assertEqual(code, 1)
        self.assertEqual(fetch.call_count, 3)
        self.assertNotIn("架空の電話値", "".join(lines))
        self.assertNotIn("example.invalid", "".join(lines))
        self.assertNotIn("架空の本文", "".join(lines))


class TransportTests(NoNetworkTest):
    def test_unknown_target_has_no_network(self):
        with patch.object(m, "build_opener") as build:
            self.assertEqual(m._download("unlisted").code, m.Code.INTERNAL)
            build.assert_not_called()

    def test_only_get_without_authentication_or_cookie(self):
        fake_response = Mock()
        fake_response.status = 200
        fake_response.geturl.return_value = m.ENTRY_URL
        fake_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        fake_response.read.return_value = ENTRY.encode("utf-8")
        fake_open = Mock()
        fake_open.open.return_value.__enter__ = Mock(return_value=fake_response)
        fake_open.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(m, "build_opener", return_value=fake_open) as build:
            result = m._download("entry")
        self.assertIsInstance(result, m.Reply)
        request = fake_open.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.full_url, m.ENTRY_URL)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertEqual(fake_open.open.call_args.kwargs["timeout"], 10)
        fake_response.read.assert_called_once_with(m.MAX_BODY_BYTES + 1)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], m.NoRedirect)

    def test_redirect_handler_never_follows_location(self):
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid/"))

    def test_network_exceptions_have_safe_codes(self):
        for error, expected in ((TimeoutError("架空の本文"), m.Code.TIMEOUT), (ssl.SSLError("架空の本文"), m.Code.TLS), (URLError(socket.timeout()), m.Code.TIMEOUT), (URLError(ssl.SSLError()), m.Code.TLS), (URLError("架空の本文"), m.Code.CONNECTION), (RuntimeError("架空の本文"), m.Code.INTERNAL)):
            with self.subTest(expected=expected):
                fake_open = Mock()
                fake_open.open.side_effect = error
                with patch.object(m, "build_opener", return_value=fake_open):
                    self.assertEqual(m._download("entry").code, expected)

    def test_http_error_does_not_read_body_or_follow_location(self):
        for status, expected in ((302, m.Code.REDIRECT), (403, m.Code.HTTP), (503, m.Code.HTTP)):
            with self.subTest(status=status):
                body = Mock()
                error = HTTPError(m.ENTRY_URL, status, "架空の本文", {"Location": "https://example.invalid/"}, body)
                fake_open = Mock()
                fake_open.open.side_effect = error
                with patch.object(m, "build_opener", return_value=fake_open):
                    self.assertEqual(m._download("entry"), m.Result(expected, status))
                body.read.assert_not_called()

    def test_hard_timeout_terminates_worker_without_network(self):
        before_children = {p.pid for p in multiprocessing.active_children()}
        start = time.monotonic()
        result = m.bounded_fetch("entry", timeout=0.2, worker=slow_worker)
        elapsed = time.monotonic() - start
        self.assertEqual(result.code, m.Code.TIMEOUT)
        self.assertLess(elapsed, 4)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before_children)

    def test_child_can_return_safe_result(self):
        self.assertEqual(m.bounded_fetch("entry", timeout=3, worker=fixture_worker).code, m.Code.CONNECTION)

    def test_child_unexpected_output_is_rejected(self):
        self.assertEqual(m.bounded_fetch("entry", timeout=3, worker=invalid_worker).code, m.Code.INTERNAL)


class ConfigurationTests(NoNetworkTest):
    def test_delivered_config_is_off(self):
        self.assertEqual(json.loads((ROOT / "tools/public_page_monitor.config.json").read_text()), {"enabled": False})

    def test_off_main_never_calls_transport(self):
        stream = io.StringIO()
        with patch.object(m, "bounded_fetch", side_effect=AssertionError("外部通信禁止")), redirect_stdout(stream):
            self.assertEqual(m.main(), 0)
        self.assertIn("停止中", stream.getvalue())

    def test_invalid_config_fails_without_reading_external_data(self):
        for content in ('{"enabled":"false"}', '{"enabled":true,"url":"https://example.invalid"}', 'null', 'broken'):
            with self.subTest(content=content):
                with patch.object(m.Path, "read_text", return_value=content), patch.object(m, "run") as run, redirect_stdout(io.StringIO()):
                    self.assertEqual(m.main(), 1)
                    run.assert_not_called()

    def test_missing_config_fails_without_network(self):
        with patch.object(m.Path, "read_text", side_effect=OSError("架空の情報")), patch.object(m, "run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(), 1)
            run.assert_not_called()

    def test_workflow_has_no_tokens_write_permissions_paid_actions_or_artifacts(self):
        text = (ROOT / ".github/workflows/public-page-monitor.yml").read_text()
        self.assertIn("permissions: {}", text)
        self.assertIn("runs-on: ubuntu-24.04", text)
        self.assertNotIn("uses:", text)
        for forbidden in ("secrets.", "GITHUB_TOKEN", "pull_request_target", "write-all", "upload-artifact", "cache@", "curl ", "wget "):
            self.assertNotIn(forbidden, text)

    def test_workflow_is_scoped_to_main_and_expected_repo(self):
        text = (ROOT / ".github/workflows/public-page-monitor.yml").read_text()
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("github.repository == 'minotake-tas/minotake-soudan'", text)
        self.assertIn("github.event_name == 'schedule'", text)
        self.assertIn("steps.deadline.outputs.active == 'true'", text)
        self.assertIn("2026-11-19T15:00:00+00:00", text)
        self.assertIn("7,22,37,52 * * * *", text)
        self.assertEqual(m.STOPS_AT, END)
        self.assertEqual(m.OPENS_AT, OPEN)


if __name__ == "__main__":
    unittest.main()
