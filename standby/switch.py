#!/usr/bin/env python3
"""memory_units.embedding を控え（memory_units_standby）のモデルに入れ替える。逆向き（戻す）も同じコマンド。

  前提: Hindsight（と、その DB に繋ぐ他のものすべて）を止めてあること。繋がっていれば拒む。
  既定は dry-run（前提の点検と計画を出して終わる。何も書かない）。--apply で初めて動く。

  forward（bge-m3 → Sarashina）:
    ./run.sh switch.py --db NAME --from-model bge-m3 --to-model sarashina3-embedding --register-active [--apply]
    （--register-active は、meta に「列に入っているモデル名」の記録がまだ無い最初の1回だけ。2回目からは meta と照合する）
  back（戻す）:
    ./run.sh switch.py --db NAME --from-model sarashina3-embedding --to-model bge-m3 \
        --from-passage-prefix '[[sarashina:document]]' --from-query-prefix '[[sarashina:query]]' [--apply]

  --apply は1つのトランザクション（失敗すれば全部元に戻る）:
    1. memory_units と mental_models を ACCESS EXCLUSIVE でロック（lock_timeout つき）
    2. いまの memory_units.embedding を「from のモデルの控え」として memory_units_standby に退避する
       （source=swap-backup、本文のハッシュ付き＝あとで filler が埋め直さない）
    3. HNSW の索引（memory_units のバンク×fact_type の部分索引、mental_models の索引）を落とす（定義は覚える）
    4. 列を vector(N) に作り替える。USING で to のモデルの控えを引く＝テーブルの書き直し1回で新しいベクトルが入る
    5. 覚えた定義で HNSW の索引を作り直す
    6. 検証（NULL の数、次元）→ meta の active_model を更新 → コミット
  コミット後に ANALYZE。

  mental_models は、embedding が入った行があると既定では拒む（--discard-mental-model-embeddings で NULL にして進む。
  Hindsight が作り直す。いまは0行）。
  DB 名は必ず指定する（既定なし）。本番の名前（hindsight）は --production が要る。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import psycopg2
import psycopg2.extras
from psycopg2 import sql

import common
from common import log


def fetch_state(conn, a):
    cur = conn.cursor()
    st = {}
    cur.execute("SELECT count(*), count(embedding) FROM memory_units")
    st["units"], st["units_with_embedding"] = cur.fetchone()
    cur.execute("SELECT count(*), count(embedding) FROM mental_models")
    st["mm_rows"], st["mm_with_embedding"] = cur.fetchone()
    cur.execute("SELECT DISTINCT vector_dims(embedding) FROM memory_units WHERE embedding IS NOT NULL")
    st["current_dims"] = sorted(r[0] for r in cur.fetchall())
    cur.execute("SELECT a.atttypmod FROM pg_attribute a WHERE a.attrelid='public.memory_units'::regclass AND a.attname='embedding'")
    st["column_typmod"] = cur.fetchone()[0]
    cur.execute("SELECT DISTINCT dim, source FROM memory_units_standby WHERE model=%s", (a.to_model,))
    st["to_dims_sources"] = sorted(cur.fetchall())
    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()")
    st["other_sessions"] = cur.fetchone()[0]
    cur.execute("SELECT coalesce(string_agg(DISTINCT coalesce(application_name,'')||'@'||coalesce(client_addr::text,'local'), ', '), '') "
                "FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()")
    st["other_sessions_who"] = cur.fetchone()[0]
    cur.execute("SELECT c.relname, t.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid "
                "JOIN pg_am am ON am.oid=c.relam WHERE t.relname IN ('memory_units','mental_models') AND am.amname='hnsw' "
                "ORDER BY 1")
    st["hnsw"] = cur.fetchall()
    cur.execute("SELECT value FROM memory_units_standby_meta WHERE key='active_model'")
    r = cur.fetchone()
    st["active_model"] = r[0] if r else None
    cur.execute("SELECT dim FROM memory_units_standby_models WHERE model=%s", (a.from_model,))
    r = cur.fetchone()
    st["from_registered_dim"] = r[0] if r else 0
    # 中身の照合: 列のベクトルと、from モデルの控え（filler が埋めた行）のコサイン。控えが無ければ飛ばす（n=0）
    cur.execute("SELECT 1 - (m.embedding <=> s.embedding) FROM memory_units m JOIN memory_units_standby s "
                "ON s.unit_id=m.id AND s.model=%s AND s.source='filler' AND s.dim=vector_dims(m.embedding) "
                "WHERE m.embedding IS NOT NULL LIMIT 300", (a.from_model,))
    cs = sorted(float(x[0]) for x in cur.fetchall())
    st["sample_n"] = len(cs)
    st["sample_median"] = cs[len(cs)//2] if cs else 1.0
    st["sample_min"] = cs[0] if cs else 1.0
    return st


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--production", action="store_true")
    ap.add_argument("--from-model", required=True, help="いま memory_units.embedding に入っているモデル（退避先の行名）")
    ap.add_argument("--to-model", required=True, help="控えから入れるモデル")
    ap.add_argument("--from-passage-prefix", default="")
    ap.add_argument("--from-query-prefix", default="")
    ap.add_argument("--register-active", action="store_true",
                    help="meta に active_model の記録が無いとき、「いま列に入っているのは --from-model だ」と自分で断言して記録する（初回だけ）")
    ap.add_argument("--apply", action="store_true", help="これが無ければ dry-run")
    ap.add_argument("--allow-stale", type=int, default=0, help="控えのハッシュが本文と合わない行をこの件数まで許す")
    ap.add_argument("--allow-synthetic", action="store_true", help="乱数の控え（計測用）を許す。試験用 DB だけ")
    ap.add_argument("--discard-mental-model-embeddings", action="store_true")
    ap.add_argument("--maintenance-work-mem", default="512MB")
    ap.add_argument("--lock-timeout", default="30s")
    ap.add_argument("--parallel-workers", type=int, default=0,
                    help="HNSW 構築の並列ワーカー数（SET LOCAL）。既定 0＝並列なし。postgres コンテナの /dev/shm は 64MB で、"
                         "並列構築が maintenance_work_mem ぶんの共有メモリを取って DiskFull になる（2026-10-30 の試験で実際に落ちた）")
    ap.add_argument("--json", action="store_true", help="最後に計時を JSON で出す")
    a = ap.parse_args()
    if a.from_model == a.to_model:
        sys.exit("from と to が同じ")

    conn = common.connect(a.db, production=a.production)
    common.ensure_schema(conn)
    t_all = time.time()
    st = fetch_state(conn, a)
    conn.rollback()
    problems: list[str] = []
    log(f"DB={a.db} units={st['units']} (embedding あり {st['units_with_embedding']}) mental_models={st['mm_rows']} "
        f"(embedding あり {st['mm_with_embedding']}) 列の次元(typmod)={st['column_typmod']} 実ベクトルの次元={st['current_dims']} "
        f"active_model={st['active_model']} 照合サンプル n={st['sample_n']} 中央値={st['sample_median']:.4f}")
    if len(st["current_dims"]) > 1:
        problems.append(f"memory_units.embedding の次元が混ざっている: {st['current_dims']}")
    # 列に入っているベクトルのモデル名の記録（memory_units_standby_meta.active_model）と照合する。
    if st["active_model"] is None:
        if not a.register_active:
            problems.append("meta に active_model の記録が無い（列に何のモデルが入っているか未登録）。"
                            f"いま列に入っているのが {a.from_model!r} だと確かなら --register-active を付ける（初回だけ）")
        else:
            log(f"meta に active_model の記録が無い。--register-active により {a.from_model!r} と断言して記録する")
    elif st["active_model"] != a.from_model:
        problems.append(f"meta の active_model は {st['active_model']!r} だが --from-model は {a.from_model!r}。取り違えている")
    if st["from_registered_dim"] and st["current_dims"] and st["from_registered_dim"] != st["current_dims"][0]:
        problems.append(f"{a.from_model} の登録次元 {st['from_registered_dim']} と、列の実ベクトルの次元 {st['current_dims'][0]} が違う")
    if st["column_typmod"] > 0 and st["current_dims"] and st["column_typmod"] != st["current_dims"][0]:
        problems.append(f"列の型の次元 {st['column_typmod']} と実ベクトルの次元 {st['current_dims'][0]} が違う")
    if st["sample_n"] >= 20 and st["sample_median"] < 0.98:
        problems.append(f"列のベクトルが、{a.from_model} の控え（filler 製）{st['sample_n']} 行と合わない"
                        f"（コサインの中央値 {st['sample_median']:.4f}、最小 {st['sample_min']:.4f}）。列の中身は別のモデルかもしれない")
    if st["other_sessions"]:
        problems.append(f"この DB に他の接続が {st['other_sessions']} 本ある（{st['other_sessions_who']}）。Hindsight を止める")
    dims = sorted({d for d, _ in st["to_dims_sources"]})
    if len(dims) != 1:
        problems.append(f"to モデル {a.to_model} の控えの次元が1つに決まらない: {st['to_dims_sources']}")
    to_dim = dims[0] if len(dims) == 1 else None
    if any(s == "synthetic" for _, s in st["to_dims_sources"]) and not a.allow_synthetic:
        problems.append("to の控えに乱数（synthetic）が混ざっている。--allow-synthetic は試験用 DB だけ")
    if a.allow_synthetic and a.production:
        problems.append("--allow-synthetic は本番に使わない")
    if st["mm_with_embedding"] and not a.discard_mental_model_embeddings:
        problems.append(f"mental_models に embedding のある行が {st['mm_with_embedding']} ある。--discard-mental-model-embeddings で NULL にして進めるか、先に対処する")

    # 控えの網羅と鮮度（文を組み直してハッシュを比べる＝filler と同じ判定）
    t0 = time.time()
    units = common.load_units(conn)
    cur = conn.cursor()
    cur.execute("SELECT unit_id::text, text_hash FROM memory_units_standby WHERE model=%s", (a.to_model,))
    have = dict(cur.fetchall())
    missing = sum(1 for u in units if u.id not in have)
    stale = sum(1 for u in units if u.id in have and have[u.id] != u.hash)
    conn.rollback()
    log(f"控え {a.to_model}: 欠け {missing}、ハッシュ不一致 {stale}（点検 {time.time()-t0:.1f}s）")
    if missing:
        problems.append(f"to の控えが {missing} 行足りない。filler を先に回す")
    if stale > a.allow_stale:
        problems.append(f"to の控えのうち {stale} 行が本文と合わない（--allow-stale {a.allow_stale}）。filler を回し直す")
    if problems:
        for p in problems:
            log("拒む: " + p)
        sys.exit(2)

    log(f"計画: {a.from_model}（{st['current_dims']}）→ {a.to_model}（{to_dim}）。HNSW {len(st['hnsw'])} 本を作り直す: "
        + ", ".join(h[0] for h in st["hnsw"]))
    if not a.apply:
        log("dry-run: 何も書いていない。動かすなら --apply（Hindsight を止めてあること）")
        return

    timings: dict[str, float] = {}
    hashes = [(u.id, u.hash) for u in units]
    cur = conn.cursor()

    def step(name):
        class _T:
            def __enter__(s): s.t = time.time(); return s
            def __exit__(s, *e):
                timings[name] = round(time.time() - s.t, 3)
                log(f"  {name}: {timings[name]}s")
        return _T()

    t_tx = time.time()
    try:
        cur.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(a.lock_timeout)))
        cur.execute(sql.SQL("SET LOCAL maintenance_work_mem = {}").format(sql.Literal(a.maintenance_work_mem)))
        cur.execute(sql.SQL("SET LOCAL max_parallel_maintenance_workers = {}").format(sql.SQL(str(a.parallel_workers))))
        with step("lock"):
            cur.execute("LOCK TABLE public.memory_units, public.mental_models IN ACCESS EXCLUSIVE MODE")
        # ロックの下でもう一度、控えの網羅を確かめる
        cur.execute("SELECT count(*) FROM memory_units m WHERE NOT EXISTS "
                    "(SELECT 1 FROM memory_units_standby s WHERE s.unit_id=m.id AND s.model=%s)", (a.to_model,))
        if cur.fetchone()[0]:
            raise RuntimeError("ロックの下で控えの欠けが見つかった")
        with step("backup"):
            cur.execute("CREATE TEMP TABLE _swap_hash (id uuid PRIMARY KEY, h text NOT NULL) ON COMMIT DROP")
            psycopg2.extras.execute_values(cur, "INSERT INTO _swap_hash VALUES %s", hashes, template="(%s::uuid, %s)", page_size=2000)
            cur.execute(
                "INSERT INTO memory_units_standby (unit_id, model, embedding, dim, text_hash, source, embedded_at) "
                "SELECT m.id, %s, m.embedding, vector_dims(m.embedding), h.h, 'swap-backup', now() "
                "FROM memory_units m JOIN _swap_hash h ON h.id=m.id WHERE m.embedding IS NOT NULL "
                "ON CONFLICT (unit_id, model) DO UPDATE SET embedding=EXCLUDED.embedding, dim=EXCLUDED.dim, "
                "text_hash=EXCLUDED.text_hash, embedded_at=now(), "
                "source=CASE WHEN memory_units_standby.source='synthetic' AND memory_units_standby.embedding = EXCLUDED.embedding "
                "THEN 'synthetic' ELSE 'swap-backup' END", (a.from_model,))
            n_backup = cur.rowcount
            cur_dim = st["current_dims"][0] if st["current_dims"] else (st["column_typmod"] if st["column_typmod"] > 0 else 0)
            cur.execute(
                "INSERT INTO memory_units_standby_models (model, dim, passage_prefix, query_prefix, note) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (model) DO NOTHING",
                (a.from_model, cur_dim, a.from_passage_prefix, a.from_query_prefix, "switch.py が退避したときに登録"))
        with step("drop_hnsw"):
            for name, _t, _d in st["hnsw"]:
                cur.execute(sql.SQL("DROP INDEX public.{}").format(sql.Identifier(name)))
        with step("alter_memory_units"):
            cur.execute(sql.SQL(
                "CREATE FUNCTION public._standby_swap_vec(uuid) RETURNS vector LANGUAGE sql STABLE AS "
                "$f$ SELECT embedding FROM public.memory_units_standby WHERE unit_id = $1 AND model = {} $f$"
            ).format(sql.Literal(a.to_model)))
            cur.execute(sql.SQL(
                "ALTER TABLE public.memory_units ALTER COLUMN embedding TYPE vector({n}) "
                "USING public._standby_swap_vec(id)::vector({n})").format(n=sql.SQL(str(to_dim))))
            cur.execute("DROP FUNCTION public._standby_swap_vec(uuid)")
        with step("alter_mental_models"):
            cur.execute(sql.SQL("ALTER TABLE public.mental_models ALTER COLUMN embedding TYPE vector({n}) "
                                "USING NULL::vector({n})").format(n=sql.SQL(str(to_dim))))
        for name, _t, d in st["hnsw"]:
            with step(f"create_{name}"):
                cur.execute(d)
        with step("verify"):
            cur.execute("SELECT count(*), count(embedding), min(vector_dims(embedding)), max(vector_dims(embedding)) FROM memory_units")
            tot, withemb, dmin, dmax = cur.fetchone()
            if withemb != st["units_with_embedding"] or dmin != to_dim or dmax != to_dim:
                raise RuntimeError(f"検証に失敗: units={tot} embedding={withemb}（前 {st['units_with_embedding']}） dims={dmin}..{dmax}（期待 {to_dim}）")
            cur.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid='public.memory_units'::regclass AND attname='embedding'")
            if cur.fetchone()[0] != to_dim:
                raise RuntimeError("列の typmod が期待と違う")
            cur.execute(
                "INSERT INTO memory_units_standby_meta (key, value) VALUES ('active_model', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", (a.to_model,))
            cur.execute(
                "INSERT INTO memory_units_standby_meta (key, value) VALUES ('last_swap', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                (json.dumps({"from": a.from_model, "to": a.to_model, "backup_rows": n_backup}),))
        with step("commit"):
            conn.commit()
    except Exception as e:
        conn.rollback()
        log(f"失敗したので全部元に戻した: {e!r}")
        sys.exit(1)
    timings["transaction_total"] = round(time.time() - t_tx, 3)
    conn2 = common.connect(a.db, production=a.production, autocommit=True)
    t0 = time.time()
    conn2.cursor().execute("ANALYZE public.memory_units")
    timings["analyze_after_commit"] = round(time.time() - t0, 3)
    timings["wall_total"] = round(time.time() - t_all, 3)
    log(f"完了: {a.from_model} → {a.to_model}。トランザクション {timings['transaction_total']}s、全体 {timings['wall_total']}s")
    if a.json:
        print(json.dumps({"timings": timings, "rows": st["units"], "from": a.from_model, "to": a.to_model}, ensure_ascii=False))


if __name__ == "__main__":
    main()
