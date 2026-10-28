#!/usr/bin/env python3
"""summarize.py の回帰テスト。

役割: 内訳・group-by・移行候補・プラン解決・入力検証・20 行制約を固定する。
CLI 経由のテストは偽 gh を PATH に置き、実ネットワークへ出ないことを保証する。
CI（ci.yml の skill-tests ジョブ）で `python3 -I -B -m unittest discover` により直接実行される。

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


def doc(source, rows, org=ORG, since=None, until=None, assume_current="absent"):
    if source == "billing":
        since, until = since or "2026-09", until or "2026-10"
    else:
        since, until = since or "2026-09-01", until or "2026-10-31"
    d = {"schema_version": 1, "source": source, "org": org, "since": since,
         "until": until, "rows": rows}
    if source == "billing" and assume_current != "absent":
        d["assume_current_visibility"] = assume_current
    return d


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


def inc_approx(bd):
    return bd["included"]["approximate"] and not bd["included"]["lower_bound"]


# 既定モード（--assume-current-visibility なし）の billing: 非有料分はすべて unknown で quota 0
DEFAULT_MODE = [
    row("2026-10-01", "priv-api", "paid", 100, usd=0.8),
    row("2026-10-02", "priv-api", "unknown", 500),
    row("2026-10-03", "pub-site", "unknown", 700, vis="public"),
]
NO_UNKNOWN = [r for r in BILLING if r["charge_class"] != "unknown"]


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

    def build(self, rows, flag, plan="team"):
        org, rd, src, w, est, ac, _ec = sm._load([self.write("b.json", doc("billing", rows, assume_current=flag))])
        return sm.build_summary(org, rd, src, "2026-10", plan, None, "repo", 5, w, today="2026-10-31",
                                assume_current=ac)

    def files(self, billing=BILLING, jobs=JOBS):
        a = ["--rows", self.write("b.json", doc("billing", billing))]
        if jobs is not None:
            a += ["--rows", self.write("j.json", doc("jobs", jobs))]
        return a


class SummaryTests(Base):
    def summary(self, plan="team", jobs=True, group_by="charge_class", top=5):
        org, rows, src, _w = sm.load_rows(
            [self.write("b.json", doc("billing", BILLING))]
            + ([self.write("j.json", doc("jobs", JOBS))] if jobs else []))
        return sm.build_summary(org, rows, src, "2026-10", plan, None, group_by, top)

    def test_breakdown(self):
        s = self.summary("team")
        bd = s["breakdown"]
        self.assertEqual(bd["paid"], {"minutes": 130.0, "net_usd": 2.48})
        self.assertEqual(bd["included"]["quota_minutes"], 800.0)
        self.assertEqual(bd["included"]["included_minutes"], 3000)
        self.assertIsNone(bd["included"]["remaining"])  # unknown ありは確定値にしない
        self.assertIsNone(bd["included"]["ratio"])
        self.assertEqual(bd["public_free"]["minutes"], 700.0)
        self.assertEqual(bd["self_hosted"], {"minutes": 40.0, "available": True, "estimated": False})
        self.assertEqual(bd["unknown"]["minutes"], 50.0)
        self.assertTrue(inc_approx(bd))  # included 行ありは分類済みなので概算
        self.assertFalse(any("下限" in w for w in s["warnings"]))

    def test_no_jobs(self):
        s = self.summary(jobs=False)
        self.assertFalse(s["breakdown"]["self_hosted"]["available"])
        self.assertIn("セルフホスト: データなし", "\n".join(sm.render_text(s)))

    def test_over_quota(self):
        s = self.summary("free")
        inc = s["breakdown"]["included"]
        self.assertIsNone(inc["remaining"])
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
        org, rows, src, _w = sm.load_rows([self.write("b.json", doc("billing", BILLING))])
        s = sm.build_summary(org, rows, src, "2026-09", "team", None, "repo", 5)
        self.assertEqual(s["breakdown"]["paid"]["minutes"], 99.0)
        s = sm.build_summary(org, rows, src, "2025-01", "team", None, "repo", 5)
        self.assertTrue(any("行なし" in w for w in s["warnings"]))


class WarningAndMissingBillingTests(Base):
    def test_source_warnings_carried_to_text_and_json(self):
        d = doc("jobs", JOBS)
        d["warnings"] = ["1,000 件上限で欠けた可能性: acme/x"]
        a = ["--rows", self.write("b.json", doc("billing", BILLING)),
             "--rows", self.write("j.json", d)]
        r = subprocess.run([sys.executable, "-I", SCRIPT] + a + ["--plan", "team", "--month", "2026-10"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1,000 件上限", r.stdout)
        r = subprocess.run([sys.executable, "-I", SCRIPT] + a + ["--plan", "team", "--month", "2026-10", "--json"],
                           capture_output=True, text=True)
        self.assertTrue(any("1,000 件上限" in w for w in json.loads(r.stdout)["warnings"]))

    def test_long_source_warning_keeps_trailing_identifiers(self):
        d = doc("jobs", JOBS)
        tail = "acme-synth/some-repo 2026-10-20"
        d["warnings"] = ["取得上限で欠けた可能性: " + "x" * 100 + " " + tail + "\x1b[31m"]
        _, _, _, ws = sm.load_rows([self.write("j.json", d)])
        self.assertIn(tail, ws[0])
        self.assertNotIn("\x1b", ws[0])

    def test_bad_warnings_rejected(self):
        d = doc("billing", BILLING)
        d["warnings"] = "x"
        with self.assertRaises(sm.FetchError):
            sm.load_rows([self.write("b.json", d)])

    def test_unknown_guidance_states_precondition(self):
        org, rows, src, w = sm.load_rows([self.write("b.json", doc("billing", DEFAULT_MODE))])
        s = sm.build_summary(org, rows, src, "2026-10", "team", None, "repo", 5, w)
        msg = " ".join(s["warnings"])
        self.assertIn("保証できる場合だけ", msg)
        self.assertIn("unknown のまま", msg)

    def test_unknown_makes_remaining_and_ratio_null(self):
        org, rows, src, w = sm.load_rows([self.write("b.json", doc("billing", BILLING))])
        s = sm.build_summary(org, rows, src, "2026-10", "team", None, "repo", 5, w, today="2026-10-31")
        inc = s["breakdown"]["included"]
        self.assertFalse(inc["lower_bound"])
        self.assertTrue(inc["approximate"])
        self.assertIsNone(inc["remaining"])
        self.assertIsNone(inc["ratio"])
        text = "\n".join(sm.render_text(s))
        self.assertIn("概算", text)
        self.assertNotIn("下限", text)
        self.assertNotIn("残り", text)
        self.assertNotIn("%、", text)

    def test_no_unknown_keeps_exact_remaining(self):
        rs = [r for r in BILLING if r["charge_class"] != "unknown"]
        org, rows, src, w = sm.load_rows([self.write("b.json", doc("billing", rs))])
        s = sm.build_summary(org, rows, src, "2026-10", "team", None, "repo", 5, w, today="2026-10-31")
        inc = s["breakdown"]["included"]
        self.assertFalse(inc["lower_bound"])
        self.assertTrue(inc["approximate"])
        self.assertIsNotNone(inc["remaining"])
        self.assertIsNotNone(inc["ratio"])
        text = "\n".join(sm.render_text(s))
        self.assertIn("残り 約", text)
        self.assertNotIn("下限", text)

    def test_dependabot_free_in_breakdown_not_candidate_nor_classified(self):
        # Issue #228: dependabot_free は内訳に出すが、移行候補にも分類済みの根拠にも数えない
        rows = [row("2026-10-01", "dep-only", "dependabot_free", 30),
                row("2026-10-01", "u", "unknown", 10)]
        s = self.build(rows, "absent")
        self.assertEqual(s["breakdown"]["dependabot_free"], {"minutes": 30.0})
        self.assertEqual(s["candidates"]["top"], [])
        inc = s["breakdown"]["included"]
        self.assertTrue(inc["lower_bound"])
        self.assertFalse(inc["approximate"])
        self.assertFalse(sm.is_classified(rows, "absent"))
        text = "\n".join(sm.render_text(s))
        self.assertIn("Dependabot（無料・含有枠外）: 30.0 分", text)
        s0 = self.build([row("2026-10-01", "u", "unknown", 10)], "absent")
        self.assertNotIn("Dependabot", "\n".join(sm.render_text(s0)))

    def test_default_mode_with_unknown_is_lower_bound(self):
        for flag in (False, "absent"):
            with self.subTest(flag=flag):
                s = self.build(DEFAULT_MODE, flag)
                inc = s["breakdown"]["included"]
                self.assertTrue(inc["lower_bound"])
                self.assertFalse(inc["approximate"])
                self.assertIsNone(inc["remaining"])
                self.assertIsNone(inc["ratio"])
                self.assertIn("下限", "\n".join(sm.render_text(s)))
                self.assertTrue(any("下限" in w for w in s["warnings"]))

    def test_flag_true_with_unknown_never_says_lower_bound(self):
        s = self.build(BILLING, True)
        inc = s["breakdown"]["included"]
        self.assertTrue(inc["approximate"])
        self.assertFalse(inc["lower_bound"])
        self.assertIsNone(inc["remaining"])
        self.assertIsNone(inc["ratio"])
        blob = "\n".join(sm.render_text(s)) + json.dumps(s, ensure_ascii=False)
        self.assertNotIn("下限", blob)
        self.assertNotIn("以上", blob)
        self.assertIn("概算", blob)
        self.assertIn("約", blob)

    def test_legacy_doc_with_included_rows_is_approximate(self):
        for flag in (False, "absent"):  # False は矛盾する文書。どちらも安全側
            with self.subTest(flag=flag):
                s = self.build(BILLING, flag)
                self.assertTrue(s["breakdown"]["included"]["approximate"])
                self.assertFalse(s["breakdown"]["included"]["lower_bound"])

    def test_classification_uses_whole_document_not_month(self):
        """旧形式の文書で、対象月が unknown だけでも別の月に分類済みの行があれば概算（雛形と同じ規則）。"""
        rows = [row("2026-09-02", "priv-api", "included", 500, quota=500),
                row("2026-10-02", "gone-repo", "unknown", 50)]
        s = self.build(rows, "absent")
        inc = s["breakdown"]["included"]
        self.assertTrue(inc["approximate"])
        self.assertFalse(inc["lower_bound"])
        self.assertNotIn("下限", "\n".join(sm.render_text(s)))

    def test_is_classified_rule(self):
        self.assertTrue(sm.is_classified([], True))
        self.assertFalse(sm.is_classified(DEFAULT_MODE, False))
        self.assertFalse(sm.is_classified(DEFAULT_MODE, None))
        self.assertTrue(sm.is_classified([row("2026-10-01", "p", "public_free", 1, vis="public")], None))
        self.assertFalse(sm.is_classified([row("2026-10-01", "p", "included", 0)], None))

    def test_unknown_plan_keeps_precision_marks(self):
        """含有分（プラン上限）が不明でも、消費値の確度（概算・下限）は表示する。"""
        for plan in ("mystery-plan",):
            with self.subTest(plan=plan):
                text = "\n".join(sm.render_text(self.build(NO_UNKNOWN, True, plan=plan)))
                self.assertIn("消費 約 800.0 分（概算）", text)
                self.assertIn("含有分不明", text)
                text = "\n".join(sm.render_text(self.build(DEFAULT_MODE, False, plan=plan)))
                self.assertIn("消費 0.0 分以上（下限。区分不明あり）", text)
                text = "\n".join(sm.render_text(self.build([row("2026-10-01", "r", "paid", 10, usd=0.08)], False, plan=plan)))
                self.assertIn("消費 0.0 分（実 0.0 分）/ 含有分不明", text)

    def test_flag_true_without_unknown_over_quota(self):
        rows = [row("2026-10-01", "r", "included", 2500, quota=2500)]
        s = self.build(rows, True, plan="free")
        text = "\n".join(sm.render_text(s))
        self.assertIn("超過 約 500.0 分", text)
        self.assertNotIn("以上", text)

    def test_no_unknown_no_classification_is_exact(self):
        rows = [row("2026-10-01", "r", "paid", 10, usd=0.08)]
        s = self.build(rows, False)
        inc = s["breakdown"]["included"]
        self.assertFalse(inc["approximate"])
        self.assertFalse(inc["lower_bound"])
        self.assertEqual(inc["ratio"], 0.0)
        text = "\n".join(sm.render_text(s))
        self.assertNotIn("約", text)
        self.assertNotIn("下限", text)

    def test_assume_current_type_checked(self):
        for bad in ("yes", 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(sm.FetchError):
                    sm._load([self.write("b.json", doc("billing", BILLING, assume_current=bad))])

    def test_cli_json_has_approximate(self):
        bindir, _ = self.fake_gh()
        p = self.write("b.json", doc("billing", BILLING, assume_current=True))
        r = self.run_cli(["--rows", p, "--month", "2026-10", "--json"], bindir)
        self.assertEqual(r.returncode, 0, r.stderr)
        inc = json.loads(r.stdout)["breakdown"]["included"]
        self.assertTrue(inc["approximate"])
        self.assertFalse(inc["lower_bound"])

    def test_jobs_only_shows_no_data(self):
        org, rows, src, w = sm.load_rows([self.write("j.json", doc("jobs", JOBS))])
        s = sm.build_summary(org, rows, src, "2026-10", "team", None, "repo", 5, w)
        self.assertIsNone(s["breakdown"]["paid"])
        self.assertIsNone(s["breakdown"]["included"])
        text = "\n".join(sm.render_text(s))
        self.assertIn("データなし（billing 未入力", text)
        self.assertIn("残量は不明", text)
        self.assertNotIn("残り", text)
        self.assertTrue(any("billing 未入力" in x for x in s["warnings"]))


class CoverageTests(Base):
    def build(self, billing_until, jobs_since, month="2026-10", jobs_until="2026-10-31"):
        paths = [self.write("b.json", doc("billing", [row("2026-09-02", "r", "paid", 5, usd=1)],
                                          since="2026-09", until=billing_until)),
                 self.write("j.json", doc("jobs", [row("2026-10-08", "s", "self_hosted", 40)],
                                          since=jobs_since, until=jobs_until))]
        org, rows, src, _w = sm.load_rows(paths)
        return sm.build_summary(org, rows, src, month, "team", None, "repo", 5)

    def test_month_outside_billing_is_unknown_not_zero(self):
        s = self.build("2026-09", "2026-10-01")
        bd = s["breakdown"]
        self.assertIsNone(bd["paid"])
        self.assertIsNone(bd["included"])
        self.assertEqual(bd["self_hosted"], {"minutes": 40.0, "available": True, "estimated": False})
        text = "\n".join(sm.render_text(s))
        self.assertNotIn("残り", text)
        self.assertTrue(any("取得期間" in w and "billing" in w for w in s["warnings"]))

    def test_month_outside_jobs_is_unavailable(self):
        s = self.build("2026-10", "2026-11-01", jobs_until="2026-11-30")
        self.assertFalse(s["breakdown"]["self_hosted"]["available"])
        self.assertTrue(any("jobs" in w and "取得期間" in w for w in s["warnings"]))

    def test_partial_month_warns(self):
        s = self.build("2026-10", "2026-10-10")
        self.assertTrue(s["breakdown"]["self_hosted"]["available"])
        self.assertTrue(any("途中から" in w for w in s["warnings"]))

    def test_tail_missing_warns_and_in_json(self):
        paths = [self.write("b.json", doc("billing", [row("2026-10-02", "r", "paid", 5, usd=1)],
                                          since="2026-10", until="2026-10")),
                 self.write("j.json", doc("jobs", [row("2026-10-08", "s", "self_hosted", 40)],
                                          since="2026-09-01", until="2026-10-10"))]
        org, rows, src, _w = sm.load_rows(paths)
        s = sm.build_summary(org, rows, src, "2026-10", "team", None, "repo", 5, today="2026-10-31")
        self.assertTrue(s["coverage"]["jobs"]["tail_missing"])
        self.assertFalse(s["coverage"]["jobs"]["head_missing"])
        self.assertFalse(s["coverage"]["billing"]["tail_missing"])
        self.assertEqual(s["coverage"]["jobs"]["until"], "2026-10-10")
        self.assertTrue(any("末尾まで届かない" in w and "2026-10-10" in w for w in s["warnings"]))

    def test_current_month_uses_today_as_expected_end(self):
        paths = [self.write("j.json", doc("jobs", [row("2026-10-08", "s", "self_hosted", 40)],
                                          since="2026-09-01", until="2026-10-10"))]
        org, rows, src, _w = sm.load_rows(paths)
        ok = sm.build_summary(org, rows, src, "2026-10", "team", None, "repo", 5, today="2026-10-10")
        self.assertFalse(ok["coverage"]["jobs"]["tail_missing"])
        ng = sm.build_summary(org, rows, src, "2026-10", "team", None, "repo", 5, today="2026-10-11")
        self.assertTrue(ng["coverage"]["jobs"]["tail_missing"])

    def test_missing_or_bad_range_rejected(self):
        for src, since, until in (("billing", "2026-9", "2026-10"), ("billing", "2026-10", "2026-09"),
                                  ("jobs", "2026-10", "2026-10-20"), ("jobs", "2026-10-20", "2026-10-01")):
            with self.assertRaises(sm.FetchError):
                sm.load_rows([self.write("x.json", doc(src, [], since=since, until=until))])
        for since, until in (("2026-99-01", "2026-10-31"), ("2026-02-31", "2026-10-31"),
                             ("2026-09-01", "2026-10-32")):
            with self.assertRaises(sm.FetchError):
                sm.load_rows([self.write("z.json", doc("jobs", [], since=since, until=until))])
        d = doc("billing", [])
        del d["since"]
        with self.assertRaises(sm.FetchError):
            sm.load_rows([self.write("y.json", d)])


class DayValidationTests(Base):
    def test_impossible_row_day_rejected(self):
        for day in ("2026-99-01", "2026-02-31"):
            with self.assertRaises(sm.FetchError):
                sm.load_rows([self.write("d.json", doc("billing", [row(day, "r", "paid", 1.0)]))])


class TrailingNewlineTests(Base):
    """Python の `$` は末尾の改行の手前にも一致する。検証用の正規表現は `\\Z` で終端する。"""

    def test_org_month_day_reject_trailing_newline(self):
        for name, ok in (("ORG_RE", "acme-synth"), ("MONTH_RE", "2026-09"), ("DAY_RE", "2026-09-01")):
            rx = getattr(sm, name)
            self.assertIsNotNone(rx.match(ok), name)
            self.assertIsNone(rx.match(ok + "\n"), name)


class CliTests(Base):
    def test_text_within_20_lines_worst_case(self):
        rows = []
        for i in range(12):
            rows.append(row("2026-10-%02d" % (i + 1), "repo-%02d" % i, "paid", 10 + i, usd=1 + i))
            rows.append(row("2026-10-%02d" % (i + 1), "inc-%02d" % i, "included", 5 + i, quota=5 + i))
        rows.append(row("2026-10-20", "u", "unknown", 3))
        rows.append(row("2026-10-20", "p", "public_free", 3, vis="public"))
        rows.append(row("2026-10-20", "d", "dependabot_free", 4))
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


class Hardening145Tests(Base):
    def test_plan_warning_is_sanitized(self):
        bindir = os.path.join(self.td.name, "bin")
        os.makedirs(bindir)
        p = os.path.join(bindir, "gh")
        with open(p, "w") as f:
            f.write("#!/bin/sh\nprintf '\\033]0;x\\007\\033[2J\\302\\233evil\\n' >&2\nexit 1\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        for extra in ([], ["--json"]):
            r = self.run_cli(self.files() + extra, bindir)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("プラン取得に失敗", r.stdout)
            for ch in ("\x1b", "\x07", "\x9b"):
                self.assertNotIn(ch, r.stdout)

    def test_rows_over_8mb_rejected(self):
        p = os.path.join(self.td.name, "big.json")
        with open(p, "w") as f:
            f.write(json.dumps(doc("billing", [])))
            f.write(" " * (8 * 1024 * 1024))
        bindir, log = self.fake_gh()
        r = self.run_cli(["--rows", p], bindir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("8 MB", r.stderr)
        self.assertFalse(os.path.exists(log))


class PlanNameSanitizeTests(Base):
    """plan.name は --json でも DEL・C1 が生で出るため、テキストと同じ safe() 規則で無害化する（Issue #159）。"""

    DIRTY = "te\x1b[2Jam\u009bx\x7fy\u0085z"

    def fake_plan_gh(self, printf_arg):
        bindir = os.path.join(self.td.name, "bin")
        os.makedirs(bindir)
        p = os.path.join(bindir, "gh")
        with open(p, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n'\nexit 0\n" % printf_arg)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        return bindir

    def test_plan_name_from_gh_is_sanitized(self):
        bindir = self.fake_plan_gh("te\\033[2Jam\\302\\233x\\177y\\302\\205z")
        for extra in ([], ["--json"]):
            r = self.run_cli(self.files() + extra, bindir)
            self.assertEqual(r.returncode, 0, r.stderr)
            for ch in ("\x1b", "\x7f", "\x9b", "\x85"):
                self.assertNotIn(ch, r.stdout)
        out = json.loads(r.stdout)["plan"]["name"]
        self.assertIsNone(sm.CTRL_RE.search(out))
        self.assertEqual(out, "te?[2Jam?x?y?z")

    def test_plan_flag_is_sanitized_in_json(self):
        r = self.run_cli(self.files() + ["--plan", self.DIRTY, "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        for ch in ("\x1b", "\x7f", "\x9b", "\x85"):
            self.assertNotIn(ch, r.stdout)

    def test_long_plan_name_truncated_in_json(self):
        r = self.run_cli(self.files() + ["--plan", "a" * 100, "--json"])
        name = json.loads(r.stdout)["plan"]["name"]
        self.assertEqual(len(name), sm.MAX_NAME_LEN)
        self.assertTrue(name.endswith("…"))

    def test_plan_none_stays_null(self):
        r = self.run_cli(self.files() + ["--json"], self.failing_gh())
        self.assertIsNone(json.loads(r.stdout)["plan"]["name"])

    def failing_gh(self):
        bindir = os.path.join(self.td.name, "bin")
        os.makedirs(bindir)
        p = os.path.join(bindir, "gh")
        with open(p, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        return bindir


class RowsStringSanitizeTests(Base):
    """Issue #170: group_by.top[].key / candidates.top[].repo を JSON でも無害化する。"""

    DIRTY = "ev\x1b[2Jil\u009bx\x7fy\u0085z"
    CLEAN = "ev?[2Jil?x?y?z"
    BAD = ("\x1b", "\x7f", "\x9b", "\x85")

    def run_both(self, rows, group_by):
        args = self.files(rows, None) + ["--plan", "team", "--month", "2026-10",
                                         "--group-by", group_by]
        outs = []
        for extra in ([], ["--json"]):
            r = self.run_cli(args + extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            for ch in self.BAD:
                self.assertNotIn(ch, r.stdout)
            outs.append(r.stdout)
        return outs[0], json.loads(outs[1])

    def test_repo_group_and_candidates_sanitized_text_and_json(self):
        rows = [row("2026-10-01", self.DIRTY, "paid", 5, usd=1),
                row("2026-10-02", "clean-repo", "paid", 3, usd=0.5)]
        _text, d = self.run_both(rows, "repo")
        keys = [e["key"] for e in d["group_by"]["top"]]
        repos = [e["repo"] for e in d["candidates"]["top"]]
        for v in keys + repos:
            self.assertIsNone(sm.CTRL_RE.search(v))
        self.assertIn(self.CLEAN, keys)
        self.assertIn(self.CLEAN, repos)

    def test_runner_type_and_visibility_keys_sanitized(self):
        rows = [row("2026-10-01", "r1", "paid", 5, usd=1, rt=self.DIRTY),
                row("2026-10-02", "r2", "paid", 4, usd=1, vis=self.DIRTY)]
        for gb, want in (("runner_type", self.CLEAN), ("visibility", self.CLEAN)):
            _text, d = self.run_both(rows, gb)
            keys = [e["key"] for e in d["group_by"]["top"]]
            self.assertIn(want, keys)
            for k in keys:
                self.assertIsNone(sm.CTRL_RE.search(k))

    def test_aggregation_uses_raw_keys(self):
        rows = [row("2026-10-01", "dup\x01", "paid", 5, usd=1),
                row("2026-10-03", "dup\x02", "paid", 7, usd=2),
                row("2026-10-04", self.DIRTY, "paid", 1, usd=0.1)]
        _text, d = self.run_both(rows, "repo")
        self.assertEqual(sorted(e["minutes"] for e in d["group_by"]["top"]
                                if e["key"] == "dup?"), [5.0, 7.0])
        self.assertEqual(len([e for e in d["candidates"]["top"] if e["repo"] == "dup?"]), 2)
        dirty = [e for e in d["candidates"]["top"] if e["repo"] == self.CLEAN]
        self.assertEqual(dirty[0]["last_used"], "2026-10-04")

    def test_long_repo_not_truncated_in_json(self):
        name = "a" * 100
        rows = [row("2026-10-01", name, "paid", 5, usd=1)]
        text, d = self.run_both(rows, "repo")
        self.assertEqual(d["candidates"]["top"][0]["repo"], name)
        self.assertEqual(d["group_by"]["top"][0]["key"], name)
        self.assertNotIn(name, text)
        self.assertIn("…", text)


NONE = object()  # estimate キーを付けない指定（None は JSON の null として付く）
EST = {"method": "sample_mean_per_workflow", "sample_runs": 3}
EST_FULL = {"method": "sample_mean_per_workflow", "sample_runs": 4, "budget_runs": 15, "zero_groups": 2, "zero_runs": 330}


class EstimateTests(Base):
    """jobs 文書の estimate を、セルフホスト行と JSON の estimated に写す（Issue #198）。"""

    def est_files(self, estimate=EST, billing=BILLING, jobs=JOBS, billing_estimate=None,
                  jobs_since=None, jobs_until=None):
        jd = doc("jobs", jobs, since=jobs_since, until=jobs_until)
        if estimate is not NONE:
            jd["estimate"] = estimate
        bd = doc("billing", billing)
        if billing_estimate is not None:
            bd["estimate"] = billing_estimate
        a = []
        if billing is not None:
            a += ["--rows", self.write("b.json", bd)]
        return a + ["--rows", self.write("j.json", jd)]

    def run_ok(self, args, extra=()):
        r = self.run_cli(args + ["--plan", "team", "--month", "2026-10"] + list(extra))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_estimate_marks_text_and_json(self):
        args = self.est_files()
        self.assertIn("セルフホスト: 40.0 分（推定）", self.run_ok(args))
        bd = json.loads(self.run_ok(args, ["--json"]))["breakdown"]
        self.assertIs(bd["self_hosted"]["estimated"], True)

    def test_no_estimate_is_not_marked(self):
        args = self.est_files(estimate=NONE)
        out = self.run_ok(args)
        self.assertIn("セルフホスト: 40.0 分", out)
        self.assertNotIn("推定", out)
        self.assertIs(json.loads(self.run_ok(args, ["--json"]))["breakdown"]["self_hosted"]["estimated"], False)

    def test_outside_jobs_period_is_not_marked(self):
        args = self.est_files(jobs_since="2026-11-01", jobs_until="2026-11-30")
        out = self.run_ok(args)
        self.assertIn("セルフホスト: データなし", out)
        self.assertNotIn("（推定）", out)
        self.assertIs(json.loads(self.run_ok(args, ["--json"]))["breakdown"]["self_hosted"]["estimated"], False)

    def test_jobs_only_branch_is_marked(self):
        args = self.est_files(billing=None)
        self.assertIn("セルフホスト: 40.0 分（推定）", self.run_ok(args))
        bd = json.loads(self.run_ok(args, ["--json"]))["breakdown"]
        self.assertIs(bd["self_hosted"]["estimated"], True)

    def test_invalid_estimate_exits_1(self):
        for bad in ([], "x", 3, None, True):
            r = self.run_cli(self.est_files(estimate=bad) + ["--plan", "team"])
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("estimate", r.stderr)

    def test_billing_estimate_is_ignored(self):
        args = self.est_files(estimate=NONE, billing_estimate=EST)
        self.assertNotIn("推定", self.run_ok(args))
        self.assertIs(json.loads(self.run_ok(args, ["--json"]))["breakdown"]["self_hosted"]["estimated"], False)

    def test_estimate_values_not_echoed(self):
        out = self.run_ok(self.est_files(estimate={"method": "SECRET_METHOD_X"}))
        self.assertNotIn("SECRET_METHOD_X", out)

    def test_text_within_20_lines_with_estimate(self):
        rows = []
        for i in range(12):
            rows.append(row("2026-10-%02d" % (i + 1), "repo-%02d" % i, "paid", 10 + i, usd=1 + i))
            rows.append(row("2026-10-%02d" % (i + 1), "inc-%02d" % i, "included", 5 + i, quota=5 + i))
        rows.append(row("2026-10-20", "u", "unknown", 3))
        out = self.run_ok(self.est_files(billing=rows), ["--group-by", "repo"])
        self.assertLessEqual(len(out.splitlines()), 20, out)
        self.assertIn("（推定）", out)


    def sh_json(self, args):
        return json.loads(self.run_ok(args, ["--json"]))["breakdown"]["self_hosted"]

    def test_counts_shown_in_text_and_json(self):
        """#219: 推定の予算と打ち切った組・run 数を要約に出す。"""
        args = self.est_files(estimate=EST_FULL)
        self.assertIn("セルフホスト: 40.0 分（推定）予算 15 run・0 分で打ち切り 2 組 / 330 run", self.run_ok(args))
        self.assertEqual(self.sh_json(args)["estimate_counts"],
                         {"budget_runs": 15, "zero_groups": 2, "zero_runs": 330})

    def test_zero_groups_zero_is_shown(self):
        out = self.run_ok(self.est_files(estimate=dict(EST_FULL, zero_groups=0, zero_runs=0)))
        self.assertIn("0 分で打ち切り 0 組 / 0 run", out)

    def test_missing_zero_runs_is_omitted(self):
        e = {k: v for k, v in EST_FULL.items() if k != "zero_runs"}
        args = self.est_files(estimate=e)
        out = self.run_ok(args)
        self.assertIn("（推定）予算 15 run・0 分で打ち切り 2 組\n", out + "\n")
        self.assertNotIn(" / 330", out)
        self.assertIsNone(self.sh_json(args)["estimate_counts"]["zero_runs"])

    def test_no_counts_keeps_plain_estimate(self):
        args = self.est_files()
        self.assertIn("セルフホスト: 40.0 分（推定）\n", self.run_ok(args) + "\n")
        self.assertNotIn("estimate_counts", self.sh_json(args))

    def test_counts_hidden_when_not_estimate(self):
        for args in (self.est_files(estimate=NONE),
                     self.est_files(estimate=EST_FULL, jobs_since="2026-11-01", jobs_until="2026-11-30"),
                     self.est_files(estimate=NONE, billing_estimate=EST_FULL)):
            out = self.run_ok(args)
            self.assertNotIn("予算", out)
            self.assertNotIn("打ち切り", out)
            self.assertNotIn("estimate_counts", self.sh_json(args))

    def test_counts_jobs_only_branch(self):
        args = self.est_files(estimate=EST_FULL, billing=None)
        self.assertIn("予算 15 run", self.run_ok(args))
        self.assertEqual(self.sh_json(args)["estimate_counts"]["zero_groups"], 2)

    def test_invalid_counts_exit_1(self):
        for k in ("budget_runs", "zero_groups", "zero_runs"):
            for bad in (True, -1, 1.5, "3", None, 10 ** 9 + 1):
                r = self.run_cli(self.est_files(estimate=dict(EST_FULL, **{k: bad})) + ["--plan", "team"])
                self.assertEqual(r.returncode, 1, (k, bad))
                self.assertIn("estimate." + k, r.stderr)

    def test_text_within_20_lines_with_counts(self):
        rows = []
        for i in range(12):
            rows.append(row("2026-10-%02d" % (i + 1), "repo-%02d" % i, "paid", 10 + i, usd=1 + i))
            rows.append(row("2026-10-%02d" % (i + 1), "inc-%02d" % i, "included", 5 + i, quota=5 + i))
        rows.append(row("2026-10-20", "u", "unknown", 3))
        out = self.run_ok(self.est_files(estimate=EST_FULL, billing=rows), ["--group-by", "repo"])
        self.assertLessEqual(len(out.splitlines()), 20, out)
        self.assertIn("打ち切り 2 組", out)


class SharedCommonTest(unittest.TestCase):
    """helper の定義が scripts/_common.py の 1 か所であることを固定する（Issue #141）。"""

    def test_helpers_are_shared_with_common(self):
        import _common
        expected = os.path.realpath(os.path.join(HERE, "..", "scripts", "_common.py"))
        self.assertEqual(os.path.realpath(_common.__file__), expected)
        for name in ('FetchError', 'ORG_RE', 'MONTH_RE', 'gh_api', 'DAY_RE',
                     'MAX_ROWS_BYTES', 'nonneg_num'):
            self.assertIs(getattr(sm, name), getattr(_common, name), name)

    def test_nonneg_num_rows_message(self):
        with self.assertRaises(sm.FetchError) as cm:
            sm.nonneg_num({"k": float("nan")}, "k", origin="rows")
        self.assertIn("rows の数値フィールド k が不正（有限の非負数ではない）", str(cm.exception))

    def test_nonneg_num_huge_int(self):
        # float の範囲を超える巨大な整数は OverflowError ではなく FetchError にする（Issue #189）
        for v in (10**400, -10**400):
            with self.assertRaises(sm.FetchError) as cm:
                sm.nonneg_num({"k": v}, "k", origin="rows")
            self.assertIn("rows の数値フィールド k が不正（有限の非負数ではない）", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
