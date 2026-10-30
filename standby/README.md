# 埋め込みのホットスタンバイ（別表方式）

Hindsight の埋め込みモデルを入れ替えるとき、**全行を埋め直す時間（Sarashina は 60 回/分で約 6 時間）のあいだ Hindsight を止めない**ための仕組み。
控えのベクトルを別表 `memory_units_standby` に先に作っておき、止める時間は列の入れ替えだけ（試験用の複製で約 50 秒）にする。
2026-10-30 に作り、本番の複製（`hindsight_standby_test`）だけで試した。**本番 DB にはまだ何も流していない。**

## 構成

| ファイル | 役割 |
|---|---|
| `schema.sql` | `memory_units_standby`（控え。`(unit_id, model)` が主キー、次元なしの `vector`）、`memory_units_standby_models`（モデルごとの接頭辞・次元）、`memory_units_standby_meta`（`active_model`＝いま列に入っているモデル名） |
| `common.py` | DB 接続（DB 名は必須・本番名は `--production` が要る）、retain と同じ埋め込み文の組み立て、OpenAI 互換の `/embeddings` 呼び出し |
| `filler.py` | 控えを埋める。`memory_units` を読むだけ。再開できる。孤児の掃除。`--check`（読むだけ）、`--dry-run`、`--sample`、`--interval` |
| `switch.py` | `memory_units.embedding` と控えの入れ替え（forward も back も同じ）。既定は dry-run |
| `run.sh` | 使い捨てのコンテナ（Hindsight の公式イメージ。psycopg2・numpy 入り）で python を流す。本番の `hindsight` コンテナには入らない |
| `compose.test.yml` | 試験用の Hindsight（別名・別ポート 18889・LLM なし・ワーカー無効・管理 UI なし） |
| `bench/` | `drift.py`（文の再現）、`quality_collect.py`＋`quality_judge.py`（検索の質）、`recall_check.py`、`sysmon.sh`、`prep_test_db.sql` |

### 埋める文（retain に揃える）

- fact（world / experience）: `本文 (happened in <Month Year>)` ＋ ` [実体名, ...]`（`engine/retain/embedding_processing.py` の `augment_texts_with_dates`、月年は `_format_readable_date` の `%B %Y`）。日付は `occurred_start`、無ければ `mentioned_at`。
- observation: 本文だけ（`consolidator._embed_observation_texts`）。
- 実体名は `memory_units` に順序つきでは残っていない。`unit_entities` の名前を、`text_signals`（retain のとき実体名を並べて残した列）の中の出現位置で並べ直す。
- 接頭辞はハッシュに含めない（モデルごとに `memory_units_standby_models` に登録し、食い違ったまま埋めるのを `filler.py` が拒む）。

### 入れ替えの手順（`switch.py --apply`、1トランザクション）

1. `memory_units`・`mental_models` を ACCESS EXCLUSIVE でロック（`lock_timeout` 30 秒）
2. いまの列のベクトルを「from のモデルの控え」に退避（`source=swap-backup`、本文のハッシュ付き）
3. HNSW（バンク×fact_type の部分索引、`mental_models` の索引）を落とす（定義は覚える）
4. 列を `vector(N)` に作り替える。`USING` で to の控えを引く関数を呼ぶ＝**テーブルの書き直し1回**で新しいベクトルが入る
5. 覚えた定義で HNSW を作り直す
6. 検証（件数・次元・typmod）→ `active_model` を更新 → コミット。コミット後に `ANALYZE`

拒む条件: 他の接続がある（Hindsight が動いている）／to の控えが足りない／本文とハッシュが合わない／乱数の控えが混ざっている（`--allow-synthetic` は試験用 DB だけ）／
**列のモデル名の記録（meta）と `--from-model` が合わない**／記録が無いのに `--register-active` が無い／登録次元と実ベクトルの次元が違う／
列のベクトルが from の控え（filler 製）と合わない（コサインの中央値 0.98 未満）／`mental_models` に embedding のある行がある。

## 計測（2026-10-30、`hindsight_standby_test`、22,360 行＝3バンクの合計）

### 入れ替えの時間（Hindsight を止めておく時間のうち、DB の仕事）

控えは Sarashina 実ベクトル 1,000 行 + **乱数の単位ベクトル 21,360 行**（時間を測るためだけ。実際の埋め込みではない）。2周やった。

| 工程 | forward 1周目 | forward 2周目 | back 1周目 | back 2周目 |
|---|---|---|---|---|
| 退避（旧ベクトルを控えへ） | 2.1 s | 2.5 s | 3.9 s | 6.1 s |
| 列の作り替え（書き直し＋他の索引の再構築） | 5.5 s | 6.4 s | 4.4 s | 5.2 s |
| HNSW 10 本の再構築 | 約 33 s | 約 38 s | 約 14 s | 約 12 s |
| **トランザクション合計** | **42.4 s** | **47.0 s** | **23.2 s** | **23.9 s** |

- 止める時間は、これに Hindsight の停止と起動（試験用で起動に約 17 秒）を足した 1 分前後。back は半分（1024 次元のほうが軽い）。
- 乱数ベクトルは HNSW に構造が無く、実データの構築時間とは違う（速いか遅いかは測っていない）。
- 並列構築は `/dev/shm`（256MB）を `maintenance_work_mem` ぶん取って `DiskFull` で落ちた（最初の試行で実際に落ち、トランザクションごと元に戻った）。`switch.py` は既定で並列 0。
- 箱の負荷（`bench/sysmon.sh`）: 切り替え中の load1 は最大 1.1〜3.2、メモリ空きは最小 7.4GB、スワップは 3.5GB のまま変化なし、postgres コンテナのメモリは最大約 960MiB。
- `memory_units_standby` の大きさは 2 モデルぶん（22,360 行ずつ）で 611MB。`memory_units` は 321MB。

### 切り替え後の起動確認

試験用の Hindsight（`compose.test.yml`、Sarashina、`[[sarashina:query]]`／`[[sarashina:document]]`）が、次元チェックを通って起動した（`dim: 1536`、列の typmod 1536）。recall は結果を返した（実ベクトルの行が上位に入る）。
LLM は none、ワーカー無効、自動統合なし。外へ出た通信は Sarashina への 3 回（次元の検出 1 回、recall の問い 2 回）だけ。終了後に停止・削除した。

### 検索の質（同じ無作為 1,000 件〔バンク nilkani06、seed=1〕の中での比較）

問い 22 件（受信文。下の「問いについて」）。bge-m3 は本番の既存ベクトル、Sarashina は filler が作った控え。

| 指標 | bge-m3 | Sarashina |
|---|---|---|
| 上位10件の重なり（22 問の平均／最小〜最大） | 4.95 / 1〜8（中央値 5） | |
| Jev が関連ありと判定（p≥0.5）した数、上位10件の平均 | 6.95 | 7.27 |
| 同、上位5件の平均 | 4.00 | 4.09 |
| Jev の p の平均（上位10件） | 0.621 | 0.633 |
| 問いごとの関連数 | bge-m3 が多い 6 | Sarashina が多い 7（同じ 9） |
| 片方だけが挙げた 111 件のうち関連ありの数 | 55 | 62 |

質は同程度（差は 22 問・1,000 件では誤差の範囲）。

**スケールが違う（重要）**: コサイン類似度は、bge-m3 は候補 1,000 件の中央値 0.37・1位の中央値 0.59 だが、Sarashina は中央値 0.70・1位の中央値 0.81、**候補の全部が 0.6 を超える**。
`HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY`（既定 0.3）と、フックの `MIN_SEMANTIC`（0.45）・`CANDIDATE_MIN_SEMANTIC`（0.35）は、そのままだと全件を通してしまう。
上位10件のうち Jev が関連ありとした行のコサインは p5=0.756、関連なしの行は中央値 0.773・p95=0.803（関連の有無で分かれない）ので、床は 0.74〜0.76 あたりが目安（22 問の標本からの推定。切り替え後に測り直す）。

### 再現のズレ（filler が作る文を bge-m3 で埋めた vs 本番の既存ベクトル、200 行）

| 種別 | n | min | p5 | 中央値 | 0.999 以上の割合 |
|---|---|---|---|---|---|
| 全体 | 200 | 0.9855 | 0.9960 | 0.99998 | 0.825 |
| world | 61 | 0.9952 | 0.9981 | 0.99998 | 0.852 |
| experience | 85 | 0.9855 | 0.9949 | 0.99997 | 0.694 |
| observation | 54 | 0.99995 | 0.99997 | 0.99998 | 1.000 |

ズレは小さい（直すほどではないと判断した）。外れは experience に寄る＝実体名の順や名前が retain 時と違うこと（`unit_entities` は解決後の名前で順序を持たない）が原因と推測（原因の切り分けは測っていない）。

## 本番への適用手順（未実施。takano32 の判断を待つ）

前提と注意:
- Sarashina の規約: 無償プランは入出力が学習に使われる、個人を識別できる情報の入力は禁止。**列は DB 全体で1本なので、1つのバンクだけでなく全バンクの全行も埋める必要がある**。
- 全行（22,360 行、増えている）を Sarashina で埋めるのに **約 6 時間**（1,000 行で 969 秒＝約 62 行/分、60 回/分の上限どおり）。nilkani06 の 8,184 行だけなら約 2.2 時間。
- 切り替え後は retain・recall・consolidation の埋め込みがすべて 60 回/分の上限に当たる（件数ぶんだけ枠を使う）。Hindsight の埋め込みの 60 秒の時間切れは設定で延ばせない（README の既知の点）。

1. 本番 DB に表を作って埋め始める（`memory_units` には触れない。Hindsight は動かしたまま）
   `./run.sh filler.py --db hindsight --production --model sarashina3-embedding --base-url http://127.0.0.1:8082/v1 --passage-prefix '[[sarashina:document]]' --query-prefix '[[sarashina:query]]' --interval 600`
2. 追いつきの確認: `./run.sh filler.py --db hindsight --production --model sarashina3-embedding --check`（`埋める` が 0 に近い）
3. 切り替えの窓（1 分前後）:
   1. filler をもう一度流して追いつく → `docker compose stop hindsight`
   2. `./run.sh switch.py --db hindsight --production --from-model bge-m3 --to-model sarashina3-embedding --register-active`（dry-run。拒まれなければ `--apply` を付けて再実行）
   3. compose の `.env` を Sarashina に（`HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL=http://sarashina-embed:8080/v1`、`..._MODEL=sarashina3-embedding`、`..._QUERY_PREFIX`／`..._PASSAGE_PREFIX`、`..._BATCH_SIZE` は小さく、`HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY` は 0.75 前後）。`compose.yml` に external network `sarashina` を足す。フックの床も測り直す
   4. `docker compose up -d` → recall で確かめる
4. 戻すスタンバイを回しておく: `./run.sh filler.py --db hindsight --production --model bge-m3 --base-url http://127.0.0.1:8081/v1 --interval 600`（切り替えのあとに増えた行の bge-m3 を埋める。ズレは上の表のとおり）
5. 戻すとき: `docker compose stop hindsight` → `switch.py --from-model sarashina3-embedding --to-model bge-m3 --from-passage-prefix '[[sarashina:document]]' --from-query-prefix '[[sarashina:query]]' --apply` → `.env` を戻して起動

## 未確認

- 実ベクトルで全行を入れ替えたときの HNSW 構築時間（乱数ベクトルでしか測っていない）。
- 切り替え後の本番の recall の質と、床の値（22 問の推定）。
- フックの Jev 判定には履歴（直前の会話）が付くが、質の測定では付けていない。
- 本番 DB に `memory_units_standby` を作ることの影響（表3つの追加と、filler の長い読み取りトランザクション。試験用でしか見ていない）。
