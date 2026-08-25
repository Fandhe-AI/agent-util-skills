#!/usr/bin/env python3
"""capture_screenshot.py — 自己完結 HTML (ワイヤーフレーム / 画面遷移図) を PNG 化する。

役割と境界:
- 指定 viewport で HTML を Playwright (Chromium) にロードし screenshot を撮る。
- HTML の内容判断・レイアウト設計は行わない（描画結果を画像化するだけ）。
- レイアウト崩れの自動判定は check_overflow.py の責務（本スクリプトは撮影のみ）。

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


def capture(html_path: Path, out_path: Path, width: int, height: int, full_page: bool) -> None:
    url = "file://" + pathname2url(str(html_path.resolve()))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url, wait_until="networkidle")
        page.screenshot(path=str(out_path), full_page=full_page)
        browser.close()


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

    capture(args.html, args.out, args.width, args.height, args.full_page)
    print(f"撮影完了: {args.out} ({args.width}x{args.height}{'  full-page' if args.full_page else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
