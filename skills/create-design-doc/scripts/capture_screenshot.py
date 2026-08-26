#!/usr/bin/env python3
"""capture_screenshot.py — 自己完結 HTML (ワイヤーフレーム / 画面遷移図) を PNG 化する。

役割と境界:
- 指定 viewport で HTML を Playwright (Chromium) にロードし screenshot を撮る。
- HTML の内容判断・レイアウト設計は行わない（描画結果を画像化するだけ）。
- レイアウト崩れの自動判定は check_overflow.py の責務（本スクリプトは撮影のみ）。
- ただし外部通信の遮断は本スクリプトも担う。SKILL.md の手順では撮影が
  check_overflow.py による検証より先に走るため、ここで遮断しないと違反 HTML が
  検証前に外部へ通信してしまう。二段構えで防ぐ:
  1. 撮影前に check_overflow.py の静的検査（外部依存・禁止 JS・inline handler）を
     同ディレクトリ import で共通実行し、違反があればブラウザを起動せず FAIL 終了する
     （route("**/*") は WebSocket 等の全経路を確実に遮断できる保証がないため、
     違反 HTML はそもそも Chromium にロードしない）。
  2. 静的検査を通過した HTML のみロードし、check_overflow.py と同じ流儀で、about: と
     文書本体自身の file:// URL（--allow-local-refs 時は文書ディレクトリ配下の
     file:// も）以外の全リクエストを abort し、遮断要求が 1 件でもあれば失敗させる。

依存: playwright (標準ライブラリ外)。venv へインストールし
`playwright install chromium` でブラウザ本体を取得してから実行する。
"""
from __future__ import annotations

import argparse
import sys
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

# 撮影前の静的ゲートは check_overflow.py の検査関数を共通利用する（検査基準の二重管理防止）。
# スクリプトがどの cwd から起動されても同ディレクトリの check_overflow を import できるよう、
# 本ファイルの置き場所を sys.path へ加える
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_overflow import check_external_dependency  # noqa: E402


def _file_url_to_path(file_url: str) -> Path:
    """file:// URL をローカルパスへ変換する（パーセントエンコード解除込み）。"""
    return Path(url2pathname(urlsplit(file_url).path))


def capture(
    html_path: Path, out_path: Path, width: int, height: int, full_page: bool, allow_local_refs: bool
) -> list[str]:
    """HTML を撮影し、遮断した文書外要求の URL 一覧を返す（空なら自己完結）。"""
    doc_path = html_path.resolve()
    base_dir = doc_path.parent
    url = "file://" + pathname2url(str(doc_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # check_overflow.py の check_viewport_overflow と同じ実行時遮断。
    # 許可するのは about: と文書本体自身の file:// URL のみ（allow_local_refs 時は
    # 文書ディレクトリ配下の file:// も追加許可）。それ以外の file:// を含む全要求を
    # abort するため、撮影対象 HTML が文書外へ実際に到達することはない
    # （検証前の撮影段階で外部通信・文書外読込が発生する抜け道の封鎖）
    blocked_requests: list[str] = []

    def _route_handler(route):
        req_url = route.request.url
        if req_url.startswith("about:"):
            route.continue_()
            return
        if req_url.startswith("file://"):
            req_path = _file_url_to_path(req_url).resolve()
            if req_path == doc_path or (allow_local_refs and req_path.is_relative_to(base_dir)):
                route.continue_()
                return
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
    parser.add_argument(
        "--allow-local-refs",
        action="store_true",
        help="文書ディレクトリ配下に実在するファイルへの相対参照を許可する（storyboard.html が"
        " screens/*.png を参照する構成向け。絶対パス・file://・`../` 脱出・欠落参照は引き続き遮断）",
    )
    args = parser.parse_args()

    if not args.html.is_file():
        print(f"HTML が存在しない: {args.html}", file=sys.stderr)
        return 1

    # 静的検査で違反を検出した HTML はブラウザで実行しない（check_overflow.py と同じゲート。
    # --allow-local-refs の扱いも check_overflow と同一）
    static_failures: list[str] = []
    html_text = args.html.read_text(encoding="utf-8", errors="replace")
    check_external_dependency(html_text, args.html.resolve().parent, args.allow_local_refs, static_failures)
    if static_failures:
        print(f"FAIL: {len(static_failures)}件の問題を検出 ({args.html})")
        for f in static_failures:
            print(f" - {f}")
        print("静的検査で違反を検出したため、ブラウザを起動せず撮影を中止した")
        return 1

    blocked = capture(args.html, args.out, args.width, args.height, args.full_page, args.allow_local_refs)
    if blocked:
        print(f"FAIL: {len(blocked)}件の文書外への要求を検出 ({args.html})")
        for req_url in blocked:
            print(f" - 実行時に文書外への要求を検出（遮断済み）: {req_url}")
        return 1

    print(f"撮影完了: {args.out} ({args.width}x{args.height}{'  full-page' if args.full_page else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
