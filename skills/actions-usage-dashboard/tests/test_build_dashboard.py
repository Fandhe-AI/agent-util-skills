#!/usr/bin/env python3
"""build_dashboard.py の回帰テスト。

役割: batch.json の形（ArtifactData batch の writes）、index.json の 256 KB 検査、
--update が dataset の source と updated だけを変えること（jobs だけの更新を含む）、--add-dataset による新 dataset の set、入力検証の fail-closed、--refresh-template によるページ（files/index.html）の差し替えと
差し替えなしの更新が従来と同じ batch を出すこと、--template-only（rows・meta を書かず雛形だけを 1 段で差し替える経路）を固定する。
CI（ci.yml の skill-tests ジョブ）で `python3 -I -B -m unittest discover` により直接実行される。

fixture は既存の合成データ（org は example-org）。本リポは PUBLIC のため実在の値を入れない。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "build_dashboard.py")
TEMPLATE = os.path.join(HERE, "..", "templates", "dashboard", "index.html")
FIX = os.path.join(HERE, "fixtures", "dashboard")
BILLING = os.path.join(FIX, "billing.json")
JOBS = os.path.join(FIX, "jobs.json")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import build_dashboard as bd  # noqa: E402

ID_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]+$")


def run(args):
    r = subprocess.run([sys.executable, "-I", "-B", SCRIPT] + args, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    return r.returncode, r.stdout, r.stderr


def base(out, rows=(BILLING, JOBS)):
    a = ["--org", "example-org", "--out-dir", out, "--updated-at", "2026-10-03T12:00:00Z"]
    for r in rows:
        a += ["--rows", r]
    return a


def urls_for(n, prefix="u"):
    """--asset-url の引数列。rows 1 件につき rows と meta の 2 件が要る。"""
    a = []
    for i in range(2 * n):
        a += ["--asset-url", "/_blob/%s%d" % (prefix, i)]
    return a


# 既存の dataset を更新するときの --if-version（rows と meta の版は別）。
IFV_BOTH = ["--if-version", "billing=3", "--if-version", "billing_meta=4",
            "--if-version", "jobs=5", "--if-version", "jobs_meta=6"]


def load(d, name):
    with open(os.path.join(d, name), encoding="utf-8") as f:
        return json.load(f)


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def stage2(self, rows=(BILLING, JOBS), extra=()):
        return run(base(self.out, rows) + urls_for(len(rows)) + list(extra))

    def test_t1_batch_shape(self):
        rc, _, err = self.stage2()
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([(w["collection"], w["doc_id"]) for w in b],
                         [("dash", "meta"), ("dash", "params"), ("datasets", "billing"),
                          ("datasets", "billing_meta"), ("datasets", "jobs"),
                          ("datasets", "jobs_meta"), ("files", "index.html")])
        for w in b:
            self.assertEqual(set(w), {"op", "collection", "doc_id", "file_path"})
            self.assertEqual(w["op"], "set")
            self.assertTrue(ID_RE.match(w["collection"]) and ID_RE.match(w["doc_id"]))
            self.assertTrue(os.path.isabs(w["file_path"]))
            self.assertTrue(w["file_path"].startswith(os.path.realpath(self.out)))
            with open(w["file_path"], encoding="utf-8") as f:
                self.assertIsInstance(json.load(f), dict)

    def test_t2_billing_only(self):
        rc, _, err = self.stage2(rows=(BILLING,))
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual(len(b), 5)
        ids = [(w["collection"], w["doc_id"]) for w in b]
        self.assertIn(("datasets", "billing_meta"), ids)
        self.assertNotIn(("datasets", "jobs"), ids)
        self.assertNotIn(("datasets", "jobs_meta"), ids)

    def test_t3_index_verbatim(self):
        self.stage2()
        with open(TEMPLATE, "rb") as f:
            html = f.read().decode("utf-8")
        self.assertEqual(load(self.out, "index.json"), {"text": html})

    def test_t4_dataset_source(self):
        self.stage2()
        d = load(self.out, "dataset-billing.json")
        self.assertEqual(d["source"], {"kind": "file", "url": "/_blob/u0", "name": "billing.json"})
        self.assertEqual(d["updated"], {"at": "2026-10-03T12:00:00Z", "by": "actions-usage-dashboard"})
        # --asset-url は「rows → その meta」の交互の順に対応する。
        want = [("billing", "u0", "billing.json"), ("billing_meta", "u1", "meta-billing.json"),
                ("jobs", "u2", "jobs.json"), ("jobs_meta", "u3", "meta-jobs.json")]
        for did, u, name in want:
            self.assertEqual(load(self.out, "dataset-%s.json" % did)["source"],
                             {"kind": "file", "url": "/_blob/" + u, "name": name}, did)

    def test_t5_params(self):
        self.stage2()
        p = load(self.out, "dash-params.json")
        self.assertEqual(p["group_by"], "charge_class")
        self.assertIsNone(p["plan_included_minutes"])
        self.stage2(extra=["--plan-included-minutes", "3000"])
        self.assertEqual(load(self.out, "dash-params.json")["plan_included_minutes"], 3000)

    def test_t6_oversize_template(self):
        import contextlib
        import io
        from unittest import mock
        big = os.path.join(self.out, "big.html")
        with open(big, "w", encoding="utf-8") as f:
            f.write("a" * (256 * 1024 + 1))
        out = os.path.join(self.out, "o")
        buf = io.StringIO()
        with mock.patch.object(bd, "TEMPLATE_PATH", big), contextlib.redirect_stderr(buf):
            rc = bd.main(base(out))
        self.assertEqual(rc, 1)
        self.assertIn("256 KB", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(out, "index.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "batch.json")))

    def test_template_arg_removed(self):
        out = os.path.join(self.out, "o")
        rc, _, err = run(base(out) + ["--template", TEMPLATE])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(out))
        with open(SCRIPT, encoding="utf-8") as f:
            self.assertIsNone(re.search(r'add_argument\(\s*"--template"', f.read()))

    def test_t7_boundary(self):
        overhead = bd.doc_bytes({"text": ""})
        bd.check_doc_size("x", {"text": "a" * (256 * 1024 - 1 - overhead)})
        with self.assertRaises(bd.BuildError):
            bd.check_doc_size("x", {"text": "a" * (256 * 1024 - overhead)})

    def test_t10_stage1(self):
        stale = os.path.join(self.out, "batch.json")
        with open(stale, "w") as f:
            f.write("[]")
        rc, out, _ = run(base(self.out))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(os.path.join(self.out, "dataset-billing.json")))
        mb = os.path.join(os.path.realpath(self.out), "meta-billing.json")
        mj = os.path.join(os.path.realpath(self.out), "meta-jobs.json")
        order = [out.index(x) for x in (os.path.abspath(BILLING), mb, os.path.abspath(JOBS), mj)]
        self.assertEqual(order, sorted(order))
        self.assertEqual(load(self.out, "meta-billing.json")[0],
                         {"source": "billing", "kind": "since", "value": "2026-09"})
        self.assertTrue(os.path.exists(mj))

    def test_asset_url_count_must_match_uploads(self):
        for n in (1, 2, 3):
            a = base(self.out)
            for i in range(n):
                a += ["--asset-url", "/_blob/u%d" % i]
            rc, _, _ = run(a)
            self.assertEqual(rc, 1, n)
        self.assertFalse(os.path.exists(os.path.join(self.out, "batch.json")))

    def test_t12_output_small(self):
        rc, out, _ = self.stage2()
        self.assertLessEqual(len(out.splitlines()), 8)
        self.assertIn(os.path.join(os.path.realpath(self.out), "batch.json"), out)
        self.assertNotIn("repo-a", out)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def upd(self, ifv=("billing=3", "billing_meta=4", "jobs=5", "jobs_meta=6"),
            urls=("/_blob/n0", "/_blob/n1", "/_blob/n2", "/_blob/n3")):
        a = base(self.out) + ["--update"]
        for u in urls:
            a += ["--asset-url", u]
        for v in ifv:
            a += ["--if-version", v]
        return run(a)

    def test_t8_update_changes_two_fields(self):
        run(base(self.out) + urls_for(2, "o"))
        created = load(self.out, "dataset-billing.json")
        rc, _, err = self.upd()
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([w["op"] for w in b], ["update"] * 4)
        self.assertEqual([w["doc_id"] for w in b], ["billing", "billing_meta", "jobs", "jobs_meta"])
        self.assertEqual([w["if_version"] for w in b], [3, 4, 5, 6])
        self.assertTrue(all(w["collection"] == "datasets" for w in b))
        upd = load(self.out, "dataset-billing.update.json")
        self.assertEqual(set(upd), {"source", "updated"})
        merged = dict(created)
        merged.update(upd)
        self.assertEqual(merged["title"], created["title"])
        self.assertEqual(merged["description"], created["description"])
        self.assertEqual(merged["source"]["kind"], created["source"]["kind"])
        self.assertEqual(merged["source"]["name"], created["source"]["name"])
        self.assertEqual(merged["source"]["url"], "/_blob/n0")

    def jobs_only(self, *extra):
        return run(base(self.out, (JOBS,)) + ["--update"] + list(extra))

    JOBS_URLS = ["--asset-url", "/_blob/n0", "--asset-url", "/_blob/n1"]

    def test_jobs_only_update(self):
        rc, out, err = self.jobs_only(*self.JOBS_URLS, "--if-version", "jobs=5", "--if-version", "jobs_meta=6")
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([(w["op"], w["collection"], w["doc_id"]) for w in b],
                         [("update", "datasets", "jobs"), ("update", "datasets", "jobs_meta")])
        self.assertEqual([w["if_version"] for w in b], [5, 6])
        upd = load(self.out, "dataset-jobs.update.json")
        self.assertEqual(set(upd), {"source", "updated"})
        self.assertEqual(upd["source"]["url"], "/_blob/n0")
        self.assertEqual(load(self.out, "dataset-jobs_meta.update.json")["source"]["name"], "meta-jobs.json")
        for n in ("dataset-billing.update.json", "dataset-billing_meta.update.json", "dash-meta.json", "index.json"):
            self.assertFalse(os.path.exists(os.path.join(self.out, n)), n)
        self.assertNotIn("example-org/", out)

    def test_jobs_only_add(self):
        rc, _, err = self.jobs_only(*self.JOBS_URLS, "--add-dataset", "jobs")
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([(w["op"], w["doc_id"]) for w in b], [("set", "jobs"), ("set", "jobs_meta")])
        self.assertNotIn("if_version", b[0])
        self.assertNotIn("if_version", b[1])
        self.assertEqual(set(load(self.out, "dataset-jobs.json")), {"title", "description", "source", "updated"})

    def test_jobs_only_stage1(self):
        with open(os.path.join(self.out, "batch.json"), "w") as f:
            f.write("[]")
        rc, out, err = self.jobs_only()
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.out, "batch.json")))
        self.assertIn("jobs.json", out)
        self.assertNotIn("billing.json", out)

    def test_jobs_only_invalid(self):
        u = self.JOBS_URLS
        cases = [
            u + ["--if-version", "billing=3"],
            u + ["--if-version", "jobs=5", "--if-version", "jobs_meta=6", "--if-version", "billing=3"],
            u + ["--if-version", "jobs=5"],
            u + ["--if-version", "jobs_meta=6"],
            u,
            u + ["--add-dataset", "jobs", "--if-version", "jobs=1"],
            u + ["--add-dataset", "jobs", "--if-version", "jobs_meta=1"],
            ["--asset-url", "/_blob/n0", "--if-version", "jobs=5", "--if-version", "jobs_meta=6"],
        ]
        for c in cases:
            rc, _, _ = self.jobs_only(*c)
            self.assertEqual(rc, 1, c)
            self.assertFalse(os.path.exists(os.path.join(self.out, "batch.json")), c)

    def test_create_requires_billing(self):
        rc, _, err = run(base(self.out, (JOBS,)) + urls_for(1))
        self.assertEqual(rc, 1)
        self.assertIn("billing", err)
        for n in ("index.json", "batch.json"):
            self.assertFalse(os.path.exists(os.path.join(self.out, n)), n)

    def test_add_dataset_choices_fixed(self):
        rc, _, _ = self.jobs_only("--add-dataset", "warnings", *self.JOBS_URLS)
        self.assertEqual(rc, 2)
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertRegex(src, r'add_argument\("--add-dataset"[^)]*choices=\["jobs", "billing_meta", "jobs_meta"\]')
        self.assertEqual(bd.ALL_IDS, ("billing", "billing_meta", "jobs", "jobs_meta"))

    def test_t9_bad_if_version(self):
        for ifv in [("billing=3",), ("billing=3", "billing=4"), ("billing=x", "jobs=1"),
                    ("billing=0", "jobs=1"), ("billing=1", "other=1")]:
            rc, _, _ = self.upd(ifv=ifv)
            self.assertEqual(rc, 1, ifv)


    def add(self, extra, rows=(BILLING, JOBS), urls=("/_blob/n0", "/_blob/n1"), update=True):
        a = base(self.out, rows) + (["--update"] if update else [])
        for u in urls:
            a += ["--asset-url", u]
        return run(a + list(extra))

    def test_add_jobs_to_billing_only_dashboard(self):
        rc, _, err = run(base(self.out, (BILLING,)) + urls_for(1, "o"))
        self.assertEqual(rc, 0, err)
        rc, _, err = self.add(["--if-version", "billing=3", "--if-version", "billing_meta=4",
                               "--add-dataset", "jobs"], urls=("/_blob/n0", "/_blob/n1", "/_blob/n2", "/_blob/n3"))
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([(w["op"], w["collection"], w["doc_id"]) for w in b],
                         [("update", "datasets", "billing"), ("update", "datasets", "billing_meta"),
                          ("set", "datasets", "jobs"), ("set", "datasets", "jobs_meta")])
        self.assertEqual([b[0]["if_version"], b[1]["if_version"]], [3, 4])
        self.assertEqual(set(b[2]), {"op", "collection", "doc_id", "file_path"})
        real = os.path.realpath(self.out)
        for w in b:
            self.assertTrue(os.path.isabs(w["file_path"]))
            self.assertTrue(w["file_path"].startswith(real + os.sep))
        jd = load(self.out, "dataset-jobs.json")
        self.assertEqual(set(jd), {"title", "description", "source", "updated"})
        self.assertEqual(jd["source"], {"kind": "file", "url": "/_blob/n2", "name": "jobs.json"})
        self.assertEqual(load(self.out, "dataset-jobs_meta.json")["source"]["url"], "/_blob/n3")
        self.assertEqual(jd["updated"]["at"], "2026-10-03T12:00:00Z")
        self.assertEqual(set(load(self.out, "dataset-billing.update.json")), {"source", "updated"})
        self.assertFalse(os.path.exists(os.path.join(self.out, "dataset-jobs.update.json")))

    def test_migrate_existing_dashboard_with_add_meta(self):
        """meta なしで作られた既存ダッシュボードの移行: rows は update、meta は set。"""
        rc, _, err = self.add(["--if-version", "billing=3", "--if-version", "jobs=5",
                               "--add-dataset", "billing_meta", "--add-dataset", "jobs_meta"],
                              urls=("/_blob/n0", "/_blob/n1", "/_blob/n2", "/_blob/n3"))
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([(w["op"], w["doc_id"], w.get("if_version")) for w in b],
                         [("update", "billing", 3), ("set", "billing_meta", None),
                          ("update", "jobs", 5), ("set", "jobs_meta", None)])

    def test_add_meta_without_rows_rejected(self):
        rc, _, _ = self.add(["--if-version", "billing=3", "--if-version", "billing_meta=4",
                             "--add-dataset", "jobs_meta"], rows=(BILLING,), urls=("/_blob/n0", "/_blob/n1"))
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(os.path.join(self.out, "batch.json")))

    def test_add_dataset_invalid(self):
        two = ("/_blob/n0", "/_blob/n1")
        cases = [
            (["--add-dataset", "jobs"], dict(update=False)),
            (["--if-version", "billing=1", "--if-version", "billing_meta=2", "--add-dataset", "jobs"],
             dict(rows=(BILLING,), urls=two)),
            (["--add-dataset", "jobs", "--if-version", "jobs=1", "--if-version", "billing=1"], {}),
            (["--add-dataset", "jobs", "--add-dataset", "jobs", "--if-version", "billing=1",
              "--if-version", "billing_meta=2"], {}),
            (["--add-dataset", "billing", "--if-version", "jobs=1"], {}),
            (["--add-dataset", "jobs"], {}),
            (["--add-dataset", "jobs", "--if-version", "billing=1"], {}),
        ]
        for extra, kw in cases:
            with tempfile.TemporaryDirectory() as d:
                self.out = d
                rc, _, _ = self.add(extra, **kw)
                self.assertIn(rc, (1, 2), extra)  # choices 違反は argparse の 2
                self.assertFalse(os.path.exists(os.path.join(d, "batch.json")), extra)

    def test_add_dataset_stage1(self):
        os.makedirs(self.out, exist_ok=True)
        for n in ("batch.json", "dataset-jobs.json"):
            with open(os.path.join(self.out, n), "w") as f:
                f.write("[]")
        with open(os.path.join(self.out, "dataset-jobs_meta.json"), "w") as f:
            f.write("[]")
        rc, out, err = self.add(["--add-dataset", "jobs"], urls=())
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.out, "batch.json")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "dataset-jobs.json")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "dataset-jobs_meta.json")))
        self.assertIn("meta-jobs.json", out)
        self.assertIn("billing.json", out)
        self.assertIn("jobs.json", out)
        rc, _, _ = self.add(["--add-dataset", "jobs"], rows=(BILLING,), urls=())
        self.assertEqual(rc, 1)


class RefreshTemplateTests(unittest.TestCase):
    """--update --refresh-template（Issue #207）。既定の更新の batch は変わらないことも固定する。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    FOUR = ("/_blob/n0", "/_blob/n1", "/_blob/n2", "/_blob/n3")

    def go(self, extra, rows=(BILLING, JOBS), urls=FOUR, update=True):
        a = base(self.out, rows) + (["--update"] if update else [])
        for u in urls:
            a += ["--asset-url", u]
        return run(a + list(extra))

    def shape(self):
        return [(w["op"], w["collection"], w["doc_id"], w.get("if_version"),
                 os.path.basename(w["file_path"])) for w in load(self.out, "batch.json")]

    def exists(self, n):
        return os.path.exists(os.path.join(self.out, n))

    def test_refresh_template_batch(self):
        rc, out, err = self.go(IFV_BOTH + ["--refresh-template", "--template-if-version", "7"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.shape(), [
            ("update", "datasets", "billing", 3, "dataset-billing.update.json"),
            ("update", "datasets", "billing_meta", 4, "dataset-billing_meta.update.json"),
            ("update", "datasets", "jobs", 5, "dataset-jobs.update.json"),
            ("update", "datasets", "jobs_meta", 6, "dataset-jobs_meta.update.json"),
            ("set", "files", "index.html", 7, "index.json")])
        last = load(self.out, "batch.json")[-1]
        self.assertEqual(set(last), {"op", "collection", "doc_id", "file_path", "if_version"})
        self.assertTrue(last["file_path"].startswith(os.path.realpath(self.out) + os.sep))
        with open(TEMPLATE, encoding="utf-8") as f:
            self.assertEqual(load(self.out, "index.json"), {"text": f.read()})
        self.assertLessEqual(len(out.splitlines()), 8)
        self.assertNotIn("<html", out.lower())

    def test_refresh_template_jobs_only(self):
        rc, _, err = self.go(["--if-version", "jobs=5", "--if-version", "jobs_meta=6", "--refresh-template",
                              "--template-if-version", "2"], rows=(JOBS,), urls=("/_blob/n0", "/_blob/n1"))
        self.assertEqual(rc, 0, err)
        self.assertEqual([x[:3] for x in self.shape()],
                         [("update", "datasets", "jobs"), ("update", "datasets", "jobs_meta"),
                          ("set", "files", "index.html")])

    def test_refresh_template_with_add_dataset(self):
        rc, _, err = self.go(["--if-version", "billing=3", "--if-version", "billing_meta=4", "--add-dataset", "jobs",
                              "--refresh-template", "--template-if-version", "4"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.shape(), [
            ("update", "datasets", "billing", 3, "dataset-billing.update.json"),
            ("update", "datasets", "billing_meta", 4, "dataset-billing_meta.update.json"),
            ("set", "datasets", "jobs", None, "dataset-jobs.json"),
            ("set", "datasets", "jobs_meta", None, "dataset-jobs_meta.json"),
            ("set", "files", "index.html", 4, "index.json")])

    def test_refresh_template_invalid(self):
        cases = [
            (["--refresh-template"], dict(update=False)),
            (["--template-if-version", "3"], dict(update=False)),
            (IFV_BOTH + ["--template-if-version", "3"], {}),
            (IFV_BOTH + ["--refresh-template"], {}),
            (IFV_BOTH + ["--refresh-template", "--template-if-version", "0"], {}),
            (IFV_BOTH + ["--refresh-template", "--template-if-version", "x"], {}),
        ]
        for extra, kw in cases:
            with tempfile.TemporaryDirectory() as d:
                self.out = d
                rc, _, _ = self.go(extra, **kw)
                self.assertIn(rc, (1, 2), extra)
                self.assertFalse(self.exists("batch.json"), extra)
                self.assertFalse(self.exists("index.json"), extra)

    def test_refresh_template_oversize(self):
        import contextlib
        import io
        from unittest import mock
        big = os.path.join(self.out, "big.html")
        with open(big, "w", encoding="utf-8") as f:
            f.write("a" * (256 * 1024 + 1))
        out = os.path.join(self.out, "o")
        for urls, extra in ((self.FOUR, IFV_BOTH + ["--template-if-version", "2"]), ((), [])):
            a = base(out) + ["--update", "--refresh-template"] + extra
            for u in urls:
                a += ["--asset-url", u]
            buf = io.StringIO()
            with mock.patch.object(bd, "TEMPLATE_PATH", big), contextlib.redirect_stderr(buf):
                rc = bd.main(a)
            self.assertEqual(rc, 1)
            self.assertIn("256 KB", buf.getvalue())
            for n in ("batch.json", "index.json", "dataset-billing.update.json", "dataset-jobs.update.json"):
                self.assertFalse(os.path.exists(os.path.join(out, n)), n)

    def test_refresh_template_stage1(self):
        for n in ("batch.json", "index.json"):
            with open(os.path.join(self.out, n), "w") as f:
                f.write("[]")
        rc, out, err = self.go(["--refresh-template"], urls=())
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.exists("batch.json"))
        self.assertTrue(self.exists("index.json"))
        self.assertIn("--template-if-version", out)

    def test_refresh_template_total_size(self):
        import contextlib
        import io
        from unittest import mock
        out = os.path.join(self.out, "o")
        a = base(out) + ["--update", "--refresh-template", "--template-if-version", "2"] + IFV_BOTH + urls_for(2, "n")
        buf = io.StringIO()
        with mock.patch.object(bd, "MAX_BATCH_BYTES", 1000), contextlib.redirect_stderr(buf):
            rc = bd.main(a)
        self.assertEqual(rc, 1)
        self.assertIn("1 MiB", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(out, "batch.json")))

    def test_default_update_batch_unchanged(self):
        cases = [
            (dict(), IFV_BOTH, [
                ("update", "datasets", "billing", 3, "dataset-billing.update.json"),
                ("update", "datasets", "billing_meta", 4, "dataset-billing_meta.update.json"),
                ("update", "datasets", "jobs", 5, "dataset-jobs.update.json"),
                ("update", "datasets", "jobs_meta", 6, "dataset-jobs_meta.update.json")]),
            (dict(rows=(JOBS,), urls=("/_blob/n0", "/_blob/n1")), ["--if-version", "jobs=5", "--if-version", "jobs_meta=6"], [
                ("update", "datasets", "jobs", 5, "dataset-jobs.update.json"),
                ("update", "datasets", "jobs_meta", 6, "dataset-jobs_meta.update.json")]),
            (dict(), ["--if-version", "billing=3", "--if-version", "billing_meta=4", "--add-dataset", "jobs"], [
                ("update", "datasets", "billing", 3, "dataset-billing.update.json"),
                ("update", "datasets", "billing_meta", 4, "dataset-billing_meta.update.json"),
                ("set", "datasets", "jobs", None, "dataset-jobs.json"),
                ("set", "datasets", "jobs_meta", None, "dataset-jobs_meta.json")]),
        ]
        for kw, extra, want in cases:
            with tempfile.TemporaryDirectory() as d:
                self.out = d
                rc, _, err = self.go(extra, **kw)
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.shape(), want)
                self.assertFalse(self.exists("index.json"))


class TemplateOnlyTests(unittest.TestCase):
    """--template-only: rows・meta・datasets を書かず files/index.html だけを版固定で書く 1 段の経路。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.out = os.path.join(self.d, "o")

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, out=None, extra=None, ver="7"):
        a = ["--org", "example-org", "--out-dir", out or self.out, "--update",
             "--refresh-template", "--template-only"]
        if ver is not None:
            a += ["--template-if-version", ver]
        return a + (extra or [])

    def test_template_only_batch(self):
        rc, _, err = run(self.args())
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([(w["op"], w["collection"], w["doc_id"], w["if_version"]) for w in b],
                         [("set", "files", "index.html", 7)])
        self.assertEqual(set(b[0]), {"op", "collection", "doc_id", "file_path", "if_version"})
        self.assertTrue(b[0]["file_path"].startswith(os.path.realpath(self.out) + os.sep))
        with open(TEMPLATE, encoding="utf-8") as f:
            self.assertEqual(load(self.out, "index.json"), {"text": f.read()})

    def test_template_only_writes_no_rows_or_meta(self):
        rc, _, err = run(self.args())
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(os.listdir(self.out)), {"batch.json", "index.json"})
        for w in load(self.out, "batch.json"):
            self.assertNotIn(w["collection"], ("datasets", "dash"))

    def test_template_only_removes_stale_outputs(self):
        os.makedirs(self.out)
        for n in ("batch.json", "dataset-billing.update.json", "meta-billing.json",
                  "dataset-jobs.json", "dash-params.json", "keep.txt"):
            with open(os.path.join(self.out, n), "w") as f:
                f.write("[]")
        rc, _, err = run(self.args())
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(os.listdir(self.out)), {"batch.json", "index.json", "keep.txt"})
        self.assertEqual(len(load(self.out, "batch.json")), 1)

    def test_template_only_invalid(self):
        cases = [
            self.args(ver=None),
            self.args(ver="0"),
            self.args(ver="x"),
            [a for a in self.args() if a != "--update"],
            [a for a in self.args() if a != "--refresh-template"],
            self.args(extra=["--rows", BILLING]),
            self.args(extra=["--asset-url", "/_blob/a"]),
            self.args(extra=["--if-version", "billing=3"]),
            self.args(extra=["--add-dataset", "jobs"]),
            self.args(extra=["--title", "x"]),
            self.args(extra=["--plan-included-minutes", "1"]),
            self.args(extra=["--updated-at", "2026-10-03T12:00:00Z"]),
            self.args(extra=["--updated-by", "x"]),
        ]
        bad_org = self.args()
        bad_org[1] = "a/b"
        cases.append(bad_org)
        for i, a in enumerate(cases):
            out = os.path.join(self.d, "c%d" % i)
            a = [out if x == self.out else x for x in a]
            rc, _, _ = run(a)
            self.assertIn(rc, (1, 2), a)
            for n in ("batch.json", "index.json"):
                self.assertFalse(os.path.exists(os.path.join(out, n)), (a, n))

    def test_template_only_invalid_keeps_existing_files(self):
        os.makedirs(self.out)
        p = os.path.join(self.out, "batch.json")
        with open(p, "w") as f:
            f.write('["keep"]')
        rc, _, _ = run(self.args(ver=None))
        self.assertEqual(rc, 1)
        with open(p) as f:
            self.assertEqual(f.read(), '["keep"]')

    def test_template_only_oversize(self):
        import contextlib
        import io
        from unittest import mock
        big = os.path.join(self.d, "big.html")
        with open(big, "w", encoding="utf-8") as f:
            f.write("a" * (256 * 1024 + 1))
        buf = io.StringIO()
        with mock.patch.object(bd, "TEMPLATE_PATH", big), contextlib.redirect_stderr(buf):
            rc = bd.main(self.args())
        self.assertEqual(rc, 1)
        self.assertIn("256 KB", buf.getvalue())
        for n in ("batch.json", "index.json"):
            self.assertFalse(os.path.exists(os.path.join(self.out, n)), n)

    def test_template_only_output_small(self):
        rc, out, err = run(self.args())
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out.strip().splitlines()), 8)
        self.assertNotIn("<html", out)
        self.assertIn(os.path.join(os.path.realpath(self.out), "batch.json"), out)
        self.assertNotIn("アップロード", out)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.out = os.path.join(self.d, "o")

    def tearDown(self):
        self.tmp.cleanup()

    def doc(self, name, **over):
        d = {"schema_version": 1, "source": "billing", "org": "example-org",
             "since": "2026-09", "until": "2026-10", "rows": []}
        d.update(over)
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            json.dump(d, f)
        return p

    def fails(self, args):
        rc, _, _ = run(args)
        self.assertEqual(rc, 1, args)
        self.assertFalse(os.path.exists(os.path.join(self.out, "batch.json")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "index.json")))

    def test_rows_required_without_template_only(self):
        """--rows を省いても雛形だけの経路とは推測せず、run() が 1 で拒否する。"""
        self.fails(["--org", "example-org", "--out-dir", self.out])
        self.fails(["--org", "example-org", "--out-dir", self.out, "--update"])
        self.fails(["--org", "example-org", "--out-dir", self.out, "--update",
                    "--refresh-template", "--template-if-version", "2"])

    def test_assume_current_visibility_must_be_bool(self):
        """summarize.py と同じく真偽値以外は止める（#209）。"""
        for bad in ("true", 1, None, [True]):
            self.fails(["--org", "example-org", "--out-dir", self.out,
                        "--rows", self.doc("b.json", assume_current_visibility=bad)])

    def test_t11_invalid(self):
        b = self.doc("b.json")
        self.fails(base(self.out, (b, JOBS)) + ["--asset-url", "/_blob/a"])
        for u in ["https://evil.example/x", "/_blob/../x", "/_blob/a?b"]:
            self.fails(base(self.out, (b,)) + ["--asset-url", u, "--asset-url", "/_blob/ok"])
        self.fails(base(self.out, (self.doc("o.json", org="other-org"),)))
        self.fails(base(self.out, (b, self.doc("b2.json"))))
        self.fails(base(self.out, (JOBS,)))
        self.fails(base(self.out, (self.doc("v2.json", schema_version=2),)))
        self.fails(["--org", "a/b", "--out-dir", self.out, "--rows", b])
        big = os.path.join(self.d, "big.json")
        with open(big, "w") as f:
            f.write('{"pad": "' + "a" * (8 * 1024 * 1024) + '"}')
        self.fails(base(self.out, (big,)))

    def test_estimate_block_in_jobs_doc_is_accepted(self):
        """fetch_jobs の推定モード出力（トップレベルの estimate 付き）をそのまま受け付ける（Issue #197）。"""
        with open(JOBS, encoding="utf-8") as f:
            j = json.load(f)
        j["estimate"] = {"method": "sample_mean_per_workflow", "sample_runs": 3, "sampled_runs": 6,
                         "estimated_runs": 40, "groups": 2, "unestimated_runs": 0}
        j["estimate"].update({"budget_runs": 15, "zero_groups": 2, "zero_runs": 330})
        j["warnings"] = ["推定値を含む: テスト"]
        jp = os.path.join(self.d, "jobs-est.json")
        with open(jp, "w") as f:
            json.dump(j, f)
        rc, _, err = run(base(self.out, (BILLING, jp)) + urls_for(2))
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.out, "batch.json")))
        # estimate の中身は渡さず、有無（"true"）だけを meta 行にする。
        meta = load(self.out, "meta-jobs.json")
        self.assertIn({"source": "jobs", "kind": "estimate", "value": "true"}, meta)
        self.assertNotIn("sample_mean_per_workflow", json.dumps(meta))
        for k, v in (("budget_runs", "15"), ("zero_groups", "2"), ("zero_runs", "330")):
            self.assertIn({"source": "jobs", "kind": "estimate_" + k, "value": v}, meta)
        self.assertIn({"source": "jobs", "kind": "warning", "value": "推定値を含む: テスト"}, meta)

    def test_t13_no_network_imports(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        for m in ("subprocess", "urllib", "socket", "http"):
            self.assertIsNone(re.search(r"^\s*(import|from)\s+%s\b" % m, src, re.M), m)


class MetaRowsTests(unittest.TestCase):
    """付帯情報の行（ページの withMeta が読む契約）の生成。"""

    def doc(self, **over):
        d = {"source": "jobs", "since": "2026-10-01", "until": "2026-10-03"}
        d.update(over)
        return d

    def test_since_until_only(self):
        self.assertEqual(bd.build_meta_rows(self.doc()), [
            {"source": "jobs", "kind": "since", "value": "2026-10-01"},
            {"source": "jobs", "kind": "until", "value": "2026-10-03"}])

    def test_warnings_cleaned_and_capped(self):
        ws = ["a\x1b[31mb", 5, None, "", "x" * 400] + ["w%d" % i for i in range(30)]
        rows = bd.build_meta_rows(self.doc(warnings=ws))
        w = [r["value"] for r in rows if r["kind"] == "warning"]
        self.assertEqual(len(w), 20)
        self.assertEqual(w[0], "a[31mb")
        self.assertEqual(w[1], "x" * 300)
        self.assertEqual(w[2], "w0")
        for r in rows:
            self.assertEqual(set(r), {"source", "kind", "value"})
            self.assertIsInstance(r["value"], str)

    def test_bad_warnings_shape_gives_no_warning_rows(self):
        for bad in (None, "text", {"a": 1}, 3):
            rows = bd.build_meta_rows(self.doc(warnings=bad))
            self.assertEqual([r["kind"] for r in rows], ["since", "until"], bad)
        rows = bd.build_meta_rows(self.doc())
        self.assertEqual([r["kind"] for r in rows], ["since", "until"])

    def test_estimate_only_when_dict(self):
        kinds = lambda d: [r["kind"] for r in bd.build_meta_rows(self.doc(estimate=d))]  # noqa: E731
        self.assertEqual(kinds({"method": "m"}), ["since", "until", "estimate"])
        for bad in ([], None, "x", 3, True):
            self.assertEqual(kinds(bad), ["since", "until"], bad)

    def test_estimate_count_rows(self):
        """#219: jobs 文書の estimate から予算・打ち切り数の整数 3 値を文字列行にする。"""
        e = {"method": "m", "budget_runs": 15, "zero_groups": 2, "zero_runs": 330}
        rows = bd.build_meta_rows(self.doc(estimate=e))
        self.assertEqual([r["kind"] for r in rows], ["since", "until", "estimate", "estimate_budget_runs",
                                                     "estimate_zero_groups", "estimate_zero_runs"])
        self.assertEqual([r["value"] for r in rows[3:]], ["15", "2", "330"])
        part = bd.build_meta_rows(self.doc(estimate={"zero_groups": 0}))
        self.assertEqual([(r["kind"], r["value"]) for r in part[2:]], [("estimate", "true"), ("estimate_zero_groups", "0")])
        self.assertNotIn("estimate_budget_runs", [r["kind"] for r in part])

    def test_estimate_count_rows_only_on_jobs(self):
        kinds = [r["kind"] for r in bd.build_meta_rows(self.doc(source="billing", estimate={"budget_runs": 5}))]
        self.assertEqual(kinds, ["since", "until", "estimate"])

    def test_estimate_count_invalid_raises(self):
        for k in bd.EST_COUNT_KEYS:
            for bad in (True, -1, 1.5, "3", None, 10 ** 9 + 1):
                with self.assertRaises(bd.BuildError, msg=(k, bad)):
                    bd.build_meta_rows(self.doc(estimate={k: bad}))

    def test_assume_current_visibility_only_when_true_on_billing(self):
        """#209: 雛形が概算と表示する根拠を billing_meta で渡す。真のときだけ、billing のときだけ。"""
        rows = bd.build_meta_rows(self.doc(source="billing", assume_current_visibility=True))
        self.assertIn({"source": "billing", "kind": "assume_current_visibility", "value": "true"}, rows)
        for flag in (False, None, "true", 1):
            kinds = [r["kind"] for r in bd.build_meta_rows(self.doc(source="billing", assume_current_visibility=flag))]
            self.assertEqual(kinds, ["since", "until"], flag)
        kinds = [r["kind"] for r in bd.build_meta_rows(self.doc(assume_current_visibility=True))]
        self.assertEqual(kinds, ["since", "until"])  # jobs 文書には付けない

    def test_assume_current_visibility_reaches_meta_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(BILLING, encoding="utf-8") as f:
                doc = json.load(f)
            doc["assume_current_visibility"] = True
            p = os.path.join(d, "b.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            out = os.path.join(d, "o")
            rc, _, err = run(base(out, rows=(p,)) + urls_for(1))
            self.assertEqual(rc, 0, err)
            self.assertIn({"source": "billing", "kind": "assume_current_visibility", "value": "true"},
                          load(out, "meta-billing.json"))

    def test_meta_file_is_row_array(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = run(base(d) + urls_for(2))
            self.assertEqual(rc, 0, err)
            for name in ("meta-billing.json", "meta-jobs.json"):
                rows = load(d, name)
                self.assertIsInstance(rows, list)
                self.assertTrue(all(set(r) == {"source", "kind", "value"} for r in rows))


class CommonParityTest(unittest.TestCase):
    """build_dashboard.py は _common.py を import せず複製する（ネットワーク非依存の保証を守るため）。
    複製が正典の _common.py と乖離しないことをここで固定する（Issue #141・#171・#182）。"""

    def test_copies_match_common(self):
        import importlib.util
        import inspect
        path = os.path.join(HERE, "..", "scripts", "_common.py")
        spec = importlib.util.spec_from_file_location("_common_parity", path)
        cm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cm)
        self.assertEqual(bd.ORG_RE.pattern, cm.ORG_RE.pattern)
        self.assertEqual(bd.ORG_RE.flags, cm.ORG_RE.flags)
        self.assertEqual(bd.ISO_RE.pattern, cm.ISO_RE.pattern)
        self.assertEqual(bd.ISO_RE.flags, cm.ISO_RE.flags)
        self.assertEqual(bd.MAX_ROWS_BYTES, cm.MAX_ROWS_BYTES)
        import summarize
        self.assertEqual(bd.MAX_EST_COUNT, summarize.MAX_EST_COUNT)
        self.assertEqual(bd.EST_COUNT_KEYS, summarize.EST_COUNT_KEYS)
        self.assertEqual(inspect.getsource(bd.atomic_write_json),
                         inspect.getsource(cm.atomic_write_json))

    def test_safe_matches_common(self):
        """safe / CTRL_RE / MAX_NAME_LEN の正典は _common.py（Issue #182）。既定長だけが意図した差。"""
        import importlib.util
        import inspect
        path = os.path.join(HERE, "..", "scripts", "_common.py")
        spec = importlib.util.spec_from_file_location("_common_parity_safe", path)
        cm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cm)
        self.assertEqual(bd.CTRL_RE.pattern, cm.CTRL_RE.pattern)
        self.assertEqual(bd.CTRL_RE.flags, cm.CTRL_RE.flags)
        self.assertEqual(inspect.signature(bd.safe).parameters["limit"].default, 300)
        self.assertEqual(inspect.signature(cm.safe).parameters["limit"].default, cm.MAX_NAME_LEN)
        self.assertEqual(cm.MAX_NAME_LEN, 60)
        ctrl = "\x00\x1b\x7f\x85\x9f"
        for limit in (1, 5, 60, 300):
            samples = ["", ctrl, "a" + ctrl + "b", ValueError("e\x1bx"), 12345]
            samples += ["x" * n for n in (limit - 1, limit, limit + 1) if n >= 0]
            samples.append(ctrl + "y" * (limit + 5))
            for s in samples:
                got = bd.safe(s, limit)
                self.assertEqual(got, cm.safe(s, limit), (limit, s))
                self.assertLessEqual(len(got), limit)
                self.assertIsNone(bd.CTRL_RE.search(got))

    def test_summarize_does_not_redefine_safe(self):
        """summarize.py は safe / CTRL_RE を _common から import し、再定義しない（Issue #182）。"""
        with open(os.path.join(HERE, "..", "scripts", "summarize.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(re.search(r"^(def safe\b|CTRL_RE\s*=|MAX_NAME_LEN\s*=)", src, re.M))

    def test_does_not_import_common(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(re.search(r"^\s*(import|from)\s+_common\b", src, re.M))


class CollisionTests(unittest.TestCase):
    def test_rows_in_out_dir_rejected_without_change(self):
        with tempfile.TemporaryDirectory() as d:
            rows = os.path.join(d, "dataset-billing.json")
            with open(BILLING, "rb") as f:
                data = f.read()
            with open(rows, "wb") as f:
                f.write(data)
            for extra in ([], ["--asset-url", "/_blob/u0", "--asset-url", "/_blob/u1"]):
                rc, _, _ = run(["--org", "example-org", "--out-dir", d, "--rows", rows] + extra)
                self.assertEqual(rc, 1)
                with open(rows, "rb") as f:
                    self.assertEqual(f.read(), data)

    def test_rows_colliding_with_meta_output_rejected(self):
        for name in ("meta-billing.json", "meta-jobs.json", "dataset-jobs_meta.json",
                     "dataset-billing_meta.update.json"):
            with tempfile.TemporaryDirectory() as d:
                rows = os.path.join(d, name)
                with open(BILLING, "rb") as f:
                    data = f.read()
                with open(rows, "wb") as f:
                    f.write(data)
                rc, _, _ = run(["--org", "example-org", "--out-dir", d, "--rows", rows])
                self.assertEqual(rc, 1, name)
                with open(rows, "rb") as f:
                    self.assertEqual(f.read(), data, name)

    def test_update_stage1_removes_stale_batch(self):
        with tempfile.TemporaryDirectory() as d:
            bp = os.path.join(d, "batch.json")
            with open(bp, "w") as f:
                f.write("[]")
            rc, _, _ = run(base(d, (BILLING,)) + ["--update"])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(bp))


class UsageCsvHintTests(unittest.TestCase):
    """Issue #228: 使用量 CSV 由来の rows を --refresh-template なしで載せるときだけ案内を出す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(BILLING, encoding="utf-8") as f:
            doc = json.load(f)
        doc["input"] = "usage_csv"
        self.csv_rows = os.path.join(self.tmp.name, "billing-csv.json")
        with open(self.csv_rows, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.out = os.path.join(self.tmp.name, "out")

    def upd(self, rows, extra=()):
        a = base(self.out, (rows,)) + ["--update"] + list(extra)
        for u in ("/_blob/n0", "/_blob/n1"):
            a += ["--asset-url", u]
        a += ["--if-version", "billing=3", "--if-version", "billing_meta=4"]
        return run(a)

    def test_hint_for_csv_update_without_refresh(self):
        rc, out, err = self.upd(self.csv_rows)
        self.assertEqual(rc, 0, err)
        self.assertIn("--refresh-template", out)
        self.assertIn("案内", out)

    def test_no_hint_for_api_rows_or_with_refresh(self):
        rc, out, err = self.upd(BILLING)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("案内", out)
        rc, out, err = self.upd(self.csv_rows, ["--refresh-template", "--template-if-version", "2"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("案内", out)

    def test_hint_does_not_change_batch(self):
        rc, _, err = self.upd(self.csv_rows)
        self.assertEqual(rc, 0, err)
        b1 = load(self.out, "batch.json")
        rc, _, err = self.upd(BILLING)
        self.assertEqual(rc, 0, err)
        self.assertEqual([w["doc_id"] for w in b1], [w["doc_id"] for w in load(self.out, "batch.json")])


if __name__ == "__main__":
    unittest.main()
