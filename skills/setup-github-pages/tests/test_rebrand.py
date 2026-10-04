"""rebrand_site.py / check_site.py / scaffold.py の回帰テスト（unittest・標準ライブラリのみ）。

fixtures/raw は生成器の実出力（rebrand 前）。手書き fixture では上流の構造ずれを検知できないため、
実物を使う。`rebrand.test.mjs` から `node --test` 経由でも実行される。
"""

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
        self.assertIn("残存検査と衝突", r.stderr)

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
                   ["--tagline", "abc\u202edef"], ["--copyright", "\u2066x\u2069"], ["--tagline", "a\u200fb"]):
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
