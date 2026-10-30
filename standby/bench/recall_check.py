#!/usr/bin/env python3
"""試験用 Hindsight（ポート 18889）に recall を撃って、返りの件数と意味スコアの分布だけ出す（本文は出さない）。
  python3 bench/recall_check.py "問い" [--bank nilkani06] [--port 18889]
"""
import argparse, json, urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query"); ap.add_argument("--bank", default="nilkani06"); ap.add_argument("--port", type=int, default=18889)
    ap.add_argument("--max-tokens", type=int, default=4096)
    a = ap.parse_args()
    body = json.dumps({"query": a.query, "budget": "low", "max_tokens": a.max_tokens}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{a.port}/v1/default/banks/{a.bank}/memories/recall", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    res = d.get("results", [])
    sems = [(x.get("scores") or {}).get("semantic") for x in res]
    sems = [s for s in sems if s is not None]
    print(f"results={len(res)} with_semantic={len(sems)} top_sem={max(sems) if sems else None} "
          f"min_sem={min(sems) if sems else None}")
    for x in res[:5]:
        print(" ", x.get("type"), (x.get("scores") or {}).get("semantic"), len(x.get("text") or ""))


if __name__ == "__main__":
    main()
