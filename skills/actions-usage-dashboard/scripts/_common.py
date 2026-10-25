"""actions-usage-dashboard の gh 呼び出し・原子的書き込み helper の唯一の定義。

役割と境界:
  fetch_billing.py / fetch_jobs.py / summarize.py が共有する GH_TIMEOUT / ORG_RE / MONTH_RE /
  DAY_RE / ISO_RE / MAX_ROWS_BYTES / MAX_NAME_LEN / CTRL_RE / safe / nonneg_num / FetchError / gh_api /
  atomic_write_json / parse_concatenated_json をここに集約する。以前は各スクリプトが同じ
  実装を複製しており、片方だけ直して乖離する恐れがあった。標準ライブラリだけで動く。

読み込み方:
  各スクリプトは `python3 -I` で起動される。隔離モードでは起動スクリプトのディレクトリが
  sys.path に入らないため、利用側が自分のディレクトリを sys.path.append してから
  `from _common import ...` する。append にするのは、同じディレクトリに標準モジュール名の
  ファイルがあっても標準ライブラリを先に解決させるため。

import しない利用者:
  build_dashboard.py は「ネットワークに出ない」保証（subprocess 等を import しない）を
  守るため本モジュールを import せず、ORG_RE / ISO_RE / MAX_ROWS_BYTES / atomic_write_json / safe / CTRL_RE
  を複製している。正典はすべて本モジュールで、safe の既定長だけが意図した差（端末向けの名前は 60 字、
  build_dashboard.py はパスとエラー文を載せるため 300 字）。一致は tests/test_build_dashboard.py が検査する。

検証用の正規表現の終端は `\\Z` に統一する（`$` は末尾の改行の手前にも一致するため）。
"""
import json
import math
import os
import re
import subprocess
import tempfile

GH_TIMEOUT = 120
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])\Z")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
# rows 入力の上限。build_dashboard.py が同値を複製している（一致はテストが検査）。
MAX_ROWS_BYTES = 8 * 1024 * 1024
MAX_NAME_LEN = 60
# 端末へ出す前に除去・置換する制御文字（C0・DEL・C1）。build_dashboard.py が同値を複製している（一致はテストが検査）。
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class FetchError(Exception):
    """取得・入力・検証の失敗。各スクリプトの main が日本語 1 行にして終了コード 1 で終える。"""


def safe(s, limit=MAX_NAME_LEN):
    """端末への制御文字注入を防ぐため、制御文字を除去し長い文字列を切り詰める。

    名前用の既定 60 文字は取得元の警告には短く、末尾の識別情報（org・repo・日付）が欠けるため、
    警告は呼び出し側が定める上限（summarize の MAX_WARNING_LEN 等）を limit に渡して呼ぶ。
    """
    s = CTRL_RE.sub("?", str(s))
    return s if len(s) <= limit else s[:limit - 1] + "…"


def nonneg_num(item, key, required=True, origin="API"):
    """item[key] を有限の非負数（float）として返す。bool・非数値・非有限・負数は FetchError。

    required=False で欠損（None）なら 0.0。origin は例外メッセージの入力元表記（API / rows）。
    """
    v = item.get(key)
    if v is None and not required:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise FetchError("%s の数値フィールド %s が不正（有限の非負数ではない）" % (origin, key))
    return float(v)


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


def parse_concatenated_json(text):
    """gh --paginate が連結した JSON 配列（[..][..]）を (1 本のリスト, ページ数) にする。

    fetch_billing（リポジトリ一覧）と fetch_jobs（同。ページ数を件数計上に使う）が共有する。
    配列以外・解析不能は FetchError（fail-closed）。
    """
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
