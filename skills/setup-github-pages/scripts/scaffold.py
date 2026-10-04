#!/usr/bin/env python3
"""対象リポジトリへ docs サイト一式（wrapper・後処理・workflow・初期サイト）を配置する。

# 役割・境界

SKILL.md の Step 2 から呼ばれ、スキル同梱の templates/ と scripts/ を対象リポジトリの
所定位置へコピーしつつ、`__SGP_*__` プレースホルダーをユーザー入力で置換する。
シェルの sed で置換すると、入力値の `/` `&` `\\` 等で置換式が壊れる・注入されるため、
値は本スクリプトが検証・エスケープして書き込む。

# 契約

- 既存ファイルは**上書きしない**（スキップして一覧に出す）。再実行しても利用者の編集を壊さない。
- `.gitignore` へは未登録の行だけを追記する。
- 配置先は `--target` 配下に限る（テンプレート側の相対パスにのみ依存し、入力値でパスを組み立てない）。

終了コード: 0 成功 / 2 入力不正。
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
    valid_owner, valid_repo_name,
)

BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

# (テンプレート相対パス, 配置先相対パス, 実行権限)
FILES = [
    ("templates/docs-site-gen/Cargo.toml", "tools/docs-site-gen/Cargo.toml", False),
    ("templates/docs-site-gen/src/main.rs", "tools/docs-site-gen/src/main.rs", False),
    ("templates/docs-site-gen/FF_REV", "tools/docs-site-gen/FF_REV", False),
    ("templates/brand.toml", "tools/docs-site-gen/brand.toml", False),
    ("scripts/build-local.sh", "tools/docs-site-gen/build-local.sh", True),
    ("scripts/rebrand_site.py", "tools/docs-site-gen/rebrand_site.py", False),
    ("scripts/check_site.py", "tools/docs-site-gen/check_site.py", False),
    ("scripts/_common.py", "tools/docs-site-gen/_common.py", False),
    ("templates/pages.yml", ".github/workflows/pages.yml", False),
    ("templates/nav.toml", "site/nav.toml", False),
    ("templates/index.md", "site/index.md", False),
    ("templates/rust-toolchain.toml", "rust-toolchain.toml", False),
]

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
    if UPSTREAM_BRAND in value.lower():
        raise ValueError(f"--{name} に `{UPSTREAM_BRAND}` を含められない")
    return value


def resolves_inside(root_real: Path, path: Path) -> bool:
    """`path`（未作成でもよい）を symlink 解決した実体が root_real の配下に収まるか。

    未作成の末端は、存在する最も近い祖先を realpath してから残りの名前を連結して求める。
    祖先の途中（tools/ や .github/ 等）が --target の外を指す symlink だと、mkdir や write が
    target 外へ到達するため、書き込みの前にここで全件検証する。
    """
    probe = path
    rest: list[str] = []
    while not os.path.lexists(probe):
        rest.append(probe.name)
        probe = probe.parent
    real = Path(os.path.realpath(probe)).joinpath(*reversed(rest))
    return real == root_real or root_real in real.parents


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
    ap.add_argument("--year", default=None, help="著作権表記の年（既定: 現在の年）")
    args = ap.parse_args(argv)

    try:
        if not args.target.is_dir():
            raise ValueError("--target が既存ディレクトリではない")
        if not valid_owner(args.owner):
            raise ValueError("--owner が GitHub の owner 名として不正")
        if not valid_repo_name(args.repo):
            raise ValueError("--repo が GitHub の repo 名として不正")
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

    root_real = Path(os.path.realpath(args.target))
    plan: list[tuple[str, str, bool]] = []
    skipped: list[str] = []
    escapes: list[str] = []
    for src_rel, dst_rel, executable in FILES:
        dst = args.target / dst_rel
        if dst.exists() or dst.is_symlink():
            skipped.append(dst_rel)
            continue
        if not resolves_inside(root_real, dst):
            escapes.append(dst_rel)
        plan.append((src_rel, dst_rel, executable))
    gi = args.target / ".gitignore"
    if not resolves_inside(root_real, gi):
        escapes.append(".gitignore")
    if escapes:
        # 部分書き込みを避けるため、1 件でも外れたら何も書かずに中止する
        print("エラー: 配置先が --target の外へ解決される（親ディレクトリ等が symlink）: "
              + ", ".join(escapes), file=sys.stderr)
        return 2

    created: list[str] = []
    for src_rel, dst_rel, executable in plan:
        dst = args.target / dst_rel
        text = (SKILL_DIR / src_rel).read_text(encoding="utf-8")
        # 1 パスの re.sub で置換する。key ごとに str.replace を重ねると、先に埋めた値が
        # 後続の置換対象になり得る（値の中のプレースホルダーが再展開される）。
        table = {"__SGP_SITE_TITLE__": title} if src_rel == "templates/index.md" else subs
        text = PLACEHOLDER_RE.sub(lambda m: table.get(m.group(0), m.group(0)), text)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        if executable:
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        created.append(dst_rel)

    existing = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    to_add = [line for line in GITIGNORE_LINES if line not in existing]
    if to_add:
        prefix = "" if not existing or gi.read_text(encoding="utf-8").endswith("\n") else "\n"
        with gi.open("a", encoding="utf-8") as fh:
            fh.write(prefix + "\n# docs サイト（setup-github-pages）\n" + "\n".join(to_add) + "\n")

    print("作成: " + (", ".join(created) or "なし"))
    print("スキップ（既存のため未変更）: " + (", ".join(skipped) or "なし"))
    print("追記した .gitignore 行: " + (", ".join(to_add) or "なし"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
