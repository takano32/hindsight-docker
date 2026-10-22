#!/usr/bin/env python3
"""failed のまま残った非同期タスク（既定は retain）を探して、入れ直す。

LLM（Haiku）が長く止まると、retain は再試行の上限（.env の HINDSIGHT_API_WORKER_MAX_RETRIES ×
HINDSIGHT_API_WORKER_TASK_RETRY_BACKOFF_SECONDS）を使い切って failed になり、その回の記憶が入らないまま残る。
これはそれを拾って、公開 API の retry で worker に戻す道具。
DB のテーブルや Hindsight の中身には触らず、公開 API（GET operations と POST .../retry）だけを使う。

LLM の利用枠が尽きると、Hindsight は retain を failed にせず、枠が戻ると言われた時刻まで pending のまま先送りする
（next_retry_at が未来の日時になる。2026-10-19 に週の上限で 10/22 まで先送りされた）。枠を先に戻したときは
--deferred で、それも拾って入れ直す（取り消してから入れ直す。公開 API の DELETE と retry）。

Hindsight のコンテナを作り直すと、動いていた処理は status=processing のまま取り残される（worker の ID はコンテナごとに変わるので、
新しい worker は自分の持ち物と見なさず、引き継がない。取り残された処理は同じ bank の後続の処理も止める＝2026-10-22 に
64 件が残って、処理待ち 600 件が動かなくなった）。--stranded は、更新が --stranded-min 分（既定 10）より前で止まっている
processing も拾って、取り消してから入れ直す。作り直した直後の、動いている処理を巻き込まないよう、作り直してから
十分に（既定の 10 分）たってから撃つ。

既定は一覧を出すだけ（dry-run）。--apply を付けたときだけ入れ直す。

  ./retry-failed.py                       # 全 bank の failed な retain を数えて並べる
  ./retry-failed.py --bank zerozari00     # bank を絞る
  ./retry-failed.py --type all            # retain 以外（consolidation など）も見る
  ./retry-failed.py --apply               # 入れ直す
  ./retry-failed.py --deferred --apply    # 先送りされた pending も入れ直す
  ./retry-failed.py --stranded --apply    # 作り直しで取り残された processing も入れ直す
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:18888").rstrip("/")
PAGE = 100


def call(method, path, params=None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def banks():
    out, offset = [], 0
    while True:
        d = call("GET", "/v1/default/banks", {"limit": PAGE, "offset": offset})
        items = d.get("banks") or d.get("items") or []
        out += [b.get("bank_id") or b.get("id") for b in items]
        if len(items) < PAGE:
            return out
        offset += PAGE


def failed_ops(bank, op_type, status="failed"):
    out, offset = [], 0
    while True:
        params = {"status": status, "limit": PAGE, "offset": offset}
        if op_type != "all":
            params["type"] = op_type
        d = call("GET", f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/operations", params)
        ops = d.get("operations") or []
        out += ops
        if len(ops) < PAGE:
            return out
        offset += PAGE


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", action="append", help="対象の bank（複数可）。省略で全部")
    ap.add_argument("--type", default="retain", help="operation の種類（retain／consolidation／all など。既定 retain）")
    ap.add_argument("--apply", action="store_true", help="入れ直す（無ければ一覧だけ）")
    ap.add_argument("--deferred", action="store_true", help="next_retry_at が未来の pending（枠切れで先送りされたもの）も対象にする")
    ap.add_argument("--stranded", action="store_true", help="更新が止まったままの processing（コンテナの作り直しで取り残されたもの）も対象にする")
    ap.add_argument("--stranded-min", type=int, default=10, help="--stranded で、更新が何分前より古い processing を取り残しと見なすか（既定 10）")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)

    total = retried = 0
    for bank in a.bank or banks():
        ops = failed_ops(bank, a.type)
        if a.deferred:
            ops += [o for o in failed_ops(bank, a.type, "pending")
                    if o.get("next_retry_at") and datetime.fromisoformat(o["next_retry_at"]) > now]
        if a.stranded:
            ops += [o for o in failed_ops(bank, a.type, "processing")
                    if o.get("updated_at") and (now - datetime.fromisoformat(o["updated_at"])).total_seconds() > a.stranded_min * 60]
        for op in ops:
            total += 1
            oid = op.get("operation_id") or op.get("id")
            err = (op.get("error_message") or "").replace("\n", " ")[:120]
            print(f"{bank}\t{op.get('status')}\t{op.get('operation_type')}\t{oid}\t{op.get('updated_at')}\t{op.get('document_id') or ''}\t{err}")
            if a.apply:
                try:
                    if op.get("status") in ("pending", "processing"):  # 先送り中・取り残しは取り消してから入れ直す（retry は failed と cancelled しか受けない）
                        call("DELETE", f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/operations/{oid}")
                    call("POST", f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/operations/{oid}/retry")
                    retried += 1
                except Exception as e:  # 1件の失敗で残りを止めない
                    print(f"  retry に失敗: {e}", file=sys.stderr)

    mode = f"入れ直した {retried} 件" if a.apply else "dry-run（--apply で入れ直す）"
    print(f"対象: {total} 件／{mode}", file=sys.stderr)


if __name__ == "__main__":
    main()
