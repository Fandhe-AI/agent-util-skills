# CLAUDE.md

This file provides guidance to Claude Code（claude.ai/code）when working with code in this repository.

## Overview

Claude Code 向けユーティリティスキル集。[Fandhe-AI/agent-cli-skills](https://github.com/Fandhe-AI/agent-cli-skills) から `create-html-report`（自己完結 HTML レポート生成）と `setup-firebase-hosting`（Firebase Hosting 公開環境構築）の 2 スキルを移設し、加えて `create-pitch-deck`（企画提案スライド生成）と `create-design-doc`（UI/UX 設計資料生成）を新規開発して、合わせて 4 スキルを本リポジトリで独立管理する。開発ワークフロースキル（agent-cli-skills）と参照スキル（agent-reference-skills）は vendoring で取り込み、消費専用として扱う。インストールは [vercel-labs/skills](https://github.com/vercel-labs/skills) CLI を使用する。

## Repository Structure

```text
skills/                               -- 本リポジトリが上流ソースとして管理する 4 スキル（各ディレクトリに SKILL.md）
  create-html-report/
    references/                       -- chart 選定・レポート設計・spec schema・a11y/セキュリティ規約
    samples/                          -- report spec の JSON 例（comparison / gantt / time-series）
    scripts/                          -- render_report.py（spec→HTML renderer）、validate_report.py（25 項目検証）
  setup-firebase-hosting/
    scripts/                          -- bootstrap-firebase.sh（GCP/Firebase 環境構築スクリプト）
    tests/                            -- firebase-tools バージョン固定（version-pin.test.mjs）・SA 鍵削除範囲
                                         と 403 全権限検査の回帰テスト（key-deletion-authority.test.mjs /
                                         perm-check-403.test.mjs）
  create-pitch-deck/
    references/                       -- deck-spec.md（deck spec スキーマ）・concept-brief-schema.md（create-design-doc と共有）
    samples/                          -- deck spec の JSON 例
    scripts/                          -- build_deck.py（spec→PPTX renderer）、validate_deck.py（はみ出し・フォント・必須スライド検証）
  create-design-doc/
    references/                       -- design-doc-structure.md・wireframe-guidelines.md・concept-brief-schema.md（create-pitch-deck と共有）
    templates/                        -- flow-diagram-template.html・wireframe-template.html
    scripts/                          -- capture_screenshot.py（Playwright PNG 撮影）、check_overflow.py（レイアウト崩れ検証）
.agents/skills/                       -- vendored スキル（消費専用。npx skills update で同期・直接編集しない）
  comment-code/ create-commit/ create-issue/ create-issue-tree/ create-plan/ create-pr/
  implement-issue/ implement-issue-tree/ implement-review/ implement-review-pr/ init-claude/
  project-add-items/ project-archive-done/ project-create-issues/ project-init/
  project-sync-issues/ project-update-items/ project-view-status/ sync-skills-lock/
  update-claude/ update-docs/ update-issue-tree/     -- Fandhe-AI/agent-cli-skills から日次同期（20件）
  anthropic-claude-code/ anthropic-claude-code-extend/ github-docs/
                                       -- Fandhe-AI/agent-reference-skills から日次同期（3件）
skills-lock.json                      -- vendored スキルの source・skillPath・computedHash 台帳
.claude/
  agents/
    research/
      skill-explorer.md               -- skills/ 横断調査・読み取り専用（Sonnet）
      sub-investigator.md             -- gh/git/CLI/hook 失敗調査（Sonnet）
      reference-researcher.md         -- 公式ドキュメント調査（Sonnet）
    author/
      skill-author.md                 -- skills/<name>/SKILL.md 作成編集（Sonnet）
      agent-author.md                 -- .claude/agents 作成編集（Sonnet）
      rules-author.md                 -- .claude/rules 作成編集（Sonnet）
      docs-writer.md                  -- CLAUDE.md/README 一覧・ツリー更新（Haiku）
    quality/
      skill-reviewer.md               -- SKILL.md 品質レビュー・読み取り専用（Sonnet）
      security-auditor.md             -- OWASP 監査・読み取り専用（Sonnet）
      frontmatter-linter.md           -- frontmatter/symlink 機械検証（Haiku）
      plan-verifier.md                -- 計画検証・読み取り専用（Sonnet）
  rules/
    delegation.md                     -- 委譲の原則（調査・設計フェーズ）
    delegation-impl.md                -- 委譲マッピング（作成・編集フェーズ）
    skill-authoring.md                -- スキル著作規約
    agent-authoring.md                -- エージェント著作規約
    conventional-commits.md           -- Conventional Commits 詳細規約
    security.md                       -- セキュリティチェック規約
    japanese-style.md                 -- 日本語スタイルガイド
    dotclaude-via-temp.md             -- .claude/ 操作時の一時ディレクトリルール
    description-style.md              -- description 著作スタイル
    reference-template.md             -- reference 型スキルの書式規約
    code-comment-style.md             -- コード内コメント・ドキュメンテーションコメント規約
    verification.md                   -- 完了ゲート規約（証拠なき完了宣言の禁止・5段階検証）
    debugging.md                      -- 根本原因デバッグ規約（修正前の原因調査・3回失敗でエスカレーション）
  skills/                             -- skills/ と .agents/skills/ へのシンボリックリンク集約
    create-html-report                -- 自前スキル（symlink: ../../skills/create-html-report）
    setup-firebase-hosting            -- 自前スキル（symlink: ../../skills/setup-firebase-hosting）
    create-pitch-deck                 -- 自前スキル（symlink: ../../skills/create-pitch-deck）
    create-design-doc                 -- 自前スキル（symlink: ../../skills/create-design-doc）
    create-skill/                     -- リポジトリ管理スキル（実ディレクトリ、sample/ に SKILL 雛形）
    create-agent/                     -- リポジトリ管理スキル（実ディレクトリ、sample/ に Agent 雛形）
    (他 create-commit 等は .agents/skills/ への symlink)
  settings.json                       -- SessionStart hook（リマインダー）
.github/
  workflows/
    ci.yml                            -- スキル構造検証（structure）+ lint-docs
    codex-review.yml                  -- Codex PR レビュー（Fandhe-AI/actions を @latest で呼び出す wrapper）
    update-external.yml               -- vendored スキルの日次同期（Fandhe-AI/actions の update-external.yml wrapper）
  scripts/
    check-skill-structure.sh          -- SKILL.md frontmatter（name/description/user-invocable）・skills-lock.json 検証
docs/
  README.md                           -- docs/ の索引
  skill-network-requirements.md       -- 各スキルのネットワーク・sandbox 実行要件
AGENTS.md                             -- codex-review が参照するレビュー観点集
```

## 委譲方針（必読）

main の役割は **対話・計画・委譲・報告** に徹する。token を消費する作業（調査・ファイル作成・編集・レビュー）は専門サブエージェントへ委譲する。

### パスベースの目安

| 操作対象パス | モード | 適用ルール |
|------------|--------|-----------|
| `docs/`・`.claude/` の**閲覧のみ** | 調査・設計モード | `.claude/rules/delegation.md` |
| `skills/`・`.claude/agents/`・`.claude/rules/`・`CLAUDE.md` の**作成・編集** | 作成・編集モード | `.claude/rules/delegation-impl.md` |

`.agents/skills/` は vendored な消費専用ディレクトリのため、このマッピングの対象外（直接編集しない）。

### model 配分戦略

| 用途 | model |
|------|-------|
| 判定・生成（スキル著作・レビュー・調査） | Sonnet |
| 機械的・集計処理（frontmatter lint・ドキュメント更新） | Haiku |
| 複雑な計画立案 | Opus |

### 並列化

独立タスクは**同一メッセージ内で複数 Agent を起動**して並列実行する。依存関係がある場合のみ逐次実行。

例: 「skill-explorer で横断調査」と「reference-researcher で外部仕様確認」は並列起動可。

## Sub-agents

サブエージェントは `.claude/agents/` 配下に `subagent_type: <name>` frontmatter を持つ。`subagent_type: <name>` で呼び出す。

### research/ — 調査系（読み取り専用）

| subagent_type | model | 概要 |
|--------------|-------|------|
| `skill-explorer` | Sonnet | skills/ 横断調査・仕様把握 |
| `sub-investigator` | Sonnet | gh / git / CLI / hook 失敗の調査 |
| `reference-researcher` | Sonnet | 公式ドキュメント・外部仕様の調査 |

### author/ — 作成・編集系

| subagent_type | model | 概要 |
|--------------|-------|------|
| `skill-author` | Sonnet | `skills/<name>/SKILL.md` の作成・編集 |
| `agent-author` | Sonnet | `.claude/agents/` の作成・編集（dotclaude-via-temp 準拠） |
| `rules-author` | Sonnet | `.claude/rules/` の作成・編集（dotclaude-via-temp 準拠） |
| `docs-writer` | Haiku | `CLAUDE.md`・`README.md` の一覧・ツリー更新 |

### quality/ — 品質・検証系（読み取り専用）

| subagent_type | model | 概要 |
|--------------|-------|------|
| `skill-reviewer` | Sonnet | SKILL.md の品質レビュー |
| `security-auditor` | Sonnet | OWASP Top 10 セキュリティ監査 |
| `frontmatter-linter` | Haiku | frontmatter・symlink の機械検証 |
| `plan-verifier` | Sonnet | 計画ファイルの完了検証 |

## Rules

| ファイル | 対象 | 概要 |
|---------|------|------|
| `delegation.md` | main | 調査・設計フェーズの委譲原則 |
| `delegation-impl.md` | main / author 系 Agent | 作成・編集フェーズの委譲マッピング |
| `skill-authoring.md` | skill-author / skill-reviewer | スキル著作フォーマット・品質基準 |
| `agent-authoring.md` | agent-author | エージェント著作フォーマット・品質基準 |
| `code-comment-style.md` | skill-author / 全 author 系 Agent | コード内コメント・ドキュメンテーションコメント規約（役割・境界・文脈） |
| `conventional-commits.md` | commit / PR 作成時 | Conventional Commits 詳細規約 |
| `security.md` | security-auditor / commit・PR 作成時 | OWASP Top 10 セキュリティチェック基準 |
| `japanese-style.md` | 全 Agent | 日本語スタイルガイド |
| `dotclaude-via-temp.md` | agent-author / rules-author | `.claude/` 操作時の一時ディレクトリルール |
| `description-style.md` | skill-author / skill-reviewer | description 著作スタイル（発火率・長さ・YAML 落とし穴） |
| `reference-template.md` | skill-author | reference 型スキルの reference/*.md と README 索引の書式規約 |
| `verification.md` | skill 修正・レビュー作業全般 | 完了ゲート（証拠なき完了宣言の禁止・5段階検証） |
| `debugging.md` | skill 修正作業 / sub-investigator | 根本原因デバッグ（修正前の原因調査・3回失敗でエスカレーション） |

## Current Skills

### 自前管理スキル（4件・skills/ に配置）

create-html-report, setup-firebase-hosting, create-pitch-deck, create-design-doc

### リポジトリ管理スキル（.claude/skills/ に配置）

create-skill, create-agent

### vendored スキル（消費専用・.agents/skills/ に配置。編集は上流リポジトリで行う）

- **Fandhe-AI/agent-cli-skills 由来（20件）**: comment-code, create-commit, create-issue, create-issue-tree, create-plan, create-pr, implement-issue, implement-issue-tree, implement-review, implement-review-pr, init-claude, project-add-items, project-archive-done, project-create-issues, project-init, project-sync-issues, project-update-items, project-view-status, sync-skills-lock, update-claude, update-docs, update-issue-tree
- **Fandhe-AI/agent-reference-skills 由来（3件）**: github-docs, anthropic-claude-code, anthropic-claude-code-extend

vendored スキルは `.github/workflows/update-external.yml` の日次同期で更新される。手元で改修した vendored スキルを upstream へ反映する場合は `contribute-skill` を使う。

## Conventions

### Conventional Commits

全スキルで `type(scope): subject` 形式を徹底する。

- **Types:** feat, fix, docs, refactor, test, chore, style, build, ci, perf
- **Subject:** 72 文字以下、命令形/現在形、日本語可
- **Breaking Changes:** `!` 接尾辞 or body に `BREAKING CHANGE:`

### .claude/ ディレクトリ操作

`.claude/` 配下のファイル作成・編集は `_/dotclaude/` で一時作業し、完了後に `mv` で移動する。`rm -rf _/dotclaude` は禁止（共有ディレクトリのため `rmdir` で空ディレクトリのみ削除）。

### セキュリティレビュー

`skills/create-html-report`・`skills/setup-firebase-hosting`・`skills/create-pitch-deck`・`skills/create-design-doc` の修正時は OWASP Top 10・ハードコードされた秘密情報・XSS・コマンドインジェクション・入力バリデーションを必須チェックする。セキュリティ問題がある場合はマージをブロックする。

### 日本語出力

全スキル・Agent・Rule の出力・レポートは日本語で記述する。

## hooks（settings.json）

`.claude/settings.json` に SessionStart hook を設定する。セッション開始時に以下のリマインダーを echo で出力する。

- 日本語でやりとりする
- 作業は subagent へ委譲し main の token 消費を抑える（delegation.md / delegation-impl.md）
- `.claude/` 配下の編集は `_/dotclaude/` 経由（dotclaude-via-temp）
- Conventional Commits 厳守（`--no-verify` 禁止）

## Skill Anatomy

各スキルは `skills/<name>/SKILL.md` に YAML frontmatter + 手順を記述する。`.claude/skills/` からシンボリックリンクで参照される。

```yaml
---
name: <skill-name>
description: <one-line description>
---
```

## Adding a New Skill

1. `create-skill` スキルを呼び出す（scaffold・symlink・update-docs まで自動化）。
   または手動で行う場合:
   1. `skills/<name>/SKILL.md` を作成（frontmatter + 手順）
   2. `.claude/skills/<name>` にシンボリックリンクを作成:
      `ln -s ../../skills/<name> .claude/skills/<name>`
   3. `update-docs` スキルで CLAUDE.md のスキル一覧・構成を更新

## Adding a New Agent

1. `create-agent` スキルを呼び出す（dotclaude-via-temp 準拠で scaffold）。
   または手動で行う場合:
   1. `_/dotclaude/agents/<category>/<name>.md` に frontmatter + 手順を作成
   2. `mv` で `.claude/agents/<category>/<name>.md` に移動
   3. `update-docs` スキルで CLAUDE.md の Sub-agents 一覧を更新
