#!/usr/bin/env python3
"""検索の質（1）: 同じ候補集合（Sarashina で埋めた無作為の n 件）の中で、bge-m3 と Sarashina の上位10件を出し、重なりを測る。
bge-m3 側＝memory_units.embedding にある既存ベクトル（本番が retain で埋めたもの）、問いは llama-embed で埋める。
Sarashina 側＝embedding_standby の控え、問いは接頭辞 [[sarashina:query]] で埋める。
読むだけ（DB に書かない）。Jev の判定用に問いと候補の本文を JSON に出す（scratchpad へ。リポジトリには置かない）。

  STANDBY_MOUNTS="/path/in:/in:ro /path/out:/out" ./run.sh bench/quality_collect.py --db hindsight_standby_test \
      --queries /in/queries.json --out /out/quality_candidates.json
"""
import argparse, json, sys
sys.path.insert(0, "/standby")
import numpy as np
import common


def unit(v):
    v = np.asarray(v, dtype="float64")
    return v / np.linalg.norm(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--production", action="store_true")
    ap.add_argument("--queries", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sarashina-model", default="sarashina3-embedding")
    ap.add_argument("--bge-url", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--sarashina-url", default="http://127.0.0.1:8082/v1")
    ap.add_argument("--k", type=int, default=10)
    a = ap.parse_args()
    conn = common.connect(a.db, production=a.production)
    cur = conn.cursor()
    cur.execute("SELECT m.id::text, m.fact_type, m.text, m.embedding::text, s.embedding::text "
                "FROM embedding_standby s JOIN memory_units m ON m.id=s.unit_id "
                "WHERE s.model=%s AND s.source='filler' AND m.embedding IS NOT NULL ORDER BY m.id", (a.sarashina_model,))
    ids, fts, texts, B, S = [], [], [], [], []
    for i, ft, t, be, se in cur:
        ids.append(i); fts.append(ft); texts.append(t); B.append(json.loads(be)); S.append(json.loads(se))
    B = np.array([unit(v) for v in B]); S = np.array([unit(v) for v in S])
    print(f"候補 {len(ids)} 件、bge dim={B.shape[1]} sarashina dim={S.shape[1]}", flush=True)
    queries = json.load(open(a.queries))
    out = {"n_candidates": len(ids), "queries": []}
    for q in queries:
        qb = unit(common.embed_batch(a.bge_url, "bge-m3", [q["text"]])[0])
        qs = unit(common.embed_batch(a.sarashina_url, a.sarashina_model, [q["text"]], prefix="[[sarashina:query]]")[0])
        cb, cs = B @ qb, S @ qs
        tb, ts = np.argsort(-cb)[: a.k], np.argsort(-cs)[: a.k]
        out["queries"].append({
            "message_id": q["message_id"], "text": q["text"],
            "bge_top": [{"id": ids[j], "cos": float(cb[j])} for j in tb],
            "sar_top": [{"id": ids[j], "cos": float(cs[j])} for j in ts],
            "overlap": len(set(tb.tolist()) & set(ts.tolist())),
            "bge_all": {"p50": float(np.median(cb)), "p90": float(np.percentile(cb, 90)), "max": float(cb.max())},
            "sar_all": {"p50": float(np.median(cs)), "p90": float(np.percentile(cs, 90)), "max": float(cs.max())},
            "bge_n_ge": {str(t): int((cb >= t).sum()) for t in (0.3, 0.35, 0.45, 0.5, 0.6)},
            "sar_n_ge": {str(t): int((cs >= t).sum()) for t in (0.3, 0.35, 0.45, 0.5, 0.6, 0.7)},
        })
        print(f"  {q['message_id']} overlap={out['queries'][-1]['overlap']}", flush=True)
    out["cands"] = {i: {"text": t, "fact_type": f} for i, t, f in zip(ids, texts, fts)}
    json.dump(out, open(a.out, "w"), ensure_ascii=False)
    ov = [q["overlap"] for q in out["queries"]]
    print(f"重なり（上位{a.k}）: 平均 {np.mean(ov):.2f}、最小 {min(ov)}、最大 {max(ov)}、中央値 {np.median(ov)}")


if __name__ == "__main__":
    main()
