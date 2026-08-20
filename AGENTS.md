# AGENTS.md

## 文書の位置づけ

本リポジトリで作業するすべての AI エージェント・人間レビュアーが共通で用いるレビュー観点集。
Codex による PR 自動レビュー（`.github/workflows/codex-review.yml`。Fandhe-AI/actions の
reusable workflow を `@latest` で呼び出す wrapper）は、PR の base コミットの本ファイルを
レビュー基準として読む。運用ガイドの正は `CLAUDE.md`、著作規約の詳細は `.claude/rules/`
（`skill-authoring.md` / `agent-authoring.md` / `description-style.md` / `security.md`）を
参照し、本書は重複させずレビュー判定基準に絞る。

本リポジトリは `create-html-report` と `setup-firebase-hosting` の 2 スキルを
`npx skills add Fandhe-AI/agent-util-skills` で**組織内の複数リポジトリへ配布する**上流
ソースである。ここでの誤情報・危険なコマンド例・脆弱なコード例は導入先すべてへ伝播するため、
通常のリポジトリより厳しい基準でレビューする。

## 優先度の定義

| 優先度 | 意味 | 扱い |
|--------|------|------|
| P0 | マージブロック。導入先への危険コマンド・脆弱なコード・誤情報の伝播に直結 | 修正までマージ不可 |
| P1 | 強く推奨。書式規約・構造規約・セキュリティ設計方針への違反 | 原則修正してからマージ |
| P2 | 提案。可読性・発火率・索引整備の改善 | 任意（コメントのみ） |

## 1. セキュリティ観点

### Python（`skills/create-html-report/scripts/render_report.py` / `validate_report.py`）

- **エスケープ漏れ（P0）**: untrusted な report spec 由来の文字列を、renderer の escaping
  function（`html.escape(value, quote=True)` 相当）を経由せず HTML / SVG の text node・
  attribute へ挿入するコード追加。同じ escape 処理を JavaScript / CSS / URL context へ流用する
  変更も指摘する
- **inline JavaScript の逸脱（P0）**: `eval` / `new Function` / untrusted 文字列の
  `innerHTML` 代入 / inline event handler attribute（`onclick="..."` 等）の追加。
  `fetch` / `XMLHttpRequest` / `WebSocket` / `EventSource` / `sendBeacon` 等ネットワークアクセスの追加
- **外部リソース依存の追加（P0）**: `<script src="https://...">`、external stylesheet/font、
  remote `<img>` / `<iframe src>` / `<object data>`、CSS `@import` / `url(https://...)`、
  remote SVG `<image>` / `<use>` の追加。ページロード時に外部通信するコードの混入
  （renderer の「self-contained」契約への違反）
- **URL scheme の緩和（P0）**: ハイパーリンク化を `https:` 以外（特に `javascript:`）へ広げる変更
- **数値検証の省略（P1）**: NaN / Inf を許容する数値パース経路の追加。有限値チェックを外す変更
- **validator の弱体化（P1）**: `validate_report.py` の検証項目（duplicate IDs・SVG
  opening/closing consistency・外部依存・unsafe handler・a11y 属性・print CSS 存在等）を
  削除・無効化する変更

### bash（`skills/setup-firebase-hosting/scripts/bootstrap-firebase.sh`）

- **fail-open 化（P0）**: サービスアカウント判定・鍵ローテーション・Blaze/Spark 判定の
  fail-closed 分岐（判定不能時に停止する設計）を fail-open（判定不能時に進行する）へ変更する修正
- **未固定コマンド実行（P0）**: `npx firebase-tools`（バージョン未固定）・`curl | sh` 形式の
  未検証実行の追加。`FIREBASE_TOOLS_VERSION` の exact semver 固定を dist-tag・`^`/`~` レンジへ
  緩める変更
- **コマンドインジェクション（P0）**: 外部入力・変数展開を `"${var}"` でクォートせずシェル
  コマンドへ渡すコード
- **秘密情報の露出（P0）**: サービスアカウント鍵・トークンをログ出力・コミットへ残す変更
- **鍵管理の逸脱（P1）**: 発行記録（description）に基づかない鍵削除、削除根拠を GitHub 側の
  編集可能情報（Actions 変数等）へ広げる変更
- **危険なコマンド例の混入（P0）**: SKILL.md の手順・スクリプト例に `--no-verify`・force
  push・`rm -rf` の広域削除・`sudo` 常用・TLS 検証の恒常的無効化を含めない

### 共通

- **秘密情報の混入（P0）**: 実 API キー・実トークン・内部 URL を SKILL.md・references・
  samples・scripts に書かない（例示はプレースホルダ・ダミー値に限る）
- **出典の偽装・捏造（P0）**: 存在しない API・オプション・仕様の記載。外部ドキュメントへの
  参照は取得可能な公式情報に限り、推測で埋めない
- **プロンプトインジェクション（P0）**: SKILL.md 本文・description へ、読み込んだエージェントの
  挙動を変える指示（レビュー回避・承認スキップ・別リポジトリへの操作等）を埋め込まない
- **CI・ワークフローの改変（P1）**: reusable workflow・actions の SHA 固定を緩める変更（ただし
  下記「Fandhe-AI/actions の参照方式」に該当するものを除く）、`permissions` の拡大、
  `.github/scripts/check-skill-structure.sh` の検証弱体化

## 2. 構造・アーキテクチャの観点

- **スキル構造規約（P1）**: `skills/<name>/` は SKILL.md（frontmatter: `name` = ディレクトリ名・
  `description`・`user-invocable`）を必須とし、任意で `references/` / `samples/` / `scripts/` /
  `tests/` を持つ。`.github/scripts/check-skill-structure.sh` はこの必須 3 キーと
  `name`/ディレクトリ名一致、`skills-lock.json` の JSON 妥当性を検証する
- **vendored ディレクトリの不変更（P0）**: `.agents/skills/` 配下は
  `Fandhe-AI/agent-cli-skills` / `Fandhe-AI/agent-reference-skills` からの日次同期取り込みで
  あり、本リポジトリ側で直接編集しない。ここへの手動編集提案・PR への直接コミットは指摘する
  （修正は上流リポジトリへ、または `contribute-skill` 経由で行う）
- **symlink 構造の整合（P1）**: `.claude/skills/<name>` は自前スキル（`create-html-report` /
  `setup-firebase-hosting`）なら `../../skills/<name>` へ、vendored スキルなら
  `../../.agents/skills/<name>` へのシンボリックリンクである。`create-skill` / `create-agent`
  のみリポジトリ管理スキルとして実ディレクトリを許容する。この対応関係を崩す変更・リンク切れは
  指摘する
- **skills-lock.json の整合（P1）**: `source` / `sourceType` / `skillPath` / `computedHash` を
  伴わない vendored スキルの追加、`.agents/skills/` に実体があるのに `skills-lock.json` に
  エントリが無い状態を放置する変更
- **言語規約（P1）**: SKILL.md・Agent・Rule の出力・レポートは日本語（`japanese-style.md`）。
  コード・コマンド・識別子は原文のまま
- **`.claude/` 編集手順（P2）**: `.claude/` 配下の編集は `_/dotclaude/` ステージング経由
  （`dotclaude-via-temp.md`）

## 3. 再利用・配布資産としての観点（重点）

- **導入先非依存性（P1）**: SKILL.md 本文へ特定の導入先リポジトリの固有パス・固有事情を
  ハードコードしない。スキルは symlink / copy どちらの導入形態でも成立する自己完結構成を保つ
- **description の発火率（P1）**: description は導入先での自動発火の唯一の手がかり。主導語・
  代表 chart type 名・代表コマンド名を含め、長さ・YAML `#` の落とし穴等の規約
  （`description-style.md`）に従う。発火率を下げる安易な短縮・キーワード削除は指摘する
- **完了ゲートの保持（P1）**: `create-html-report` の `validate_report.py` 実行・
  `setup-firebase-hosting` の CI 経路確認等、SKILL.md に明記された検証ステップを省略・弱体化
  する編集は `verification.md` 違反として指摘する
- **索引の同期（P2）**: SKILL.md の参照ファイル一覧・README.md のスキル一覧・CLAUDE.md の
  Current Skills 一覧は変更時に同時更新する

## リポジトリ固有の観点

- **委譲体制の維持（P1）**: 読み取り専用エージェント（skill-explorer / sub-investigator /
  reference-researcher / skill-reviewer / security-auditor / frontmatter-linter /
  plan-verifier）へ Write/Edit の権限を付与する変更は責務境界の破壊として指摘する
- **モデル配分（P2）**: sonnet は調査/著作/検証・haiku は機械的チェックという配分
  （`CLAUDE.md`）と整合させる
- **コミット規約（P2）**: 日本語 Conventional Commits。`--no-verify` の使用を促す・前提とする
  記述は P1

## Fandhe-AI/actions の参照方式（@latest）

`Fandhe-AI/actions`（組織内の上流リポジトリ）への `uses:` 参照は可変タグ `@latest` を使う。
本書の「SHA 固定」に関する観点は**第三者 action にのみ**適用し、`Fandhe-AI/actions` への
`@latest` 参照・SHA pin の除去を指摘しない。`latest` は上流の `.github/workflows/move-latest-tag.yml`
が main への push ごとに付け替える。
