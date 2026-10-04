#!/usr/bin/env python3
"""対象リポジトリへ docs サイト一式（wrapper・後処理・workflow・初期サイト）を配置する。

# 役割・境界

SKILL.md の Step 2 から呼ばれ、スキル同梱の templates/ と scripts/ を対象リポジトリの
所定位置へコピーしつつ、`__SGP_*__` プレースホルダーをユーザー入力で置換する。
シェルの sed で置換すると、入力値の `/` `&` `\\` 等で置換式が壊れる・注入されるため、
値は本スクリプトが検証・エスケープして書き込む。

# 契約

- 既存ファイルの扱いは種別で分ける（FILES 参照）。スキル所有ファイルは内容一致ならスキップ・不一致なら競合で中止
  （--update で上書き）、利用者編集ファイルは常に保持する。再実行しても利用者の編集を壊さない。
- `.gitignore` へは未登録の行だけを追記する。
- 配置先は `--target` 配下に限る（テンプレート側の相対パスにのみ依存し、入力値でパスを組み立てない）。

終了コード: 0 成功 / 2 入力不正・書き込み先が不適 / 3 競合（スキル所有ファイルの内容不一致。--update で解消）/
4 配置後の check_site 失敗（ファイルは配置済み。指摘箇所を直す）。
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    BIDI_RE, COLOR_RE, CONTROL_RE, FF_REV_RE, PLACEHOLDER_RE, LANG_RE, LETTER_RE, MAX_TEXT_LEN, UPSTREAM_BRAND, Brand,
    has_upstream_word, is_upstream_repo, write_target_problem, valid_owner, valid_repo_name,
)

BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

# (テンプレート相対パス, 配置先相対パス, 実行権限, 種別)
# 種別 OWNED: スキルが所有する機械的ファイル。内容はスキルの版と引数から一意に決まり、利用者が編集する前提では
#   ない。既存の内容が生成予定と違えば「競合」とし、何も書かずに中止する（別用途の同名ファイルを黙って
#   「配置済み」と扱うと、必要な構成が無いまま後続 Step へ進んでしまうため）。スキル更新時は --update で上書きする。
# 種別 USER: 利用者が編集する前提のファイル。再実行時に必ず内容が食い違うため、既存なら「保持（利用者編集）」
#   とし、書き換えない。内容の妥当性は check_site.py が検証する。
OWNED, USER = "owned", "user"
FILES = [
    ("templates/docs-site-gen/Cargo.toml", "tools/docs-site-gen/Cargo.toml", False, OWNED),
    ("templates/docs-site-gen/src/main.rs", "tools/docs-site-gen/src/main.rs", False, OWNED),
    ("templates/docs-site-gen/FF_REV", "tools/docs-site-gen/FF_REV", False, OWNED),
    ("templates/brand.toml", "tools/docs-site-gen/brand.toml", False, USER),
    ("scripts/build-local.sh", "tools/docs-site-gen/build-local.sh", True, OWNED),
    ("scripts/rebrand_site.py", "tools/docs-site-gen/rebrand_site.py", False, OWNED),
    ("scripts/check_site.py", "tools/docs-site-gen/check_site.py", False, OWNED),
    ("scripts/_common.py", "tools/docs-site-gen/_common.py", False, OWNED),
    ("templates/pages.yml", ".github/workflows/pages.yml", False, OWNED),
    ("templates/nav.toml", "site/nav.toml", False, USER),
    ("templates/index.md", "site/index.md", False, USER),
    ("templates/rust-toolchain.toml", "rust-toolchain.toml", False, USER),
]

# 終了コード: 0 成功 / 2 入力不正・書き込み先が不適 / 3 競合（OWNED の不一致）/ 4 配置後の check_site 失敗
EXIT_CONFLICT, EXIT_CHECK_FAILED = 3, 4

GITIGNORE_LINES = ["_ff/", "tools/docs-site-gen/target/", "tools/docs-site-gen/Cargo.lock", "_site/"]


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def validate_text(name: str, value: str, *, required: bool) -> str:
    if required and not value.strip():
        raise ValueError(f"--{name} は必須")
    if CONTROL_RE.search(value):
        raise ValueError(f"--{name} に制御文字を含められない")
    if len(value) > MAX_TEXT_LEN:
        raise ValueError(f"--{name} は {MAX_TEXT_LEN} 文字以内")
    if BIDI_RE.search(value):
        raise ValueError(f"--{name} に双方向制御文字を含められない")
    if PLACEHOLDER_RE.search(value):
        # 置換結果が再置換されて意図しない値になる経路を入力段階で断つ
        raise ValueError(f"--{name} にプレースホルダー（__SGP_*__）を含められない")
    if has_upstream_word(value):
        raise ValueError(
            f"--{name} に上流名 `{UPSTREAM_BRAND}` を独立した語として含められない"
            "（生成後の残存検査と区別できない。`fandhe-frontend-docs` のような別の語の一部は可。"
            "--copyright の既定値は owner を含むため、必要なら --copyright を明示する）"
        )
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", required=True, type=Path, help="対象リポジトリのルート（既存ディレクトリ）")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True, help="既定ブランチ名（gh repo view で解決した値）")
    ap.add_argument("--title", required=True, help="サイトタイトル（nav.toml [site].title・トップ見出し）")
    ap.add_argument("--brand", default=None, help="ヘッダーのブランド名（既定: --title）")
    ap.add_argument("--tagline", default="")
    ap.add_argument("--copyright", dest="copyright_", default=None, help="既定: © <年> <owner>")
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--version-badge", default="")
    ap.add_argument("--favicon-letter", default=None, help="既定: ブランド名の先頭英数字")
    ap.add_argument("--favicon-color", default="#2b6cb0")
    ap.add_argument("--update", action="store_true",
                    help="スキルが所有するファイル（workflow・wrapper・スクリプト・FF_REV）の不一致を上書きする。"
                         "利用者編集ファイル（nav.toml・index.md・brand.toml・rust-toolchain.toml）は触らない")
    ap.add_argument("--year", default=None, help="著作権表記の年（既定: 現在の年）")
    args = ap.parse_args(argv)

    try:
        if not args.target.is_dir():
            raise ValueError("--target が既存ディレクトリではない")
        if not valid_owner(args.owner):
            raise ValueError("--owner が GitHub の owner 名として不正")
        if not valid_repo_name(args.repo):
            raise ValueError("--repo が GitHub の repo 名として不正")
        if is_upstream_repo(args.owner, args.repo):
            raise ValueError("--owner/--repo が上流リポジトリ（Fandhe-AI/fandhe-frontend）そのもの。自サイトのリポジトリを指定する")
        if not BRANCH_RE.fullmatch(args.branch) or ".." in args.branch:
            raise ValueError("--branch が不正（英数字・. _ / - のみ）")
        title = validate_text("title", args.title, required=True)
        brand = validate_text("brand", args.brand if args.brand is not None else title, required=True)
        tagline = validate_text("tagline", args.tagline, required=False)
        badge = validate_text("version-badge", args.version_badge, required=False)
        if not LANG_RE.fullmatch(args.lang):
            raise ValueError("--lang は BCP 47 風（例: ja / en）")
        if not COLOR_RE.fullmatch(args.favicon_color):
            raise ValueError("--favicon-color は #RRGGBB 形式")
        letter = args.favicon_letter
        if letter is None:
            m = re.search(r"[A-Za-z0-9]", brand)
            letter = m.group(0).upper() if m else ""
        if not LETTER_RE.fullmatch(letter):
            raise ValueError("--favicon-letter は英数字 1 文字")
        if args.copyright_ is None:
            import datetime
            year = args.year or str(datetime.date.today().year)
            if not re.fullmatch(r"[0-9]{4}", year):
                raise ValueError("--year は 4 桁の数字")
            copyright_ = f"© {year} {args.owner}"
        else:
            copyright_ = args.copyright_
        copyright_ = validate_text("copyright", copyright_, required=True)
        ff_rev = (SKILL_DIR / "templates/docs-site-gen/FF_REV").read_text().strip()
        if not FF_REV_RE.fullmatch(ff_rev):
            raise ValueError("同梱の FF_REV が 40 桁 hex ではない（スキル側の不整合）")
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2

    repository = f"https://github.com/{args.owner}/{args.repo}"
    probe = Brand(brand, repository, args.owner, args.repo, tagline, copyright_, args.lang,
                  badge, letter, args.favicon_color)
    subs = {
        "__SGP_SITE_TITLE__": toml_escape(title),
        "__SGP_BASE_PATH__": probe.base_path,
        "__SGP_BRAND__": toml_escape(brand),
        "__SGP_REPOSITORY__": repository,
        "__SGP_TAGLINE__": toml_escape(tagline),
        "__SGP_COPYRIGHT__": toml_escape(copyright_),
        "__SGP_LANG__": args.lang,
        "__SGP_VERSION_BADGE__": toml_escape(badge),
        "__SGP_FAVICON_LETTER__": letter,
        "__SGP_FAVICON_COLOR__": args.favicon_color,
        "__SGP_DEFAULT_BRANCH__": args.branch,
    }

    def render(src_rel: str) -> str:
        text = (SKILL_DIR / src_rel).read_text(encoding="utf-8")
        # 1 パスの re.sub で置換する。key ごとに str.replace を重ねると、先に埋めた値が
        # 後続の置換対象になり得る（値の中のプレースホルダーが再展開される）。
        table = {"__SGP_SITE_TITLE__": title} if src_rel == "templates/index.md" else subs
        return PLACEHOLDER_RE.sub(lambda m: table.get(m.group(0), m.group(0)), text)

    # 全件を分類してから書く（部分書き込みなし）。
    #   create    存在しない → 書く
    #   same      存在し、生成予定の内容と一致 → 何もしない（冪等な再実行）
    #   update    OWNED で不一致、かつ --update 指定 → 上書き
    #   conflict  OWNED で不一致（--update なし）、または通常ファイルでない → 中止
    #   keep      USER で既存 → 保持（利用者編集）
    root_real = Path(os.path.realpath(args.target))
    plan: list[tuple[str, str, bool, str]] = []   # 書くもの（create / update）
    same: list[str] = []
    keep: list[str] = []
    conflicts: list[str] = []
    problems: list[str] = []
    for src_rel, dst_rel, executable, kind in FILES:
        dst = args.target / dst_rel
        text = render(src_rel)
        if not (dst.exists() or dst.is_symlink()):
            why = write_target_problem(root_real, dst)
            if why:
                problems.append(f"{dst_rel}（{why}）")
            plan.append((dst_rel, text, executable, "create"))
            continue
        if kind == USER:
            keep.append(dst_rel)   # symlink でも書かない（リンク先へは決して書かない）
            continue
        if dst.is_symlink() or not dst.is_file():
            conflicts.append(f"{dst_rel}（{'シンボリックリンク' if dst.is_symlink() else '通常ファイルではない'}）")
            continue
        try:
            current = dst.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
        if current == text:
            same.append(dst_rel)
        elif args.update:
            why = write_target_problem(root_real, dst)
            if why:
                problems.append(f"{dst_rel}（{why}）")
            plan.append((dst_rel, text, executable, "update"))
        else:
            conflicts.append(f"{dst_rel}（内容がスキルの生成予定と異なる）")
    gi = args.target / ".gitignore"
    why = write_target_problem(root_real, gi)
    if why:
        problems.append(f".gitignore（{why}）")
    if problems:
        # 部分書き込みを避けるため、1 件でも不適なら何も書かずに中止する
        print("エラー: 書き込み先が不適（リンク先の内外を問わず symlink には書かない）: "
              + ", ".join(problems), file=sys.stderr)
        return 2
    if conflicts:
        print("エラー: スキルが所有するファイルが既存で、内容が生成予定と一致しない（競合）。何も書かずに中止する:",
              file=sys.stderr)
        for c in conflicts:
            print(f"  - {c}", file=sys.stderr)
        print("  対処: 別用途のファイルなら手動で統合する（スキルの templates/ と scripts/ の該当ファイルを参照）。"
              "スキル更新後の差分を取り込むだけなら、同じ引数に --update を付けて再実行する"
              "（上書きされるのは所有ファイルのみ。pages.yml の paths を手で足している場合は先に差分を控える）。",
              file=sys.stderr)
        return EXIT_CONFLICT

    created: list[str] = []
    updated: list[str] = []
    for dst_rel, text, executable, action in plan:
        dst = args.target / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        if executable:
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (created if action == "create" else updated).append(dst_rel)

    existing = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    to_add = [line for line in GITIGNORE_LINES if line not in existing]
    if to_add:
        prefix = "" if not existing or gi.read_text(encoding="utf-8").endswith("\n") else "\n"
        with gi.open("a", encoding="utf-8") as fh:
            fh.write(prefix + "\n# docs サイト（setup-github-pages）\n" + "\n".join(to_add) + "\n")

    print("作成: " + (", ".join(created) or "なし"))
    print("更新（--update）: " + (", ".join(updated) or "なし"))
    print("一致（変更なし）: " + (", ".join(same) or "なし"))
    print("保持（利用者編集）: " + (", ".join(keep) or "なし"))
    print("追記した .gitignore 行: " + (", ".join(to_add) or "なし"))
    if keep:
        print("注: 保持したファイルの内容（brand.toml・nav.toml 等）は生成予定と一致する保証がない。"
              "下の check_site で検証する（失敗したら該当ファイルを直す）。")

    # 配置後の検証（利用者編集ファイルを含む構成全体が build の前提を満たすか）
    import check_site
    brand_path = args.target / "tools" / "docs-site-gen" / "brand.toml"
    try:
        errors, warnings = check_site.check(Path(os.path.realpath(args.target)), brand_path)
    except ValueError as e:
        errors, warnings = [str(e)], []
    for w in warnings:
        print(f"警告 {w}")
    for e in errors:
        print(f"NG {e}", file=sys.stderr)
    if errors:
        print("エラー: 配置後の検証（check_site）に失敗した。上の項目を直してから再実行する", file=sys.stderr)
        return EXIT_CHECK_FAILED
    print("check_site ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
