<!-- source: skills/actions-usage-dashboard/scripts（fetch_billing.py / fetch_jobs.py の実装）と .agents/skills/github-docs/references/rest-api/（billing/usage.md・overview/rate-limits.md・overview/permissions.md）。公式: https://docs.github.com/en/rest/billing/usage -->
<!-- 最終確認日: 2026-10-23 -->
<!-- 取得状況: ✅ 実装（main 時点の 2 スクリプト）と同梱の github-docs 参照で確認済み / ✅ 動作確認済みの経路は classic token の admin:org のみ（org 設定の書き込みを含む広いスコープ。使い終えたら失効させる） / ⚠️ 未検証: fine-grained token の billing 用権限名（確認は #143）、請求画面と API の更新時刻の公式仕様 -->

# データ源と換算式

このスキルが扱うデータ源、項目の対応、含有枠の換算式、請求画面と数字がずれる理由、必要な権限をまとめる。
記述の正はスクリプトの実装である。仕様を変えるときはスクリプトと本書を同時に直す。

## 概要

| データ源 | 担当スクリプト | 対象 |
|---|---|---|
| billing usage API | [`fetch_billing.py`](../scripts/fetch_billing.py) | GitHub ホストランナー |
| Actions runs / jobs API | [`fetch_jobs.py`](../scripts/fetch_jobs.py) | セルフホストランナー |

2 つの対象は互いに重ならない。billing usage API にセルフホストは含まれず、jobs 側は GitHub ホストの job を行にしない。

料金区分 `charge_class` は次の 5 値である。

| 値 | 意味 | 出力元 |
|---|---|---|
| `paid` | 金額が発生した分 | billing |
| `included` | 含有枠で賄われた分（非公開・internal リポジトリ） | billing |
| `public_free` | 公開リポジトリの無料分 | billing |
| `unknown` | 利用日時点の公開範囲を確認できない非有料分（既定の非有料分） | billing |
| `self_hosted` | セルフホストランナー。GitHub への支払いなし | jobs |

## 日付と期間の規約

- 日付はすべて UTC で扱う
- billing の `day` は明細の `date` を UTC に直した日。対象月でない明細は捨てる
- jobs の `day` は job の `started_at` の UTC 日
- 期間の指定は billing が `--since` / `--until`（`YYYY-MM`、`--until` の上限は UTC の当月）、jobs が `--since`（`YYYY-MM-DD`）

## billing usage API

取得先は `GET organizations/<org>/settings/billing/usage?year=<Y>&month=<M>` で、月ごとに 1 回呼ぶ。
`product` が `actions` かつ `unitType` が `minutes` の明細だけを取り込む。
数値項目が有限の非負数でなければ取得を止める（部分ファイルは書かない）。

### 項目と出力列の対応

| API の項目 | 出力列 | 扱い |
|---|---|---|
| `date` | `day` | UTC の日に直す |
| `quantity` | `minutes` | 区分ごとに分ける |
| `grossAmount` | `gross_usd` | 定価。区分ごとに按分する |
| `discountAmount` | （直接は使わない） | 出力の `discount_usd` は `gross_usd - net_usd`（負なら 0）から求める |
| `netAmount` | `net_usd` | 有料分数の判定にも使う |
| `pricePerUnit` | （出力しない） | 有料分数の換算と Linux 単価の決定に使う |
| `repositoryName` | `repo` | 先頭の `<org>/` を外す。空なら `(unattributed)`（区分は常に `unknown`） |
| `sku` | `sku` / `runner_type` | `runner_type` は SKU を正規化して照合（`linux`+`arm` は `ubuntu-arm`、`linux` は `ubuntu`、`macos`、`windows`、該当なしは `other`） |
| `product` / `unitType` | （取り込み条件） | 上記の絞り込みだけに使う |

公開範囲 `visibility` は billing usage に無く、`orgs/<org>/repos` から取得時点の値を取る。

## 有料分と含有枠の換算式

有料分数は明細ごとに求め、そのあと (day, repo, sku) 単位にまとめる。

```text
paid_i = min(max(netAmount / pricePerUnit, 0), quantity)
```

単価の異なる明細をまとめてから平均単価で割ると有料分数が狂うため、明細ごとに求める。
有料分の `gross` は `grossAmount * paid_i / quantity` で按分する。残りが有料でない分である。

Linux 単価の決め方は次のとおり。

1. その月の正規化後の SKU が `actions linux` の明細のうち、正の `pricePerUnit` の最小値を採る
2. その月に無ければ前月の値を引き継ぐ
3. 換算が必要な行があるのに決まらなければ、エラーで止める

月ごとの単価は出力の `linux_price_per_minute` に記録される。

含有枠の消費分は次の式で求める。

```text
quota_minutes = gross_c / Linux 単価
```

`gross_c` は included 行に割り当てた定価分（有料分に按分した gross を除いた残り）である。
Windows や macOS のように単価が高い SKU は、定価を Linux 単価で割るため、その倍率がそのまま含有枠の消費に反映される。
これが「非公開リポジトリの定価 ÷ Linux 単価」の意味である。

- paid / public_free / unknown の行は `quota_minutes` が 0
- `billable_minutes` は paid 行の分数だけで、ほかの行は 0
- 含有枠の上限（請求画面の「N min included」）は契約プランで決まる値で、API の出力にもスクリプトにも含まれない。全 org 共通の数字として扱わない

## 請求画面と数字が合わない理由

- 当日分（UTC）は集計中で、あとから増える
- 請求画面と API で更新時刻がずれることがある（公式の仕様は未検証）
- 公開範囲は取得時点の値しか取れない。期間中に公開・非公開を切り替えたリポジトリは、過去月の included / public_free が実態とずれる。誤分類で quota が消えたり過大になったりするのを避けるため、既定では非有料分を `unknown` にして quota に数えない。切り替えが無いと保証できる場合だけ `--assume-current-visibility` を付ける。`public` は `public_free`、`private` / `internal` は `included` に確定する
- 削除済みのリポジトリや権限不足で公開範囲を引けないリポジトリは、オプションを付けても `unknown` のままになる
- 有料分は公開範囲ではなく `netAmount` から判定するため、この影響を受けない

## jobs API（セルフホスト）

### 判定と集計

- `labels` に `self-hosted` を含み、`status` が `completed` で、`started_at` と `completed_at` が正しい時刻の job だけを数える
- 分数は `completed_at - started_at` をそのまま使い、丸めない
- 行は日 × リポジトリ × `runner_group_name` の単位。グループ名が無ければ `(unknown)` にする
- 金額系の列と `quota_minutes` は 0、`charge_class` は `self_hosted`

### 二重計上を避ける理由

独自のランナーグループに置いた GitHub ホストの larger runner は `self-hosted` ラベルを持たず、billing usage に載る。
そのため判定はラベルだけで行い、グループ名から推定しない。GitHub ホストの job は jobs 側で行にしない。

### 取得範囲と上限

- 対象はアーカイブを除く org の全リポジトリ
- run は `created>=since` で絞る。`--since` より前に作られた run を再実行した分は取れない（更新日での絞り込みが無い）
- runs の検索結果は 1,000 件が上限。超えたときは期間を二分して取り直し、1 日でも超えるときは出力の `warnings` に載る
- jobs は `filter=all` で全 attempt を取り、job id で重複を除く。1 run あたり 100 ページまで

### キャッシュ

- 置き場所は `<--cache>/jobs-cache-<org>.json`
- jobs を取り終えた完了済みの run だけを登録する。`--since` より前に作られた run は実行時に削除する
- 中断時は run 一覧を `listing` に保存する。同じ UTC 日に同じ `--since` で再実行すると続きから取る
- キャッシュは信頼しない入力として検証し、壊れていれば止まる
- 消し方は `jobs-cache-<org>.json` のファイル 1 つだけを削除する（ディレクトリごとは消さない）。次回は全件を取り直す

### レート制限と終了コード

- `gh api rate_limit` の `resources.core.remaining` を、run やリポジトリの境目で、API 呼び出し 25 回ごと（`--rate-check-every`）に確認する
- 残りが `--min-remaining`（既定 200）を下回ったら、キャッシュを保存して終了コード 3 で止まり、`--out` は書かない。reset 後に同じコマンドで再実行すると続きから取る
- 呼び出し回数の目安は、リポジトリ一覧のページ数、リポジトリごとの runs のページ数（二分探索の分を含む）、新しい run ごとの jobs のページ数の合計
- 1 時間あたりの REST 上限は認証の種類で変わる。PAT・OAuth は 5,000 回、`GITHUB_TOKEN` は 1,000 回（同梱の `overview/rate-limits.md` による）

| 終了コード | 意味 |
|---|---|
| 0 | 成功 |
| 1 | 失敗（`--out` は書かない） |
| 3 | レート制限の残量不足で中断（jobs のみ。キャッシュ保存済み） |

## 必要な権限

トークンはスクリプトが扱わず、`gh` の認証だけに頼る。付与は billing 閲覧に必要な範囲に限る。現状は広いスコープでしか確認できていない（下表）。

| 用途 | 必要な権限 |
|---|---|
| billing usage | org の管理者または billing manager（同梱の `billing/usage.md` は organization admin を要求）。動作確認済みの経路は classic token の `admin:org` だけで、org 設定の書き込みを含む広いスコープである。使い終わったらトークンを失効させるか、有効期限の短いトークンを使う。fine-grained token の権限名は未確認（確認は #143）で、ここでは断定しない |
| jobs（runs / jobs / リポジトリ一覧） | 対象リポジトリの Actions の読み取り。fine-grained token ではリポジトリ権限の `actions`（読み取り）、`metadata` は選択時に自動付与される（同梱の `overview/permissions.md`）。リポジトリ一覧のエンドポイント個別の要件は未検証 |
| `rate_limit` | 権限不要 |

## 関連

- [SKILL.md](../SKILL.md)
- [`fetch_billing.py`](../scripts/fetch_billing.py)
- [`fetch_jobs.py`](../scripts/fetch_jobs.py)
