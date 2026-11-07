#!/usr/bin/env python3
"""公開２ページの補助監視。標準ライブラリのみ。初期設定では通信しない。"""

from __future__ import annotations

import json
import multiprocessing
import re
import socket
import ssl
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener


UTC = timezone.utc
OPENS_AT = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)  # 日本時間９時
STOPS_AT = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)  # 日本時間11月20日０時
TIMEOUT_SECONDS = 10
RETRY_SECONDS = 15
MAX_BODY_BYTES = 1024 * 1024
ENTRY_URL = "https://minotake-tas.github.io/minotake-soudan/"
REGISTRATION_URL = "https://sodankai-app.vercel.app/qr/5391e78a3ca2464abc357f5eca1f6dda"
TARGETS = ("entry", "registration")
URLS = {"entry": ENTRY_URL, "registration": REGISTRATION_URL}
LABELS = {"entry": "チラシの入口", "registration": "事前のお申し込み"}
OPENING_TEXT = "11月9日（月）午前9時から受付開始"
CONTACT_NAMES = frozenset({"c_kana", "c_name", "c_phone", "c_rest"})


class Code(Enum):
    OK = "正常"
    TIMEOUT = "10秒以内に読み込みを完了できませんでした"
    CONNECTION = "通信できませんでした"
    TLS = "暗号化通信を確認できませんでした"
    HTTP = "応答番号が正常ではありません"
    REDIRECT = "別のページへ移動する応答でした"
    MEDIA = "通常のページ形式ではありません"
    ENCODING = "ページの文字を読み取れませんでした"
    TOO_LARGE = "ページの読み込み上限を超えました"
    HEADING = "予定したページの見出しがありません"
    EVENT = "11月19日の会場案内を確認できません"
    ENTRY_LINK = "申込み先のボタンを確認できません"
    OPENING = "11月9日午前9時の受付開始案内を確認できません"
    EARLY_FORM = "受付開始前に入力欄が表示されています"
    FORM = "受付開始後の申込み欄を確認できません"
    LOGIN = "職員用ログインの入力欄が表示されています"
    PREFILL = "公開ページの連絡先欄に入力済みの値があります"
    INTERNAL = "監視処理を完了できませんでした"


@dataclass(frozen=True)
class Reply:
    status: int
    url: str
    content_type: str
    body: bytes
    content_encoding: str = "identity"


@dataclass(frozen=True)
class Result:
    code: Code
    status: int | None = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(target: str) -> Reply | Result:
    """送信先は固定。認証情報、クッキー、環境のプロキシを使わない。"""
    if target not in TARGETS:
        return Result(Code.INTERNAL)
    request = Request(URLS[target], method="GET", headers={
        "User-Agent": "Kurashi-Sodankai-PageCheck/1.0",
        "Accept": "text/html",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    })
    opener = build_opener(ProxyHandler({}), NoRedirect(),
                          HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                return Result(Code.TOO_LARGE, response.status)
            return Reply(response.status, response.geturl(),
                         response.headers.get("Content-Type", ""), body,
                         response.headers.get("Content-Encoding", "identity"))
    except HTTPError as error:
        status = error.code if type(error.code) is int and 100 <= error.code <= 599 else None
        error.close()
        return Result(Code.REDIRECT if status is not None and 300 <= status < 400 else Code.HTTP, status)
    except (TimeoutError, socket.timeout):
        return Result(Code.TIMEOUT)
    except ssl.SSLError:
        return Result(Code.TLS)
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return Result(Code.TIMEOUT)
        if isinstance(error.reason, ssl.SSLError):
            return Result(Code.TLS)
        return Result(Code.CONNECTION)
    except Exception:
        # 例外の文章にはURLや応答本文が含まれる場合がある。記録しない。
        return Result(Code.INTERNAL)


def _fetch_worker(target, channel):
    try:
        channel.send(_download(target))
    finally:
        channel.close()


def bounded_fetch(target: str, *, timeout: float = TIMEOUT_SECONDS,
                  worker: Callable = _fetch_worker) -> Reply | Result:
    """別プロセスで読み込み、DNS・応答本文の待ちも含め10秒で打ち切る。"""
    if target not in TARGETS:
        return Result(Code.INTERNAL)
    context = multiprocessing.get_context("spawn")
    incoming, outgoing = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(target, outgoing), daemon=True)
    started = False
    try:
        process.start()
        started = True
        outgoing.close()
        if not incoming.poll(timeout):
            return Result(Code.TIMEOUT)
        response = incoming.recv()
        if isinstance(response, (Reply, Result)):
            return response
        return Result(Code.INTERNAL)
    except Exception:
        return Result(Code.INTERNAL)
    finally:
        incoming.close()
        outgoing.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if not process.is_alive():
                process.close()


def normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


@dataclass
class Frame:
    tag: str
    hidden: bool
    disabled: bool
    attributes: dict
    parts: list[str] = field(default_factory=list)
    inputs: set[str] = field(default_factory=set)
    submits: list[str] = field(default_factory=list)


class PageLimit(Exception):
    """極端な入れ子や要素数は、本文を記録せず読み取りを止める。"""


class Page(HTMLParser):
    """HTMLを実行せずに読む。隠し欄とスクリプト内の文字を正常判定に使わない。"""

    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
    IGNORED = frozenset({"script", "style", "template", "noscript", "head"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.frames: list[Frame] = []
        self.text: list[str] = []
        self.headings: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.form_count = 0
        self.forms: list[Frame] = []
        self.visible_inputs = 0
        self.elements = 0
        self.form_inputs: set[str] = set()
        self.submit_buttons: list[str] = []
        self.password = False
        self.prefilled = False

    def handle_starttag(self, tag, attributes):
        self.elements += 1
        if len(self.frames) >= 128 or self.elements > 20000:
            raise PageLimit
        attrs = dict(attributes)
        style = re.sub(r"\s+", "", attrs.get("style", "") or "").lower()
        hidden = any(frame.hidden for frame in self.frames) or tag in self.IGNORED or "hidden" in attrs or (attrs.get("aria-hidden", "").lower() == "true") or "display:none" in style or "visibility:hidden" in style
        disabled = "disabled" in attrs or any(frame.disabled for frame in self.frames if frame.tag == "fieldset")
        frame = Frame(tag, hidden, disabled, attrs)
        in_form = any(parent.tag == "form" and not parent.hidden for parent in self.frames)
        if not hidden:
            if tag == "form":
                self.form_count += 1
                self.forms.append(frame)
            if tag == "input":
                kind = (attrs.get("type") or "text").lower()
                self.password |= kind == "password"
                if kind != "hidden" and not disabled:
                    self.visible_inputs += 1
                if in_form and kind != "hidden" and not disabled:
                    name = attrs.get("name", "")
                    self.form_inputs.add(name)
                    active_form = next(parent for parent in reversed(self.frames) if parent.tag == "form" and not parent.hidden)
                    active_form.inputs.add(name)
                    if name in CONTACT_NAMES and attrs.get("value"):
                        self.prefilled = True
        if tag not in self.VOID:
            self.frames.append(frame)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if any(frame.hidden for frame in self.frames):
            return
        self.text.append(data)
        for frame in self.frames:
            if frame.tag in {"h1", "a", "button", "form"}:
                frame.parts.append(data)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.frames) - 1, -1, -1) if self.frames[i].tag == tag), None)
        if index is None:
            return
        frames = self.frames[index:]
        active_form = next((parent for parent in reversed(self.frames[:index]) if parent.tag == "form" and not parent.hidden), None)
        in_form = active_form is not None
        del self.frames[index:]
        frame = frames[0]
        if frame.hidden:
            return
        text = normalize("".join(frame.parts))
        if tag == "h1":
            self.headings.append(text)
        elif tag == "a":
            self.links.append((frame.attributes.get("href", ""), text))
        elif tag == "button" and in_form and not frame.disabled and (frame.attributes.get("type") or "submit").lower() == "submit":
            self.submit_buttons.append(text)
            active_form.submits.append(text)


def check_page(target: str, response: Reply | Result, at: datetime) -> Result:
    if isinstance(response, Result):
        return response
    if target not in TARGETS:
        return Result(Code.INTERNAL)
    status = response.status
    if response.url != URLS[target] or 300 <= status < 400:
        return Result(Code.REDIRECT, status)
    if status != 200:
        return Result(Code.HTTP, status)
    if response.content_type.split(";", 1)[0].strip().lower() != "text/html" or response.content_encoding.lower() not in {"", "identity"}:
        return Result(Code.MEDIA, status)
    if len(response.body) > MAX_BODY_BYTES:
        return Result(Code.TOO_LARGE, status)
    try:
        page = Page()
        page.feed(response.body.decode("utf-8", errors="strict"))
        page.close()
    except PageLimit:
        return Result(Code.TOO_LARGE, status)
    except Exception:
        return Result(Code.ENCODING, status)
    if page.password:
        return Result(Code.LOGIN, status)
    if page.prefilled:
        return Result(Code.PREFILL, status)
    heading = "くらしのまるごと相談会" if target == "entry" else "相談会の事前登録"
    if normalize(heading) not in page.headings:
        return Result(Code.HEADING, status)
    text = normalize("".join(page.text))
    if not all(normalize(word) in text for word in ("くらしのまるごと相談会", "11月19日（木）", "四街道店", "臼井駅前店")) or any(word in text for word in ("動作確認用", "架空の相談会", "一般受付ではありません")):
        return Result(Code.EVENT, status)
    if target == "entry":
        if (REGISTRATION_URL, normalize("事前のお申し込みへ")) not in page.links:
            return Result(Code.ENTRY_LINK, status)
        if normalize(OPENING_TEXT) not in text:
            return Result(Code.OPENING, status)
    elif at < OPENS_AT:
        if page.form_count or page.visible_inputs:
            return Result(Code.EARLY_FORM, status)
        if normalize(OPENING_TEXT) not in text:
            return Result(Code.OPENING, status)
    elif not any({"c_kana", "c_phone"} <= form.inputs and normalize("相談したい専門職と時間を選んでください") in normalize("".join(form.parts)) and normalize("選んだ内容で申し込む") in form.submits for form in page.forms):
        return Result(Code.FORM, status)
    return Result(Code.OK, status)


def utc_now():
    return datetime.now(UTC)


def run(enabled: bool, *, clock: Callable = utc_now, fetch: Callable = bounded_fetch,
        sleep: Callable = time.sleep, output: Callable = print) -> int:
    if type(enabled) is not bool:
        output("設定を確認できません。公開ページへの通信は行っていません。")
        return 1
    if not enabled:
        output("補助監視は停止中です。公開ページへの通信は行っていません。")
        return 0
    if clock() >= STOPS_AT:
        output("補助監視の終了日時を過ぎています。公開ページへの通信は行っていません。")
        return 0
    failed = False
    for target in TARGETS:
        for attempt in (1, 2):
            at = clock()
            if at >= STOPS_AT:
                output("補助監視の終了日時になったため、残りの通信を中止しました。")
                return 0
            try:
                result = check_page(target, fetch(target), at)
            except Exception:
                result = Result(Code.INTERNAL)
            # 固定の対象名・定型の理由・時刻・応答番号だけを記録する。
            stamp = at.isoformat(timespec="seconds")
            status = f"／応答{result.status}" if type(result.status) is int and 100 <= result.status <= 599 else ""
            reason = result.code.value if isinstance(result.code, Code) else Code.INTERNAL.value
            output(f"{stamp}／{LABELS[target]}／確認{attempt}：{reason}{status}")
            if result.code == Code.OK:
                break
            if attempt == 2:
                failed = True
            elif clock() < STOPS_AT:
                sleep(RETRY_SECONDS)
    return 1 if failed else 0


def main() -> int:
    try:
        path = Path(__file__).with_name("public_page_monitor.config.json")
        config = json.loads(path.read_text(encoding="utf-8"))
        if type(config) is not dict or set(config) != {"enabled"} or type(config["enabled"]) is not bool:
            raise ValueError
    except Exception:
        print("設定を確認できません。公開ページへの通信は行っていません。")
        return 1
    return run(config["enabled"])


if __name__ == "__main__":
    raise SystemExit(main())
