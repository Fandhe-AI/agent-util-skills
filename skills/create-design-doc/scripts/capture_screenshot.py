#!/usr/bin/env python3
"""capture_screenshot.py — 自己完結 HTML (ワイヤーフレーム / 画面遷移図) を PNG 化する。

役割と境界:
- 指定 viewport で HTML を Playwright (Chromium) にロードし screenshot を撮る。
- HTML の内容判断・レイアウト設計は行わない（描画結果を画像化するだけ）。
- レイアウト崩れの自動判定は check_overflow.py の責務（本スクリプトは撮影のみ）。
- ただし外部通信の遮断は本スクリプトも担う。SKILL.md の手順では撮影が
  check_overflow.py による検証より先に走るため、ここで遮断しないと違反 HTML が
  検証前に外部へ通信してしまう。check_overflow.py と同じ流儀で file:// と about:
  以外の全リクエストを abort し、遮断要求が 1 件でもあれば撮影を失敗させる。

依存: playwright (標準ライブラリ外)。venv へインストールし
`playwright install chromium` でブラウザ本体を取得してから実行する。
"""
from __future__ import annotations

import argparse
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


def capture(html_path: Path, out_path: Path, width: int, height: int, full_page: bool) -> list[str]:
    """HTML を撮影し、遮断した外部リクエスト URL の一覧を返す（空なら自己完結）。"""
    url = "file://" + pathname2url(str(html_path.resolve()))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # check_overflow.py の check_viewport_overflow と同じ実行時遮断。
    # file:// と about: 以外はすべて abort するため、撮影対象 HTML が外部へ実際に
    # 通信することはない（検証前の撮影段階で外部通信が発生する抜け道の封鎖）
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
        page.screenshot(path=str(out_path), full_page=full_page)
        browser.close()

    return list(dict.fromkeys(blocked_requests))


def main() -> int:
    parser = argparse.ArgumentParser(description="自己完結 HTML を PNG へ撮影する")
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--full-page", action="store_true", help="縦方向のフルページ撮影")
    args = parser.parse_args()

    if not args.html.is_file():
        print(f"HTML が存在しない: {args.html}", file=sys.stderr)
        return 1

    blocked = capture(args.html, args.out, args.width, args.height, args.full_page)
    if blocked:
        print(f"FAIL: {len(blocked)}件の外部リクエストを検出 ({args.html})")
        for req_url in blocked:
            print(f" - 実行時に外部リクエストを検出（遮断済み）: {req_url}")
        return 1

    print(f"撮影完了: {args.out} ({args.width}x{args.height}{'  full-page' if args.full_page else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
