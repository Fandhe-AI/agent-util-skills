#!/usr/bin/env python3
"""validate_slides.py — 生成された自己完結 HTML スライドの機械検証。

役割と境界:
- build_slides.py（または手直しした html）の出力が SKILL.md の契約
  （役割順序 / 各スライドのはみ出しなし / 自己完結 / inline JS の安全性 /
  前提と解釈・承認事項の必須配置）を満たすかを、生成された .html ファイル
  そのものから検証する（spec を信頼しない。create-html-report/scripts/
  validate_report.py と同じ設計方針）。
- 全スライドを実際にキーボード操作・クリック操作で遷移させながら検証し、
  各スライドの PNG（1440x900）を確認用に撮影する。
- 検証のみを行い、ファイルの修正は行わない。

使い方:
    python3 validate_slides.py <deck.html> --screenshots-dir <dir>

終了コード: 全チェック PASS で 0、1 件でも FAIL で 1。
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

VIEWPORT = {"width": 1440, "height": 900}
OVERFLOW_TOLERANCE_PX = 1

FRONT_HALF_ROLES = ["cover", "premise", "problem", "solution", "scope", "winning"]
BACK_HALF_TAIL_ROLES = ["validation", "approval"]
SCREEN_FLOW_ROLE = "screen_flow"
SCREEN_FLOW_MIN = 2
SCREEN_FLOW_MAX = 4
APPROVAL_ITEM_MIN = 3
APPROVAL_ITEM_MAX = 5

# 自己完結契約: 外部 URL への参照を禁止する（data: URI は許可。png/jpeg/gif/webp
# 以外の data: MIME は untrusted な能動コンテンツを埋め込める経路のため対象外
# — create-html-report/scripts/validate_report.py の DATA_URI_ALLOWED と同じ方針）。
EXTERNAL_URL_RE = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.IGNORECASE)
CDN_IMPORT_RE = re.compile(r"@import\s+url\(\s*['\"]?https?://", re.IGNORECASE)
UNSAFE_DATA_URI_RE = re.compile(
    r"""src\s*=\s*["']data:(?!image/(png|jpeg|gif|webp)[;,])""", re.IGNORECASE
)

# inline JavaScript の逸脱チェック（AGENTS.md P0）。eval・new Function・
# innerHTML 代入・inline event handler 属性・network API の不使用を確認する。
INLINE_HANDLER_RE = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
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


def check_self_contained(html_text: str, failures: list[str]) -> None:
    if EXTERNAL_URL_RE.search(html_text):
        failures.append("外部 URL への src/href 参照を検出（自己完結契約違反）")
    if CDN_IMPORT_RE.search(html_text):
        failures.append("CSS @import による外部リソース参照を検出")
    if UNSAFE_DATA_URI_RE.search(html_text):
        failures.append("png/jpeg/gif/webp 以外の data: URI（image/svg+xml 等）を検出")


def check_inline_js_safety(html_text: str, failures: list[str]) -> None:
    if INLINE_HANDLER_RE.search(html_text):
        failures.append("inline event handler 属性（onclick= 等）を検出")
    for pattern, label in FORBIDDEN_JS_PATTERNS:
        if pattern.search(html_text):
            failures.append(f"禁止された JavaScript パターンを検出: {label}")


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
        help="各スライドの PNG（1440x900）を出力するディレクトリ。省略時は撮影しない",
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
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)
        page.goto(url, wait_until="networkidle")

        roles = page.eval_on_selector_all(".slide", "els => els.map(e => e.dataset.role)")
        total = len(roles)
        check_roles(roles, failures)

        if args.screenshots_dir:
            args.screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: 先頭スライドから ArrowRight で全スライドを順に遷移し、
        # 各スライドで overflow を検証・PNG を撮影する。
        #
        # 注意: document.documentElement の scrollWidth/scrollHeight は使わない。
        # html/body に overflow:hidden を設定しているため、非表示スライド
        # （display:none）はもちろん、表示中スライドの内部コンテンツが
        # ビューポートを超えてもドキュメント全体としては overflow:hidden で
        # 隠れてしまい scrollHeight がビューポートサイズのまま変化しない
        # （実測で確認済み: 意図的に長文を仕込んでも documentElement.scrollHeight
        # は 900 のままだったが、該当 .slide 要素自身の scrollHeight は 2051 を示した）。
        # 判定は「現在表示中の .slide 要素自身」の scrollHeight/scrollWidth と
        # clientHeight/clientWidth を比較することで行う。
        for i in range(total):
            if i > 0:
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(30)
            dims = page.eval_on_selector(
                ".slide.active",
                "el => ({sh: el.scrollHeight, ch: el.clientHeight, sw: el.scrollWidth, cw: el.clientWidth})",
            )
            if dims["sw"] - dims["cw"] > OVERFLOW_TOLERANCE_PX:
                failures.append(
                    f"slide {i + 1}/{total} (role={roles[i]}): 横方向オーバーフロー "
                    f"(scrollWidth={dims['sw']} > clientWidth={dims['cw']})"
                )
            if dims["sh"] - dims["ch"] > OVERFLOW_TOLERANCE_PX:
                failures.append(
                    f"slide {i + 1}/{total} (role={roles[i]}): 縦方向オーバーフロー "
                    f"(scrollHeight={dims['sh']} > clientHeight={dims['ch']})"
                )
            if args.screenshots_dir:
                out = args.screenshots_dir / f"slide-{i + 1:02d}-{roles[i]}.png"
                page.screenshot(path=str(out))

        # Step 2: ArrowRight を末尾より多く押しても範囲外に出ない（clamp）ことを確認
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(30)
        last_role = page.eval_on_selector(
            ".slide.active", "el => el.dataset.role"
        )
        if last_role != "approval":
            failures.append(
                f"末尾を超えて ArrowRight しても role=approval に留まらない（検出: {last_role}）"
            )

        # Step 3: R キーで先頭へ戻ることを確認
        page.keyboard.press("r")
        page.wait_for_timeout(30)
        first_role = page.eval_on_selector(".slide.active", "el => el.dataset.role")
        if first_role != "cover":
            failures.append(f"'R' キーで先頭（role=cover）へ戻らない（検出: {first_role}）")

        # Step 4: クリックでの次スライド遷移を確認
        page.click("#next-btn")
        page.wait_for_timeout(30)
        second_role = page.eval_on_selector(".slide.active", "el => el.dataset.role")
        if second_role != "premise":
            failures.append(
                f"next ボタンクリックで2枚目（role=premise）へ進まない（検出: {second_role}）"
            )

        # Step 5: premise（2枚目）に「前提」が含まれるか
        # role 順序契約が壊れて .slide[data-role='premise'] 自体が存在しない
        # 場合に例外で落ちないよう、要素の有無を先に確認する
        # （role 順序違反自体は Step 0 の check_roles() で既に failures 済み）。
        if "premise" not in roles:
            failures.append("role=premise のスライドが存在しない（前提と解釈の内容チェック不可）")
        else:
            premise_text = page.eval_on_selector(".slide[data-role='premise']", "el => el.innerText")
            if "前提" not in premise_text:
                failures.append("role=premise のスライドに「前提」という語が含まれない")

        # Step 6: approval（最終）に「承認」と3〜5件の番号付き項目が含まれるか
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

        # Step 7: print media でナビゲーション要素が非表示になるか
        page.emulate_media(media="print")
        for selector in (".topbar", ".progress", ".navbtn"):
            display = page.eval_on_selector(selector, "el => getComputedStyle(el).display")
            if display != "none":
                failures.append(f"print media で {selector} が非表示になっていない（display={display}）")
        page.emulate_media(media="screen")

        browser.close()

    if failures:
        print(f"FAIL: {len(failures)}件の問題を検出（{total}枚）")
        for f in failures:
            print(f" - {f}")
        return 1

    print(f"PASS: {args.html} は全チェックを通過（{total}枚）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
