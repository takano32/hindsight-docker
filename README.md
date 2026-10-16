# hindsight-docker

[Hindsight](https://github.com/vectorize-io/hindsight) の公式イメージを、postgresql-docker の共有 PostgreSQL に繋いで動かす。
本家リポジトリは fork も改変もしない。版は `.env` の `HINDSIGHT_VERSION`（2026-10-16 時点の最新リリース 1.0.0）。

## 前提

- `~/opt/postgresql-docker` が `up` していること（network `postgresql` と、hindsight のロールとデータベース）。
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

## 設定（.env）

| 変数 | 意味 | いまの値 |
|---|---|---|
| `HINDSIGHT_API_LLM_PROVIDER` | 書き込み時の LLM。`none` は抽出なし（原文の塊を保存、API キー不要） | `none` |
| `HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL` | 埋め込みモデル。Hugging Face の名前か `/models/<名前>`（下の「日本語向けの埋め込みモデル」）。Hindsight 既定の `BAAI/bge-small-en-v1.5` は英語専用 | `BAAI/bge-m3`（候補は `/models/ruri-v3-130m`） |
| `HINDSIGHT_API_SEMANTIC_MIN_SIMILARITY` | 意味検索の足切り（コサイン類似度）。モデルの尺度に合わせる。bge-m3 は 0.3、ruri-v3 は 0.78 前後 | `0.3` |
| `HINDSIGHT_API_RERANKER_PROVIDER` | 融合後の並べ替え。`rrf` は並べ替えなし（モデルを読まない）。`local` の既定モデルは英語専用 | `rrf` |
| `HINDSIGHT_API_RERANKER_LOCAL_MODEL` | `local` のときのモデル。Hindsight 既定の `cross-encoder/ms-marco-MiniLM-L-6-v2` は英語専用で日本語の順位を壊す。多言語は `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`（約 470MB、recall が 0.13 秒→1.0 秒） | Hindsight 既定 |
| `HINDSIGHT_ENABLE_CP` | 管理 UI（port 9999） | `true` |

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
