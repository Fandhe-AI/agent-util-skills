#!/usr/bin/env python3
"""fetch_jobs.py の回帰テスト。純粋関数の単体テストと、gh スタブを使った CLI の E2E を持つ。

fixture はすべて合成データ（org は acme-synth）。gh スタブは PATH の先頭に置いた
Python スクリプトで、呼び出しを calls.log に記録し、cfg.json の内容から応答を返す。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "fetch_jobs.py")
spec = importlib.util.spec_from_file_location("fetch_jobs", SCRIPT)
fj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fj)

ORG = "acme-synth"

STUB = r'''#!%(py)s
import json, os, sys
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(" ".join(args) + "\n")
cfg = json.load(open(os.path.join(d, "cfg.json")))
log = open(os.path.join(d, "calls.log")).read().splitlines()
n_api = len([l for l in log if not l.startswith("api rate_limit")])
fail_after = cfg.get("fail_after")
low_after = cfg.get("low_after")
q = {}
a = args[1:]
i = 0
path = None
while i < len(a):
    if a[i] == "-f":
        k, v = a[i + 1].split("=", 1); q[k] = v; i += 2
    elif a[i] == "-X":
        i += 2
    elif a[i] == "--paginate":
        i += 1
    else:
        path = a[i]; i += 1
if path == "rate_limit":
    rem = 5000
    if low_after is not None and n_api >= low_after:
        rem = cfg.get("low_value", 3)
    print(json.dumps({"resources": {"core": {"remaining": rem, "reset": 1}}}))
    sys.exit(0)
if fail_after is not None and n_api > fail_after:
    sys.stderr.write("HTTP 500 boom\n"); sys.exit(1)
if path.startswith("orgs/"):
    repos = cfg["repos"]
    print(json.dumps(repos[:2])); print(json.dumps(repos[2:]))
    sys.exit(0)
parts = path.split("?")[0].split("/")
page = int(q.get("page", "1")); per = int(q.get("per_page", "30"))
if parts[-1] == "runs":
    runs = cfg["runs"].get(parts[2], [])
    c = q["created"]
    if c.startswith(">="):
        lo, hi = c[2:], "9999-12-31"
    else:
        lo, hi = c.split("..")
    runs = [r for r in runs if lo <= r["created_at"][:10] <= hi]
    capped = runs[:1000]
    print(json.dumps({"total_count": len(runs), "workflow_runs": capped[(page-1)*per:page*per]}))
elif parts[-1] == "jobs":
    jobs = cfg["jobs"].get(parts[-2], [])
    print(json.dumps({"total_count": len(jobs), "jobs": jobs[(page-1)*per:page*per]}))
else:
    sys.stderr.write("unknown " + path + "\n"); sys.exit(1)
'''


def job(jid, labels, group, start, minutes, status="completed"):
    s = datetime.fromisoformat(start)
    c = (s + timedelta(minutes=minutes)).isoformat() if status == "completed" else None
    return {"id": jid, "labels": labels, "runner_group_name": group, "status": status,
            "started_at": start, "completed_at": c}


def run(rid, created="2026-10-10T00:00:00Z", status="completed", attempt=1):
    return {"id": rid, "status": status, "run_attempt": attempt, "created_at": created}


def base_cfg():
    return {
        "repos": [
            {"name": "pub-site", "visibility": "public", "archived": False},
            {"name": "priv-api", "visibility": "private", "archived": False},
            {"name": "old-archived", "visibility": "private", "archived": True},
        ],
        "runs": {"pub-site": [run(1)], "priv-api": [run(2), run(3, status="in_progress")],
                 "old-archived": [run(9)]},
        "jobs": {
            "1": [job(101, ["self-hosted", "linux"], "build-farm", "2026-10-10T10:00:00+00:00", 10),
                  job(102, ["ubuntu-latest"], "GitHub Actions", "2026-10-10T10:00:00+00:00", 99)],
            "2": [job(201, ["Self-Hosted"], "build-farm", "2026-10-10T23:30:00+00:00", 90),
                  job(202, ["x"], "build-farm", "2026-10-10T10:00:00+00:00", 5, status="in_progress")],
            "3": [job(301, ["self-hosted"], "build-farm", "2026-10-10T10:00:00+00:00", 7)],
        },
    }


class TrailingNewlineTest(unittest.TestCase):
    """Python の `$` は末尾の改行の手前にも一致する。検証用の正規表現は `\\Z` で終端する。"""

    def test_org_repo_date_reject_trailing_newline(self):
        for name, ok in (("ORG_RE", "acme-synth"), ("REPO_RE", "repo.name_1"), ("DATE_RE", "2026-09-01")):
            rx = getattr(fj, name)
            self.assertIsNotNone(rx.match(ok), name)
            self.assertIsNone(rx.match(ok + "\n"), name)


class Pure(unittest.TestCase):
    def test_is_self_hosted(self):
        f = fj.is_self_hosted
        self.assertTrue(f({"labels": ["self-hosted", "linux"], "runner_group_name": "GitHub Actions"}))
        self.assertTrue(f({"labels": ["Self-Hosted"], "runner_group_name": None}))
        # 独自グループの GitHub ホスト larger runner は self-hosted ラベルを持たず、二重計上しない
        self.assertFalse(f({"labels": ["a"], "runner_group_name": "build-farm"}))
        self.assertFalse(f({"labels": ["ubuntu-latest"], "runner_group_name": "GitHub Actions"}))
        self.assertFalse(f({"labels": ["ubuntu-latest"], "runner_group_name": None}))
        self.assertFalse(f({"labels": ["ubuntu-latest"], "runner_group_name": ""}))

    def test_unfinished_excluded(self):
        t = "2026-10-10T10:00:00+00:00"
        base = job(1, ["self-hosted"], "g", t, 5)
        self.assertIsNotNone(fj.job_window(base))
        for patch in ({"status": "in_progress"}, {"completed_at": None}, {"started_at": None},
                      {"completed_at": "2026-10-10T09:00:00+00:00"}, {"started_at": "garbage"}):
            self.assertIsNone(fj.job_window(dict(base, **patch)), patch)

    def test_aggregate_tz_dedupe_zero(self):
        jobs = [
            job(1, ["self-hosted"], "g", "2026-10-10T08:30:00+09:00", 1.5),  # UTC では 10-09 の 23:30（日付が UTC へ寄る）
            job(1, ["self-hosted"], "g", "2026-10-10T08:30:00+09:00", 1.5),  # 重複
            job(2, ["self-hosted"], "g", "2026-10-09T23:30:00+00:00", 90),   # 日またぎは開始日
            job(3, ["self-hosted"], None, "2026-10-09T23:00:00+00:00", 0),   # 0 分
            job(4, ["self-hosted"], None, "2026-10-09T23:00:00+00:00", 2),
        ]
        agg, hosted, unfinished = fj.aggregate_jobs(jobs)
        self.assertEqual(agg, [{"day": "2026-10-09", "runner_group": "(unknown)", "minutes": 2.0},
                               {"day": "2026-10-09", "runner_group": "g", "minutes": 91.5}])
        self.assertEqual((hosted, unfinished), (0, 0))

    def test_build_rows_columns_and_order(self):
        ents = [{"repo": "b", "agg": [{"day": "2026-10-02", "runner_group": "g", "minutes": 1.0}]},
                {"repo": "a", "agg": [{"day": "2026-10-02", "runner_group": "g", "minutes": 2.0},
                                      {"day": "2026-09-01", "runner_group": "g", "minutes": 9.0},
                                      {"day": "2026-10-03", "runner_group": "g", "minutes": 0.0}]}]
        rows = fj.build_rows(ents, {"a": "public"}, "2026-10-01")
        self.assertEqual([(r["day"], r["repo"]) for r in rows], [("2026-10-02", "a"), ("2026-10-02", "b")])
        self.assertEqual(set(rows[0]), {"day", "repo", "visibility", "sku", "runner_type", "charge_class",
                                        "runner_group", "minutes", "quota_minutes", "billable_minutes",
                                        "gross_usd", "discount_usd", "net_usd"})
        self.assertEqual(rows[0]["charge_class"], "self_hosted")
        self.assertEqual(rows[0]["visibility"], "public")
        self.assertEqual(rows[0]["net_usd"], 0.0)

    def test_validation(self):
        with self.assertRaises(fj.InvalidJob):
            fj.aggregate_jobs([{"id": "x", "labels": []}])
        with self.assertRaises(fj.FetchError):
            fj.aggregate_jobs([{"id": 1, "labels": "self-hosted"}])
        for bad in (True, "1", 0, -1):
            with self.assertRaises(fj.FetchError):
                fj._run_key({"id": 1, "run_attempt": bad})
        with self.assertRaises(fj.FetchError):
            fj._run_key({"id": True})


class Cli(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fj-test-")
        self.stub = os.path.join(self.root, "stub")
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.stub)
        os.makedirs(self.work)
        gh = os.path.join(self.stub, "gh")
        with open(gh, "w") as f:
            f.write(STUB % {"py": sys.executable})
        os.chmod(gh, 0o755)
        self.cfg = base_cfg()
        self.write_cfg()
        self.out = os.path.join(self.work, "jobs.json")
        self.cache = os.path.join(self.work, "cache")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_cfg(self):
        with open(os.path.join(self.stub, "cfg.json"), "w") as f:
            json.dump(self.cfg, f)

    def calls(self):
        p = os.path.join(self.stub, "calls.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def reset_calls(self):
        p = os.path.join(self.stub, "calls.log")
        if os.path.exists(p):
            os.unlink(p)

    def run_cli(self, extra=None, out=None, cache=None, org=ORG, since="2026-10-01"):
        env = dict(os.environ, PATH=self.stub + os.pathsep + os.environ.get("PATH", ""), STUB_DIR=self.stub)
        cmd = [sys.executable, "-I", "-B", SCRIPT, "--org", org, "--since", since,
               "--out", out or self.out, "--cache", cache or self.cache] + (extra or [])
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=self.work)

    def rows(self, path=None):
        with open(path or self.out) as f:
            return json.load(f)["rows"]

    def test_first_run(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.rows()
        self.assertEqual([(x["day"], x["repo"], x["runner_group"], x["minutes"]) for x in rows],
                         [("2026-10-10", "priv-api", "build-farm", 90.0),
                          ("2026-10-10", "pub-site", "build-farm", 10.0)])
        # run 2 の job 201 は 23:30 開始 90 分で開始日に計上、202 は未完了で除外。run 3 は保留
        self.assertEqual({(x["repo"], x["minutes"]) for x in rows}, {("priv-api", 90.0), ("pub-site", 10.0)})
        self.assertFalse(any("old-archived" in c or "/9/" in c for c in self.calls()))
        lines = r.stdout.splitlines()
        self.assertLessEqual(len(lines), 10)
        for name in ("pub-site", "priv-api", "old-archived"):
            self.assertNotIn(name, r.stdout)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".")], [])

    def test_incremental_and_pending_and_retry(self):
        self.assertEqual(self.run_cli().returncode, 0)
        self.reset_calls()
        self.cfg["runs"]["pub-site"].append(run(4))
        self.cfg["jobs"]["4"] = [job(401, ["self-hosted"], "g2", "2026-10-11T01:00:00+00:00", 4)]
        self.cfg["jobs"]["3"] = [job(301, ["self-hosted"], "build-farm", "2026-10-10T10:00:00+00:00", 7)]
        self.cfg["runs"]["priv-api"][1]["status"] = "completed"  # 保留していた run 3 が完了
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        jobs_calls = [c for c in self.calls() if c.endswith("/jobs") or "/jobs " in c]
        ids = sorted(c.split("/runs/")[1].split("/")[0] for c in jobs_calls)
        self.assertEqual(ids, ["3", "4"])  # 既知の run 1・2 は取らない
        self.assertTrue(any("actions/runs " in c or c.endswith("actions/runs") or "actions/runs -f" in c
                            for c in self.calls()))
        total = sum(x["minutes"] for x in self.rows())
        self.assertEqual(total, 10 + 90 + 7 + 4)
        # 再実行（attempt 増）で取り直す
        self.reset_calls()
        self.cfg["runs"]["pub-site"][0]["run_attempt"] = 2
        self.cfg["jobs"]["1"] = [job(103, ["self-hosted"], "build-farm", "2026-10-10T10:00:00+00:00", 20)]
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), 20 + 90 + 7 + 4)

    def test_rerun_pending_excluded_and_invalid_job_skipped(self):
        self.assertEqual(self.run_cli().returncode, 0)
        before = sum(x["minutes"] for x in self.rows())
        # run 1 が再実行中（attempt 増・未完了）: 完了済みの旧 attempt の分は集計に残す
        self.cfg["runs"]["pub-site"][0]["run_attempt"] = 2
        self.cfg["runs"]["pub-site"][0]["status"] = "in_progress"
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), before)
        # 不正な job を含む run はスキップし、全体は成功する
        self.cfg["runs"]["pub-site"][0]["status"] = "completed"
        self.cfg["jobs"]["1"] = [{"id": "bad", "labels": []}]
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out) as f:
            self.assertTrue(any("スキップ" in w for w in json.load(f)["warnings"]))

    def test_rate_limit_resume_matches_uninterrupted(self):
        # 比較用: 中断なし
        ref_out = os.path.join(self.root, "ref.json")
        self.assertEqual(self.run_cli(out=ref_out, cache=os.path.join(self.root, "refcache")).returncode, 0)
        self.reset_calls()
        # 本番: 途中で残量不足
        self.cfg["low_after"] = 4  # repos 1 + runs 2 + jobs 1 回の後で残量不足にする
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.out))
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            first = json.load(f)["runs"]
        self.assertTrue(len(first) >= 1)
        self.reset_calls()
        self.cfg["low_after"] = None
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        for rid in first:
            self.assertFalse(any("/runs/%s/jobs" % rid in c for c in self.calls()))
        self.assertEqual(self.rows(), self.rows(ref_out))

    def test_jobs_requested_with_all_filter(self):
        self.assertEqual(self.run_cli().returncode, 0)
        jobs_calls = [c for c in self.calls() if "/jobs" in c]
        self.assertTrue(jobs_calls)
        self.assertTrue(all("filter=all" in c for c in jobs_calls))

    def test_cache_keeps_run_with_in_period_job(self):
        # 期間前（10/09）に作られた run が、期間内（10/10）開始の job を持つ
        self.cfg["runs"]["pub-site"] = [run(1, created="2026-10-09T23:00:00Z")]
        self.cfg["runs"]["priv-api"] = []
        self.write_cfg()
        self.assertEqual(self.run_cli(since="2026-10-01").returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), 10)
        for _ in range(2):  # 期間を狭めても、繰り返しても分数が減らない
            self.assertEqual(self.run_cli(since="2026-10-10").returncode, 0)
            self.assertEqual(sum(x["minutes"] for x in self.rows()), 10)

    def test_renamed_repo_keeps_cached_minutes(self):
        self.assertEqual(self.run_cli().returncode, 0)
        before = sum(x["minutes"] for x in self.rows())
        self.cfg["repos"][0]["name"] = "pub-site-renamed"
        self.cfg["runs"]["pub-site-renamed"] = self.cfg["runs"].pop("pub-site")
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        rows = self.rows()
        self.assertEqual(sum(x["minutes"] for x in rows), before)
        self.assertIn("pub-site-renamed", {x["repo"] for x in rows})

    def test_deleted_run_stays_in_output(self):
        self.assertEqual(self.run_cli().returncode, 0)
        before = sum(x["minutes"] for x in self.rows())
        self.cfg["runs"]["pub-site"] = []  # GitHub 側で run が一覧から消えた
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), before)

    def test_listing_resumes_after_rate_limit(self):
        self.cfg["low_after"] = 3  # repos 1 + runs 1 回の後で残量不足にする
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            saved = json.load(f)
        self.assertTrue(saved["listing"]["repos"])
        done = list(saved["listing"]["repos"])
        self.reset_calls()
        self.cfg["low_after"] = None
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        for repo in done:
            had_pending = any(i["status"] != "completed" for i in saved["listing"]["repos"][repo])
            relisted = any("repos/%s/%s/actions/runs " % (ORG, repo) in c for c in self.calls())
            # 未完了 run を含むリポジトリだけ一覧を取り直し、完了済みのみなら再利用する
            self.assertEqual(relisted, had_pending)
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            self.assertNotIn("listing", json.load(f))

    def test_paging(self):
        self.cfg["runs"] = {"pub-site": [run(i) for i in range(1, 121)]}
        self.cfg["repos"] = self.cfg["repos"][:1]
        self.cfg["jobs"] = {str(i): [] for i in range(1, 121)}
        self.cfg["jobs"]["5"] = [job(1000 + i, ["self-hosted"], "g", "2026-10-10T10:00:00+00:00", 1)
                                 for i in range(150)]
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rows()[0]["minutes"], 150.0)
        self.assertTrue(any("page=2" in c and "/runs/5/jobs" in c for c in self.calls()))
        self.assertTrue(any("actions/runs " in c and "page=2" in c for c in self.calls()))

    def test_search_cap_split(self):
        # 1,000 件上限を超える期間は二分して取り直し、欠けも重複もないことを一覧件数で確かめる
        runs = []
        rid = 1
        for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
            for _ in range(400):
                runs.append(run(rid, created=day + "T00:00:00Z", status="queued"))
                rid += 1
        self.cfg["repos"] = self.cfg["repos"][:1]
        self.cfg["runs"] = {"pub-site": runs}
        self.cfg["jobs"] = {}
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("一覧 1200", r.stdout)
        self.assertNotIn("警告", r.stdout)

    def test_gh_failure_keeps_cache_no_out(self):
        self.cfg["fail_after"] = 4
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("gh api 失敗", r.stderr)

    def test_bad_cache_stops(self):
        os.makedirs(self.cache)
        p = os.path.join(self.cache, "jobs-cache-%s.json" % ORG)
        for body in ("{not json", json.dumps({"schema_version": 1, "org": "other", "runs": {}}),
                     json.dumps({"schema_version": 1, "org": ORG, "runs": {"1": {
                         "repo": "pub-site", "attempt": 1, "created_at": "",
                         "agg": [{"day": "2026-10-10", "runner_group": "g", "minutes": -1}]}}}),
                     ) + tuple(json.dumps({"schema_version": 1, "org": ORG, "runs": {"1": dict(
                         {"repo": "pub-site", "attempt": 1, "agg": []}, **extra)}})
                         for extra in ({}, {"created_at": None}, {"created_at": 20261010})):
            with open(p, "w") as f:
                f.write(body)
            r = self.run_cli()
            self.assertEqual(r.returncode, 1, body)
            self.assertIn("キャッシュが壊れている", r.stderr)
            with open(p) as f:
                self.assertEqual(f.read(), body)
            self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.calls(), [])

    def test_bad_args_never_call_gh(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        a_file = os.path.join(self.work, "afile")
        open(a_file, "w").close()
        cases = [
            dict(org="bad_org!"), dict(since="2026-10-1"), dict(since="2026-13-01"), dict(since=future),
            dict(out=self.work), dict(out=os.path.join(self.work, "nodir", "x.json")), dict(cache=a_file),
        ]
        for kw in cases:
            r = self.run_cli(**kw)
            self.assertEqual(r.returncode, 1, kw)
            self.assertEqual(self.calls(), [], kw)
        self.assertEqual(os.listdir(self.work), ["afile"])
        self.assertFalse(os.path.exists(self.cache))
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
