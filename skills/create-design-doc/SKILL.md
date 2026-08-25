---
name: create-design-doc
description: >
  UX/UI デザイナーの専門家として、アイデア・要件文書から UI/UX 設計資料一式
  (design-doc.md・画面遷移図・HTML ワイヤーフレーム・PC/モバイルスクリーンショット) を生成。
  「UI設計して」「ワイヤーフレーム作って」「画面遷移図作って」「デザイン資料まとめて」で使用。
  Figma ではなく自己完結 HTML + Playwright でワイヤーフレームを作る。企画は create-pitch-deck
  が担当し、両スキルは concept-brief.md を介して整合する。
model: sonnet
user-invocable: true
argument-hint: "<入力文書のパス> [--output <dir>] [--brief <path>]"
tools: [Bash, Read, Write, Glob, Grep]
---

# create-design-doc

$ARGUMENTS で渡された入力文書（アイデア・要件文書）を読み、**UX/UI デザイナーの専門家**として
UI/UX 設計資料一式を生成する。

プロト開発の前に「どんな見た目で・どんな場面で・どう使うか」を示す設計資料を作り、ユーザーの
フィードバック・修正を挟むことで、期待値ずれを開発前に検出することが本スキルの存在理由であ
る。そのため成果物は必ず**冒頭に「前提と解釈」**、**末尾に「フィードバック観点」**を含む。

## Figma ではなく HTML ワイヤーフレームを採用する理由

Figma はファイル生成 API が無くエージェント生成に不向き。HTML はブラウザ確認・スクショ資料
化・実装への直接流用が可能なため採用する。

## ペルソナ

実行主体は UX/UI デザイナーの専門家として振る舞い、常に次を自問しながら作業する。

- 想定ユーザーは、いつ・どこで・何のためにこの画面を開くか
- 画面の目的は1つに絞れているか。情報の優先順位は妥当か
- 状態（空・読込中・エラー・正常）を考慮しているか
- 色・タイポ・余白は一貫しているか（デザイントークンから逸脱していないか）
- PC とモバイルで体験が破綻していないか

## 連携: create-pitch-deck との整合（concept-brief.md）

本スキルと `create-pitch-deck` は、共有合意文書 `concept-brief.md` を介して整合する
（スキーマは [references/concept-brief-schema.md](references/concept-brief-schema.md)）。
企画（`create-pitch-deck`）とデザイン（本スキル）が別々に作った成果物で「言っていることが
食い違う」事態を防ぐための仕組みであり、両スキルとも同じ手順（Step 1・Step 8 相当）を持つ。

- 続けて両方を実行する場合は **企画 → デザイン** の順を推奨する。concept-brief.md は先に
  実行される `create-pitch-deck` が起案する（本スキルが先に実行された場合は本スキルが起案
  する）。
- 2スキルで確実に共有するには、両方の呼び出しで**同一の `--brief <path>`** を明示指定する
  （既定値はスキルごとの成果物ディレクトリ直下で異なるため、指定なしでは自動的には共有
  されない）。

## 使い方

引数でアイデア・要件文書のパス（ディレクトリまたはファイル列挙）を渡す。引数が無い・曖昧な
場合は Step 2 でユーザーに確認する。

- 出力先ディレクトリはユーザー指定がなければ `design/`
- `--brief <path>` で concept-brief.md の場所を指定できる（既定 `<output>/concept-brief.md`）
- 利用例: アイデア検討フロー（`01-brainstorm.md` 〜 `03-poc/`）を入力とする場合、
  `ideas/<name>/` をそのまま渡す

## 成果物

| ファイル | 内容 |
|---------|------|
| `design/design-doc.md` | 前提と解釈 / 想定ユーザーと主要シナリオ / 画面一覧 / 情報設計 / デザイントークン / フィードバック観点。詳細構成は [references/design-doc-structure.md](references/design-doc-structure.md) |
| `design/flow.png` | 画面遷移図。HTML/SVG（[templates/flow-diagram-template.html](templates/flow-diagram-template.html)）を Playwright で PNG 化。アクター別レーン・フロー色分け・凡例必須 |
| `design/wireframes/*.html` | 主要画面の自己完結 HTML ワイヤーフレーム。規約は [references/wireframe-guidelines.md](references/wireframe-guidelines.md) |
| `design/screens/*.png` | 各ワイヤーフレームの PC/モバイルスクリーンショット（Playwright） |

## フロー

### Step 1: concept-brief.md を確認する

`--brief` で指定されたパス（既定 `<output>/concept-brief.md`）を確認する。

- **存在する場合**: 必須入力として読む。Step 2 の文書読解の結果と矛盾を感じた場合、
  concept-brief.md を無断で上書きせず、矛盾点を明示してユーザーに確認する。
- **存在しない場合**: Step 2 の文書読解を踏まえ、
  [references/concept-brief-schema.md](references/concept-brief-schema.md) のテンプレート
  で起案する。各項目に出典を付ける。Step 3 の骨子確認と同時にユーザーへ提示し、承認を得て
  から確定版を書き出す。

### Step 2: 入力文書を読解する

対象文書を読み、目的・要件概要・PoC 結果・未解決疑問点を把握する。

### Step 3: 画面一覧＋主要フロー骨子を提示しユーザー承認を得る（必須ゲート）

画面一覧（各画面の目的1行）と主要フロー（アクター・ステップ概要）、Step 1 の
concept-brief.md 案（起案した場合）をまとめてユーザーに提示する。**承認を得るまで Step 4
へ進まない**。無承認のまま生成しない。

### Step 4: venv を用意し Playwright を導入する

まず `python3` の存在を確認する。

```bash
command -v python3 >/dev/null || echo "python3 が見つからない"
```

未導入の場合は処理を中止し、導入方法を案内する（macOS: `brew install python3`。その他の環境
は各環境の公式手順）。

```bash
python3 -m venv "_/design-doc/.venv" 2>/dev/null || true
"_/design-doc/.venv/bin/pip" install -q playwright
"_/design-doc/.venv/bin/playwright" install chromium
```

グローバル環境を汚染しないよう、必ずスキル専用の venv（`_/design-doc/.venv` 等）へインス
トールする。`playwright install chromium` はブラウザ本体（100MB超）をダウンロードするため
時間を要する。

### Step 5: 生成する

順序: 画面遷移図 → ワイヤーフレーム → スクリーンショット → design-doc.md。

1. **画面遷移図**: [templates/flow-diagram-template.html](templates/flow-diagram-template.html)
   をコピーし、Step 3 で承認された主要フローに合わせてレーン・ノード・エッジ・凡例を書き
   換える。次で PNG 化する。

   ```bash
   "_/design-doc/.venv/bin/python3" "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" \
     --html "<output>/flow.html" --out "<output>/flow.png" --width 1440 --height 720 --full-page
   ```

2. **ワイヤーフレーム**: 画面ごとに
   [templates/wireframe-template.html](templates/wireframe-template.html) をコピーし、
   [references/wireframe-guidelines.md](references/wireframe-guidelines.md) に従って画面
   内容へ書き換える（デザイントークンは design-doc.md と同じ値にする）。

3. **スクリーンショット**: ワイヤーフレームごとに PC・モバイル双方を撮影する。

   ```bash
   "_/design-doc/.venv/bin/python3" "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" \
     --html "<wireframe.html>" --out "<output>/screens/<screen>-desktop.png" --width 1440 --height 900 --full-page
   "_/design-doc/.venv/bin/python3" "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" \
     --html "<wireframe.html>" --out "<output>/screens/<screen>-mobile.png" --width 375 --height 812 --full-page
   ```

4. **design-doc.md**: [references/design-doc-structure.md](references/design-doc-structure.md)
   の節構成で作成する。フィードバック観点は3〜5件、ユーザーに実際に確認してほしい具体的な
   問いにする。

### Step 6: 検証する

ワイヤーフレームごとにレイアウト崩れを機械検証する。

```bash
"_/design-doc/.venv/bin/python3" "${CLAUDE_SKILL_DIR}/scripts/check_overflow.py" "<wireframe.html>"
```

FAIL の場合は HTML を修正し、再生成してから validator を再実行する。PASS するまで完了扱い
にしない。加えてスクリーンショット（PC/モバイル双方）を目視確認し、崩れが無いことを確認する。

### Step 7: concept-brief.md との相互整合チェック

生成した design-doc.md・画面一覧を concept-brief.md と照合する。

- ターゲットユーザー・主要シナリオ・スコープ・トーン＆マナーに矛盾がないか
- `create-pitch-deck` の成果物（既定 `_/pitch-deck/*.pptx`）が見つかる場合は、勝ち筋と画面
  設計の整合／スコープ外機能の混入／用語・呼称の不一致／課題と解決 UI の対応漏れを横断確認
  する

矛盾を検出した場合は concept-brief.md・本成果物のいずれも自動修正せず、「concept-brief.md
を直すか／本成果物を直すか」をユーザーに提示する。

### Step 8: フィードバック反映ループ

ユーザーからのフィードバックを反映する場合は該当ワイヤーフレーム・doc（必要なら
concept-brief.md も）を修正し、Step 5〜7 を再実行する。

## 検証

生成後、必ず validator を実行し、以下の5段階ゲートで完了を確認する
（`.claude/rules/verification.md` 準拠）。

1. **特定**: `check_overflow.py` の実行と終了コードをもって完了とみなす（ワイヤーフレーム
   ごとに実行）
2. **実行**: `"_/design-doc/.venv/bin/python3" "${CLAUDE_SKILL_DIR}/scripts/check_overflow.py" "<wireframe.html>"` を全ワイヤーフレームに対して新規実行する
3. **読取**: 出力全体（PASS/FAIL・失敗一覧）と終了コードを確認する
4. **検証**: 失敗が0件であること、PC/モバイル双方のスクリーンショットを目視確認し崩れが
   無いこと、Step 7 の相互整合チェックで未解決の矛盾が無いことを確認する
5. **宣言**: 全ワイヤーフレームで validator が PASS し、目視確認・相互整合チェックが済んだ
   場合のみ完了を宣言する。「たぶん崩れていない」等の推測で完了主張しない

`check_overflow.py` は最低限以下を確認する。

- PC（1440px）・モバイル（375px）双方で横スクロールを誘発するオーバーフローが無い
- 外部 CDN・外部フォント・外部 script/stylesheet への参照が無い（自己完結契約）

## よくある失敗

| 問題 | 回避策 |
|------|--------|
| ダミーデータが「サンプル」「テスト」等の placeholder のまま | 実際にありそうな商品名・金額・日付等、具体的でリアルな内容にする |
| モバイルで横スクロールが発生する | `check_overflow.py` を必ず実行し、`@media (max-width: 480px)` 等で1カラム化する |
| design-doc.md とワイヤーフレームでトークンの値が食い違う | `references/design-doc-structure.md` のデザイントークンと `wireframes/*.html` の `:root` を同じ値にする |
| フィードバック観点が形骸化する | 3〜5件を「ユーザーに実際に確認したい具体的な問い」にする |

## 注意事項

- `wireframes/*.html` は CDN・外部フォント・外部画像・外部 JS を一切使用しない
- `_/design-doc/.venv` はビルドツールであり生成物ではない。commit しない（`_/` は
  `.gitignore` 済み）
- 出力先ディレクトリ（`design/` 等）が存在しない場合は `mkdir -p` で作成してから書き出す

## 最終報告

完了時は簡潔に以下を報告する。

- 生成した成果物一覧の絶対パス（design-doc.md / flow.png / wireframes / screens）
- validation result（各ワイヤーフレームの PASS/FAIL）
- concept-brief.md のパスと、新規起案／既存読込のいずれか
- Step 7 の相互整合チェック結果（矛盾の有無）
- 設計方針の要旨を一文

## 参照ファイル

必要な場合だけ読む。

- [references/design-doc-structure.md](references/design-doc-structure.md) — design-doc.md の節構成・デザイントークン記述フォーマット
- [references/wireframe-guidelines.md](references/wireframe-guidelines.md) — HTML ワイヤーフレームの必須要件
- [references/concept-brief-schema.md](references/concept-brief-schema.md) — concept-brief.md のスキーマ（`create-pitch-deck` と共有）
- [templates/flow-diagram-template.html](templates/flow-diagram-template.html) — 画面遷移図の雛形
- [templates/wireframe-template.html](templates/wireframe-template.html) — ワイヤーフレームの雛形

## sandbox 環境での実行

このスキルの主要フロー（Step 1〜3、Step 5 の HTML 編集、Step 8）は sandbox 環境で実行でき
る。ネットワークを要するのは Step 4 の `pip install playwright` と `playwright install
chromium`（初回セットアップ時のみ。導入済み venv を再利用する2回目以降は不要）、および
Step 5〜6 の Playwright 起動処理（ローカル Chromium を操作するのみで外部通信は行わないが、
初回のブラウザダウンロードはネットワークを要する）。これらのコマンドのみ sandbox を無効に
して実行する。既定の出力先（`design/`）・既定の brief パスはいずれもワークスペース内だが、
`--output` / `--brief` に絶対パスや `../` を含む相対パスを指定した場合はワークスペース外へ
も書き込み得るため、その場合は出力先を自らの責任で選ぶこと。
