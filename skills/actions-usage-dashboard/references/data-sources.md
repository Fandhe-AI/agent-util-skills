<!-- source: skills/actions-usage-dashboard/scripts（fetch_billing.py / fetch_jobs.py の実装）と .agents/skills/github-docs/references/rest-api/（billing/usage.md・overview/rate-limits.md・overview/permissions.md）。公式: https://docs.github.com/en/rest/billing/usage 、https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens 、https://docs.github.com/en/billing/reference/billing-reports 、https://docs.github.com/en/billing/concepts/product-billing/github-actions 、https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs 、https://docs.github.com/en/rest/orgs/orgs -->
<!-- 最終確認日: 2026-10-28 -->
<!-- 取得状況: ✅ 実装と公式文書（docs.github.com）で確認済み: fine-grained token の権限名（実機は未検証。動作確認済みの経路は classic token の admin:org のみで、org 設定の書き込みを含む広いスコープ。使い終えたら失効させる）・run の再実行期間（初回実行から 30 日）・orgs/<org> でプランを含む全詳細を見る条件（owner。classic は admin:org、GitHub App は Organization plan 権限） / ⚠️ 未確認: billing usage の保持期間（本体ページに記載なし）、orgs/<org> の fine-grained PAT 用の権限、Actions の分数について請求画面と API の更新間隔・時差の公式記載（docs.github.com に見つからなかった）、billing manager が billing usage API を呼べるか、含有枠の消費の換算（定価 ÷ Linux 単価）が請求画面の換算と一致するか（公式は倍率を明記せず、SKU ごとの分単価だけを載せる） -->

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

## 含有枠の消費の確度

`summarize.py` は含有枠の消費を、次の条件で「下限」「概算」「確定」に分けて表示する。

| 条件 | 表示 | `breakdown.included` のキー | 残量・割合 |
|---|---|---|---|
| `assume_current_visibility` が真、または included / public_free 行・quota > 0 がある | 約 X 分（概算）。「下限」「以上」は書かない | `approximate: true`、`lower_bound: false` | unknown があれば不明、なければ「約」付きの概算値 |
| 上記以外で unknown がある | X 分以上（下限。区分不明あり） | `lower_bound: true`、`approximate: false` | 不明 |
| 上記以外（unknown なし・分類なし） | 確定値 | どちらも false | 確定値 |

- 「下限」が成り立つのは、quota が構造上 0 の既定モード（非有料分がすべて unknown）で、unknown 分だけ少なく数えている場合に限る
- `--assume-current-visibility` で分類した quota は、現在の公開範囲と「定価 ÷ Linux 単価」の換算に依存する。上振れも下振れもあり得るため概算とし、下限とは言わない
- `fetch_billing.py` は出力のトップレベルに `assume_current_visibility`（真偽値）を記録する。旧形式の文書（キーなし）は行の内容で判定し、included / public_free 行があれば概算に倒す。値が真偽値以外なら `summarize.py` は止まる
- フラグが偽なのに分類済みの行がある矛盾した文書も、安全側の概算にする
- 分類済みかどうかは、`summarize.py`（`is_classified`）も雛形（`quotaApprox`）も入力の文書全体で判定する（対象月・選択期間では変えない）。旧形式の文書で、対象月が unknown だけでも別の月に分類済みの行があれば概算にする。分類済みの行は分数か quota が正のものだけを数える
- 雛形 `templates/dashboard/index.html` も同じ規則を適用する。`assume_current_visibility` は billing_meta の `assume_current_visibility` 行（`value` が `"true"`）で受け取る（「ページへ渡る値」節）。概算のときは、区分不明の有無にかかわらず枠消費の値（タイル・表、無料枠を除いたグラフの凡例・ツールチップ）に「（概算）」を付け、「以上」は付けない。区分不明があるときのグラフ下の注記も概算の文言にする。下限のときは区分不明を含む値に「以上」を付ける。残量は区分不明があれば不明、なければ概算のとき「約」を付ける。この行を持たない meta で作られたダッシュボードは、次の更新で meta が作り直されるまで行の内容だけで判定する

上振れ・下振れの要因（含有枠の消費が請求画面と一致しない理由）:

- 利用時は公開・現在は非公開のリポジトリは、現在値で included に数えるため上振れする。逆に利用時は非公開・現在は公開なら public_free になり下振れする
- 換算式が請求画面の換算と一致するかは未確認である。公式の Actions 課金ページ（確認日 2026-10-28）は SKU ごとの分単価を載せるが、含有分の消費の倍率は明記していない。arm64 の SKU が含有分を消費するかも記載がない。larger runner は含有分を使わず常に課金されると記載がある
- 端数は行ごとに 6 桁で丸める。請求画面の丸め方は未確認である
- 区分不明（削除済み・権限不足のリポジトリ）の分は quota に入らず、下振れする

実 org での確認（#138）では、フラグ付きの概算値が請求画面の使用量を約 0.9% 上回った。上の要因のどれが効いたかは未検証（再測が必要）である。

## 請求画面と数字が合わない理由

- 当日分（UTC）は集計中で、あとから増える
- Actions の分数について、請求画面と billing usage API の更新間隔・時差は docs.github.com に記載が見つからず、**未確認**である（確認日 2026-10-23。確認したページは末尾の「出典」節）。ずれる可能性は想定しておく
  - 参考: 公式に更新時間が書かれているのは artifact storage の「6〜12 時間以内」だけで、minutes には当てはまらない
  - 参考: 詳細な利用レポートは Web UI 専用で、REST API は集計済みの情報だけを返す
- 公開範囲は取得時点の値しか取れない。期間中に公開・非公開を切り替えたリポジトリは、過去月の included / public_free が実態とずれる。誤分類で quota が消えたり過大になったりするのを避けるため、既定では非有料分を `unknown` にして quota に数えない。切り替えが無いと保証できる場合だけ `--assume-current-visibility` を付ける。`public` は `public_free`、`private` / `internal` は `included` に確定する
- 含有枠の消費を下限と言える条件と上振れの要因は「含有枠の消費の確度」を参照する
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
- 2 回目以降の runs 一覧は、リポジトリごとに `created>=from` から取る。`from` は「実行日 - 45 日」「処理後も未登録の run（未完了・再実行中・不正 job）の作成日」「1,000 件上限で欠けた日」の最小値。45 日は run を再実行できる期間（公式: 初回実行から 30 日まで。出典節）に余裕を持たせた値で、窓内の再実行を取りこぼさない。30 日や 31 日に絞らない理由は、公式の起点が「初回実行」で、一覧の絞り込みに使う作成日より遅れ得るため（セルフホストのキュー待ち・fork PR の承認待ちなど）。作成から開始までの遅れが「窓 - 30 日」を超えると、settled 後の再実行を取りこぼして分数が過少になる。`test_fetch_jobs.py` が 31 日未満への縮小を検出する。`--since` を前回より過去にしたときと、cursor が無いリポジトリは全期間を一覧する。一覧から外れた古い run も、出力はキャッシュ済みの履歴から作るので集計に残る
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

### 推定モード

`fetch_jobs.py --estimate [--sample-runs K]` は、正確さより速さを優先して傾向を見るためのモード。正確な値が要るとき、特に請求や合計分数の照合には `--estimate` なしの全件取得を使う（誤差は「実測との比較（#199）」）。

- 目的: セルフホストかどうかは jobs のラベルにしかなく、全件取得は run ごとに jobs API を呼ぶ。run の多い org（4 週間で約 47,000 run）では初回に十数時間かかるため、jobs の取得を抜き取りに絞る
- 単位と式: `(repo, workflow_id)` の組ごとに、完了 run から最大 K run（既定 3、1〜20）を基準に jobs だけ取る（実際の数は次の 2 段階の配分で決まり、合計は予算 `B = Σ min(n, K)` 以下。n は組の完了 run 数）。抜き取った run の runner_group ごとのセルフホスト分数を run 当たりの平均にし、抜き取らなかった完了 run に 1 run ずつ割り当てる（平均 × 完了 run 数）。セルフホスト分が 0 の抜き取り run も分母に入る。推定分は各 run の作成日（UTC）に載せる。抜き取った run は実測値を使う
- 抜き取りの選び方: `(created_at, id)` 順に並べた列から等間隔に選ぶ（先頭・末尾を含む。K=1 は中央）。乱数は使わない。期間の前・中・後に散らすので、期間の途中でセルフホストへ移した workflow の値を期間全体に広げない。決定的なので、中断後の再開でも同じ run を選び、取得済みの分がキャッシュに当たる
- 2 段階の配分（#205）: 段階 1 で各組の先頭と末尾の 2 run（試し取り）の jobs を取る。両方が 0 分（GitHub ホストだけ）の組は「0 分組」として、試し取りだけで打ち切り、残りを 0 分と推定する。段階 2 で、0 分組で浮いた分（`min(n, K)` − 2）を 0 分でない組へ run 数 n に比例して足す（各組の上限は n。溢れた分は残りの組へ配り直し、整数化は最大剰余法、同点は `(repo, workflow_id)` の文字列昇順）。0 分でない組と試し取りが不正 job の組（不明）は、均等配分と同じ `min(n, K)` を下限として保証するので、組別の精度は下がらない。試し取りは必ず最終の抜き取りに含まれる。K ≤ 2 では浮く予算が無く、均等配分と同じ結果になる。判定は取得済みの run の値（キャッシュ）だけから決まり、中断後の再開でも同じ配分になる
  - 先頭と末尾の 2 run で判定する理由: 1 run だけの判定は、K=1 のとき 0 分でない組を 0 分と誤った例があり危険側になる。期間の途中でセルフホストへ移した組は片端が 0 分でないため、打ち切られない
- 経過時間（`updated_at` − `run_started_at`）で割り戻す方式は採らない。並列 job・キュー待ち・再実行で値がずれるため
- run の区分

| 区分 | 条件 | 扱い |
|---|---|---|
| 実測（キャッシュ） | `cache["runs"]` に attempt 以上で登録済み | キャッシュの値を使う |
| 0 分で打ち切り | 0 分組で、試し取り以外の完了 run | 残りを 0 分として推定する（jobs を取らない。キャッシュには入れない） |
| 抜き取り | 抜き取り（試し取りを含む）に入り、未登録または attempt が増えている | jobs を取り、`cache["runs"]` に実測値として登録する |
| 旧 attempt 残し | 抜き取り外で、古い attempt だけ登録済み | 推定しない。旧 attempt の値を使う（過少になり得る） |
| 推定 | 抜き取り外で未登録 | 平均を作成日に割り当てる。キャッシュには入れない |
| 除外 | 不正な job でスキップした run | 数えず、警告に残す |

- 推定値を `cache["runs"]` に入れない理由: 入れると、後続の実行が取得済みと誤認して実測へ置き換わらず、cursor も縮んでしまう
- 推定区分の run は `cache["runs"]` に無いので、cursor の `from` はその作成日まで戻る。次回の実行（全件でも推定でも）が一覧し直すための正しい挙動で、不具合ではない
- 推定モードを繰り返すと、毎回の抜き取りが実測としてキャッシュに積まれ、全件取得へ少しずつ近づく
- `listing` の要素に `workflow_id`（正の整数か null）を持つ。旧版の listing（キー無し）は壊れ扱いにせず、推定モードでは該当リポジトリの一覧を取り直す
- 出力: 推定モードのときだけトップレベルに `estimate` を足す。全件取得の出力には無い。値は数値と固定文字列だけ

```json
"estimate": {"method": "sample_mean_per_workflow", "sample_runs": 3, "sampled_runs": 12,
             "estimated_runs": 340, "groups": 5, "unestimated_runs": 0,
             "budget_runs": 15, "zero_groups": 2}
```

  `sampled_runs` は有効な抜き取り run の合計、`estimated_runs` は推定した run 数、`groups` は組の数、`unestimated_runs` は抜き取りが全て不正 job（または作成日が不正）で 0 分として数えた run 数、`budget_runs` は予算 `Σ min(n, K)`（jobs 呼び出し数の上限）、`zero_groups` は試し取りで打ち切った 0 分組の数（`sample_runs` は K）。`warnings` に推定値である旨の 1 行（数値のみ）が入り、標準出力にも 1 行足す
- 推定の式（再掲）: 組 g の推定分 = (Σ 抜き取り run のセルフホスト分 ÷ 有効な抜き取り数) × 抜き取らなかった完了 run 数（runner_group ごと）
- API 呼び出し数: リポジトリ一覧のページ数 + runs 一覧のページ数（二分探索を含む）+ 抜き取りで新規に取る run の jobs ページ数。jobs が 1 ページなら jobs の呼び出しは予算 `B = Σ min(n, K)`（K × 組数以下）以下。runs 一覧が支配的になり、約 640 回 + B（最大でも K × 組数）、1〜1.5 時間程度の見込み（呼び出し 1 回 0.5〜1 秒の仮定。未計測）
- 誤差要因（実装は fetch_jobs.py の `plan_samples` / `pilot_samples` / `allocate_samples` / `estimate_entries`）
  - workflow 内のばらつき: 条件付き job・matrix・`paths` による job の有無で、run ごとの分数が大きく変わる
  - 母集団は完了 run 全体: cancelled・failure など短く終わった run も同じ平均に入る
  - K が小さいほど外れ値の影響が大きい
  - 0 分組の打ち切り: 両端が 0 分でも、途中の run にセルフホスト分がある組は 0 分と推定される（過少）
  - 期間途中のランナー移行: 抜き取りを分散させて和らげるが、組の平均は期間全体に広がる
  - 旧 attempt 残し（再実行で完了した run）: 推定せず旧 attempt の値を使うため過少になり得る
  - 推定不能（抜き取りが全て不正 job、または作成日が不正）: 0 分として数えるため過少になる（`unestimated_runs`）
  - 割り当て日は run の作成日（UTC）: 日をまたぐ run は実測と日別の配分がずれる
  - 誤差の大きさと傾向は次の「実測との比較（#199）」に載せた
- 要約・ダッシュボードの表示: どちらも `estimate` があるかないかだけを読み、数値・文字列は表示に使わない。ダッシュボードは jobs 文書そのものではなく `jobs_meta` の `estimate` 行（値は固定の `"true"`）で判定する（下の「ページへ渡る値」）
  - `summarize.py`: セルフホスト行に「（推定）」を付け、`--json` の `breakdown.self_hosted.estimated` を true にする（キーは常にあり、推定でなければ false。対象月が jobs の取得期間外なら false）。`estimate` がオブジェクトでなければ終了コード 1。billing 文書の `estimate` は無視する
  - 雛形 `templates/dashboard/index.html`: リード文・セルフホストのタイルに「（推定）」、タイルの注記と総分数の注記・注記行に推定である旨を出す。選択期間に jobs がないとき、jobs 未接続のときは出さない。グループ別の上位行・グラフ・内訳表には推定表示がない

#### 実測との比較（#199）

既定の K と使い分けを決めるため、推定を実測と比べた。この節の表と数値は均等配分（#205 以前）で測った値である。#205 の配分での再測は次の「実測との比較（#205）」に載せた（この節の数値は書き換えない）。

測り方:

- 手元の実測データ（全件取得のキャッシュ）に対して、本番の `plan_samples` / `estimate_entries` / `build_rows` をそのまま使い、推定をオフラインに再現した。抜き取った run だけを実測として扱い、残りを推定させ、全 run を実測した値と比べた
- 母集団は 8 日・7,361 run・128 組（実測が 0 分でない組は 57）。runs 一覧が 1,000 件上限に達した日と、取得が途中で止まって完了 run が欠けているセルは除き、取得が完全なセルだけを使った
- 正しさの確認として、K を母集団より大きくして全件を抜き取ると、合計・日別・組別の誤差はすべて 0% になった

本番どおりの等間隔抜き取りは結果が 1 通りに決まる。合計の誤差は少数の大きな組の当たり外れで決まり、K に対して単調に減らない（K=3 が -2.7%、K=10 が -11.9%）。そこで、同じ式のまま抜き取りの開始位置だけを無作為にして 2,000 回繰り返した |合計誤差| の中央値と p90 を併記する。

| K | 合計誤差（等間隔の 1 通り） | 日別 \|誤差\| 中央値 / 最大 | 組別 \|誤差\| 中央値 / p90 | 組別 \|誤差\| 50% 超の割合 | 開始位置を無作為にした \|合計誤差\| 中央値 / p90 | jobs 呼び出し数（全件取得比） |
|---|---|---|---|---|---|---|
| 1 | -39.1% | 43.1% / 68.6% | 16.6% / 54.7% | 12.3%（7/57） | 34.5% / 60.9% | 1.7% |
| 2 | +14.7% | 24.9% / 58.6% | 7.9% / 37.0% | 5.3%（3/57） | 20.4% / 44.2% | 3.3% |
| 3（既定） | -2.7% | 6.8% / 59.7% | 2.1% / 26.4% | 1.8%（1/57） | 16.2% / 39.7% | 4.6% |
| 5 | +6.7% | 9.3% / 48.4% | 2.1% / 17.6% | 1.8%（1/57） | 12.8% / 31.7% | 7.1% |
| 10 | -11.9% | 0.6% / 31.2% | 0.0% / 7.2% | 0%（0/57） | 8.6% / 18.9% | 11.0% |

- 「開始位置を無作為にした」列は、抜き取りの開始位置を無作為にした模擬（2,000 回）の中央値と p90 であり、本番の先頭・末尾を含む決定的な抜き取りの分布ではない。
- 組別の割合は実測が 0 分でない 57 組が分母。実測が 0 分の 71 組は推定も必ず 0 分になる。実測が 0 分でないのに推定が 0 分になった組は K=1 で 2 組、K=2 以上で 0 組
- jobs 呼び出し数は run 1 件を 1 呼び出しと数えた全件取得に対する比で、jobs の 1 ページ目だけを数える（runs 一覧は推定でも全件でも同じなので含めない）。呼び出しの半分以上は、実測が 0 分の組（GitHub ホストのみ）の抜き取りに使われる

既定 K=3 を据え置く理由:

- 組単位（どの workflow がセルフホストを多く使うか）の把握には足りる。組別の誤差は中央値 2%・p90 26%
- K=1 から 3 への改善が大きい（組別の中央値 16.6% から 2.1%、p90 54.7% から 26.4%）
- K=5 は呼び出しが 1.5 倍になるのに対し、合計の誤差の改善が小さい（今回の母集団での模擬の中央値 16.2% から 12.8%、p90 39.7% から 31.7%）。合計の精度は K を上げても緩やかにしか良くならない（おおむね 1/√K）

使い分け:

- 合計分数は、今回の母集団で抜き取りの開始位置を無作為にした模擬では、K=3 の誤差が中央値 16%・p90 40% だった。本番の決定的な抜き取りでの発生頻度でも、他の org・期間での保証でもない。請求や合計の照合に使うなら、推定ではなく `--estimate` なしの全件取得を使う
- 日別の推定は構造的に偏る。組の期間全体の平均を各日に一律に配るため、日によって所要時間が変わると、K を増やしても残る。抜き取り誤差のない真の平均を割り当てても、日別の誤差は -10% から +57% の幅になった。日別の推移は参考程度に見る
- 組別の順位づけ・移行候補の洗い出しには推定で足りる。大きな組は全件取得で確かめる

誤差が大きくなる傾向（実測が 0 分でない 57 組、K=3 の組別 \|誤差\|）:

| 特徴 | 区分 | 組数 | 中央値 | p90 |
|---|---|---|---|---|
| 0 分の run の混在 | 無し | 52 | 1.2% | 21.2% |
| | 有り | 5 | 27.3% | 34.9% |
| 非 0 分 run の変動係数 | 0.25 未満 | 28 | 0.9% | 21.9% |
| | 0.25 以上 1 以下 | 24 | 8.9% | 28.5% |
| runner_group | 単一 | 37 | 1.3% | 14.4% |
| | 複数 | 20 | 19.8% | 42.4% |
| 組の run 数 | 1〜3 | 20 | 0.0% | 0.0% |
| | 4〜10 | 28 | 6.9% | 21.3% |
| | 11〜50 | 2 | 34.8% | 57.7% |
| | 50 超 | 7 | 27.3% | 39.9% |

- 0 分の run が混ざる組（条件付き job、途中で `runs-on` を変えた workflow）は、抜き取りに 0 分の run が入るかどうかで大きく振れる。K=1 では中央値が 95.7% になる
- runner_group が複数の組（matrix、途中の `runs-on` の変更）と、所要時間のばらつき（変動係数）が大きい組で大きくなる
- run 数が K 以下の組は全件取得になり誤差が 0 になるので、run 数の多い組ほど誤差が大きく見える。誤差の大半は run 数 50 超の少数の組から来る。この 7 組は実測分数の約 59% を占め、複数 runner_group を持つ割合・変動係数・0 分 run の混在割合がそれ以外の組より高い
- K=3 で 50% 超ずれた組は 1 組だけだった（複数 runner_group・変動係数 0.94・run 数 16）。この 1 組で実測分数の約 13% を占める

制約:

- 母集団は 8 日分で、組の run 数が本番の期間（例: 30 日）より小さい。K=10 では 57 組中 48 組が全件取得になり、組別の誤差は実際より小さく出ている。長い期間では同じ K でも組別の誤差は大きくなり得る
- 合計の誤差の中央値と p90 は今回の母集団での目安にすぎない。別の org・期間では、組の構成（大きな組の変動係数・0 分の run の混在）によって変わる
- 等間隔の結果は決定的な 1 通りで、ばらつきは開始位置を変えた模擬でしか見ていない

#### 実測との比較（#205）

run 数比例配分（#205・PR #212）を、#199 と同じ母集団でオフラインに再現して旧方式（均等配分）と比べた。測定日は 2026-10-28。数値はすべて誤差率・件数・割合である。

測り方:

- 本番の `plan_samples` / `pilot_samples` / `allocate_samples` / `estimate_entries` / `build_rows` をそのまま使い、jobs の取得だけを実測済みデータの参照に置き換えた。試し取りから 2 段目まで本番と同じ順序で呼んだ
- 母集団は #199 と同じ（247 セル・7,361 run・128 組。実測が 0 分でない組は 57）。件数の一致を確認してから比べた
- 正しさの確認として、K=1・2 では新旧が全指標で一致した（打ち切りが起きないため）。K を母集団より大きくすると誤差はすべて 0% になった

jobs 呼び出し数は旧方式と同数だった（予算 Σmin(n, K) と一致し、使い残しは 0）。新方式の内訳は次のとおり。

| K | 呼び出し数（新旧同数） | うち試し取り | うち 2 段目 | 打ち切った組 |
|---|---|---|---|---|
| 1 | 128 | 128 | 0 | 0 |
| 2 | 242 | 242 | 0 | 0 |
| 3 | 341 | 242 | 99 | 56 |
| 5 | 520 | 242 | 278 | 56 |
| 10 | 811 | 242 | 569 | 56 |

K=1・2 は新旧が同一のため、以下の表は K=3・5・10 だけを載せる。

(a) 本番どおりの決定的な抜き取り（結果は 1 通りに決まる）:

| K | 方式 | 合計の誤差率 | 日別 \|誤差\| 中央値 / 最大 | 組別 \|誤差\| 中央値 / p90 | 組別 \|誤差\| 50% 超の割合 |
|---|---|---|---|---|---|
| 3 | 旧 | -2.75% | 6.80% / 59.67% | 2.05% / 26.44% | 1.8%（1/57） |
| 3 | 新 | +17.44% | 24.32% / 55.27% | 1.86% / 21.20% | 1.8%（1/57） |
| 5 | 旧 | +6.68% | 9.29% / 48.44% | 2.14% / 17.64% | 1.8%（1/57） |
| 5 | 新 | +16.45% | 15.14% / 54.86% | 0.00% / 12.42% | 0%（0/57） |
| 10 | 旧 | -11.93% | 0.59% / 31.20% | 0.00% / 7.15% | 0%（0/57） |
| 10 | 新 | -1.06% | 0.71% / 17.83% | 0.00% / 1.48% | 0%（0/57） |

(b) 抜き取り位置を無作為にした 2,000 回反復（合計の誤差）:

| K | 方式 | \|合計誤差\| 中央値 | p90 | ±10% 以内の割合 |
|---|---|---|---|---|
| 3 | 旧 | 16.4% | 39.7% | 29.9% |
| 3 | 新 | 6.6% | 16.8% | 67.4% |
| 5 | 旧 | 12.1% | 30.9% | 42.7% |
| 5 | 新 | 4.1% | 9.4% | 93.8% |
| 10 | 旧 | 8.7% | 19.2% | 56.2% |
| 10 | 新 | 3.6% | 6.8% | 100.0% |

- 無作為化のやり方: 新方式は、打ち切りの判定と組ごとの抜き取り数を本番どおりに固定し、打ち切らなかった組を、組の run を作成順に並べて等間隔・開始位置を一様乱数にした系統抽出で抜き取る。旧方式は K を使って同じ方法で抜き取る
- 同じ無作為化での日別 \|誤差\| 中央値の典型値（反復の中央値）も K=3・5・10 のすべてで改善した（K=3 は 17.4% から 14.3%、K=10 は 5.9% から 0.9%）。K=3 の日別最大の典型値だけが 52.3% から 58.0% に悪化した

2 つの評価が割れた:

- (b) と組別の指標は K≥3 ですべて改善した。K=3 の (b) の \|合計誤差\| 中央値と p90 は、旧方式の K=10（8.7%・19.2%）よりも良い
- 本番どおり 1 回の抜き取りでは、K=3・5 で合計と日別の誤差が悪化した（合計 -2.75% が +17.44%、+6.68% が +16.45%）
- 悪化の原因は、実測の 44% を占める 1 組の配置である。この組は新方式で抜き取り数が増え、等間隔の格子が所要時間の長い run に当たった。配分の偏りではなく、無作為化したときの期待値の偏りは合計の ±0.1% 未満だった
- 旧方式の K=3 の -2.75% は、大きい 2 組の誤差（寄与は +8.5 ポイントと -13.1 ポイント）が打ち消し合った値で、各組が正確だったわけではない。新方式では大きい方の組の誤差が符号を変えて縮み、もう一方と同じ向きにそろったため合計が大きくなった
- 結論: 期待される誤差は小さくなったが、決定的な 1 回の値は母集団によって旧方式より外れ得る。合計分数の照合に推定を使わない方針（上の「使い分け」）は変わらない

新しい失敗の型（実測が 0 分でないのに 0 分として打ち切られる組）:

- 本番どおりの抜き取りでは 0 組だった。打ち切った 56 組はすべて実測 0 分だった
- 試し取りの位置を無作為にした感度分析では、実測が 0 分でない組を打ち切る反復が K=3 で 13.1%、K=5 で 14.1%、K=10 で 12.5% あった（K=3〜10 で 12.5〜14.1%）。失われる量は、平均で実測の 1.4〜1.5%、最大で 47.7%（実測シェアの大きい組が丸ごと 0 分になる場合）
- 今回の母集団で 0 組だったのは、0 分の run が混ざる 5 組の両端がたまたま 0 分でなかったためである。0 分の run が混ざる組では、試し取りの 2 件が共に 0 分になる配置が他の期間で起こり得る。今回は 0 件でも、起きると大きいリスクとして残る

制約:

- 母集団は #199 と同じ 8 日分の部分的なセルで、#199 の制約（組の run 数が本番の期間より小さい等）はそのまま当てはまる。長い期間では打ち切りで浮く予算が増えるため、比例配分の効果は本結果より大きくなり得る
- 新しい失敗の型の発生率はこの母集団では推定できない。感度分析は本番の試し取り位置（先頭と末尾）とは異なる配置での目安にすぎない
- jobs 呼び出し数は jobs の 1 ページ目だけを数え、runs 一覧の呼び出しは含まない（#199 と同じ）

### レート制限と終了コード

- `gh api rate_limit` の `resources.core.remaining` を、run やリポジトリの境目で、API 呼び出し 25 回ごと（`--rate-check-every`）に確認する
- 残りが `--min-remaining`（既定 200）を下回ったら、キャッシュを保存して終了コード 3 で止まり、`--out` は書かない。reset 後に同じコマンドで再実行すると続きから取る
- `--max-seconds N`（既定 0 = 無制限）に達したら、run / リポジトリの境目でキャッシュを保存して終了コード 4 で止まり、`--out` は書かない。待たずに同じコマンドを再実行してよい。一覧と jobs のそれぞれで 1 単位は処理してから止まるため、小さな値でも再実行を重ねれば必ず前進する
- 一時的な 5xx（HTTP 500〜599）は `--retries`（既定 3、0〜10）回まで、`--retry-wait`（既定 5 秒、0〜60）から始まる指数バックオフ（5・10・20 秒。1 回 60 秒上限）で再試行する。対象は fetch_jobs.py の runs / jobs / `rate_limit` / リポジトリ一覧の全呼び出し（すべて GET で冪等）。4xx（403 の secondary rate limit・429 を含む）、レート制限（終了コード 3）、タイムアウトは再試行しない。上限まで 5xx が続くと `gh api 失敗（HTTP 502・5xx を 3 回再試行しても回復しない）` で終了コード 1。再試行の待ちは `--max-seconds` の予算の外で、その上に乗る（予算は境目でしか見ないため）。5xx の判定は gh の stderr の `HTTP <3 桁>` に基づく。4xx の形式は実測したが、実機の 5xx は未検証
- 進捗は stderr に数値だけの行（`進捗: runs 一覧 k/N リポジトリ・API 呼び出し C・経過 S 秒`、`進捗: jobs 取得 k/M run ...`）を出す。リポジトリ名は出さない
- 呼び出し回数の目安は、リポジトリ一覧のページ数、リポジトリごとの runs のページ数（二分探索の分を含む）、新しい run ごとの jobs のページ数の合計（推定モードでは jobs は抜き取った run の分だけ）
- 1 時間あたりの REST 上限は認証の種類で変わる。PAT・OAuth は 5,000 回、`GITHUB_TOKEN` は 1,000 回（同梱の `overview/rate-limits.md` による）

| 終了コード | 意味 |
|---|---|
| 0 | 成功 |
| 1 | 失敗（`--out` は書かない。5xx の再試行上限超過を含む） |
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

確認日は 2026-10-24（Actions 課金ページの含有分・larger runner の記述のみ 2026-10-28）。

- https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens : `orgs/<org>/repos` は Repository "Metadata"（read）、runs / jobs は Repository "Actions"（read）。`organizations/<org>/settings/billing/usage` は Organization "Administration"（read）。`GET /orgs/{org}` は載っていない
- https://docs.github.com/en/rest/billing/usage : 呼び出せるのは organization の administrator。billing manager の記載は無い。fine-grained token は "Administration" organization（read）で対応と記載。「過去 24 か月のみ」の注記は ai_credit / premium_request / usage/summary にあり、`/settings/billing/usage` 本体には保持期間の記載が無い（未確認）
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs : workflow run・失敗した job・個別の job は初回実行から 30 日まで再実行できる（同梱 docs の `actions/limits.md` に再実行 50 回の上限もある）
- https://docs.github.com/en/rest/orgs/orgs : `GET /orgs/{org}` の全詳細は org owner のみ。OAuth app token と classic PAT は `admin:org`、GitHub App は Organization plan 権限
- 使用状況レポートのチュートリアル（公式。URL は特定できておらず 2026-10-24 は未再確認）: 課金系エンドポイントは fine-grained token 非対応とする記述（上の rest/billing/usage と食い違う）
- https://docs.github.com/en/billing/reference/billing-reports : 利用はすべて UTC で記録される。集計レポートは最大 1 年・詳細レポートは最大 31 日（Web UI のレポート期間で、API の保持期間ではない）。詳細レポートは Web UI 専用で、REST API は集計済みの情報だけを返す
- https://docs.github.com/en/billing/concepts/product-billing/github-actions : 含有分の消費の倍率は記載なし（SKU ごとの分単価のみ）。larger runner は常に課金される。arm64 が含有分を消費するかは記載なし。artifact storage の使用量は 6〜12 時間以内に更新される（minutes の記載ではない）
- 未確認: Actions の分数について請求画面と API の更新間隔・時差（https://docs.github.com/en/billing/how-tos/products/view-productlicense-use を含め、上記のどのページにも記載が無い）

## 関連

- [SKILL.md](../SKILL.md)
- [`fetch_billing.py`](../scripts/fetch_billing.py)
- [`fetch_jobs.py`](../scripts/fetch_jobs.py)

## ページへ渡る値

出典: claude.ai の Dashboard 型 Artifact の `SKILL.md`（An attached file）と `artifact-type/reference/features.md`（A loader）。2026-10-28 に Artifact の read で確かめた。

- 添付する JSON は「行オブジェクトの配列、または `rows` / `data` / `results` の下にある配列」。`dash.data(id).data` には行の配列だけが届き、セルは文字列として扱うのが安全
- `dash.data(id).meta` に値が入るのは、query dataset の `dash.loader` が `meta` を返したときだけで、`kind: "file"` の dataset では使えない
- したがって fetch 出力のトップレベルの `since` / `until` / `warnings` / `estimate` / `assume_current_visibility` / `generated_at` はそのままではページに届かない
- そこで source ごとに付帯情報の dataset を 1 つ対応させる（`billing` ↔ `billing_meta`、`jobs` ↔ `jobs_meta`）。build_dashboard が検証済みの rows 文書から `meta-<source>.json` を作る。行は `{"source": "billing"|"jobs", "kind": "since"|"until"|"warning"|"estimate"|"assume_current_visibility", "value": "<文字列>"}`
  - `since` / `until`: 1 行ずつ（billing は `YYYY-MM`、jobs は `YYYY-MM-DD`）
  - `warning`: `warnings` の文字列要素を先頭から最大 20 件。制御文字を除き 300 字で切る
  - `estimate`: 文書の `estimate` がオブジェクトのときだけ 1 行（`value` は `"true"`。中身は渡さない）
  - `assume_current_visibility`: billing 文書の `assume_current_visibility` が `true` のときだけ 1 行（`value` は `"true"`）。偽・欠落なら行を作らない。真偽値以外なら build_dashboard は止まる（`summarize.py` と同じ）
- ページは `withMeta` で行配列と meta 行から `{rows, since, until, warnings, estimate?, assume_current_visibility?}` を組み直し、既存の `span` / `coverage` / `isEstimate` と `quotaApprox` に渡す。meta が読めない（`missing` / `error`、行なし）ときは取得期間が `unknown` になり、注記を出す（読み込み中は出さない）
- source ごとに分けるのは、jobs だけの更新で billing の取得期間が消えないようにするため。meta は rows と同じ版固定・同じ更新の扱いで、meta の警告文も rows と同様にダッシュボードを開ける全員が読める
