# スキルのネットワーク・sandbox 実行要件

本リポジトリが上流ソースとして管理する `skills/` 配下の 4 スキルについて、ネットワーク
越しの操作を要するか・sandbox（ネットワーク制限下の実行環境）で完走できるかをまとめる。
`create-html-report` / `setup-firebase-hosting` の判定は移設元
[Fandhe-AI/agent-cli-skills](https://github.com/Fandhe-AI/agent-cli-skills) の
`docs/sandbox-tls.md` に記載された値を引き継ぐ。`create-pitch-deck` / `create-design-doc`
は本リポジトリで新規に作成したスキルのため、継承元を持たず本ファイルが判定の正典となる。

## 判定一覧

| スキル | 判定 | 根拠 |
|--------|------|------|
| `create-html-report` | 不要（既定フローはワークスペース内完結、任意の出力先指定でワークスペース外へ書き込み得る） | renderer（`scripts/render_report.py`）は純ローカルの Python でネットワーク呼び出しはない。既定出力先 `_/reports/` はワークスペース内だが、`--output <path>` に絶対パスや `../` を含む相対パスを指定した場合はワークスペース外へも書き込み得るため、単純な「不要」ではなくこの判定値を使う |
| `setup-firebase-hosting` | 一部要 | `scripts/bootstrap-firebase.sh`（GCP/Firebase 認証・API 呼び出し・`gh secret set` 等）はネットワーク必須。`firebase.json` の作成やローカル検証（Step 3〜4 相当）はネットワーク不要 |
| `create-pitch-deck` | 一部要 | 生成・検証（`scripts/build_deck.py` / `scripts/validate_deck.py`）は python-pptx によるローカル処理でネットワーク不要。初回の venv セットアップ（`pip install python-pptx`）のみネットワークを要する |
| `create-design-doc` | 一部要 | 生成・検証（`scripts/capture_screenshot.py` / `scripts/check_overflow.py`）は Playwright（Chromium）を起動するローカル処理でページロード時の外部通信は行わない。初回の venv セットアップ（`pip install playwright` と `playwright install chromium` のブラウザ本体ダウンロード、100MB超）のみネットワークを要する |

## 判定値の意味

| 判定値 | 意味 |
|--------|------|
| `不要` | 主要フローがネットワークを一切要さず、ワークスペース内で完結する |
| `不要（既定フローは…、任意の出力先指定でワークスペース外へ書き込み得る）` | ネットワークは不要だが、任意の出力先指定によりワークスペース外への書き込みが起こり得るため単純な「不要」と区別する |
| `一部要` | 主要フローはネットワーク不要。一部の任意ステップ・前提操作のみネットワークを要する |

## 各スキルの詳細

### create-html-report

- `scripts/render_report.py`・`scripts/validate_report.py` はいずれも Python 標準ライブラリのみで完結し、外部パッケージ導入や外部通信を行わない
- 既定の出力先は `_/reports/<report-name>.html`（ワークスペース内）
- `--output <path>` にワークスペース外を指すパス（絶対パス・`../` を含む相対パス）を指定した場合はワークスペース外へ書き込み得る。`--output` を省略するか、正規化後にワークスペース配下へ解決されるパスを指定する限り、実行結果はワークスペース内に収まる
- SKILL.md 側の詳細は [../skills/create-html-report/SKILL.md](../skills/create-html-report/SKILL.md) の「sandbox 環境での実行」節を参照

### setup-firebase-hosting

- `scripts/bootstrap-firebase.sh` は `gcloud auth login` のブラウザ認証・GCP/Firebase API 呼び出し・`gh secret set` などネットワーク越しの認証操作を必須とするため、sandbox（ネットワーク制限下）では実行できない。認証済みのローカル端末または CI 上で実行する
- `firebase.json` の作成やローカル検証（emulator 起動・`curl` での配信設定確認）はネットワーク不要なため sandbox でも実行可能
- SKILL.md 側の詳細は [../skills/setup-firebase-hosting/SKILL.md](../skills/setup-firebase-hosting/SKILL.md) の「sandbox 環境での実行について」節を参照

### create-pitch-deck

- `scripts/build_deck.py`（deck spec → PPTX）・`scripts/validate_deck.py`（PPTX 検証）は python-pptx によるローカル処理のみで、実行時に外部通信は行わない
- ネットワークを要するのは初回セットアップの `pip install python-pptx` のみ。venv 導入済みであれば2回目以降はネットワーク不要
- 既定の出力先は `_/pitch-deck/`（ワークスペース内）。`--output` / `--brief` にワークスペース外を指すパスを指定した場合はワークスペース外へも書き込み得る
- SKILL.md 側の詳細は [../skills/create-pitch-deck/SKILL.md](../skills/create-pitch-deck/SKILL.md) の「sandbox 環境での実行」節を参照

### create-design-doc

- `scripts/capture_screenshot.py`・`scripts/check_overflow.py` は Playwright（Chromium）でローカル HTML（`file://`）を開くのみで、ページロード時の外部通信は行わない
- ネットワークを要するのは初回セットアップの `pip install playwright` と `playwright install chromium`（ブラウザ本体ダウンロード）のみ。導入済みであれば2回目以降はネットワーク不要
- 既定の出力先は `design/`（ワークスペース内）。`--output` / `--brief` にワークスペース外を指すパスを指定した場合はワークスペース外へも書き込み得る
- SKILL.md 側の詳細は [../skills/create-design-doc/SKILL.md](../skills/create-design-doc/SKILL.md) の「sandbox 環境での実行」節を参照

## 関連

- 判定値の正典・分担方針は移設元の `docs/sandbox-tls.md`（本リポジトリには含まれない。判定
  ラベル自体の変更管理は移設元リポジトリ側で行う）
- スキル本文の該当節は `.claude/rules/skill-authoring.md` の「sandbox 節の定型文」に従って
  記述する
