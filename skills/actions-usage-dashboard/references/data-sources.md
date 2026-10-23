<!-- source: skills/actions-usage-dashboard/scripts（fetch_billing.py / fetch_jobs.py の実装）と .agents/skills/github-docs/references/rest-api/（billing/usage.md・overview/rate-limits.md・overview/permissions.md）。公式: https://docs.github.com/en/rest/billing/usage 、https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens 、https://docs.github.com/en/billing/reference/billing-reports 、https://docs.github.com/en/billing/concepts/product-billing/github-actions 、https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs 、https://docs.github.com/en/rest/orgs/orgs -->
<!-- 最終確認日: 2026-10-24 -->
<!-- 取得状況: ✅ 実装と公式文書（docs.github.com）で確認済み: fine-grained token の権限名（実機は未検証。動作確認済みの経路は classic token の admin:org のみで、org 設定の書き込みを含む広いスコープ。使い終えたら失効させる）・run の再実行期間（初回実行から 30 日）・orgs/<org> でプランを含む全詳細を見る条件（owner。classic は admin:org、GitHub App は Organization plan 権限） / ⚠️ 未確認: billing usage の保持期間（本体ページに記載なし）、orgs/<org> の fine-grained PAT 用の権限、Actions の分数について請求画面と API の更新間隔・時差の公式記載（docs.github.com に見つからなかった）、billing manager が billing usage API を呼べるか -->

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

- 日付はすべて UTC で扱う（公式: 利用は UTC で記録される。出典は末尾の「出典」節）
- billing の `day` は明細の `date` を UTC に直した日。対象月でない明細は捨てる
- jobs の `day` は job の `started_at` の UTC 日
- 期間の指定は billing が `--since` / `--until`（`YYYY-MM`、`--until` の上限は UTC の当月）、jobs が `--since`（`YYYY-MM-DD`）
- billing の期間は最大 24 か月（since から until まで）。超えると gh を呼ばずに終了コード 1 で終わる。24 は同系エンドポイント（ai_credit / premium_request / usage/summary）の公式記載「過去 24 か月のみ」に合わせた値である。usage 本体のページ（rest/billing/usage）には保持期間の記載が無く、**未確認**（確認日 2026-10-24）。billing-reports の「集計レポートは最大 1 年・詳細レポートは最大 31 日」は Web UI のレポート期間で、API の保持期間ではない
- summarize.py の rows 入力は 8 MB 以下（build_dashboard.py と同じ。超えると終了コード 1）。グラフは 744 日（24 か月 x 31 日）を超えると描画せず警告を出す

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
- Actions の分数について、請求画面と billing usage API の更新間隔・時差は docs.github.com に記載が見つからず、**未確認**である（確認日 2026-10-23。確認したページは末尾の「出典」節）。ずれる可能性は想定しておく
  - 参考: 公式に更新時間が書かれているのは artifact storage の「6〜12 時間以内」だけで、minutes には当てはまらない
  - 参考: 詳細な利用レポートは Web UI 専用で、REST API は集計済みの情報だけを返す
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
- 2 回目以降の runs 一覧は、リポジトリごとに `created>=from` から取る。`from` は「実行日 - 45 日」「処理後も未登録の run（未完了・再実行中・不正 job）の作成日」「1,000 件上限で欠けた日」の最小値。45 日は run を再実行できる期間（公式: 初回実行から 30 日まで。出典節）に余裕を持たせた値で、窓内の再実行を取りこぼさない。30 日や 31 日に絞らない理由は、公式の起点が「初回実行」で、一覧の絞り込みに使う作成日より遅れ得るため（セルフホストのキュー待ち・fork PR の承認待ちなど）。作成から開始までの遅れが「窓 - 30 日」を超えると、settled 後の再実行を取りこぼして分数が過少になる。`test_fetch_jobs.py` が 31 日未満への縮小を検出する`--since` を前回より過去にしたときと、cursor が無いリポジトリは全期間を一覧する。一覧から外れた古い run も、出力はキャッシュ済みの履歴から作るので集計に残る
- リポジトリ数の上限オプションは設けない。一部のリポジトリを黙って落とした rows が「org 全体」として読まれるため。短くしたいときは `--since` を使う
- jobs は `filter=all` で全 attempt を取り、job id で重複を除く。1 run あたり 100 ページまで

### キャッシュ

- 置き場所は `<--cache>/jobs-cache-<org>.json`
- jobs を取り終えた完了済みの run だけを登録する。`--since` より前に作られた run は実行時に削除する
- 中断時は run 一覧を `listing` に保存する。同じ UTC 日に同じ `--since` で再実行すると続きから取る
- `cursor` にリポジトリごとの「次回の一覧の下限候補」（`since` と `from`）を持つ。成功時に作り直し、今回の対象外のリポジトリの分は捨てる。検証は他のキーと同じく非信頼入力として行う
- 進捗を出すたび（`--progress-interval` 秒ごと、既定 30）にキャッシュを保存する（checkpoint）。外部から kill されても、それまでの取得が残る
- キャッシュは信頼しない入力として検証し、壊れていれば止まる
- 消し方は `jobs-cache-<org>.json` のファイル 1 つだけを削除する（ディレクトリごとは消さない）。次回は全件を取り直す

### レート制限と終了コード

- `gh api rate_limit` の `resources.core.remaining` を、run やリポジトリの境目で、API 呼び出し 25 回ごと（`--rate-check-every`）に確認する
- 残りが `--min-remaining`（既定 200）を下回ったら、キャッシュを保存して終了コード 3 で止まり、`--out` は書かない。reset 後に同じコマンドで再実行すると続きから取る
- `--max-seconds N`（既定 0 = 無制限）に達したら、run / リポジトリの境目でキャッシュを保存して終了コード 4 で止まり、`--out` は書かない。待たずに同じコマンドを再実行してよい。一覧と jobs のそれぞれで 1 単位は処理してから止まるため、小さな値でも再実行を重ねれば必ず前進する
- 進捗は stderr に数値だけの行（`進捗: runs 一覧 k/N リポジトリ・API 呼び出し C・経過 S 秒`、`進捗: jobs 取得 k/M run ...`）を出す。リポジトリ名は出さない
- 呼び出し回数の目安は、リポジトリ一覧のページ数、リポジトリごとの runs のページ数（二分探索の分を含む）、新しい run ごとの jobs のページ数の合計
- 1 時間あたりの REST 上限は認証の種類で変わる。PAT・OAuth は 5,000 回、`GITHUB_TOKEN` は 1,000 回（同梱の `overview/rate-limits.md` による）

| 終了コード | 意味 |
|---|---|
| 0 | 成功 |
| 1 | 失敗（`--out` は書かない） |
| 3 | レート制限の残量不足で中断（jobs のみ。キャッシュ保存済み。reset 後に再実行） |
| 4 | `--max-seconds` の時間上限で中断（jobs のみ。キャッシュ保存済み。すぐに再実行してよい） |

## 必要な権限

トークンはスクリプトが扱わず、`gh` の認証だけに頼る。付与は billing 閲覧に必要な範囲に限る。

| 用途 | 必要な権限 |
|---|---|
| billing usage（`organizations/<org>/settings/billing/usage`） | 公式は org の administrator を要求する（出典: rest/billing/usage）。billing manager で呼べるかは**未確認**。公式文書の記述は食い違う。rest/billing/usage の本体ページと permissions-required ページは、fine-grained token（GitHub App の user / installation access token・fine-grained PAT）で "Administration" の Organization permissions（read）があれば呼べるとする（確認日 2026-10-24）。一方、使用状況レポートのチュートリアルは課金エンドポイントが fine-grained token に対応しないとする（この確認日にチュートリアルの URL は特定できず再確認していない）。実機で確かめていないため fine-grained token は使わない。動作確認済みの経路は classic token の `admin:org` だけで、org 設定の書き込みを含む広いスコープである。公式文書で classic のより狭いスコープを確認できるまで、これ以外は推奨しない。使い終わったらトークンを失効させるか、有効期限の短いトークンを使う |
| jobs（runs / jobs / リポジトリ一覧） | リポジトリ一覧 `orgs/<org>/repos` は Repository permissions の "Metadata"（read）、`actions/runs` と `runs/<id>/jobs` は Repository permissions の "Actions"（read）（出典: permissions-required ページ）。`fetch_billing.py` も visibility の取得に `orgs/<org>/repos` を呼ぶため、"Metadata"（read）が要る。実機は未検証 |
| プラン名（`orgs/<org>`。summarize.py が含有分の引き当てに呼ぶ。`--plan` を指定すれば呼ばない） | 公式: 全詳細を見るには org の owner であること。classic PAT / OAuth app token は `admin:org` scope、GitHub App は Organization plan 権限が要る（出典: rest/orgs/orgs）。`plan` が全詳細に含まれるとは明記されていない。fine-grained PAT 用の権限は permissions-required ページに `GET /orgs/{org}` が載っておらず、**未確認**（権限名は創作しない）。取得に失敗（権限不足を含む）すると警告を出し、含有分は「不明」のまま続行する（summarize.py の実装）。最小権限にしたいときは `--plan` で明示する |
| `rate_limit` | 権限不要 |

## 出典

確認日は 2026-10-24。

- https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens : `orgs/<org>/repos` は Repository "Metadata"（read）、runs / jobs は Repository "Actions"（read）。`organizations/<org>/settings/billing/usage` は Organization "Administration"（read）。`GET /orgs/{org}` は載っていない
- https://docs.github.com/en/rest/billing/usage : 呼び出せるのは organization の administrator。billing manager の記載は無い。fine-grained token は "Administration" organization（read）で対応と記載。「過去 24 か月のみ」の注記は ai_credit / premium_request / usage/summary にあり、`/settings/billing/usage` 本体には保持期間の記載が無い（未確認）
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs : workflow run・失敗した job・個別の job は初回実行から 30 日まで再実行できる（同梱 docs の `actions/limits.md` に再実行 50 回の上限もある）
- https://docs.github.com/en/rest/orgs/orgs : `GET /orgs/{org}` の全詳細は org owner のみ。OAuth app token と classic PAT は `admin:org`、GitHub App は Organization plan 権限
- 使用状況レポートのチュートリアル（公式。URL は特定できておらず 2026-10-24 は未再確認）: 課金系エンドポイントは fine-grained token 非対応とする記述（上の rest/billing/usage と食い違う）
- https://docs.github.com/en/billing/reference/billing-reports : 利用はすべて UTC で記録される。集計レポートは最大 1 年・詳細レポートは最大 31 日（Web UI のレポート期間で、API の保持期間ではない）。詳細レポートは Web UI 専用で、REST API は集計済みの情報だけを返す
- https://docs.github.com/en/billing/concepts/product-billing/github-actions : artifact storage の使用量は 6〜12 時間以内に更新される（minutes の記載ではない）
- 未確認: Actions の分数について請求画面と API の更新間隔・時差（https://docs.github.com/en/billing/how-tos/products/view-productlicense-use を含め、上記のどのページにも記載が無い）

## 関連

- [SKILL.md](../SKILL.md)
- [`fetch_billing.py`](../scripts/fetch_billing.py)
- [`fetch_jobs.py`](../scripts/fetch_jobs.py)
