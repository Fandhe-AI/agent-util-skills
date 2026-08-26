#!/usr/bin/env python3
"""check_overflow.py — 自己完結 HTML ワイヤーフレームのレイアウト崩れを機械検証する。

役割と境界:
- 指定 viewport（既定: PC 1440px・モバイル 375px）で HTML をロードし、
  横スクロールを誘発する水平方向のオーバーフローが無いかを検証する。
- 外部 CDN・外部 script 等の self-contained 契約違反も検出する
  （create-html-report/scripts/validate_report.py の外部依存チェックと同じ考え方）。
  検出は静的検査（属性・srcset・CSS url()/@import・JS ネットワーク API）と、
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

DEFAULT_VIEWPORTS = [("desktop", 1440, 900), ("mobile", 375, 812)]
OVERFLOW_TOLERANCE_PX = 1  # サブピクセル丸め誤差の許容量

# コメント内の URL 例示（テンプレート冒頭の説明文等）を誤検出しないよう、
# 検査前に HTML コメントと CSS/JS ブロックコメントを除去する
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# 外部リソースを参照し得る属性（引用符あり・なしの双方）。
# object[data]・SVG の image/use（href / xlink:href）もここでカバーする
EXTERNAL_ATTR_RE = re.compile(
    r"""\b(?:src|href|xlink:href|data|poster|action|formaction)\s*=\s*["']?\s*(?:https?:)?//""",
    re.IGNORECASE,
)
# srcset は「URL 幅記述子, URL 幅記述子, ...」形式のため属性値全体を取り出して候補ごとに検査する
SRCSET_RE = re.compile(r"""\bsrcset\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
# CSS の url(...) 全般（@font-face の src・background-image 等）。引用符の有無を問わない
CSS_URL_RE = re.compile(r"""\burl\(\s*(["']?)([^"')]+)\1\s*\)""", re.IGNORECASE)
# @import は url(...) 形式・文字列直接指定の双方で外部 URL を検査する
CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?["']?\s*(?:https?:)?//""", re.IGNORECASE)
# 自己完結ワイヤーフレームに通信 API は不要のため、存在自体を契約違反とする
JS_NETWORK_RE = re.compile(
    r"\b(?:fetch\s*\(|XMLHttpRequest\b|WebSocket\s*\(|sendBeacon\s*\(|EventSource\s*\(|importScripts\s*\()"
)


def _is_external_ref(target: str) -> bool:
    """url() / srcset の参照先が外部リソースかを判定する。

    data URI・ページ内 fragment・ローカル相対/絶対パスのみ自己完結とみなし、
    http(s) はもちろん protocol-relative（//）やその他のスキームも外部扱いにする。
    """
    t = target.strip().strip("\"'").lower()
    if not t or t.startswith(("data:", "#")):
        return False
    if t.startswith("//"):
        return True
    return bool(re.match(r"^[a-z][a-z0-9+.-]*:", t))


def check_external_dependency(html_text: str, failures: list[str]) -> None:
    text = HTML_COMMENT_RE.sub("", html_text)
    text = BLOCK_COMMENT_RE.sub("", text)

    if EXTERNAL_ATTR_RE.search(text):
        failures.append(
            "外部 URL への属性参照（src / href / xlink:href / data / poster 等）を検出"
            "（CDN 禁止・自己完結契約違反）"
        )
    for m in SRCSET_RE.finditer(text):
        value = next(g for g in m.groups() if g is not None)
        for candidate in value.split(","):
            parts = candidate.strip().split()
            if parts and _is_external_ref(parts[0]):
                failures.append(f"srcset に外部 URL を検出: {parts[0]}")
    for m in CSS_URL_RE.finditer(text):
        target = m.group(2).strip()
        if _is_external_ref(target):
            failures.append(f"CSS url() に外部参照を検出: {target}")
    if CSS_IMPORT_RE.search(text):
        failures.append("CSS @import による外部リソース参照を検出")
    if JS_NETWORK_RE.search(text):
        failures.append(
            "JS のネットワーク API（fetch / XMLHttpRequest / WebSocket / sendBeacon 等）を検出"
            "（自己完結契約違反）"
        )


def check_viewport_overflow(html_path: Path, width: int, height: int, label: str, failures: list[str]) -> None:
    url = "file://" + pathname2url(str(html_path.resolve()))
    # 静的検査をすり抜けた動的な外部要求（JS 実行・CSS 解決由来）を実行時に検出する。
    # file:// と about: 以外はすべて abort するため、検査対象 HTML が外部へ実際に
    # 通信することはない（「通信した後で PASS する」抜け道の封鎖）。
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
        context = browser.new_context(viewport={"width": width, "height": height})
        context.route("**/*", _route_handler)
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        client_width = page.evaluate("document.documentElement.clientWidth")
        browser.close()

    for req_url in dict.fromkeys(blocked_requests):
        failures.append(f"{label} ({width}px): 実行時に外部リクエストを検出（遮断済み）: {req_url}")

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
    check_external_dependency(html_text, failures)
    for label, w, h in viewports:
        check_viewport_overflow(args.html, w, h, label, failures)

    if failures:
        print(f"FAIL: {len(failures)}件の問題を検出 ({args.html})")
        for f in failures:
            print(f" - {f}")
        return 1

    print(f"PASS: {args.html} は全チェックを通過（{', '.join(l for l, _, _ in viewports)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
