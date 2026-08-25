---
name: create-pitch-deck
description: >
  事業企画の専門家として、アイデア・要件文書から PO 承認会向け企画提案スライド
  (PPTX, 16:9, Google スライド取込可) を生成。「ピッチデック作って」「企画スライドに
  まとめて」「プレゼン資料作って」「承認会用の資料を作って」「勝ち筋を整理して」で使用。
  前半（前提と解釈・課題・解決アプローチ・対象範囲・勝ち筋）→後半（画面と操作の流れ・
  検証計画・承認いただきたい事項）の10〜14枚可変構成。画面と操作の流れは
  create-design-doc の storyboard/screenshot を取り込む。両スキルは concept-brief.md を
  介して整合し、推奨実行順序は create-design-doc → create-pitch-deck。
model: sonnet
user-invocable: true
argument-hint: "<入力文書のパス> [--output <path.pptx>] [--brief <path>] [--brand <primary-hex>]"
tools: [Bash, Read, Write, Glob, Grep]
---

# create-pitch-deck

$ARGUMENTS で渡された入力文書（アイデア・要件文書）を読み、**事業企画の専門家**として PO
（プロダクトオーナー）承認会向けの企画提案スライド（PPTX）を生成する。

プロト開発の前に「なぜ必要か・どういう勝ち筋か」を分かりやすく捉えられるスライドを作り、
ユーザーのフィードバック・修正を挟むことで、期待値ずれを開発前に検出することが本スキルの
存在理由である。そのため成果物は必ず**冒頭に「前提と解釈」**、**末尾に「承認いただきたい
事項・確認事項」**を含む（詳細は後述の「スライド構成」節を参照）。

想定利用シーンは「ユーザーが PO で、AI が承認会に資料を持ってきて説明する」形。**前半で
解決する課題と解決方法、後半で具体的にどのような画面でどういう流れの操作になるのかを説明し、
PO から承認をもらう**という説明の流れを意識して構成する。

## ペルソナ

実行主体は事業企画の専門家として振る舞い、常に次を自問しながら作業する。

- なぜ今か（市場・タイミング）
- 誰の・どんな痛みを解くか
- 差別化・勝ち筋は何か。それは入力文書に根拠のある事実か、根拠のない仮説か
- **PO に「この画面が・この場面で・こう使われる」と具体的に伝わる説明になっているか**
- 検証（PoC）で何が分かっていて、何がまだ未検証か
- 承認をもらう上で PO が判断に迷いそうな点は何か（承認事項・確認事項として明示する）

## 連携: create-design-doc との整合（concept-brief.md・推奨実行順序）

本スキルと `create-design-doc` は、共有合意文書 `concept-brief.md` を介して整合する
（スキーマは [references/concept-brief-schema.md](references/concept-brief-schema.md)）。
企画（本スキル）とデザイン（`create-design-doc`）が別々に作った成果物で「言っていることが
食い違う」事態を防ぐための仕組みであり、両スキルとも同じ手順（Step 1・Step 9 相当）を持つ。

**推奨実行順序: `create-design-doc` → `create-pitch-deck`。** 本スキルの「画面と操作の
流れ」（`screen_flow`）スライドは `create-design-doc` が生成する `storyboard.png` /
`screens/*.png` を取り込むため、先に `create-design-doc` を実行しておく。

- **逆順で実行した場合**（本スキルを先に実行済み）: `screen_flow` はいったん
  `image: null` ＋ `note: "create-design-doc 未実行のためテキスト概略のみ"` で生成する
  （Step 6 参照）。`create-design-doc` の実行後、本スキルを**画面素材が揃った状態で
  再実行**する。deck spec の該当 `screen_flow` エントリの `image` に実ファイルパスを
  設定し `note` を外せば、Step 7〜8 を再実行するだけで画面スライドが差し込まれる
  （骨子確認からやり直す必要は無い）。
- 2スキルで確実に共有するには、両方の呼び出しで**同一の `--brief <path>`** を明示指定する
  （既定値はスキルごとの成果物ディレクトリ直下で異なるため、指定なしでは自動的には共有
  されない）。**`--brief` は入力文書ディレクトリ直下を指定することを推奨する**（例:
  `ideas/<name>/concept-brief.md`）。

## 使い方

引数でアイデア・要件文書のパス（ディレクトリまたはファイル列挙）を渡す。無指定の場合は
ユーザーに確認する。

- 出力先はユーザー指定がなければ `_/pitch-deck/<deck-name>.pptx`
- `--brief <path>` で concept-brief.md の場所を指定できる（既定 `<output の親ディレクトリ>/concept-brief.md`。上記のとおり入力文書ディレクトリ直下の指定を推奨）
- `--brand <primary-hex>` でブランドカラーの主色（`#RRGGBB`）を指定できる。省略時はニュート
  ラルなブランドパレット（`references/deck-spec.md` の `DEFAULT_BRAND` 相当）を使う
- **大部な入力文書を読む際の優先順位**: ディレクトリを渡された場合、まず `README.md`（概要・
  ステータス）→ 判定ゲート「判定」節（確定した結論）→ Go/No-Go 基準・成功基準の照合表
  （`02-poc-plan.md` 等）の順に読み、その後に本文を読む。要点を早く掴んでから詳細を確認する
- 利用例: アイデア検討フロー（`01-brainstorm.md` 〜 `03-poc/`）を入力とする場合、
  `ideas/<name>/` をそのまま渡す（`01-brainstorm.md` の要件概要・`02-poc-plan.md` の
  Go/No-Go 基準・`03-poc/` の検証結果を主な参照元とする）

## スライド構成（前半固定＋後半可変・10〜14枚）

**前半（固定・この順）**: 表紙 / **前提と解釈** / 課題 / 解決アプローチ / 対象範囲（In/Out）
/ **勝ち筋**。

**後半**: **画面と操作の流れ**（`screen_flow`、2〜4枚。シナリオの場面ごとに「この場面で・
この画面が・こう使われる」を説明）→ 検証計画・現在地 → **承認いただきたい事項・確認事項**。

role・必須フィールド・順序契約の詳細スキーマは
[references/deck-spec.md](references/deck-spec.md) を参照する。

## フロー

### Step 1: concept-brief.md を確認する

`--brief` で指定されたパス（既定 `<output の親ディレクトリ>/concept-brief.md`）を確認する。

- **存在する場合**: 必須入力として読む。Step 2 の文書読解の結果と矛盾を感じた場合、
  concept-brief.md を無断で上書きせず、矛盾点を明示してユーザーに確認する。
- **存在しない場合**: Step 2 の文書読解を踏まえ、
  [references/concept-brief-schema.md](references/concept-brief-schema.md) のテンプレート
  で起案する。各項目に出典を付ける。Step 3 の骨子確認と同時にユーザーへ提示し、承認を得て
  から確定版を書き出す。

### Step 2: 入力文書を読解する

対象文書を読み、目的・要件概要・PoC 結果・未解決疑問点を把握する（優先順位は
[使い方](#使い方)参照）。

### Step 3: create-design-doc の成果物を探索する

`create-design-doc` が先に実行され `storyboard.png` / `screens/*.png` が生成済みかどうかを
**Glob で探索する**（既定パス `design/storyboard.png` を直書きで決め打ちしない。ユーザーが
`--output` を変更している場合があるため）。見つかった場合は Step 6 の `screen_flow` 作成で
使う。見つからない場合は Step 6 でテキスト概略＋注記のみの `screen_flow` を作る。

### Step 4: 骨子を提示しユーザー承認を得る（必須ゲート）

前半6枚のタイトル＋1行要旨、後半の `screen_flow` 各枚（シナリオ場面名＋使う画像の有無）、
検証計画・承認事項の骨子、Step 1 の concept-brief.md 案（起案した場合）をまとめてユーザーに
提示する。**承認を得るまで Step 5 へ進まない**。無承認のまま生成しない。

### Step 5: deck spec（JSON）を組み立てる

[references/deck-spec.md](references/deck-spec.md) のスキーマに従い spec を作成する。

- 数値・事実は入力文書からの引用のみとする。捏造しない
- `winning.items[].label` の `"事実"` は**入力文書に記録された実測・調査結果に限る**。留保
  （サンプルサイズが小さい等）は `text` に併記する。それ以外の主張（推測・期待・一般論）は
  必ず `"仮説"` にする
- `approval.items`（承認いただきたい事項・確認事項）は3〜5件。`kind: "承認"` は方針として
  確定させたい事項、`kind: "確認"` は PO の判断を仰ぎたい事項に分ける

### Step 6: screen_flow スライドを組み立てる

シナリオの場面ごとに2〜4枚、次の方針で作成する。

- Step 3 で `storyboard.png` / `screens/*.png` が見つかった場合: 場面に対応する画像
  （`screens/<screen>-desktop.png` 等）を `image` に指定し、`narrative` に「この場面で・
  この画面が・こう使われる」を説明する文を書く
- 見つからない場合: `image: null`、`note` に
  `"create-design-doc 未実行のためテキスト概略のみ"` 等を設定し、`narrative` にテキストの
  みで場面を概略する

### Step 7: venv を用意し python-pptx を導入する

まず `python3` の存在を確認する。

```bash
command -v python3 >/dev/null || echo "python3 が見つからない"
```

未導入の場合は処理を中止し、導入方法を案内する（macOS: `brew install python3`。その他の
環境: 各環境の公式セットアップ手順または環境管理者に確認する。権限昇格を要するコマンドは
案内しない）。

```bash
python3 -m venv "_/pitch-deck/.venv" 2>/dev/null || true
"_/pitch-deck/.venv/bin/pip" install -q python-pptx
```

グローバル環境を汚染しないよう、必ずスキル専用の venv へインストールする。venv の置き場所
（`_/pitch-deck/.venv`）はリポジトリ外の一時作業領域（例: `/tmp/<work>/venv`）でもよく、
パスは固定ではない。**既に作成済みの venv（python-pptx 導入済み）があれば再作成せず
再利用してよい**（`pip install` は既存環境を壊さない冪等な操作のため、存在確認せず実行
しても害はない）。

以降のコマンド例では venv の Python を `"${PITCH_DECK_VENV_PY}"`（例:
`_/pitch-deck/.venv/bin/python3`）として表記する。

### Step 8: PPTX を生成する

```bash
"${PITCH_DECK_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/build_deck.py" \
  --spec "<deck-spec.json>" \
  --output "<output.pptx>"
```

`${CLAUDE_SKILL_DIR}` が展開されない・未設定の環境では、本 SKILL.md ファイルが置かれて
いるディレクトリの絶対パスに読み替える（例: `skills/create-pitch-deck`）。

`SpecError`（終了コード1）で失敗した場合はエラーメッセージに従って spec を修正し再実行する。

### Step 9: 検証する

```bash
"${PITCH_DECK_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/validate_deck.py" "<output.pptx>"
```

FAIL の場合は spec または `.pptx` を修正し、再生成してから validator を再実行する。PASS
するまで完了扱いにしない。

### Step 10: concept-brief.md との相互整合チェック

生成した spec・成果物を concept-brief.md と照合する。

- 課題定義・ターゲットユーザー・スコープ・解決コンセプトに矛盾がないか
- `create-design-doc` の成果物（Step 3 で発見できた場合）と、勝ち筋と画面設計の整合／
  スコープ外機能の混入／用語・呼称の不一致／課題と解決内容の対応漏れを横断確認する

矛盾を検出した場合は concept-brief.md・本スライドのいずれも自動修正せず、「concept-brief.md
を直すか／本スライドを直すか」をユーザーに提示する。

### Step 11: フィードバック反映ループ

ユーザーからのフィードバックを反映する場合は spec（必要なら concept-brief.md も）を修正し、
Step 8〜10 を再実行する。`create-design-doc` を後から実行した場合は Step 6 に戻って
`screen_flow` の `image` を差し込む。

## 検証

生成後、必ず validator を実行し、以下の5段階ゲートで完了を確認する
（`.claude/rules/verification.md` 準拠）。

1. **特定**: `validate_deck.py` の実行と終了コードをもって完了とみなす
2. **実行**: `"${PITCH_DECK_VENV_PY}" "${CLAUDE_SKILL_DIR}/scripts/validate_deck.py" "<output.pptx>"` を新規実行する
3. **読取**: 出力全体（PASS/FAIL・失敗一覧）と終了コードを確認する
4. **検証**: 失敗が0件であること、かつ Step 10 の相互整合チェックで未解決の矛盾が無いことを確認する
5. **宣言**: validator が PASS し、相互整合チェックが済んだ場合のみ完了を宣言する。「たぶん通る」等の推測で完了主張しない

validator（`validate_deck.py`）は最低限以下を確認する。

- スライド枚数が10〜14枚の範囲内
- 全 shape（画像を含む）がスライド境界（動的取得した `slide_width` / `slide_height`）内に収まる（はみ出しチェック）
- 全ての非空テキストに `a:latin` と `a:ea` の両方の typeface が設定されている（日本語フォント fallback）
- 2枚目に「前提」が含まれる、最終スライドに「承認」と3〜5件の番号付き承認・確認事項が含まれる

## よくある失敗

| 問題 | 回避策 |
|------|--------|
| 差別化ポイントを断定的に書いてしまう | 入力文書に数値・実測の根拠が無い主張は必ず `label: "仮説"` にする。「事実」は記録された実測・調査結果に限る |
| Google スライド取込で日本語が意図しないフォントになる | `a:latin` だけでなく `a:ea` にも typeface を設定する（`build_deck.py` の `set_run_font` が対応） |
| スライド境界からのはみ出し | 座標をハードコードした EMU 定数と比較しない。`prs.slide_width` / `prs.slide_height` から動的取得して比較する（`validate_deck.py` 準拠） |
| 縦長の storyboard 画像をそのまま貼ると小さくなりすぎる／はみ出す | `add_picture_fit` は幅基準→高さ超過時に高さ基準で再配置するが、それでも読みにくい場合は `create-design-doc` 側でシナリオ場面ごとに分割したスクリーンショット（`screens/*.png`）を使う。storyboard.png 全体を1枚に無理に収めない |
| 承認事項が形骸化する | 3〜5件を「PO に実際に判断・確認してほしい具体的な事項」にする。曖昧な依頼文にしない |

## 注意事項

- 生成される pptx は 16:9・10〜14枚可変構成（`screen_flow` が2〜4枚）。role・必須フィール
  ド・順序は [references/deck-spec.md](references/deck-spec.md) の契約
- venv はビルドツールであり生成物ではない。`_/` 配下に置いた場合は commit しない（`_/` は
  `.gitignore` 済み）。リポジトリ外の一時領域に置いた場合はそもそも commit 対象にならない
- レポート化対象に機密情報・非公開の実数値が含まれる場合、出力先が公開領域でないことを
  事前にユーザーへ確認する
- 出力先ディレクトリ（`_/pitch-deck/` 等）が存在しない場合は `mkdir -p` で作成してから
  書き出す（`build_deck.py` は `--output` の親ディレクトリを自動作成する）

## 最終報告

完了時は簡潔に以下を報告する。

- 生成した pptx の絶対パス
- validation result（PASS/FAIL、スライド枚数）
- concept-brief.md のパスと、新規起案／既存読込のいずれか
- `create-design-doc` 成果物の取り込み状況（画像あり枚数／未実行のためテキストのみの枚数）
- Step 10 の相互整合チェック結果（矛盾の有無）
- デッキの要旨を一文

例:

```text
企画提案スライドを生成しました:
<absolute-path>/pitch-deck.pptx

Validation: PASS（11枚）
concept-brief.md: <absolute-path>/concept-brief.md（既存読込）
画面と操作の流れ: 3枚中2枚は create-design-doc の screens/*.png を取り込み済み、1枚は
create-design-doc 未実行のためテキスト概略のみ
相互整合チェック: create-design-doc の成果物と用語・スコープの矛盾なし
内容: 徒歩圏内おつかい代行という課題に対し、3画面で完結する解決アプローチを提案
```

## 参照ファイル

必要な場合だけ読む。

- [references/deck-spec.md](references/deck-spec.md) — deck spec（JSON）のスキーマ・スライド role・検証ルール
- [references/concept-brief-schema.md](references/concept-brief-schema.md) — concept-brief.md のスキーマ（`create-design-doc` と共有）
- [samples/pitch-deck-sample.json](samples/pitch-deck-sample.json) — deck spec の記入例（架空アイデア「おつかいパンダ便」。`screen_flow` の画像あり／なし両パターンを含む）

## sandbox 環境での実行

このスキルの主要フロー（Step 1〜6、Step 8〜11）は sandbox 環境で実行できる。python-pptx を
使った生成・検証はローカル処理でネットワークを使わない。ネットワークを要するのは Step 7 の
`pip install python-pptx`（初回の venv セットアップ時のみ。導入済み venv を再利用する2回目
以降は不要）で、このコマンドのみ sandbox を無効にして実行する。既定の出力先
（`_/pitch-deck/`）・既定の brief パスはいずれもワークスペース内だが、`--output` /
`--brief` に絶対パスや `../` を含む相対パスを指定した場合はワークスペース外へも書き込み
得るため、その場合は出力先を自らの責任で選ぶこと。
