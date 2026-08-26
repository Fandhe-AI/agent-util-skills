---
name: create-design-doc
description: >
  UX/UI デザイナーの専門家として、アイデア・要件文書から UI/UX 設計資料一式
  (design-doc.md・画面遷移図・ストーリーボード・HTML ワイヤーフレーム・PC/モバイル
  スクリーンショット) を生成。「UI設計して」「ワイヤーフレーム作って」「画面遷移図作って」
  「デザイン資料まとめて」「PO承認会用の画面説明資料を作って」で使用。主役成果物は
  storyboard.png（縦長1枚、主要シナリオの流れに沿って各画面の表示要素を日本語説明）。
  Figma ではなく自己完結 HTML + Playwright でワイヤーフレームを作る。企画は create-pitch-deck
  が担当し、両スキルは concept-brief.md を介して整合する。推奨実行順序:
  create-design-doc → create-pitch-deck（pitch が画面素材を取り込むため）。
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

想定利用シーンは「ユーザーが PO（プロダクトオーナー）で、AI が承認会に資料を持ってきて説明
する」形。**レビューの主役は storyboard.png**（主要シナリオの流れに沿って各画面の表示要素を
日本語説明する縦長1枚）であり、`wireframes/*.html` と `screens/*.png` はその部品／実装用
素材という位置づけになる。

## Figma ではなく HTML ワイヤーフレームを採用する理由

Figma はファイル生成 API が無くエージェント生成に不向き。HTML はブラウザ確認・スクショ資料
化・実装への直接流用が可能なため採用する。

## 推奨実行タイミング

アイデア文書と検証計画（PoC 結果を含む）が固まり、プロトタイプ／本開発に入る**前**が最適。
ビヘイビア・詳細仕様の定義を行うフローを持つ場合は、**その直前に本スキルを実行し、承認済み
の資料を仕様定義の入力にする**。詳細・企画側の同旨は `create-pitch-deck/SKILL.md` の
「推奨実行タイミング」節を参照。

## ペルソナ

実行主体は UX/UI デザイナーの専門家として振る舞い、常に次を自問しながら作業する。

- 想定ユーザーは、いつ・どこで・何のためにこの画面を開くか
- 画面の目的は1つに絞れているか。情報の優先順位は妥当か
- 状態（空・読込中・エラー・正常）を考慮しているか
- 色・タイポ・余白は一貫しているか（デザイントークンから逸脱していないか）
- PC とモバイルで体験が破綻していないか
- **PO が初見で「なぜこの画面がこう動くのか」を理解できる説明になっているか**（storyboard の
  表示要素説明が本スキルの核心）

## 連携: create-pitch-deck との整合（concept-brief.md・推奨実行順序）

本スキルと `create-pitch-deck` は、共有合意文書 `concept-brief.md` を介して整合する
（スキーマは [references/concept-brief-schema.md](references/concept-brief-schema.md)）。
企画（`create-pitch-deck`）とデザイン（本スキル）が別々に作った成果物で「言っていることが
食い違う」事態を防ぐための仕組みであり、両スキルとも同じ手順（Step 1・Step 9 相当）を持つ。

**推奨実行順序: `create-design-doc` → `create-pitch-deck`。** `create-pitch-deck` は本スキル
の成果物（`storyboard.png` / `screens/*.png`）を「画面と操作の流れ」スライドへ取り込むため、
先に本スキルを実行しておくと pitch 側の生成がスムーズになる。concept-brief.md はどちらが
先に実行されても、先に実行された側が起案する。

- 逆順（`create-pitch-deck` を先に実行済み）の場合でも支障はない。本スキル実行後に
  `create-pitch-deck` を**画面素材が揃った状態で再実行**すれば、「画面と操作の流れ」
  スライドが差し込まれる（詳細は `create-pitch-deck/SKILL.md` の連携節）。
- 2スキルで確実に共有するには、両方の呼び出しで**同一の `--brief <path>`** を明示指定する
  （既定値はスキルごとの成果物ディレクトリ直下で異なるため、指定なしでは自動的には共有
  されない）。**`--brief` は入力文書ディレクトリ直下を指定することを推奨する**（例:
  `ideas/<name>/concept-brief.md`）。入力文書と同じ場所に置くことで、後から実行する側が
  `--output` の既定値に関わらず確実に発見できる。

## 使い方

引数でアイデア・要件文書のパス（ディレクトリまたはファイル列挙）を渡す。引数が無い・曖昧な
場合は Step 2 でユーザーに確認する。

- 出力先ディレクトリはユーザー指定がなければ `design/`
- `--brief <path>` で concept-brief.md の場所を指定できる（既定 `<output>/concept-brief.md`。
  上記のとおり入力文書ディレクトリ直下の指定を推奨）
- **大部な入力文書を読む際の優先順位**: ディレクトリを渡された場合、まず `README.md`（概要・
  ステータス）→ 判定ゲート「判定」節（確定した結論）→ Go/No-Go 基準・成功基準の照合表
  （`02-poc-plan.md` 等）の順に読み、その後に本文を読む。要点を早く掴んでから詳細を確認する
  ことで、大量の文書がある場合でも手戻りを減らせる
- 利用例: アイデア検討フロー（`01-brainstorm.md` 〜 `03-poc/`）を入力とする場合、
  `ideas/<name>/` をそのまま渡す

## 成果物

| ファイル | 内容 | 位置づけ |
|---------|------|----------|
| `design/storyboard.html` → `design/storyboard.png` | 主要シナリオの流れに沿って各画面を1枚ずつ説明する縦長1枚もの（[templates/storyboard-template.html](templates/storyboard-template.html)） | **レビューの主役** |
| `design/design-doc.md` | 前提と解釈 / 想定ユーザーと主要シナリオ / 画面一覧（表示要素欄必須） / 情報設計 / デザイントークン / フィードバック観点。詳細構成は [references/design-doc-structure.md](references/design-doc-structure.md) | レビュー補助・記録 |
| `design/flow.png` | 画面遷移図（全体俯瞰）。単一 inline SVG（[templates/flow-diagram-template.html](templates/flow-diagram-template.html)）を Playwright で PNG 化。storyboard と役割を重複させない（[references/design-doc-structure.md](references/design-doc-structure.md) 参照） | 全体構造の把握用 |
| `design/wireframes/*.html` | 主要画面の自己完結 HTML ワイヤーフレーム。規約は [references/wireframe-guidelines.md](references/wireframe-guidelines.md) | 実装用素材 |
| `design/screens/*.png` | 各ワイヤーフレームの PC/モバイルスクリーンショット（Playwright）。storyboard の部品にもなる | 実装用素材・storyboard 部品 |

## フロー

### Step 1: concept-brief.md を確認する

`--brief` で指定されたパス（既定 `<output>/concept-brief.md`。入力文書ディレクトリ直下を
推奨）を確認する。

- **存在する場合**: 必須入力として読む。Step 2 の文書読解の結果と矛盾を感じた場合、
  concept-brief.md を無断で上書きせず、矛盾点を明示してユーザーに確認する。
- **存在しない場合**: Step 2 の文書読解を踏まえ、
  [references/concept-brief-schema.md](references/concept-brief-schema.md) のテンプレート
  で起案する。各項目に出典を付ける。Step 3 の骨子確認と同時にユーザーへ提示し、承認を得て
  から確定版を書き出す。

### Step 2: 入力文書を読解する

対象文書を読み、目的・要件概要・PoC 結果・未解決疑問点を把握する（優先順位は
[使い方](#使い方)参照）。

### Step 3: 画面一覧＋主要シナリオ骨子を提示しユーザー承認を得る（必須ゲート）

画面一覧（各画面の目的1行）と主要シナリオ（フェーズ区切り・各フェーズのステップ概要）、
Step 1 の concept-brief.md 案（起案した場合）をまとめてユーザーに提示する。フェーズ区切り
は design-doc.md の「想定ユーザーと主要シナリオ」・storyboard.html の「フェーズ見出し」と
一致させる。**承認を得るまで Step 4 へ進まない**。無承認のまま生成しない。

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

グローバル環境を汚染しないよう、必ずスキル専用の venv へインストールする。venv の置き場所
（`_/design-doc/.venv`）はリポジトリ外の一時作業領域（例: `/tmp/<work>/venv`）でもよく、
パスは固定ではない。**既に別スキル・別セッションで作成済みの venv（playwright 導入済み）が
あれば、再作成せずそのまま再利用してよい**（`pip install` は既存環境を壊さない冪等な操作の
ため、存在確認せず実行しても害はない）。`playwright install chromium` はブラウザ本体
（100MB超）をダウンロードするため時間を要する。

以降のコマンド例では venv の Python を `"${DESIGN_DOC_VENV_PY}"`（例:
`_/design-doc/.venv/bin/python3`）として表記する。

### Step 5: 生成する

順序: 画面遷移図 → ワイヤーフレーム → スクリーンショット → **ストーリーボード** →
design-doc.md。

1. **画面遷移図**: [templates/flow-diagram-template.html](templates/flow-diagram-template.html)
   をコピーし、Step 3 で承認された主要フローに合わせてレーン・ノード・エッジ・凡例を単一
   `<svg>` 内で書き換える（div レーンと別 svg のハイブリッドにしない）。

   ```bash
   "${DESIGN_DOC_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" \
     --html "<output>/flow.html" --out "<output>/flow.png" --width 1440 --height 720 --full-page
   ```

   `${CLAUDE_SKILL_DIR}` が展開されない・未設定の環境では、本 SKILL.md ファイルが置かれて
   いるディレクトリの絶対パスに読み替える（例: `skills/create-design-doc`）。

2. **ワイヤーフレーム**: 画面ごとに
   [templates/wireframe-template.html](templates/wireframe-template.html) をコピーし、
   [references/wireframe-guidelines.md](references/wireframe-guidelines.md) に従って画面
   内容へ書き換える（デザイントークンは design-doc.md と同じ値にする。`position: sticky` /
   `fixed` は使わない）。

3. **スクリーンショット**: ワイヤーフレームごとに PC・モバイル双方を撮影する。

   ```bash
   "${DESIGN_DOC_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" \
     --html "<wireframe.html>" --out "<output>/screens/<screen>-desktop.png" --width 1440 --height 900 --full-page
   "${DESIGN_DOC_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" \
     --html "<wireframe.html>" --out "<output>/screens/<screen>-mobile.png" --width 375 --height 812 --full-page
   ```

4. **ストーリーボード（主役成果物）**:
   [templates/storyboard-template.html](templates/storyboard-template.html) をコピーし、
   Step 3 で承認されたフェーズ区切り・シナリオ順に、フェーズごとに以下を並べる。

   - ①各ステップの画面スクリーンショット（`<img src="screens/<screen>-desktop.png">` で
     参照する。iframe や wireframe markup のインライン結合は使わない。理由は
     [templates/storyboard-template.html](templates/storyboard-template.html) 冒頭コメント
     参照）
   - ②画面名と目的（見出し1行）
   - ③**表示要素の日本語説明**（この画面に何が表示され、ユーザーは何を見て・何を操作するか。
     design-doc.md の画面一覧「表示要素」欄と整合させる）
   - ④次画面への遷移条件（矢印＋ラベル）

   フェーズの切り替わりで `.phase-title` 見出しを挟む。

   ```bash
   "${DESIGN_DOC_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/capture_screenshot.py" \
     --html "<output>/storyboard.html" --out "<output>/storyboard.png" --width 1000 --full-page \
     --allow-local-refs
   ```

   storyboard.html は `screens/*.png` を相対参照するため `--allow-local-refs` を付ける
   （許可対象は `<img src>` / srcset / CSS 画像参照からの、文書ディレクトリ配下に実在する
   raster 画像（.png/.jpg/.jpeg/.gif/.webp）のみ。iframe/object 等の埋め込み参照・
   絶対パス・`file://`・`../` 脱出・外部 URL は引き続き遮断される）。
   ワイヤーフレーム・flow.html の撮影には付けない。

5. **design-doc.md**: [references/design-doc-structure.md](references/design-doc-structure.md)
   の節構成で作成する。画面一覧の「表示要素」欄は必須。フィードバック観点は3〜5件、ユーザー
   に実際に確認してほしい具体的な問いにする。

### Step 6: 検証する

ワイヤーフレーム・flow.html・storyboard.html **すべて**についてレイアウト崩れを機械検証する
（flow.html も wireframe と同様に必ず実行する）。

```bash
# ワイヤーフレーム・flow.html（strict: data URI とページ内 fragment のみ許容）
"${DESIGN_DOC_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/check_overflow.py" "<html>"
# storyboard.html（screens/*.png の相対参照のみ追加許容）
"${DESIGN_DOC_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/check_overflow.py" "<output>/storyboard.html" --allow-local-refs
```

FAIL の場合は HTML を修正し、再生成してから validator を再実行する。PASS するまで完了扱い
にしない。加えて全スクリーンショット（PC/モバイル双方）と storyboard.png を目視確認し、
[references/wireframe-guidelines.md](references/wireframe-guidelines.md) の「モバイル
スクリーンショットの目視確認観点」に沿って崩れが無いことを確認する。

### Step 7: create-pitch-deck の成果物を探索する

`create-pitch-deck` が先に実行され自己完結 HTML スライドが生成済みかどうかを **Glob で
探索する**（既定パス `_/pitch-deck/*.html` を直書きで決め打ちしない。ユーザーが `--output`
を変更している場合があるため。`create-pitch-deck` は PPTX を生成しない）。見つかった場合は
Step 8 の相互整合チェックで参照する。見つからない場合は「create-pitch-deck 未実行」として
扱う。

### Step 8: concept-brief.md との相互整合チェック

生成した design-doc.md・画面一覧・storyboard を concept-brief.md と照合する。

- ターゲットユーザー・主要シナリオ・スコープ・トーン＆マナーに矛盾がないか
- Step 7 で `create-pitch-deck` の成果物が見つかった場合は、勝ち筋と画面設計の整合／スコープ
  外機能の混入／用語・呼称の不一致／課題と解決 UI の対応漏れを横断確認する

矛盾を検出した場合は concept-brief.md・本成果物のいずれも自動修正せず、「concept-brief.md
を直すか／本成果物を直すか」をユーザーに提示する。

### Step 9: フィードバック反映ループ

ユーザーからのフィードバックを反映する場合は該当ワイヤーフレーム・storyboard・doc（必要なら
concept-brief.md も）を修正し、Step 5〜8 を再実行する。

## 検証

生成後、必ず validator を実行し、以下の5段階ゲートで完了を確認する
（`.claude/rules/verification.md` 準拠）。

1. **特定**: `check_overflow.py` の実行と終了コードをもって完了とみなす（ワイヤーフレーム・
   flow.html・storyboard.html すべてに対して実行）
2. **実行**: `"${DESIGN_DOC_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/check_overflow.py" "<html>"` を全対象に対して新規実行する
3. **読取**: 出力全体（PASS/FAIL・失敗一覧）と終了コードを確認する
4. **検証**: 失敗が0件であること、PC/モバイル双方のスクリーンショットと storyboard.png を
   目視確認し崩れが無いこと、Step 8 の相互整合チェックで未解決の矛盾が無いことを確認する
5. **宣言**: 全対象で validator が PASS し、目視確認・相互整合チェックが済んだ場合のみ完了
   を宣言する。「たぶん崩れていない」等の推測で完了主張しない

`check_overflow.py` は最低限以下を確認する。

- PC（1440px）・モバイル（375px）双方で横スクロールを誘発するオーバーフローが無い
- 単一ファイルで自己完結している（既定の strict モードで許容する参照は、許可 MIME の
  data URI とページ内 fragment のみ。外部 URL に加え、相対パス・絶対パス・`file://` も
  単一ファイル配布で欠落・解決不能になるため違反として検出する）。実行時も文書本体以外への
  全要求を遮断・記録し、1件でもあれば FAIL とする
- storyboard.html のみ `--allow-local-refs` で検証する（`<img src="screens/...">` /
  srcset / CSS 画像参照からの、文書ディレクトリ配下に実在する raster 画像
  （.png/.jpg/.jpeg/.gif/.webp）だけを追加許容。iframe/object 等の埋め込み参照・
  絶対パス・`file://`・`../` 脱出・欠落参照は不可）

## よくある失敗

| 問題 | 回避策 |
|------|--------|
| ダミーデータが「サンプル」「テスト」等の placeholder のまま | 実際にありそうな商品名・金額・日付等、具体的でリアルな内容にする |
| モバイルで横スクロールが発生する | `check_overflow.py` を必ず実行し、`@media (max-width: 480px)` 等で1カラム化する |
| モバイルの full-page screenshot で下部ナビ等が中途半端な高さに浮く | `position: sticky` / `fixed` を使わない。通常フロー内の静的配置にする（`templates/wireframe-template.html` 参照） |
| design-doc.md とワイヤーフレームでトークンの値が食い違う | `references/design-doc-structure.md` のデザイントークンと `wireframes/*.html` の `:root` を同じ値にする |
| storyboard がただの画面集合になり「流れ」が伝わらない | フェーズ見出し・遷移条件（矢印＋ラベル）を必ず入れ、シナリオの順序で並べる |
| フィードバック観点が形骸化する | 3〜5件を「ユーザーに実際に確認したい具体的な問い」にする |

## 注意事項

- `wireframes/*.html`・`flow.html`・`storyboard.html` は CDN・外部フォント・外部画像を
  一切使用せず、JavaScript 自体を全面禁止とする（`<script>` は本文の有無を問わず不可。
  inline event handler 属性・`javascript:` URL も不可。静的な見た目の表現のみで完結
  させる）。ローカルファイル参照も原則不可（画像は data URI で埋め込む）。
  例外は storyboard.html の `screens/*.png` 相対参照のみで、検証・撮影時に
  `--allow-local-refs` を明示して許可する（許可対象は `<img src>` / srcset / CSS 画像参照
  からの raster 画像に限定。iframe/object 等でのローカル HTML 埋め込みは不可）
- venv はビルドツールであり生成物ではない。`_/` 配下に置いた場合は commit しない（`_/` は
  `.gitignore` 済み）。リポジトリ外の一時領域に置いた場合はそもそも commit 対象にならない
- 出力先ディレクトリ（`design/` 等）が存在しない場合は `mkdir -p` で作成してから書き出す

## 最終報告

完了時は簡潔に以下を報告する。

- 生成した成果物一覧の絶対パス（storyboard.png / design-doc.md / flow.png / wireframes /
  screens）
- validation result（対象ごとの PASS/FAIL）
- concept-brief.md のパスと、新規起案／既存読込のいずれか
- Step 8 の相互整合チェック結果（矛盾の有無、`create-pitch-deck` 成果物の有無）
- 設計方針の要旨を一文

## 参照ファイル

必要な場合だけ読む。

- [references/design-doc-structure.md](references/design-doc-structure.md) — design-doc.md の節構成・デザイントークン記述フォーマット・flow/storyboard の役割分担
- [references/wireframe-guidelines.md](references/wireframe-guidelines.md) — HTML ワイヤーフレームの必須要件・position sticky 禁止・モバイル目視確認観点
- [references/concept-brief-schema.md](references/concept-brief-schema.md) — concept-brief.md のスキーマ（`create-pitch-deck` と共有）
- [templates/flow-diagram-template.html](templates/flow-diagram-template.html) — 画面遷移図（単一 inline SVG）の雛形
- [templates/wireframe-template.html](templates/wireframe-template.html) — ワイヤーフレームの雛形
- [templates/storyboard-template.html](templates/storyboard-template.html) — ストーリーボード（レビュー主役）の雛形

## sandbox 環境での実行

このスキルの主要フロー（Step 1〜3、Step 5 の HTML 編集、Step 9）は sandbox 環境で実行でき
る。ネットワークを要するのは Step 4 の `pip install playwright` と `playwright install
chromium`（初回セットアップ時のみ。導入済み venv を再利用する2回目以降は不要）、および
Step 5〜6 の Playwright 起動処理（ローカル Chromium を操作するのみで外部通信は行わないが、
初回のブラウザダウンロードはネットワークを要する）。これらのコマンドのみ sandbox を無効に
して実行する。既定の出力先（`design/`）・既定の brief パスはいずれもワークスペース内だが、
`--output` / `--brief` に絶対パスや `../` を含む相対パスを指定した場合はワークスペース外へ
も書き込み得るため、その場合は出力先を自らの責任で選ぶこと。
