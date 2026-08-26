# design-doc.md 構成

`design/design-doc.md`（出力先は引数で変更可）が満たすべき節構成。上から順に埋める。

| # | 節 | 内容 |
|---|----|------|
| 1 | 前提と解釈 | 入力文書をどう読んだか。concept-brief.md がある場合はそれとの整合も明記 |
| 2 | 想定ユーザーと主要シナリオ | いつ・どこで・何のために使うか。concept-brief.md の「主要ユーザーシナリオ」と一致させる。ここで定義したシナリオのフェーズ区切りが storyboard.html の「フェーズ見出し」と一致すること |
| 3 | 画面一覧 | 各画面の目的・主要素・**表示要素**・状態（空・読込中・エラー等）。`wireframes/*.html` と1:1対応させる（下記フォーマット） |
| 4 | 情報設計 | 画面間の情報の親子関係・ナビゲーション構造。`flow.png` の参照を含める |
| 5 | デザイントークン | 色・タイポ・余白を CSS カスタムプロパティ形式で定義（下記フォーマット） |
| 6 | フィードバック観点 | ユーザーに確認してほしい点（3〜5個） |

## flow.png と storyboard.png の役割分担（重複させない）

| 成果物 | 役割 | レビューでの位置づけ |
|--------|------|----------------------|
| `flow.png` | 画面同士の**つながりを俯瞰**する遷移図（アクター別レーン・矢印・分岐） | 全体構造の把握用（開発者にも有用） |
| `storyboard.png` | 主要シナリオの**流れに沿って各画面を1枚ずつ詳細説明**（スクショ＋表示要素の日本語説明＋遷移条件） | **レビューの主役**。PO 承認会・フィードバック取得はこちらを軸に進める |

両者は同じ情報を重複記載しない。flow.png は構造、storyboard.png は各画面の中身と流れ、と役割を分ける。

## デザイントークンの記述フォーマット

`wireframes/*.html` の `:root` と**同じ値**を使う（doc とワイヤーフレームの実装がズレない
ようにするため）。

```css
:root {
  --color-bg: #ffffff;
  --color-surface: #f4f6f8;
  --color-fg: #1c1c1c;
  --color-muted: #5b6470;
  --color-border: #d8dee6;
  --color-primary: #1f3a93;
  --color-primary-contrast: #ffffff;
  --color-accent: #e08e45;
  --color-success: #1e824c;
  --color-warning: #b7791f;
  --color-danger: #c0392b;
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --radius-md: 8px;
  --font-family: "Noto Sans JP", "Hiragino Sans", "Yu Gothic", sans-serif;
  --font-size-sm: 13px;
  --font-size-md: 15px;
  --font-size-lg: 20px;
}
```

`--color-success` / `--color-warning` / `--color-danger` は同期状態・検証結果・エラー表示等の
状態バッジに使う（`--color-accent` は強調用の汎用アクセントで、状態の意味づけには使わない）。

色は concept-brief.md の「プロダクトのトーン＆マナー」を反映して選定する
（`create-pitch-deck` の brand 設定と主色を揃えられる場合は揃える）。

## 画面一覧テーブルの書式

「表示要素」欄は**必須**。画面に何が表示され、ユーザーが何を操作でき、状態によって何が
変わるかを日本語で簡潔に列挙する（storyboard.html の「③表示要素の日本語説明」の要約版）。

```markdown
| 画面 | 目的 | 主要素 | 表示要素 | 状態 | ワイヤーフレーム | スクリーンショット |
|------|------|--------|----------|------|------------------|---------------------|
| 在庫一覧 | 在庫の分断状況を一望する | 商品カード・同期状態バッジ | 商品名・在庫数・チャネル別同期状態（成功=緑/遅延=黄/失敗=赤）を一覧表示。カードクリックで詳細へ遷移 | 通常・空・同期エラー | [wireframes/inventory-list.html](../wireframes/inventory-list.html) | [screens/inventory-list-desktop.png](../screens/inventory-list-desktop.png) |
```

## 出力ディレクトリ構成（既定）

```text
design/
  design-doc.md
  flow.png
  storyboard.png            -- レビューの主役。1枚もの・縦長
  wireframes/
    <screen-name>.html      -- 開発用素材
  screens/
    <screen-name>-desktop.png
    <screen-name>-mobile.png
```
