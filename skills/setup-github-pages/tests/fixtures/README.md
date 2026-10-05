# fixtures

`raw/` は FF_REV（`../../templates/docs-site-gen/FF_REV`）の docs-site-gen が出力した
**rebrand 前**の実物（手書きではない）。構成: トップ・404・本文に fandhe-frontend の言及を
含むページ（usage）・redirect ページ（old-usage）・favicon・検索インデックス。
FF_REV を更新したら再生成し、`rebrand.test.mjs` が通ることを確認する。

最終再生成: FF_REV `b3e31ef663a98b6080feb98c84ade238d1074a08`。同 rev で scaffold → wrapper build →
`docs-site-gen` 直接実行（rebrand 前）した出力が `raw/` の保存ファイルと完全一致することを確認済み
（上流の DOM 変更は旧 rev から新 rev の間で fixture に現れる範囲では無かった）。
`assets/` は `favicon.svg` と `search-index.json` のみを保存する（上流が増やした
`search-index/guide.json` 等は rebrand の検証対象外のため保存しない）。

## site-keys/

`site-keys/` は FF_REV の docs-site に `nav.toml` の `[site]`（10 キー）を指定して生成した**実出力**
（`index.html`・`404.html`。手書き・加工なし）。`VerifyAttributionTest` が `verify_attribution` の入力に使う。
上流の DOM が変わって帰属表記の並びが崩れたら、実ビルドの verify とこのテストが止まる。

再生成: 空の一時ディレクトリへ `python3 scripts/scaffold.py --target <dir> --owner acme --repo mini-repo --branch main --title Mini --tagline "Tiny site" --year 2026`
を実行し、`<dir>` で（使い捨ての `CARGO_HOME` を付けて）`bash tools/docs-site-gen/build-local.sh --clean --write-third-party` を実行する。
後処理は無い。`_site/index.html` と `_site/404.html` をそのままコピーする。最終再生成: FF_REV `b3e31ef663a98b6080feb98c84ade238d1074a08`。
