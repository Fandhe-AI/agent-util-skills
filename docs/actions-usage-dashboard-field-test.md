# actions-usage-dashboard 実 org 実測記録

`skills/actions-usage-dashboard` を管理者権限のある実 org（Team プラン）に通した結果の記録である。
本リポジトリは PUBLIC のため、org 名・非公開リポジトリ名・金額（USD）・Artifact の URL は書かない。

## 概要

| 項目 | 内容 |
|------|------|
| 実施日 | 2026-10-23（UTC） |
| 認証 | `gh` の classic token（scope に `admin:org`）。GET のみ使用 |
| スクリプトの基準 | origin/main `f77fa00` |
| 結論 | 取得系（billing）と summarize は実データで動作。jobs 取得・Artifact 系・請求画面照合・翌日更新は未検証 |

## 作成フロー

- billing 取得（`--since 2026-09`）は成功した。1142 行、期間 2026-09..2026-10
- 既定（`--assume-current-visibility` なし）では全行が `unknown` になり、`summarize --plan team --month 2026-09` の含有枠は下限表示（`lower_bound: true`、quota 0）になった。data-sources.md の記述どおりの挙動である
- `--assume-current-visibility` 付きの取り直しでは paid 0 / included 64 行 / public_free 1077 行 / unknown 1 行になった
- Artifact の作成（quickstart、Dashboard 型 publish、アップロード、batch、check 文書）は未検証。理由: 自動運転の実行ではデータ本体を非公開リポジトリ名ごと Artifact へ載せる公開範囲の判断を利用者に確認できないため実施しなかった
- 軸の切り替え・無料枠トグルの不変条件検査（node:vm 評価）は未実施

## 確認フロー

`summarize.py --plan team --month 2026-09 --json` の含有枠（`--assume-current-visibility` 付き）:

| 項目 | 値 |
|------|----|
| 含有分（included_minutes） | 3000 分 |
| 実分（minutes） | 1018 分 |
| quota 換算（quota_minutes） | 約 1180.7 分 |
| 割合（ratio） | 約 39.4 % |
| 残り（remaining） | 約 1819.3 分 |
| warnings | 0 件 |

- 請求画面の「N min used / M min included」との照合は未検証。旧 API `orgs/<org>/settings/billing/actions` は 410 Gone で、請求画面の値は API から直接取れない
- `organizations/<org>/settings/billing/usage/summary`（public preview）は 200 を返した。値の突き合わせは未実施
- 含有分 3000 分（team）が請求画面の「M min included」と一致するかは未検証

## 更新フロー

- fetch_jobs の初回実行（`--since 2026-09-01`、`--cache` 指定）は 600 秒の timeout で前景完了せず、結果を得られなかった。API 呼び出し数の初回 / 2 回目の比較は未検証
- 所感: 大規模 org では fetch_jobs の初回取得が 10 分を超える。`--min-remaining` や期間の短縮を先に使う運用が必要になり得る
- dataset の version 増加・URL 不変・翌日以降の日の増加は未検証

## トークン節約の確認

- Read / `cat` / `jq` した対象: スクリプトの標準出力、`summarize --json` の集計フィールドのみ
- rows JSON・キャッシュ・index.html・`dataset-*.json`・`dash-*.json` は開いていない

## 未検証事項と後続手順

1. 対話セッションで SKILL.md の作成フローを実行し、check 文書の成功状態と batch 全件成功を確認する
2. fetch_jobs を期間を絞って実行し、同じ `--cache` で 2 回目を実行して「API 呼び出し: 計 N」の減少を記録する
3. 請求画面（Settings → Billing → Actions）の used / included と、summarize の含有枠消費を比べる
4. UTC で翌日以降に更新フローを実行し、行の最新日が進み URL が変わらないことを確認する

## スコープ外として見つけた事項

- 大規模 org で fetch_jobs が 10 分超になる点の改善（進捗表示・上限指定）。対応案: 進捗行の出力と実行時間の目安を SKILL.md に追記する。Issue 番号 TBD
