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
| `slides` | ✅ | array | [スライド定義](#スライド定義)。9種の role を過不足なく1回ずつ含むこと |

`brand` のキー: `primary` / `secondary` / `accent` / `background` / `surface` / `text` / `muted`（すべて `#RRGGBB`）、`font_latin` / `font_ea`（typeface 名。既定 `Arial` / `Noto Sans JP`）。

## スライド定義

`slides` は以下 9 role を**過不足なく1回ずつ**、**この順序**で含む（`build_deck.py` の `validate_spec` が強制する）。

| 順序 | role | 必須フィールド | 備考 |
|---|---|---|---|
| 1 | `cover` | `title` | `subtitle` / `date` / `meta` は任意 |
| 2 | `premise` | `title`, `bullets`（1件以上） | **前提と解釈**。入力文書の解釈をここで明示する契約。`source_note` 任意 |
| 3 | `problem` | `title`, `bullets`（1件以上） | 誰の・どんな痛み・なぜ今、を bullets で表現 |
| 4 | `solution` | `title`, `bullets`（1件以上） | |
| 5 | `scope` | `title`, `in_scope`（1件以上）, `out_scope`（1件以上） | |
| 6 | `winning` | `title`, `items`（1件以上、各 `{text, label}`） | **勝ち筋**。`label` は `事実` / `仮説` のいずれか必須。根拠が文書にない主張は必ず `仮説` |
| 7 | `story` | `title`, `steps`（1件以上、各 `{title, desc?}`） | 利用ストーリー |
| 8 | `validation` | `title`, `bullets`（1件以上） | 検証計画・現在地 |
| 9 | `feedback` | `title`, `items`（**3〜5件**の文字列配列） | **フィードバック観点**。ユーザーに確認してほしい点 |

role の重複・欠落・順序違反・`feedback` の件数逸脱・`winning.items` の `label` 未指定は
`build_deck.py` が日本語 `SpecError`（終了コード1）で拒否する。

## 検証ルール（`validate_deck.py` が pptx から直接確認）

- スライド枚数: 8〜14枚
- 全 shape がスライド境界（`prs.slide_width` / `prs.slide_height` から動的取得）内に収まる
- 非空の全テキスト run に `a:latin` と `a:ea` の両方の typeface が設定されている
- 2枚目のテキストに「前提」が含まれる
- 最終スライドのテキストに「フィードバック」が含まれ、`^\d+\.` 形式の番号付き項目が3〜5件

## 最小例

[samples/pitch-deck-sample.json](../samples/pitch-deck-sample.json) を参照。
