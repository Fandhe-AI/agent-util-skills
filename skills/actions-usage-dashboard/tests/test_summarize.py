#!/usr/bin/env python3
"""summarize.py の回帰テスト。

役割: 内訳・group-by・移行候補・プラン解決・入力検証・20 行制約を固定する。
CLI 経由のテストは偽 gh を PATH に置き、実ネットワークへ出ないことを保証する。
summarize.test.mjs から `node --test` 経由で実行される。

fixture はすべて合成データ（org は acme-synth）。本リポは PUBLIC のため、実在の
非公開リポ名や金額を入れない。
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "summarize.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import summarize as sm  # noqa: E402

ORG = "acme-synth"


def row(day, repo, cls, minutes, quota=0.0, usd=0.0, vis="private", rt="ubuntu"):
    return {"day": day, "repo": repo, "visibility": vis, "sku": "Actions Linux",
            "runner_type": rt, "charge_class": cls, "minutes": minutes,
            "quota_minutes": quota, "net_usd": usd}


def doc(source, rows, org=ORG):
    return {"schema_version": 1, "source": source, "org": org, "rows": rows}


BILLING = [
    row("2026-09-30", "old-repo", "paid", 99, usd=9),
    row("2026-10-01", "priv-api", "paid", 100, usd=0.8),
    row("2026-10-02", "priv-api", "included", 500, quota=500),
    row("2026-10-03", "int-tools", "included", 300, quota=300, vis="internal"),
    row("2026-10-04", "int-tools", "paid", 10, usd=0.08, vis="internal"),
    row("2026-10-05", "pub-site", "public_free", 700, vis="public"),
    row("2026-10-06", "unk-repo", "unknown", 50),
    row("2026-10-07", "mac-repo", "paid", 20, usd=1.6, rt="macos"),
]
JOBS = [row("2026-10-08", "self-repo", "self_hosted", 40, rt="self-hosted")]


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def write(self, name, obj):
        p = os.path.join(self.td.name, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return p

    def fake_gh(self, plan="team", fail=False):
        bindir = os.path.join(self.td.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        log = os.path.join(self.td.name, "gh.log")
        p = os.path.join(bindir, "gh")
        body = '#!/bin/sh\necho "$@" >> "%s"\n' % log
        body += "exit 1\n" if fail else 'echo "%s"\n' % plan
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        return bindir, log

    def run_cli(self, args, bindir=None):
        env = {"PATH": (bindir + os.pathsep if bindir else "") + "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-I", "-B", SCRIPT] + args,
                              capture_output=True, text=True, env=env)

    def files(self, billing=BILLING, jobs=JOBS):
        a = ["--rows", self.write("b.json", doc("billing", billing))]
        if jobs is not None:
            a += ["--rows", self.write("j.json", doc("jobs", jobs))]
        return a


class SummaryTests(Base):
    def summary(self, plan="team", jobs=True, group_by="charge_class", top=5):
        org, rows, src = sm.load_rows(
            [self.write("b.json", doc("billing", BILLING))]
            + ([self.write("j.json", doc("jobs", JOBS))] if jobs else []))
        return sm.build_summary(org, rows, src, "2026-10", plan, None, group_by, top)

    def test_breakdown(self):
        s = self.summary("team")
        bd = s["breakdown"]
        self.assertEqual(bd["paid"], {"minutes": 130.0, "net_usd": 2.48})
        self.assertEqual(bd["included"]["quota_minutes"], 800.0)
        self.assertEqual(bd["included"]["included_minutes"], 3000)
        self.assertEqual(bd["included"]["remaining"], 2200.0)
        self.assertAlmostEqual(bd["included"]["ratio"], 800 / 3000, places=5)
        self.assertEqual(bd["public_free"]["minutes"], 700.0)
        self.assertEqual(bd["self_hosted"], {"minutes": 40.0, "available": True})
        self.assertEqual(bd["unknown"]["minutes"], 50.0)
        self.assertTrue(any("過小" in w for w in s["warnings"]))

    def test_no_jobs(self):
        s = self.summary(jobs=False)
        self.assertFalse(s["breakdown"]["self_hosted"]["available"])
        self.assertIn("セルフホスト: データなし", "\n".join(sm.render_text(s)))

    def test_over_quota(self):
        s = self.summary("free")
        inc = s["breakdown"]["included"]
        self.assertEqual(inc["remaining"], 1200.0)
        self.assertEqual(inc["over"], 0.0)
        rows = [row("2026-10-01", "r", "included", 2500, quota=2500)]
        bd = sm.breakdown(rows, 2000, False)
        self.assertEqual((bd["included"]["remaining"], bd["included"]["over"]), (0.0, 500.0))

    def test_plans(self):
        for name, n in (("free", 2000), ("Team", 3000), ("enterprise", 50000), ("business", 50000)):
            self.assertEqual(sm.included_for(name), n)
        self.assertIsNone(sm.included_for("mystery"))
        self.assertIsNone(self.summary("mystery")["plan"]["included_minutes"])

    def test_group_by_all(self):
        exp = {
            "charge_class": ["included", "public_free", "paid", "unknown"],
            "repo": ["pub-site", "priv-api", "int-tools", "unk-repo"],
            "runner_type": ["ubuntu", "self-hosted", "macos"],
            "visibility": ["private", "public", "internal"],
        }
        for key, order in exp.items():
            g = self.summary(group_by=key)["group_by"]
            self.assertEqual([e["key"] for e in g["top"]][:len(order)], order, key)

    def test_group_top_others_and_ties(self):
        rows = [row("2026-10-01", n, "paid", 10) for n in ("b", "a", "c")]
        g = sm.group_top(rows, "repo", 2)
        self.assertEqual([e["key"] for e in g["top"]], ["a", "b"])
        self.assertEqual(g["others"], 1)

    def test_candidates(self):
        c = self.summary()["candidates"]
        repos = [e["repo"] for e in c["top"]]
        # 有料あり: net_usd 降順 -> priv-api(0.8) は mac-repo(1.6) より後
        self.assertEqual(repos, ["mac-repo", "priv-api", "int-tools"])
        for excluded in ("pub-site", "unk-repo", "self-repo", "old-repo"):
            self.assertNotIn(excluded, repos)
        by = {e["repo"]: e for e in c["top"]}
        self.assertEqual(by["priv-api"]["last_used"], "2026-10-02")
        self.assertEqual(by["int-tools"]["last_used"], "2026-10-04")

    def test_candidates_included_only_after_paid(self):
        rows = [row("2026-10-01", "z", "included", 5, quota=5),
                row("2026-10-01", "y", "included", 9, quota=9),
                row("2026-10-02", "x", "paid", 1, usd=0.01)]
        c = sm.candidates(rows, rows, 5)
        self.assertEqual([e["repo"] for e in c["top"]], ["x", "y", "z"])

    def test_month_filter(self):
        org, rows, src = sm.load_rows([self.write("b.json", doc("billing", BILLING))])
        s = sm.build_summary(org, rows, src, "2026-09", "team", None, "repo", 5)
        self.assertEqual(s["breakdown"]["paid"]["minutes"], 99.0)
        s = sm.build_summary(org, rows, src, "2025-01", "team", None, "repo", 5)
        self.assertTrue(any("行なし" in w for w in s["warnings"]))


class CliTests(Base):
    def test_text_within_20_lines_worst_case(self):
        rows = []
        for i in range(12):
            rows.append(row("2026-10-%02d" % (i + 1), "repo-%02d" % i, "paid", 10 + i, usd=1 + i))
            rows.append(row("2026-10-%02d" % (i + 1), "inc-%02d" % i, "included", 5 + i, quota=5 + i))
        rows.append(row("2026-10-20", "u", "unknown", 3))
        rows.append(row("2026-10-20", "p", "public_free", 3, vis="public"))
        bindir, _ = self.fake_gh(plan="mystery")
        r = self.run_cli(self.files(rows) + ["--group-by", "repo"], bindir)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertLessEqual(len(lines), 20, r.stdout)
        self.assertIn("有料", lines[1])
        self.assertIn("不明", r.stdout)

    def test_default_month_is_latest_and_plan_from_gh(self):
        bindir, log = self.fake_gh(plan="team")
        r = self.run_cli(self.files(), bindir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2026-10", r.stdout.splitlines()[0])
        self.assertIn("3000", r.stdout.splitlines()[0])
        with open(log) as f:
            self.assertIn("orgs/acme-synth", f.read())

    def test_plan_flag_skips_gh(self):
        bindir, log = self.fake_gh()
        r = self.run_cli(self.files() + ["--plan", "free"], bindir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(log))
        self.assertIn("2000", r.stdout)

    def test_gh_failure_is_warning(self):
        bindir, _ = self.fake_gh(fail=True)
        r = self.run_cli(self.files(), bindir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("プラン取得に失敗", r.stdout)

    def test_json_matches_text(self):
        args = self.files() + ["--plan", "team"]
        j = json.loads(self.run_cli(args + ["--json"]).stdout)
        self.assertEqual(j["schema_version"], 1)
        self.assertEqual(j["breakdown"]["paid"]["net_usd"], 2.48)
        self.assertEqual(j["plan"], {"name": "team", "included_minutes": 3000})
        text = self.run_cli(args).stdout
        self.assertIn("%.2f USD" % j["breakdown"]["paid"]["net_usd"], text)

    def test_invalid_inputs_exit_1_without_gh(self):
        bindir, log = self.fake_gh()
        good = doc("billing", BILLING)
        cases = {
            "schema": dict(good, schema_version=2),
            "bad org": dict(good, org="../x"),
            "bad class": doc("billing", [row("2026-10-01", "r", "weird", 1)]),
            "neg": doc("billing", [row("2026-10-01", "r", "paid", -1)]),
            "nan": doc("billing", [row("2026-10-01", "r", "paid", float("nan"))]),
            "bool": doc("billing", [row("2026-10-01", "r", "paid", True)]),
            "bad day": doc("billing", [row("10/01", "r", "paid", 1)]),
        }
        for name, d in cases.items():
            r = self.run_cli(["--rows", self.write(name + ".json", d)], bindir)
            self.assertEqual(r.returncode, 1, name)
        b = self.write("b.json", good)
        j_other = self.write("jo.json", doc("jobs", JOBS, org="other-org"))
        dup = self.write("dup.json", good)
        missing = os.path.join(self.td.name, "nope.json")
        bad = os.path.join(self.td.name, "bad.json")
        with open(bad, "w") as f:
            f.write("{")
        for args in (["--rows", b, "--rows", j_other], ["--rows", b, "--rows", dup],
                     ["--rows", missing], ["--rows", bad], ["--rows", b, "--month", "2026-13"],
                     ["--rows", b, "--top", "0"], ["--rows", b, "--top", "x"]):
            r = self.run_cli(args, bindir)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertFalse(os.path.exists(log), "不正入力で gh が呼ばれた")

    def test_control_chars_stripped(self):
        rows = [row("2026-10-01", "evil\x1b[31mrepo", "paid", 5, usd=1)]
        r = self.run_cli(self.files(rows, None) + ["--plan", "team"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\x1b", r.stdout)


if __name__ == "__main__":
    unittest.main()
