"""embedding standby の共通部分（DB 接続、retain と同じ埋め込み文の組み立て、OpenAI 互換の埋め込み呼び出し）。

標準ライブラリ＋psycopg2 だけ。Hindsight の公式イメージ（psycopg2・numpy が入っている）の python で動かす
（run.sh が使い捨てのコンテナで流す）。本番の hindsight コンテナの中では動かさない。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

import psycopg2
import psycopg2.extras

PRODUCTION_DB = "hindsight"  # 本番の DB 名。既定にはしない。触るには --production が要る


def connect(db: str, *, production: bool = False, autocommit: bool = False):
    """DB 名は呼ぶ側が必ず明示する（既定なし）。本番の名前は --production が無ければ拒む。"""
    if not db:
        sys.exit("--db が要る（既定は無い）")
    if db == PRODUCTION_DB and not production:
        sys.exit(f"DB '{db}' は本番。触るなら --production を付ける（試験は hindsight_standby_test）")
    user = os.environ.get("HINDSIGHT_DB_USER")
    password = os.environ.get("HINDSIGHT_DB_PASSWORD")
    if not user or not password:
        sys.exit("HINDSIGHT_DB_USER / HINDSIGHT_DB_PASSWORD が環境に無い（run.sh が .env から渡す）")
    conn = psycopg2.connect(
        host=os.environ.get("STANDBY_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("STANDBY_DB_PORT", "5432")),
        dbname=db, user=user, password=password,
        application_name="embedding-standby",
        options="-c TimeZone=UTC",
    )
    conn.autocommit = autocommit
    return conn


def ensure_schema(conn) -> None:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql"), encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


# ---------------------------------------------------------------------------
# 埋め込み文の組み立て（retain と同じ）
#   fact（world / experience）: engine/retain/embedding_processing.py の augment_texts_with_dates
#       text + " (happened in <Month Year>)"  または " (happened from <Month Year> to <Month Year>)"
#       + " [name1, name2, ...]"（実体名）。日付は occurred_start、無ければ mentioned_at。
#       月年の書式は engine/memory_engine.py の _format_readable_date（"%B %Y"）。
#   observation: engine/consolidation/consolidator.py の _embed_observation_texts は本文だけを埋める。
# 実体名は memory_units に順序つきでは残っていない。retain のとき text_signals に
# 「実体名を半角空白でつないだ列＋日付の語」が残るので、その並びを手がかりに unit_entities の名前を並べ直す。
# ---------------------------------------------------------------------------

def readable_date(dt: datetime) -> str:
    return f"{dt.strftime('%B')} {dt.strftime('%Y')}"


def order_entities(names: list[str], signals: str | None) -> list[str]:
    if not names:
        return []
    if not signals:
        return sorted(names)

    def key(n: str):
        p = signals.find(n)
        return (p if p >= 0 else 10**9, n)

    return sorted(names, key=key)


def augment(fact_type: str, text: str, occurred_start, occurred_end, mentioned_at, entities: list[str]) -> str:
    if fact_type == "observation":
        return text
    fact_date = occurred_start or mentioned_at
    if fact_date is not None:
        d = readable_date(fact_date)
        if occurred_end and occurred_end != occurred_start:
            out = f"{text} (happened from {d} to {readable_date(occurred_end)})"
        else:
            out = f"{text} (happened in {d})"
    else:
        out = text
    if entities:
        out = f"{out} [{', '.join(entities)}]"
    return out


def text_hash(embedded_text: str) -> str:
    return hashlib.sha256(embedded_text.encode("utf-8")).hexdigest()


@dataclass
class Unit:
    id: str
    bank_id: str
    fact_type: str
    embedded_text: str
    hash: str


def load_units(conn, banks: list[str] | None = None, fact_types: list[str] | None = None) -> list[Unit]:
    """memory_units を読んで、埋め込む文とそのハッシュを作る（書き込まない）。id 順。"""
    where, params = [], []
    if banks:
        where.append("bank_id = ANY(%s)")
        params.append(banks)
    if fact_types:
        where.append("fact_type = ANY(%s)")
        params.append(fact_types)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    ents: dict[str, list[str]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ue.unit_id::text, e.canonical_name FROM unit_entities ue "
            f"JOIN entities e ON e.id = ue.entity_id "
            f"WHERE ue.unit_id IN (SELECT id FROM memory_units {w})", params)
        for uid, name in cur:
            ents[uid].append(name)
    units: list[Unit] = []
    with conn.cursor(name="load_units") as cur:
        cur.itersize = 5000
        cur.execute(
            f"SELECT id::text, bank_id, fact_type, text, occurred_start, occurred_end, mentioned_at, text_signals "
            f"FROM memory_units {w} ORDER BY id", params)
        for uid, bank, ft, text, os_, oe, ma, sig in cur:
            et = augment(ft, text, os_, oe, ma, order_entities(ents.get(uid, []), sig))
            units.append(Unit(uid, bank, ft, et, text_hash(et)))
    return units


def vec_literal(v) -> str:
    return "[" + ",".join(format(float(x), ".8g") for x in v) + "]"


# ---------------------------------------------------------------------------
# OpenAI 互換の /embeddings（鍵は見ない相手を想定。Sarashina の変換サーバー、llama-embed）
# ---------------------------------------------------------------------------

class EmbedError(RuntimeError):
    pass


def embed_batch(base_url: str, model: str, texts: list[str], *, prefix: str = "", timeout: float = 300.0,
                retries: int = 4, backoff: float = 5.0, api_key: str = "none") -> list[list[float]]:
    body = json.dumps({"model": model, "input": [prefix + t for t in texts]}).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/embeddings", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            rows = sorted(data["data"], key=lambda d: d["index"])
            if len(rows) != len(texts):
                raise EmbedError(f"{len(rows)} vectors for {len(texts)} texts")
            return [d["embedding"] for d in rows]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code not in (429, 500, 502, 503, 504):
                raise EmbedError(last) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = repr(e)
        time.sleep(backoff * (attempt + 1))
    raise EmbedError(f"retries exhausted: {last}")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
