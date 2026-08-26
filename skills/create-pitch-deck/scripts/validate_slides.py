#!/usr/bin/env python3
"""validate_slides.py — 生成された自己完結 HTML スライドの機械検証。

役割と境界:
- build_slides.py（または手直しした html）の出力が SKILL.md の契約
  （役割順序 / フラグメント送りの整合 / 各ステップでのはみ出しなし / 自己完結 /
  inline JS の安全性 / screen_flow のスポットライト連動 / 前提と解釈・承認事項の
  必須配置）を満たすかを、生成された .html ファイルそのものから検証する
  （spec を信頼しない。create-html-report/scripts/validate_report.py と同じ設計方針）。
- 全スライド・全フラグメントステップを実際にキーボード操作で遷移させながら検証し、
  各ステップの PNG（1440x900、`slide-<n>-step-<s>.png`）を確認用に撮影する。
- 自己完結の検証は静的解析（_SlideAuditor）に加え、Playwright 実行時に file:// と
  about: 以外の全リクエストを遮断・記録し、1件でもあれば FAIL にする（動的な抜け道の封鎖）。
- 検証のみを行い、ファイルの修正は行わない。

使い方:
    python3 validate_slides.py <deck.html> --screenshots-dir <dir>

終了コード: 全チェック PASS で 0、1 件でも FAIL で 1。
"""
from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import pathname2url

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "playwright が見つからない。venv へ `pip install playwright` の後 "
        "`playwright install chromium` を実行してから再実行すること。",
        file=sys.stderr,
    )
    sys.exit(1)

VIEWPORT = {"width": 1440, "height": 900}
OVERFLOW_TOLERANCE_PX = 1

FRONT_HALF_ROLES = ["cover", "premise", "problem", "solution", "scope", "winning"]
BACK_HALF_TAIL_ROLES = ["validation", "approval"]
SCREEN_FLOW_ROLE = "screen_flow"
SCREEN_FLOW_MIN = 2
SCREEN_FLOW_MAX = 4
APPROVAL_ITEM_MIN = 3
APPROVAL_ITEM_MAX = 5

# 自己完結・inline JS 安全性の検査は、HTML 全文への正規表現ではなく _SlideAuditor で
# 構造解析して行う。全文 regex ではエスケープ済みのスライド本文（例: 「fetch( を使う」
# という説明文の bullet）にまで禁止 JS パターンが誤マッチして FAIL する誤検知があるため、
# 禁止 JS パターンは <script> 要素の中身、inline handler は on* 属性、CSS 検査は
# <style>/style 属性へ検査対象を限定する
# （create-html-report/scripts/validate_report.py の構造解析と同じ設計方針）。

# data: URI は受動的な画像 MIME に限り許可（png/jpeg/gif/webp 以外の data: MIME は
# untrusted な能動コンテンツを埋め込める経路のため不許可
# — create-html-report/scripts/validate_report.py の DATA_URI_ALLOWED と同じ方針）。
DATA_URI_ALLOWED_RE = re.compile(r"^\s*data:image/(png|jpeg|gif|webp)[;,]", re.IGNORECASE)
# 任意 scheme の検出（http(s) に限定しない）。javascript: や blob: 等も含め、
# scheme 付きの参照はすべて外部扱いにする（data: のみ上の allowlist で先に判定）。
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
# 外部リソースを参照し得る属性の一覧（check_overflow.py の EXTERNAL_ATTR_RE と同等の
# 網羅）。object[data]・video/audio[poster]・form[action]・button[formaction]・
# SVG の xlink:href をカバーする。srcset は複数候補 URL 形式のため個別に処理する。
RESOURCE_ATTRS = frozenset({"src", "href", "xlink:href", "data", "poster", "action", "formaction"})
# srcset 候補の分割は「comma + 空白」に限定する（分割理由は srcset_candidates 参照）
SRCSET_SPLIT_RE = re.compile(r",\s+")
# 空白を伴わない comma の変則表記で紛れ込む外部 URL の fail-closed 検出用
SRCSET_SMUGGLED_URL_RE = re.compile(r"(?:^|[\s,])((?:https?:)?//[^\s,]+)", re.IGNORECASE)


def srcset_candidates(value: str) -> list[str]:
    """srcset 属性値から検査対象の URL 候補を取り出す。

    候補の分割を「comma + 空白」に限定するのは、data URI
    （data:image/png;base64,AA）の base64 区切り comma を候補区切りと誤認して
    後続を相対参照と誤検出しないため。空白を伴わない comma で外部 URL が
    紛れ込む変則表記は、値全体への protocol(-relative) URL 走査で検出する。
    """
    refs: list[str] = []
    for candidate in SRCSET_SPLIT_RE.split(value):
        parts = candidate.strip().split()
        if parts:
            refs.append(parts[0])
    for m in SRCSET_SMUGGLED_URL_RE.finditer(value):
        refs.append(m.group(1))
    return refs


def classify_ref(value: str) -> str:
    """リソース参照値を "ok" / "external" / "relative" / "bad-data" に分類する。

    pitch deck は単一 HTML 配布が契約（wireframe は srcdoc 埋め込み・画像は data URI）
    のため、許可は文書内 #fragment と許可画像 MIME の data: URI のみ。http(s) に限らず
    任意 scheme・protocol-relative（//）を外部扱いにし、ローカル相対参照も配布時に
    ファイルが欠落するため不許可にする（fail-closed）。
    """
    v = value.strip().strip("\"'")
    if not v or v.startswith("#"):
        return "ok"
    if v.lower().startswith("data:"):
        return "ok" if DATA_URI_ALLOWED_RE.match(v) else "bad-data"
    if v.startswith("//") or SCHEME_RE.match(v):
        return "external"
    return "relative"
# @import は参照先が相対 URL でも配布先でのファイル欠落の原因になるため出現自体を不許可。
CSS_IMPORT_RE = re.compile(r"@import", re.IGNORECASE)
# CSS の url(...) トークン抽出（引用付き/無引用を別分岐でパースする。引用付きは値中の
# `)` を含み得るため単一の文字クラスでは正しく抽出できない）。@import 以外にも
# @font-face の src や background 等の url(https://...) が外部依存の経路になる。
CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)""", re.IGNORECASE)
# コメントアウト済みの url(...) を実際の外部依存として誤検出しないための除去用。
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# inline JavaScript の逸脱チェック（AGENTS.md P0）。eval・new Function・
# innerHTML 代入・inline event handler 属性・network API の不使用を確認する。
INLINE_HANDLER_ATTR_RE = re.compile(r"^on[a-z]+$", re.IGNORECASE)
FORBIDDEN_JS_PATTERNS = [
    (re.compile(r"\beval\s*\("), "eval("),
    (re.compile(r"\bnew\s+Function\s*\("), "new Function("),
    (re.compile(r"\.innerHTML\s*="), ".innerHTML ="),
    (re.compile(r"\bfetch\s*\("), "fetch("),
    (re.compile(r"\bXMLHttpRequest\b"), "XMLHttpRequest"),
    (re.compile(r"\bWebSocket\s*\("), "WebSocket("),
    (re.compile(r"\bEventSource\s*\("), "EventSource("),
    (re.compile(r"\bsendBeacon\s*\("), "navigator.sendBeacon("),
]

NUMBERED_ITEM_RE = re.compile(r"^\s*\d+\.\s*\S")


class _SlideAuditor(HTMLParser):
    """HTML を構造解析し、検査対象（<script> 本文・<style>/style 属性・リソース属性・
    on* 属性・iframe srcdoc）を種類別に収集する収集器。

    判定は行わず収集のみを担う。判定は check_self_contained /
    check_inline_js_safety が収集結果に対して行う。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []          # inline <script> の中身
        self.styles: list[str] = []           # <style> の中身
        self.style_attrs: list[str] = []      # style="..." 属性値
        self.inline_handlers: list[str] = []  # on* 属性の出現箇所
        self.external_refs: list[str] = []    # 外部（任意 scheme・//）を指すリソース属性
        self.relative_refs: list[str] = []    # ローカル相対参照（単一ファイル配布で欠落）
        self.bad_data_uris: list[str] = []    # 許可 MIME 以外の data: URI 参照
        self.srcdocs: list[str] = []          # iframe srcdoc（wireframe 埋め込み）
        self._in: str | None = None
        self._buf = ""

    def _record_ref(self, tag: str, name: str, value: str) -> None:
        kind = classify_ref(value)
        where = f"<{tag} {name}>"
        if kind == "external":
            self.external_refs.append(where)
        elif kind == "relative":
            self.relative_refs.append(where)
        elif kind == "bad-data":
            self.bad_data_uris.append(where)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            v = value or ""
            if INLINE_HANDLER_ATTR_RE.match(name):
                self.inline_handlers.append(f"<{tag} {name}=...>")
            elif name in RESOURCE_ATTRS:
                self._record_ref(tag, name, v)
            elif name == "srcset":
                # srcset は「URL 幅記述子, URL 幅記述子, ...」形式のため
                # 候補ごとに URL 部分を取り出して個別に分類する
                for ref in srcset_candidates(v):
                    self._record_ref(tag, "srcset", ref)
            elif name == "style":
                self.style_attrs.append(v)
            elif name == "srcdoc":
                # HTMLParser は属性値を unescape 済みで渡すため、srcdoc の値は
                # 完全な HTML 文書として再帰解析できる（_audit 側で合流させる）
                self.srcdocs.append(v)
        if tag in ("script", "style"):
            self._in, self._buf = tag, ""

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # ブラウザは HTML の script/style で self-closing スラッシュ（<style/> 等）を
        # 無視し、実際の終了タグまで raw text として読み続ける。html.parser の既定
        # （starttag+endtag の即時クローズ扱い）のままだと `<style/>@import ...` や
        # `<script/>fetch(...)` の後続内容が収集から漏れて検査をバイパスできるため、
        # script/style に限り通常の開始タグとして収集モードに入る。他タグは既定挙動。
        if tag in ("script", "style"):
            self.handle_starttag(tag, attrs)
        else:
            super().handle_startendtag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._in:
            self._buf += data

    def handle_endtag(self, tag: str) -> None:
        if self._in == tag:
            (self.scripts if tag == "script" else self.styles).append(self._buf)
            self._in, self._buf = None, ""

    def close(self) -> None:
        super().close()
        # 閉じタグを欠いた `<script>fetch(...)` 等が検査を素通りしないよう、
        # EOF 時点で未終端の script/style も fail-closed で収集対象に含める
        if self._in:
            (self.scripts if self._in == "script" else self.styles).append(self._buf)
            self._in, self._buf = None, ""


def _audit(html_text: str) -> _SlideAuditor:
    auditor = _SlideAuditor()
    auditor.feed(html_text)
    auditor.close()
    # srcdoc（wireframe）は独立した HTML 文書なので再帰的に解析し、収集結果を合流させる
    # （Playwright での iframe contentDocument 検査に加えた静的検査の防御多層）
    pending = list(auditor.srcdocs)
    while pending:
        sub = _SlideAuditor()
        sub.feed(pending.pop())
        sub.close()
        for field in (
            "scripts", "styles", "style_attrs",
            "inline_handlers", "external_refs", "relative_refs", "bad_data_uris",
        ):
            getattr(auditor, field).extend(getattr(sub, field))
        pending.extend(sub.srcdocs)
    return auditor


def check_self_contained(html_text: str, failures: list[str], label: str = "outer HTML") -> None:
    auditor = _audit(html_text)
    if auditor.external_refs:
        failures.append(
            f"{label}: 外部 URL へのリソース属性参照"
            "（src / href / xlink:href / data / poster / action / formaction / srcset）"
            "を検出（自己完結契約違反）: " + ", ".join(sorted(set(auditor.external_refs)))
        )
    if auditor.relative_refs:
        failures.append(
            f"{label}: ローカル相対参照を検出: "
            + ", ".join(sorted(set(auditor.relative_refs)))
            + "（単一 HTML ファイル配布で参照先が欠落するため禁止。画像は data URI、"
            "wireframe は srcdoc で埋め込むこと）"
        )
    css_texts = [CSS_COMMENT_RE.sub("", t) for t in auditor.styles + auditor.style_attrs]
    if any(CSS_IMPORT_RE.search(t) for t in css_texts):
        failures.append(f"{label}: CSS @import による外部リソース参照を検出")
    bad_css: dict[str, list[str]] = {"external": [], "relative": [], "bad-data": []}
    for t in css_texts:
        for m in CSS_URL_RE.finditer(t):
            value = next(g for g in m.groups() if g is not None)
            kind = classify_ref(value)
            if kind != "ok":
                bad_css[kind].append(value)
    if bad_css["external"]:
        failures.append(
            f"{label}: CSS url() による外部リソース参照（@font-face / background 等）を検出: "
            + ", ".join(bad_css["external"][:3])
        )
    if bad_css["relative"]:
        failures.append(
            f"{label}: CSS url() によるローカル相対参照を検出: "
            + ", ".join(bad_css["relative"][:3])
            + "（単一 HTML ファイル配布で参照先が欠落するため禁止）"
        )
    if auditor.bad_data_uris or bad_css["bad-data"]:
        failures.append(f"{label}: png/jpeg/gif/webp 以外の data: URI（image/svg+xml 等）を検出")


def check_inline_js_safety(html_text: str, failures: list[str], label: str = "outer HTML") -> None:
    auditor = _audit(html_text)
    if auditor.inline_handlers:
        failures.append(
            f"{label}: inline event handler 属性（onclick= 等）を検出: "
            + ", ".join(sorted(set(auditor.inline_handlers)))
        )
    detected: list[str] = []
    for script in auditor.scripts:
        for pattern, name in FORBIDDEN_JS_PATTERNS:
            if name not in detected and pattern.search(script):
                detected.append(name)
    for name in detected:
        failures.append(f"{label}: <script> 内に禁止された JavaScript パターンを検出: {name}")


def check_roles(roles: list[str], failures: list[str]) -> None:
    prefix = roles[: len(FRONT_HALF_ROLES)]
    if prefix != FRONT_HALF_ROLES:
        failures.append(
            f"前半 {len(FRONT_HALF_ROLES)}枚の role 順序が契約と不一致（検出: {prefix}）"
        )
        return

    idx = len(FRONT_HALF_ROLES)
    n_flow = 0
    while idx < len(roles) and roles[idx] == SCREEN_FLOW_ROLE:
        n_flow += 1
        idx += 1
    if not (SCREEN_FLOW_MIN <= n_flow <= SCREEN_FLOW_MAX):
        failures.append(
            f"role={SCREEN_FLOW_ROLE} の連続枚数が{SCREEN_FLOW_MIN}〜{SCREEN_FLOW_MAX}枚"
            f"の範囲外（検出 {n_flow}枚）"
        )

    suffix = roles[idx:]
    if suffix != BACK_HALF_TAIL_ROLES:
        failures.append(
            f"role={SCREEN_FLOW_ROLE} の直後が role={BACK_HALF_TAIL_ROLES} の順で"
            f"終わっていない（検出: {suffix}）"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="自己完結 HTML スライドを機械検証する")
    parser.add_argument("html", type=Path)
    parser.add_argument(
        "--screenshots-dir", type=Path, default=None,
        help="各ステップの PNG（1440x900）を出力するディレクトリ。省略時は撮影しない",
    )
    args = parser.parse_args()

    if not args.html.is_file():
        print(f"FAIL: HTML が存在しない: {args.html}")
        return 1

    html_text = args.html.read_text(encoding="utf-8", errors="replace")
    failures: list[str] = []

    check_self_contained(html_text, failures)
    check_inline_js_safety(html_text, failures)

    url = "file://" + pathname2url(str(args.html.resolve()))
    # 静的検査をすり抜けた動的な外部要求（JS の Image().src 代入・CSS 解決由来等）を
    # 実行時に検出する。file:// と about: 以外はすべて abort するため、検査対象 HTML が
    # 外部へ実際に通信することはない（「通信した後で PASS する」抜け道の封鎖。
    # create-design-doc/scripts/check_overflow.py と同じ流儀）。
    blocked_requests: list[str] = []

    def _route_handler(route):
        req_url = route.request.url
        if req_url.startswith(("file://", "about:")):
            route.continue_()
        else:
            blocked_requests.append(req_url)
            route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT)
        context.route("**/*", _route_handler)
        page = context.new_page()
        page.goto(url, wait_until="networkidle")

        roles = page.eval_on_selector_all(".slide", "els => els.map(e => e.dataset.role)")
        total = len(roles)
        check_roles(roles, failures)

        if args.screenshots_dir:
            args.screenshots_dir.mkdir(parents=True, exist_ok=True)

        def check_active_overflow(label: str) -> None:
            dims = page.eval_on_selector(
                ".slide.active",
                "el => ({sh: el.scrollHeight, ch: el.clientHeight, sw: el.scrollWidth, cw: el.clientWidth})",
            )
            if dims["sw"] - dims["cw"] > OVERFLOW_TOLERANCE_PX:
                failures.append(
                    f"{label}: 横方向オーバーフロー (scrollWidth={dims['sw']} > clientWidth={dims['cw']})"
                )
            if dims["sh"] - dims["ch"] > OVERFLOW_TOLERANCE_PX:
                failures.append(
                    f"{label}: 縦方向オーバーフロー (scrollHeight={dims['sh']} > clientHeight={dims['ch']})"
                )

        def check_screen_flow_spotlight(slide_idx: int, role: str, step: int, label: str) -> None:
            if role != SCREEN_FLOW_ROLE:
                return
            has_iframe = page.eval_on_selector_all(".slide.active iframe", "els => els.length") > 0
            if not has_iframe:
                return  # wireframe が無い（テキストのみ）screen_flow はスポットライト対象外
            step_items = page.eval_on_selector_all(".slide.active .step-item", "els => els.length")
            if step_items == 0:
                return
            spot_count = page.eval_on_selector(
                ".slide.active iframe",
                "el => (el.contentDocument ? el.contentDocument.querySelectorAll('.__pitch_spotlight').length : -1)",
            )
            if spot_count == -1:
                failures.append(f"{label}: iframe.contentDocument にアクセスできない")
                return
            if step == 0:
                if spot_count != 0:
                    failures.append(f"{label}: step0（未着手）でスポットライトが残存している（{spot_count}件）")
            else:
                if spot_count != 1:
                    expected_sel = page.eval_on_selector_all(
                        ".slide.active .step-item", "els => els.map(e => e.dataset.selector)"
                    )
                    sel = expected_sel[step - 1] if step - 1 < len(expected_sel) else "?"
                    failures.append(
                        f"{label}: selector '{sel}' に対するスポットライトが1件でない"
                        f"（検出 {spot_count}件。セレクタが対象要素にマッチしていない可能性）"
                    )

        def check_iframe_self_contained(slide_idx: int, role: str, label: str) -> None:
            if role != SCREEN_FLOW_ROLE:
                return
            frame_count = page.eval_on_selector_all(".slide.active iframe", "els => els.length")
            if frame_count == 0:
                return
            outer = page.eval_on_selector(
                ".slide.active iframe",
                "el => (el.contentDocument ? el.contentDocument.documentElement.outerHTML : null)",
            )
            if outer is None:
                failures.append(f"{label}: iframe 内 HTML を取得できない")
                return
            check_self_contained(outer, failures, label=f"{label} の iframe 内 HTML")
            check_inline_js_safety(outer, failures, label=f"{label} の iframe 内 HTML（__pitchHighlight 除く既知パターン）")

        # 先頭スライドから → でフラグメント→スライドの順に全域を遷移し、
        # 各ステップでオーバーフロー・スポットライト整合を検証し PNG を撮影する。
        #
        # 注意: document.documentElement の scrollWidth/scrollHeight は使わない。
        # html/body に overflow:hidden を設定しているため、非表示スライドはもちろん、
        # 表示中スライドの内部コンテンツがビューポートを超えてもドキュメント全体としては
        # overflow:hidden で隠れてしまい scrollHeight がビューポートサイズのまま
        # 変化しない（実測で確認済み）。判定は「現在表示中の .slide 要素自身」の
        # scrollHeight/scrollWidth と clientHeight/clientWidth の比較で行う。
        cur = 0
        step = 0
        visited = 0
        max_iterations = total * 8 + 20  # フラグメント数を含めても十分な安全マージン
        while cur < total and visited < max_iterations:
            visited += 1
            role = roles[cur]
            label = f"slide {cur + 1}/{total} (role={role}) step={step}"
            check_active_overflow(label)
            check_screen_flow_spotlight(cur, role, step, label)
            check_iframe_self_contained(cur, role, label)
            if args.screenshots_dir:
                frag_count = page.eval_on_selector(".slide.active", "el => el.querySelectorAll('.fragment').length")
                name = f"slide-{cur + 1:02d}-{role}.png" if frag_count == 0 else f"slide-{cur + 1:02d}-step-{step}.png"
                page.screenshot(path=str(args.screenshots_dir / name))

            frag_count = page.eval_on_selector(".slide.active", "el => el.querySelectorAll('.fragment').length")
            if step < frag_count:
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(60)
                step += 1
            elif cur < total - 1:
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(60)
                cur += 1
                step = 0
            else:
                break

        if visited >= max_iterations:
            failures.append("走査が想定回数を超えた（フラグメント/スライド送りが終端しない可能性）")

        # 末尾を超えて ArrowRight しても role=approval に留まる（clamp）ことを確認
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(30)
        last_role = page.eval_on_selector(".slide.active", "el => el.dataset.role")
        if last_role != "approval":
            failures.append(
                f"末尾を超えて ArrowRight しても role=approval に留まらない（検出: {last_role}）"
            )

        # 'R' キーで先頭（cover・step0）へ戻ることを確認
        page.keyboard.press("r")
        page.wait_for_timeout(30)
        first_role = page.eval_on_selector(".slide.active", "el => el.dataset.role")
        if first_role != "cover":
            failures.append(f"'R' キーで先頭（role=cover）へ戻らない（検出: {first_role}）")

        # クリックでの次フラグメント/次スライド遷移を確認（cover は0フラグメントなので
        # 1クリックで2枚目 premise の step0 へ進むはず）
        page.click("#next-btn")
        page.wait_for_timeout(30)
        second_role = page.eval_on_selector(".slide.active", "el => el.dataset.role")
        if second_role != "premise":
            failures.append(
                f"next ボタンクリックで2枚目（role=premise）へ進まない（検出: {second_role}）"
            )

        # ← キーで戻る際、フラグメント0の状態からは前スライドの最終ステップへ戻ることを確認
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(30)
        back_role = page.eval_on_selector(".slide.active", "el => el.dataset.role")
        if back_role != "cover":
            failures.append(f"'←' で premise の step0 から cover へ戻らない（検出: {back_role}）")

        # premise（2枚目）に「前提」が含まれるか
        if "premise" not in roles:
            failures.append("role=premise のスライドが存在しない（前提と解釈の内容チェック不可）")
        else:
            premise_text = page.eval_on_selector(".slide[data-role='premise']", "el => el.innerText")
            if "前提" not in premise_text:
                failures.append("role=premise のスライドに「前提」という語が含まれない")

        # approval（最終）に「承認」と3〜5件の項目が含まれるか
        if "approval" not in roles:
            failures.append("role=approval のスライドが存在しない（承認事項の内容チェック不可）")
        else:
            approval_text = page.eval_on_selector(".slide[data-role='approval']", "el => el.innerText")
            if "承認" not in approval_text:
                failures.append("role=approval のスライドに「承認」という語が含まれない")
            approval_items = page.eval_on_selector_all(
                ".slide[data-role='approval'] .approval-item", "els => els.length"
            )
            if not (APPROVAL_ITEM_MIN <= approval_items <= APPROVAL_ITEM_MAX):
                failures.append(
                    f"role=approval の項目数が{APPROVAL_ITEM_MIN}〜{APPROVAL_ITEM_MAX}件の"
                    f"範囲外（検出 {approval_items}件）"
                )

        # print media: ナビゲーション要素が非表示、全フラグメントが表示状態になるか
        page.emulate_media(media="print")
        for selector in (".topbar", ".progress", ".navbtn"):
            display = page.eval_on_selector(selector, "el => getComputedStyle(el).display")
            if display != "none":
                failures.append(f"print media で {selector} が非表示になっていない（display={display}）")
        frag_opacities = page.eval_on_selector_all(
            ".fragment", "els => els.map(e => getComputedStyle(e).opacity)"
        )
        if any(op != "1" for op in frag_opacities):
            failures.append("print media で opacity!=1 のフラグメントが残っている（全ステップ表示の契約違反）")
        page.emulate_media(media="screen")

        browser.close()

    # 実行時に発生した外部リクエストは全件遮断済み。1件でもあれば自己完結契約違反
    for req_url in dict.fromkeys(blocked_requests):
        failures.append(f"実行時に外部リクエストを検出（遮断済み）: {req_url}")

    if failures:
        print(f"FAIL: {len(failures)}件の問題を検出（{total}枚）")
        for f in failures:
            print(f" - {f}")
        return 1

    print(f"PASS: {args.html} は全チェックを通過（{total}枚）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
