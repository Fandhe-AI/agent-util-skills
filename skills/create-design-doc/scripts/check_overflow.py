#!/usr/bin/env python3
"""check_overflow.py — 自己完結 HTML ワイヤーフレームのレイアウト崩れを機械検証する。

役割と境界:
- 指定 viewport（既定: PC 1440px・モバイル 375px）で HTML をロードし、
  横スクロールを誘発する水平方向のオーバーフローが無いかを検証する。
- 外部 CDN・外部 script 等の self-contained 契約違反も検出する
  （create-html-report/scripts/validate_report.py の外部依存チェックと同じ考え方）。
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

EXTERNAL_URL_RE = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.IGNORECASE)
CDN_IMPORT_RE = re.compile(r"@import\s+url\(\s*['\"]?https?://", re.IGNORECASE)


def check_external_dependency(html_text: str, failures: list[str]) -> None:
    if EXTERNAL_URL_RE.search(html_text):
        failures.append("外部 URL への src/href 参照を検出（CDN 禁止・自己完結契約違反）")
    if CDN_IMPORT_RE.search(html_text):
        failures.append("CSS @import による外部リソース参照を検出")


def check_viewport_overflow(html_path: Path, width: int, height: int, label: str, failures: list[str]) -> None:
    url = "file://" + pathname2url(str(html_path.resolve()))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url, wait_until="networkidle")
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        client_width = page.evaluate("document.documentElement.clientWidth")
        browser.close()

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
