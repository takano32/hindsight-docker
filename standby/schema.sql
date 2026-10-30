-- 埋め込みのホットスタンバイ（別表方式）。memory_units には触れず、控えのベクトルをこの表に持つ。
-- filler.py が埋め、switch.py が memory_units.embedding と入れ替える。何度流しても同じ（IF NOT EXISTS）。

-- 控えのベクトル。モデルごとに1行（同じ unit_id で bge-m3 と sarashina を別々に持てる）。
-- FK は張らない（memory_units への削除・更新の負担にしない。消えた行は filler が掃除する）。
-- embedding は次元なしの vector（モデルごとに次元が違う）。HNSW は張らない（切り替えのとき memory_units 側に作る）。
CREATE TABLE IF NOT EXISTS memory_units_standby (
    unit_id     uuid        NOT NULL,
    model       text        NOT NULL,
    embedding   vector      NOT NULL,
    dim         integer     NOT NULL,
    -- 埋めた文（retain と同じ日付・実体名の足し方をした文。接頭辞は含めない）の sha256。
    -- memory_units の本文・日付・実体名が変わるとこの値と合わなくなり、filler が埋め直す。
    text_hash   text        NOT NULL,
    -- filler＝標準。swap-backup＝switch.py が入れ替えのとき退避した、元の memory_units.embedding。
    -- synthetic＝計測用の乱数ベクトル（本番に使わない。switch.py は --allow-synthetic が無ければ拒む）
    source      text        NOT NULL DEFAULT 'filler',
    embedded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (unit_id, model)
);
CREATE INDEX IF NOT EXISTS memory_units_standby_model_idx ON memory_units_standby (model);

-- モデルごとの設定（filler が最初に登録する）。接頭辞が食い違ったまま埋めると混ざるので、filler は食い違いを拒む。
CREATE TABLE IF NOT EXISTS memory_units_standby_models (
    model          text PRIMARY KEY,
    dim            integer NOT NULL,
    passage_prefix text    NOT NULL DEFAULT '',
    query_prefix   text    NOT NULL DEFAULT '',
    base_url       text,
    note           text,
    registered_at  timestamptz NOT NULL DEFAULT now()
);

-- いま memory_units.embedding に入っているモデル（active_model）と、直近の入れ替えの記録。
CREATE TABLE IF NOT EXISTS memory_units_standby_meta (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
