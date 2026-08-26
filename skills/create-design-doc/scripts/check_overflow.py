#!/usr/bin/env python3
"""check_overflow.py — 自己完結 HTML ワイヤーフレームのレイアウト崩れを機械検証する。

役割と境界:
- 指定 viewport（既定: PC 1440px・モバイル 375px）で HTML をロードし、
  横スクロールを誘発する水平方向のオーバーフローが無いかを検証する。
- 外部 CDN・外部 script 等の self-contained 契約違反も検出する
  （create-html-report/scripts/validate_report.py の外部依存チェックと同じ考え方）。
  検出は静的検査（属性・srcset・CSS url()/@import・<script> 全面禁止・
  inline event handler 禁止）と、
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

# コメント内の URL 例示（CSS コメント・legacy な <style> 内 HTML コメント等）を
# 誤検出しないよう、CSS 検査対象テキストから検査前に除去する
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# リソースを参照し得る属性。object[data]・SVG の image/use（href / xlink:href）も
# ここでカバーする。値は _WireframeAuditor がタグ文脈付きで構造的に収集し、
# classify_ref で外部 URL・相対パス・絶対パス・file:// を判定する
REF_ATTRS = {"src", "href", "xlink:href", "data", "poster", "action", "formaction", "srcset"}
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
# --allow-local-refs で許可するローカル参照の拡張子（data URI 許可 MIME と同じ raster 4種）。
# 静的検査（classify_ref）と実行時 route（check_overflow / capture_screenshot 双方が
# local_file_violation 経由で使用）で共有する単一定義。iframe/object 等で HTML を
# 持ち込むと参照先の <script> が未検査のまま実行されるため、画像以外は許可しない
ALLOWED_LOCAL_REF_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
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
class _WireframeAuditor(HTMLParser):
    """<script> 要素と on* 属性を構造的に検出する監査パーサ。

    デザイン成果物（wireframe / flow / storyboard）は静的な見た目の表現だけで完結する
    契約のため、<script> は本文の有無を問わず存在自体を違反とする。禁止識別子の
    部分一致検査は `window['Web' + 'Sock' + 'et']` のような動的組み立てで迂回できる
    ため採らない（全面禁止が fail-closed）。ブラウザは <script/> のような自己閉じ
    表記も開始タグとして扱うため、startendtag も開始扱いで数える。
    iframe の srcdoc 属性は埋め込み HTML 文書として再帰検査し、srcdoc 内の
    script・on* 属性も同じ基準で検出する。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_handlers: list[str] = []  # "<tag> の <on*属性>" 形式で記録
        self.refs: list[tuple[str, str, str, str]] = []  # (scope, tag, attr, value)
        self.script_tags = 0
        # CSS の url()/@import 検査を <style> 本文と style 属性値に限定するための収集先
        # （可視テキストや <code> 中の url() 例示を外部依存と誤判定しないため）
        self.style_bodies: list[str] = []
        self.style_attrs: list[str] = []
        self._in_style = False
        self._style_buf: list[str] = []

    def _visit_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if not name:
                continue
            lname = name.lower()
            if lname.startswith("on"):
                self.inline_handlers.append(f"<{tag}> の {name}")
            elif lname == "style" and value:
                self.style_attrs.append(value)
            elif lname == "srcdoc" and value:
                # HTMLParser は属性値を unescape 済みで渡すため、srcdoc の値は
                # 完全な HTML 文書として再帰解析できる
                sub = _WireframeAuditor()
                sub.feed(value)
                sub.close()
                self.inline_handlers.extend(f"srcdoc 内: {h}" for h in sub.inline_handlers)
                self.refs.extend(("srcdoc 内 " + s, t, a, v) for s, t, a, v in sub.refs)
                self.script_tags += sub.script_tags
                self.style_bodies.extend(sub.style_bodies)
                self.style_attrs.extend(sub.style_attrs)
            elif lname in REF_ATTRS and value is not None:
                # 参照元のタグ文脈を保持して収集する（--allow-local-refs の許可判定が
                # 「<img src> / srcset か否か」に依存するため）
                self.refs.append(("", tag.lower(), lname, value))
        if tag.lower() == "script":
            self.script_tags += 1
        if tag.lower() == "style":
            # <style/> 自己閉じ表記もブラウザは開始タグとして扱うため startendtag 経由でも開始扱い。
            # 収集中に別の <style> 開始タグへ再突入した場合は、進行中のバッファを捨てず
            # フラッシュしてから新規収集を始める（捨てると収集済み CSS が失われ、
            # ブラウザが適用する外部 CSS を静的検査が見逃す fail-open になる）
            if self._in_style and self._style_buf:
                self.style_bodies.append("".join(self._style_buf))
            self._in_style = True
            self._style_buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._visit_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._visit_tag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self._in_style:
            self.style_bodies.append("".join(self._style_buf))
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_buf.append(data)

    def close(self) -> None:
        super().close()
        # 閉じタグ欠落時も fail-closed で EOF までを style 本文として収集する
        if self._in_style:
            self.style_bodies.append("".join(self._style_buf))
            self._in_style = False


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
    文書ディレクトリ（base_dir）配下に実在する承認済み raster 画像
    （ALLOWED_LOCAL_REF_SUFFIXES）へ解決される相対参照を追加で許容する。
    参照元の限定（<img src> / srcset / CSS 画像参照のみ）は呼び出し元が判定して
    allow_local_refs に反映する。絶対パス・file://・`../` による base_dir 外への脱出・
    欠落参照・raster 以外の拡張子は常に違反。
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
        return (
            "ローカルファイル参照（単一ファイル配布で欠落するため不可。--allow-local-refs の"
            "許可対象も <img src> / srcset / CSS 画像参照からの raster 画像に限る）"
        )
    if t.startswith("/"):
        return "絶対パス参照（配布先の環境で解決できないため不可）"
    resolved = (base_dir / t.split("#", 1)[0].split("?", 1)[0]).resolve()
    if resolved != base_dir.resolve() and not resolved.is_relative_to(base_dir.resolve()):
        return "文書ディレクトリ外への相対参照（`../` 脱出は不可）"
    # HTML 等を持ち込むと参照先の <script> が未検査のまま実行されるため、
    # 承認済み raster 拡張子以外は範囲内でも拒否する
    if resolved.suffix.lower() not in ALLOWED_LOCAL_REF_SUFFIXES:
        return "承認済み raster 画像（.png/.jpg/.jpeg/.gif/.webp）以外のローカル参照は不可"
    # 範囲内でも欠落参照（screens/missing.png 等）は画像欠けのまま PASS しないよう
    # fail-closed で実在（通常ファイル）を確認する
    if not resolved.is_file():
        return "参照先ファイルが存在しない（欠落参照のまま完了扱いにしないため不可）"
    return None


def check_external_dependency(
    html_text: str, base_dir: Path, allow_local_refs: bool, failures: list[str]
) -> None:
    # <script>・on* 属性・参照属性・CSS（<style> 本文と style 属性値）は正規表現の
    # 全文検索ではなく構造解析で収集する（可視テキスト中の説明コピーへの誤マッチ防止と、
    # 参照元タグの正確な判定のため）
    auditor = _WireframeAuditor()
    auditor.feed(html_text)
    auditor.close()

    # --allow-local-refs の許可対象は「<img src> / srcset / CSS 画像参照」の raster 画像に
    # 限定する。iframe/object/embed 等の埋め込み属性からのローカル参照は、参照先 HTML の
    # <script> が未検査のまま実行されるため base_dir 配下でも拒否する
    for scope, tag, attr, value in auditor.refs:
        image_site = (tag == "img" and attr == "src") or (tag in ("img", "source") and attr == "srcset")
        site_allow = allow_local_refs and image_site
        if attr == "srcset":
            for ref in srcset_candidates(value):
                reason = classify_ref(ref, base_dir, site_allow)
                if reason:
                    failures.append(f"{scope}srcset に自己完結契約違反の参照を検出: {ref}（{reason}）")
        else:
            reason = classify_ref(value, base_dir, site_allow)
            if reason:
                failures.append(
                    f"{scope}<{tag}> の {attr} 属性に自己完結契約違反の参照を検出: {value}（{reason}）"
                )
    # CSS 検査は auditor が収集した <style> 本文と style 属性値（srcdoc 内含む）に限定する。
    # CSS コメント・legacy な HTML コメント内の url() 例示は検査前に除去して誤検知を防ぐ
    css_text = "\n".join(auditor.style_bodies + auditor.style_attrs)
    css_text = HTML_COMMENT_RE.sub("", css_text)
    css_text = BLOCK_COMMENT_RE.sub("", css_text)
    for m in CSS_URL_RE.finditer(css_text):
        target = m.group(2).strip()
        reason = classify_ref(target, base_dir, allow_local_refs)
        if reason:
            failures.append(f"CSS url() に自己完結契約違反の参照を検出: {target}（{reason}）")
    if CSS_IMPORT_RE.search(css_text):
        failures.append(
            "CSS @import を検出（外部・ローカルを問わずスタイル分割は単一ファイル契約違反。"
            "<style> 内へ直接記述すること）"
        )
    for handler in auditor.inline_handlers:
        failures.append(f"inline event handler 属性を検出: {handler}（禁止 JS・契約違反）")
    if auditor.script_tags:
        failures.append(
            f"<script> 要素を検出（{auditor.script_tags}件）: デザイン成果物では script を"
            "全面禁止（識別子の動的組み立てで禁止語検査を迂回できるため、本文の有無を"
            "問わず存在自体を契約違反とする）"
        )


def _file_url_to_path(file_url: str) -> Path:
    """file:// URL をローカルパスへ変換する（パーセントエンコード解除込み）。"""
    return Path(url2pathname(urlsplit(file_url).path))


def local_file_violation(req_path: Path) -> str | None:
    """--allow-local-refs 時の base_dir 配下 file:// 要求を許可できない場合、理由を返す。

    静的検査（classify_ref）と同じ allowlist（実在する通常ファイル・承認済み raster
    拡張子）を実行時 route に適用する。check_overflow / capture_screenshot の双方が
    共有する単一定義（判定基準の二重管理防止）。
    """
    if not req_path.is_file():
        return "参照先ファイルが存在しない"
    if req_path.suffix.lower() not in ALLOWED_LOCAL_REF_SUFFIXES:
        return "承認済み raster 画像（.png/.jpg/.jpeg/.gif/.webp）以外のローカル参照"
    return None


def check_viewport_overflow(
    html_path: Path, width: int, height: int, label: str, allow_local_refs: bool, failures: list[str]
) -> None:
    doc_path = html_path.resolve()
    base_dir = doc_path.parent
    url = "file://" + pathname2url(str(doc_path))
    # 静的検査をすり抜けた動的な外部要求（JS 実行・CSS 解決由来）を実行時に検出する。
    # 許可するのは about: と文書本体自身の file:// URL のみ（allow_local_refs 時は
    # 文書ディレクトリ配下に**実在する承認済み raster 画像**への file:// も追加許可。
    # 欠落参照・raster 以外は静的検査と同様に fail-closed で遮断・FAIL にする）。
    # それ以外の file:// を含む全要求を abort するため、検査対象 HTML が文書外へ
    # 実際に到達することはない（「通信・読込した後で PASS する」抜け道の封鎖）。
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
                # 範囲内でも欠落参照・raster 以外は fail-closed で遮断する
                # （静的検査と同じ allowlist。local_file_violation を共有）
                reason = local_file_violation(req_path)
                if reason is None:
                    route.continue_()
                    return
                blocked_requests.append(f"{req_url}（{reason}）")
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
        help="<img src> / srcset / CSS 画像参照からの、文書ディレクトリ配下に実在する raster 画像"
        "（.png/.jpg/.jpeg/.gif/.webp）への相対参照のみ許可する（storyboard.html が screens/*.png を"
        " 参照する構成向け。iframe/object 等の埋め込み参照・絶対パス・file://・`../` 脱出・"
        "欠落参照は引き続き違反）",
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
    # 静的検査で違反を検出した HTML はブラウザで実行しない。route("**/*") は
    # WebSocket 等の全経路を確実に遮断できる保証がなく、検出済みの禁止 JS・
    # ネットワーク API が失敗報告前に実行・通信し得るため、起動前に FAIL 終了する
    if failures:
        print(f"FAIL: {len(failures)}件の問題を検出 ({args.html})")
        for f in failures:
            print(f" - {f}")
        print("静的検査で違反を検出したため、実行時検査（viewport overflow・ネットワーク遮断）はスキップした")
        return 1
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
