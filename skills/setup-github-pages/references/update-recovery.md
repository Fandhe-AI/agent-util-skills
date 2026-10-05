<!-- source: skills/setup-github-pages/SKILL.md の更新フロー Step U2（このスキル自身の手順。挙動の正は scripts/scaffold.py の --show-diff と tests/test_rebrand.py） -->
<!-- 最終確認日: 2026-10-05 -->
<!-- 取得状況: ✅ 実際の git リポジトリで手順を実行して確認済み -->

# 更新の取り消し（Step U2 の復旧手順）

更新フロー（SKILL.md の Step U0〜U5）で、ローカルビルドが**決定的に**失敗したときに、更新を取り消す手順。

## いつ復旧するか

- 復旧するのは**決定的な失敗**だけ（`rebrand_site.py` の「一致数が 0」、`verify` の失敗など。上流のデザイン更新で HTML 構造が変わり、後処理の置換対象が合わなくなったことの検知）。対象リポジトリ側の後処理は書き換えず、スキル側の修正が必要であることを利用者に報告する
- ネットワーク断や cargo の一時障害は復旧せず、まず**再試行**する
- 実行前に `git status --short` を利用者へ示し、了承を取る

## 保証する範囲

**スキルが書いたままの（U1 の適用後に手が入っていない）ファイルだけを自動で戻す。** U1 の後に利用者が同じファイルへ加えた手動統合・修正は、自動では消さず、利用者に差分を示して個別に判断を仰ぐ。U0 以降に利用者が新しく作ったファイルや、スキルが触っていない変更は、そもそも対象にしない（`git clean` や `git restore -- .` は使わない）。

## 復旧の手順

### 判定する（書き込みなし）

U1 で保持した JSON（`updated`・`created`・`manifest_written`・`gitignore_added`。再実行があれば両方を合わせたもの）と、**現在の状態**を突き合わせる。現在の状態は、`--update` なしの `--show-diff` で調べる。

```bash
python3 "${SKILL_DIR}/scripts/scaffold.py" --target . --branch "${BASE}" --show-diff --json
```

`--show-diff` は**分類だけして何も書かずに終了する**（再実行で欠けたファイルを作る経路を通らない）。出力の JSON の `same`（スキルの版と一致する所有ファイル）と `conflicts`（適用後に手が入った所有ファイル）を使う。

| 対象 | 判定 | 扱い |
|------|------|------|
| `updated[].path`（所有ファイル） | `same` にある（スキルが書いたまま） | **自動で戻す**（`git restore --source=HEAD --staged --worktree -- <path>`） |
| | `conflicts` にある（U1 の後に手が入った） | 自動で戻さない。`--show-diff` の差分を利用者に示し、個別に判断（戻す・残す・手動で統合）を仰ぐ |
| | どちらにもない（削除された、`pages.yml` の利用者区間だけ編集された、など） | 自動で戻さず、利用者に確認する |
| `created[]`（所有ファイル） | `same` にある | **自動で削除する**（`rm -f -- <path>`） |
| | それ以外 | 自動で削除せず、利用者に確認する |
| `created[]`（利用者編集ファイル: `brand.toml`・`nav.toml`・`index.md`・`rust-toolchain.toml`。`missing` の再作成で作られたもの） | — | **自動で削除しない**。作成後に利用者が編集したかを判定できないため、利用者に確認する |
| マニフェスト（`manifest_written` が true） | — | そのまま戻す（スキルしか書かない） |
| `THIRD-PARTY-LICENSES`（`--write-third-party` を付けたビルド） | — | そのまま戻す（ビルドの生成物） |
| `.gitignore`（`gitignore_added` が空でない） | `git diff --no-color HEAD -- .gitignore \| cat -v` の差分が、スキルが追記した行（`gitignore_added` と見出しのコメント行）だけ | 戻す |
| | それ以外の差分がある（利用者が U0 以降に手で編集した） | 自動で戻さず、利用者に確認する |

U1 の JSON の形に注意する: `updated` は `{"path": …, "reason": …}` のオブジェクトの配列（`path` の値を使う）、`created` は文字列（パス）の配列。転記するのは `path` の値だけ。

### 実行する（自動で戻してよいと判定したものだけ）

```bash
revert_path() {   # 追跡されていれば HEAD へ復元、未追跡（今回新規作成）なら削除する
  if git ls-files --error-unmatch -- "$1" >/dev/null 2>&1; then
    git restore --source=HEAD --staged --worktree -- "$1"
  else
    rm -f -- "$1"
  fi
}
# 上の表で「自動で戻す」「自動で削除する」と判定したパスだけを列挙する（判定していないパスを入れない）
git restore --source=HEAD --staged --worktree -- <自動で戻す updated[].path の各値>
rm -f -- <自動で削除する created[] の各値>        # 該当がなければ実行しない
revert_path tools/docs-site-gen/.scaffold-manifest.json   # manifest_written が true のときだけ
revert_path THIRD-PARTY-LICENSES                          # --write-third-party を付けたビルドのときだけ（U0 の時点で追跡されていなければ今回新規作成）
revert_path .gitignore                                    # 上の表で「戻す」と判定したときだけ
```

### 元のブランチへ戻る

自動で戻さなかったファイル（利用者に確認して残したもの）がある場合は、それらの扱いが決まってから戻る。更新用ブランチにコミットが無いこと（`git log --oneline "${BASE}..${NAME}"` が空）を確かめてから削除する。コミットがあれば削除せず、利用者に確認する。

```bash
git switch "${START_BRANCH}" && git branch -D "${NAME}"     # U0 で控えた値
```

復旧後、利用者が U0 以降に手で行った変更（exit 3 の統合、exit 4 の `brand.toml` 追記、新規ファイル）と、自動で戻さず残したファイルは、作業ツリーに残り、元のブランチへ持ち越される。その旨を利用者に伝える。
