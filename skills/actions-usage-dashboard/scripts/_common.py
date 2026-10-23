"""actions-usage-dashboard の gh 呼び出し・原子的書き込み helper の唯一の定義。

役割と境界:
  fetch_billing.py / fetch_jobs.py / summarize.py が共有する GH_TIMEOUT / ORG_RE /
  FetchError / gh_api / atomic_write_json をここに集約する。以前は各スクリプトが同じ
  実装を複製しており、片方だけ直して乖離する恐れがあった。標準ライブラリだけで動く。

読み込み方:
  各スクリプトは `python3 -I` で起動される。隔離モードでは起動スクリプトのディレクトリが
  sys.path に入らないため、利用側が自分のディレクトリを sys.path.append してから
  `from _common import ...` する。append にするのは、同じディレクトリに標準モジュール名の
  ファイルがあっても標準ライブラリを先に解決させるため。

import しない利用者:
  build_dashboard.py は「ネットワークに出ない」保証（subprocess 等を import しない）を
  守るため本モジュールを import せず、ORG_RE / atomic_write_json を複製している。
  複製が本モジュールと一致することは tests/test_build_dashboard.py が検査する。

検証用の正規表現の終端は `\\Z` に統一する（`$` は末尾の改行の手前にも一致するため）。
"""
import json
import os
import re
import subprocess
import tempfile

GH_TIMEOUT = 120
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")


class FetchError(Exception):
    """取得・入力・検証の失敗。各スクリプトの main が日本語 1 行にして終了コード 1 で終える。"""


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
