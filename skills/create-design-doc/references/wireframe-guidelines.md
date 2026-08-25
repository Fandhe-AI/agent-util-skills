# ワイヤーフレーム作成ガイドライン

`wireframes/*.html` を作成する際の規約。`templates/wireframe-template.html` をコピーして
使う。

## 必須要件

- **自己完結**: 外部 CDN・外部フォント・外部画像・外部 JavaScript library を使用しない。
  `<link rel="stylesheet" href="https://...">` 等は禁止（`scripts/check_overflow.py` が
  検出する）
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

## Figma ではなく HTML を採用する理由（再掲）

Figma はファイル生成 API が無くエージェント生成に不向き。HTML はブラウザ確認・スクショ
資料化・実装への直接流用が可能なため採用する（詳細は SKILL.md 冒頭）。

## 検証コマンド

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/check_overflow.py" <wireframe.html>
python3 "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" --html <wireframe.html> --out <out-desktop.png> --width 1440 --height 900 --full-page
python3 "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" --html <wireframe.html> --out <out-mobile.png> --width 375 --height 812 --full-page
```
