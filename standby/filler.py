#!/usr/bin/env python3
"""控えのベクトルを memory_units_standby に埋める（memory_units には書かない）。

  * memory_units を読み、retain と同じ文（common.augment）を作ってハッシュを取る。
  * memory_units_standby に（unit_id, model）の行が無い／ハッシュが違う行を、控えのモデルで埋める。
  * 1バッチごとにコミットする＝止めても続きから再開できる。
  * memory_units に無い unit_id の控えは掃除する（--no-prune で止められる）。
  * --check は読むだけ（件数を出して終わる）。--dry-run は埋める行の数を出すだけで API も書き込みもしない。

DB 名は --db で必ず指定する（既定なし）。本番の名前（hindsight）は --production が要る。
run.sh 経由で使い捨てのコンテナの中で動かす（README.md）。
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sys
import time

import common
from common import log


def table_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.memory_units_standby') IS NOT NULL")
        return cur.fetchone()[0]


def pick_sample(units, n: int, seed: str):
    """seed で決まる無作為の n 件（md5(seed || id) の小さい順）。同じ seed なら同じ集合。"""
    ranked = sorted(units, key=lambda u: hashlib.md5((seed + u.id).encode()).hexdigest())
    return ranked[:n]


def synthetic_vector(unit_id: str, dim: int) -> list[float]:
    """計測用の乱数の単位ベクトル（unit_id で決まる）。実データの埋め込みではない。"""
    import numpy as np
    rng = np.random.default_rng(int(hashlib.md5(unit_id.encode()).hexdigest()[:15], 16))
    v = rng.standard_normal(dim).astype("float32")
    v /= np.linalg.norm(v)
    return v.tolist()


def register_model(conn, args) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT passage_prefix, query_prefix, dim FROM memory_units_standby_models WHERE model=%s", (args.model,))
        row = cur.fetchone()
        if row:
            if row[0] != args.passage_prefix or row[1] != args.query_prefix:
                sys.exit(f"model {args.model} は接頭辞が登録と違う（登録: passage={row[0]!r} query={row[1]!r}）。"
                         f"混ざるので拒む。埋め直すなら、そのモデルの行を消してから登録し直す")
            if args.dim and row[2] != args.dim:
                sys.exit(f"model {args.model} の dim が登録（{row[2]}）と違う")
        else:
            cur.execute(
                "INSERT INTO memory_units_standby_models (model, dim, passage_prefix, query_prefix, base_url, note) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (args.model, args.dim or 0, args.passage_prefix, args.query_prefix, args.base_url, args.note))
    conn.commit()


def run_once(args) -> dict:
    conn = common.connect(args.db, production=args.production)
    stat = {"units": 0, "selected": 0, "uptodate": 0, "todo": 0, "embedded": 0, "pruned": 0, "orphans": 0}
    try:
        banks = args.bank or None
        fts = args.fact_type or None
        have: dict[str, str] = {}
        if table_exists(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT unit_id::text, text_hash FROM memory_units_standby WHERE model=%s", (args.model,))
                have = dict(cur.fetchall())
        elif not args.check and not args.dry_run:
            common.ensure_schema(conn)
        t0 = time.time()
        units = common.load_units(conn, banks, fts)
        log(f"memory_units {len(units)} 行を読んで文を組んだ（{time.time()-t0:.1f}s）")
        stat["units"] = len(units)
        if args.sample:
            units = pick_sample(units, args.sample, args.seed)
        stat["selected"] = len(units)
        todo = [u for u in units if have.get(u.id) != u.hash]
        stat["uptodate"] = len(units) - len(todo)
        if args.limit:
            todo = todo[: args.limit]
        stat["todo"] = len(todo)
        with conn.cursor() as cur:  # 孤児（memory_units に無い控え）の数
            if table_exists(conn):
                cur.execute("SELECT count(*) FROM memory_units_standby s WHERE s.model=%s AND NOT EXISTS "
                            "(SELECT 1 FROM memory_units m WHERE m.id=s.unit_id)", (args.model,))
                stat["orphans"] = cur.fetchone()[0]
        log(f"model={args.model}: 対象 {stat['selected']}、最新 {stat['uptodate']}、埋める {stat['todo']}、孤児 {stat['orphans']}")
        if args.check or args.dry_run:
            conn.rollback()
            return stat
        register_model(conn, args)
        # 埋める
        t_start = time.time()
        i = 0
        while i < len(todo):
            if args.max_seconds and time.time() - t_start > args.max_seconds:
                log(f"--max-seconds {args.max_seconds} に達したので止める（続きは次の実行で）")
                break
            chunk = todo[i: i + args.batch_size]
            tb = time.time()
            if args.synthetic_random:
                vecs = [synthetic_vector(u.id, args.dim) for u in chunk]
                source = "synthetic"
            else:
                vecs = common.embed_batch(args.base_url, args.model, [u.embedded_text for u in chunk],
                                          prefix=args.passage_prefix, timeout=args.timeout)
                source = "filler"
            dim = len(vecs[0])
            if any(len(v) != dim for v in vecs):
                sys.exit("バッチの中で次元が揃わない")
            rows = [(u.id, args.model, common.vec_literal(v), dim, u.hash, source) for u, v in zip(chunk, vecs)]
            with conn.cursor() as cur:
                import psycopg2.extras
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO memory_units_standby (unit_id, model, embedding, dim, text_hash, source, embedded_at) "
                    "VALUES %s ON CONFLICT (unit_id, model) DO UPDATE SET embedding=EXCLUDED.embedding, "
                    "dim=EXCLUDED.dim, text_hash=EXCLUDED.text_hash, source=EXCLUDED.source, embedded_at=now()",
                    rows, template="(%s::uuid, %s, %s::vector, %s, %s, %s, now())")
                cur.execute("UPDATE memory_units_standby_models SET dim=%s WHERE model=%s AND dim<>%s", (dim, args.model, dim))
            conn.commit()
            i += len(chunk)
            stat["embedded"] += len(chunk)
            el = time.time() - t_start
            log(f"  {min(i,len(todo))}/{len(todo)}（バッチ {time.time()-tb:.1f}s、累計 {el:.0f}s、残り約 {el/max(i,1)*max(len(todo)-i,0):.0f}s）")
        if not args.no_prune and not args.sample:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_units_standby s WHERE s.model=%s AND NOT EXISTS "
                            "(SELECT 1 FROM memory_units m WHERE m.id=s.unit_id)", (args.model,))
                stat["pruned"] = cur.rowcount
            conn.commit()
            if stat["pruned"]:
                log(f"孤児の控えを {stat['pruned']} 行掃除した")
        return stat
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--production", action="store_true", help="本番の DB 名（hindsight）を触る")
    ap.add_argument("--model", required=True, help="控えの行に付けるモデル名（例 sarashina3-embedding / bge-m3）")
    ap.add_argument("--base-url", default="", help="OpenAI 互換の /v1（例 http://127.0.0.1:8082/v1。--check/--synthetic-random では不要）")
    ap.add_argument("--passage-prefix", default="", help="文書側の接頭辞（Sarashina は [[sarashina:document]]）")
    ap.add_argument("--query-prefix", default="", help="問い側の接頭辞（登録だけ。Sarashina は [[sarashina:query]]）")
    ap.add_argument("--dim", type=int, default=0, help="期待する次元（0＝最初の返りから）。--synthetic-random では必須")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--bank", action="append", help="このバンクだけ（繰り返し可）")
    ap.add_argument("--fact-type", action="append", choices=["world", "experience", "observation"])
    ap.add_argument("--sample", type=int, default=0, help="無作為にこの件数だけ（--seed で固定）")
    ap.add_argument("--seed", default="1")
    ap.add_argument("--limit", type=int, default=0, help="1回に埋める上限件数")
    ap.add_argument("--max-seconds", type=float, default=0, help="この秒数を超えたら止める（再開できる）")
    ap.add_argument("--check", action="store_true", help="読むだけ。件数を出して終わる")
    ap.add_argument("--dry-run", action="store_true", help="埋める行の数を出すだけ（API も書き込みもしない）")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--interval", type=float, default=0, help="この秒数ごとに繰り返す（0＝1回で終わる）")
    ap.add_argument("--note", default=None)
    ap.add_argument("--synthetic-random", action="store_true",
                    help="計測用。API を呼ばず乱数の単位ベクトルで埋める（source=synthetic。本番に使わない）")
    args = ap.parse_args()
    if args.synthetic_random:
        if not args.dim:
            sys.exit("--synthetic-random には --dim が要る")
        if args.production:
            sys.exit("--synthetic-random は本番に使わない")
    elif not (args.check or args.dry_run) and not args.base_url:
        sys.exit("--base-url が要る")
    while True:
        stat = run_once(args)
        log("結果 " + " ".join(f"{k}={v}" for k, v in stat.items()))
        if not args.interval:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
