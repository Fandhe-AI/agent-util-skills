# Agent Util Skills

Claude Code 向けのユーティリティスキル集。`create-html-report`（自己完結 HTML レポート生成）と `setup-firebase-hosting`（Firebase Hosting 公開環境構築）の 2 スキルを、開発ワークフロースキル集 [Fandhe-AI/agent-cli-skills](https://github.com/Fandhe-AI/agent-cli-skills) から本リポジトリへ移設して独立管理する。加えて `create-pitch-deck`（企画提案スライド生成）と `create-design-doc`（UI/UX 設計資料生成）を本リポジトリで新規開発し、合わせて 4 スキルを上流ソースとして管理する。

インストールには [vercel-labs/skills](https://github.com/vercel-labs/skills) CLI を使用する。

## 使い方 — スキルの追加

```bash
# スキル一覧を表示
npx skills add github:Fandhe-AI/agent-util-skills --list

# 特定のスキルを追加（例: create-html-report）
npx skills add github:Fandhe-AI/agent-util-skills --skill create-html-report

# 複数のスキルを追加
npx skills add github:Fandhe-AI/agent-util-skills --skill create-html-report --skill setup-firebase-hosting

# 全スキルを追加
npx skills add github:Fandhe-AI/agent-util-skills --all
```

デフォルトではシンボリックリンクとして `.claude/skills/` に追加される。`--copy` でファイルコピー、`-g` でグローバル（`~/.claude/skills/`）にインストールできる。

## スキル一覧

### create-html-report

分析・比較・調査結果・進捗・計画を、意思決定しやすい自己完結 HTML レポートとして生成する。「HTML レポート作って」「レポートにまとめて」「比較を可視化」「グラフで見せて」「ガントチャート作って」「ダッシュボード風にまとめて」「見やすくまとめて」で使用する。

- データと伝えたい関係に応じて `bar` / `line` / `scatter` / `heatmap` / `waterfall` / `donut` / `radar` / `gantt` の 8 種類から適切な chart type を選定する
- Python 標準ライブラリのみで動く renderer（`scripts/render_report.py`）が中間 report spec（JSON）から単一 HTML ファイルを生成し、`scripts/validate_report.py` が 25 項目以上を機械検証する
- 外部 CDN・外部 font・外部 JavaScript library・外部 stylesheet・外部画像に依存せず、ページロード時に外部通信しない
- アクセシブル（WCAG 志向・色だけに依存しない・screen reader 対応）・レスポンシブ・dark mode・印刷対応
- 詳細は [skills/create-html-report/SKILL.md](skills/create-html-report/SKILL.md)、chart 選定・レポート設計・spec schema・a11y/セキュリティ規約は `references/` を参照

### setup-firebase-hosting

静的サイトを Firebase Hosting（Spark プラン・課金なし）で公開し、GitHub Actions から自動デプロイする環境をコードで構築する。「Firebase で公開したい」「無料でデプロイ」「CI からデプロイ」などで使用する。

- GCP プロジェクト作成・API 有効化・CI 用サービスアカウント発行・GitHub Secret/変数登録・`firebase.json` 生成・デプロイ workflow 生成までを `scripts/bootstrap-firebase.sh` で一括構築する
- 公開先は請求先アカウント未紐付けの Spark プランを既定とし、アクセス集中時も課金が発生しない設計
- サービスアカウント鍵の発行記録に基づく安全な鍵ローテーション、PR 起動 workflow への secret 非付与など、セキュリティを優先した構成をガイドする
- 詳細は [skills/setup-firebase-hosting/SKILL.md](skills/setup-firebase-hosting/SKILL.md)、GCP/Firebase 環境構築スクリプトは `scripts/bootstrap-firebase.sh` を参照

### create-pitch-deck

事業企画の専門家として、アイデア・要件文書から企画提案スライド（PPTX・16:9・Google スライド取込可）を生成する。「ピッチデック作って」「企画スライドにまとめて」「プレゼン資料作って」「勝ち筋を整理して」などで使用する。

- 表紙 / 前提と解釈 / 課題 / 解決アプローチ / 対象範囲（In/Out） / 勝ち筋 / 利用ストーリー / 検証計画・現在地 / フィードバック観点の 9 枚固定構成
- `scripts/build_deck.py` が deck spec（JSON）から python-pptx で PPTX を生成し、`scripts/validate_deck.py` がスライド境界のはみ出し・日本語フォント fallback（`a:latin`/`a:ea`）・必須スライド配置を機械検証する
- `create-design-doc` と共有合意文書 `concept-brief.md` を介して整合し、事業企画とデザイン設計の食い違いを防ぐ
- 詳細は [skills/create-pitch-deck/SKILL.md](skills/create-pitch-deck/SKILL.md)、deck spec の仕様は `references/deck-spec.md` を参照

### create-design-doc

UX/UI デザイナーの専門家として、アイデア・要件文書から UI/UX 設計資料一式（design-doc.md・画面遷移図・HTML ワイヤーフレーム・PC/モバイルスクリーンショット）を生成する。「UI設計して」「ワイヤーフレーム作って」「画面遷移図作って」などで使用する。

- Figma ではなく自己完結 HTML ワイヤーフレームを採用し、Playwright（Chromium）でスクリーンショット・画面遷移図の PNG を生成する
- `scripts/check_overflow.py` が PC/モバイル双方の横スクロール崩れ・外部 CDN 依存の有無を機械検証する
- `create-pitch-deck` と共有合意文書 `concept-brief.md` を介して整合する
- 詳細は [skills/create-design-doc/SKILL.md](skills/create-design-doc/SKILL.md) を参照

各スキルのネットワーク・sandbox 実行要件は [docs/skill-network-requirements.md](docs/skill-network-requirements.md) を参照。

## リポジトリ構成

```text
skills/                                -- 本リポジトリが上流ソースとして管理する 4 スキル
  create-html-report/
    SKILL.md                           -- JSON report spec を Python renderer で自己完結 HTML レポート生成
    references/                        -- chart 選定・レポート設計・spec schema・a11y/セキュリティ規約
    samples/                           -- report spec の JSON 例（comparison / gantt / time-series）
    scripts/                           -- render_report.py（spec→HTML renderer）、validate_report.py（25 項目検証）
  setup-firebase-hosting/
    SKILL.md                           -- Firebase Hosting（Spark プラン）への公開環境をコードで構築
    scripts/                           -- bootstrap-firebase.sh（GCP/Firebase 環境構築スクリプト）
    tests/                             -- firebase-tools バージョン固定の回帰テスト
  create-pitch-deck/
    SKILL.md                           -- 事業企画の専門家として deck spec（JSON）から企画提案 PPTX を生成
    references/                        -- deck-spec.md・concept-brief-schema.md（create-design-doc と共有）
    samples/                           -- deck spec の JSON 例
    scripts/                           -- build_deck.py（spec→PPTX renderer）、validate_deck.py（はみ出し・フォント・必須スライド検証）
  create-design-doc/
    SKILL.md                           -- UX/UI デザイナーの専門家として design-doc.md・画面遷移図・HTML ワイヤーフレーム・スクショを生成
    references/                        -- design-doc-structure.md・wireframe-guidelines.md・concept-brief-schema.md（create-pitch-deck と共有）
    templates/                         -- flow-diagram-template.html・wireframe-template.html
    scripts/                           -- capture_screenshot.py（Playwright PNG 撮影）、check_overflow.py（レイアウト崩れ検証）
.agents/skills/                        -- vendored スキル（消費専用・編集は上流で）
  <開発ワークフロー20件>                 -- Fandhe-AI/agent-cli-skills から日次同期
  anthropic-claude-code/                -- Fandhe-AI/agent-reference-skills から日次同期
  anthropic-claude-code-extend/
  github-docs/
skills-lock.json                       -- vendored スキルの source・computedHash 台帳
.claude/
  agents/
    research/
      skill-explorer.md                -- skills/ 横断調査・読み取り専用（Sonnet）
      sub-investigator.md              -- gh/git/CLI/hook 失敗調査（Sonnet）
      reference-researcher.md          -- 公式ドキュメント調査（Sonnet）
    author/
      skill-author.md                  -- skills/<name>/SKILL.md 作成編集（Sonnet）
      agent-author.md                  -- .claude/agents 作成編集（Sonnet）
      rules-author.md                  -- .claude/rules 作成編集（Sonnet）
      docs-writer.md                   -- CLAUDE.md/README 一覧・ツリー更新（Haiku）
    quality/
      skill-reviewer.md                -- SKILL.md 品質レビュー・読み取り専用（Sonnet）
      security-auditor.md              -- OWASP 監査・読み取り専用（Sonnet）
      frontmatter-linter.md            -- frontmatter/symlink 機械検証（Haiku）
      plan-verifier.md                 -- 計画検証・読み取り専用（Sonnet）
  rules/                                -- 13 件（delegation・skill-authoring・security 等）
  skills/                                -- symlink 集約
    create-html-report -> ../../skills/create-html-report
    setup-firebase-hosting -> ../../skills/setup-firebase-hosting
    create-pitch-deck -> ../../skills/create-pitch-deck
    create-design-doc -> ../../skills/create-design-doc
    create-skill/                      -- リポジトリ管理スキル（実ディレクトリ）
    create-agent/                      -- リポジトリ管理スキル（実ディレクトリ）
    (他は .agents/skills/ への symlink)
  settings.json                        -- SessionStart hook（リマインダー）
.github/
  workflows/
    ci.yml                             -- スキル構造検証 + lint-docs
    codex-review.yml                   -- Codex PR レビュー（Fandhe-AI/actions 呼び出し）
    update-external.yml                -- vendored スキルの日次同期
  scripts/
    check-skill-structure.sh           -- SKILL.md frontmatter・skills-lock.json 検証
docs/
  README.md                            -- docs/ の索引
  skill-network-requirements.md        -- 各スキルのネットワーク・sandbox 実行要件
AGENTS.md                              -- codex-review のレビュー観点集
CLAUDE.md                              -- Claude Code 向け運用ガイド
```

## vendored スキルの扱い

`.agents/skills/` 配下と `skills-lock.json` は [Fandhe-AI/agent-cli-skills](https://github.com/Fandhe-AI/agent-cli-skills)（開発ワークフロースキル 20 件）と [Fandhe-AI/agent-reference-skills](https://github.com/Fandhe-AI/agent-reference-skills)（`github-docs` / `anthropic-claude-code` / `anthropic-claude-code-extend`）からの **消費専用の取り込みコピー**である。`.github/workflows/update-external.yml` が日次で上流の変更を取り込み、`skills-lock.json` の `computedHash` を同期する。

- 本リポジトリ側で `.agents/skills/` を直接編集しない。修正は上流リポジトリへ行う
- 上流の `source` を持つ vendored スキルへ手元で修正を加えた場合の PR 作成・upstream 反映は `contribute-skill` スキル（vendored 側に含まれる）を使う
- 自前で管理する 4 スキル（`create-html-report` / `setup-firebase-hosting` / `create-pitch-deck` / `create-design-doc`）だけが `skills/` 配下の実ディレクトリであり、通常の編集フロー（`skill-author` への委譲 + `update-docs`）に従う

## 関連リポジトリ

- [Fandhe-AI/agent-cli-skills](https://github.com/Fandhe-AI/agent-cli-skills) — 本リポジトリが移設元・vendoring 元とする開発ワークフロースキル集
- [Fandhe-AI/agent-reference-skills](https://github.com/Fandhe-AI/agent-reference-skills) — Claude Code / GitHub 等のリファレンススキル集（vendoring 元）
