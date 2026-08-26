#!/usr/bin/env python3
"""deck spec (JSON) から自己完結 HTML フルスクリーンスライド（1ファイル）を生成する renderer。

役割: report spec -> HTML の create-html-report/scripts/render_report.py と対になる
存在。spec の構造検証・HTML/CSS/JS 組み立て・ワイヤーフレームの iframe srcdoc 埋め込みは
本スクリプトの責務とし、呼び出し側 (SKILL.md の手順) は spec の内容判断にのみ集中できる
ようにする。演出の仕組みは references/presentation-patterns.md の一般化パターンに基づく。

スライド構成: 前半 = 課題・解決アプローチ・スコープ・勝ち筋、後半 = 画面と操作の
流れ（screen_flow, 2〜4枚。create-design-doc の wireframes/*.html を iframe srcdoc で
実寸レンダリングし、ステップ送りに合わせてスポットライト表示する）→ 検証計画 →
承認いただきたい事項・確認事項。

各スライドの箇条書き・カード等は「フラグメント」として → キーで1つずつ出現し、
尽きたら次スライドへ進む（reveal.js 的な2段階ナビゲーション）。

依存: 標準ライブラリのみ（Pillow 等の画像ライブラリ不要）。生成された HTML の検証には
別スクリプト validate_slides.py（Playwright 必須）を使う。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

# 前半（固定・この順序）: 課題認識から勝ち筋まで
FRONT_HALF_ROLES = ["cover", "premise", "problem", "solution", "scope", "winning"]
# 後半固定末尾（screen_flow の直後、この順序・この2枚で終わること）
BACK_HALF_TAIL_ROLES = ["validation", "approval"]
SCREEN_FLOW_ROLE = "screen_flow"
SCREEN_FLOW_MIN = 2
SCREEN_FLOW_MAX = 4

APPROVAL_ITEM_MIN = 3
APPROVAL_ITEM_MAX = 5
APPROVAL_KINDS = {"承認", "確認"}
WINNING_LABELS = {"事実", "仮説"}

# brand の各値に許す色形式（#RGB / #RGBA / #RRGGBB / #RRGGBBAA の16進カラーのみ）。
# brand 値は CSS_TEMPLATE.format() で <style> 内へそのまま展開されるため、任意文字列を
# 許すと `</style><script>` によるタグ脱出注入や `{`/`}` による format 破壊が可能になる。
# 色リテラル以外は validate_spec が SpecError で拒否する（fail-closed）。
BRAND_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

ROLE_LABELS = {
    "cover": "COVER",
    "premise": "PREMISE",
    "problem": "PROBLEM",
    "solution": "SOLUTION",
    "scope": "SCOPE",
    "winning": "WINNING",
    "screen_flow": "SCREEN FLOW",
    "validation": "VALIDATION",
    "approval": "APPROVAL",
}

DARK_THEME = {
    "bg": "#0B1220", "surface": "#141C2E", "fg": "#F5F7FA", "muted": "#8B93A7",
    "border": "#26304A", "primary": "#5B8CFF", "accent": "#F5C242",
    "success": "#3DDC84", "warning": "#F5C242", "danger": "#FF6B6B",
}
LIGHT_THEME = {
    "bg": "#FFFFFF", "surface": "#F4F6F8", "fg": "#1C1C1C", "muted": "#5B6470",
    "border": "#D8DEE6", "primary": "#1F3A93", "accent": "#E08E45",
    "success": "#1E824C", "warning": "#B7791F", "danger": "#C0392B",
}
FONT_FAMILY = '"Noto Sans JP", "Hiragino Sans", "Yu Gothic", sans-serif'
FONT_MONO = '"Roboto Mono", "SFMono-Regular", Consolas, monospace'

# --- 自己完結・inline JS 安全性チェック（wireframe を埋め込む前の生テキストに適用する） ---
# HTML エスケープ後に正規表現で検出しようとすると `="` が `=&quot;` に変わるなどして
# 検出が効かなくなるため、埋め込み前の生テキストに対して必ずチェックする
# （references/presentation-patterns.md「5. iframe の scale とレイアウト崩れの罠」と対の
# 教訓: 「エスケープ後の文字列を検査しても手遅れ」）。
# 外部リソースを参照し得る属性の一覧（check_overflow.py の EXTERNAL_ATTR_RE と同等の
# 網羅）。object[data]・video/audio[poster]・form[action]・button[formaction]・
# SVG の xlink:href をカバーする。srcset は複数候補 URL 形式のため個別に処理する。
RESOURCE_ATTRS = frozenset({"src", "href", "xlink:href", "data", "poster", "action", "formaction"})
# srcset 候補の分割は「comma + 空白」に限定する（分割理由は srcset_candidates 参照）
SRCSET_SPLIT_RE = re.compile(r",\s+")
# 空白を伴わない comma の変則表記で紛れ込む外部 URL の fail-closed 検出用
SRCSET_SMUGGLED_URL_RE = re.compile(r"(?:^|[\s,])((?:https?:)?//[^\s,]+)", re.IGNORECASE)
# @import は url(...) 形式・文字列直接指定・相対参照のいずれでも自己完結を壊すため
# 出現自体を不許可にする（validate_slides.py の CSS_IMPORT_RE と同方針）
CDN_IMPORT_RE = re.compile(r"@import\b", re.IGNORECASE)
# inline event handler 属性の検出。主ゲートは _RefCollector の属性名判定
# （INLINE_HANDLER_ATTR_RE。HTMLParser の attrs は引用形式に依存しないため
# `<body onload=alert(1)>` のような引用符なし属性も検出できる）。本 regex は
# 引用符付きの表記のみ拾える生テキスト走査で、パーサが解釈しない断片（コメント
# 崩れ・CDATA 等）に残る表記を拾う防御多層として残す。
INLINE_HANDLER_RE = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
INLINE_HANDLER_ATTR_RE = re.compile(r"^on[a-z]+$", re.IGNORECASE)
# CSS の url(...) トークン抽出（引用付き/無引用を別分岐でパースする。引用付きは値中の
# `)` を含み得るため単一の文字クラスでは正しく抽出できない）。@import 以外にも
# @font-face の src や background 等の url(https://...) が外部依存の経路になるため、
# CSS 内の全 url(...) を検査対象にする（validate_slides.py と対の実装）。
CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)""", re.IGNORECASE)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# data: URI は受動的な画像 MIME に限り許可（それ以外の data: MIME は untrusted な
# 能動コンテンツを埋め込める経路のため不許可）。
DATA_URI_ALLOWED_RE = re.compile(r"^\s*data:image/(png|jpeg|gif|webp)[;,]", re.IGNORECASE)
# 任意 scheme の検出（http(s) に限定しない）。javascript: や blob: 等も含め、
# scheme 付きの参照はすべて外部扱いにする（data: のみ上の allowlist で先に判定）。
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
# 生テキストへの直接表記検査。wireframe は <script> 自体を全面禁止にしたため
# （find_disallowed_refs の "script" 検出が主ゲート）、本リストは防御多層。
# window['eval'](...) 等のプロパティアクセス表記や 'ev'+'al' の文字列連結は regex では
# 原理的に捕捉できないが、script 全面禁止と validate_slides.py の実行時 route 遮断が
# その経路を閉じる。
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


class SpecError(Exception):
    """spec の構造・内容が契約を満たさない場合に送出する。"""


def classify_ref(value: str) -> str:
    """リソース参照値を "ok" / "external" / "relative" / "bad-data" に分類する。

    pitch deck は単一 HTML 配布が契約（wireframe は srcdoc 埋め込み・画像は data URI）
    のため、許可は文書内 #fragment と許可画像 MIME の data: URI のみ。http(s) に限らず
    任意 scheme・protocol-relative（//）を外部扱いにし、ローカル相対参照も配布時に
    ファイルが欠落するため不許可にする（fail-closed。validate_slides.py と対の実装）。
    """
    v = value.strip().strip("\"'")
    if not v or v.startswith("#"):
        return "ok"
    if v.lower().startswith("data:"):
        return "ok" if DATA_URI_ALLOWED_RE.match(v) else "bad-data"
    if v.startswith("//") or SCHEME_RE.match(v):
        return "external"
    return "relative"


def srcset_candidates(value: str) -> list[str]:
    """srcset 属性値から検査対象の URL 候補を取り出す。

    候補の分割を「comma + 空白」に限定するのは、data URI
    （data:image/png;base64,AA）の base64 区切り comma を候補区切りと誤認して
    後続を相対参照と誤検出しないため。空白を伴わない comma で外部 URL が
    紛れ込む変則表記は、値全体への protocol(-relative) URL 走査で検出する
    （validate_slides.py と対の実装）。
    """
    refs: list[str] = []
    for candidate in SRCSET_SPLIT_RE.split(value):
        parts = candidate.strip().split()
        if parts:
            refs.append(parts[0])
    for m in SRCSET_SMUGGLED_URL_RE.finditer(value):
        refs.append(m.group(1))
    return refs


class _RefCollector(HTMLParser):
    """wireframe 生 HTML のタグ属性からリソース参照値（RESOURCE_ATTRS / srcset）と
    iframe srcdoc（入れ子の HTML 文書）を収集する収集器（build 側の事前検査用）。

    属性検査を正規表現の全文走査にすると <script> 内の `var data = ...` のような
    JS 代入まで属性として誤検出するため、タグ属性のみを構造的に取り出す。
    srcdoc の値は HTML エスケープ済みで生テキスト走査に露出しないため、unescape 済みの
    属性値として収集し find_disallowed_refs 側で再帰解析する。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.srcdocs: list[str] = []          # iframe srcdoc（find_disallowed_refs が再帰解析する）
        self.script_tags = 0                  # <script> の出現数（wireframe では全面禁止）
        self.inline_handlers: list[str] = []  # on* 属性の出現箇所（引用形式を問わず検出）
        self.styles: list[str] = []           # <style> の本文（CSS url()/@import 検査の対象）
        self.style_attrs: list[str] = []      # style="..." 属性値（同上）
        self._in: str | None = None
        self._buf = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.script_tags += 1
        for name, value in attrs:
            v = value or ""
            if INLINE_HANDLER_ATTR_RE.match(name):
                # regex（INLINE_HANDLER_RE）は引用符付きしか拾えないため、
                # `<body onload=alert(1)>` の引用符なし表記もここで構造的に検出する
                self.inline_handlers.append(f"<{tag} {name}=...>")
            elif name in RESOURCE_ATTRS:
                self.refs.append(v)
            elif name == "srcset":
                # srcset は「URL 幅記述子, URL 幅記述子, ...」形式のため候補ごとに取り出す
                self.refs.extend(srcset_candidates(v))
            elif name == "srcdoc":
                self.srcdocs.append(v)
            elif name == "style":
                self.style_attrs.append(v)
        if tag in ("script", "style"):
            self._in, self._buf = tag, ""

    def handle_data(self, data: str) -> None:
        if self._in == "style":
            self._buf += data

    def handle_endtag(self, tag: str) -> None:
        if self._in == tag:
            if tag == "style":
                self.styles.append(self._buf)
            self._in, self._buf = None, ""

    def close(self) -> None:
        super().close()
        # 閉じタグを欠いた <style> も fail-closed で検査対象に含める
        if self._in:
            if self._in == "style":
                self.styles.append(self._buf)
            self._in, self._buf = None, ""

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # ブラウザは HTML の script/style で self-closing スラッシュ（<style/> 等）を
        # 無視して開始タグとして扱う。validate_slides.py の _SlideAuditor と挙動を
        # 揃え、script/style は endtag 副作用なしの開始タグ扱いに固定する
        # （属性収集は handle_starttag 側で行われるため取りこぼしも生じない）。
        if tag in ("script", "style"):
            self.handle_starttag(tag, attrs)
        else:
            super().handle_startendtag(tag, attrs)


def find_disallowed_refs(text: str) -> dict[str, list[str]]:
    """wireframe 生 HTML から自己完結契約に違反する参照を抽出する。

    リソース属性（src / href / xlink:href / data / poster / action / formaction）と
    リソース属性・srcset の候補 URL・<style> 本文・style 属性値を _RefCollector で
    構造的に収集し、classify_ref で分類して "ok" 以外を種類別に返す。
    CSS の url(...) / @import 検査は収集した <style> 本文・style 属性値に限定する:
    HTML 全文への適用だと、可視テキスト中の「url(https://...)」のような説明文だけで
    deck 生成が拒否される誤検知があるため（CSS コメント内の例示は除去してから検査。
    srcdoc 内も末尾の再帰解析で同じ限定検査になる）。<script> の出現も "script" として
    返す（wireframe は静的ワイヤーフレームで script の正当用途がないため全面禁止。
    window['eval'](...) 等のプロパティアクセス表記は文字列パターン検査で原理的に
    捕捉しきれないため、識別子検査ではなく script 自体を拒否して fail-closed にする）。
    """
    bad: dict[str, list[str]] = {
        "external": [], "relative": [], "bad-data": [], "script": [], "handler": [],
        "import": [],
    }

    def record(value: str) -> None:
        # srcset の候補走査と変則表記走査が重なった場合等の重複報告を避ける
        kind = classify_ref(value)
        if kind != "ok" and value not in bad[kind]:
            bad[kind].append(value)

    collector = _RefCollector()
    collector.feed(text)
    collector.close()
    for ref in collector.refs:
        record(ref)
    if collector.script_tags:
        bad["script"].append(f"<script> x{collector.script_tags}")
    for handler in collector.inline_handlers:
        if handler not in bad["handler"]:
            bad["handler"].append(handler)

    css_texts = [CSS_COMMENT_RE.sub("", t) for t in collector.styles + collector.style_attrs]
    for t in css_texts:
        for m in CSS_URL_RE.finditer(t):
            record(next(g for g in m.groups() if g is not None))
    if any(CDN_IMPORT_RE.search(t) for t in css_texts):
        bad["import"].append("@import")

    # iframe srcdoc は HTML エスケープ済みの入れ子文書のため、生テキスト走査では
    # 属性も CSS url() も露出しない。unescape 済みの値を独立した HTML 文書として
    # 再帰解析し、ネストされた srcdoc の参照も含めて build 時点で拒否する
    # （validate_slides.py の _audit と同じ方針）。
    for sub_doc in collector.srcdocs:
        for kind, values in find_disallowed_refs(sub_doc).items():
            for v in values:
                if v not in bad[kind]:
                    bad[kind].append(v)
    return bad


def check_embeddable_html(text: str, label: str) -> None:
    """screen_flow.wireframe として埋め込む前の生 HTML テキストを検査する。

    自己完結契約（外部 URL・ローカル相対参照・CDN import 不在。単一 HTML 配布のため
    許可は #fragment と許可画像 MIME の data: URI のみ）と inline JS の安全性
    （AGENTS.md P0: eval・innerHTML代入・inline handler・network API 不使用）を
    満たさない wireframe は埋め込まず SpecError で拒否する。
    """
    violations = []
    bad_refs = find_disallowed_refs(text)
    if bad_refs["external"]:
        violations.append(
            "外部リソース参照（src / href / xlink:href / data / poster / action / "
            "formaction / srcset 属性、または CSS url()。任意 scheme・// を含む）: "
            + ", ".join(bad_refs["external"][:3])
        )
    if bad_refs["relative"]:
        violations.append(
            "ローカル相対参照: " + ", ".join(bad_refs["relative"][:3])
            + "（単一 HTML ファイル配布で参照先が欠落するため禁止。画像は data URI で埋め込むこと）"
        )
    if bad_refs["bad-data"]:
        violations.append(
            "png/jpeg/gif/webp 以外の data: URI（能動コンテンツを埋め込める経路のため不許可）"
        )
    if bad_refs["script"]:
        violations.append(
            "<script> 要素: " + ", ".join(bad_refs["script"][:3])
            + "（wireframe は静的ワイヤーフレームのため script は全面禁止。"
            "window['eval'] 等の表記迂回を含め fail-closed に拒否する。"
            "スポットライト演出は build_slides.py が埋め込み時に注入する）"
        )
    if bad_refs["import"]:
        violations.append("CSS @import による外部リソース参照（<style> / style 属性内）")
    if bad_refs["handler"]:
        violations.append(
            "inline event handler 属性（引用符の有無を問わず検出）: "
            + ", ".join(bad_refs["handler"][:3])
        )
    elif INLINE_HANDLER_RE.search(text):
        # 防御多層: パーサが解釈しない断片（コメント崩れ等）に残る引用符付き表記を拾う
        violations.append("inline event handler 属性（onclick= 等）")
    for pattern, name in FORBIDDEN_JS_PATTERNS:
        if pattern.search(text):
            violations.append(f"禁止された JavaScript パターン: {name}")
    if violations:
        raise SpecError(
            f"{label} は自己完結・安全な JS の契約を満たさないため埋め込めない: "
            + " / ".join(violations)
        )


def load_spec(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path} が妥当な JSON でない: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecError("spec のルートは JSON object であること")
    return raw


def validate_spec(spec: dict) -> None:
    """generate 前の構造チェック。

    構成契約: 前半 [cover, premise, problem, solution, scope, winning]
    （この順・各1枚）→ screen_flow 2〜4枚（連続） → [validation, approval]
    （この順・各1枚、これで終わること）。
    """
    # brand は build_html で CSS_TEMPLATE.format() に展開されるため、値を色リテラルへ
    # 厳格に制限する（BRAND_COLOR_RE の理由コメント参照）。未知キーは build_html 側で
    # 無視されるが、注入経路を残さないよう全キーの値を一律に検査する。
    # キーの存在で判定する（`is not None` 判定だと `"brand": null` が素通りし、
    # build_html の brand.items() で AttributeError になる）。
    if "brand" in spec:
        brand = spec["brand"]
        if not isinstance(brand, dict):
            raise SpecError("spec.brand は object であること（null は不可。省略はキーごと削除する）")
        for key, value in brand.items():
            if not isinstance(value, str) or not BRAND_COLOR_RE.match(value):
                raise SpecError(
                    f"brand.{key} は #RGB / #RRGGBB（末尾に alpha 桁可）の16進カラーで"
                    f"あること（現在: {value!r}）。<style> へ展開されるため色リテラル"
                    "以外は受け付けない"
                )

    slides = spec.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SpecError("spec.slides は 1 件以上の配列であること")

    # 非 dict 要素を黙って除外すると、正規 role 一式 + 数値等の混入 JSON が順序検証を
    # 通過した後に slide.get() の AttributeError（traceback 終了）になるため、
    # 全要素の型と role の妥当性を先に fail-closed で検証する。
    for i, slide in enumerate(slides):
        if not isinstance(slide, dict):
            raise SpecError(
                f"spec.slides[{i}] は object であること（現在: {type(slide).__name__}）"
            )
        role = slide.get("role")
        if role not in ROLE_RENDERERS:
            raise SpecError(
                f"spec.slides[{i}].role が不正: {role!r}"
                f"（許可: {sorted(ROLE_RENDERERS)}）"
            )

    roles = [s["role"] for s in slides]

    prefix = roles[: len(FRONT_HALF_ROLES)]
    if prefix != FRONT_HALF_ROLES:
        raise SpecError(
            f"前半 {len(FRONT_HALF_ROLES)}枚は role={FRONT_HALF_ROLES} の順であること"
            f"（現在: {prefix}）"
        )

    idx = len(FRONT_HALF_ROLES)
    n_flow = 0
    while idx < len(roles) and roles[idx] == SCREEN_FLOW_ROLE:
        n_flow += 1
        idx += 1
    if not (SCREEN_FLOW_MIN <= n_flow <= SCREEN_FLOW_MAX):
        raise SpecError(
            f"role={SCREEN_FLOW_ROLE}（画面と操作の流れ）は"
            f"{SCREEN_FLOW_MIN}〜{SCREEN_FLOW_MAX}枚連続で配置すること"
            f"（現在 {n_flow}枚）"
        )

    suffix = roles[idx:]
    if suffix != BACK_HALF_TAIL_ROLES:
        raise SpecError(
            f"role={SCREEN_FLOW_ROLE} の直後は role={BACK_HALF_TAIL_ROLES} の順で"
            f"終わること（現在: {suffix}）"
        )

    for slide in slides:
        role = slide.get("role")
        title = slide.get("title")
        if not title or not isinstance(title, str):
            raise SpecError(f"role={role} に title (string) が必要")

        if role == "cover":
            # 任意フィールドも型は保証する。truthy な数値・object が通過すると
            # render_cover の "  |  ".join(meta_bits) 等で TypeError になる
            for key in ("subtitle", "date", "meta"):
                value = slide.get(key)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise SpecError(
                        f"role=cover.{key} は非空文字列であること（現在: {value!r}。"
                        "省略はキーごと削除する）"
                    )
        elif role in ("premise", "problem", "solution", "validation"):
            _require_str_list(slide, "bullets", role, min_len=1)
        elif role == "scope":
            _require_str_list(slide, "in_scope", role, min_len=1)
            _require_str_list(slide, "out_scope", role, min_len=1)
        elif role == "winning":
            items = slide.get("items")
            if not isinstance(items, list) or not items:
                raise SpecError("role=winning に items (1件以上) が必要")
            for item in items:
                if not isinstance(item, dict):
                    raise SpecError("winning.items の各要素は object であること")
                text = item.get("text")
                # truthy 判定だけだと text: 1 等の非文字列が通過し、
                # wrap_countup の NUMBER_RE.search(text) で TypeError になる
                if not isinstance(text, str) or not text.strip():
                    raise SpecError("winning.items の各要素は text (非空文字列) を持つこと")
                if item.get("label") not in WINNING_LABELS:
                    raise SpecError(
                        "winning.items の各要素は label に "
                        f"{sorted(WINNING_LABELS)} のいずれかを指定すること。"
                        "'事実' は入力文書に記録された実測・調査結果に限る"
                        "（留保がある場合は text に併記する）。それ以外は '仮説'"
                    )
        elif role == SCREEN_FLOW_ROLE:
            narrative = slide.get("narrative")
            if not narrative or not isinstance(narrative, str):
                raise SpecError(
                    f"role={SCREEN_FLOW_ROLE} に narrative (string) が必要。"
                    "「この場面で・この画面が・こう使われる」を説明する導入文"
                )
            wireframe = slide.get("wireframe")
            if wireframe is not None and not isinstance(wireframe, str):
                raise SpecError(f"role={SCREEN_FLOW_ROLE}.wireframe は string か null であること")
            if wireframe:
                steps = slide.get("steps")
                if not isinstance(steps, list) or not steps:
                    raise SpecError(
                        f"role={SCREEN_FLOW_ROLE} で wireframe を指定する場合、"
                        "steps ({selector, note} の配列。1件以上) が必要"
                    )
                for step in steps:
                    if not isinstance(step, dict):
                        raise SpecError(
                            f"role={SCREEN_FLOW_ROLE}.steps の各要素は "
                            "{selector, note} の object であること"
                        )
                    selector = step.get("selector")
                    note = step.get("note")
                    if (
                        not isinstance(selector, str) or not selector.strip()
                        or not isinstance(note, str) or not note.strip()
                    ):
                        raise SpecError(
                            f"role={SCREEN_FLOW_ROLE}.steps の各要素は "
                            "selector (非空の CSS セレクタ文字列) と note (非空 string) を持つこと"
                        )
                    # CSS 構文の完全検証は Python 側では行わない。実セレクタ検証は
                    # validate_slides.py が querySelector 試行で行い、実行時は
                    # スポットライト側（SPOTLIGHT_INJECTION）の try/catch が不正
                    # セレクタを安全に無視する。ここでは明らかな不正のみ拒否する
                    if any(ord(ch) < 0x20 for ch in selector):
                        raise SpecError(
                            f"role={SCREEN_FLOW_ROLE}.steps[].selector に改行・制御文字を"
                            f"含めないこと: {selector!r}"
                        )
            else:
                note = slide.get("note")
                if not note or not isinstance(note, str):
                    raise SpecError(
                        f"role={SCREEN_FLOW_ROLE} で wireframe が無い場合は note (string) が"
                        "必要。例: 'create-design-doc 未実行のためテキスト概略のみ'"
                    )
        elif role == "approval":
            items = slide.get("items")
            if not isinstance(items, list):
                raise SpecError("role=approval に items (配列) が必要")
            if not (APPROVAL_ITEM_MIN <= len(items) <= APPROVAL_ITEM_MAX):
                raise SpecError(
                    "role=approval の items は"
                    f"{APPROVAL_ITEM_MIN}〜{APPROVAL_ITEM_MAX}件であること"
                    f"（現在 {len(items)}件）"
                )
            for item in items:
                if not isinstance(item, dict):
                    raise SpecError("approval.items の各要素は object であること")
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise SpecError("approval.items の各要素は text (非空文字列) を持つこと")
                if item.get("kind") not in APPROVAL_KINDS:
                    raise SpecError(
                        f"approval.items の各要素は kind に {sorted(APPROVAL_KINDS)} の"
                        "いずれかを指定すること"
                    )


def _require_str_list(slide: dict, key: str, role: str, min_len: int = 1) -> None:
    value = slide.get(key)
    if not isinstance(value, list) or len(value) < min_len:
        raise SpecError(f"role={role} に {key} ({min_len}件以上の配列) が必要")
    for v in value:
        if not isinstance(v, str) or not v.strip():
            raise SpecError(f"role={role}.{key} の各要素は非空文字列であること")


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# 事実（label="事実"）の winning item テキスト中の最初の数値をカウントアップ演出付きの
# span で包む。ID っぽい表記（"PoC-1" 等、数字の前後が英数字/ハイフン）は対象外にする。
# 前後の判定には \w ではなく [A-Za-z0-9-] を使う: Python の re は既定で Unicode 対応の
# \w を使うため、漢字・ひらがな（例: 「8分」の「分」、「が8分」の「が」）が \w に含まれて
# しまい、日本語の数字表現をことごとく除外してしまう不具合が実測で見つかったため
# （例: 「8分」が \w 境界判定に阻まれてマッチしなかった）。
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9-])(\d{1,3}(?:,\d{3})+|\d+)(?![A-Za-z0-9])")


def wrap_countup(text: str) -> str:
    """countup 対象の数値を span で包む。初期 DOM には最終値をそのまま出力する。

    初期値を 0 にすると、JS が動かない環境や JS 未発火のまま印刷/PDF 化した場合に
    「8分」が「0分」のまま固定される。最終値を初期表示とし、フラグメントが
    is-current になった時点で JS 側（tickCountups）が 0 から最終値まで巻き上げる。
    """
    m = NUMBER_RE.search(text)
    if not m:
        return esc(text)
    prefix, number, suffix = text[: m.start()], m.group(1), text[m.end():]
    target = number.replace(",", "")
    return (
        f"{esc(prefix)}"
        f'<span class="countup" data-target="{esc(target)}">{esc(number)}</span>'
        f"{esc(suffix)}"
    )


def render_cover(slide: dict, base_dir: Path) -> str:
    parts = [f'<h1 class="cover-title">{esc(slide["title"])}</h1>']
    subtitle = slide.get("subtitle")
    if subtitle:
        parts.append(f'<p class="cover-subtitle">{esc(subtitle)}</p>')
    meta_bits = [b for b in (slide.get("date"), slide.get("meta")) if b]
    if meta_bits:
        parts.append(f'<p class="cover-meta">{esc("  |  ".join(meta_bits))}</p>')
    return f'<div class="cover">{"".join(parts)}</div>'


def render_bullets(slide: dict, base_dir: Path) -> str:
    items = "".join(f'<li class="fragment">{esc(b)}</li>' for b in slide["bullets"])
    body = f'<h2>{esc(slide["title"])}</h2><ul class="bullets">{items}</ul>'
    source_note = slide.get("source_note")
    if source_note:
        body += f'<p class="source-note">出典: {esc(source_note)}</p>'
    return body


def render_scope(slide: dict, base_dir: Path) -> str:
    in_items = "".join(f"<li>{esc(x)}</li>" for x in slide["in_scope"])
    out_items = "".join(f"<li>{esc(x)}</li>" for x in slide["out_scope"])
    return (
        f'<h2>{esc(slide["title"])}</h2>'
        '<div class="scope-grid">'
        f'<div class="scope-col fragment"><h3>In Scope</h3><ul class="bullets">{in_items}</ul></div>'
        f'<div class="scope-col scope-out fragment"><h3>Out of Scope</h3><ul class="bullets">{out_items}</ul></div>'
        "</div>"
    )


def render_winning(slide: dict, base_dir: Path) -> str:
    items = ""
    for item in slide["items"]:
        cls = "hypothesis" if item["label"] == "仮説" else "fact"
        text_html = wrap_countup(item["text"]) if item["label"] == "事実" else esc(item["text"])
        items += (
            f'<li class="winning-item fragment {cls}"><span class="badge">{esc(item["label"])}</span>'
            f"{text_html}</li>"
        )
    return f'<h2>{esc(slide["title"])}</h2><ul class="winning-list">{items}</ul>'


def render_screen_flow(slide: dict, base_dir: Path) -> str:
    wireframe = slide.get("wireframe")
    narrative = f'<p class="screen-flow-narrative-intro">{esc(slide["narrative"])}</p>'

    if wireframe:
        # spec 由来のパスをそのまま開くと、絶対パス・`../`・symlink 経由で spec 外の
        # 任意ローカルファイル（機密 HTML 等）を srcdoc として deck へ取り込めてしまう。
        # spec ディレクトリ配下の通常ファイルのみ許可する。resolve() は symlink を
        # 解決するため、resolve 後の is_relative_to 比較で symlink 経由の脱出も防げる。
        base = base_dir.resolve()
        wf_input = Path(wireframe)
        if wf_input.is_absolute():
            raise SpecError(
                "screen_flow.wireframe は spec ディレクトリ配下への相対パスで指定する"
                "こと（絶対パスは任意ローカルファイル取り込みの経路になるため不可）: "
                f"{wireframe}"
            )
        wf_path = (base / wf_input).resolve()
        if not wf_path.is_relative_to(base):
            raise SpecError(
                "screen_flow.wireframe が spec ディレクトリの外を指している"
                "（`../` や symlink による親ディレクトリ脱出は任意ローカルファイル"
                f"取り込みの経路になるため不可）: {wireframe}"
            )
        if not wf_path.is_file():
            raise SpecError(f"screen_flow.wireframe が存在しない: {wf_path}")
        wf_text = wf_path.read_text(encoding="utf-8")
        check_embeddable_html(wf_text, f"screen_flow.wireframe ({wf_path})")
        injected = wf_text.replace(
            "</body>", SPOTLIGHT_INJECTION + "</body>", 1
        ) if "</body>" in wf_text else wf_text + SPOTLIGHT_INJECTION
        visual = (
            '<div class="screen-frame">'
            f'<iframe class="screen-iframe" srcdoc="{esc(injected)}" '
            'title="画面のライブプレビュー" scrolling="no"></iframe>'
            "</div>"
        )
        steps_html = "".join(
            f'<li class="fragment step-item" data-selector="{esc(step["selector"])}">{esc(step["note"])}</li>'
            for step in slide["steps"]
        )
        steps_block = f'<ol class="screen-flow-steps">{steps_html}</ol>'
    else:
        note = esc(slide.get("note", ""))
        visual = f'<div class="screen-shot-placeholder"><p>{note}</p></div>'
        steps_block = ""

    return (
        f'<h2>{esc(slide["title"])}</h2>'
        '<div class="screen-flow-body">'
        f'<div class="screen-flow-visual">{visual}</div>'
        f'<div class="screen-flow-narrative">{narrative}{steps_block}</div>'
        "</div>"
    )


def render_approval(slide: dict, base_dir: Path) -> str:
    items = ""
    for i, item in enumerate(slide["items"], start=1):
        items += (
            f'<li class="approval-item fragment" data-kind="{esc(item["kind"])}">'
            f'<span class="approval-index">{i}</span>'
            f'<span class="badge">{esc(item["kind"])}</span>{esc(item["text"])}</li>'
        )
    return f'<h2>{esc(slide["title"])}</h2><ul class="approval-list">{items}</ul>'


ROLE_RENDERERS = {
    "cover": render_cover,
    "premise": render_bullets,
    "problem": render_bullets,
    "solution": render_bullets,
    "scope": render_scope,
    "winning": render_winning,
    "screen_flow": render_screen_flow,
    "validation": render_bullets,
    "approval": render_approval,
}

# wireframe の srcdoc へ注入するスポットライト機構。
# 親ドキュメントから iframe.contentWindow.__pitchHighlight(selector) を呼ぶと、
# (a) 直前のハイライトを解除し (b) 対象要素に .{"__pitch_spotlight"} を付与して
# z-index で dim オーバーレイの上に持ち上げ (c) 画面全体を薄暗くする dim オーバーレイを
# 追加し (d) 対象要素を scrollIntoView する。addEventListener/classList/DOM 操作のみで
# 完結し、eval・innerHTML 代入・inline handler・network API を一切使わない。
SPOTLIGHT_INJECTION = """
<style id="__pitch_spotlight_style">
.__pitch_dim{position:fixed;inset:0;background:rgba(6,8,20,.55);z-index:99998;pointer-events:none;}
.__pitch_spotlight{position:relative !important;z-index:99999 !important;outline:3px solid #F5C242;outline-offset:4px;box-shadow:0 0 0 8px rgba(245,194,66,.22),0 0 24px 4px rgba(245,194,66,.5);border-radius:6px;}
</style>
<script>
(function(){
  window.__pitchHighlight = function(selector){
    var prevDim = document.querySelector('.__pitch_dim');
    if(prevDim){ prevDim.parentNode.removeChild(prevDim); }
    var prevTarget = document.querySelector('.__pitch_spotlight');
    if(prevTarget){ prevTarget.classList.remove('__pitch_spotlight'); }
    if(!selector){ return; }
    var target = null;
    /* 不正な CSS セレクタ（"[" 等）の DOMException でナビゲーション全体が停止しない
       よう安全に無視する（validate_slides.py が不正セレクタを FAIL として報告する） */
    try { target = document.querySelector(selector); } catch (err) { return; }
    if(!target){ return; }
    var dim = document.createElement('div');
    dim.className = '__pitch_dim';
    document.body.appendChild(dim);
    target.classList.add('__pitch_spotlight');
    target.scrollIntoView({behavior:'smooth', block:'center'});
  };
})();
</script>
"""


CSS_TEMPLATE = """
:root {{
  --color-bg: {bg};
  --color-surface: {surface};
  --color-fg: {fg};
  --color-muted: {muted};
  --color-border: {border};
  --color-primary: {primary};
  --color-accent: {accent};
  --color-success: {success};
  --color-warning: {warning};
  --color-danger: {danger};
  --font-family: {font_family};
  --font-mono: {font_mono};
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0;
  height: 100%;
  overflow: hidden;
  background: var(--color-bg);
  color: var(--color-fg);
  font-family: var(--font-family);
}}
.deck {{ position: relative; width: 100vw; height: 100vh; }}
.slide {{
  position: absolute;
  inset: 0;
  display: none;
  flex-direction: column;
  justify-content: center;
  padding: 104px 96px 56px;
  opacity: 0;
  transform: translateX(32px);
  transition: opacity .35s ease, transform .35s ease;
}}
.slide.active {{ display: flex; opacity: 1; transform: none; }}
.slide h1, .slide h2 {{ font-weight: 800; line-height: 1.15; margin: 0 0 28px; }}
.slide h2 {{ font-size: clamp(28px, 4.2vw, 52px); }}

/* ---------- cover 入場演出 ---------- */
.cover {{ text-align: left; }}
.cover-title, .cover-subtitle, .cover-meta {{ opacity: 0; transform: translateY(14px); }}
.slide-cover.active .cover-title {{ animation: coverIn .55s ease forwards; }}
.slide-cover.active .cover-subtitle {{ animation: coverIn .55s ease .12s forwards; }}
.slide-cover.active .cover-meta {{ animation: coverIn .55s ease .22s forwards; }}
@keyframes coverIn {{ to {{ opacity: 1; transform: none; }} }}
.cover-title {{ font-size: clamp(40px, 7vw, 84px); margin: 0 0 20px; }}
.cover-subtitle {{ font-size: clamp(16px, 2vw, 24px); color: var(--color-muted); margin: 0 0 16px; }}
.cover-meta {{ font-family: var(--font-mono); font-size: 13px; color: var(--color-muted); }}

/* ---------- フラグメント（ステップ送り）の3状態 ---------- */
.fragment {{ opacity: .16; transition: opacity .35s ease, transform .35s ease; }}
.fragment.is-current {{ opacity: 1; }}
.fragment.is-done {{ opacity: .5; }}

.bullets {{ list-style: none; margin: 0; padding: 0; font-size: clamp(16px, 2vw, 26px); line-height: 1.7; }}
.bullets li {{ margin-bottom: 14px; padding-left: 1.4em; position: relative; }}
.bullets li::before {{ content: "—"; position: absolute; left: 0; color: var(--color-primary); transition: color .3s; }}
.bullets li.fragment.is-current::before {{ content: "▶"; color: var(--color-accent); }}
.source-note {{ margin-top: 24px; font-size: 13px; color: var(--color-muted); }}

.scope-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 32px; }}
.scope-col {{ background: var(--color-surface); border: 1px solid var(--color-border); border-radius: 12px; padding: 24px; transform: translateY(10px); }}
.scope-col.is-current, .scope-col.is-done {{ transform: none; }}
.scope-col h3 {{ margin: 0 0 12px; font-size: 18px; color: var(--color-primary); }}
.scope-out h3 {{ color: var(--color-muted); }}

.winning-list {{ list-style: none; margin: 0; padding: 0; font-size: clamp(16px, 2vw, 24px); line-height: 1.7; }}
.winning-item {{ margin-bottom: 16px; }}
.winning-item.hypothesis {{ color: var(--color-accent); }}
.countup {{ font-variant-numeric: tabular-nums; font-weight: 800; }}
.badge {{
  display: inline-block; font-family: var(--font-mono); font-size: 11px; letter-spacing: .08em;
  padding: 2px 8px; margin-right: 10px; border-radius: 4px; border: 1px solid currentColor; vertical-align: middle;
}}

/* ---------- screen_flow: 実寸ワイヤーフレーム + スポットライト ---------- */
.screen-flow-body {{ display: flex; gap: 40px; align-items: stretch; flex: 1; min-height: 0; }}
.screen-flow-visual {{ flex: 0 0 56%; display: flex; align-items: center; justify-content: center; min-width: 0; }}
.screen-frame {{
  position: relative; width: 100%; max-width: 720px; aspect-ratio: 1440 / 900;
  overflow: hidden; border: 1px solid var(--color-border); border-radius: 10px;
  background: #fff; box-shadow: 0 18px 40px rgba(0,0,0,.35);
}}
.screen-iframe {{
  position: absolute; top: 0; left: 0; width: 1440px; height: 900px; border: 0;
  transform-origin: top left;
  /* 実寸(1440x900)を .screen-frame の実表示幅に合わせて縮小する。実測倍率は
     JS の fitScreenFrame がアクティブ表示時に inline style で上書きする
     （CSS だけでは親要素の実測ピクセル幅を参照できないため）。ここでの
     scale(0.5) は max-width:720px 時の既定倍率で、JS 未発火のスライド
     （印刷時の未訪問スライド等）でも左上だけ切れた表示にならないための保険。 */
  transform: scale(0.5);
}}
.screen-shot-placeholder {{
  width: 100%; height: 240px; border: 1px dashed var(--color-border); border-radius: 8px;
  display: flex; align-items: center; justify-content: center; color: var(--color-muted);
  font-size: 14px; text-align: center; padding: 16px;
}}
.screen-flow-narrative {{ flex: 1 1 auto; min-width: 0; }}
.screen-flow-narrative-intro {{ font-size: clamp(15px, 1.8vw, 20px); line-height: 1.7; margin: 0 0 20px; color: var(--color-muted); }}
.screen-flow-steps {{ list-style: none; margin: 0; padding: 0; counter-reset: step; }}
.screen-flow-steps .step-item {{
  counter-increment: step; margin-bottom: 14px; padding-left: 2em; position: relative;
  font-size: clamp(15px, 1.8vw, 20px); line-height: 1.6;
}}
.screen-flow-steps .step-item::before {{
  content: counter(step); position: absolute; left: 0; top: 0; width: 1.5em; height: 1.5em;
  border-radius: 50%; border: 1.5px solid var(--color-border); color: var(--color-muted);
  display: flex; align-items: center; justify-content: center; font-family: var(--font-mono); font-size: 12px;
  transition: border-color .3s, color .3s, background .3s;
}}
.screen-flow-steps .step-item.is-current::before {{
  border-color: var(--color-accent); color: var(--color-bg); background: var(--color-accent); font-weight: 800;
}}

/* ---------- approval ---------- */
.approval-list {{ list-style: none; margin: 0; padding: 0; font-size: clamp(16px, 2vw, 24px); line-height: 1.8; }}
.approval-item {{ margin-bottom: 18px; display: flex; align-items: baseline; gap: 4px; }}
.approval-index {{ font-family: var(--font-mono); color: var(--color-muted); margin-right: 8px; }}
.slide-approval {{ background: var(--color-surface); }}

/* ---------- HUD / progress / nav ---------- */
.topbar {{
  position: absolute; top: 0; left: 0; right: 0; height: 64px;
  display: flex; align-items: center; justify-content: space-between; padding: 0 40px; pointer-events: none;
}}
.topbar .role-label {{
  font-family: var(--font-mono); font-size: 12px; letter-spacing: .28em; text-transform: uppercase; color: var(--color-muted);
}}
.slide.active .topbar .role-label {{ animation: labelIn .4s ease; }}
@keyframes labelIn {{ from {{ letter-spacing: .05em; opacity: .3; }} to {{ letter-spacing: .28em; opacity: 1; }} }}
.topbar .page-num {{ font-family: var(--font-mono); font-size: 12px; color: var(--color-muted); }}

.progress {{ position: fixed; bottom: 0; left: 0; right: 0; height: 4px; display: flex; gap: 3px; padding: 0 3px; z-index: 20; }}
.progress .seg {{ flex: 1; background: var(--color-border); border-radius: 2px; overflow: hidden; }}
.progress .seg-fill {{ width: calc(var(--fill, 0) * 1%); height: 100%; background: var(--color-accent); transition: width .25s ease; }}
.progress .seg.filled .seg-fill {{ width: 100%; }}

.navbtn {{
  position: fixed; top: 50%; transform: translateY(-50%); width: 48px; height: 48px; border-radius: 50%;
  border: 1px solid var(--color-border); background: var(--color-surface); color: var(--color-fg);
  font-size: 20px; cursor: pointer; z-index: 20; display: flex; align-items: center; justify-content: center;
}}
.navbtn:hover {{ border-color: var(--color-primary); }}
#prev-btn {{ left: 24px; }}
#next-btn {{ right: 24px; }}

@media print {{
  .topbar, .progress, .navbtn {{ display: none !important; }}
  html, body {{ height: auto; overflow: visible; }}
  .deck {{ position: static; width: auto; height: auto; }}
  .slide {{
    position: static !important; display: flex !important; opacity: 1 !important; transform: none !important;
    width: 100vw; height: 100vh; page-break-after: always;
  }}
  .slide:last-child {{ page-break-after: auto; }}
  /* 印刷時は「今どのステップか」を無視し全フラグメントを最終状態にする */
  .fragment {{ opacity: 1 !important; transform: none !important; }}
  /* cover の入場演出は opacity:0 始まりで .slide-cover.active の animation でしか
     可視化されないため、印刷では animation を切って最終状態を強制する
     （さもないと表紙のタイトル・サブタイトル・メタ情報が空白ページになる） */
  .cover-title, .cover-subtitle, .cover-meta {{
    opacity: 1 !important; transform: none !important; animation: none !important;
  }}
  /* 印刷では fitScreenFrame（アクティブ時のみ動作）が未訪問スライドに効かないため、
     フレーム幅を既定倍率 scale(0.5) と対応する 720px に固定し、実寸 1440x900 の
     iframe が overflow:hidden で切れずに収まることを保証する */
  .screen-frame {{ width: 720px; max-width: 720px; flex: none; }}
  .screen-iframe {{ transform: scale(0.5) !important; }}
  @page {{ size: landscape; margin: 0; }}
}}

@media (prefers-reduced-motion: reduce) {{
  * {{ animation-duration: .001s !important; transition-duration: .001s !important; }}
}}
"""

# 保守方針: JS は addEventListener / classList / textContent のみで完結させ、
# eval・new Function・untrusted innerHTML 代入・inline onclick 属性・
# network API（fetch/XMLHttpRequest/WebSocket/EventSource/sendBeacon）を
# 一切使わない（AGENTS.md P0 の inline JavaScript 逸脱に抵触しないため）。
# validate_slides.py はこの契約を出力 HTML から機械検証する。
JS_TEMPLATE = """
(function () {
  var slides = document.querySelectorAll('.slide');
  var segs = document.querySelectorAll('.progress .seg');
  var total = slides.length;
  var cur = 0;
  var step = 0;

  function fragCount(i) { return slides[i].querySelectorAll('.fragment').length; }

  function fitScreenFrame(slide) {
    var frame = slide.querySelector('.screen-frame');
    var iframe = slide.querySelector('.screen-iframe');
    if (!frame || !iframe) { return; }
    var scale = frame.clientWidth / 1440;
    iframe.style.transform = 'scale(' + scale + ')';
  }

  function highlightScreenFlow(slide, s) {
    var iframe = slide.querySelector('.screen-iframe');
    if (!iframe || !iframe.contentWindow || !iframe.contentWindow.__pitchHighlight) { return; }
    var stepItems = slide.querySelectorAll('.step-item');
    var sel = null;
    if (s >= 1 && stepItems[s - 1]) { sel = stepItems[s - 1].getAttribute('data-selector'); }
    iframe.contentWindow.__pitchHighlight(sel);
  }

  function render() {
    slides.forEach(function (s, i) { s.classList.toggle('active', i === cur); });
    var active = slides[cur];
    var frags = active.querySelectorAll('.fragment');
    frags.forEach(function (el, idx) {
      var n = idx + 1;
      el.classList.toggle('is-current', n === step);
      el.classList.toggle('is-done', n < step);
    });
    if (active.dataset.role === 'screen_flow') {
      fitScreenFrame(active);
      highlightScreenFlow(active, step);
    }
    segs.forEach(function (seg, idx) {
      seg.classList.toggle('filled', idx < cur);
      if (idx === cur) {
        var f = fragCount(cur);
        var pct = f > 0 ? Math.round((step / f) * 100) : 100;
        seg.style.setProperty('--fill', pct);
      } else if (idx > cur) {
        seg.style.setProperty('--fill', 0);
      }
    });
  }

  function next() {
    var f = fragCount(cur);
    if (step < f) { step += 1; render(); }
    else if (cur < total - 1) { cur += 1; step = 0; render(); }
  }

  function prev() {
    if (step > 0) { step -= 1; render(); }
    else if (cur > 0) { cur -= 1; step = fragCount(cur); render(); }
  }

  function resetToStart() { cur = 0; step = 0; render(); }

  document.addEventListener('keydown', function (e) {
    // PageDown/PageUp はプレゼン用リモコン（ページ送りキーを送出する機種が多い）
    // 対応。references/deck-spec.md「HTML スライドの操作仕様」の契約に含まれる。
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === ' ' || e.key === 'PageDown') { e.preventDefault(); next(); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'PageUp') { e.preventDefault(); prev(); }
    else if (e.key === 'r' || e.key === 'R') { resetToStart(); }
  });

  var prevBtn = document.getElementById('prev-btn');
  var nextBtn = document.getElementById('next-btn');
  if (prevBtn) { prevBtn.addEventListener('click', prev); }
  if (nextBtn) { nextBtn.addEventListener('click', next); }

  window.addEventListener('resize', function () {
    var active = slides[cur];
    if (active.dataset.role === 'screen_flow') { fitScreenFrame(active); }
  });

  render();

  // countup: フラグメントが is-current になった時点で発火する軽量カウントアップ。
  // 初期 DOM には最終値が入っている（wrap_countup 参照。JS 未発火の印刷/PDF でも
  // 最終値が出るようにするため）。発火時に 0 から最終値まで巻き上げ直す。
  // MutationObserver ではなく render() 呼び出し直後に毎回全走査する単純な実装にして
  // タイミングのズレを避ける（要素数が少ないため毎回の全走査でも軽量）。
  var animatedCountups = new WeakSet();
  function tickCountups() {
    document.querySelectorAll('.fragment.is-current .countup').forEach(function (el) {
      if (animatedCountups.has(el)) { return; }
      animatedCountups.add(el);
      var target = parseInt(el.getAttribute('data-target'), 10) || 0;
      var startTime = null;
      var duration = 600;
      function step(ts) {
        if (startTime === null) { startTime = ts; }
        var p = Math.min(1, (ts - startTime) / duration);
        el.textContent = Math.round(target * p).toLocaleString('ja-JP');
        if (p < 1) { requestAnimationFrame(step); }
      }
      requestAnimationFrame(step);
    });
  }
  var originalRender = render;
  render = function () { originalRender(); tickCountups(); };
  tickCountups();
})();
"""


def build_html(spec: dict, base_dir: Path, theme_name: str) -> str:
    theme_defaults = DARK_THEME if theme_name == "dark" else LIGHT_THEME
    brand = spec.get("brand", {})
    theme = {**theme_defaults, **{k: v for k, v in brand.items() if k in theme_defaults}}

    slides = spec["slides"]
    total = len(slides)
    slide_html_parts = []
    for idx, slide_spec in enumerate(slides):
        role = slide_spec["role"]
        renderer = ROLE_RENDERERS[role]
        inner = renderer(slide_spec, base_dir)
        role_label = ROLE_LABELS[role]
        page_label = f"{idx + 1} / {total}"
        active_cls = " active" if idx == 0 else ""
        slide_html_parts.append(
            f'<section class="slide slide-{role}{active_cls}" data-role="{role}" data-index="{idx}">'
            f'<div class="topbar"><span class="role-label">{esc(role_label)}</span>'
            f'<span class="page-num">{esc(page_label)}</span></div>'
            f"{inner}"
            "</section>"
        )

    segs_html = "".join('<div class="seg"><div class="seg-fill"></div></div>' for _ in range(total))
    css = CSS_TEMPLATE.format(
        bg=theme["bg"], surface=theme["surface"], fg=theme["fg"], muted=theme["muted"],
        border=theme["border"], primary=theme["primary"], accent=theme["accent"],
        success=theme["success"], warning=theme["warning"], danger=theme["danger"],
        font_family=FONT_FAMILY, font_mono=FONT_MONO,
    )
    title = esc(spec.get("title", ""))

    return f"""<!doctype html>
<html lang="ja" data-theme="{esc(theme_name)}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="deck">{"".join(slide_html_parts)}</div>
<div class="progress">{segs_html}</div>
<button id="prev-btn" class="navbtn" aria-label="前のスライド">&#8249;</button>
<button id="next-btn" class="navbtn" aria-label="次のスライド">&#8250;</button>
<script>{JS_TEMPLATE}</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="deck spec (JSON) から自己完結 HTML スライドを生成する")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--theme", choices=["dark", "light"], default="dark")
    args = parser.parse_args()

    try:
        spec = load_spec(args.spec)
        validate_spec(spec)
        html_out = build_html(spec, args.spec.resolve().parent, args.theme)
    except SpecError as exc:
        print(f"SpecError: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_out, encoding="utf-8")
    size_kb = args.output.stat().st_size / 1024
    print(f"生成完了: {args.output}（{size_kb:.1f} KB、{len(spec['slides'])}枚、theme={args.theme}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
