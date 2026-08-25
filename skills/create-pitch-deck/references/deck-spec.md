# deck spec スキーマ

`scripts/build_deck.py` が受け取る JSON spec の仕様。renderer は本スキーマの spec を
16:9 の自己完結 PPTX へ変換する。`scripts/validate_deck.py` は生成された `.pptx` 自体を
検証する（spec は信頼しない）。

## 実行方法

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/build_deck.py" --spec <spec.json> --output <out.pptx>
python3 "${CLAUDE_SKILL_DIR}/scripts/validate_deck.py" <out.pptx>   # 生成後必ず実行
```

## トップレベル構造

| フィールド | 必須 | 型 | 意味 |
|---|---|---|---|
| `title` | ✅ | string | デッキ全体のタイトル（footer にも使用） |
| `brand` | — | object | ブランドカラー・フォント。省略時は `DEFAULT_BRAND`（ニュートラルパレット）を使用 |
| `slides` | ✅ | array | 後述の「スライド構成」節の順序契約を過不足なく満たすこと |

`brand` のキー: `primary` / `secondary` / `accent` / `background` / `surface` / `text` / `muted`（すべて `#RRGGBB`）、`font_latin` / `font_ea`（typeface 名。既定 `Arial` / `Noto Sans JP`）。

## スライド構成（PO 承認会向け・前半固定＋後半可変）

`slides` は次の順序契約を満たす（`build_deck.py` の `validate_spec` が強制する）。**枚数は
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
  （例: 「n=5 の予備調査では…」）
- `"事実"` に該当しない主張（推測・期待・一般論）はすべて `"仮説"` にする

### `screen_flow` の `image` / `note`

- `image`: `create-design-doc` が生成した `storyboard.png` の一部相当、または
  `screens/<screen>-desktop.png` 等への相対パス／絶対パス。ワイド〜横長画像・縦長画像の
  いずれもスライド内に収まるよう `add_picture_fit`（幅基準→高さ超過時は高さ基準で再配置）
  が自動調整する
- `image` が `null`（`create-design-doc` 未実行等で画面素材が無い場合）: `note`
  （例: `"create-design-doc 未実行のためテキスト概略のみ"`）を必須にする。この場合
  `narrative` はテキストのみで場面を概略する

role の順序契約違反・`screen_flow` の連続枚数逸脱・`approval` の件数逸脱・
`winning.items[].label` 不正・`screen_flow` の `image`/`note` 欠落は `build_deck.py` が
日本語 `SpecError`（終了コード1）で拒否する。

## 検証ルール（`validate_deck.py` が pptx から直接確認）

- スライド枚数: 10〜14枚
- 全 shape（画像を含む）がスライド境界（動的取得した `slide_width` / `slide_height`）内に収まる
- 全ての非空テキストに `a:latin` と `a:ea` の両方の typeface が設定されている（日本語フォント fallback）
- 2枚目に「前提」が含まれる、最終スライドに「承認」と3〜5件の番号付き承認・確認事項が含まれる

## 最小例

[samples/pitch-deck-sample.json](../samples/pitch-deck-sample.json)（架空アイデア
「おつかいパンダ便」を題材にした記入例。`screen_flow` の画像あり／なし両パターンを含む）
を参照。
