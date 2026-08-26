# deck spec スキーマ

`scripts/build_slides.py` が受け取る JSON spec の仕様。renderer は本スキーマの spec を
自己完結・フルスクリーンの HTML スライド（単一ファイル）へ変換する。
`scripts/validate_slides.py` は生成された `.html` 自体を Playwright で操作しながら
検証する（spec は信頼しない）。演出の仕組みの背景は
[references/presentation-patterns.md](presentation-patterns.md) を参照する。

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
`success` / `warning` / `danger`。値は `#RGB` / `#RRGGBB`（末尾に alpha 桁を付けた
`#RGBA` / `#RRGGBBAA` も可）の16進カラーのみ。値は `<style>` 内へそのまま展開されるため、
色リテラル以外の文字列は `build_slides.py` が `SpecError` で拒否する。

## スライド構成（PO 承認会向け・前半固定＋後半可変）

`slides` は次の順序契約を満たす（`build_slides.py` の `validate_spec` が強制する）。

| 区間 | role | 枚数 | 必須フィールド | 備考 |
|---|---|---|---|---|
| 前半（この順・固定） | `cover` | 1 | `title` | `subtitle` / `date` / `meta` は任意。フラグメント無し（0ステップ） |
| | `premise` | 1 | `title`, `bullets`（1件以上） | **前提と解釈**。`source_note` 任意。各 bullet が1フラグメント |
| | `problem` | 1 | `title`, `bullets`（1件以上） | 課題。各 bullet が1フラグメント |
| | `solution` | 1 | `title`, `bullets`（1件以上） | 解決アプローチ。各 bullet が1フラグメント |
| | `scope` | 1 | `title`, `in_scope`, `out_scope`（各1件以上） | 対象範囲。In/Out の2ブロックがそれぞれ1フラグメント（計2フラグメント） |
| | `winning` | 1 | `title`, `items`（各 `{text, label}`） | 勝ち筋。`label` は `事実`/`仮説`。各 item が1フラグメント |
| 後半（`screen_flow` は連続） | `screen_flow` | **2〜4** | `title`, `narrative`, `wireframe`（string \| null）, `wireframe` があれば `steps` 必須、無ければ `note` 必須 | **画面と操作の流れ**。1枚＝1シナリオ場面。`steps[]` の各要素が1フラグメント |
| | `validation` | 1 | `title`, `bullets`（1件以上） | 検証計画・現在地。各 bullet が1フラグメント |
| | `approval` | 1 | `title`, `items`（**3〜5件**、各 `{text, kind}`） | **承認いただきたい事項・確認事項**。`kind` は `承認`/`確認`。各 item が1フラグメント |

**フラグメント数は spec に明示せず、要素数から導出する**（`screen_flow` の `steps` のみ
明示的な配列を持つ。それ以外は `bullets`/`items`/In・Out ブロックの数がそのままフラグメント
数になる）。

### `winning.items[].label` の定義（重要）

- `"事実"`: 入力文書に**記録された実測・調査結果**に限る。数値・出典が無い場合は使わない。
  留保（サンプルサイズが小さい等）がある場合は `text` に併記する。`事実` の item に含まれる
  最初の数値は自動でカウントアップ演出付きの span に包まれる（ID 表記等の誤検出を避けるため
  数字の前後が半角英数字・ハイフンの場合は対象外にする）
- `"事実"` に該当しない主張（推測・期待・一般論）はすべて `"仮説"` にする

### `screen_flow` の `wireframe` / `steps` / `note`

- `wireframe`: `create-design-doc` が生成した `wireframes/*.html` への相対パス（spec ファイル
  からの相対）または絶対パス。`build_slides.py` が読み込み、**`<iframe srcdoc="...">` として
  実寸（1440×900）でレンダリング**し、`.screen-frame` の表示幅に合わせて JS が
  `transform: scale()` で縮小表示する（静止画は使わない。PO 指示）
- `steps`: `wireframe` を指定する場合は**1件以上必須**。各要素は
  `{"selector": "<CSS セレクタ>", "note": "<説明文>"}`。フラグメント送りでステップが
  current になるたびに、対応する `selector` の要素へ iframe 内でスポットライト
  （dim オーバーレイ＋対象への outline/glow）を当て、`scrollIntoView` で見える位置へ
  寄せる。右側の `note` リストも同じフラグメント機構で連動する
- `wireframe` が `null`（`create-design-doc` 未実行等で画面素材が無い場合）: `note`
  （例: `"create-design-doc 未実行のためテキスト概略のみ"`）を必須にする。この場合
  フラグメントは0（`steps` を持たない）

wireframe ファイルは埋め込み前に自己完結性・inline JS 安全性（外部 URL・CDN import・
inline event handler・`eval`/`innerHTML`代入/network API の不在）を生テキストの時点で検査し、
違反があれば `SpecError` で拒否する（HTML エスケープ後は正規表現での検出が効かなくなるため、
必ず埋め込み前に検査する）。

role の順序契約違反・`screen_flow` の連続枚数逸脱・`approval` の件数逸脱・
`winning.items[].label` 不正・`screen_flow` の `wireframe`/`steps`/`note` 欠落・wireframe の
自己完結性違反は `build_slides.py` が日本語 `SpecError`（終了コード1）で拒否する。

## HTML スライドの操作仕様

生成される `.html` は次のインタラクションを持つ単一の自己完結ファイル（CDN・外部フォント・
外部画像なし。`screen_flow` の wireframe は `srcdoc` として同一文書内に埋め込む）。

- フルスクリーン・1スライド＝1画面（`100vh`・スクロールなし）
- **スライド内フラグメント送り**: `→`/`Space`/`PageDown` は「現在スライドに残っている
  フラグメントがあれば1つ進める、尽きていれば次スライドへ」の2段階。`←`/`PageUp` は逆順
  （フラグメント0の状態で戻ると前スライドの最終フラグメント状態へ戻る）
- フラグメントは出現前（暗い・opacity .16）→現在（強調・opacity 1・箇条書きは "▶" マーカー）
  →既読（中間の減光・opacity .5）の3状態を持つ
- `R`: 先頭スライド（`cover`）の先頭フラグメントへリセット
- クリック: 画面左右の丸ボタン（`#prev-btn` / `#next-btn`）
- 上部バー: 左に区分ラベル（`COVER` / `PROBLEM` 等、モノスペース・字間広め）、右に `n / N`
- 下部: スライド枚数分のセグメント型プログレスバー。現在スライドのセグメントはフラグメント
  進捗（`step / 総フラグメント数`）に応じて内側が塗り足されていく
- 印刷/PDF: `@media print` で1スライド＝1ページ、**全フラグメントが最終状態（opacity:1）**
  で表示され、上部バー・プログレスバー・ナビボタンは非表示
- inline JavaScript は `addEventListener` / `classList` / `querySelector` のみで完結し、
  `eval`・`new Function`・untrusted な `innerHTML` 代入・inline event handler 属性・
  ネットワーク API（`fetch` 等）を一切使わない（`screen_flow` の wireframe に注入する
  スポットライト用スクリプトも同じ制約に従う）

## 検証ルール（`validate_slides.py` が html から直接確認）

- スライド枚数・role の順序契約（DOM の `data-role` 属性から判定）
- 全スライド・全フラグメントステップを実際に `→` キーで遷移させながら、各ステップで
  表示中スライド自身（`.slide.active`）の `scrollHeight`/`scrollWidth` が
  `clientHeight`/`clientWidth`（= 1440×900 の viewport）を超えていないか
- `screen_flow` の各フラグメントステップで、iframe 内に**期待した1件だけ**スポットライト
  （`.__pitch_spotlight`）が付与されているか（0件＝セレクタ不一致、2件以上＝解除漏れを検出）
- `screen_flow` の iframe 内 HTML にも外部リソース参照・危険な JS パターンが無いか
  （親 HTML のチェックとは独立に、iframe の実際にレンダリングされた DOM を検査する）
- 外部 URL 参照・CDN import・許可されない MIME の `data:` URI がゼロ件（親 HTML）
- inline event handler 属性・`eval`/`new Function`/`innerHTML`代入/network API がゼロ件（親 HTML）
- 末尾を超えて `→` しても `approval` に留まる（clamp）、`R` で `cover` に戻る、クリックで
  `premise` へ進む、フラグメント0で `←` すると前スライドへ戻る
- 2枚目に「前提」が含まれる、最終スライドに「承認」と3〜5件の `.approval-item` が含まれる
- `@media print` でナビゲーション要素が非表示になり、全フラグメントの `opacity` が `1` になる

各ステップの PNG（`slide-<n(2桁)>-step-<s>.png`。フラグメント0枚のスライドは
`slide-<n(2桁)>-<role>.png`）を `--screenshots-dir` に撮影する。

## 最小例

[samples/pitch-deck-sample.json](../samples/pitch-deck-sample.json)（架空アイデア
「おつかいパンダ便」を題材にした記入例。`screen_flow` の wireframe あり×2／なし×1を含み、
[samples/wireframes/](../samples/wireframes/) に埋め込み用のサンプルワイヤーフレームを
同梱する）を参照。
