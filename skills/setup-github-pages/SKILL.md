---
name: setup-github-pages
description: fandhe-frontend 公式サイトと同じデザインの GitHub Pages ドキュメントサイトを任意リポジトリへ構築。Rust 製 SSG・Markdown 管理・Actions 自動デプロイ・ブランド置換まで一括。「GitHub Pages で公開したい」「docs サイト作って」「fandhe-frontend と同じデザイン」で使用。Firebase で公開するなら setup-firebase-hosting、単発 HTML は create-html-report。
model: sonnet
user-invocable: true
---

# setup-github-pages

任意のリポジトリ（新規・既存）に、[fandhe-frontend の公式サイト](https://fandhe-ai.github.io/fandhe-frontend/)と同じデザイン・同じ仕組みの GitHub Pages ドキュメントサイトを構築する。Markdown を `site/` に置き、既定ブランチへ push すると GitHub Actions がビルドして公開する。

仕組みは次のとおり。

- **生成器**: fandhe-frontend の Rust 製 SSG（`crates/docs-site`）をそのまま使う。`tools/docs-site-gen/FF_REV` の commit を匿名 shallow fetch し、薄い wrapper crate（`tools/docs-site-gen/`）経由でビルドする
- **ブランド置換**: 生成器にはヘッダーのブランド名・GitHub リンク・フッター等がハードコードされている。同梱の Python 後処理（`rebrand_site.py`）が `brand.toml` の値へ置き換える。上流が対応したら不要になる（「上流改修の追跡」節）
- **デプロイ**: `Fandhe-AI/actions` の共通 reusable workflow（`pages-deploy.yml@latest`）を呼ぶ

## 使い方

```text
/setup-github-pages
```

自然文の例: 「このリポジトリを GitHub Pages で公開したい」「docs サイト作って」「fandhe-frontend と同じデザインのドキュメントサイトにして」。

以降、このスキルのディレクトリ（この SKILL.md があるディレクトリ）の絶対パスを `SKILL_DIR` と書く。

### 前提条件

| 項目 | 内容 |
|------|------|
| ツール | `git`、`cargo` / `rustup`（stable）、`gh`（認証済み）、`python3`（標準ライブラリのみ使用。3.12 で実測）。テスト実行には Node.js |
| 権限 | 対象リポジトリの管理者権限（Pages の有効化に必要。`viewerPermission` が `ADMIN`） |
| ネットワーク | **必須**。GitHub への匿名 fetch（fandhe-frontend の取得）・`gh api`・Actions 実行・Pages 配信のすべてでネットワークを使う |
| 対象リポジトリ | GitHub 上に存在すること。Pages のサイトは private リポジトリでも原則として**公開 URL で誰でも閲覧できる**（Enterprise Cloud のアクセス制御を除く） |

ネットワーク越しの GitHub 操作（`git fetch`・`gh repo view`・`gh api`・`cargo build`）を必須とするため、これらのコマンドはコマンド単位で sandbox 無効にして実行する（`docs/skill-network-requirements.md` 参照）。ネットワーク遮断を解除できない環境では実行できない。

### 最初にユーザーへ確認すること

実行前に次を確認する。決まっていない項目は既定値を提示して合意を取る。

| 項目 | 既定・注意 |
|------|-----------|
| 対象リポジトリ（`owner/repo`） | 作業ディレクトリの `origin` から推定して提示する |
| サイト title | フッターのブランド名と、トップページの見出しに使う |
| `base_path` | プロジェクトサイトは `/<リポジトリ名>`（自動導出し、変更しない）。リポジトリ名が `<owner>.github.io` の場合のみ空 |
| ブランド表示 | ヘッダーのブランド名・タグライン（空可）・著作権表記・言語（`ja` 等）・バージョン badge（空なら削除）・favicon の 1 文字と色（既定 `#2b6cb0`。白文字を載せるため暗めの色） |
| ナビ構成 | セクションとページの一覧。既存の Markdown（`README.md`・`docs/`）を公開する場合は、そのパス |
| 公開範囲の了解 | 上記のとおりサイトは公開される。公開してよい内容か |

ブランド表示・タグライン・著作権・title に、上流名 `fandhe-frontend` を**独立した語として**含められない（生成後の残存検査と区別できないため、入力検証で理由付きで拒否される）。`fandhe-frontend-docs` のような別の語の一部は可。リポジトリ名・owner に含まれていてもよい（base_path・自サイトの GitHub URL は上流の残存と数えない）。ただし `Fandhe-AI/fandhe-frontend` 自体を自サイトのリポジトリにはできない。owner に含まれる場合、`--copyright` の既定値（`© <年> <owner>`）が拒否されることがあるので、その際は `--copyright` を明示する。

## フロー

### Step 1: 対象リポジトリを解決する

既定ブランチは `main` と決め打ちせず、必ず API から解決する。

```bash
REPO="owner/repo"   # 「最初にユーザーへ確認すること」で確認した値
# GitHub の命名規則（owner: 英数字とハイフン・先頭末尾と連続ハイフン不可、repo: 英数字 - _ .・`.` `..` 単独と .git 終端は不可）。
# 定義の正は scripts/_common.py の valid_owner / valid_repo_name で、tests が本関数との一致を検証する。
check_repo() {
  local owner="${1%%/*}" name="${1#*/}"
  [[ "$1" == */* && "${name}" != */* && "${#owner}" -le 39 && "${#name}" -le 100 ]] \
    && [[ "${owner}" =~ ^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$ ]] \
    && [[ "${name}" =~ ^[A-Za-z0-9_.-]+$ && "${name}" != "." && "${name}" != ".." && "${name}" != *.git ]]
}
check_repo "${REPO}" || { echo "不正なリポジトリ指定: ${REPO}"; exit 1; }

gh repo view "${REPO}" --json nameWithOwner,defaultBranchRef,visibility,viewerPermission \
  --jq '{repo: .nameWithOwner, branch: .defaultBranchRef.name, visibility: .visibility, perm: .viewerPermission}'
```

`perm` が `ADMIN` でなければ Step 5（Pages 有効化）はユーザーの操作が必要になる旨を伝える。以降の作業は対象リポジトリのクローンのルートで行う。

### Step 2: テンプレートを配置する

`scaffold.py` が同梱テンプレートを所定位置へコピーし、プレースホルダー（`__SGP_*__`）を**検証済みの値**で置換する。シェルの `sed` で自前置換しない（入力値の `/` `&` `"` で置換式や TOML が壊れる・注入される）。

```bash
python3 "${SKILL_DIR}/scripts/scaffold.py" \
  --target . \
  --owner "<owner>" --repo "<repo>" --branch "<既定ブランチ>" \
  --title "<サイト title>" --brand "<ブランド名>" \
  --tagline "<タグライン>" --copyright "© 2026 <名義>" \
  --lang ja --favicon-letter "<英数字1文字>" --favicon-color "#2b6cb0"
```

配置されるもの（既存ファイルは**上書きせずスキップ**して一覧に出す。再実行しても利用者の編集を壊さない。配置先の親ディレクトリが symlink で `--target` の外へ解決される場合は、1 件も書かずに中止する）:

| 配置先 | 役割 |
|--------|------|
| `tools/docs-site-gen/{Cargo.toml,src/main.rs}` | 上流 docs-site を `EMPTY_REGISTRY` で呼ぶ wrapper |
| `tools/docs-site-gen/FF_REV` | 取得する fandhe-frontend の commit SHA（**唯一の定義元**） |
| `tools/docs-site-gen/brand.toml` | ブランド表示の入力 |
| `tools/docs-site-gen/{build-local.sh,rebrand_site.py,check_site.py,_common.py}` | ビルド入口・後処理・事前検証（4 ファイルは同じディレクトリに置く） |
| `.github/workflows/pages.yml` | build → deploy の workflow |
| `site/{nav.toml,index.md}` | 初期サイト |
| `rust-toolchain.toml`（無い場合のみ） | `channel = "stable"` |
| `.gitignore`（未登録行のみ追記） | `_ff/` `tools/docs-site-gen/target/` `tools/docs-site-gen/Cargo.lock` `_site/` |

`Cargo.lock` を無視する理由: wrapper と上流の依存はすべて path 依存で crates.io の crate が 0 件のため、lock は `FF_REV` から決定的に導かれる。

### Step 3: サイトの内容を整える

ユーザーと決めたナビ構成に合わせて `site/nav.toml` と Markdown を編集する。書式・制約は [`references/site-format.md`](references/site-format.md)（nav.toml のサブセット、予約パス・予約アセット名、Markdown の対応範囲、リンク検査）を参照する。

押さえるべき点:

- `nav.toml` で使えるのは `key = "文字列"` だけ（配列・整数・bool は不可）
- `path` を `/themes/` `/primitives/` `/blocks/` `/wireframes/` で始めない（上流のショーケースが混入する。`check_site.py` が拒否する）
- 画像は非対応。図は表・コードブロックで代替する
- 内部リンクは `[x](./other.md)`（nav に登録済みの `.md`）で書く。絶対パスで書く場合は `base_path` を含める
- 既存の `README.md` や `docs/` を公開するなら、`nav.toml` の `source` に相対パスで指定し、`pages.yml` の `paths` にもそのディレクトリを追加する

### Step 4: ローカルでビルドして確認する

```bash
bash tools/docs-site-gen/build-local.sh --write-third-party
```

`build-local.sh` は CI と同じ入口で、次を順に行う（失敗した工程は stderr の `==> ` 行で分かる）。

1. `FF_REV` を `^[0-9a-f]{40}$` で検証
2. fandhe-frontend を `_ff/` へ匿名 shallow fetch（submodule は取らない）。`_ff/` は生成器のキャッシュ専用で、未コミット変更・未追跡ファイルがある、または git 作業ツリーでない既存ディレクトリの場合は**破棄せず中止**する（退避または手動削除してから再実行）
3. `--write-third-party` 指定時: `_ff/LICENSE-MIT` から `THIRD-PARTY-LICENSES` を生成
4. `check_site.py`（予約パス・base_path 整合・予約アセット・プレースホルダー残存）
5. wrapper を `cargo build --release`、サイトを `_site/` へ生成（リンク検査は fail-closed）
6. `rebrand_site.py` による置換と、最小 verify・残存検査

2 回目以降は `--clean` で既定の出力先 `_site/` を空にしてから再生成する。出力先が非空だとエラーで止まる。

`THIRD-PARTY-LICENSES` はリポジトリへコミットする。生成サイトには上流 SSG の出力（HTML / CSS / JS）が含まれるため、MIT の著作権表示とライセンス文の同梱が必要になる。フッターの「Built with fandhe-frontend docs-site (MIT OR Apache-2.0)」の表記とライセンスリンクは後処理が保持する。これは帰属の実務手順であり、法的助言ではない。判断が必要な場合は法務に確認する。

### Step 5: GitHub Pages を有効化する

Pages の Source を「GitHub Actions」（`build_type=workflow`）にする。状態の判定も操作結果の確認も **HTTP status** で行う（`gh api` はエラー本文も stdout に出すため、終了コードだけでは 404 と 403 を区別できない）。管理者権限が無い場合、200 で `build_type` が `workflow` 以外の場合、判定不能の場合は**変更せず中止**する。

**5-a: 状態確認と新規有効化**（読み取りと、未有効時の POST のみ）

```bash
REPO="owner/repo"   # Step 1 と同じ値
check_repo() {
  local owner="${1%%/*}" name="${1#*/}"
  [[ "$1" == */* && "${name}" != */* && "${#owner}" -le 39 && "${#name}" -le 100 ]] \
    && [[ "${owner}" =~ ^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$ ]] \
    && [[ "${name}" =~ ^[A-Za-z0-9_.-]+$ && "${name}" != "." && "${name}" != ".." && "${name}" != *.git ]]
}
check_repo "${REPO}" || { echo "不正なリポジトリ指定: ${REPO}"; exit 1; }
status_of() { awk 'NR==1{print $2}'; }   # `gh api -i` の 1 行目（HTTP/x NNN）から status を取る

perm="$(gh repo view "${REPO}" --json viewerPermission --jq '.viewerPermission')"
[[ "${perm}" == "ADMIN" ]] || { echo "管理者権限が無い（perm=${perm:-?}）。Settings → Pages を手動設定するようユーザーへ案内して中止"; exit 1; }

code="$(gh api -i "repos/${REPO}/pages" 2>/dev/null | status_of)"
case "${code}" in
  404)
    post="$(gh api -i -X POST "repos/${REPO}/pages" -f build_type=workflow 2>/dev/null | status_of)"
    [[ "${post}" == "201" ]] || { echo "Pages 有効化に失敗（HTTP ${post:-?}）。中止"; exit 1; }
    echo "Pages を workflow 方式で有効化した（HTTP 201）" ;;
  200)
    bt="$(gh api "repos/${REPO}/pages" --jq '.build_type')"
    if [[ "${bt}" == "workflow" ]]; then
      echo "既に workflow 方式で有効（変更なし）"
    else
      echo "現在の build_type=${bt}。既存の公開設定を置き換えることになるため、ここで停止する（5-b へ）"
      gh api "repos/${REPO}/pages" --jq '{build_type, source, html_url}'
      exit 3
    fi ;;
  *) echo "判定不能 (HTTP ${code:-?})。認証・権限を確認する。中止"; exit 1 ;;
esac
```

**5-b: 既存設定の切り替え（5-a が現在の設定を表示して停止した場合のみ。ユーザーの明示的な了承後に実行）**

5-a が表示した `build_type` / `source` を伝え、ブランチ配信から GitHub Actions 配信へ切り替えてよいかをユーザーに確認する。了承が得られるまで実行しない。

```bash
REPO="owner/repo"   # 5-a と同じ値。別シェルで実行されても安全なよう、検証と権限確認をここでもやり直す
check_repo() {
  local owner="${1%%/*}" name="${1#*/}"
  [[ "$1" == */* && "${name}" != */* && "${#owner}" -le 39 && "${#name}" -le 100 ]] \
    && [[ "${owner}" =~ ^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$ ]] \
    && [[ "${name}" =~ ^[A-Za-z0-9_.-]+$ && "${name}" != "." && "${name}" != ".." && "${name}" != *.git ]]
}
check_repo "${REPO}" || { echo "不正なリポジトリ指定: ${REPO}"; exit 1; }
perm="$(gh repo view "${REPO}" --json viewerPermission --jq '.viewerPermission')"
[[ "${perm}" == "ADMIN" ]] || { echo "管理者権限が無い（perm=${perm:-?}）。中止"; exit 1; }
put="$(gh api -i -X PUT "repos/${REPO}/pages" -f build_type=workflow 2>/dev/null | awk 'NR==1{print $2}')"
[[ "${put}" == "204" ]] || { echo "切り替えに失敗（HTTP ${put:-?}）。中止"; exit 1; }
[[ "$(gh api "repos/${REPO}/pages" --jq '.build_type')" == "workflow" ]] || { echo "切り替え後も build_type が workflow でない。中止"; exit 1; }
echo "workflow 方式へ切り替えた"
```

403 など判定不能なときは有効化せず、Settings → Pages → Source を手動で「GitHub Actions」にするようユーザーへ案内する。

### Step 6: コミットして公開する

変更をコミットし、既定ブランチへ入れる（PR 経由なら `create-pr`、コミットは `create-commit`）。`pages.yml` は既定ブランチへの push（`paths` に該当する変更）か手動実行（`workflow_dispatch`）で動く。

```bash
git status --short                # 追加されるのは tools/docs-site-gen/ site/ .github/workflows/pages.yml THIRD-PARTY-LICENSES .gitignore 等
git check-ignore -q _ff && echo "_ff は無視済み"
```

`_ff/`・`_site/`・`tools/docs-site-gen/target/` がステージされていないことを確認してからコミットする。コミットメッセージの例: `feat(docs): GitHub Pages ドキュメントサイトを追加`。

## 検証

完了を宣言する前に、次を**実際に実行**して出力を確認する（`.claude/rules/verification.md` の 5 段階ゲート）。

### ローカル

| 確認 | コマンド・期待値 |
|------|-----------------|
| ビルド全体 | `bash tools/docs-site-gen/build-local.sh --clean` が終了コード 0。末尾に `rebrand ok` と `verify ok: … 残存 0・帰属表記あり` |
| 残存検査 | `python3 tools/docs-site-gen/rebrand_site.py --dist _site --brand tools/docs-site-gen/brand.toml --verify-only` が 0 |
| 目視の補助 | `grep -o fandhe-frontend _site/index.html \| wc -l` が帰属表記の 3 件（`Built with …` と LICENSE リンク 2 件）のみ（リポジトリ名に `fandhe-frontend` を含む場合は base_path・自サイトの URL も数えられるため、代わりに `rebrand_site.py --verify-only` の結果を正とする）。`grep -c` は HTML が 1 行のため行数しか数えず使えない |

ブラウザ確認（`base_path` 配下で配信されるため、同名ディレクトリ経由で配信する）:

```bash
PREVIEW="$(mktemp -d)"
ln -s "${PWD}/_site" "${PREVIEW}/<repo>"
python3 -m http.server --directory "${PREVIEW}" --bind 127.0.0.1 8000
# → http://127.0.0.1:8000/<repo>/ を開く
```

確認ポイント: ヘッダー左上のブランド名・マーク、右上の GitHub リンク先、バージョン badge（削除した場合は無いこと）、左サイドバーと前後ページ移動、検索（`/` キー）、テーマ切替、フッターのタグライン・著作権・「Built with fandhe-frontend docs-site」と LICENSE リンク、favicon、404 ページ（存在しない URL）。

### CI・公開

```bash
RUN_ID="$(gh run list --repo "${REPO}" --workflow pages.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch --repo "${REPO}" "${RUN_ID}" --exit-status         # build → deploy が success
URL="$(gh api "repos/${REPO}/pages" --jq '.html_url')"
curl -sS -o /dev/null -w '%{http_code}\n' "${URL}"              # 200
```

`curl` が 404 のときは反映待ち（数十秒）を疑い、再実行しても 404 なら Pages の Source 設定と `base_path` を確認する。`deploy` ジョブが pending のままなら「よくある失敗」の `runner-label` を確認する。

## 注意事項

- **入力検証**: ブランド表示・URL・ブランチ名はすべて `scaffold.py` / `_common.py` が検証・エスケープする（リポジトリ URL は `https://github.com/<owner>/<repo>` のみ、HTML 出力は `html.escape`）。シェルへ渡す変数は常に `"${VAR}"` でクォートする。外部入力を `sed` や `bash -c` の文字列に展開しない
- **CSP を壊さない**: 生成物の CSP は `script-src 'self'` 等で厳格。後処理はインライン script / style を一切追加しない。サイトへ手でインラインスクリプトを足さない
- **置換は構造的に行う**: GitHub URL を一括置換しない。ヘッダー・フッターの「リポジトリへのリンク」要素だけを置換し、LICENSE-MIT / LICENSE-APACHE リンクと「Built with fandhe-frontend docs-site」の帰属表記は保持する。本文（`<main>`）は書き換えない
- **供給網**: 取得する上流は `FF_REV` の 40 桁 commit SHA で固定する。サードパーティ action は commit SHA 固定（tag はコメントで併記）。例外として `Fandhe-AI/actions` の reusable workflow は組織の運用方針により `@latest` を使う（ユーザー決定済み。呼び出し先は public リポジトリのため他組織のリポジトリからも呼べる）。キャッシュは wrapper の `target/` のみで、秘密情報は入れない
- **`@latest` の可変参照**: `id-token: write` を持つ deploy ジョブへ可変参照 `Fandhe-AI/actions/.github/workflows/pages-deploy.yml@latest` を渡している。`latest` タグが書き換えられると任意のコードがその権限で動くため、Fandhe-AI/actions 側の `latest` タグ保護（更新権限の限定・ruleset）が前提になる。保護を確認できない環境では commit SHA 固定へ切り替える
- **localStorage キー**: テーマ設定は `fandhe-docs-theme` で保存される。同一 origin（`<owner>.github.io`）の他サイトと共有されるが、保存されるのはテーマのみで無害なため置換しない
- **UI 文言は日本語固定**: 検索ボタン等の UI ラベルは上流が日本語で埋め込んでいる。`lang` を `en` にしても UI ラベルは変わらない
- **トップページの制約**: registry を空にするため、トップはヒーロー / カードグリッドの無い通常の Docs レイアウトになる
- **redirects.toml**: 任意機能。`site/redirects.toml` に `[[redirect]]` の `from` / `to` を書く（書式は references）。1 件の生成と rebrand 通過を実測済み
- **セキュリティ問題の扱い**: 秘密情報の混入やインジェクションの経路を検出したら処理を中止してユーザーへ報告する（`.claude/rules/security.md`）
- **コミット**: `.claude/rules/conventional-commits.md` に従う。`--no-verify` は使わない
- **既存の Rust workspace**: 対象リポジトリのルート `Cargo.toml` が広い glob の `members` を持つ場合は `exclude = ["_ff", "tools/docs-site-gen"]` を追加する（wrapper は独立 workspace として動かすため）
- **キャッシュの効果は未実測**: `actions/cache` の対象 `target/` は、fresh checkout で `_ff/` の mtime が更新されると path 依存のクレートが再ビルドされ、効果が限定的な可能性がある（ビルドは約 15 秒）。CI の実行時間を見て、効果が無ければ cache ステップを削除してよい
- **テスト**: `node --test "skills/setup-github-pages/tests/*.test.mjs"`（rev 固定・workflow 方針・python スクリプトの回帰）。Node.js 24 ではディレクトリ引数が使えないため glob で指定する

## よくある失敗

| 問題 | 回避策 |
|------|--------|
| `cargo install --git` が submodule 取得で認証失敗する（`docs/spec` が private リポジトリを指す） | 使わない。`build-local.sh` は submodule を取らない shallow fetch + path 依存でビルドする |
| deploy ジョブが永久に pending | reusable workflow の `runner-label` 既定は `self-hosted`。`pages.yml` では必ず `runner-label: ubuntu-latest` を明示する（テンプレートは設定済み。消さない） |
| `path` が `/themes/…` 等で始まりショーケースが混入する | `check_site.py` が拒否する。別の `path` にする。上流の registry を空にしても予約パスは衝突する |
| `site/assets/` に `site.css` 等を置いてビルドエラー | 予約アセット名（`references/site-format.md`）を避ける。`check_site.py` が具体名を報告する |
| リンク切れで生成が失敗し `_site/` に何も出力されない | fail-closed 仕様。出力されたエラーの 1 件ずつを直す（存在しない `#anchor`・nav 未登録の `.md`・存在しない絶対パス） |
| ページ内リンクが公開後に 404 | 絶対パスリンクに `base_path`（`/<repo>`）が無い。`[x](/<repo>/usage/)` と書くか、`[x](./usage.md)` を使う |
| 画像が表示されない | 上流は画像非対応（`![a](x)` は `!` とリンクになる）。表・コードブロックで代替する |
| nav.toml の title に `fandhe-frontend` を入れて失敗する | 独立した語としての上流名は残存検査と区別できない。`check_site.py` が事前に拒否するので別の表記にする（`fandhe-frontend-docs` のような別の語の一部は可） |
| `rebrand_site.py` が「一致数が 0（期待 1）」で失敗する | 上流 DOM が変わったか、二重実行。dist を作り直して再実行する。`FF_REV` 更新直後なら「FF_REV の更新手順」で置換対象を再確認する |
| `build-local.sh` が「`_ff` に未コミットの変更または未追跡ファイルがある」で止まる | `_ff/` はキャッシュ専用。必要な変更は退避し、不要なら `_ff/` を手動で削除して再実行する（スクリプトは破棄しない） |
| `build-local.sh` が「出力先が既に存在し空ではない」で止まる | `--clean` を付ける（既定の `_site/` のみ削除対象） |
| build は成功するが deploy だけ失敗する | Pages の Source が「GitHub Actions」でない。Step 5 を実行する |
| `${{ }}` を `run:` に書き足してしまう | env 経由で渡す（式の直書きはインジェクション経路になる） |

## FF_REV の更新手順

現在の固定値: `cf5edb9b8f1bf2a63d51dcf50a8d2806e2d8f9f9`（2026-10-04 時点で匿名ビルド・生成・rebrand を実測済み）。

唯一の定義元は `tools/docs-site-gen/FF_REV`（スキル側は `templates/docs-site-gen/FF_REV`）。`pages.yml` と `build-local.sh` は値を直書きせずこのファイルを読み、使用前に `^[0-9a-f]{40}$` で検証する。cache キーも同ファイルのハッシュを含むため、変更すると古いビルド成果物は再利用されない。

更新するときは次の順で行う。

1. 上流の新しい commit を確認する（`gh api repos/Fandhe-AI/fandhe-frontend/commits/main --jq .sha`）。**固定値は 40 桁の commit SHA のみ**。ブランチ名・タグは使わない
2. `FF_REV` を書き換える。このスキル内では `templates/docs-site-gen/FF_REV` と本節の「現在の固定値」を同時に更新する（`tests/rev-pin.test.mjs` が不一致を検出する）
3. `bash tools/docs-site-gen/build-local.sh --clean` を実行する。「registry 依存が 0 件であることを検査」の工程（`cargo metadata` で `source` が null 以外のパッケージを数える）が失敗したら、上流が外部 crate を導入した合図なので、匿名・隔離ビルドの前提と供給網の固定方針を見直すまで更新しない。wrapper のコンパイルエラーは上流 API（`build_site_with` / `EMPTY_REGISTRY`）の変更を示す
4. `rebrand_site.py` が「一致数が 0」で失敗したら、上流 DOM の変更を意味する。生成された HTML を読み、ヘッダー・フッターの該当要素を特定して `rebrand_site.py` のルールを直す。`brand.toml` に無い新しいハードコード表示が増えていないかも、`grep -o fandhe-frontend` と `Fandhe-AI` で確認する
5. 帰属表記の件数を再確認する。`grep -o fandhe-frontend _site/index.html | wc -l` が 3（`Built with …` と LICENSE リンク 2 件）のままであること。増減していれば上流がフッターを変えている
6. `references/site-format.md` の制約（nav.toml の書式・予約パス・予約アセット・Markdown 対応範囲）が変わっていないか上流ソースで再確認し、`scripts/check_site.py` の `RESERVED_ASSET_NAMES`（上流 `build.rs` の `RESERVED_ASSET_NAMES`）を更新する
7. `tests/fixtures/raw/` を新 rev の生成物で作り直し、`node --test "tests/*.test.mjs"` を通す

## 上流改修の追跡

本スキルの wrapper と後処理は、上流 fandhe-frontend の次の制約を回避するための**暫定措置**である。上流が対応したら該当部分を削除する。

| 回避している制約 | 暫定措置 | 上流が対応したら |
|------------------|----------|------------------|
| stock の `docs-site` が page section registry（8 パス必須・固有リンク注入）を強制する | wrapper が `build_site_with(…, &EMPTY_REGISTRY)` を呼ぶ | registry を無効化するフラグ（または外部サイト向けモード）が入ったら wrapper を廃止し stock バイナリを使う。トップのヒーロー / カードグリッドも使える可能性がある |
| ブランド表示がハードコード（`[site]` で変えられるのは `title`（フッターのみ）と `base_path`） | `rebrand_site.py` + `brand.toml` | `[site]` にブランド名・リポジトリ URL・タグライン・著作権・lang・favicon 等のキーが追加されたら、`rebrand_site.py` と `brand.toml` を削除し `nav.toml` へ移す |
| `cargo install --git` が submodule（`docs/spec` → private リポジトリ）で失敗する | 手動 shallow fetch + path 依存 | submodule の分離、または docs-site が crates.io などで配布されたらそちらへ切り替える |

追跡 Issue: [https://github.com/Fandhe-AI/fandhe-frontend/issues/3713](https://github.com/Fandhe-AI/fandhe-frontend/issues/3713)（トラッキング）

| Issue | 対象 |
|-------|------|
| [#3716](https://github.com/Fandhe-AI/fandhe-frontend/issues/3716) | registry 無効化 |
| [#3717](https://github.com/Fandhe-AI/fandhe-frontend/issues/3717) | ショーケース注入 |
| [#3718](https://github.com/Fandhe-AI/fandhe-frontend/issues/3718) | submodule |
| [#3720](https://github.com/Fandhe-AI/fandhe-frontend/issues/3720) / [#3721](https://github.com/Fandhe-AI/fandhe-frontend/issues/3721) / [#3722](https://github.com/Fandhe-AI/fandhe-frontend/issues/3722) | ブランドキー |
| [#3727](https://github.com/Fandhe-AI/fandhe-frontend/issues/3727) | 試行用リポジトリでの CI デプロイ確認 |

対応状況の確認は、上記 Issue の状態と、上流の `crates/docs-site/src/{layout.rs,site_footer.rs,favicon.rs,nav.rs,page_sections.rs}` の変更を `FF_REV` 更新時に見比べて行う。

## 関連

- `setup-firebase-hosting` — Firebase Hosting で公開する場合
- `create-html-report` — 単発の自己完結 HTML レポートで足りる場合
- `create-commit` / `create-pr` — Step 6 のコミット・PR 作成
