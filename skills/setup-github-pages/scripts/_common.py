"""setup-github-pages 共通部品: TOML サブセットのパーサと nav.toml `[site]` の検証。

# 役割・境界

`check_site.py`（生成前の事前検証）と `scaffold.py`（雛形の配置）が同じ解釈で
`nav.toml` を読み、`[site]` のブランド値に同じ検証を通すための共有モジュール
（`rebrand_site.py` も `brand.toml` の読み込みに使う。廃止は #51）。
対象リポジトリの `tools/docs-site-gen/` へ 3 つの .py を一緒に配置する前提で、同じ
ディレクトリからの `import _common` で読み込まれる。標準ライブラリのみに依存する。

# なぜ tomllib を使わないか

生成器（fandhe-frontend docs-site の nav.rs）は TOML の厳密なサブセット
（`key = "文字列"` のみ。整数・bool・配列・inline table は不可）しか受理しない。
tomllib は上位互換のため「tomllib では通るが生成器が落とす」入力を事前検知できない。
事前検証の判定を生成器と揃えるため、nav.rs の文法を同じ制約で再実装している。
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# nav.rs の MAX_INPUT_BYTES と同値。
MAX_INPUT_BYTES = 1024 * 1024

# 生成物のブランド表示に使う文字列の上限。ヘッダー・フッターのレイアウトが崩れる長さを防ぐ。
MAX_TEXT_LEN = 120

# scaffold.py が書き込むプレースホルダー。他の記法と衝突しない固有接頭辞にして、
# ユーザーの Markdown 本文に偶然現れる `__INIT__` 等を誤検出しないようにしている。
PLACEHOLDER_RE = re.compile(r"__SGP_[A-Z0-9_]+__")

# 表示順を偽装する双方向制御文字（Trojan Source）。ブランド表示・タイトルに混入させない。
BIDI_RE = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

# C0・DEL・C1（NEL=\x85 を含む）と行・段落区切り（U+2028/2029）。行ベースの解釈が
# 実装（Python / Rust / TOML 系パーサ）で食い違う文字を、表示値へ入れさせない。
CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")

# GitHub の命名規則を 1 箇所で定義する（scaffold.py・check_repo（SKILL.md）・brand.toml 検証が共有）。
# owner: 英数字とハイフン、先頭・末尾ハイフン不可、連続ハイフン不可、39 文字以内。
# repo: 英数字・`-`・`_`・`.`、100 文字以内。`.` / `..` 単独と `.git` 終端は不可
# （`_example` `.github` `a..b` は有効な名前）。
OWNER_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def valid_owner(owner: str) -> bool:
    return len(owner) <= 39 and OWNER_RE.fullmatch(owner) is not None


def valid_repo_name(name: str) -> bool:
    return (
        REPO_NAME_RE.fullmatch(name) is not None
        and name not in (".", "..")
        and not name.endswith(".git")
    )


REPOSITORY_RE = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{1,8})*$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LETTER_RE = re.compile(r"^[A-Za-z0-9]$")
FF_REV_RE = re.compile(r"^[0-9a-f]{40}$")

# 出力に上流ブランド名が残らないことを検査する語（大文字小文字を区別しない）。
UPSTREAM_BRAND = "fandhe-frontend"

# 上流を指す表示だけを「残存」「入力拒否」の対象にする。単純な部分一致にすると、利用者のリポジトリ名が
# `fandhe-frontend-docs` のような場合に、自サイトの正当なリンク（base_path・GitHub URL）まで
# 上流の残存と誤検出する。語境界で区別する。
#
# _WORD_TEXT: 表示文字列（brand / tagline / title 等）用。`fandhe-frontend` が独立した語として現れたら拒否
#   （`fandhe-frontend-docs` のような別の語の一部は許可）。
# _WORD_ATTR: 生成 HTML の残存検査用。上の語境界に加え、直前が `/` `.` のもの（`/fandhe-frontend-docs/`
#   や `github.com/acme/fandhe-frontend` のような URL・パスの一部）は利用者のものとして除外する。
_WORD_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_-])fandhe-frontend(?![A-Za-z0-9_-])", re.I)
RESIDUAL_RE = re.compile(
    r"(?<![A-Za-z0-9_/.-])fandhe-frontend(?![A-Za-z0-9_-])"
    r"|github\.com/Fandhe-AI/fandhe-frontend(?![A-Za-z0-9_.-])"
    r"|crates\.io/crates/fandhe-frontend",
    re.I,
)


# 人間には見えず LLM には読める文字は、指示文を仕込む経路になる（Unicode タグ文字・ゼロ幅文字など）。
# 一般カテゴリ Cc（制御）・Cf（書式: ゼロ幅・bidi・タグ文字・U+00AD・U+2060〜2064 など）・Cs・Co・Cn・
# Zl・Zp に加え、カテゴリは Mn / Lo だが不可視な変異セレクタ・結合用の文字・フィラーを明示的に含める。
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_INVISIBLE_EXTRA = frozenset(
    [0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0, 0xFEFF]
    + list(range(0x180B, 0x180F)) + list(range(0xFE00, 0xFE10)) + list(range(0xE0100, 0xE01F0))
)


def _is_hidden(ch: str) -> bool:
    return unicodedata.category(ch) in _INVISIBLE_CATEGORIES or ord(ch) in _INVISIBLE_EXTRA


def sanitize(value: object, limit: int = 300) -> str:
    """対象リポジトリ由来の文字列を、端末・ログ・エージェントの文脈へ出す前に無害化する（1 行にする）。

    制御文字（ESC・改行・タブ・NEL 等）と、人間に見えない文字（双方向制御・ゼロ幅・Unicode タグ・変異セレクタ等）は
    `\\uXXXX`（BMP 外は `\\UXXXXXXXX`）へ置換し、長さを制限する。nav.toml の title・差分の行・パース失敗の
    断片・git の origin など、攻撃者が内容を決められる文字列は、出力先がターミナルでも、読み手が AI エージェント
    でも、指示文や偽の行として働かないようにする（出力は常に「データ」であり、指示として扱わない）。
    """
    out = []
    for ch in str(value):
        if _is_hidden(ch):
            out.append(f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
        else:
            out.append(ch)
    res = "".join(out)
    return res if len(res) <= limit else res[:limit] + "…"


def read_bounded_text(path: Path, cap: int) -> str:
    """通常ファイルを上限付きで UTF-8 として読む。上限超過・通常ファイルでない・UTF-8 でないは ValueError / OSError。

    呼び出し側が事前に `resolves_inside` で読み取り先を確かめる（symlink を辿るかどうかは呼び出し側の方針）。
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("通常ファイルではない")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(cap + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > cap:
        raise ValueError(f"{cap} バイトを超える")
    return data.decode("utf-8")


def has_upstream_word(text: str) -> bool:
    """表示文字列に上流名が独立した語として含まれるか（入力検証用）。"""
    return _WORD_TEXT_RE.search(text) is not None


def is_upstream_repo(owner: str, repo: str) -> bool:
    """上流リポジトリそのもの（Fandhe-AI/fandhe-frontend。大文字小文字・.git を正規化）か。"""
    r = repo.lower()
    if r.endswith(".git"):
        r = r[:-4]
    return owner.lower() == "fandhe-ai" and r == UPSTREAM_BRAND


def _resolve_real(path: Path) -> Path:
    """`path`（未作成でもよい）の symlink 解決後の実体。未作成の末端は、存在する最も近い祖先の realpath に連結する。"""
    probe = path
    rest: list[str] = []
    while not os.path.lexists(probe):
        rest.append(probe.name)
        probe = probe.parent
    return Path(os.path.realpath(probe)).joinpath(*reversed(rest))


def _in_git_dir(root_real: Path, real: Path) -> bool:
    """実体が root 直下の `.git` 配下（`.git` 自体を含む）か。

    名前は大文字小文字を区別せず比べる。macOS（APFS 既定）・Windows は大文字小文字を区別しないため、
    `.GIT` / `.Git` も同じディレクトリを指す（`ln -s .GIT site` で `.git/` へ書き込み・読み取りが通ってしまう）。
    区別するファイルシステムでは `.GIT` は別名の通常ディレクトリだが、判定を環境で変えず常に拒否する（安全側）。
    """
    try:
        parts = real.relative_to(root_real).parts
    except ValueError:
        return False
    return bool(parts) and parts[0].lower() == ".git"


def resolves_inside(root_real: Path, path: Path) -> bool:
    """`path`（未作成でもよい）を symlink 解決した実体が root_real の配下（`.git` 配下を除く）に収まるか。

    書き込み先・読み取り先を限定する不変条件の Python 側の唯一の実装（bash 側は build-local.sh の guard_path）。
    祖先の途中が root の外を指す symlink だと、mkdir・write・read が root の外へ到達するため、
    操作の前にここで検証する。`.git` 配下（設定・フック・オブジェクト）はスキルの読み書き対象ではないため
    拒否する（`.gitignore -> .git/config` のようなリンクや、親ディレクトリ経由の到達を防ぐ）。
    """
    real = _resolve_real(path)
    if _in_git_dir(root_real, real):
        return False
    return real == root_real or root_real in real.parents


def write_target_problem(root_real: Path, path: Path) -> str | None:
    """root 配下への書き込み先として不適なら理由、問題なければ None。

    Python 側で「書く」前に必ず通す唯一のゲート。bash 側 guard_path と同じ不変条件
    （実体が root 配下・末端が symlink でない）に加え、既存の宛先は通常ファイルに限る
    （ディレクトリ・デバイス等へ書かない）。末端 symlink は、リンク先が root の内か外かを問わず拒否する
    （リポジトリ内を指す `.gitignore -> .git/config` でも、追記が別の設定ファイルを壊すため）。
    """
    if path.is_symlink():
        return "シンボリックリンク"
    real = _resolve_real(path)
    if _in_git_dir(root_real, real):
        return "`.git` 配下は書き込み対象にできない"
    if not (real == root_real or root_real in real.parents):
        return "対象の外へ解決される（親ディレクトリが symlink の可能性）"
    if os.path.lexists(path) and not path.is_file():
        return "通常ファイルではない"
    # 未作成の宛先は、存在する最も近い祖先がディレクトリでなければ書けない（親が通常ファイルだと mkdir が
    # FileExistsError / NotADirectoryError になり、書き込みの途中で落ちて部分書き込みが残る）。書く前にここで拒否する。
    ancestor = path.parent
    while not os.path.lexists(ancestor) and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if os.path.lexists(ancestor) and not ancestor.is_dir():
        return "親パスの途中にディレクトリではないもの（通常ファイル等）がある"
    return None


# 書き込み途中の一時ファイル名の接尾辞。拡張子（.rs / .py / .yml / .toml 等）で終わらせず、
# 万一プロセスが強制終了されて残っても、cargo・GitHub Actions・python が拾う名前にならないようにする。
ATOMIC_TMP_SUFFIX = ".sgp-tmp"


def atomic_write_bytes(path: Path, data: bytes, *, executable: bool = False) -> None:
    """`path` をファイル単位で原子的に書く（未変更か完全な内容のどちらかにしかならない）。

    同じディレクトリの一時ファイルへ全バイトを書き、fsync してから `os.replace` で置き換える。途中で失敗
    （空き容量不足・I/O エラー・権限・シグナル）しても、既存のファイルは元の内容のまま残り、新規ファイルは
    作られない（`open(..., "wb")` で書くと、先に 0 バイトへ切り詰められ、途中で失敗すると生成予定でも
    旧版でもない中途半端な内容が残り、再実行で競合になる）。失敗時は一時ファイルを消して例外を再送出する
    （消せなかったときは、例外の `leftover_tmp` 属性にそのパスを載せる）。

    - 権限: 既存ファイルはその権限を引き継ぐ。新規は umask に従う。`executable` なら実行ビットを足す。
    - `os.replace` は末端が symlink でもリンク先へは書かずリンク自体を置き換えるため、検査後に差し替えられても
      リンク先へ書き込まない（検査は `write_target_problem` が先に行う）。
    - 親ディレクトリは呼び出し側が作っておく。
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    if executable:
        mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=ATOMIC_TMP_SUFFIX)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)   # 容量不足は flush 時に初めて表面化し得る。置き換えの前に確定させる
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            e.leftover_tmp = tmp   # type: ignore[attr-defined]
        raise


# ---------------------------------------------------------------- nav.toml [site]

# 上流（fandhe-frontend docs-site の nav.rs）が受理する `[site]` の任意キーと上限。
# scaffold.py（書く前）と check_site.py（ビルド前）が同じ規則で検証し、「scaffold が書いた値を
# check_site が拒否する」「上流が拒否する値を check_site が通す」食い違いを作らない。
SITE_BRAND_MAX = 64
SITE_TEXT_MAX = 200
SITE_BADGE_MAX = 32
SITE_LANG_MAX = 35

# 必須 6 キーと追記例。未指定だと上流の既定表示（上流のブランド名・リンク）が公開されるため、
# 値の空かどうかではなくキーの存在で判定する（version_badge の空文字は「非表示」の正式な指定）。
SITE_REQUIRED_KEYS: dict[str, str] = {
    "brand": "<ヘッダーのブランド名>",
    "repository_url": "https://github.com/<owner>/<repo>",
    "tagline": "<サイトの説明を 1 行>",
    "copyright": "© <年> <名義>",
    "version_badge": "",
    "brand_mark": "<英数字1文字>",
}
SITE_OPTIONAL_KEYS = ("lang", "brand_color")
SITE_KNOWN_KEYS = frozenset({"title", "base_path", *SITE_REQUIRED_KEYS, *SITE_OPTIONAL_KEYS})

_SITE_LANG_RE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*")
_SITE_MARK_RE = re.compile(r"[A-Za-z0-9]")
_SITE_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def pages_base_path(owner: str, repo: str) -> str:
    """GitHub Pages の公開パス。`<owner>.github.io`（User/Org サイト）はルート配信、それ以外は `/<repo>`。"""
    if repo.lower() == f"{owner.lower()}.github.io":
        return ""
    return f"/{repo}"


def parse_repository_url(url: str) -> tuple[str, str] | None:
    """`https://github.com/<owner>/<repo>` を (owner, repo) へ。形式・命名規則に合わなければ None。"""
    m = REPOSITORY_RE.fullmatch(url)
    if not m or not valid_owner(m.group("owner")) or not valid_repo_name(m.group("repo")):
        return None
    return m.group("owner"), m.group("repo")


def _text_problem(value: str, lo: int, hi: int) -> str | None:
    if CONTROL_RE.search(value):
        return "制御文字を含められない"
    if BIDI_RE.search(value):
        return "双方向制御文字を含められない"
    if PLACEHOLDER_RE.search(value):
        return "プレースホルダー（__SGP_*__）が残っている"
    if not (lo <= len(value) <= hi):
        return f"{lo}〜{hi} 文字にする"
    if lo >= 1 and not value.strip():
        return "空白のみにできない"
    return None


def site_value_problem(key: str, value: str) -> str | None:
    """`[site]` の 1 キーの規則違反の理由を返す（問題なければ None）。

    上流 nav.rs と同じ規則に、スキル独自の追加（repository_url を GitHub 形式に限る・上流自身を指さない・
    制御文字/BIDI/プレースホルダーの拒否）を重ねる。理由に値は載せない（制御・不可視文字を出力へ流さない）。
    `has_upstream_word` は呼ばない（ブランド値に上流名を含めてよい。上流名は帰属表記の中にしか現れない）。
    """
    if key == "brand":
        return _text_problem(value, 1, SITE_BRAND_MAX)
    if key in ("tagline", "copyright"):
        return _text_problem(value, 1, SITE_TEXT_MAX)
    if key == "version_badge":
        if value == "":
            return None
        why = _text_problem(value, 1, SITE_BADGE_MAX)
        return why.replace(f"1〜{SITE_BADGE_MAX} 文字にする", f"{SITE_BADGE_MAX} 文字以内にする（空文字は非表示）") if why else None
    if key == "lang":
        if len(value) > SITE_LANG_MAX or _SITE_LANG_RE.fullmatch(value) is None:
            return "BCP 47 風（例: ja / en / zh-Hant-TW。35 文字以内）にする"
        return None
    if key == "brand_mark":
        return None if _SITE_MARK_RE.fullmatch(value) else "ASCII 英数字 1 文字にする"
    if key == "brand_color":
        return None if _SITE_COLOR_RE.fullmatch(value) else "#RRGGBB 形式にする"
    if key == "repository_url":
        parsed = parse_repository_url(value)
        if parsed is None:
            return "https://github.com/<owner>/<repo> 形式のみ許可（末尾 / ・.git 終端は不可）"
        if is_upstream_repo(*parsed):
            return "上流リポジトリ（Fandhe-AI/fandhe-frontend）そのもの。自サイトのリポジトリ URL を指定する"
        return None
    return None


def check_site_values(values: dict[str, str]) -> list[str]:
    """`[site]` の値の辞書を検証し、問題の一覧を返す（値そのものは載せない）。"""
    problems: list[str] = []
    unknown = sorted(set(values) - SITE_KNOWN_KEYS)
    if unknown:
        problems.append("[site] に未知のキー: " + ", ".join(sanitize(k, 60) for k in unknown))
    missing = [k for k in SITE_REQUIRED_KEYS if k not in values]
    if missing:
        example = ", ".join(f'{k} = "{SITE_REQUIRED_KEYS[k]}"' for k in missing)
        problems.append(
            f"[site] の必須キーが不足している: {', '.join(missing)}。未指定だと上流の既定表示が公開されるため"
            f"nav.toml の [site] へ追記する。追記例: {example}"
        )
    for key in SITE_KNOWN_KEYS - {"title", "base_path"}:
        if key in values:
            why = site_value_problem(key, values[key])
            if why:
                problems.append(f"[site] の `{key}`: {why}")
    return problems


def title_problem(title: str) -> str | None:
    """nav の title に上流名が独立した語として含まれる場合の理由（#51 で撤去予定の暫定規則）。

    ブランド値の上流名拒否は #50 で撤去したが、title だけは `build-local.sh` の `verify_attribution` が
    `[site]` の値以外に出る上流名を残存として拒否するため、check_site.py と scaffold の `--title` の
    両方でこの関数を呼んで事前に止める（片方だけ外すと scaffold が書いた直後に check_site が落ちる）。
    #51 が残存検査を外した時点でこの関数と 2 つの呼び出しを同時に消す。
    """
    if has_upstream_word(title):
        return (f"title に上流名 `{UPSTREAM_BRAND}` を独立した語として含められない"
                "（この制限は #51 で撤去予定。`fandhe-frontend-docs` のような別の語の一部は可）")
    return None


class SubsetError(ValueError):
    """TOML サブセットの構文違反。メッセージには行番号を含める。"""


@dataclass
class Table:
    header: str
    line: int
    values: dict[str, str] = field(default_factory=dict)


def _parse_quoted(value_part: str, line: int) -> tuple[str, str]:
    if not value_part.startswith('"'):
        raise SubsetError(f"line {line}: 二重引用符の文字列のみ使用できる")
    out: list[str] = []
    i = 1
    while i < len(value_part):
        c = value_part[i]
        if c == '"':
            return "".join(out), value_part[i + 1 :]
        if c == "\\":
            i += 1
            if i >= len(value_part):
                raise SubsetError(f"line {line}: エスケープが途中で終わっている")
            e = value_part[i]
            mapping = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}
            if e not in mapping:
                raise SubsetError(f"line {line}: 未対応のエスケープ \\{e}")
            out.append(mapping[e])
        else:
            out.append(c)
        i += 1
    raise SubsetError(f"line {line}: 文字列が閉じていない")


def _check_trailing(rest: str, line: int) -> None:
    rest = rest.lstrip()
    if rest and not rest.startswith("#"):
        raise SubsetError(f"line {line}: 末尾に余分な内容 `{rest}`")


def parse_subset(text: str, allowed_headers: set[str]) -> list[Table]:
    """TOML サブセットを `Table` のリスト（出現順）へ変換する。

    `[a]` / `[[a.b]]` は同じく `Table(header="a" / "a.b")` として並べる。
    ヘッダー名は `allowed_headers` に含まれるものだけを受理する（未知は SubsetError）。
    """
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise SubsetError("入力が 1 MiB 上限を超えている")
    tables: list[Table] = []
    # str.splitlines() は \x0b \x0c \x1c-\x1e \x85 U+2028/2029 でも分割するが、生成器（Rust の
    # str::lines）は \n のみで分割し末尾の \r を落とす。解釈を揃えるため \n 分割 + 末尾 \r 除去にする。
    for no, raw in enumerate(text.split("\n"), start=1):
        raw = raw[:-1] if raw.endswith("\r") else raw
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[["):
            end = s.find("]]")
            if end < 0:
                raise SubsetError(f"line {no}: `]]` が無い")
            header = s[2:end].strip()
            _check_trailing(s[end + 2 :], no)
            if header not in allowed_headers:
                raise SubsetError(f"line {no}: 未知のテーブル [[{header}]]")
            tables.append(Table(header, no))
            continue
        if s.startswith("["):
            end = s.find("]")
            if end < 0:
                raise SubsetError(f"line {no}: `]` が無い")
            header = s[1:end].strip()
            _check_trailing(s[end + 1 :], no)
            if header not in allowed_headers:
                raise SubsetError(f"line {no}: 未知のテーブル [{header}]")
            tables.append(Table(header, no))
            continue
        eq = s.find("=")
        if eq < 0:
            raise SubsetError(f"line {no}: `key = \"value\"` の形式ではない")
        key = s[:eq].strip()
        if not key or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            raise SubsetError(f"line {no}: 不正なキー `{key}`")
        if not tables:
            raise SubsetError(f"line {no}: テーブルの外にキーがある")
        value, rest = _parse_quoted(s[eq + 1 :].lstrip(), no)
        _check_trailing(rest, no)
        if key in tables[-1].values:
            raise SubsetError(f"line {no}: キー `{key}` が重複している")
        tables[-1].values[key] = value
    return tables


NAV_HEADERS = {
    "site",
    "section",
    "section.page",
    "section.group",
    "section.group.page",
    "menu",
    "menu.item",
}


def parse_nav(text: str) -> list[Table]:
    return parse_subset(text, NAV_HEADERS)


# ---------------------------------------------------------------- brand.toml


@dataclass
class Brand:
    brand: str
    repository: str
    owner: str
    repo: str
    tagline: str
    copyright: str
    lang: str
    version_badge: str
    favicon_letter: str
    favicon_color: str

    @property
    def base_path(self) -> str:
        """GitHub Pages の公開パス。`<owner>.github.io` リポジトリ（User/Org サイト）はルート配信。"""
        return pages_base_path(self.owner, self.repo)


class BrandError(ValueError):
    pass




def _text(values: dict[str, str], key: str, *, required: bool) -> str:
    v = values.get(key, "")
    if required and not v.strip():
        raise BrandError(f"brand.toml: `{key}` は必須")
    if CONTROL_RE.search(v):
        raise BrandError(f"brand.toml: `{key}` に制御文字を含められない")
    if len(v) > MAX_TEXT_LEN:
        raise BrandError(f"brand.toml: `{key}` は {MAX_TEXT_LEN} 文字以内")
    if has_upstream_word(v):
        # 後処理後の残存検査（出力に上流ブランド名が無いこと）と区別できなくなるため入力段階で拒否する。
        raise BrandError(
            f"brand.toml: `{key}` に上流名 `{UPSTREAM_BRAND}` を独立した語として含められない"
            "（生成後の残存検査と区別できない。`fandhe-frontend-docs` のような別の語の一部は可）"
        )
    if BIDI_RE.search(v):
        raise BrandError(f"brand.toml: `{key}` に双方向制御文字を含められない")
    if PLACEHOLDER_RE.search(v):
        raise BrandError(f"brand.toml: `{key}` にプレースホルダー（__SGP_*__）が残っている")
    return v


# brand.toml の必須キーと追記例の既定値。スキルのテンプレートに新しい必須キーが増えたとき、
# 既存の brand.toml（利用者編集のため scaffold は上書きしない）に不足があれば、汎用の検証エラーではなく
# 「不足キーと追記例」を案内する。キーを増やすときはここへ追加する（tests が不足の案内を検査する）。
BRAND_REQUIRED_KEYS: dict[str, str] = {
    "brand": "<ヘッダーのブランド名>",
    "repository": "https://github.com/<owner>/<repo>",
    "copyright": "© <年> <名義>",
    "lang": "ja",
    "favicon_letter": "<英数字1文字>",
    "favicon_color": "#2b6cb0",
}


def load_brand(path: Path) -> Brand:
    try:
        text = read_bounded_text(path, 64 * 1024)
        tables = parse_subset(text, {"brand"})
    except (OSError, ValueError, SubsetError) as e:
        raise BrandError(f"brand.toml を読めない: {e}") from e
    if len(tables) != 1:
        raise BrandError("brand.toml: [brand] テーブルがちょうど 1 つ必要")
    v = tables[0].values
    allowed = {
        "brand", "repository", "tagline", "copyright", "lang",
        "version_badge", "favicon_letter", "favicon_color",
    }
    unknown = set(v) - allowed
    if unknown:
        raise BrandError(f"brand.toml: 未知のキー {sorted(unknown)}")

    missing = [k for k in BRAND_REQUIRED_KEYS if k not in v]
    if missing:
        example = ", ".join(f'{k} = "{BRAND_REQUIRED_KEYS[k]}"' for k in missing)
        raise BrandError(
            f"brand.toml: 必須キーが不足している: {', '.join(missing)}。[brand] テーブルへ追記する"
            f"（スキルのテンプレートに新しいキーが増えた場合は templates/brand.toml を参照）。追記例: {example}"
        )

    brand = _text(v, "brand", required=True)
    tagline = _text(v, "tagline", required=False)
    copyright_ = _text(v, "copyright", required=True)
    version_badge = _text(v, "version_badge", required=False)

    repository = v.get("repository", "")
    m = REPOSITORY_RE.fullmatch(repository)
    if not m or not valid_owner(m.group("owner")) or not valid_repo_name(m.group("repo")):
        raise BrandError(
            "brand.toml: `repository` は https://github.com/<owner>/<repo> 形式のみ許可"
            "（GitHub の命名規則。`.` `..` 単独・.git 終端は不可）"
        )
    repo = m.group("repo")
    if is_upstream_repo(m.group("owner"), repo):
        raise BrandError(
            "brand.toml: `repository` が上流リポジトリ（Fandhe-AI/fandhe-frontend）そのもの。"
            "自サイトのリポジトリ URL を指定する"
        )

    lang = v.get("lang", "")
    if not LANG_RE.fullmatch(lang):
        raise BrandError("brand.toml: `lang` は BCP 47 風（例: ja / en / en-US）")
    letter = v.get("favicon_letter", "")
    if not LETTER_RE.fullmatch(letter):
        raise BrandError("brand.toml: `favicon_letter` は英数字 1 文字")
    color = v.get("favicon_color", "")
    if not COLOR_RE.fullmatch(color):
        raise BrandError("brand.toml: `favicon_color` は #RRGGBB 形式")

    return Brand(
        brand=brand,
        repository=repository,
        owner=m.group("owner"),
        repo=repo,
        tagline=tagline,
        copyright=copyright_,
        lang=lang,
        version_badge=version_badge,
        favicon_letter=letter,
        favicon_color=color,
    )
