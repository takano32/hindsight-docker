# hindsight-docker

[Hindsight](https://github.com/vectorize-io/hindsight) の公式イメージを、postgresql-docker の共有 PostgreSQL に繋いで動かす。
本家リポジトリは fork も改変もしない。版は `.env` の `HINDSIGHT_VERSION`（2026-10-16 時点の最新リリース 1.0.0）。

## 前提

- `~/opt/postgresql-docker` が `up` していること（network `postgresql` と、hindsight のロールとデータベース）。
- `~/opt/ollama-docker` が `up` していること（network `ollama`。埋め込みを `local` で動かすときも、compose が network を参照するので要る）。
- `.env`（chmod 600）の `HINDSIGHT_DB_PASSWORD` が postgresql-docker の `.env` と同じ値。

## 使い方

```sh
cd ~/opt/hindsight-docker
docker compose up -d
docker compose logs -f hindsight     # 初回は埋め込みモデル（bge-m3 約 2.3GB）のダウンロードで数分かかる
curl -s http://127.0.0.1:8888/health
docker compose down                  # -v を付けなければモデルのキャッシュは残る
```

- API: `http://127.0.0.1:8888`（OpenAPI は `/docs`）。管理 UI: `http://127.0.0.1:9999`。
- MCP: `http://127.0.0.1:8888/mcp/{bank_id}/`（streamable HTTP、既定で認証なし。ポートは 127.0.0.1 だけに開けている）。
  Claude Code に足すなら `claude mcp add --transport http hindsight http://127.0.0.1:8888/mcp/<bank>/`。
- モデルのキャッシュは名前付きボリューム `hindsight_hf_cache`（コンテナ内 `/home/hindsight/.cache/huggingface`）。

## 量子化した埋め込みモデルを ollama で動かす

ollama は [ollama-docker](https://github.com/takano32/ollama-docker)（箱に1台の共有 ollama）に置いてある。
GGUF の量子化モデル（Q8_0 など）をそこで動かし、Hindsight は `openai` プロバイダ（OpenAI 互換 `/v1/embeddings`）で
`http://ollama:11434/v1` を指す（external network `ollama`、ホスト名 `ollama`）。compose の既定がこの向き。
版・同時に載せるモデル数・メモリ上限は ollama-docker の `.env` で決める。

```sh
# hindsight-docker の .env に
#   HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai
#   HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL=embeddinggemma:300m-qat-q8_0
#   HINDSIGHT_API_EMBEDDINGS_QUERY_PREFIX=task: search result | query: 
#   HINDSIGHT_API_EMBEDDINGS_PASSAGE_PREFIX=title: none | text: 
#   HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY=0.3    # ollama の尺度では既定の 0.3 のまま（llama-server なら 0.43）
(cd ~/opt/ollama-docker && docker compose up -d && docker compose exec ollama ollama pull embeddinggemma:300m-qat-q8_0)   # 初回は 338MB
docker compose up -d                                          # hindsight が ollama に繋ぐ
curl -s http://127.0.0.1:8888/health
```

- 2026-10-16 の昼までは同じ compose に2つ目のサービス（profile `ollama`）として置き、夕方に ollama-docker へ分け、
  夜にいちど ollama をやめて [llama-cpp-docker](https://github.com/takano32/llama-cpp-docker)（llama-server 2本）に替え、
  同じ夜のうちに ollama へ戻した（llama-server は1プロセス1モデルで埋め込みと LLM の2本立てになり、ollama の1つの口で2つのモデルを扱える手軽さを取った。
  llama-server のときの数字は経緯として下の表に残す）。
- 接頭辞は `openai` プロバイダに効く（`local` には効かない）。モデルのカードが求める形をそのまま入れる。
  Qwen3-Embedding は問いだけ `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:`（改行を含む。compose の文字列で `\n` と書く）。
- 次元が pgvector の HNSW 上限 2000 を超えるモデル（Qwen3-Embedding-4B の 2560 など）は `HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS` で MRL の切り詰めを頼める（ollama 0.40.5 の `/v1/embeddings` は `dimensions` を受けた。llama-server で受けるかは確かめていない。切り詰めた質はモデルのカードを見る）。
- ollama 側の鍵は使われない。`HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY` の既定 `none` は Hindsight の起動時検査を通すための埋め草。

### 計測（2026-10-16、この箱、LLM なし、リランカー rrf、自作 36 文・関係ある問い 42 本＋難しい 26 本）

最初の表は ollama 0.40.5 経由（同日の夕方。いまの構成はこちら）。llama-server（build 11681）経由で同じ試験をやり直した行は、その下の表（同日の夜、いまは使っていない）。

最終1位＝Hindsight が返した順位で狙った文が1位だった率（基本／難しい）、意味1位＝意味スコアだけで並べたときの1位率、
recall は1問の中央値、メモリは待機時の hindsight＋ollama（`docker stats`）、DL はこの回線（約 5MB/秒）での pull。

| モデル（ollama のタグ） | 接頭辞 | 最終1位 | 意味1位 | recall | メモリ | 容量 | DL |
|---|---|---|---|---|---|---|---|
| `embeddinggemma:300m-qat-q8_0` | あり | **100% / 92%** | 100% / 96% | **0.06s** | 358+627 MiB | 338MB | 69s |
| 同（接頭辞なし） | なし | 98% / 85% | 98% / 92% | 0.06s | 304+626 | | |
| `embeddinggemma:300m`（bf16） | あり | 100% / 92% | 100% / 96% | 0.65s | 356+689 | 621MB | 105s |
| `qwen3-embedding:0.6b-q8_0` | 問いに Instruct | 98% / 88% | 98% / 96% | 0.21s | 357+1158 | 639MB | 109s |
| 同（接頭辞なし） | なし | 95% / 81% | 95% / 96% | 0.10s | 304+1162 | | |
| `qwen3-embedding:4b`（Q4_K_M、1024 次元に切り詰め） | 問いに Instruct | 95% / 85% | 100% / 96% | 1.74s | 345+3090 | 2.5GB | 527s |
| `bge-m3:567m`（f16） | なし | 83% / 69% | 98% / 96% | 0.13s | 304+1332 | 1.2GB | 197s |
| `snowflake-arctic-embed2:568m` | 問いに `query: ` | 95% / 81% | 93% / 81% | 0.14s | 304+1333 | 1.2GB | 197s |
| `nomic-embed-text-v2-moe` | `search_query: `／`search_document: ` | 95% / 88% | 93% / 81% | 0.10s | 304+1137 | 958MB | 163s |
| `paraphrase-multilingual:278m` | なし | 93% / 73% | 86% / 77% | 0.06s | 304+746 | 563MB | 97s |
| `granite-embedding:278m` | なし | 43% / 31% | 93% / 88% | 0.07s | 304+747 | 563MB | 98s |
| `embeddinggemma-2:270m-q8_0-text` | あり | 45% / 27% | 98% / 100% | 0.08s | 304+481 | 303MB | 53s |
| 同＋足切り 0.66 | あり | 90% / 69% | 95% / 100% | 0.08s | 304+488 | | |

#### llama-server 経由（同日の夜、llama-cpp-docker、build 11681。経緯として残す。いまは ollama に戻している）

同じ試験を llama-server で。モデルは同じ元のチェックポイント（`ggml-org/embeddinggemma-300m-qat-q8_0-GGUF`、313MB、768 次元、mean pooling）。メモリは hindsight＋llama-embed。

| モデル（llama-server の名前） | 足切り | 最終1位 | 意味1位 | recall | メモリ | 容量 | DL |
|---|---|---|---|---|---|---|---|
| `embeddinggemma`（接頭辞あり） | 0.3（既定） | 93% / 58% | **100% / 100%** | 0.07s | 415+370 MiB | 313MB | 約100s（LLM と同時に落としていた） |
| 同 | **0.43** | **100% / 88%** | 100% / 100% | 0.07s | 304+371 | | |

- 意味の順位は ollama のときより良い（難しい組の取りこぼし1件が無くなった）のに、既定の足切り 0.3 のままだと最終1位が落ちる。
  理由はスコアの尺度の違いで、同じ文の組でもコサイン類似度が ollama より一律に 0.1 ほど高く出る
  （関係ある文の最小が 0.32→0.44、中央値 0.46→0.59、無関係な問いの1位の最大が 0.3 未満→0.43）。0.3 のままだと無関係な文が全部候補に入り、RRF で全文検索の雑音に負ける（ruri-v3 と同じ型）。
  **llama-server で embeddinggemma を使うなら `HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY=0.43`**。関係ある文の最小 0.44 と無関係の1位の最大 0.43 が接していて余裕は薄い。
- 尺度が違う理由は確かめていない（`--pooling` を外しても値は同じだったので pooling ではない。ollama 側の GGUF の作り〈プーリング後の dense 層の有無など〉の違いを疑っている）。
- ollama 版（`embeddinggemma:300m-qat-q8_0`、待機 627MiB）より llama-embed（371MiB）はメモリが 256MiB 少ない。recall と retain の速さは同じ。

- bge-m3 は `local`（fp32、待機 2.4GiB、recall 0.5s）で測った前日の値と最終1位率が同じ（83%／69%）で、ollama 経由でも順位は変わらず、メモリと速さだけ良くなった。
- granite と embeddinggemma-2 は意味の順位は良いのにスコアが 0.6〜0.7 台に詰まり、既定の足切り 0.3 だと全件が候補に入って RRF で負ける（ruri-v3 と同じ型）。embeddinggemma-2 は 0.66 で 90% まで戻るが、関係ある文の最小 0.69 と無関係の問いの1位の最大 0.67 が接していて薄氷。
- Qwen3-Embedding の 4B は 2 vCPU では1問 1.7 秒で、順位も 0.6B の Q8 を超えなかった。載せるなら ollama の `mem_limit` を 4g に上げる（読み込みで 3.1GiB）。8B（Q4_K_M で 4.7GB）は箱のメモリの線（MemAvailable 1.5GB）に収まらないので測っていない。
- 生データ（bench.json）は当日の scratchpad。前日の `local` 版（ruri-v3、Qwen3 float32 など）の数字は nilkani06 の `.agents/skills/hindsight/SKILL.md`。

## 設定（.env）

| 変数 | 意味 | いまの値 |
|---|---|---|
| `HINDSIGHT_API_LLM_PROVIDER` | 書き込み時の LLM。`none` は抽出なし（原文の塊を保存、API キー不要） | `none` |
| `HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL` | 埋め込みモデル。Hugging Face の名前か `/models/<名前>`（下の「日本語向けの埋め込みモデル」）。Hindsight 既定の `BAAI/bge-small-en-v1.5` は英語専用 | `BAAI/bge-m3`（候補は `/models/ruri-v3-130m`） |
| `HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY` | 意味検索の足切り（コサイン類似度）。モデルの尺度に合わせる。bge-m3 は 0.3、ollama の embeddinggemma も 0.3、ruri-v3 は 0.78 前後、llama-server の embeddinggemma は 0.43 | `0.3` |
| `HINDSIGHT_API_RERANKER_PROVIDER` | 融合後の並べ替え。`rrf` は並べ替えなし（モデルを読まない）。`local` の既定モデルは英語専用 | `rrf` |
| `HINDSIGHT_API_RERANKER_LOCAL_MODEL` | `local` のときのモデル。Hindsight 既定の `cross-encoder/ms-marco-MiniLM-L-6-v2` は英語専用で日本語の順位を壊す。多言語は `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`（約 470MB、recall が 0.13 秒→1.0 秒） | Hindsight 既定 |
| `HINDSIGHT_ENABLE_CP` | 管理 UI（port 9999） | `true` |
| `HINDSIGHT_API_EMBEDDINGS_PROVIDER` | `local`（コンテナ内 SentenceTransformers）か `openai`（OpenAI 互換の口＝ollama-docker） | `local` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL` | `openai` のときの口 | `http://ollama:11434/v1` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL` | `openai` のときのモデル（ollama のタグ） | `embeddinggemma:300m-qat-q8_0` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS` | 出力次元の切り詰め（MRL 対応モデルのみ） | （なし＝モデルの既定） |
| `HINDSIGHT_API_EMBEDDINGS_QUERY_PREFIX`／`PASSAGE_PREFIX` | 問い／文書の接頭辞（`openai`・`tei` 系にだけ効く） | （空） |

埋め込みモデルを次元の違うものに替えるときは、データが入ったままだと起動時に止まる（列の型を変えられない）。
空にするか、export-bank して入れ直す。

## 日本語向けの埋め込みモデル（models/）

Hindsight の `local` プロバイダは SentenceTransformers の `encode_query`／`encode_document` に任せるので、
**モデルが自前で持つ prompts（`config_sentence_transformers.json`）だけ**が接頭辞として付く。
[cl-nagoya/ruri-v3](https://huggingface.co/cl-nagoya/ruri-v3-130m)（問いに「検索クエリ: 」、文書に「検索文書: 」）や
[intfloat/multilingual-e5](https://huggingface.co/intfloat/multilingual-e5-large)（`query: `／`passage: `）は
カードが接頭辞を求めるのに prompts が空なので、そのまま名前で指すと接頭辞なしで動く。

本家は触らず、`models/fetch.sh` で **モデルを `models/<名前>/` に落として prompts を書き足し**、`/models/<名前>` で指す。
公式イメージの python と huggingface_hub を使うので、ホストに Python は要らない。

```sh
models/fetch.sh cl-nagoya/ruri-v3-130m "検索クエリ: " "検索文書: "   # 約 540MB、この箱で 45 秒
# .env に
#   HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=/models/ruri-v3-130m
#   HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY=0.78
docker compose up -d
```

**`HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY` を一緒に上げること。**既定の 0.3 は bge-m3 の尺度で、ruri-v3 は無関係な文にも
コサイン類似度 0.7 台を出す。0.3 のままだと全件が意味検索の候補に入り、順位融合（RRF、k=60 で平ら）で
「全文検索にも引っかかった無関係な文」に負けて、意味検索で1位の文が最終順位で 8〜17 位に落ちる
（2026-10-16 の計測：自作 36 文・関係ある問い 42 本で、最終1位率が 0.3 のとき 33%、0.78 で 90%。
同条件の bge-m3 は 83%）。`models/` は `.gitignore` 済みで、消すときはディレクトリごと消す。

計測した候補と数字（同日・この箱＝aarch64 2 vCPU、LLM なし、リランカー rrf）は nilkani06 の `.agents/skills/hindsight/SKILL.md`。

LLM を入れるときは `HINDSIGHT_API_LLM_PROVIDER` と、そのプロバイダの変数（API キーやモデル名）を `.env` に足す。
一覧は [設定の文書](https://hindsight.vectorize.io/developer/configuration)。
