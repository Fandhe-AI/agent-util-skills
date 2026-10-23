---
name: actions-usage-dashboard
description: >
  org の GitHub Actions 利用分数ダッシュボード用データ取得スキル。「Actions の使用量を集計して」
  「課金分数を日次で出して」「billing usage を取得して」で使用。billing usage API から
  GitHub ホストランナーの日 x リポジトリ x SKU x 料金区分（paid / included / public_free / unknown）の行を
  JSON へ書く。fetch_billing.py を提供。ダッシュボード HTML 化は create-html-report も参照。
user-invocable: true
---

# actions-usage-dashboard

org の Actions 利用分数を billing usage API から取得し、日次の行データ（JSON）へ正規化する。現状は最初のデータ源 `scripts/fetch_billing.py` のみを提供する。

## 使い方

以下のコマンドの `<skill-root>` は、この SKILL.md が置かれたディレクトリ（導入先により `skills/` / `.agents/skills/` / `.claude/skills/` 配下の `actions-usage-dashboard`）の絶対パスに読み替える。cwd 直下の `skills/` を前提にしない。

```bash
python3 <skill-root>/scripts/fetch_billing.py \
  --org <org> --since YYYY-MM [--until YYYY-MM] --out <出力 JSON>
```

- `--until` の既定は UTC の当月。`--since <= --until <= 当月` を満たすこと
- 前提: `gh` が認証済みで、org の billing 閲覧権限があること

## フロー

### Step 1: 引数の確認

org 名と期間を確認する。`--out` の親ディレクトリは存在している必要がある。

### Step 2: 取得

上記コマンドを実行する。月ごとに取得し、いずれかの月で失敗した場合は部分ファイルを書かず非 0 で終了する（既存の出力も壊さない）。

### Step 3: 出力の確認

標準出力の要約（行数・区分別分数・月別合計）を確認する。出力 JSON は `schema_version: 1` で、行は `day / repo / visibility / sku / runner_type / charge_class / minutes / quota_minutes / billable_minutes / gross_usd / discount_usd / net_usd` を持つ。

- 有料分は公開範囲ではなく元明細ごとの `netAmount / pricePerUnit` から求める（単価の異なる明細を平均して割らない）
- `quota_minutes` は Linux 単価（前月引き継ぎ）で換算した含有枠消費分。paid 行は 0、included 行は含有枠に対応する金額分のみを換算する
- 非有料分は既定で `unknown`（`quota_minutes` は 0）。API が返すのは現在の公開範囲だけで、公開範囲を途中で変えたリポジトリの過去分を誤分類しないため。期間中に公開範囲が変わっていないと保証できる場合だけ `--assume-current-visibility` を付けると、現在値で `public` は `public_free`、`private` / `internal` は `included` に確定する（現在の公開範囲を引けない明細は指定時も `unknown` のまま）
- セルフホストランナーは対象外（jobs 側で扱う）

## 検証

```bash
python3 -m pytest -q <skill-root>/tests
```

全件パス・失敗 0 件であることを確認する。

## 注意事項

- このスキルはネットワーク越しの GitHub 操作（`gh api`）を必須とする。該当コマンドはコマンド単位で sandbox 無効にして実行する（sandbox 内ではネットワークが遮断され `gh api` が失敗するため）。ネットワーク遮断を解除できない環境では実行できない
- トークンは扱わない。`gh api` 経由のみで、標準ライブラリだけで動く
- コミット・PR は導入先リポジトリのコミット規約に従う
