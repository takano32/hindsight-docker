#!/usr/bin/env python3
"""failed のまま残った非同期タスク（既定は retain）を探して、入れ直す。

LLM（Haiku）が長く止まると、retain は再試行の上限（.env の HINDSIGHT_API_WORKER_MAX_RETRIES ×
HINDSIGHT_API_WORKER_TASK_RETRY_BACKOFF_SECONDS）を使い切って failed になり、その回の記憶が入らないまま残る。
これはそれを拾って、公開 API の retry で worker に戻す道具。
DB のテーブルや Hindsight の中身には触らず、公開 API（GET operations と POST .../retry）だけを使う。

既定は一覧を出すだけ（dry-run）。--apply を付けたときだけ入れ直す。

  ./retry-failed.py                       # 全 bank の failed な retain を数えて並べる
  ./retry-failed.py --bank zerozari00     # bank を絞る
  ./retry-failed.py --type all            # retain 以外（consolidation など）も見る
  ./retry-failed.py --apply               # 入れ直す
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

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


def failed_ops(bank, op_type):
    out, offset = [], 0
    while True:
        params = {"status": "failed", "limit": PAGE, "offset": offset}
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
    a = ap.parse_args()

    total = retried = 0
    for bank in a.bank or banks():
        for op in failed_ops(bank, a.type):
            total += 1
            oid = op.get("operation_id") or op.get("id")
            err = (op.get("error_message") or "").replace("\n", " ")[:120]
            print(f"{bank}\t{op.get('operation_type')}\t{oid}\t{op.get('updated_at')}\t{op.get('document_id') or ''}\t{err}")
            if a.apply:
                try:
                    call("POST", f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/operations/{oid}/retry")
                    retried += 1
                except Exception as e:  # 1件の失敗で残りを止めない
                    print(f"  retry に失敗: {e}", file=sys.stderr)

    mode = f"入れ直した {retried} 件" if a.apply else "dry-run（--apply で入れ直す）"
    print(f"failed: {total} 件／{mode}", file=sys.stderr)


if __name__ == "__main__":
    main()
