# deck spec スキーマ

`scripts/build_slides.py` が受け取る JSON spec の仕様。renderer は本スキーマの spec を
自己完結・フルスクリーンの HTML スライド（単一ファイル）へ変換する。
`scripts/validate_slides.py` は生成された `.html` 自体を Playwright で操作しながら
検証する（spec は信頼しない）。

## 実行方法

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/build_slides.py" --spec <spec.json> --output <out.html> --theme dark
python3 "${CLAUDE_SKILL_DIR}/scripts/validate_slides.py" <out.html> --screenshots-dir <dir>   # 生成後必ず実行
```

## トップレベル構造

| フィールド | 必須 | 型 | 意味 |
|---|---|---|---|
| `title` | ✅ | string | デッキ全体のタイトル（`<title>` に使用） |
| `brand` | — | object | テーマトークンの上書き。省略時は `--theme` の既定値（`DARK_THEME`/`LIGHT_THEME`）をそのまま使う |
| `slides` | ✅ | array | 後述の「スライド構成」節の順序契約を過不足なく満たすこと |

`brand` のキー（省略した項目のみ `--theme` の既定値を使う）: `primary` / `accent` /
`success` / `warning` / `danger`（すべて `#RRGGBB`）。`bg` / `surface` / `fg` / `muted` /
`border` も上書き可能だが、通常は `--theme dark|light` の切替に任せる。

## スライド構成（PO 承認会向け・前半固定＋後半可変）

`slides` は次の順序契約を満たす（`build_slides.py` の `validate_spec` が強制する）。**枚数は
10〜14枚**（前半6枚＋固定末尾2枚＋`screen_flow` 2〜4枚）。

| 区間 | role | 枚数 | 必須フィールド | 備考 |
|---|---|---|---|---|
| 前半（この順・固定） | `cover` | 1 | `title` | `subtitle` / `date` / `meta` は任意 |
| | `premise` | 1 | `title`, `bullets`（1件以上） | **前提と解釈**。`source_note` 任意 |
| | `problem` | 1 | `title`, `bullets`（1件以上） | 課題 |
| | `solution` | 1 | `title`, `bullets`（1件以上） | 解決アプローチ |
| | `scope` | 1 | `title`, `in_scope`, `out_scope`（各1件以上） | 対象範囲 |
| | `winning` | 1 | `title`, `items`（各 `{text, label}`） | 勝ち筋。`label` は `事実`/`仮説` |
| 後半（`screen_flow` は連続） | `screen_flow` | **2〜4** | `title`, `narrative`, `image`（string \| null）, `image` が null の場合は `note` 必須 | **画面と操作の流れ**。1枚＝1シナリオ場面 |
| | `validation` | 1 | `title`, `bullets`（1件以上） | 検証計画・現在地 |
| | `approval` | 1 | `title`, `items`（**3〜5件**、各 `{text, kind}`） | **承認いただきたい事項・確認事項**。`kind` は `承認`/`確認` |

### `winning.items[].label` の定義（重要）

- `"事実"`: 入力文書に**記録された実測・調査結果**に限る。数値・出典が無い場合は使わない。
  留保（サンプルサイズが小さい等）がある場合は `text` に併記する
- `"事実"` に該当しない主張（推測・期待・一般論）はすべて `"仮説"` にする

### `screen_flow` の `image` / `note`（base64 data URI 埋め込み）

- `image`: `create-design-doc` が生成した `screens/<screen>-desktop.png` 等への相対パス
  （spec ファイルからの相対）または絶対パス。`build_slides.py` が読み込み、
  **base64 data URI としてスライド HTML に直接埋め込む**（外部ファイル参照にしない。
  自己完結契約のため）
- 埋め込み上限: **幅 1600px 以下・ファイルサイズ 2MB 以下**（PNG の IHDR チャンクを自前で
  パースして判定。Pillow 等の画像ライブラリには依存しない）。超過した場合は
  `build_slides.py` が `SpecError` で拒否するので、`create-design-doc` の
  `capture_screenshot.py` で `--width 1440` 程度に再キャプチャしてから指定する
- `image` が `null`（`create-design-doc` 未実行等で画面素材が無い場合）: `note`
  （例: `"create-design-doc 未実行のためテキスト概略のみ"`）を必須にする

role の順序契約違反・`screen_flow` の連続枚数逸脱・`approval` の件数逸脱・
`winning.items[].label` 不正・`screen_flow` の `image`/`note` 欠落・画像サイズ超過は
`build_slides.py` が日本語 `SpecError`（終了コード1）で拒否する。

## HTML スライドの操作仕様

生成される `.html` は次のインタラクションを持つ単一の自己完結ファイル（CDN・外部フォント・
外部画像なし）。

- フルスクリーン・1スライド＝1画面（`100vh`・スクロールなし）
- キーボード: `→`/`↓` で次へ、`←`/`↑` で前へ、`R` で先頭（`cover`）へ
- クリック: 画面左右の丸ボタン（`#prev-btn` / `#next-btn`）
- 上部バー: 左に区分ラベル（`COVER` / `PROBLEM` 等、モノスペース・字間広め）、右に `n / N`
- 下部: スライド枚数分のセグメント型プログレスバー（現在位置までがアクセント色）
- 印刷/PDF: `@media print` で1スライド＝1ページ、上部バー・プログレスバー・ナビボタンは
  非表示（`window.print()` またはブラウザの「PDF に保存」で配布用 PDF 化できる）
- inline JavaScript は `addEventListener` / `classList` のみで完結し、`eval`・
  `new Function`・untrusted な `innerHTML` 代入・inline event handler 属性・
  ネットワーク API（`fetch` 等）を一切使わない

## 検証ルール（`validate_slides.py` が html から直接確認）

- スライド枚数・role の順序契約（DOM の `data-role` 属性から判定）
- 全スライドを実際に `→` キーで遷移させながら、各スライド自身（`.slide.active`）の
  `scrollHeight`/`scrollWidth` が `clientHeight`/`clientWidth`（= 1440×900 の viewport）を
  超えていないか（`document.documentElement` ではなく、表示中の `.slide` 要素自身で判定する
  ことが重要。`html`/`body` の `overflow: hidden` により documentElement 側の scrollHeight は
  内部コンテンツが溢れても変化しないため）
- 外部 URL 参照・CDN import・許可されない MIME の `data:` URI がゼロ件
- inline event handler 属性・`eval`/`new Function`/`innerHTML`代入/network API がゼロ件
- 末尾を超えて `→` しても `approval` に留まる（clamp）、`R` で `cover` に戻る、クリックで
  `premise` へ進む
- 2枚目に「前提」が含まれる、最終スライドに「承認」と3〜5件の `.approval-item` が含まれる
- `@media print` で上部バー・プログレスバー・ナビボタンが `display: none` になる

## 最小例

[samples/pitch-deck-sample.json](../samples/pitch-deck-sample.json)（架空アイデア
「おつかいパンダ便」を題材にした記入例。`screen_flow` の画像あり／なし両パターンを含む）
を参照。
