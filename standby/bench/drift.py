#!/usr/bin/env python3
"""再現のズレ: filler が作る文を bge-m3（llama-embed）で埋めたベクトルと、memory_units にある既存ベクトル
（retain のとき本番が埋めたもの）のコサインを測る。読むだけ（DB に書かない）。本文は出さない（件数と統計だけ）。

  ./run.sh bench/drift.py --db hindsight_standby_test --n 200 --seed 1 [--show-worst 5]
"""
import argparse, hashlib, json, sys
sys.path.insert(0, "/standby")
import numpy as np
import common


def pct(a, q):
    return float(np.percentile(a, q))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--production", action="store_true")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--model", default="bge-m3")
    ap.add_argument("--bank", action="append")
    ap.add_argument("--dump-worst", default="", help="最悪の数件の id と理由の手がかり（本文なし）をここに書く")
    ap.add_argument("--variant", default="entities-sorted", help="文の作り方の変種（比較用）")
    a = ap.parse_args()
    conn = common.connect(a.db, production=a.production)
    units = common.load_units(conn, a.bank)
    ranked = sorted(units, key=lambda u: hashlib.md5((a.seed + u.id).encode()).hexdigest())[: a.n]
    ids = [u.id for u in ranked]
    cur = conn.cursor()
    cur.execute("SELECT id::text, embedding::text FROM memory_units WHERE id = ANY(%s::uuid[])", (ids,))
    old = {i: np.array(json.loads(e), dtype="float32") for i, e in cur}
    new = {}
    for k in range(0, len(ranked), 16):
        chunk = ranked[k:k + 16]
        vs = common.embed_batch(a.base_url, a.model, [u.embedded_text for u in chunk])
        for u, v in zip(chunk, vs):
            new[u.id] = np.array(v, dtype="float32")
    rows = []
    for u in ranked:
        o, n = old[u.id], new[u.id]
        c = float(o @ n / (np.linalg.norm(o) * np.linalg.norm(n)))
        rows.append((u.fact_type, c, u.id))
    def report(name, sel):
        c = np.array([r[1] for r in sel])
        if len(c) == 0: return
        print(f"{name:12s} n={len(c):4d} min={c.min():.6f} p5={pct(c,5):.6f} p25={pct(c,25):.6f} median={pct(c,50):.6f} "
              f"mean={c.mean():.6f} frac>=0.999={float((c>=0.999).mean()):.3f} frac>=0.99={float((c>=0.99).mean()):.3f}")
    report("all", rows)
    for ft in ("world", "experience", "observation"):
        report(ft, [r for r in rows if r[0] == ft])
    rows.sort(key=lambda r: r[1])
    if a.dump_worst:
        with open(a.dump_worst, "w") as f:
            for ft, c, i in rows[:30]:
                f.write(f"{c:.4f}\t{ft}\t{i}\n")

if __name__ == "__main__":
    main()
