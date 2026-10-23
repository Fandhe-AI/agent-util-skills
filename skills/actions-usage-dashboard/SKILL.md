---
name: actions-usage-dashboard
description: >
  org の GitHub Actions 利用分数ダッシュボード用データ取得スキル。「Actions の使用量を集計して」
  「課金分数を日次で出して」「billing usage を取得して」で使用。billing usage API から
  GitHub ホストランナーの日 x リポジトリ x SKU x 料金区分（paid / included / public_free / unknown）の行を
  JSON へ書く。「セルフホストの実行時間を集計して」で jobs API から日 x リポジトリ x ランナーグループの行も作る。
  「無料枠の残りを確認して」「Actions の利用状況を確認して」でターミナルに 20 行以内の要約と
  セルフホスト移行候補を出す。fetch_billing.py・fetch_jobs.py・summarize.py を提供。ダッシュボード HTML 化は create-html-report も参照。
user-invocable: true
---

# actions-usage-dashboard

org の Actions 利用分数を billing usage API から取得し、日次の行データ（JSON）へ正規化する。GitHub ホスト分は `scripts/fetch_billing.py`、セルフホスト分は `scripts/fetch_jobs.py` が担当する。

データ源の項目の対応・換算式・判定ルール・権限・注意点は [`references/data-sources.md`](references/data-sources.md) を参照する。

## 使い方

以下のコマンドの `<skill-root>` は、この SKILL.md が置かれたディレクトリ（導入先により `skills/` / `.agents/skills/` / `.claude/skills/` 配下の `actions-usage-dashboard`）の絶対パスに読み替える。cwd 直下の `skills/` を前提にしない。

```bash
python3 <skill-root>/scripts/fetch_billing.py \
  --org <org> --since YYYY-MM [--until YYYY-MM] --out <出力 JSON>
```

- `--until` の既定は UTC の当月。`--since <= --until <= 当月` を満たすこと
- 前提: `gh` が認証済みで、org の billing 閲覧権限があること

セルフホストランナー分（billing usage API には含まれない）は jobs API から作る。

```bash
python3 <skill-root>/scripts/fetch_jobs.py \
  --org <org> --since YYYY-MM-DD --out <出力 JSON> --cache <キャッシュ dir>
```

- 対象は org の全リポジトリ（アーカイブ除く）。`labels` に `self-hosted` を含む完了済み job だけを `charge_class: self_hosted` の行にする（GitHub ホストは billing が正）
- `--cache` に増分キャッシュを置く。2 回目以降は新しい run の jobs だけを取る
- レート制限の残量が `--min-remaining`（既定 200）を下回ると、キャッシュを保存して終了コード 3 で止まる。`--out` は書かない。reset 後に同じコマンドで再実行すると続きから取る
- 注意: run は作成日（`created`）で絞るため、`--since` より前に作られた run を `--since` 以降に再実行した分は取れない（API に更新日での絞り込みがない）。必要なら `--since` を遡らせる
- jobs は全 attempt（`filter=all`）を job id で重複除去して数える。再実行中の run は、完了済みの旧 attempt の分を集計に残し、完了後に全 attempt を取り直す。不正な job を含む run は警告してスキップし、全体は止めない。`--since` より前に作られた run のキャッシュは実行時に削除する
- 独自グループに置いた GitHub ホストの larger runner は `self-hosted` ラベルを持たないためセルフホストに数えない（billing との二重計上を避ける）
- run 一覧はリポジトリ単位でキャッシュに保存し、レート制限で中断しても同じ UTC 日の同じ `--since` なら続きから取る（未完了 run を含むリポジトリは一覧を取り直す。再利用した一覧の取得時点は `warnings` に出る）。削除や 1,000 件上限で一覧から消えた run も、キャッシュ済みなら集計に残す

取得済みの rows JSON から、ターミナルで利用状況を確認する（check モード。読み取り専用）。

```bash
python3 <skill-root>/scripts/summarize.py --rows <billing.json> [--rows <jobs.json>] \
  [--month YYYY-MM] [--group-by charge_class|repo|runner_type|visibility] [--top N] [--plan <name>] [--json]
```

- 出力は 20 行以内（既定）。先頭に無料・有料の内訳（有料の分数と請求額、含有枠の消費量とプラン含有分に対する割合・残り、公開リポの無料分、セルフホスト分）、続けて group-by 別の上位 N 件、最後に移行候補（有料のリポを先に、最終利用日付き）を出す
- `--month` の既定は rows の最新月。`--group-by` の既定は `charge_class`、`--top` の既定は 5
- プランの含有分（Free 2,000 / Team 3,000 / Enterprise 50,000 分）は `--plan` で指定する。未指定なら `gh api orgs/<org>` でプラン名を取る。取れない・不明なときは含有分を「不明」として警告し、続行する
- `--json` は同じ内容を機械可読で出す

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
- `fetch_billing.py` はセルフホストランナーを対象外とする（`fetch_jobs.py` で扱う）

## 検証

```bash
python3 -I -B <skill-root>/tests/test_fetch_billing.py
python3 -I -B <skill-root>/tests/test_fetch_jobs.py
python3 -I -B <skill-root>/tests/test_summarize.py
```

全件パス・失敗 0 件であることを確認する。

## 注意事項

- このスキルはネットワーク越しの GitHub 操作（`gh api`）を必須とする。該当コマンドはコマンド単位で sandbox 無効にして実行する（sandbox 内ではネットワークが遮断され `gh api` が失敗するため）。ネットワーク遮断を解除できない環境では実行できない
- トークンは扱わない。`gh api` 経由のみで、標準ライブラリだけで動く
- コミット・PR は導入先リポジトリのコミット規約に従う
