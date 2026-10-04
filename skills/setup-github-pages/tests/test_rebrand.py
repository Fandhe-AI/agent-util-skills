"""rebrand_site.py / check_site.py / scaffold.py の回帰テスト（unittest・標準ライブラリのみ）。

fixtures/raw は生成器の実出力（rebrand 前）。手書き fixture では上流の構造ずれを検知できないため、
実物を使う。`rebrand.test.mjs` から `node --test` 経由でも実行される。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPTS = SKILL / "scripts"
FIXTURE = HERE / "fixtures" / "raw"

BRAND_TOML = """[brand]
brand = "{brand}"
repository = "{repository}"
tagline = "{tagline}"
copyright = "{copyright}"
lang = "{lang}"
version_badge = "{badge}"
favicon_letter = "{letter}"
favicon_color = "{color}"
"""


def brand_toml(**kw):
    d = dict(brand="Acme Docs", repository="https://github.com/acme/mini-repo",
             tagline="Tiny site", copyright="© 2026 Acme", lang="en", badge="",
             letter="A", color="#2f855a")
    d.update(kw)
    return BRAND_TOML.format(**d)


def run(script, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                          capture_output=True, text=True)


def read_all(dist: Path):
    return {p.relative_to(dist).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(dist.rglob("*")) if p.is_file()}


class RebrandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dist = self.tmp / "dist"
        shutil.copytree(FIXTURE, self.dist)
        self.brand = self.tmp / "brand.toml"
        self.brand.write_text(brand_toml(), encoding="utf-8")

    def rebrand(self, *extra):
        return run("rebrand_site.py", "--dist", self.dist, "--brand", self.brand, *extra)

    def test_fixture_has_upstream_brand_before_rebrand(self):
        # 前提の確認: fixture が本当に rebrand 前の実物であること
        self.assertIn("Fandhe-AI/fandhe-frontend", (self.dist / "index.html").read_text())

    def test_success_replaces_brand_and_keeps_license_attribution(self):
        before = (self.dist / "index.html").read_text()
        license_anchors = re.findall(r'<a [^>]*LICENSE-(?:MIT|APACHE)"[^>]*>(?:MIT|Apache-2\.0)</a>', before)
        self.assertEqual(len(license_anchors), 2)
        r = self.rebrand()
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in ("index.html", "404.html"):
            t = (self.dist / rel).read_text()
            self.assertIn('<html lang="en">', t)
            self.assertIn("</span>Acme Docs</a>", t)
            self.assertIn('href="https://github.com/acme/mini-repo"', t)
            self.assertIn("Tiny site", t)
            self.assertIn("© 2026 Acme", t)
            self.assertNotIn("crates.io", t)
            self.assertNotIn("core v", t)  # version badge は空指定で削除
            self.assertIn("Built with fandhe-frontend docs-site (", t)
            for a in license_anchors:  # ライセンスリンクはバイト列まで不変
                self.assertIn(a, t)
        fav = (self.dist / "assets/favicon.svg").read_text()
        self.assertIn('fill="#2f855a"', fav)
        self.assertIn(">A</text>", fav)
        self.assertNotIn("fandhe-frontend", fav)

    def test_no_inline_script_added_and_csp_untouched(self):
        before = {k: v.count("<script") for k, v in read_all(self.dist).items() if k.endswith(".html")}
        csp_before = re.search(r'Content-Security-Policy" content="[^"]*"', (self.dist / "index.html").read_text()).group(0)
        self.assertEqual(self.rebrand().returncode, 0)
        after = read_all(self.dist)
        for k, n in before.items():
            self.assertEqual(after[k].count("<script"), n, k)
            self.assertNotRegex(after[k], r"<script(?![^>]*\bsrc=)")
        self.assertEqual(re.search(r'Content-Security-Policy" content="[^"]*"', after["index.html"]).group(0), csp_before)

    def test_article_content_is_left_untouched_and_not_flagged(self):
        usage_before = (self.dist / "usage/index.html").read_text()
        body = re.search(r'<article class="docs-content">.*?</article>', usage_before, re.S).group(0)
        self.assertIn("fandhe-frontend", body)  # 本文中の言及（ユーザーの Markdown）
        self.assertEqual(self.rebrand().returncode, 0)
        usage_after = (self.dist / "usage/index.html").read_text()
        self.assertIn(body, usage_after)  # 本文は一切書き換えない

    def test_redirect_page_is_tolerated(self):
        before = (self.dist / "old-usage/index.html").read_text()
        self.assertEqual(self.rebrand().returncode, 0)
        self.assertEqual((self.dist / "old-usage/index.html").read_text(), before)

    def test_version_badge_custom_value(self):
        self.brand.write_text(brand_toml(badge="v1.2.3"), encoding="utf-8")
        self.assertEqual(self.rebrand().returncode, 0)
        self.assertIn(">v1.2.3</span>", (self.dist / "index.html").read_text())

    def test_html_escaping_of_user_values(self):
        self.brand.write_text(brand_toml(brand="<b>&\\\"x", tagline="<script>alert(1)</script>"), encoding="utf-8")
        r = self.rebrand()
        self.assertEqual(r.returncode, 0, r.stderr)
        t = (self.dist / "index.html").read_text()
        self.assertNotIn("<script>alert(1)</script>", t)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", t)
        self.assertIn("&lt;b&gt;&amp;", t)
        self.assertNotIn("<b>&", t)

    def test_second_run_fails_closed_without_writing(self):
        self.assertEqual(self.rebrand().returncode, 0)
        snap = read_all(self.dist)
        r = self.rebrand()
        self.assertEqual(r.returncode, 1)
        self.assertIn("一致数が 0（期待 1）", r.stderr)
        self.assertEqual(read_all(self.dist), snap)

    def test_missing_target_fails_and_dist_is_unchanged(self):
        idx = self.dist / "404.html"
        idx.write_text(idx.read_text().replace('<span class="docs-github-link">', '<span class="x-link">'))
        snap = read_all(self.dist)
        r = self.rebrand()
        self.assertEqual(r.returncode, 1)
        self.assertIn("GitHub リンク", r.stderr)
        self.assertEqual(read_all(self.dist), snap)  # 他ファイルも含め何も書かない

    def test_unknown_page_without_chrome_fails(self):
        (self.dist / "weird.html").write_text("<html><body>no chrome</body></html>")
        self.assertEqual(self.rebrand().returncode, 1)

    def test_missing_license_line_fails(self):
        p = self.dist / "index.html"
        p.write_text(p.read_text().replace("Licensed under ", "Licensed by "))
        self.assertEqual(self.rebrand().returncode, 1)

    def test_invalid_inputs_rejected_with_exit_2(self):
        bad = [
            dict(repository="https://github.com/acme/repo/extra"),
            dict(repository="http://github.com/acme/repo"),
            dict(repository="https://github.com/acme/repo.git"),
            dict(repository="https://github.com/acme/repo\" onclick=\"x"),
            dict(repository="https://github.com/Fandhe-AI/fandhe-frontend"),
            dict(brand="my fandhe-frontend"),
            dict(color="red"),
            dict(letter="ab"),
            dict(lang="ja\"><script>"),
        ]
        for kw in bad:
            self.brand.write_text(brand_toml(**kw), encoding="utf-8")
            r = self.rebrand()
            self.assertEqual(r.returncode, 2, f"{kw}: {r.stderr}")

    def test_verify_only_detects_residual_in_chrome(self):
        r = self.rebrand("--verify-only")  # 未置換の dist
        self.assertEqual(r.returncode, 1)
        self.assertIn("残っている", r.stderr)
        self.assertEqual(self.rebrand().returncode, 0)
        self.assertEqual(self.rebrand("--verify-only").returncode, 0)

    def test_verify_only_fails_when_attribution_removed(self):
        self.assertEqual(self.rebrand().returncode, 0)
        p = self.dist / "index.html"
        p.write_text(p.read_text().replace("Built with fandhe-frontend docs-site", "Built with something"))
        self.assertEqual(self.rebrand("--verify-only").returncode, 1)

    def test_empty_dist_fails(self):
        shutil.rmtree(self.dist)
        self.dist.mkdir()
        self.assertEqual(self.rebrand().returncode, 1)


class SubsetParserTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import _common
        self.c = _common

    def test_splits_on_lf_only_and_accepts_crlf(self):
        t = self.c.parse_subset('[site]\r\ntitle = "a"\r\nbase_path = "/b"\r\n', {"site"})
        self.assertEqual(t[0].values, {"title": "a", "base_path": "/b"})

    def test_unicode_line_separators_do_not_split_lines(self):
        # Python の splitlines なら U+2028 / \x85 で分割され、Rust の lines() とは別解釈になる
        for sep in ("\u2028", "\x85", "\x0b", "\x1c"):
            with self.assertRaises(self.c.SubsetError, msg=repr(sep)):
                self.c.parse_subset(f'[site]\ntitle = "a"{sep}base_path = "/b"\n', {"site"})

    def test_line_numbers_follow_lf(self):
        with self.assertRaises(self.c.SubsetError) as cm:
            self.c.parse_subset('[site]\n\nbad line\n', {"site"})
        self.assertIn("line 3", str(cm.exception))

class RepoNameTest(unittest.TestCase):
    """owner / repo の命名規則が _common.py に 1 箇所で定義され、全利用箇所と一致することを確認する。"""

    CASES = [
        ("acme", "_example", True), ("acme", ".github", True), ("acme", "a..b", True),
        ("acme", "mini-repo", True), ("a", "r", True), ("a-b-c", "r", True), ("acme", "x" * 100, True),
        ("acme", ".", False), ("acme", "..", False), ("acme", "r.git", False), ("acme", "", False),
        ("acme", "a/b", False), ("acme", "a b", False), ("acme", "x" * 101, False),
        ("-x", "r", False), ("x-", "r", False), ("a--b", "r", False), ("", "r", False),
        ("a" * 40, "r", False), ("a_b", "r", False), ("a.b", "r", False),
    ]

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import _common
        self.c = _common

    def test_common_validators(self):
        for owner, repo, ok in self.CASES:
            self.assertEqual(self.c.valid_owner(owner) and self.c.valid_repo_name(repo), ok, (owner, repo))

    def test_scaffold_agrees_with_common(self):
        for owner, repo, ok in self.CASES:
            if not owner or not repo or "/" in repo or " " in repo:
                continue  # 空・区切り文字は argparse / パス組み立ての前提外（common 側で拒否を確認済み）
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            r = run("scaffold.py", "--target", tmp, "--owner", owner, "--repo", repo, "--branch", "main", "--title", "T")
            self.assertEqual(r.returncode == 0, ok, (owner, repo, r.stderr))

    def test_brand_toml_repository_agrees(self):
        for owner, repo, ok in self.CASES:
            if not owner or not repo or "/" in repo or " " in repo or '"' in repo:
                continue
            d = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
            (d / "b.toml").write_text(brand_toml(repository=f"https://github.com/{owner}/{repo}"), encoding="utf-8")
            try:
                self.c.load_brand(d / "b.toml")
                got = True
            except self.c.BrandError:
                got = False
            self.assertEqual(got, ok, (owner, repo))

    def test_skill_md_check_repo_matches_common(self):
        md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        funcs = re.findall(r"(check_repo\(\) \{.*?\n\})", md, re.S)
        self.assertGreaterEqual(len(funcs), 3, "SKILL.md に check_repo が 3 箇所（Step 1・5-a・5-b）無い")
        self.assertEqual(len(set(funcs)), 1, "SKILL.md の check_repo 定義が箇所ごとに食い違っている")
        for owner, repo, ok in self.CASES:
            if not owner or not repo:
                continue
            r = subprocess.run(["bash", "-c", funcs[0] + '\ncheck_repo "$1"', "_", f"{owner}/{repo}"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode == 0, ok, (owner, repo, r.stderr))
        for bad in ("acme", "acme/a/b", "/r", "acme/"):
            r = subprocess.run(["bash", "-c", funcs[0] + '\ncheck_repo "$1"', "_", bad], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, bad)


class ScaffoldSymlinkTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.target = self.base / "repo"
        self.target.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()

    def scaffold(self):
        return run("scaffold.py", "--target", self.target, "--owner", "acme", "--repo", "r", "--branch", "main", "--title", "T")

    def assert_nothing_written(self):
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertEqual([p.name for p in self.target.iterdir()], [p.name for p in self.target.iterdir() if p.is_symlink()])

    def test_symlinked_parent_dir_outside_target_aborts_without_writing(self):
        for name in ("tools", "site", ".github"):
            for p in self.target.iterdir():
                p.unlink()
            (self.target / name).symlink_to(self.outside)
            r = self.scaffold()
            self.assertEqual(r.returncode, 2, name)
            self.assertIn("--target の外", r.stderr)
            self.assert_nothing_written()

    def test_nested_symlink_in_missing_chain_detected(self):
        (self.target / ".github").mkdir()
        (self.target / ".github/workflows").symlink_to(self.outside)
        self.assertEqual(self.scaffold().returncode, 2)
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertFalse((self.target / "tools").exists(), "全件検証前に一部を書いてはいけない")

    def test_gitignore_symlink_outside_aborts(self):
        (self.outside / "victim").write_text("keep\n")
        (self.target / ".gitignore").symlink_to(self.outside / "victim")
        self.assertEqual(self.scaffold().returncode, 2)
        self.assertEqual((self.outside / "victim").read_text(), "keep\n")
        self.assertFalse((self.target / "tools").exists())

    def test_symlink_that_stays_inside_target_is_allowed(self):
        (self.target / "real-tools").mkdir()
        (self.target / "tools").symlink_to(self.target / "real-tools")
        self.assertEqual(self.scaffold().returncode, 0)
        self.assertTrue((self.target / "real-tools/docs-site-gen/FF_REV").is_file())

    def test_target_itself_a_symlink_to_dir_is_fine(self):
        link = self.base / "link"
        link.symlink_to(self.target)
        r = run("scaffold.py", "--target", link, "--owner", "acme", "--repo", "r", "--branch", "main", "--title", "T")
        self.assertEqual(r.returncode, 0, r.stderr)


class FetchFfTest(unittest.TestCase):
    """build-local.sh の fetch_ff（目印 `>>> fetch_ff` ～ `<<< fetch_ff` の区間）を一時 git リポで単体実行する。

    `_ff` は生成器のキャッシュ専用だが、利用者が手で編集していた場合に変更を破棄しないこと
    （P0 回帰）と、clean な場合だけ checkout で進むことを確認する。ネットワークは使わず、
    FF_URL にローカルのリポジトリを渡す。
    """

    def git(self, cwd, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.src = self.base / "src"
        self.src.mkdir()
        self.git(self.src, "init", "-q", "-b", "main")
        (self.src / "f.txt").write_text("one\n")
        self.git(self.src, "add", ".")
        self.git(self.src, "commit", "-q", "-m", "a")
        self.rev_a = self.git(self.src, "rev-parse", "HEAD")
        (self.src / "f.txt").write_text("two\n")
        self.git(self.src, "commit", "-q", "-am", "b")
        self.rev_b = self.git(self.src, "rev-parse", "HEAD")
        sh = (SCRIPTS / "build-local.sh").read_text(encoding="utf-8")
        body = re.search(r"# >>> fetch_ff.*?\n(.*?)# <<< fetch_ff", sh, re.S).group(1)
        self.func = body
        self.ff = self.base / "_ff"

    def fetch(self, rev):
        script = f'set -euo pipefail\nFF_DIR="$1"; FF_URL="$2"; FF_REV="$3"\n{self.func}\nfetch_ff'
        return subprocess.run(["bash", "-c", script, "_", str(self.ff), f"file://{self.src}", rev],
                              capture_output=True, text=True,
                              env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"))

    def test_fresh_fetch_and_clean_advance(self):
        r = self.fetch(self.rev_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.ff / "f.txt").read_text(), "one\n")
        r = self.fetch(self.rev_a)  # 一致 → 再利用
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("再利用", r.stderr)
        r = self.fetch(self.rev_b)  # clean なら進められる
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.ff / "f.txt").read_text(), "two\n")

    def test_uncommitted_change_aborts_and_is_preserved(self):
        self.assertEqual(self.fetch(self.rev_a).returncode, 0)
        (self.ff / "f.txt").write_text("my local edit\n")
        r = self.fetch(self.rev_b)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("破棄しない", r.stderr)
        self.assertEqual((self.ff / "f.txt").read_text(), "my local edit\n")
        self.assertEqual(self.git(self.ff, "rev-parse", "HEAD"), self.rev_a)

    def test_dirty_even_when_head_matches_aborts(self):
        self.assertEqual(self.fetch(self.rev_a).returncode, 0)
        (self.ff / "f.txt").write_text("edit\n")
        r = self.fetch(self.rev_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.ff / "f.txt").read_text(), "edit\n")

    def test_untracked_file_aborts_and_is_preserved(self):
        self.assertEqual(self.fetch(self.rev_a).returncode, 0)
        (self.ff / "notes.txt").write_text("keep me\n")
        r = self.fetch(self.rev_b)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.ff / "notes.txt").read_text(), "keep me\n")

    def test_non_git_nonempty_dir_aborts_and_is_preserved(self):
        self.ff.mkdir()
        (self.ff / "precious.txt").write_text("data\n")
        r = self.fetch(self.rev_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("git 作業ツリーではない", r.stderr)
        self.assertEqual((self.ff / "precious.txt").read_text(), "data\n")
        self.assertFalse((self.ff / ".git").exists())

    def test_origin_mismatch_aborts_without_rewriting(self):
        self.assertEqual(self.fetch(self.rev_a).returncode, 0)
        other = "https://example.invalid/someone/else.git"
        self.git(self.ff, "remote", "set-url", "origin", other)
        r = self.fetch(self.rev_b)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("origin が期待する上流 URL と異なる", r.stderr)
        self.assertEqual(self.git(self.ff, "remote", "get-url", "origin"), other)
        self.assertEqual(self.git(self.ff, "rev-parse", "HEAD"), self.rev_a)
        # HEAD が一致する再利用経路でも同じ
        r = self.fetch(self.rev_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.git(self.ff, "remote", "get-url", "origin"), other)

    def test_dot_git_symlink_or_file_aborts(self):
        outside = self.base / "elsewhere.git"
        outside.mkdir()
        self.git(outside, "init", "-q", "--bare")
        before = sorted(p.name for p in outside.iterdir())
        self.ff.mkdir()
        (self.ff / ".git").symlink_to(outside)
        r = self.fetch(self.rev_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("通常のディレクトリではない", r.stderr)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)
        (self.ff / ".git").unlink()
        (self.ff / ".git").write_text(f"gitdir: {outside}\n")
        r = self.fetch(self.rev_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)

    def test_script_has_no_force_checkout_or_clean(self):
        code = "\n".join(l for l in self.func.split("\n") if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"checkout\s+(-q\s+)?-f|--force|git[^\n]* clean|reset --hard")
        self.assertNotIn("set-url", code, "既存 origin を書き換えてはいけない")

class UpstreamLikeRepoNameTest(unittest.TestCase):
    """利用者のリポジトリ名・owner が `fandhe-frontend` を含む場合（例: Fandhe-AI/fandhe-frontend-docs）。

    base_path・自サイトの GitHub URL・title に上流名が部分文字列として現れても誤検出せず、
    上流そのものの表示が残っていれば検出する（偽陰性を作らない）ことを確認する。
    fixture は base_path `/mini-repo` の実出力のため、`/mini-repo` を新しい base_path へ置換して
    生成器の出力と同じ形（全リンクの href が `/fandhe-frontend-docs/…`）にする。
    """

    OWNER, REPO = "Fandhe-AI", "fandhe-frontend-docs"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dist = self.tmp / "dist"
        shutil.copytree(FIXTURE, self.dist)
        for p in self.dist.rglob("*"):
            if p.is_file():
                p.write_text(p.read_text(encoding="utf-8").replace("/mini-repo", f"/{self.REPO}"), encoding="utf-8")
        self.brand = self.tmp / "brand.toml"
        self.brand.write_text(brand_toml(repository=f"https://github.com/{self.OWNER}/{self.REPO}",
                                         brand=self.REPO, copyright="© 2026 Fandhe-AI"), encoding="utf-8")

    def rebrand(self, *extra):
        return run("rebrand_site.py", "--dist", self.dist, "--brand", self.brand, *extra)

    def test_scaffold_check_site_and_rebrand_pass(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        r = run("scaffold.py", "--target", repo, "--owner", self.OWNER, "--repo", self.REPO,
                "--branch", "main", "--title", "fandhe-frontend-docs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('base_path = "/fandhe-frontend-docs"', (repo / "site/nav.toml").read_text())
        r = run("check_site.py", "--root", repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.rebrand()
        self.assertEqual(r.returncode, 0, r.stderr)
        t = (self.dist / "index.html").read_text()
        self.assertIn(f'href="https://github.com/{self.OWNER}/{self.REPO}"', t)
        self.assertIn(f'href="/{self.REPO}/usage/"', t)  # base_path 由来のリンクは残る（誤検出しない）
        self.assertIn("Built with fandhe-frontend docs-site (", t)  # 帰属表記は保持
        self.assertEqual(len(re.findall(r'LICENSE-(?:MIT|APACHE)"', t)), 2)
        self.assertEqual(self.rebrand("--verify-only").returncode, 0)

    def test_owner_containing_upstream_name_with_explicit_copyright(self):
        repo = self.tmp / "repo2"
        repo.mkdir()
        r = run("scaffold.py", "--target", repo, "--owner", "my-fandhe-frontend", "--repo", "fandhe-frontend",
                "--branch", "main", "--title", "Docs", "--copyright", "© 2026 Acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(run("check_site.py", "--root", repo).returncode, 0)

    def test_standalone_upstream_word_in_display_text_rejected_with_guidance(self):
        repo = self.tmp / "repo3"
        repo.mkdir()
        r = run("scaffold.py", "--target", repo, "--owner", "acme", "--repo", "r", "--branch", "main",
                "--title", "T", "--tagline", "Powered by fandhe-frontend.")
        self.assertEqual(r.returncode, 2)
        self.assertIn("独立した語", r.stderr)
        self.assertIn("--copyright", r.stderr)

    def test_upstream_repository_itself_rejected(self):
        for url in ("https://github.com/Fandhe-AI/fandhe-frontend", "https://github.com/fandhe-ai/FANDHE-FRONTEND"):
            self.brand.write_text(brand_toml(repository=url), encoding="utf-8")
            self.assertEqual(self.rebrand().returncode, 2, url)
        r = run("scaffold.py", "--target", self.tmp / "x", "--owner", "fandhe-ai", "--repo", "Fandhe-Frontend",
                "--branch", "main", "--title", "T")
        self.assertEqual(r.returncode, 2)

    def test_leftover_upstream_chrome_is_still_detected(self):
        self.assertEqual(self.rebrand().returncode, 0)
        idx = self.dist / "index.html"
        good = idx.read_text()
        mutations = {
            "上流 GitHub リンク": lambda t: t.replace(f'"https://github.com/{self.OWNER}/{self.REPO}"', '"https://github.com/Fandhe-AI/fandhe-frontend"', 1),
            "上流 crates.io リンク": lambda t: t.replace("</ul></div></div></nav>", '<li><a href="https://crates.io/crates/fandhe-frontend-core">crates.io</a></li></ul></div></div></nav>', 1),
            "上流のブランド名": lambda t: t.replace(f"</span>{self.REPO}</a>", "</span>fandhe-frontend</a>", 1),
            "上流の著作権表記": lambda t: t.replace("© 2026 Fandhe-AI", "© 2026 Fandhe-AI / fandhe-frontend contributors", 1),
            "文末の上流名": lambda t: t.replace("Tiny site", "Built on fandhe-frontend.", 1),
            "上流 favicon の aria-label": lambda t: t.replace(f'aria-label="{self.REPO}"', 'aria-label="fandhe-frontend"', 1),
        }
        for name, mutate in mutations.items():
            mutated = mutate(good)
            self.assertNotEqual(mutated, good, f"{name}: 変異が適用されていない（テスト自体の不備）")
            idx.write_text(mutated, encoding="utf-8")
            r = self.rebrand("--verify-only")
            self.assertEqual(r.returncode, 1, f"{name}: 残存を検出できていない（偽陰性）")
        idx.write_text(good, encoding="utf-8")
        self.assertEqual(self.rebrand("--verify-only").returncode, 0)

    def test_unreplaced_upstream_dist_still_fails_verify(self):
        shutil.rmtree(self.dist)
        shutil.copytree(FIXTURE, self.dist)  # base_path も上流のまま（rebrand 前の実物）
        r = self.rebrand("--verify-only")
        self.assertEqual(r.returncode, 1)
        self.assertIn("残っている", r.stderr)

class WriteBoundaryTest(unittest.TestCase):
    """書き込み・削除先が対象リポジトリの外へ出ないことの回帰テスト（symlink 経由の脱出）。

    build-local.sh は guard_path（bash）、scaffold.py / rebrand_site.py は resolves_inside（Python）を通す。
    ガードは fetch・cargo より前で実行されるため、ネットワーク・ビルド無しで中止を確認できる。
    各ケースで「リンク先（外部ディレクトリ）が無変更」であることを検証する。
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        r = run("scaffold.py", "--target", self.repo, "--owner", "acme", "--repo", "r", "--branch", "main", "--title", "T")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("keep\n")
        self.snapshot = self.snap()

    def snap(self):
        return sorted((p.relative_to(self.outside).as_posix(), p.read_text() if p.is_file() else "") for p in self.outside.rglob("*"))

    def build(self, *args):
        return subprocess.run(["bash", str(self.repo / "tools/docs-site-gen/build-local.sh"), *args],
                              capture_output=True, text=True, cwd=self.repo)

    def assert_aborted(self, r, needle=None):
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.snap(), self.snapshot, "リンク先が変更されている")
        if needle:
            self.assertIn(needle, r.stderr)
        self.assertNotIn("==> fandhe-frontend", r.stderr.split("エラー")[0] if "エラー" in r.stderr else "")

    def test_ff_symlink_to_outside_dir(self):
        (self.repo / "_ff").symlink_to(self.outside)
        self.assert_aborted(self.build(), "_ff")

    def test_ff_symlink_to_empty_outside_dir(self):
        empty = self.base / "empty"
        empty.mkdir()
        (self.repo / "_ff").symlink_to(empty)
        r = self.build()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(empty.iterdir()), [], "空のリンク先に git init された")

    def test_target_dir_symlink(self):
        (self.repo / "tools/docs-site-gen/target").symlink_to(self.outside)
        self.assert_aborted(self.build(), "target")

    def test_cargo_lock_symlink(self):
        (self.repo / "tools/docs-site-gen/Cargo.lock").symlink_to(self.outside / "keep.txt")
        self.assert_aborted(self.build(), "Cargo.lock")

    def test_third_party_licenses_symlink_file_and_dir(self):
        link = self.repo / "THIRD-PARTY-LICENSES"
        link.symlink_to(self.outside / "keep.txt")
        self.assert_aborted(self.build("--write-third-party"), "THIRD-PARTY-LICENSES")
        link.unlink()
        link.symlink_to(self.outside)
        self.assert_aborted(self.build("--write-third-party"), "THIRD-PARTY-LICENSES")
        link.unlink()
        link.mkdir()
        self.assert_aborted(self.build("--write-third-party"), "ディレクトリ")

    def test_out_symlink_and_default_site_symlink_with_clean(self):
        (self.repo / "_site").symlink_to(self.outside)
        self.assert_aborted(self.build("--clean"), "シンボリックリンク")
        self.assert_aborted(self.build(), "シンボリックリンク")
        link = self.base / "outlink"
        link.symlink_to(self.outside)
        self.assert_aborted(self.build("--out", str(link)), "シンボリックリンク")

    def test_out_inside_repo_via_symlinked_parent_escape(self):
        (self.repo / "build").symlink_to(self.outside)
        self.assert_aborted(self.build("--out", str(self.repo / "build" / "x")), "対象リポジトリの外")

    def test_tools_dir_symlinked_outside(self):
        real = self.base / "real-tools"
        shutil.move(str(self.repo / "tools"), str(real))
        (self.repo / "tools").symlink_to(real)
        r = subprocess.run(["bash", str(self.repo / "tools/docs-site-gen/build-local.sh")],
                           capture_output=True, text=True, cwd=self.repo)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("対象リポジトリの外", r.stderr)

    def test_clean_only_removes_default_site_dir(self):
        (self.repo / "_site").mkdir()
        (self.repo / "_site" / "inner-link").symlink_to(self.outside)
        # rm -r は dist 内の symlink を辿らず、リンク自体だけを消す。リンク先は無傷。
        r = self.build("--clean", "--out", str(self.base / "elsewhere"))
        self.assertEqual(r.returncode, 2)
        self.assertTrue((self.repo / "_site").exists())
        self.assertEqual(self.snap(), self.snapshot)

    # ---- Python 側

    def test_rebrand_refuses_dist_with_symlinks(self):
        dist = self.base / "dist"
        shutil.copytree(FIXTURE, dist)
        (self.outside / "victim.html").write_text("untouched\n")
        self.snapshot = self.snap()
        brand = self.base / "b.toml"
        brand.write_text(brand_toml(), encoding="utf-8")
        (dist / "assets" / "favicon.svg").unlink()
        (dist / "assets" / "favicon.svg").symlink_to(self.outside / "victim.html")
        r = run("rebrand_site.py", "--dist", dist, "--brand", brand)
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)
        self.assertEqual(self.snap(), self.snapshot)
        # ディレクトリ symlink（辿って中の HTML を書き換えない）
        (dist / "assets" / "favicon.svg").unlink()
        shutil.copy(FIXTURE / "assets/favicon.svg", dist / "assets/favicon.svg")
        shutil.copy(FIXTURE / "index.html", self.outside / "index.html")
        self.snapshot = self.snap()
        (dist / "linked").symlink_to(self.outside)
        r = run("rebrand_site.py", "--dist", dist, "--brand", brand)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.snap(), self.snapshot)
        r = run("rebrand_site.py", "--dist", dist, "--brand", brand, "--verify-only")
        self.assertEqual(r.returncode, 1)

    def test_scaffold_gitignore_symlink_and_dangling(self):
        repo2 = self.base / "repo2"
        repo2.mkdir()
        (repo2 / ".gitignore").symlink_to(self.base / "does-not-exist")  # 外を指すぶら下がりリンク
        r = run("scaffold.py", "--target", repo2, "--owner", "acme", "--repo", "r", "--branch", "main", "--title", "T")
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.base / "does-not-exist").exists())

    def test_resolves_inside_helper(self):
        sys.path.insert(0, str(SCRIPTS))
        import _common
        root = Path(os.path.realpath(self.repo))
        (self.repo / "ln").symlink_to(self.outside)
        self.assertTrue(_common.resolves_inside(root, self.repo / "new" / "deep" / "f"))
        self.assertFalse(_common.resolves_inside(root, self.repo / "ln" / "f"))
        self.assertFalse(_common.resolves_inside(root, self.repo / "ln" / "a" / "b"))
        self.assertTrue(_common.resolves_inside(root, root))


class CheckSiteTest(unittest.TestCase):
    NAV = """[site]
title = "T"
base_path = "/mini-repo"

[[section]]
title = "Guide"
index_path = "/"

[[section.page]]
title = "Home"
source = "site/index.md"
path = "/"
{extra}"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "site").mkdir()
        (self.root / "site/index.md").write_text("# T\n")
        (self.root / "tools/docs-site-gen").mkdir(parents=True)
        (self.root / "tools/docs-site-gen/brand.toml").write_text(
            brand_toml(repository="https://github.com/acme/mini-repo"), encoding="utf-8")
        self.write_nav("")

    def write_nav(self, extra, base="/mini-repo"):
        (self.root / "site/nav.toml").write_text(
            self.NAV.format(extra=extra).replace("/mini-repo", base, 1), encoding="utf-8")

    def check(self):
        return run("check_site.py", "--root", self.root)

    def test_ok(self):
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_reserved_paths_rejected(self):
        for path in ("/themes/accordion/", "/primitives/button/", "/blocks/hero/", "/wireframes/login/"):
            (self.root / "site/a.md").write_text("# a\n")
            self.write_nav(f'\n[[section.page]]\ntitle = "A"\nsource = "site/a.md"\npath = "{path}"\n')
            r = self.check()
            self.assertEqual(r.returncode, 1, path)
            self.assertIn("予約パス", r.stderr)

    def test_reserved_index_path_rejected(self):
        (self.root / "site/nav.toml").write_text(self.NAV.format(extra="").replace('index_path = "/"', 'index_path = "/themes/x/"'))
        self.assertEqual(self.check().returncode, 1)

    def test_similar_but_allowed_path(self):
        (self.root / "site/a.md").write_text("# a\n")
        self.write_nav('\n[[section.page]]\ntitle = "A"\nsource = "site/a.md"\npath = "/theme-guide/"\n')
        self.assertEqual(self.check().returncode, 0)

    def test_upstream_name_in_nav_title_rejected_early(self):
        (self.root / "site/a.md").write_text("# a\n")
        self.write_nav('\n[[section.page]]\ntitle = "Using Fandhe-Frontend"\nsource = "site/a.md"\npath = "/a/"\n')
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("独立した語", r.stderr)

    def test_base_path_mismatch(self):
        self.write_nav("", base="/other")
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("base_path", r.stderr)

    def test_user_site_repo_requires_empty_base_path(self):
        (self.root / "tools/docs-site-gen/brand.toml").write_text(
            brand_toml(repository="https://github.com/acme/acme.github.io"), encoding="utf-8")
        self.assertEqual(self.check().returncode, 1)
        self.write_nav("", base="")
        self.assertEqual(self.check().returncode, 0)

    def test_reserved_asset_names(self):
        (self.root / "site/assets/search-index").mkdir(parents=True)
        (self.root / "site/assets/site.css").write_text("")
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("site.css", r.stderr)
        self.assertIn("search-index", r.stderr)

    def test_placeholder_left_behind(self):
        (self.root / "site/index.md").write_text("# __SGP_SITE_TITLE__\n")
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("__SGP_SITE_TITLE__", r.stderr)

    def test_non_subset_toml_rejected(self):
        self.write_nav('\n[[section.page]]\ntitle = "A"\nsource = "x"\npath = ["/a/"]\n')
        self.assertEqual(self.check().returncode, 2)

    def test_warnings_for_images_and_absolute_links(self):
        (self.root / "site/index.md").write_text("![a](x.png)\n[l](/wrong/)\n`![ok](x)`\n```\n![f](x)\n```\n")
        r = self.check()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.count("画像記法"), 1)
        self.assertIn("/wrong/", r.stderr)


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def scaffold(self, *extra):
        return run("scaffold.py", "--target", self.tmp, "--owner", "acme", "--repo", "mini-repo",
                   "--branch", "main", "--title", "Mini", *extra)

    def test_scaffold_then_check_site_passes_and_no_overwrite(self):
        r = self.scaffold()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(run("check_site.py", "--root", self.tmp).returncode, 0)
        (self.tmp / "site/index.md").write_text("edited\n")
        self.assertEqual(self.scaffold().returncode, 0)
        self.assertEqual((self.tmp / "site/index.md").read_text(), "edited\n")
        gi = (self.tmp / ".gitignore").read_text()
        self.assertEqual(gi.count("_ff/"), 1)
        self.assertTrue((self.tmp / "tools/docs-site-gen/build-local.sh").stat().st_mode & 0o111)

    def test_injection_like_values(self):
        r = self.scaffold("--tagline", 'x"\nevil = "1')
        self.assertEqual(r.returncode, 2)
        r = run("scaffold.py", "--target", self.tmp, "--owner", "a/b", "--repo", "r", "--branch", "main", "--title", "T")
        self.assertEqual(r.returncode, 2)
        r = run("scaffold.py", "--target", self.tmp, "--owner", "acme", "--repo", "r", "--branch", "main; rm -rf /", "--title", "T")
        self.assertEqual(r.returncode, 2)

    def test_placeholder_and_bidi_values_rejected(self):
        for kw in (["--tagline", "x __SGP_REPOSITORY__ y"], ["--title", "a __SGP_BASE_PATH__"],
                   ["--tagline", "abc\u202edef"], ["--tagline", "a\u061cb"], ["--tagline", "a\u0085b"],
                   ["--tagline", "a\u2028b"], ["--tagline", "a\u2029b"], ["--tagline", "a\x9fb"], ["--copyright", "\u2066x\u2069"], ["--tagline", "a\u200fb"]):
            r = self.scaffold(*kw)
            self.assertEqual(r.returncode, 2, kw)
        self.assertFalse((self.tmp / "site").exists())

    def test_brand_toml_rejects_bidi(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        shutil.copytree(FIXTURE, d / "dist")
        (d / "b.toml").write_text(brand_toml(brand="a\u202eb"), encoding="utf-8")
        r = run("rebrand_site.py", "--dist", d / "dist", "--brand", d / "b.toml")
        self.assertEqual(r.returncode, 2)

    def test_quotes_in_title_are_escaped(self):
        r = run("scaffold.py", "--target", self.tmp, "--owner", "acme", "--repo", "mini-repo",
                "--branch", "main", "--title", 'Say "hi" \\ there')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(run("check_site.py", "--root", self.tmp).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
