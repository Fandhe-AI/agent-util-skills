#!/usr/bin/env python3
"""check_overflow.py — 自己完結 HTML ワイヤーフレームのレイアウト崩れを機械検証する。

役割と境界:
- 指定 viewport（既定: PC 1440px・モバイル 375px）で HTML をロードし、
  横スクロールを誘発する水平方向のオーバーフローが無いかを検証する。
- 外部 CDN・外部 script 等の self-contained 契約違反も検出する
  （create-html-report/scripts/validate_report.py の外部依存チェックと同じ考え方）。
  検出は静的検査（属性・srcset・CSS url()/@import・JS ネットワーク API・禁止 JS
  パターン・inline event handler）と、
  Playwright 実行時の全ネットワーク遮断（file:// の自ファイル以外は abort して記録）の
  両輪で行う。静的検査をすり抜けた動的な外部要求も実行時遮断で FAIL になり、
  かつ abort により外部への通信自体が発生しない。
- レイアウトの見た目の良し悪しは判定しない（機械的に判定できる overflow のみ）。

依存: playwright（Chromium）。
"""
from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import pathname2url, url2pathname

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "playwright が見つからない。venv へ `pip install playwright` の後 "
        "`playwright install chromium` を実行してから再実行すること。",
        file=sys.stderr,
    )
    sys.exit(1)

DEFAULT_VIEWPORTS = [("desktop", 1440, 900), ("mobile", 375, 812)]
OVERFLOW_TOLERANCE_PX = 1  # サブピクセル丸め誤差の許容量

# コメント内の URL 例示（テンプレート冒頭の説明文等）を誤検出しないよう、
# 検査前に HTML コメントと CSS/JS ブロックコメントを除去する
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# リソースを参照し得る属性の値を抽出する（引用符あり・なしの双方）。
# object[data]・SVG の image/use（href / xlink:href）もここでカバーする。
# 外部 URL だけでなく相対パス・絶対パス・file:// も classify_ref で判定するため、
# 値そのものをキャプチャする
REF_ATTR_RE = re.compile(
    r"""\b(src|href|xlink:href|data|poster|action|formaction)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)
# 単一ファイル配布でも欠落しない data URI の許可 MIME。raster 画像のみに制限する。
# image/svg+xml は許可しない: <object data="data:image/svg+xml,...">・<embed> 経由で
# script 入り SVG（onload= 等）が Chromium 上で実行され得るため。ベクター図形は
# inline <svg> 要素で書く（属性・script 検査の対象になる）。フォントはシステム
# フォントスタックを使う契約のため data URI 埋め込み自体を不要とする
ALLOWED_DATA_URI_MIME = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}
# srcset は「URL 幅記述子, URL 幅記述子, ...」形式のため属性値全体を取り出して候補ごとに検査する
SRCSET_RE = re.compile(r"""\bsrcset\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
# srcset 候補の分割は「comma + 空白」に限定する（分割理由は srcset_candidates 参照。
# create-pitch-deck/scripts/validate_slides.py の同名関数と同じ解析方式）
SRCSET_SPLIT_RE = re.compile(r",\s+")
# 空白を伴わない comma の変則表記で紛れ込む外部 URL の fail-closed 検出用
SRCSET_SMUGGLED_URL_RE = re.compile(r"(?:^|[\s,])((?:https?:)?//[^\s,]+)", re.IGNORECASE)
# CSS の url(...) 全般（@font-face の src・background-image 等）。引用符の有無を問わない
CSS_URL_RE = re.compile(r"""\burl\(\s*(["']?)([^"')]+)\1\s*\)""", re.IGNORECASE)
# @import は外部・ローカルを問わずスタイルの分割自体が単一ファイル契約に反するため、
# url(...) 形式・文字列直接指定の双方で存在自体を検出する
CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\(|["'])""", re.IGNORECASE)
# 自己完結ワイヤーフレームに通信 API は不要のため、script 内での存在自体を契約違反とする。
# 可視テキストや属性に「fetch( を使う」等の説明コピーが現れても誤検知しないよう、
# 検査は _WireframeAuditor が構造的に収集した <script> 本文に限定する
# （script 外の動的な抜け道は実行時の全ネットワーク遮断で検出する）
JS_NETWORK_RE = re.compile(
    r"\b(?:fetch\s*\(|XMLHttpRequest\b|WebSocket\s*\(|sendBeacon\s*\(|EventSource\s*\(|importScripts\s*\()"
)
# 禁止 JavaScript 識別子（create-pitch-deck/scripts/validate_slides.py の禁止 JS 検査と
# 同等以上）。撮影時に Chromium 上で実行されるため、動的コード実行・HTML 注入系 API は
# 存在自体を契約違反とする。\b 付き識別子マッチのため、`eval(` の直接呼び出しに加えて
# `window['eval']` / `window["eval"]` のようなブラケット表記（文字列としての出現）も
# 同じパターンで検出でき、fail-closed になる（例: "retrieval" 等の内包語には
# 単語境界が成立せずマッチしない）
BANNED_JS_PATTERNS = [
    (re.compile(r"\beval\b"), "eval"),
    (re.compile(r"\bFunction\b"), "Function コンストラクタ（new Function 等）"),
    (re.compile(r"\binnerHTML\b"), "innerHTML"),
    (re.compile(r"\bouterHTML\b"), "outerHTML"),
    (re.compile(r"\binsertAdjacentHTML\b"), "insertAdjacentHTML"),
    (re.compile(r"\bdocument\s*\.\s*write(?:ln)?\b|['\"]write(?:ln)?['\"]"), "document.write / writeln"),
]


class _WireframeAuditor(HTMLParser):
    """on* 属性と <script> 本文を構造的に収集する監査パーサ。

    正規表現による全文検索では可視テキスト中の説明コピーへ誤マッチするため、
    HTMLParser で構造を解釈して「タグの属性として現れた on*」と「script 要素の本文」
    だけを検査対象として取り出す。ブラウザは <script/> のような自己閉じ表記も開始
    タグとして扱い実終了タグまで script と解釈するため、startendtag も開始扱いにする。
    閉じタグ欠落時は fail-closed で EOF までを script 本文として収集する。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_handlers: list[str] = []  # "<tag> の <on*属性>" 形式で記録
        self.script_bodies: list[str] = []
        self._in_script = False
        self._buf: list[str] = []

    def _visit_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, _value in attrs:
            if name and name.lower().startswith("on"):
                self.inline_handlers.append(f"<{tag}> の {name}")
        if tag.lower() == "script":
            self._in_script = True
            self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._visit_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._visit_tag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self.script_bodies.append("".join(self._buf))
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        if self._in_script:
            self.script_bodies.append("".join(self._buf))
            self._in_script = False


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


def classify_ref(target: str, base_dir: Path, allow_local_refs: bool) -> str | None:
    """属性・srcset・CSS url() の参照先を分類し、自己完結契約違反ならその理由を返す。

    既定（strict）で許容するのは許可 MIME の data URI とページ内 fragment のみ。
    相対パス・絶対パス・file:// も「単一ファイル配布で欠落・解決不能になる」ため違反とする。
    allow_local_refs=True（storyboard.html が screens/*.png を参照する構成向け）の場合のみ、
    文書ディレクトリ（base_dir）配下の実在する通常ファイルへ解決される相対参照を追加で
    許容する。絶対パス・file://・`../` による base_dir 外への脱出・欠落参照は常に違反。
    """
    t = target.strip().strip("\"'")
    if not t or t.startswith("#"):
        return None
    low = t.lower()
    if low.startswith("data:"):
        mime = low[len("data:"):].split(";", 1)[0].split(",", 1)[0].strip()
        if mime in ALLOWED_DATA_URI_MIME:
            return None
        return f"許可外 MIME の data URI（{mime or '未指定'}）"
    if low.startswith("//") or low.startswith(("http://", "https://")):
        return "外部 URL 参照（CDN 禁止・自己完結契約違反）"
    if re.match(r"^[a-z][a-z0-9+.-]*:", low):
        return "スキーム付き参照（file:// 等。単一ファイル配布で解決できないため不可）"
    if not allow_local_refs:
        return "ローカルファイル参照（単一ファイル配布で欠落するため不可）"
    if t.startswith("/"):
        return "絶対パス参照（配布先の環境で解決できないため不可）"
    resolved = (base_dir / t.split("#", 1)[0].split("?", 1)[0]).resolve()
    if resolved != base_dir.resolve() and not resolved.is_relative_to(base_dir.resolve()):
        return "文書ディレクトリ外への相対参照（`../` 脱出は不可）"
    # 範囲内でも欠落参照（screens/missing.png 等）は画像欠けのまま PASS しないよう
    # fail-closed で実在（通常ファイル）を確認する
    if not resolved.is_file():
        return "参照先ファイルが存在しない（欠落参照のまま完了扱いにしないため不可）"
    return None


def check_external_dependency(
    html_text: str, base_dir: Path, allow_local_refs: bool, failures: list[str]
) -> None:
    text = HTML_COMMENT_RE.sub("", html_text)
    text = BLOCK_COMMENT_RE.sub("", text)

    for m in REF_ATTR_RE.finditer(text):
        attr = m.group(1)
        value = next(g for g in m.groups()[1:] if g is not None)
        reason = classify_ref(value, base_dir, allow_local_refs)
        if reason:
            failures.append(f"{attr} 属性に自己完結契約違反の参照を検出: {value}（{reason}）")
    for m in SRCSET_RE.finditer(text):
        value = next(g for g in m.groups() if g is not None)
        for ref in srcset_candidates(value):
            reason = classify_ref(ref, base_dir, allow_local_refs)
            if reason:
                failures.append(f"srcset に自己完結契約違反の参照を検出: {ref}（{reason}）")
    for m in CSS_URL_RE.finditer(text):
        target = m.group(2).strip()
        reason = classify_ref(target, base_dir, allow_local_refs)
        if reason:
            failures.append(f"CSS url() に自己完結契約違反の参照を検出: {target}（{reason}）")
    if CSS_IMPORT_RE.search(text):
        failures.append(
            "CSS @import を検出（外部・ローカルを問わずスタイル分割は単一ファイル契約違反。"
            "<style> 内へ直接記述すること）"
        )
    # on* 属性・script 本文は正規表現の全文検索ではなく構造解析で収集する
    # （可視テキスト中の説明コピーへの誤マッチ防止と、属性・要素境界の正確な判定のため）
    auditor = _WireframeAuditor()
    auditor.feed(html_text)
    auditor.close()
    for handler in auditor.inline_handlers:
        failures.append(f"inline event handler 属性を検出: {handler}（禁止 JS・契約違反）")
    script_text = "\n".join(auditor.script_bodies)
    if JS_NETWORK_RE.search(script_text):
        failures.append(
            "script 内に JS のネットワーク API（fetch / XMLHttpRequest / WebSocket / sendBeacon 等）"
            "を検出（自己完結契約違反）"
        )
    for pattern, name in BANNED_JS_PATTERNS:
        if pattern.search(script_text):
            failures.append(f"script 内に禁止された JavaScript パターンを検出: {name}")


def _file_url_to_path(file_url: str) -> Path:
    """file:// URL をローカルパスへ変換する（パーセントエンコード解除込み）。"""
    return Path(url2pathname(urlsplit(file_url).path))


def check_viewport_overflow(
    html_path: Path, width: int, height: int, label: str, allow_local_refs: bool, failures: list[str]
) -> None:
    doc_path = html_path.resolve()
    base_dir = doc_path.parent
    url = "file://" + pathname2url(str(doc_path))
    # 静的検査をすり抜けた動的な外部要求（JS 実行・CSS 解決由来）を実行時に検出する。
    # 許可するのは about: と文書本体自身の file:// URL のみ（allow_local_refs 時は
    # 文書ディレクトリ配下に**実在する通常ファイル**への file:// も追加許可。欠落参照は
    # 静的検査と同様に fail-closed で遮断・FAIL にする）。それ以外の file:// を含む
    # 全要求を abort するため、検査対象 HTML が文書外へ実際に到達することはない
    # （「通信・読込した後で PASS する」抜け道の封鎖）。
    blocked_requests: list[str] = []

    def _route_handler(route):
        req_url = route.request.url
        if req_url.startswith("about:"):
            route.continue_()
            return
        if req_url.startswith("file://"):
            req_path = _file_url_to_path(req_url).resolve()
            if req_path == doc_path:
                route.continue_()
                return
            if allow_local_refs and req_path.is_relative_to(base_dir):
                if req_path.is_file():
                    route.continue_()
                    return
                blocked_requests.append(f"{req_url}（参照先ファイルが存在しない）")
                route.abort()
                return
        blocked_requests.append(req_url)
        route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": height})
        context.route("**/*", _route_handler)
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        client_width = page.evaluate("document.documentElement.clientWidth")
        browser.close()

    for req_url in dict.fromkeys(blocked_requests):
        failures.append(f"{label} ({width}px): 実行時に文書外への要求を検出（遮断済み）: {req_url}")

    if scroll_width - client_width > OVERFLOW_TOLERANCE_PX:
        failures.append(
            f"{label} ({width}px): 水平方向のオーバーフローを検出 "
            f"(scrollWidth={scroll_width} > clientWidth={client_width})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="HTML ワイヤーフレームの overflow / 自己完結性を検証する")
    parser.add_argument("html", type=Path)
    parser.add_argument(
        "--viewport",
        action="append",
        metavar="LABEL:WIDTHxHEIGHT",
        help="検証する viewport を追加指定（例: desktop:1440x900）。省略時は既定の PC/モバイルを使う",
    )
    parser.add_argument(
        "--allow-local-refs",
        action="store_true",
        help="文書ディレクトリ配下に実在するファイルへの相対参照を許可する（storyboard.html が"
        " screens/*.png を参照する構成向け。絶対パス・file://・`../` 脱出・欠落参照は引き続き違反）",
    )
    args = parser.parse_args()

    if not args.html.is_file():
        print(f"FAIL: HTML が存在しない: {args.html}")
        return 1

    viewports = DEFAULT_VIEWPORTS
    if args.viewport:
        viewports = []
        for spec in args.viewport:
            label, dims = spec.split(":", 1)
            w, h = dims.split("x", 1)
            viewports.append((label, int(w), int(h)))

    failures: list[str] = []
    html_text = args.html.read_text(encoding="utf-8", errors="replace")
    check_external_dependency(html_text, args.html.resolve().parent, args.allow_local_refs, failures)
    for label, w, h in viewports:
        check_viewport_overflow(args.html, w, h, label, args.allow_local_refs, failures)

    if failures:
        print(f"FAIL: {len(failures)}件の問題を検出 ({args.html})")
        for f in failures:
            print(f" - {f}")
        return 1

    print(f"PASS: {args.html} は全チェックを通過（{', '.join(l for l, _, _ in viewports)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
