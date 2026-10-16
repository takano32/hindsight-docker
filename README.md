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
| `HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL` | 埋め込みモデル。既定の `BAAI/bge-small-en-v1.5` は英語専用 | `BAAI/bge-m3` |
| `HINDSIGHT_API_RERANKER_PROVIDER` | 融合後の並べ替え。`rrf` は並べ替えなし（モデルを読まない）。`local` の既定モデルは英語専用 | `rrf` |
| `HINDSIGHT_ENABLE_CP` | 管理 UI（port 9999） | `true` |

埋め込みモデルを次元の違うものに替えるときは、データが入ったままだと起動時に止まる（列の型を変えられない）。
空にするか、export-bank して入れ直す。

LLM を入れるときは `HINDSIGHT_API_LLM_PROVIDER` と、そのプロバイダの変数（API キーやモデル名）を `.env` に足す。
一覧は [設定の文書](https://hindsight.vectorize.io/developer/configuration)。
