#!/usr/bin/env python3
"""build_dashboard.py の回帰テスト。

役割: batch.json の形（ArtifactData batch の writes）、index.json の 256 KB 検査、
--update が dataset の source と updated だけを変えること、入力検証の fail-closed を固定する。
build-dashboard.test.mjs から `node --test` 経由で実行される。

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
        urls = []
        for i in range(len(rows)):
            urls += ["--asset-url", "/_blob/u%d" % i]
        return run(base(self.out, rows) + urls + list(extra))

    def test_t1_batch_shape(self):
        rc, _, err = self.stage2()
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([(w["collection"], w["doc_id"]) for w in b],
                         [("dash", "meta"), ("dash", "params"), ("datasets", "billing"),
                          ("datasets", "jobs"), ("files", "index.html")])
        for w in b:
            self.assertEqual(set(w), {"op", "collection", "doc_id", "file_path"})
            self.assertEqual(w["op"], "set")
            self.assertTrue(ID_RE.match(w["collection"]) and ID_RE.match(w["doc_id"]))
            self.assertTrue(os.path.isabs(w["file_path"]))
            self.assertTrue(w["file_path"].startswith(os.path.realpath(self.out)))
            self.assertIsInstance(json.load(open(w["file_path"], encoding="utf-8")), dict)

    def test_t2_billing_only(self):
        rc, _, err = self.stage2(rows=(BILLING,))
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual(len(b), 4)
        self.assertNotIn(("datasets", "jobs"), [(w["collection"], w["doc_id"]) for w in b])

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

    def test_t5_params(self):
        self.stage2()
        p = load(self.out, "dash-params.json")
        self.assertEqual(p["group_by"], "charge_class")
        self.assertIsNone(p["plan_included_minutes"])
        self.stage2(extra=["--plan-included-minutes", "3000"])
        self.assertEqual(load(self.out, "dash-params.json")["plan_included_minutes"], 3000)

    def test_t6_oversize_template(self):
        big = os.path.join(self.out, "big.html")
        with open(big, "w", encoding="utf-8") as f:
            f.write("a" * (256 * 1024 + 1))
        out = os.path.join(self.out, "o")
        rc, _, err = run(base(out) + ["--template", big])
        self.assertEqual(rc, 1)
        self.assertIn("256 KB", err)
        self.assertFalse(os.path.exists(os.path.join(out, "index.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "batch.json")))

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
        self.assertLess(out.index(os.path.abspath(BILLING)), out.index(os.path.abspath(JOBS)))

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

    def upd(self, ifv=("billing=3", "jobs=5"), urls=("/_blob/n0", "/_blob/n1")):
        a = base(self.out) + ["--update"]
        for u in urls:
            a += ["--asset-url", u]
        for v in ifv:
            a += ["--if-version", v]
        return run(a)

    def test_t8_update_changes_two_fields(self):
        run(base(self.out) + ["--asset-url", "/_blob/o0", "--asset-url", "/_blob/o1"])
        created = load(self.out, "dataset-billing.json")
        rc, _, err = self.upd()
        self.assertEqual(rc, 0, err)
        b = load(self.out, "batch.json")
        self.assertEqual([w["op"] for w in b], ["update", "update"])
        self.assertEqual([w["if_version"] for w in b], [3, 5])
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

    def test_t9_bad_if_version(self):
        for ifv in [("billing=3",), ("billing=3", "billing=4"), ("billing=x", "jobs=1"),
                    ("billing=0", "jobs=1"), ("billing=1", "other=1")]:
            rc, _, _ = self.upd(ifv=ifv)
            self.assertEqual(rc, 1, ifv)


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

    def test_t11_invalid(self):
        b = self.doc("b.json")
        self.fails(base(self.out, (b, JOBS)) + ["--asset-url", "/_blob/a"])
        for u in ["https://evil.example/x", "/_blob/../x", "/_blob/a?b"]:
            self.fails(base(self.out, (b,)) + ["--asset-url", u])
        self.fails(base(self.out, (self.doc("o.json", org="other-org"),)))
        self.fails(base(self.out, (b, self.doc("b2.json"))))
        self.fails(base(self.out, (JOBS,)))
        self.fails(base(self.out, (self.doc("v2.json", schema_version=2),)))
        self.fails(["--org", "a/b", "--out-dir", self.out, "--rows", b])
        big = os.path.join(self.d, "big.json")
        with open(big, "w") as f:
            f.write('{"pad": "' + "a" * (8 * 1024 * 1024) + '"}')
        self.fails(base(self.out, (big,)))

    def test_t13_no_network_imports(self):
        src = open(SCRIPT, encoding="utf-8").read()
        for m in ("subprocess", "urllib", "socket", "http"):
            self.assertIsNone(re.search(r"^\s*(import|from)\s+%s\b" % m, src, re.M), m)


class CollisionTests(unittest.TestCase):
    def test_rows_in_out_dir_rejected_without_change(self):
        with tempfile.TemporaryDirectory() as d:
            rows = os.path.join(d, "dataset-billing.json")
            with open(BILLING, "rb") as f:
                data = f.read()
            with open(rows, "wb") as f:
                f.write(data)
            for extra in ([], ["--asset-url", "/_blob/u0"]):
                rc, _, _ = run(["--org", "example-org", "--out-dir", d, "--rows", rows] + extra)
                self.assertEqual(rc, 1)
                with open(rows, "rb") as f:
                    self.assertEqual(f.read(), data)

    def test_update_stage1_removes_stale_batch(self):
        with tempfile.TemporaryDirectory() as d:
            bp = os.path.join(d, "batch.json")
            with open(bp, "w") as f:
                f.write("[]")
            rc, _, _ = run(base(d, (BILLING,)) + ["--update"])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(bp))


if __name__ == "__main__":
    unittest.main()
