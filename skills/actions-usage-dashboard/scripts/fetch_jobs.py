#!/usr/bin/env python3
"""Actions jobs API からセルフホストランナーの日次行を作る。

役割と境界:
  actions-usage-dashboard の 2 つ目のデータ源。billing usage API（fetch_billing.py）には
  セルフホストランナーの実行時間が含まれないため、org の全リポジトリ（アーカイブ除く）の
  Actions runs / jobs API からセルフホストの job だけを拾い、
  「日（UTC）x リポジトリ x ランナーグループ」の行へ集計して JSON に書く。
  GitHub ホストの job は行にしない（分数は billing 側を正とし、二重計上を避ける）。
  GitHub への支払いが発生しないため charge_class は self_hosted で固定する。

呼び出し元: summarize.py / build_dashboard.py が fetch_billing.py の出力と合わせて読む。
  行の列は fetch_billing.py の row に揃え、セルフホスト固有の runner_group を足している。
  列名や区分を変えるときは fetch_billing.py と後続スクリプトを同時に更新する。

出力スキーマ（schema_version 1）:
  {schema_version, source: "jobs", org, since, until, generated_at, warnings, rows}
  row = day / repo / visibility / sku / runner_type / charge_class / runner_group /
        minutes / quota_minutes / billable_minutes / gross_usd / discount_usd / net_usd
  金額系と quota_minutes は 0。day は job の started_at の UTC 日。

増分キャッシュ: <--cache>/jobs-cache-<org>.json
  {schema_version, org, runs: {"<run_id>": {repo, attempt, created_at, agg: [{day, runner_group, minutes}]}}}
  jobs を全ページ取り終えた「完了済み run」だけを run 単位で登録する。未完了の run は
  後から job が完了するため登録せず、次回の一覧で再び候補にする。run_attempt が増えた
  （再実行）ときは取り直す。キャッシュは非信頼入力として検証し、壊れていれば止まる。

終了コード: 0 成功 / 1 失敗 / 3 レート制限の残量不足で中断（キャッシュ保存済み。
  同じコマンドで再実行すると続きから取る。中断時と失敗時は --out を書かない）。

注意: GitHub ホストの larger runner が独自のランナーグループに入っている場合、判定式
  （labels に self-hosted、または runner_group_name が GitHub Actions 以外）では
  セルフホスト扱いになり billing と二重計上し得る。判定は is_self_hosted に集約している。

helper の複製: gh_api / atomic_write_json / ORG_RE / FetchError は fetch_billing.py と
  同じ実装を複製している（python3 -I 起動ではスクリプト隣接の import ができず、各スクリプトを
  自己完結にするため）。直すときは両方を直す。

アクセス: gh api 経由のみ（読み取り専用）。トークンは扱わない。標準ライブラリだけで動く。
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

SCHEMA_VERSION = 1
GH_TIMEOUT = 120
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOSTED_GROUP = "GitHub Actions"
SELF_HOSTED_LABEL = "self-hosted"
UNKNOWN_GROUP = "(unknown)"
RUNS_SEARCH_CAP = 1000
PER_PAGE = 100
MAX_JOB_PAGES = 100
DEFAULT_MIN_REMAINING = 200
DEFAULT_RATE_CHECK_EVERY = 25
EXIT_RATE_LIMITED = 3
ZERO_COLS = ("quota_minutes", "billable_minutes", "gross_usd", "discount_usd", "net_usd")


class FetchError(Exception):
    """取得・検証の失敗。main が日本語 1 行にして終了コード 1 で終える。"""


class InvalidJob(FetchError):
    """job の要素が不正。main は該当 run だけ警告してスキップする（全体は止めない）。"""


class RateLimited(Exception):
    """レート制限の残量が閾値を下回った。main が保存して終了コード 3 で終える。"""

    def __init__(self, remaining, reset):
        super().__init__("rate limited")
        self.remaining, self.reset = remaining, reset


# ---- 純粋関数（gh に依存しない） ----

def is_self_hosted(job):
    labels = job.get("labels")
    if any(isinstance(x, str) and x.lower() == SELF_HOSTED_LABEL for x in labels):
        return True
    g = job.get("runner_group_name")
    return isinstance(g, str) and g.strip() != "" and g != HOSTED_GROUP


def _parse_ts(v):
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def job_window(job):
    """完了済み job の (開始 UTC, 分数) を返す。未完了・時刻不正・負の所要時間は None。"""
    if job.get("status") != "completed":
        return None
    s, c = _parse_ts(job.get("started_at")), _parse_ts(job.get("completed_at"))
    if s is None or c is None or c < s:
        return None
    return s, (c - s).total_seconds() / 60.0


def validate_job(job):
    if not isinstance(job, dict):
        raise InvalidJob("job の要素が不正")
    jid = job.get("id")
    if isinstance(jid, bool) or not isinstance(jid, int):
        raise InvalidJob("job の id が整数ではない")
    labels = job.get("labels")
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise InvalidJob("job の labels が文字列の配列ではない")
    return job


def aggregate_jobs(jobs):
    """jobs を (agg, hosted, unfinished) にする。agg は [{day, runner_group, minutes}]。job id で重複除去。"""
    seen, acc, hosted, unfinished = set(), {}, 0, 0
    for job in jobs:
        validate_job(job)
        if job["id"] in seen:
            continue
        seen.add(job["id"])
        if not is_self_hosted(job):
            hosted += 1
            continue
        w = job_window(job)
        if w is None:
            unfinished += 1
            continue
        start, minutes = w
        g = job.get("runner_group_name")
        group = g if isinstance(g, str) and g.strip() else UNKNOWN_GROUP
        key = (start.date().isoformat(), group)
        acc[key] = acc.get(key, 0.0) + minutes
    agg = [{"day": d, "runner_group": g, "minutes": round(m, 6)} for (d, g), m in sorted(acc.items())]
    return agg, hosted, unfinished


def build_rows(entries, visibility, since):
    """run エントリ（{repo, agg}）群から (day, repo, runner_group) 単位の行を作る。"""
    acc = {}
    for e in entries:
        for a in e["agg"]:
            if a["day"] < since:
                continue
            k = (a["day"], e["repo"], a["runner_group"])
            acc[k] = acc.get(k, 0.0) + a["minutes"]
    rows = []
    for (day, repo, group), m in sorted(acc.items()):
        m = round(m, 6)
        if m <= 0:
            continue
        row = {"day": day, "repo": repo, "visibility": visibility.get(repo, "unknown"),
               "sku": "self-hosted", "runner_type": "self-hosted", "charge_class": "self_hosted",
               "runner_group": group, "minutes": m}
        row.update({c: 0.0 for c in ZERO_COLS})
        rows.append(row)
    return rows


# ---- gh 層 ----

def gh_api(args):
    try:
        r = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                           timeout=GH_TIMEOUT, shell=False)
    except FileNotFoundError:
        raise FetchError("gh コマンドが見つからない")
    except subprocess.TimeoutExpired:
        raise FetchError("gh api が %d 秒でタイムアウト" % GH_TIMEOUT)
    if r.returncode != 0:
        lines = (r.stderr or "").strip().splitlines()
        raise FetchError("gh api 失敗: %s" % (lines[0][:200] if lines else "exit %d" % r.returncode))
    return r.stdout


def _json(text, what):
    try:
        return json.loads(text)
    except ValueError:
        raise FetchError("%s の応答を JSON として解析できない" % what)


def parse_concatenated_json(text):
    """gh --paginate が連結した JSON 配列（[..][..]）を 1 本のリストにする。"""
    dec, i, n, out, pages = json.JSONDecoder(), 0, len(text), [], 0
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            raise FetchError("リポジトリ一覧の JSON を解析できない")
        if not isinstance(obj, list):
            raise FetchError("リポジトリ一覧が配列ではない")
        out.extend(obj)
        pages += 1
    return out, pages


class GhClient:
    """API 呼び出し数の計数とレート制限の確認を担う。runs / jobs は page を手で回す。"""

    def __init__(self, min_remaining, check_every):
        self.counts = {"repos": 0, "runs": 0, "jobs": 0}
        self.rate_checks = 0
        self.min_remaining, self.check_every = min_remaining, check_every
        self._since_check = None  # None は未確認

    def total(self):
        return sum(self.counts.values())

    def _call(self, kind, args, n=1):
        out = gh_api(args)
        self.counts[kind] += n
        if self._since_check is not None:
            self._since_check += n
        return out

    def check_rate(self, force=False):
        """run / リポジトリの境目でだけ呼ぶ。途中ページで止めず、半端な状態を残さない。"""
        if not force and self._since_check is not None and self._since_check < self.check_every:
            return
        d = _json(gh_api(["rate_limit"]), "rate_limit")
        self.rate_checks += 1
        self._since_check = 0
        core = d.get("resources", {}).get("core", {}) if isinstance(d, dict) else {}
        rem, reset = core.get("remaining"), core.get("reset")
        if isinstance(rem, bool) or not isinstance(rem, int):
            raise FetchError("rate_limit の remaining を解釈できない")
        if rem < self.min_remaining:
            raise RateLimited(rem, reset)

    def list_repos(self, org):
        text = gh_api(["--paginate", "orgs/%s/repos?per_page=100&type=all" % org])
        repos, pages = parse_concatenated_json(text)
        self.counts["repos"] += max(pages, 1)
        if self._since_check is not None:
            self._since_check += max(pages, 1)
        return repos

    def _runs_pages(self, repo, org, created):
        runs, total, page = [], None, 1
        while True:
            self.check_rate()  # 二分探索で呼び出しが増えても残量を確認する（runs 一覧は途中中断してもキャッシュを壊さない）
            text = self._call("runs", ["-X", "GET", "repos/%s/%s/actions/runs" % (org, repo),
                                       "-f", "created=%s" % created, "-f", "per_page=%d" % PER_PAGE,
                                       "-f", "page=%d" % page])
            d = _json(text, "runs")
            wr = d.get("workflow_runs") if isinstance(d, dict) else None
            tc = d.get("total_count") if isinstance(d, dict) else None
            if not isinstance(wr, list) or isinstance(tc, bool) or not isinstance(tc, int):
                raise FetchError("runs の応答形式が不正")
            if total is None:
                total = tc
            runs.extend(wr)
            if len(wr) < PER_PAGE or page * PER_PAGE >= RUNS_SEARCH_CAP:
                return runs, total
            page += 1

    def list_runs(self, repo, org, since, today, truncated):
        """created>=since の run を全件取る。1,000 件上限を超えるときは期間を二分して取り直す。"""
        out = {}

        def collect(lo, hi, spec):
            runs, total = self._runs_pages(repo, org, spec)
            if total > RUNS_SEARCH_CAP:
                if lo >= hi:
                    truncated.append("%s/%s %s" % (org, repo, lo.isoformat()))
                else:
                    mid = lo + (hi - lo) // 2
                    collect(lo, mid, "%s..%s" % (lo.isoformat(), mid.isoformat()))
                    collect(mid + timedelta(days=1), hi,"%s..%s" % ((mid + timedelta(days=1)).isoformat(), hi.isoformat()))
                    return
            for r in runs:
                out[_run_key(r)[0]] = r

        collect(since, today, ">=%s" % since.isoformat())
        return list(out.values())

    def list_jobs(self, repo, org, run_id):
        jobs = []
        for page in range(1, MAX_JOB_PAGES + 1):
            text = self._call("jobs", ["-X", "GET", "repos/%s/%s/actions/runs/%d/jobs" % (org, repo, run_id),
                                       "-f", "filter=all", "-f", "per_page=%d" % PER_PAGE,
                                       "-f", "page=%d" % page])
            d = _json(text, "jobs")
            lst = d.get("jobs") if isinstance(d, dict) else None
            if not isinstance(lst, list):
                raise FetchError("jobs の応答形式が不正")
            jobs.extend(lst)
            if len(lst) < PER_PAGE:
                return jobs
        raise FetchError("jobs のページ数が上限を超えた")


def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _run_key(r):
    """run の (id, attempt) を検証して返す。"""
    if not isinstance(r, dict) or not _pos_int(r.get("id")):
        raise FetchError("run の id が正の整数ではない")
    att = r.get("run_attempt", 1)
    if not _pos_int(att):
        raise FetchError("run の run_attempt が正の整数ではない")
    return r["id"], att


# ---- キャッシュ層 ----

def load_cache(path, org):
    empty = {"schema_version": SCHEMA_VERSION, "org": org, "runs": {}}
    if not os.path.exists(path):
        return empty
    bad = FetchError("キャッシュが壊れている: %s（削除して再取得するか確認すること）" % path)
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        raise bad
    if not isinstance(c, dict) or c.get("schema_version") != SCHEMA_VERSION or c.get("org") != org \
            or not isinstance(c.get("runs"), dict):
        raise bad
    for rid, e in c["runs"].items():
        if not rid.isdigit() or not isinstance(e, dict) or not isinstance(e.get("repo"), str) \
                or not REPO_RE.match(e["repo"]) or not _pos_int(e.get("attempt")) \
                or not isinstance(e.get("created_at"), str) or not isinstance(e.get("agg"), list):
            raise bad
        for a in e["agg"]:
            m = a.get("minutes") if isinstance(a, dict) else None
            if not isinstance(a, dict) or not isinstance(a.get("runner_group"), str) \
                    or not isinstance(a.get("day"), str) or not DATE_RE.match(a["day"]) \
                    or isinstance(m, bool) or not isinstance(m, (int, float)) \
                    or not math.isfinite(m) or m < 0:
                raise bad
            try:
                date.fromisoformat(a["day"])
            except ValueError:
                raise bad
    return c


def atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- main ----

def parse_since(s, today):
    if not DATE_RE.match(s):
        raise FetchError("--since は YYYY-MM-DD 形式で指定する")
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise FetchError("--since が実在しない日付")
    if d > today:
        raise FetchError("--since が未来日")
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description="Actions jobs API からセルフホストランナーの日次行を作る")
    ap.add_argument("--org", required=True)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD（UTC）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", required=True, help="増分キャッシュのディレクトリ")
    ap.add_argument("--min-remaining", type=int, default=DEFAULT_MIN_REMAINING,
                    help="core レート制限の残量がこの値を下回ったら中断する")
    ap.add_argument("--rate-check-every", type=int, default=DEFAULT_RATE_CHECK_EVERY,
                    help="API 呼び出しがこの回数増えるごとに残量を確認する")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    today = now.date()
    stats = {"hosted": 0, "unfinished": 0, "listed": 0, "fetched": 0, "cached": 0, "pending": 0,
             "archived": 0, "badname": 0}
    truncated, skipped, stale, cache, cache_path, client = [], [], [], None, None, None
    until_s = today.isoformat()

    def lines(rows=None, note=None):
        out = ["出力: %s" % a.out, "期間: %s..%s（UTC）" % (a.since, until_s),
               "リポジトリ: 対象 %d（アーカイブ除外 %d / 名前不正で除外 %d）" % (
                   stats.get("targets", 0), stats["archived"], stats["badname"]),
               "API 呼び出し: 計 %d（repos %d / runs %d / jobs %d）・rate_limit 確認 %d 回" % (
                   client.total(), client.counts["repos"], client.counts["runs"],
                   client.counts["jobs"], client.rate_checks),
               "run: 一覧 %d / 新規取得 %d / キャッシュ利用 %d / 未完了で保留 %d" % (
                   stats["listed"], stats["fetched"], stats["cached"], stats["pending"])]
        if rows is not None:
            out.append("行数: %d・セルフホスト合計 %.1f 分（除外: GitHub ホスト job %d / 未完了 job %d）" % (
                len(rows), sum(r["minutes"] for r in rows), stats["hosted"], stats["unfinished"]))
        if truncated:
            out.append("警告: 1,000 件上限で欠けた可能性のある区間 %d" % len(truncated))
        if skipped:
            out.append("警告: 不正な job でスキップした run %d" % len(skipped))
        if stale:
            out.append("警告: 再実行中などで旧 attempt を集計から除外した run %d" % len(stale))
        if note:
            out.append(note)
        return out

    try:
        if not ORG_RE.match(a.org):
            raise FetchError("--org が不正")
        since = parse_since(a.since, today)
        if a.min_remaining < 0 or a.rate_check_every < 1:
            raise FetchError("--min-remaining は 0 以上、--rate-check-every は 1 以上の整数で指定する")
        if os.path.isdir(a.out):
            raise FetchError("--out がディレクトリを指している")
        if not os.path.isdir(os.path.dirname(os.path.abspath(a.out))):
            raise FetchError("--out の親ディレクトリが存在しない")
        if os.path.exists(a.cache) and not os.path.isdir(a.cache):
            raise FetchError("--cache がディレクトリではない")
        if not os.path.isdir(os.path.dirname(os.path.abspath(a.cache))):
            raise FetchError("--cache の親ディレクトリが存在しない")
        cache_path = os.path.join(a.cache, "jobs-cache-%s.json" % a.org)
        cache = load_cache(cache_path, a.org)
        os.makedirs(a.cache, exist_ok=True)
        client = GhClient(a.min_remaining, a.rate_check_every)
    except FetchError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1

    visibility, listed = {}, []
    try:
        client.check_rate(force=True)
        targets = []
        for repo in client.list_repos(a.org):
            if not isinstance(repo, dict) or not isinstance(repo.get("name"), str):
                continue
            name = repo["name"]
            if repo.get("archived") is True:
                stats["archived"] += 1
                continue
            if not REPO_RE.match(name) or name in (".", ".."):
                stats["badname"] += 1
                continue
            vis = repo.get("visibility")
            if not isinstance(vis, str):
                vis = "private" if repo.get("private") else "public"
            visibility[name] = vis.lower()
            targets.append(name)
        stats["targets"] = len(targets)
        for repo in targets:
            client.check_rate()
            for run in client.list_runs(repo, a.org, since, today, truncated):
                rid, att = _run_key(run)
                listed.append((rid, att, repo, run))
        stats["listed"] = len(listed)
        listed.sort(key=lambda t: t[0])
        for rid, att, repo, run in listed:
            ent = cache["runs"].get(str(rid))
            if ent is not None and att <= ent["attempt"]:
                stats["cached"] += 1
                continue
            if run.get("status") != "completed":
                stats["pending"] += 1
                if ent is not None:
                    # 再実行中: 旧 attempt の分を集計に残すと古い値になるため、完了まで集計から外す
                    stale.append(str(rid))
                continue
            client.check_rate()
            jobs = client.list_jobs(repo, a.org, rid)
            try:
                agg, hosted, unfinished = aggregate_jobs(jobs)
            except InvalidJob as e:
                skipped.append("%s/%s run %d: %s" % (a.org, repo, rid, e))
                if ent is not None:
                    stale.append(str(rid))
                continue
            created = run.get("created_at") if isinstance(run.get("created_at"), str) else ""
            cache["runs"][str(rid)] = {"repo": repo, "attempt": att, "created_at": created, "agg": agg}
            stats["fetched"] += 1
            stats["hosted"] += hosted
            stats["unfinished"] += unfinished
        entries = [cache["runs"][str(rid)] for rid, _, _, _ in listed
                   if str(rid) in cache["runs"] and str(rid) not in stale]
        # --since より前に作られた run はこの期間では再参照されないため、キャッシュの無制限な増加を防ぐ
        for k in [k for k, e in cache["runs"].items() if e["created_at"][:10] and e["created_at"][:10] < a.since]:
            del cache["runs"][k]
        rows = build_rows(entries, visibility, a.since)
        atomic_write_json(cache_path, cache)
        atomic_write_json(a.out, {
            "schema_version": SCHEMA_VERSION, "source": "jobs", "org": a.org,
            "since": a.since, "until": until_s,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "warnings": (["1,000 件上限で欠けた可能性: %s" % t for t in truncated]
                         + ["不正な job を含むため run をスキップ: %s" % t for t in skipped]
                         + ["再実行中または再取得できず旧 attempt を集計から除外: run %s" % t for t in stale]),
            "rows": rows,
        })
    except RateLimited as e:
        _save_quietly(cache_path, cache)
        print("\n".join(lines(note="中断: レート制限の残量不足（キャッシュ保存済み）")))
        print("エラー: レート制限の残量 %s が閾値 %d を下回ったため中断した。キャッシュ保存済み。"
              "reset（epoch %s）後に同じコマンドで再実行すると続きから取る" % (e.remaining, a.min_remaining, e.reset),
              file=sys.stderr)
        return EXIT_RATE_LIMITED
    except FetchError as e:
        _save_quietly(cache_path, cache)
        print("エラー: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("エラー: 書き込みに失敗: %s" % e.strerror, file=sys.stderr)
        return 1
    print("\n".join(lines(rows)))
    return 0


def _save_quietly(path, cache):
    """中断・失敗時に、完了済み run だけを含むキャッシュを保存する（保存失敗は無視）。"""
    if path is None or cache is None:
        return
    try:
        atomic_write_json(path, cache)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
