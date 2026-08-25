#!/usr/bin/env python3
"""deck spec (JSON) から自己完結 HTML フルスクリーンスライド（1ファイル）を生成する renderer。

役割: report spec -> HTML の create-html-report/scripts/render_report.py と対になる
存在。spec の構造検証・HTML/CSS/JS 組み立て・画像の base64 data URI 埋め込みは
本スクリプトの責務とし、呼び出し側 (SKILL.md の手順) は spec の内容判断にのみ
集中できるようにする。PPTX 版（旧 build_deck.py）は廃止した。

スライド構成: 前半 = 課題・解決アプローチ・スコープ・勝ち筋、後半 = 画面と操作の
流れ（screen_flow, 2〜4枚。create-design-doc の screens/storyboard 画像を
base64 data URI で取り込む）→ 検証計画 → 承認いただきたい事項・確認事項。

依存: 標準ライブラリのみ（Pillow 等の画像ライブラリ不要。PNG 幅は IHDR チャンクを
自前でパースして読む）。生成された HTML の検証には別スクリプト validate_slides.py
（Playwright 必須）を使う。
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import struct
import sys
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

# 埋め込み画像の上限。create-design-doc の既定キャプチャ幅 1440px を基準に、
# 若干の余裕（retina 相当の再エンコード等）を見て 1600px までを許容する。
# 超過時は Pillow 等に依存したリサイズをせず、SpecError で再キャプチャを促す
# （依存追加より、埋め込みサイズの責務を呼び出し側に戻す方が安全なため）。
MAX_IMAGE_WIDTH_PX = 1600
MAX_IMAGE_BYTES = 2_000_000  # 2MB

DARK_THEME = {
    "bg": "#0B1220",
    "surface": "#141C2E",
    "fg": "#F5F7FA",
    "muted": "#8B93A7",
    "border": "#26304A",
    "primary": "#5B8CFF",
    "accent": "#F5C242",
    "success": "#3DDC84",
    "warning": "#F5C242",
    "danger": "#FF6B6B",
}
LIGHT_THEME = {
    "bg": "#FFFFFF",
    "surface": "#F4F6F8",
    "fg": "#1C1C1C",
    "muted": "#5B6470",
    "border": "#D8DEE6",
    "primary": "#1F3A93",
    "accent": "#E08E45",
    "success": "#1E824C",
    "warning": "#B7791F",
    "danger": "#C0392B",
}
FONT_FAMILY = '"Noto Sans JP", "Hiragino Sans", "Yu Gothic", sans-serif'
FONT_MONO = '"Roboto Mono", "SFMono-Regular", Consolas, monospace'


class SpecError(Exception):
    """spec の構造・内容が契約を満たさない場合に送出する。"""


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
    slides = spec.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SpecError("spec.slides は 1 件以上の配列であること")

    roles = [s.get("role") for s in slides if isinstance(s, dict)]

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
            f"（現在 {n_flow}枚）。PO 承認会でシナリオごとに画面の使われ方を説明する枠"
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

        if role == "premise":
            _require_str_list(slide, "bullets", role, min_len=1)
        elif role in ("problem", "solution", "validation"):
            _require_str_list(slide, "bullets", role, min_len=1)
        elif role == "scope":
            _require_str_list(slide, "in_scope", role, min_len=1)
            _require_str_list(slide, "out_scope", role, min_len=1)
        elif role == "winning":
            items = slide.get("items")
            if not isinstance(items, list) or not items:
                raise SpecError("role=winning に items (1件以上) が必要")
            for item in items:
                if not isinstance(item, dict) or not item.get("text"):
                    raise SpecError("winning.items の各要素は text を持つこと")
                label = item.get("label")
                if label not in WINNING_LABELS:
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
                    "「この場面で・この画面が・こう使われる」を説明する文"
                )
            image = slide.get("image")
            if image is not None and not isinstance(image, str):
                raise SpecError(f"role={SCREEN_FLOW_ROLE}.image は string か null であること")
            if not image:
                note = slide.get("note")
                if not note or not isinstance(note, str):
                    raise SpecError(
                        f"role={SCREEN_FLOW_ROLE} で image が無い場合は note (string) が必要。"
                        "例: 'create-design-doc 未実行のためテキスト概略のみ'"
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
                if not isinstance(item, dict) or not item.get("text"):
                    raise SpecError("approval.items の各要素は text を持つこと")
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


def read_png_size(path: Path) -> tuple[int, int]:
    """PNG の IHDR チャンクから (width, height) を読む。Pillow 非依存。"""
    with path.open("rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise SpecError(f"{path} は PNG シグネチャを持たない（PNG 形式のみ埋め込み可）")
        f.read(4)  # chunk length
        chunk_type = f.read(4)
        if chunk_type != b"IHDR":
            raise SpecError(f"{path} の先頭チャンクが IHDR でない（不正な PNG）")
        width, height = struct.unpack(">II", f.read(8))
        return width, height


def embed_image_data_uri(path: Path) -> str:
    if not path.is_file():
        raise SpecError(f"screen_flow.image で指定されたファイルが存在しない: {path}")
    size_bytes = path.stat().st_size
    if size_bytes > MAX_IMAGE_BYTES:
        raise SpecError(
            f"{path} のファイルサイズが上限を超える（{size_bytes}バイト > "
            f"{MAX_IMAGE_BYTES}バイト）。create-design-doc で 1440px 幅程度に再キャプチャ"
            "するか圧縮してから指定すること"
        )
    width, _ = read_png_size(path)
    if width > MAX_IMAGE_WIDTH_PX:
        raise SpecError(
            f"{path} の画像幅が上限を超える（{width}px > {MAX_IMAGE_WIDTH_PX}px）。"
            "create-design-doc の capture_screenshot.py で --width 1440 程度に"
            "再キャプチャしてから指定すること"
        )
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def resolve_image_path(image: str, base_dir: Path) -> Path:
    p = Path(image)
    return p if p.is_absolute() else (base_dir / p)


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
    items = "".join(f"<li>{esc(b)}</li>" for b in slide["bullets"])
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
        f'<div class="scope-col"><h3>In Scope</h3><ul class="bullets">{in_items}</ul></div>'
        f'<div class="scope-col scope-out"><h3>Out of Scope</h3><ul class="bullets">{out_items}</ul></div>'
        "</div>"
    )


def render_winning(slide: dict, base_dir: Path) -> str:
    items = ""
    for item in slide["items"]:
        cls = "hypothesis" if item["label"] == "仮説" else "fact"
        items += (
            f'<li class="winning-item {cls}"><span class="badge">{esc(item["label"])}</span>'
            f"{esc(item['text'])}</li>"
        )
    return f'<h2>{esc(slide["title"])}</h2><ul class="winning-list">{items}</ul>'


def render_screen_flow(slide: dict, base_dir: Path) -> str:
    image = slide.get("image")
    if image:
        data_uri = embed_image_data_uri(resolve_image_path(image, base_dir))
        visual = f'<img class="screen-shot" src="{data_uri}" alt="{esc(slide["title"])}の画面" />'
    else:
        note = esc(slide.get("note", ""))
        visual = f'<div class="screen-shot-placeholder"><p>{note}</p></div>'
    return (
        f'<h2>{esc(slide["title"])}</h2>'
        '<div class="screen-flow-body">'
        f'<div class="screen-flow-visual">{visual}</div>'
        f'<div class="screen-flow-narrative"><p>{esc(slide["narrative"])}</p></div>'
        "</div>"
    )


def render_approval(slide: dict, base_dir: Path) -> str:
    items = ""
    for i, item in enumerate(slide["items"], start=1):
        items += (
            f'<li class="approval-item" data-kind="{esc(item["kind"])}">'
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
}}
.slide.active {{ display: flex; }}
.slide h1, .slide h2 {{
  font-weight: 800;
  line-height: 1.15;
  margin: 0 0 28px;
}}
.slide h2 {{ font-size: clamp(28px, 4.2vw, 52px); }}
.cover {{ text-align: left; }}
.cover-title {{ font-size: clamp(40px, 7vw, 84px); margin: 0 0 20px; }}
.cover-subtitle {{ font-size: clamp(16px, 2vw, 24px); color: var(--color-muted); margin: 0 0 16px; }}
.cover-meta {{ font-family: var(--font-mono); font-size: 13px; color: var(--color-muted); }}
.bullets {{ list-style: none; margin: 0; padding: 0; font-size: clamp(16px, 2vw, 26px); line-height: 1.7; }}
.bullets li {{ margin-bottom: 14px; padding-left: 1.4em; position: relative; }}
.bullets li::before {{ content: "—"; position: absolute; left: 0; color: var(--color-primary); }}
.source-note {{ margin-top: 24px; font-size: 13px; color: var(--color-muted); }}
.scope-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 32px; }}
.scope-col {{ background: var(--color-surface); border: 1px solid var(--color-border); border-radius: 12px; padding: 24px; }}
.scope-col h3 {{ margin: 0 0 12px; font-size: 18px; color: var(--color-primary); }}
.scope-out h3 {{ color: var(--color-muted); }}
.winning-list {{ list-style: none; margin: 0; padding: 0; font-size: clamp(16px, 2vw, 24px); line-height: 1.7; }}
.winning-item {{ margin-bottom: 16px; }}
.winning-item.hypothesis {{ color: var(--color-accent); }}
.badge {{
  display: inline-block;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  padding: 2px 8px;
  margin-right: 10px;
  border-radius: 4px;
  border: 1px solid currentColor;
  vertical-align: middle;
}}
.screen-flow-body {{ display: flex; gap: 40px; align-items: center; flex: 1; min-height: 0; }}
.screen-flow-visual {{ flex: 0 0 46%; max-height: 60vh; display: flex; align-items: center; justify-content: center; }}
.screen-shot {{ max-width: 100%; max-height: 60vh; border: 1px solid var(--color-border); border-radius: 8px; }}
.screen-shot-placeholder {{
  width: 100%; height: 240px; border: 1px dashed var(--color-border); border-radius: 8px;
  display: flex; align-items: center; justify-content: center; color: var(--color-muted);
  font-size: 14px; text-align: center; padding: 16px;
}}
.screen-flow-narrative {{ flex: 1 1 auto; font-size: clamp(16px, 2vw, 24px); line-height: 1.7; }}
.approval-list {{ list-style: none; margin: 0; padding: 0; font-size: clamp(16px, 2vw, 24px); line-height: 1.8; }}
.approval-item {{ margin-bottom: 18px; display: flex; align-items: baseline; gap: 4px; }}
.approval-index {{ font-family: var(--font-mono); color: var(--color-muted); margin-right: 8px; }}
.slide-approval {{ background: var(--color-surface); }}

.topbar {{
  position: absolute; top: 0; left: 0; right: 0; height: 64px;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 40px; pointer-events: none;
}}
.topbar .role-label {{
  font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.28em;
  text-transform: uppercase; color: var(--color-muted);
}}
.topbar .page-num {{ font-family: var(--font-mono); font-size: 12px; color: var(--color-muted); }}

.progress {{
  position: fixed; bottom: 0; left: 0; right: 0; height: 4px;
  display: flex; gap: 3px; padding: 0 3px; z-index: 20;
}}
.progress .seg {{ flex: 1; background: var(--color-border); border-radius: 2px; }}
.progress .seg.filled {{ background: var(--color-accent); }}

.navbtn {{
  position: fixed; top: 50%; transform: translateY(-50%);
  width: 48px; height: 48px; border-radius: 50%;
  border: 1px solid var(--color-border); background: var(--color-surface);
  color: var(--color-fg); font-size: 20px; cursor: pointer; z-index: 20;
  display: flex; align-items: center; justify-content: center;
}}
.navbtn:hover {{ border-color: var(--color-primary); }}
#prev-btn {{ left: 24px; }}
#next-btn {{ right: 24px; }}

@media print {{
  .topbar, .progress, .navbtn {{ display: none !important; }}
  html, body {{ height: auto; overflow: visible; }}
  .deck {{ position: static; width: auto; height: auto; }}
  .slide {{
    position: static !important; display: flex !important;
    width: 100vw; height: 100vh; page-break-after: always;
  }}
  .slide:last-child {{ page-break-after: auto; }}
  @page {{ size: landscape; margin: 0; }}
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
  var current = 0;

  function goTo(i) {
    if (i < 0) { i = 0; }
    if (i > total - 1) { i = total - 1; }
    slides[current].classList.remove('active');
    current = i;
    slides[current].classList.add('active');
    segs.forEach(function (seg, idx) {
      seg.classList.toggle('filled', idx <= current);
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { goTo(current + 1); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { goTo(current - 1); }
    else if (e.key === 'r' || e.key === 'R') { goTo(0); }
  });

  var prevBtn = document.getElementById('prev-btn');
  var nextBtn = document.getElementById('next-btn');
  if (prevBtn) { prevBtn.addEventListener('click', function () { goTo(current - 1); }); }
  if (nextBtn) { nextBtn.addEventListener('click', function () { goTo(current + 1); }); }

  goTo(0);
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

    segs_html = "".join('<div class="seg"></div>' for _ in range(total))
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
