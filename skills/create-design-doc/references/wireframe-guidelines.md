# ワイヤーフレーム作成ガイドライン

`wireframes/*.html` を作成する際の規約。`templates/wireframe-template.html` をコピーして
使う。

## 位置づけ（レビューの主役ではない）

`wireframes/*.html` と `screens/*.png` は**実装用素材**として存続する（実装フェーズでの
そのまま流用・詳細な状態確認に使う）。**レビュー（PO 承認会・フィードバック取得）の主役は
`storyboard.png`**（[design-doc-structure.md](design-doc-structure.md) 参照）であり、
wireframes は storyboard を組み立てるための部品という位置づけになる。ユーザーに提示する際は
まず storyboard.png を見せ、実装詳細を確認したい場合のみ個別の wireframe/screenshot を参照
する、という順で案内する。

## 必須要件

- **自己完結**: 外部 CDN・外部フォント・外部画像・外部 JavaScript library を使用しない。
  `<link rel="stylesheet" href="https://...">` 等は禁止（`scripts/check_overflow.py` が
  検出する）
- **JavaScript 全面禁止**: `<script>` は本文の有無を問わず置かない（srcdoc 内も同様）。
  inline event handler 属性（onclick= 等）・`javascript:` URL も禁止。ワイヤーフレームは
  静的な見た目の表現のみで完結させ、状態差は画面を分けて表現する
  （`scripts/check_overflow.py` が検出し、違反時はブラウザ実行前に FAIL する）
- **インライン CSS**: `<style>` 内にすべて記述する。別ファイルの `.css` を参照しない
- **デザイントークンを CSS カスタムプロパティで埋め込む**: `:root` に
  [references/design-doc-structure.md](design-doc-structure.md) の色・タイポ・余白を定義し、
  以降のスタイルはすべてトークン経由で参照する（直接カラーコードを書かない）
- **PC/モバイル両対応のレスポンシブ**: `@media (max-width: 480px)` 等のブレークポイントで
  1カラム化・ナビゲーション変更等を行う。`scripts/check_overflow.py` で両 viewport の横
  スクロール崩れが無いことを確認する
- **ダミーデータは具体的でリアルな内容にする**: 「サンプル」「テスト」「Lorem ipsum」等の
  placeholder 文字列を使わない。実際にありそうな商品名・金額・日付・ユーザー名を書く
  - 悪い例: `<h2>商品名サンプル</h2>` `<p>テストテキスト</p>`
  - 良い例: `<h2>ハンドメイド真鍮ピアス（波紋）</h2>` `<p>在庫3点・BASE/STORES 同期済み</p>`

## 画面ごとに明記すること

- 主要な状態（空・読込中・エラー・正常）をコメントまたは別ブロックで示す
- 主要操作の遷移先（例: 「クリックで詳細画面へ」）をコメントで示す
- アクセシビリティの基本（見出し階層・フォーカス可能な操作要素の視認性）を損なわない

## position: sticky / fixed を使わない

下部ナビ等を `position: sticky` / `fixed` にすると、Playwright の full-page screenshot は
撮影時に viewport 高さを一時的に拡張して撮るため、sticky/fixed 要素が文書の実際の末尾では
なく元の viewport 高さの位置に取り残され、キャプチャ結果でナビが中途半端な高さに浮いて
見える不具合が実走で確認されている。下部ナビ等は通常フロー内の静的配置にする
（`templates/wireframe-template.html` 参照）。

## モバイルスクリーンショットの目視確認観点

`check_overflow.py` は横スクロールの有無しか機械検証できない。PASS した後も、モバイル
（375px）のスクリーンショットを必ず目視し、以下を確認する。

- `position: sticky` / `fixed` を使った要素が意図しない高さに浮いていないか（上記参照）
- テキストの折り返しで文字が重なったり、ボタン内の文字がはみ出したりしていないか
- カード・テーブル等が極端に潰れて崩れていないか（表は横スクロール用ラッパーで囲む等）
- タップ対象（ボタン・リンク）が隣接要素と密着しすぎていないか

## Figma ではなく HTML を採用する理由（再掲）

Figma はファイル生成 API が無くエージェント生成に不向き。HTML はブラウザ確認・スクショ
資料化・実装への直接流用が可能なため採用する（詳細は SKILL.md 冒頭）。

## 検証コマンド

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/check_overflow.py" <wireframe.html>
python3 "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" --html <wireframe.html> --out <out-desktop.png> --width 1440 --height 900 --full-page
python3 "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" --html <wireframe.html> --out <out-mobile.png> --width 375 --height 812 --full-page
```
